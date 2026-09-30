#!/usr/bin/env python3
from __future__ import annotations

import argparse
import atexit
from collections import deque
import hashlib
import importlib.metadata
import json
import re
import os
try:
    import pwd  # Unix contest runtime
except ImportError:  # Windows host/dev
    pwd = None  # type: ignore
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
import urllib.request
from urllib.parse import urlparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
import claude_agent_sdk
from arcbench_agent_runtime import AgentRuntime

import sdk_driver


SUBMISSION_DIR = Path(os.environ.get("ARCBENCH_SUBMISSION_DIR", Path(__file__).resolve().parent))
LOCK_PATH = SUBMISSION_DIR / "runtime.lock.json"
RUNTIME_DIR = SUBMISSION_DIR / "runtime"
# Tao contest user-level CLAUDE.md (tracked asset; installed to HOME/.claude + project).
CONTEST_USER_CLAUDE_MD = SUBMISSION_DIR / "contest" / "CLAUDE.md"
_children: list[subprocess.Popen] = []



@dataclass(frozen=True)
class RequirementModule:
    index: int
    total: int
    node_id: str
    name: str
    subtree: dict[str, Any]






@dataclass(frozen=True)
class ClaudeRunResult:
    returncode: int
    is_error: bool
    terminal_reason: str
    subtype: str
    api_error_status: int | None
    tail: str
    skills_loaded: tuple[str, ...] = ()
    mcp_tools_used: tuple[str, ...] = ()
    # v5x G1: in-STEP Write|Edit paths observed by SDK (business-path gate).
    builtin_writes: tuple[str, ...] = ()
    # Wall-clock epoch when this SDK/CLI attempt started (mtime ≥ step_start gate).
    step_started_at: float | None = None
    # v5ab: thrash telemetry for soft supervisor (never marks green).
    thrash_hit: bool = False
    thrash_events: tuple[dict, ...] = ()
    deny_events: tuple[dict, ...] = ()
    thrash_counts: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class FailureClassification:
    retryable: bool
    reason: str








# Official STEP loop (fail-closed). Advance only when artifact/MCP gates pass.
# Skills: official setting_sources+skills wiring only — NOT force-loaded / fail-closed.

# Optional P1 audit STEPs (ARC_ENABLE_MCP_AUDIT_STEPS; default OFF for v5ag thin orchestrator).











RETRYABLE_MARKERS = (
    "connection reset",
    "econnreset",
    "connection refused",
    "temporarily unavailable",
    "service unavailable",
    "gateway timeout",
    "timed out",
    "timeout",
    "upstream",
    "overloaded",
    "rate limit",
    "too many requests",
    "http 429",
    "status 429",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "http 529",
    "status 500",
    "status 502",
    "status 503",
    "status 504",
    "status 529",
    # Autocompact thrash / rapid refill: retry with preserved workspace (self-heal).
    "rapid_refill",
    "autocompact is thrashing",
    "context refilled",
)

NON_RETRYABLE_MARKERS = (
    "invalid api key",
    "invalid_api_key",
    "authentication_error",
    "unauthorized",
    "forbidden",
    "budget_exhausted",
    "budget exhausted",
    "insufficient_quota",
    "invalid_request_error",
    "model not found",
    "model_not_found",
    "unknown model",
    "invalid model",
    "context length exceeded",
)

# v5an/v5ao: soft session ends must NOT fail-closed the whole run mid-pipeline.
# Official v5am GitHub died terminal_reason=blocking_limit after DESIGN; Sheet died max_turns.
# Official v5an both tracks died terminal_reason=rapid_refill_breaker after IMPLEMENT (attempts=3).
# Thin launcher relaunches the next phase (prompt + CC) instead of killing early.
PHASE_SOFT_CONTINUE_REASONS = frozenset({
    "blocking_limit",
    "max_turns",
    "rapid_refill_breaker",
    "rapid_refill",
})


def phase_soft_continue_reason(result: "ClaudeRunResult") -> str | None:
    """Return soft-continue reason if CC ended on a non-fatal session limit."""
    reason = str(result.terminal_reason or "").strip().lower()
    if reason in PHASE_SOFT_CONTINUE_REASONS:
        return reason
    subtype = str(result.subtype or "").strip().lower()
    if subtype in PHASE_SOFT_CONTINUE_REASONS:
        return subtype
    return None


def env_int(name: str, default: int, *, minimum: int = 0, maximum: int = 10_000) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        die(f"{name} must be an integer, got {raw!r}")
    if value < minimum or value > maximum:
        die(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def env_bool(name: str, default: bool = True) -> bool:
    """Parse a boolean env flag. Empty/unset returns default. Default is MCP-enabled."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on", "y"}:
        return True
    if value in {"0", "false", "no", "off", "n"}:
        return False
    die(f"{name} must be a boolean (1/0 true/false), got {raw!r}")




def _anthropic_base_url_from_openai(base_url: str) -> str:
    """Claude Code talks Anthropic Messages API and appends /v1/messages.

    ARC injects OpenAI-compatible OPENAI_BASE_URL (often .../v1). Strip a trailing
    /v1 so we do not request .../v1/v1/messages. Keep the host/path otherwise.
    """
    u = (base_url or "").strip().rstrip("/")
    if u.endswith("/v1"):
        u = u[:-3].rstrip("/")
    return u



def _is_claude_builtin_model_alias(model: str) -> bool:
    """True for Claude Code local allowlist aliases (sonnet/opus/haiku/...)."""
    m = (model or "").strip().lower()
    if not m:
        return False
    # Common Anthropic / Claude Code aliases and versioned ids.
    builtins = {
        "sonnet", "opus", "haiku", "claude",
        "claude-sonnet-4", "claude-opus-4", "claude-haiku-4",
        "claude-3-5-sonnet", "claude-3-5-haiku", "claude-3-opus",
        "claude-3-sonnet", "claude-3-haiku",
    }
    if m in builtins:
        return True
    return m.startswith("claude-")


def claude_cli_model(*, model: str, using_proxy: bool) -> str:
    """Model string passed to `claude --model`.

    Proxy path historically used `sonnet` and remapped upstream.
    Official/no-proxy path must also avoid raw contest ids like deepseek-v4-flash:
    Claude Code 2.1 emits unrecognized_model locally before any API call.
    Contest MODEL is applied via ANTHROPIC_DEFAULT_*_MODEL in apply_official_claude_env.
    """
    if using_proxy:
        return "sonnet"
    if _is_claude_builtin_model_alias(model):
        return model
    return "sonnet"

def apply_official_claude_env(
    env: dict[str, str],
    *,
    base_url: str,
    api_key: str,
    model: str = "",
) -> dict[str, str]:
    """Map ARC-injected OPENAI_* into Claude env the same way as the official CC starter.

    Official starter (`claude_env_from_openai_env`):
      ANTHROPIC_API_KEY = ""
      ANTHROPIC_BASE_URL = OPENAI_BASE_URL (when set; trailing /v1 stripped)
      ANTHROPIC_AUTH_TOKEN = OPENAI_API_KEY (when set)
    No production protocol proxy. Raw MODEL is passed via --model.

    Extra (needed for Claude Code 2.1+ with non-Anthropic contest models such as
    deepseek-v4-flash; matches DeepSeek/OpenRouter CC gateway guidance):
      ANTHROPIC_MODEL / ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL / CLAUDE_CODE_SUBAGENT_MODEL
      CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1
    Without these, CC emits unrecognized_model locally (duration_api_ms=0) before any
    upstream call — the failure mode Smoke v5j hit with anthropic_proxy=false.
    """
    out = env.copy()
    # Prefer auth-token mapping; empty API key so Claude Code does not prefer a stale key.
    out["ANTHROPIC_API_KEY"] = ""
    if base_url:
        out["ANTHROPIC_BASE_URL"] = _anthropic_base_url_from_openai(base_url)
    else:
        out.pop("ANTHROPIC_BASE_URL", None)
    if api_key:
        out["ANTHROPIC_AUTH_TOKEN"] = api_key
    else:
        out.pop("ANTHROPIC_AUTH_TOKEN", None)
    model = (model or "").strip()
    if model:
        out["ANTHROPIC_MODEL"] = model
        out["ANTHROPIC_DEFAULT_OPUS_MODEL"] = model
        out["ANTHROPIC_DEFAULT_SONNET_MODEL"] = model
        out["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = model
        out["CLAUDE_CODE_SUBAGENT_MODEL"] = model
    # Allow non-Anthropic model ids through Claude Code's local allowlist.
    out["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] = "1"
    for stale in (
        "ANTHROPIC_API_KEY_OLD",
        "CLAUDE_CODE_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_CUSTOM_HEADERS",
    ):
        out.pop(stale, None)
    return out


def _arc_maybe_start_anthropic_proxy(base_url: str, api_key: str, model: str) -> subprocess.Popen | None:
    """Claude Messages → OpenAI chat/completions protocol bridge.

    Required for contest deepseek (ARC serves chat/completions; ClaudeSDKClient/
    Claude Code speak Anthropic Messages). Auto-starts when the bundled binary is
    present. Opt-out: ARC_DISABLE_ANTHROPIC_PROXY=1.
    Client auth MUST be ANTHROPIC_API_KEY=arc-local (proxy CLIENT_KEY).
    """
    gateway_bin = SUBMISSION_DIR / "runtime" / "gateway" / "anthropic-proxy"
    if not gateway_bin.is_file() or env_bool("ARC_DISABLE_ANTHROPIC_PROXY", False):
        return None
    gateway_bin.chmod(0o755)
    chat = base_url.rstrip("/")
    if not chat.endswith("/chat/completions"):
        chat = chat + ("/chat/completions" if chat.endswith("/v1") else "/v1/chat/completions")
    artifacts = Path(os.environ.get("ARCBENCH_ARTIFACTS_DIR", "/tmp/arcbench-artifacts"))
    artifacts.mkdir(parents=True, exist_ok=True)
    log_path = artifacts / "anthropic-proxy.log"
    env = os.environ.copy()
    env.update({
        "ANTHROPIC_PROXY_LISTEN_ADDR": "127.0.0.1:8787",
        "ANTHROPIC_PROXY_UPSTREAM_URL": chat,
        "ANTHROPIC_PROXY_UPSTREAM_API_KEY": api_key,
        "ANTHROPIC_PROXY_DEFAULT_MODEL": model,
        "ANTHROPIC_PROXY_FORCE_MODEL": "1",
        "ANTHROPIC_PROXY_TOOL_FORMAT": "native",
        "ANTHROPIC_PROXY_CLIENT_KEY": "arc-local",
        "ANTHROPIC_PROXY_LOG_LEVEL": os.environ.get("ARC_GATEWAY_LOG_LEVEL", "info"),
        "ANTHROPIC_PROXY_REQUEST_TIMEOUT_SEC": "600",
    })
    logf = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen([str(gateway_bin), "serve"], cwd=str(SUBMISSION_DIR), env=env, stdout=logf, stderr=subprocess.STDOUT)
    import urllib.request
    deadline = time.time() + 20
    last_err = None
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"anthropic-proxy exited early rc={proc.returncode}; see {log_path}")
        try:
            with urllib.request.urlopen("http://127.0.0.1:8787/health", timeout=1) as resp:
                if resp.status < 500:
                    print(json.dumps({"event":"anthropic_proxy","action":"started","listen":"127.0.0.1:8787","upstream_chat":chat,"upstream_host":upstream_host(base_url),"client_key":"arc-local","log":str(log_path)}, ensure_ascii=False), flush=True)
                    return proc
        except Exception as exc:
            last_err = exc
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError(f"anthropic-proxy health check failed: {last_err}")


def mcp_enabled() -> bool:
    """GSC MCP is ON by default. Set ARC_ENABLE_MCP=0 only as an explicit escape hatch."""
    return env_bool("ARC_ENABLE_MCP", True)


# GSC MCP exposes ~70 tools (~63k schema tokens). Autocompact then re-injects
# them and historically tripped rapid_refill_breaker. Keep MCP ON; advertise P0+P1 via
# allowedTools, and strip the rest with --disallowedTools (inventory − allowlist).
# Canonical inventory: MCP_TOOLS_INVENTORY.md / MCP_FULL_UPGRADE_PLAN.md §10.

GSC_MCP_INVENTORY_SHORT_NAMES: tuple[str, ...] = (
    # PRD/SPEC (10)
    "prd",
    "prd_govern",
    "spec_read",
    "spec_write",
    "spec_govern",
    "spec_exchange",
    "spec_migrate",
    "spec_similarity",
    "grok_md_migrate",
    "trace",
    # State/gates (8)
    "state_read",
    "state_update",
    "commit_gate",
    "workflow_guard",
    "worktree_guard",
    "artifact_read",
    "artifact_grep",
    "cache_stats",
    # Arch/code (11)
    "architect",
    "discoverer",
    "solver",
    "arch_insight",
    "search_code",
    "refactor_code",
    "format_code",
    "trace_failure",
    "debug_process",
    "debug_binary",
    "read_image",
    # KB (5)
    "kb_query",
    "kb_inject",
    "kb_index_check",
    "kb_submit",
    "kb_build",
    # Account (1)
    "account_manage",
    # Design (5)
    "design_asset",
    "design_capture",
    "design_compose",
    "design_style",
    "design_audit",
    # Pipeline (1)
    "pipeline",
    # Sessions (3)
    "session_list",
    "session_read",
    "session_search",
    # Browser (26)
    "lifecycle",
    "navigate",
    "go_back",
    "go_forward",
    "reload",
    "close",
    "tabs",
    "click",
    "hover",
    "type",
    "press_key",
    "select_option",
    "fill_form",
    "drag",
    "drop",
    "file_upload",
    "handle_dialog",
    "snapshot",
    "take_screenshot",
    "wait_for",
    "evaluate",
    "console_messages",
    "network_requests",
    "network_request",
    "resize",
    "query",
)

# Never in default allowlist (Tao explicit opt-in via ARC_MCP_ALLOWED_TOOLS only).
GSC_MCP_NEVER_DEFAULT: frozenset[str] = frozenset({"account_manage", "debug_binary"})

# Optional P2 tools (env ARC_MCP_P2_TOOLS=1 or comma list). Not in default Official.
GSC_MCP_P2_OPTIONAL_TOOLS: tuple[str, ...] = (
    "pipeline",
    "session_list",
    "session_read",
    "session_search",
    "kb_submit",
    "kb_build",
    "spec_exchange",
    "spec_migrate",
    "grok_md_migrate",
    "worktree_guard",
    "cache_stats",
    "debug_process",
    "click",
    "type",
    "fill_form",
    "evaluate",
    "network_requests",
    "resize",
    "tabs",
    "reload",
)

# v5q full-landing = plan §5.1-A + §5.1-B. Never include account_manage / debug_binary.
DEFAULT_GSC_MCP_ALLOWED_TOOLS = (
    # §5.1-A (P0)
    "prd",
    "prd_govern",
    "spec_read",
    "spec_write",
    "spec_govern",
    "state_read",
    "state_update",
    "artifact_read",
    "artifact_grep",
    "commit_gate",
    "workflow_guard",
    "architect",
    "discoverer",
    "search_code",
    "read_image",
    "design_style",
    "design_asset",
    "lifecycle",
    "query",
    # §5.1-B (P1)
    "trace",
    "spec_similarity",
    "arch_insight",
    "kb_query",
    "kb_inject",
    "kb_index_check",
    "refactor_code",
    "format_code",
    "trace_failure",
    "solver",
    "design_audit",
    "design_capture",
    "design_compose",
    "navigate",
    "snapshot",
    "take_screenshot",
    "wait_for",
    "console_messages",
)




def gsc_mcp_allowed_tools() -> list[str]:
    """MCP tool allowlist (short names). Override with ARC_MCP_ALLOWED_TOOLS=a,b,c.

    Default = §5.1-A+B. ARC_MCP_P2_TOOLS=1 adds P2 optional set (still excludes
    account_manage/debug_binary unless explicitly listed in ARC_MCP_ALLOWED_TOOLS).
    """
    raw = os.environ.get("ARC_MCP_ALLOWED_TOOLS", "").strip()
    if raw:
        tools = [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]
        if tools:
            return tools
    tools = list(DEFAULT_GSC_MCP_ALLOWED_TOOLS)
    p2_raw = os.environ.get("ARC_MCP_P2_TOOLS", "").strip()
    if p2_raw and p2_raw.lower() not in {"0", "false", "no", "off"}:
        if p2_raw.lower() in {"1", "true", "yes", "on"}:
            extra = list(GSC_MCP_P2_OPTIONAL_TOOLS)
        else:
            extra = [part.strip() for part in p2_raw.replace(";", ",").split(",") if part.strip()]
        for name in extra:
            if name in GSC_MCP_NEVER_DEFAULT:
                continue
            if name not in tools and name in GSC_MCP_INVENTORY_SHORT_NAMES:
                tools.append(name)
    # Belt-and-suspenders: never leak never-default into the generated default list.
    return [t for t in tools if t not in GSC_MCP_NEVER_DEFAULT]


# v5x G3 legacy narrow list — v5ah does NOT apply this to disallowed/allow (满配 kept).
# Kept for tests/docs only; run_claude_via_sdk ignores it for MCP wiring.
IMPLEMENT_DEGRADED_MCP_ALLOW: tuple[str, ...] = (
    "search_code",
    "spec_read",
    "state_read",
    "artifact_read",
    "artifact_grep",
)

DEGRADED_SYSTEM_APPEND = """
DEGRADED MODE (rapid_refill self-heal — MCP stays ON + 满配; Skills stay all; v5am: no thrash kill gate):
- Immediately Write or Edit business code under frontend/ / backend/ / src/.
- Do NOT re-call identical MCP reads (spec_read/state_read/artifact_read) with the same args.
- Do NOT re-load the same Skill. At most one Skill invocation if needed, then Write.
- Prefer: search_code once → Write skeleton → stop thrashing on reads.
- MCP allowlist is NOT narrowed on degrade (v5ah full perception).
- Harness will NOT deny Read / identical MCP via PreToolUse (v5am observe-only).
""".strip()


# v5ac: force early Write skeleton on IMPLEMENT (business path) before thrash.
IMPLEMENT_EARLY_WRITE_APPEND = """
IMPLEMENT LOOP (v5ai — Agent owns coding+test; design already finished):
1. Write|Edit business code under frontend/src, backend/src, or src/.
2. Run tests (vitest/npm test) as you go; fix failures; repeat. Mid-dev tests ARE allowed.
3. Soft: design_* MCP tools if UI needs them (no separate pages STEP).
4. Skills only via official Skill tool (setting_sources/skills=all) — never Read SKILL.md.
5. Do NOT thrash identical MCP reads; prefer Write → test → fix.
""".strip()








def mcp_short_name(tool: str) -> str:
    """mcp__arch__prd -> prd; bare short names pass through."""
    if not tool:
        return ""
    return tool.split("__")[-1]


def mcp_tools_matching(used: tuple[str, ...] | list[str], *shorts: str) -> list[str]:
    want = set(shorts)
    return [t for t in used if mcp_short_name(t) in want]





def prepared_gsc_mcp_tools() -> list[str]:
    """Prepared MCP inventory the agent must perceive (P0+P1 default allow, no never-default)."""
    return list(gsc_mcp_allowed_tools())


def gsc_mcp_disallowed_tool_names(*, prefixed: bool = True, allow_override: list[str] | tuple[str, ...] | None = None) -> list[str]:
    """Disallowed = inventory − allowlist, but NEVER hide prepared (P0+P1) tools.

    v5ah: prepared MCP must stay 100% perceptible. allow_override may narrow
    *guidance* historically, but prepared short names stay out of disallowed.
    Never-default (account_manage/debug_binary) always denied.
    """
    allowed = set(allow_override if allow_override is not None else gsc_mcp_allowed_tools())
    # Hard guarantee: prepared P0+P1 always remain allowed (满配 perception).
    prepared = set(DEFAULT_GSC_MCP_ALLOWED_TOOLS) - set(GSC_MCP_NEVER_DEFAULT)
    allowed |= prepared
    denied: list[str] = []
    for name in GSC_MCP_INVENTORY_SHORT_NAMES:
        if name in GSC_MCP_NEVER_DEFAULT:
            denied.append(name)
            continue
        if name in allowed:
            continue
        denied.append(name)
    for name in sorted(GSC_MCP_NEVER_DEFAULT):
        if name not in denied:
            denied.append(name)
    if prefixed:
        return [f"mcp__arch__{n}" for n in denied]
    return denied


def n_mcp_disallowed_prepared_leak(*, allow_override: list[str] | tuple[str, ...] | None = None) -> int:
    """Count of prepared tools incorrectly present in disallowed (must be 0)."""
    prepared = set(DEFAULT_GSC_MCP_ALLOWED_TOOLS) - set(GSC_MCP_NEVER_DEFAULT)
    denied = set(gsc_mcp_disallowed_tool_names(prefixed=False, allow_override=allow_override))
    return len(prepared & denied)


def builtin_disallowed_tools_csv() -> str:
    """Non-MCP Claude builtins we always ban (Agent/Task/...); MCP deny list appended separately."""
    return (
        "Agent,Task,WebSearch,WebFetch,"
        "CronCreate,CronDelete,CronList,NotebookEdit,"
        "EnterWorktree,ExitWorktree,ListAgents,"
        "ScheduleWakeup,SendMessage,Workflow,DesignSync,ReportFindings"
    )


def claude_disallowed_tools_csv() -> str:
    """Builtin bans + auto MCP disallowed (inventory − allowlist)."""
    mcp_deny = gsc_mcp_disallowed_tool_names(prefixed=True)
    return builtin_disallowed_tools_csv() + ("," + ",".join(mcp_deny) if mcp_deny else "")


def write_gsc_mcp_config(gsc_dir: Path, dest_dir: Path) -> Path:
    """Write a Claude --mcp-config that points at the packaged GSC bootstrap.

    Includes allowedTools (§5.1-A+B by default). Pair with claude_disallowed_tools_csv()
    so init n_mcp shrinks even when plugin MCP still advertises ~70 tools.
    MCP stays connected (WaitForMcpServers still allowed).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    bootstrap = gsc_dir / "mcp" / "src" / "bootstrap.mjs"
    require_file(bootstrap, "GSC MCP bootstrap")
    config_path = dest_dir / "gsc-mcp.json"
    allowed = gsc_mcp_allowed_tools()
    denied = gsc_mcp_disallowed_tool_names(prefixed=False)
    payload = {
        "mcpServers": {
            "arch": {
                "command": "node",
                "args": [str(bootstrap)],
                "env": {
                    "CLAUDE_PLUGIN_ROOT": str(gsc_dir),
                    "GSC_ARC_PACKAGED_RUNTIME": "1",
                    "GSC_RUNTIME_SERVER_BIN": str(gsc_dir / "bin" / "gsc-spec-server"),
                },
                "allowedTools": allowed,
            }
        }
    }
    config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    meta_path = dest_dir / "gsc-mcp-allowlist.json"
    meta_path.write_text(
        json.dumps(
            {
                "allowed_tools": allowed,
                "disallowed_short": denied,
                "disallowed_prefixed": [f"mcp__arch__{n}" for n in denied],
                "n_allowed": len(allowed),
                "n_disallowed": len(denied),
                "n_inventory": len(GSC_MCP_INVENTORY_SHORT_NAMES),
                "never_default": sorted(GSC_MCP_NEVER_DEFAULT),
                "audit_steps": False,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "event": "gsc_mcp_config_written",
                "path": str(config_path),
                "n_allowed": len(allowed),
                "n_disallowed": len(denied),
                "audit_steps": False,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return config_path

def claude_mcp_cli_args(*, enabled: bool, mcp_config: Path | None) -> list[str]:
    """Less-destructive MCP thrash mitigation vs v4's total ban.

    When enabled (default): --mcp-config + --strict-mcp-config loads ONLY GSC.
    Claude -p waits for MCP connect up to MCP_TIMEOUT before the first turn.
    When disabled: --strict-mcp-config alone ignores plugin MCP (escape hatch).
    Never writes CLAUDE.md / prompt bans that forbid MCP.
    """
    if enabled:
        if mcp_config is None:
            raise ValueError("mcp_config is required when MCP is enabled")
        return ["--mcp-config", str(mcp_config), "--strict-mcp-config"]
    return ["--strict-mcp-config"]


def configured_base_urls(primary: str) -> list[str]:
    urls = [primary.strip()]
    raw = os.environ.get("ARC_FALLBACK_BASE_URLS", "").strip()
    if raw:
        for candidate in raw.replace(";", ",").split(","):
            value = candidate.strip()
            if not value or value in urls:
                continue
            parsed = urlparse(value)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                die(f"invalid ARC_FALLBACK_BASE_URLS entry: {value!r}")
            urls.append(value)
    return urls


def base_url_for_attempt(base_urls: list[str], attempt: int) -> str:
    if not base_urls:
        raise ValueError("base_urls must not be empty")
    return base_urls[min(max(attempt, 1) - 1, len(base_urls) - 1)]

def upstream_host(base_url: str) -> str:
    try:
        parsed = urlparse(base_url)
        return parsed.hostname or parsed.netloc or "<unknown>"
    except Exception:
        return "<unknown>"


def parse_terminal_result(lines: list[str]) -> tuple[bool, str, str, int | None]:
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except Exception:
            continue
        if payload.get("type") != "result":
            continue
        raw_status = payload.get("api_error_status")
        try:
            api_error_status = int(raw_status) if raw_status is not None else None
        except (TypeError, ValueError):
            api_error_status = None
        return (
            bool(payload.get("is_error")),
            str(payload.get("terminal_reason") or ""),
            str(payload.get("subtype") or ""),
            api_error_status,
        )
    return False, "", "", None


def classify_claude_failure(result: ClaudeRunResult) -> FailureClassification:
    if result.returncode == 0 and not result.is_error:
        return FailureClassification(False, "success")

    text = "\n".join(
        part for part in (result.terminal_reason, result.subtype, result.tail) if part
    ).lower()

    for marker in NON_RETRYABLE_MARKERS:
        if marker in text:
            return FailureClassification(False, f"non-retryable:{marker}")

    if result.api_error_status in (401, 403, 404):
        return FailureClassification(False, f"non-retryable:http_{result.api_error_status}")
    if result.api_error_status in (408, 429, 500, 502, 503, 504, 529):
        return FailureClassification(True, f"retryable:http_{result.api_error_status}")

    for marker in RETRYABLE_MARKERS:
        if marker in text:
            reason = f"retryable:{marker}"
            # v5x G3 / v5am: signal degrade restart on next attempt (no refill budget kill).
            if marker == "rapid_refill":
                reason = "retryable:rapid_refill_needs_degrade"
            return FailureClassification(True, reason)

    if result.terminal_reason.lower() == "api_error" and result.api_error_status is None:
        return FailureClassification(True, "retryable:api_error")
    return FailureClassification(False, "non-retryable:process_failure")


def retry_delay_seconds(retry_index: int, base_seconds: int, max_seconds: int) -> int:
    if retry_index <= 0:
        return 0
    return min(max_seconds, base_seconds * (2 ** (retry_index - 1)))


def execute_with_retry(
    run_attempt,
    *,
    max_retries: int,
    base_seconds: int,
    max_seconds: int,
    on_retry=None,
    sleep_fn=time.sleep,
) -> tuple[ClaudeRunResult, int]:
    """Retry retryable CC failures. v5am: NO rapid_refill_breaker budget kill gate.
    v5ao: first rapid_refill* hit returns immediately (no 3-retry burn); soft-continue
    mid-pipeline relaunches the next phase instead of exit 1.

    Official v5al died on terminal_reason=rapid_refill_breaker after Read-streak
    PreToolUse denies. Official v5an burned max_retries then fail-closed on IMPLEMENT.
    """
    total_attempts = max_retries + 1
    last_result: ClaudeRunResult | None = None
    for attempt in range(1, total_attempts + 1):
        result = run_attempt(attempt)
        last_result = result
        classification = classify_claude_failure(result)
        if not (result.returncode != 0 or result.is_error):
            return result, attempt
        if "rapid_refill" in classification.reason:
            print(
                json.dumps(
                    {
                        "event": "rapid_refill_retryable",
                        "attempt": attempt,
                        "max_attempts": total_attempts,
                        "classification": classification.reason,
                        "terminal_reason": result.terminal_reason or "unknown",
                        "note": (
                            "v5ao: first rapid_refill* hit returns without burning "
                            "max_retries; soft-continue handles mid-pipeline"
                        ),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            # v5ao: do not consume remaining degrade retries — hand off to soft-continue.
            return result, attempt
        if not classification.retryable or attempt >= total_attempts:
            return result, attempt
        delay = retry_delay_seconds(attempt, base_seconds, max_seconds)
        if on_retry is not None:
            on_retry(attempt, result, classification, delay)
        sleep_fn(delay)
    assert last_result is not None
    return last_result, total_attempts



def run_claude_via_sdk(
    *,
    prompt: str,
    output_dir: Path,
    model: str,
    claude_bin: Path | None,
    gsc_dir: Path | None,
    mcp_config: Path | None,
    enable_mcp: bool,
    attempt_env: dict[str, str],
    skills_dir: Path | None,
    max_budget_usd: str | float | None,
    max_turns: int | None = None,
    step_id: str | None = None,
    degrade_mode: bool = False,
) -> ClaudeRunResult:
    """Primary contest driver: ClaudeSDKClient (+ anthropic-proxy when active)."""
    step_started_at = time.time()
    plugins = sdk_driver.gsc_plugins(gsc_dir)
    mcp_servers = sdk_driver.gsc_mcp_servers(
        gsc_dir=gsc_dir, mcp_config=mcp_config, enable_mcp=enable_mcp
    )
    budget = None
    if max_budget_usd not in (None, ""):
        try:
            budget = float(max_budget_usd)
        except (TypeError, ValueError):
            budget = None
    using_proxy = (
        attempt_env.get("ANTHROPIC_API_KEY") == "arc-local"
        or (attempt_env.get("ANTHROPIC_BASE_URL") or "").startswith("http://127.0.0.1:8787")
    )
    # Ensure OPENAI_* visible to claude_env_from_openai_env inside the turn (no-proxy path).
    prev = {k: os.environ.get(k) for k in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "MODEL", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")}
    try:
        sdk_env = {k: v for k, v in attempt_env.items() if isinstance(v, str)}
        if using_proxy:
            # Bridge: Claude speaks Messages to local proxy; proxy FORCE_MODELs to contest id.
            # Local CC allowlist needs a builtin alias (sonnet); do NOT remap OPENAI_* over API key.
            sdk_env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:8787"
            sdk_env["ANTHROPIC_API_KEY"] = "arc-local"
            for _k in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY_OLD", "CLAUDE_CODE_API_KEY"):
                sdk_env.pop(_k, None)
            os.environ["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:8787"
            os.environ["ANTHROPIC_API_KEY"] = "arc-local"
            os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
            os.environ["OPENAI_API_KEY"] = "arc-local"
            os.environ["OPENAI_BASE_URL"] = "http://127.0.0.1:8787"
            os.environ["MODEL"] = model
            sdk_model = "sonnet"
            sdk_env = sdk_driver.apply_contest_model_env(sdk_env, "sonnet")
            apply_openai_env = False
        else:
            if attempt_env.get("OPENAI_API_KEY"):
                os.environ["OPENAI_API_KEY"] = attempt_env["OPENAI_API_KEY"]
            if attempt_env.get("OPENAI_BASE_URL"):
                os.environ["OPENAI_BASE_URL"] = attempt_env["OPENAI_BASE_URL"]
            os.environ["MODEL"] = model
            sdk_model = sdk_driver.sdk_model_for_options(model)
            sdk_env = sdk_driver.apply_contest_model_env(sdk_env, model)
            apply_openai_env = True
        turns = (
            int(max_turns)
            if max_turns is not None
            else sdk_driver.max_turns_for_step(step_id)
        )
        system_append = sdk_driver.contest_system_prompt_append(skills_dir)
        # v5ac: always push early Write skeleton on IMPLEMENT (independent of degrade).
        if (step_id or "") == "implement":
            system_append = system_append + "\n\n" + IMPLEMENT_EARLY_WRITE_APPEND
        if degrade_mode:
            # v5ah: keep full MCP + Skills; only append behavioral guidance (no allow narrow).
            system_append = system_append + "\n\n" + DEGRADED_SYSTEM_APPEND
            print(
                json.dumps(
                    {
                        "event": "rapid_refill_degrade_restart",
                        "step_id": step_id,
                        "mcp_allow_override": None,
                        "note": "full MCP满配 kept; degraded system append only; Skills=all; MCP ON; v5am no thrash kill",
                        "classification": "retryable:rapid_refill_needs_degrade",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        extra_deny = None
        if enable_mcp:
            extra_deny = gsc_mcp_disallowed_tool_names(prefixed=True)
        # Fail loudly if MCP off without explicit env escape hatch.
        if not enable_mcp:
            raw_mcp = (os.environ.get("ARC_ENABLE_MCP") or "").strip().lower()
            if raw_mcp not in {"0", "false", "no", "off"}:
                return ClaudeRunResult(
                    returncode=2,
                    is_error=True,
                    terminal_reason="cc_config_incomplete",
                    subtype="mcp_disabled_without_env",
                    api_error_status=None,
                    tail="enable_mcp=False but ARC_ENABLE_MCP not explicitly disabling",
                    step_started_at=step_started_at,
                )
        options = sdk_driver.build_agent_options(
            cwd=output_dir,
            model=sdk_model,
            mcp_servers=mcp_servers,
            plugins=plugins,
            cli_path=claude_bin,
            env=sdk_env,
            system_prompt_append=system_append,
            max_turns=turns,
            max_budget_usd=budget,
            permission_mode="acceptEdits",
            extra_disallowed_tools=extra_deny,
            step_id=step_id,
        )
        cfg = assert_cc_full_config(
            options=options,
            enable_mcp=enable_mcp,
            plugins=plugins,
            home_dir=Path(sdk_env.get("HOME") or os.environ.get("HOME") or ""),
            output_dir=output_dir,
            mcp_servers=mcp_servers,
        )
        print(
            json.dumps(
                {
                    "event": "sdk_turn_start",
                    "driver": "ClaudeSDKClient",
                    "contest_model": model,
                    "sdk_model": sdk_model,
                    "anthropic_proxy": using_proxy,
                    "anthropic_base_url": sdk_env.get("ANTHROPIC_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL"),
                    "permission_mode": "acceptEdits",
                    "max_turns": turns,
                    "step_id": step_id,
                    "mcp_enabled": enable_mcp,
                    "mcp_servers": str(mcp_servers) if mcp_servers is not None else None,
                    "plugins": plugins,
                    "cli_path": str(claude_bin) if claude_bin else None,
                    "setting_sources": getattr(options, "setting_sources", None),
                    "skills": getattr(options, "skills", None),
                    "allowed_tools_has_Skill": "Skill" in (getattr(options, "allowed_tools", None) or []),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if cfg.get("ok") is False:
            return ClaudeRunResult(
                returncode=2,
                is_error=True,
                terminal_reason="cc_config_incomplete",
                subtype=",".join(cfg.get("issues") or []) or "cc_config_incomplete",
                api_error_status=None,
                tail=json.dumps(cfg, ensure_ascii=False),
                step_started_at=step_started_at,
            )
        turn = sdk_driver.run_sdk_turn(prompt=prompt, options=options, apply_openai_env=apply_openai_env)
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return ClaudeRunResult(
        returncode=turn.returncode,
        is_error=turn.is_error,
        terminal_reason=turn.terminal_reason,
        subtype=turn.subtype,
        api_error_status=turn.api_error_status,
        tail=turn.tail,
        skills_loaded=turn.skills_loaded,
        mcp_tools_used=turn.mcp_tools_used,
        builtin_writes=getattr(turn, "builtin_writes", ()) or (),
        step_started_at=step_started_at,
        thrash_hit=bool(getattr(turn, "thrash_hit", False)),
        thrash_events=tuple(getattr(turn, "thrash_events", ()) or ()),
        deny_events=tuple(getattr(turn, "deny_events", ()) or ()),
        thrash_counts=tuple(getattr(turn, "thrash_counts", ()) or ()),
    )


def run_claude_streaming(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    preexec_fn,
) -> ClaudeRunResult:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        preexec_fn=preexec_fn,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    _children.append(process)
    stdout_tail: deque[str] = deque(maxlen=300)
    stderr_tail: deque[str] = deque(maxlen=300)
    mcp_tool_counts: dict[str, int] = {}
    skill_loads: list[str] = []
    mcp_lock = threading.Lock()

    def _tool_blocks_from_payload(payload: dict) -> list[dict]:
        blocks: list[dict] = []
        content = None
        if isinstance(payload.get("message"), dict):
            content = payload["message"].get("content")
        if content is None:
            content = payload.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    blocks.append(block)
        tool_use = payload.get("tool_use") or payload.get("toolUse")
        if isinstance(tool_use, dict):
            blocks.append(tool_use)
        if isinstance(payload.get("name"), str):
            blocks.append(payload)
        return blocks

    def note_tools_from_line(line: str) -> None:
        """Audit MCP tool_use + optional Skill tool telemetry (not fail-closed)."""
        if "mcp__" not in line and "Skill" not in line:
            return
        try:
            payload = json.loads(line)
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            return

        for block in _tool_blocks_from_payload(payload):
            name = block.get("name")
            if not isinstance(name, str):
                continue
            if name.startswith("mcp__"):
                with mcp_lock:
                    mcp_tool_counts[name] = mcp_tool_counts.get(name, 0) + 1
                    count = mcp_tool_counts[name]
                print(
                    json.dumps(
                        {"event": "mcp_tool_use", "tool": name, "count": count},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            elif name == "Skill":
                inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                skill_name = (
                    inp.get("skill")
                    or inp.get("name")
                    or inp.get("skill_name")
                    or inp.get("skillName")
                    or ""
                )
                skill_name = str(skill_name).strip()
                if not skill_name:
                    continue
                skill_name = skill_name.rstrip("/").split("/")[-1]
                with mcp_lock:
                    skill_loads.append(skill_name)
                print(
                    json.dumps(
                        {"event": "skill_loaded", "skill": skill_name, "via": "skill_tool"},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

    def pump(stream, sink, tail: deque[str], *, watch_tools: bool = False) -> None:
        if stream is None:
            return
        try:
            for line in iter(stream.readline, ""):
                tail.append(line.rstrip("\n"))
                sink.write(line)
                sink.flush()
                if watch_tools:
                    note_tools_from_line(line)
        finally:
            stream.close()

    threads = [
        threading.Thread(
            target=pump,
            args=(process.stdout, sys.stdout, stdout_tail),
            kwargs={"watch_tools": True},
            daemon=True,
        ),
        threading.Thread(target=pump, args=(process.stderr, sys.stderr, stderr_tail), daemon=True),
    ]
    for thread in threads:
        thread.start()
    returncode = process.wait()
    for thread in threads:
        thread.join(timeout=5.0)

    if mcp_tool_counts:
        print(
            json.dumps(
                {
                    "event": "mcp_tool_use_summary",
                    "tools": dict(sorted(mcp_tool_counts.items())),
                    "total": sum(mcp_tool_counts.values()),
                    "note": "Official audit: MCP role/value from observed tool_use",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    if skill_loads:
        print(
            json.dumps(
                {
                    "event": "skill_load_summary",
                    "skills": list(dict.fromkeys(skill_loads)),
                    "total": len(skill_loads),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    stdout_lines = list(stdout_tail)
    is_error, terminal_reason, subtype, api_error_status = parse_terminal_result(stdout_lines)
    combined_tail = "\n".join((stdout_lines + list(stderr_tail))[-300:])
    return ClaudeRunResult(
        returncode=returncode,
        is_error=is_error,
        terminal_reason=terminal_reason,
        subtype=subtype,
        api_error_status=api_error_status,
        tail=combined_tail,
        skills_loaded=tuple(dict.fromkeys(skill_loads)),
        mcp_tools_used=tuple(sorted(mcp_tool_counts.keys())),
        builtin_writes=(),
        step_started_at=time.time(),
    )


def safe_node_id(node_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in node_id)

































def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ARC-Bench Factory agent: original Claude Code + GSC")
    parser.add_argument("requirement_path", nargs="?", default=os.environ.get("ARCBENCH_TASK_DIR", "requirements"))
    parser.add_argument("--output-dir", default=os.environ.get("ARCBENCH_OUTPUT_DIR", "."))
    parser.add_argument("--type", dest="task_type", default=os.environ.get("ARCBENCH_TASK_TYPE", "web"))
    return parser.parse_args()



def die(message: str, code: int = 2) -> None:
    print(f"[arc-claude-gsc] ERROR: {message}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def require_file(path: Path, label: str) -> None:
    if not path.is_file():
        die(f"{label} not found: {path}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path, expected: str, label: str) -> None:
    actual = sha256_file(path)
    if actual != expected:
        die(f"{label} SHA-256 mismatch: expected {expected}, got {actual}")


def load_lock() -> dict:
    require_file(LOCK_PATH, "runtime lock")
    try:
        return json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        die(f"invalid runtime.lock.json: {exc}")


def download_verified(url: str, destination: Path, expected: str, label: str) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and sha256_file(destination) == expected:
        return destination

    max_attempts = env_int("ARC_RUNTIME_DOWNLOAD_ATTEMPTS", 4, minimum=1, maximum=10)
    temp = destination.with_name(destination.name + ".part")
    for attempt in range(1, max_attempts + 1):
        offset = temp.stat().st_size if temp.is_file() else 0
        headers = {"User-Agent": "arc-claude-gsc/Factory26"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        print(
            json.dumps(
                {
                    "event": "runtime_download",
                    "label": label,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "resume_bytes": offset,
                    "upstream_host": upstream_host(url),
                }
            ),
            flush=True,
        )
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                append = offset > 0 and getattr(response, "status", None) == 206
                mode = "ab" if append else "wb"
                if not append and offset:
                    offset = 0
                with temp.open(mode) as stream:
                    shutil.copyfileobj(response, stream, length=1024 * 1024)
        except Exception as exc:
            if attempt >= max_attempts:
                die(f"failed to download {label} after {attempt} attempt(s): {exc}")
            delay = retry_delay_seconds(attempt, 2, 20)
            print(
                json.dumps(
                    {
                        "event": "runtime_download_retry",
                        "label": label,
                        "attempt": attempt,
                        "sleep_seconds": delay,
                        "error": type(exc).__name__,
                    }
                ),
                flush=True,
            )
            time.sleep(delay)
            continue

        actual = sha256_file(temp)
        if actual == expected:
            temp.replace(destination)
            return destination

        # A complete response with the wrong digest is not safe to resume.
        temp.unlink(missing_ok=True)
        if attempt >= max_attempts:
            die(f"{label} SHA-256 mismatch after {attempt} attempt(s): expected {expected}, got {actual}")
        delay = retry_delay_seconds(attempt, 2, 20)
        print(
            json.dumps(
                {
                    "event": "runtime_download_retry",
                    "label": label,
                    "attempt": attempt,
                    "sleep_seconds": delay,
                    "error": "sha256_mismatch",
                }
            ),
            flush=True,
        )
        time.sleep(delay)

    raise AssertionError("unreachable")


def extract_gsc_payload(payload: Path, zstd_bin: Path, expected: str, cache_root: Path) -> Path:
    target = cache_root / "gsc"
    marker = target / ".arc-payload-sha256"
    server = target / "bin" / "gsc-spec-server"
    bootstrap = target / "mcp" / "src" / "bootstrap.mjs"
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == expected and server.is_file() and bootstrap.is_file():
        return target

    temp = cache_root / f".gsc-{os.getpid()}.tmp"
    shutil.rmtree(temp, ignore_errors=True)
    temp.mkdir(parents=True)
    zstd = subprocess.Popen([str(zstd_bin), "-dc", str(payload)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert zstd.stdout is not None
    tar = subprocess.run(
        ["tar", "-xf", "-", "-C", str(temp)],
        stdin=zstd.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    zstd.stdout.close()
    zstd_stderr = (zstd.stderr.read() if zstd.stderr else b"").decode("utf-8", "replace")
    zstd_rc = zstd.wait()
    if zstd_rc != 0 or tar.returncode != 0:
        shutil.rmtree(temp, ignore_errors=True)
        die(
            "failed to extract GSC runtime: "
            f"zstd={zstd_rc} {zstd_stderr[-500:]!r}; "
            f"tar={tar.returncode} {tar.stderr[-500:]!r}"
        )

    unpacked = temp / "plugin-final"
    if not unpacked.is_dir():
        shutil.rmtree(temp, ignore_errors=True)
        die("GSC runtime archive is missing plugin-final/")
    (unpacked / ".arc-payload-sha256").write_text(expected + "\n", encoding="utf-8")
    shutil.rmtree(target, ignore_errors=True)
    unpacked.rename(target)
    shutil.rmtree(temp, ignore_errors=True)
    return target


def bundled_claude_code(lock: dict) -> Path:
    sdk_version = importlib.metadata.version("claude-agent-sdk")
    sdk_root = Path(claude_agent_sdk.__file__).resolve().parent
    binary = sdk_root / "_bundled" / "claude"
    require_file(binary, f"Claude Code bundled by claude-agent-sdk {sdk_version}")
    verify_sha256(binary, lock["claudeCode"]["binarySha256"], "Claude Code bundled by claude-agent-sdk")
    binary.chmod(0o755)
    print(f"[arc-claude-gsc] using claude-agent-sdk {sdk_version} bundled Claude Code: {binary}", flush=True)
    return binary


def prepare_runtime(lock: dict, artifacts_dir: Path) -> tuple[Path, Path]:
    cache_root = artifacts_dir / "runtime"
    cache_root.mkdir(parents=True, exist_ok=True)

    zstd_info = lock["zstd"]
    zstd_url = (
        f"https://github.com/putao520/arc-claude-gsc/releases/download/"
        f"{zstd_info['releaseTag']}/{zstd_info['asset']}"
    )
    zstd_bin = download_verified(
        zstd_url,
        cache_root / "bin" / "zstd",
        zstd_info["sha256"],
        "zstd helper",
    )
    zstd_bin.chmod(0o755)

    gsc_info = lock["gsc"]
    gsc_url = (
        f"https://github.com/putao520/arc-claude-gsc/releases/download/"
        f"{gsc_info['releaseTag']}/{gsc_info['asset']}"
    )
    payload = download_verified(
        gsc_url,
        cache_root / "downloads" / "gsc-runtime.tar.zst",
        gsc_info["sha256"],
        "GSC runtime",
    )
    gsc_dir = extract_gsc_payload(payload, zstd_bin, gsc_info["sha256"], cache_root)
    claude_bin = bundled_claude_code(lock)
    return gsc_dir, claude_bin


def copy_template_contents(output_dir: Path) -> None:
    template_dir = SUBMISSION_DIR / "template"
    if not template_dir.is_dir():
        die(f"Factory starter template not found: {template_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    marker = output_dir / ".arc" / "arc-claude-gsc-initialized"
    existing_runtime_state = (output_dir / ".git").exists() and (output_dir / ".arc" / "traceability").exists()
    if marker.is_file() or existing_runtime_state:
        if not marker.is_file():
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("initialized\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "event": "workspace_resume",
                    "action": "preserve_existing_output",
                    "marker": str(marker.relative_to(output_dir)),
                }
            ),
            flush=True,
        )
        return

    for source in sorted(template_dir.iterdir()):
        if source.name == "template.yaml":
            continue
        destination = output_dir / source.name
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(source, destination)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("initialized\n", encoding="utf-8")


def copy_arc_skills(output_dir: Path) -> Path | None:
    source = SUBMISSION_DIR / "skills"
    if not source.is_dir():
        return None
    destination = output_dir / ".claude" / "skills"
    shutil.copytree(source, destination, dirs_exist_ok=True)
    return destination


def load_requirement_tree(requirements_dir: Path) -> dict[str, Any]:
    path = requirements_dir / "requirements.yaml"
    require_file(path, "requirements.yaml")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or str(payload.get("id") or "").strip() != "ROOT":
        die("requirements.yaml must contain a ROOT mapping")
    return payload


def load_root_modules(payload: dict[str, Any]) -> list[RequirementModule]:
    children = [item for item in payload.get("children", []) if isinstance(item, dict)]
    if not children:
        die("ROOT must contain at least one child module")
    result = []
    for index, subtree in enumerate(children, start=1):
        node_id = str(subtree.get("id") or subtree.get("req_id") or "").strip()
        if not node_id:
            die(f"ROOT child {index} has no id")
        result.append(RequirementModule(index, len(children), node_id, str(subtree.get("name") or node_id).strip(), subtree))
    return result



































































def ensure_gsc_spec(output_dir: Path, module: RequirementModule) -> Path:
    """Seed a minimal HTML SPEC stub so MCP spec_write has a home. Agent expands it."""
    spec_dir = output_dir / "SPEC" / "arcbench"
    spec_dir.mkdir(parents=True, exist_ok=True)
    safe_id = safe_node_id(module.node_id)
    md_path = spec_dir / f"{safe_id}.md"
    if md_path.exists():
        md_path.unlink()
    html_path = spec_dir / f"{safe_id}.html"
    if html_path.is_file() and html_path.stat().st_size > 50:
        return html_path
    title = f"{module.node_id}: {module.name}".replace("<", "").replace(">", "")
    brief = str(module.subtree.get("description") or module.name)[:500].replace("<", "").replace(">", "")
    html_path.write_text(
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN" data-spec-root>\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        f'  <meta name="spec-file" content="{safe_id}">\n'
        '  <meta name="spec-category" content="arcbench">\n'
        f'  <meta name="spec-title" content="{title}">\n'
        f"  <title>{title}</title>\n"
        "</head>\n"
        "<body>\n"
        "  <header data-spec-header>\n"
        f"    <h1>{title}</h1>\n"
        f'    <p data-arc-module-brief>{brief}</p>\n'
        "  </header>\n"
        '  <main data-spec-content>\n'
        "    <p data-arc-spawn>ARC v5al: Agent owns SPEC expansion via MCP. "
        "Do not invent features.</p>\n"
        "  </main>\n"
        "</body>\n"
        "</html>\n",
        encoding="utf-8",
    )
    return html_path


def load_contest_user_claude_md() -> str:
    """Exact Tao contest user-level CLAUDE.md (tracked under contest/CLAUDE.md)."""
    path = CONTEST_USER_CLAUDE_MD
    if not path.is_file():
        raise FileNotFoundError(f"contest user CLAUDE.md missing: {path}")
    body = path.read_text(encoding="utf-8")
    if "# 角色" not in body or "§1 故障根治 SOP" not in body or "技能索引" not in body:
        raise ValueError(f"contest CLAUDE.md missing required headings: {path}")
    return body


ARC_CONTEST_CLAUDE_FOOTER = """
---
# ARC contest footer (harness only — keep short)
- MCP stays ON; WaitForMcpServers OK; never set ARC_ENABLE_MCP=0; never ban MCP tools.
- Never invent tool names; never call mcp__arch__account_manage or mcp__arch__debug_binary.
- Skills via official Skill tool only (setting_sources=["user","project"] + skills=all); never Read/Bash/cat SKILL.md.
- Project phases (v5ai, NON-NEGOTIABLE): DESIGN once (ALL PRD+SPEC+TEST_DAG) → IMPLEMENT once (all coding; DOMAIN worktrees OK for parallel coding ONLY — never re-run PRD/SPEC/TEST_DAG) → BATCH_TEST once (one consolidated project test). No PAGES stage. Govern/audit OFF by default. No per-REQ design re-entry after IMPLEMENT starts.
""".strip()


def install_contest_claude_md(output_dir: Path, *, home_dir: Path | None = None) -> dict[str, str]:
    """Install Tao CLAUDE.md to user HOME/.claude and project output_dir/CLAUDE.md.

    Replaces the old harness-invented STEP theater softener body.
    """
    body = load_contest_user_claude_md()
    project_body = body.rstrip() + "\n\n" + ARC_CONTEST_CLAUDE_FOOTER + "\n"
    output_dir.mkdir(parents=True, exist_ok=True)
    project_path = output_dir / "CLAUDE.md"
    project_path.write_text(project_body, encoding="utf-8")
    paths: dict[str, str] = {"project_claude_md": str(project_path)}
    resolved_home = home_dir
    if resolved_home is None:
        env_home = (os.environ.get("HOME") or "").strip()
        if env_home:
            resolved_home = Path(env_home)
    if resolved_home is not None:
        user_dir = resolved_home / ".claude"
        user_dir.mkdir(parents=True, exist_ok=True)
        user_path = user_dir / "CLAUDE.md"
        user_path.write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")
        paths["user_claude_md"] = str(user_path)
    return paths


def assert_cc_full_config(
    *,
    options: object,
    enable_mcp: bool,
    plugins: list | None,
    home_dir: Path,
    output_dir: Path,
    mcp_servers: object = None,
) -> dict:
    """Log cc_full_config and return {ok, issues, ...}. Fail-closed when incomplete."""
    setting_sources = list(getattr(options, "setting_sources", None) or [])
    skills = getattr(options, "skills", None)
    allowed = list(getattr(options, "allowed_tools", None) or [])
    skill_allowed = "Skill" in allowed
    n_allowed = len(gsc_mcp_allowed_tools()) if enable_mcp else 0
    n_disallowed = len(gsc_mcp_disallowed_tool_names(prefixed=False)) if enable_mcp else 0
    leak = n_mcp_disallowed_prepared_leak() if enable_mcp else 0
    user_md = home_dir / ".claude" / "CLAUDE.md" if home_dir and str(home_dir) else Path("")
    project_md = output_dir / "CLAUDE.md"
    user_exists = bool(home_dir and str(home_dir) and user_md.is_file())
    project_exists = project_md.is_file()
    plugins_present = bool(plugins)
    mcp_servers_present = mcp_servers is not None and mcp_servers != {} and mcp_servers != []

    issues: list[str] = []
    if list(setting_sources) != ["user", "project"] and set(setting_sources) != {"user", "project"}:
        if "user" not in setting_sources or "project" not in setting_sources:
            issues.append("setting_sources_missing_user_or_project")
    if skills != "all":
        issues.append("skills_not_all")
    if not skill_allowed:
        issues.append("Skill_not_in_allowed_tools")
    # MCP must be on unless env explicitly disables (checked by caller too).
    raw_mcp = (os.environ.get("ARC_ENABLE_MCP") or "").strip().lower()
    env_explicit_off = raw_mcp in {"0", "false", "no", "off"}
    if not enable_mcp and not env_explicit_off:
        issues.append("mcp_off_without_env")
    if enable_mcp and not mcp_servers_present and not plugins_present:
        # plugins OR mcp_servers should attach GSC; prefer both.
        issues.append("mcp_servers_and_plugins_missing")
    if enable_mcp and leak != 0:
        issues.append(f"prepared_mcp_disallowed_leak={leak}")
    if not user_exists:
        issues.append("user_claude_md_missing")
    if not project_exists:
        issues.append("project_claude_md_missing")

    payload = {
        "event": "cc_full_config",
        "ok": len(issues) == 0,
        "issues": issues,
        "setting_sources": setting_sources,
        "skills": skills,
        "Skill_in_allowed": skill_allowed,
        "mcp_enabled": enable_mcp,
        "n_mcp_allowed": n_allowed,
        "n_mcp_disallowed": n_disallowed,
        "n_mcp_disallowed_prepared_leak": leak,
        "user_claude_md": str(user_md) if home_dir and str(home_dir) else None,
        "user_claude_md_exists": user_exists,
        "project_claude_md": str(project_md),
        "project_claude_md_exists": project_exists,
        "plugins_present": plugins_present,
        "mcp_servers_present": mcp_servers_present,
    }
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return payload
































def chown_tree(path: Path, uid: int, gid: int) -> None:
    try:
        os.chown(path, uid, gid, follow_symlinks=False)
    except (FileNotFoundError, PermissionError):
        return
    if not path.is_dir():
        return
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            item = Path(root) / name
            try:
                os.chown(item, uid, gid, follow_symlinks=False)
            except (FileNotFoundError, PermissionError):
                pass


def choose_agent_identity(output_dir: Path, home_dir: Path, plugin_data: Path) -> tuple[int, int, str] | None:
    if pwd is None or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return None

    template_stat = output_dir.stat()
    if template_stat.st_uid != 0:
        uid, gid = template_stat.st_uid, template_stat.st_gid
        try:
            username = pwd.getpwuid(uid).pw_name
        except KeyError:
            username = f"uid-{uid}"
    else:
        account = None
        for candidate in ("pwuser", "node", "nobody"):
            try:
                found = pwd.getpwnam(candidate)
            except KeyError:
                continue
            if found.pw_uid != 0:
                account = found
                break
        if account is None:
            die("ARC is running as root and no non-root execution user is available")
        uid, gid, username = account.pw_uid, account.pw_gid, account.pw_name
        chown_tree(output_dir, uid, gid)

    chown_tree(home_dir, uid, gid)
    chown_tree(plugin_data, uid, gid)
    return uid, gid, username


def privilege_dropper(identity: tuple[int, int, str] | None):
    if identity is None:
        return None
    uid, gid, _ = identity

    def drop() -> None:
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)

    return drop


def cleanup() -> None:
    for child in reversed(_children):
        if child.poll() is None:
            try:
                child.terminate()
            except ProcessLookupError:
                pass

    deadline = time.monotonic() + 3.0
    for child in reversed(_children):
        if child.poll() is None:
            try:
                child.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    child.kill()
                except ProcessLookupError:
                    pass





def main() -> int:
    """v5ao: thin CC launcher ONLY — build phase prompts + start ClaudeSDKClient.

    Agent owns DESIGN / IMPLEMENT / merge / verify / BATCH_TEST.
    Harness does NOT merge, run vitest, stamp receipts, or wave-orchestrate.
    Fixed CC launch: MCP ON + official Skills (setting_sources/skills=all) + CLAUDE.md.
    Soft session ends (blocking_limit / max_turns) mid-pipeline do NOT fail-closed;
    harness relaunches the next phase (still prompt + one CC start).
    """
    args = parse_args()
    requirements_dir = Path(args.requirement_path).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    if not requirements_dir.is_dir():
        die(f"requirement directory not found: {requirements_dir}")

    base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    model = os.environ.get("MODEL", "").strip()
    if not base_url or not api_key or not model:
        die("OPENAI_BASE_URL, OPENAI_API_KEY, and MODEL are required")

    copy_template_contents(output_dir)
    skills_dir = copy_arc_skills(output_dir)
    requirement_tree = load_requirement_tree(requirements_dir)
    modules = load_root_modules(requirement_tree)

    subprocess.run(
        ["git", "config", "--global", "--add", "safe.directory", str(output_dir)],
        check=False,
    )
    runtime = AgentRuntime.from_env(project_dir=str(output_dir))
    runtime.traceability.init_store(reset=False)
    runtime.traceability.store_requirement_tree(requirement_tree)
    runtime.git.ensure_repo(create_initial_commit=True)
    runtime.events.mark_run_started("arc-claude-gsc thin CC launcher (v5al)")

    artifacts_dir = Path(
        os.environ.get("ARCBENCH_ARTIFACTS_DIR", str(output_dir.parent / "artifacts"))
    ).expanduser().resolve()
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    gsc_dir, claude_bin = prepare_runtime(load_lock(), artifacts_dir)
    require_file(gsc_dir / "bin" / "gsc-spec-server", "compiled GSC server")
    require_file(gsc_dir / "mcp" / "src" / "bootstrap.mjs", "GSC MCP bootstrap")

    enable_mcp = mcp_enabled()
    mcp_config_path: Path | None = None
    if enable_mcp:
        mcp_config_path = write_gsc_mcp_config(gsc_dir, artifacts_dir / "mcp")
    mcp_timeout_ms = env_int("MCP_TIMEOUT", 60_000, minimum=1_000, maximum=600_000)

    home_dir = artifacts_dir / "home"
    plugin_data = artifacts_dir / "gsc-plugin-data"
    home_dir.mkdir(parents=True, exist_ok=True)
    plugin_data.mkdir(parents=True, exist_ok=True)
    identity = choose_agent_identity(output_dir, home_dir, plugin_data)

    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    install_contest_claude_md(output_dir, home_dir=home_dir)
    env["GSC_ARC_PACKAGED_RUNTIME"] = "1"
    env["GSC_RUNTIME_SERVER_BIN"] = str(gsc_dir / "bin" / "gsc-spec-server")
    env["CLAUDE_PLUGIN_ROOT"] = str(gsc_dir)
    env["CLAUDE_PLUGIN_DATA"] = str(plugin_data)
    env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"
    env["MCP_TIMEOUT"] = str(mcp_timeout_ms)
    env["PATH"] = os.pathsep.join(
        [
            str(gsc_dir / "bin"),
            str(gsc_dir / "lsp" / "web" / "node_modules" / ".bin"),
            env.get("PATH", ""),
        ]
    )

    gateway_proc = _arc_maybe_start_anthropic_proxy(base_url, api_key, model)
    claude_env = env.copy()
    if gateway_proc is not None:
        claude_env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:8787"
        claude_env["ANTHROPIC_API_KEY"] = "arc-local"
        for _k in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY_OLD", "CLAUDE_CODE_API_KEY"):
            claude_env.pop(_k, None)
        claude_env = sdk_driver.apply_contest_model_env(claude_env, "sonnet")

        def _stop_gateway(proc=gateway_proc):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()

        atexit.register(_stop_gateway)
    else:
        claude_env = apply_official_claude_env(
            env, base_url=base_url, api_key=api_key, model=model
        )
        claude_env["ANTHROPIC_API_KEY"] = ""
        if api_key:
            claude_env["ANTHROPIC_AUTH_TOKEN"] = api_key
    for key in ("SUDO_USER", "SUDO_UID", "SUDO_GID"):
        claude_env.pop(key, None)
    if identity is not None:
        _, _, username = identity
        claude_env["USER"] = username
        claude_env["LOGNAME"] = username

    max_budget_usd = os.environ.get("ARC_MAX_BUDGET_USD", "150").strip() or "150"
    for module in modules:
        ensure_gsc_spec(output_dir, module)

    all_req_ids = [m.node_id for m in modules]
    all_subtrees = [m.subtree for m in modules]
    modules_summary = [
        {"node_id": m.node_id, "name": m.name, "index": m.index, "total": m.total}
        for m in modules
    ]
    print(
        json.dumps(
            {
                "event": "arc_runtime_policy",
                "driver": "ClaudeSDKClient",
                "orchestration": "v5ao_thin_cc_launcher_soft_continue",
                "v5am_thin_cc_launcher_no_thrash_kill": True,
                "v5al_thin_cc_launcher_prompts_only": True,
                "mcp_enabled": enable_mcp,
                "mcp_config": str(mcp_config_path) if mcp_config_path else None,
                "max_budget_usd": max_budget_usd,
                "anthropic_proxy": gateway_proc is not None,
                "phases": ["design", "implement", "batch_test"],
                "n_modules": len(modules),
                "req_ids": all_req_ids,
                "v5al_thin_cc_launcher_shell_only": True,
                "v5am_no_rapid_refill_breaker": True,
                "v5am_no_read_streak_kill_gate": True,
                "v5am_thrash_observe_only": True,
                "v5an_no_blocking_limit_fail_closed": True,
                "v5an_no_max_turns_fail_closed": True,
                "v5an_phase_soft_continue": True,
                "v5an_raised_max_turns": True,
                "v5ao_soft_continue_rapid_refill": True,
                "v5ao_no_rapid_refill_retry_burn": True,
                "v5ao_phase_soft_continue": True,
                "v5al_agent_owns_merge_test": True,
                "v5al_no_wave_worktree_orchestrator": True,
                "v5al_no_python_vitest": True,
                "v5al_no_merge_helpers": True,
                "v5al_no_stamp_receipt_theater": True,
                "v5al_delete_agent_os": True,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    skills_hint = (
        f"ARC / project skills directory: {skills_dir}."
        if skills_dir
        else "Project skills may be under .claude/skills; GSC plugin skills also available."
    )
    req_json = json.dumps(all_subtrees, ensure_ascii=False, indent=2)
    modules_json = json.dumps(modules_summary, ensure_ascii=False, indent=2)

    def _phase_prompt(phase: str) -> str:
        common = textwrap.dedent(
            f"""
            You are Claude Code on ARC-Bench Official. GSC MCP is ON. Official Skills
            are available via Skill tool (setting_sources user+project, skills=all).
            Contest CLAUDE.md is installed (user + project). Budget floor ≥$150.

            Target task type: {args.task_type}
            Requirement source directory: {requirements_dir}
            ROOT modules: {modules_json}
            {skills_hint}

            HARD RULES:
            - YOU (the Agent) own ALL work: design, implement, git merge of any worktrees
              you create, verify, and BATCH_TEST. The harness only starts you with prompts.
            - GSC MCP stays ON. Never disable MCP / WaitForMcpServers.
            - THREE PHASES once: DESIGN-all → IMPLEMENT-all (+ your own merges) → BATCH_TEST
              (you run vitest/npm test + npm run build). Never per-REQ serial design/batch.
            - NO separate pages STEP. Do NOT invent features outside SPEC/TEST_DAG.
            - Prefer main session; harness will NOT create DOMAIN worktrees or merge for you.
            - Do NOT Read/Bash/cat SKILL.md; invoke Skills via the Skill tool.
            - Use only real mcp__arch__* tools (never invent names).

            ALL ROOT subtrees:
            ```json
            {req_json}
            ```
            """
        ).strip()
        if phase == "design":
            return (
                common
                + "\n\n"
                + textwrap.dedent(
                    """
                    CURRENT PHASE: DESIGN (project-wide, once for ALL REQs).
                    Do in order:
                    1) PRD for whole project when needed (or document skip if leaves already SPEC-ready).
                    2) HTML SPEC under SPEC/arcbench covering ALL REQs (prefer mcp__arch__spec_write).
                    3) TEST_DAG: write `.arc/steps/<primary>/test_dag.json` with NON-EMPTY `api` and `ui`.
                    Do NOT start IMPLEMENT coding yet. Do NOT run full vitest suite yet.
                    When done, summarize artifacts produced and stop.
                    """
                ).strip()
            )
        if phase == "implement":
            return (
                common
                + "\n\n"
                + textwrap.dedent(
                    """
                    CURRENT PHASE: IMPLEMENT (project-wide coding for ALL modules).
                    Design is done (or continue from existing SPEC/TEST_DAG if present).
                    YOU write all business code under frontend|backend|src.
                    YOU may use git worktrees for parallel coding IF YOU WANT — but YOU must
                    merge them back to mainline yourself (harness will NOT call git merge).
                    Mid-dev vitest/npm test ARE allowed; fix failures; repeat.
                    NEVER re-run PRD/SPEC/TEST_DAG. Soft design_* MCP if UI needs them.
                    When all modules are implemented and merged to mainline, summarize and stop.
                    """
                ).strip()
            )
        if phase == "batch_test":
            return (
                common
                + "\n\n"
                + textwrap.dedent(
                    """
                    CURRENT PHASE: BATCH_TEST (project-wide, once).
                    YOU run one consolidated project harness test on mainline:
                      prefer `npx vitest run` / `npm test` then cheap `npm run build`.
                    Fix failures yourself (Write/Edit + re-run). Harness will NOT run vitest.
                    Write a short proof receipt to `.arc/steps/<primary>/batch_ok.json` with
                    `{"ok": true, "summary": "..."}` when green.
                    FORBIDDEN: serial per-REQ BATCH_TEST. When green, summarize and stop.
                    """
                ).strip()
            )
        raise ValueError(phase)

    def _launch(phase: str, step_id: str) -> ClaudeRunResult:
        prompt = _phase_prompt(phase)
        degrade_mode = {"on": False}
        max_retries = env_int("ARC_CLAUDE_MAX_RETRIES", 2, minimum=0, maximum=8)
        base_seconds = env_int("ARC_CLAUDE_RETRY_BASE_SECONDS", 2, minimum=0, maximum=120)
        max_seconds = env_int("ARC_CLAUDE_RETRY_MAX_SECONDS", 30, minimum=0, maximum=600)

        def _attempt_env() -> dict[str, str]:
            attempt_env = claude_env.copy()
            attempt_env["MODEL"] = model
            if gateway_proc is not None:
                attempt_env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:8787"
                attempt_env["ANTHROPIC_API_KEY"] = "arc-local"
                for _k in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY_OLD", "CLAUDE_CODE_API_KEY"):
                    attempt_env.pop(_k, None)
                attempt_env["OPENAI_API_KEY"] = "arc-local"
                attempt_env["OPENAI_BASE_URL"] = "http://127.0.0.1:8787"
                attempt_env = sdk_driver.apply_contest_model_env(attempt_env, "sonnet")
            else:
                attempt_env["OPENAI_API_KEY"] = api_key
                attempt_env["OPENAI_BASE_URL"] = base_url
                attempt_env = apply_official_claude_env(
                    attempt_env, base_url=base_url, api_key=api_key, model=model
                )
                attempt_env["ANTHROPIC_API_KEY"] = ""
                if api_key:
                    attempt_env["ANTHROPIC_AUTH_TOKEN"] = api_key
                attempt_env = sdk_driver.apply_contest_model_env(attempt_env, model)
            return attempt_env

        print(
            json.dumps(
                {
                    "event": f"phase_{phase}_started",
                    "step_id": step_id,
                    "req_ids": all_req_ids,
                    "note": "v5ao thin: prompt + ClaudeSDKClient; soft-continue blocking_limit/max_turns/rapid_refill*",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        def run_attempt(attempt: int) -> ClaudeRunResult:
            return run_claude_via_sdk(
                prompt=prompt,
                output_dir=output_dir,
                model=model,
                claude_bin=claude_bin,
                gsc_dir=gsc_dir,
                mcp_config=mcp_config_path,
                enable_mcp=enable_mcp,
                attempt_env=_attempt_env(),
                skills_dir=skills_dir,
                max_budget_usd=max_budget_usd,
                step_id=step_id,
                degrade_mode=bool(degrade_mode["on"]),
            )

        def on_retry(attempt, result, classification, delay):
            if "rapid_refill" in classification.reason:
                degrade_mode["on"] = True
            print(
                json.dumps(
                    {
                        "event": "phase_cc_retry",
                        "phase": phase,
                        "step_id": step_id,
                        "attempt": attempt,
                        "delay_seconds": delay,
                        "classification": classification.reason,
                        "terminal_reason": result.terminal_reason,
                        "degrade_mode": degrade_mode["on"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        result, used_attempts = execute_with_retry(
            run_attempt,
            max_retries=max_retries,
            base_seconds=base_seconds,
            max_seconds=max_seconds,
            on_retry=on_retry,
        )
        print(
            json.dumps(
                {
                    "event": f"phase_{phase}_completed",
                    "step_id": step_id,
                    "returncode": result.returncode,
                    "is_error": result.is_error,
                    "terminal_reason": result.terminal_reason,
                    "attempts": used_attempts,
                    "degrade_mode": degrade_mode["on"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return result

    phases = (
        ("design", "prd"),
        ("implement", "implement"),
        ("batch_test", "batch_test"),
    )
    try:
        for idx, (phase, step_id) in enumerate(phases):
            result = _launch(phase, step_id)
            failed = result.is_error or (
                result.returncode not in (0, None) and int(result.returncode) != 0
            )
            if not failed:
                continue
            soft = phase_soft_continue_reason(result)
            # Mid-pipeline soft session end → relaunch next phase (still thin: prompt+CC).
            if soft is not None and idx < len(phases) - 1:
                print(
                    json.dumps(
                        {
                            "event": "phase_soft_continue",
                            "phase": phase,
                            "step_id": step_id,
                            "terminal_reason": soft,
                            "returncode": result.returncode,
                            "next_phase": phases[idx + 1][0],
                            "note": (
                                "v5ao: soft-continue mid-phase (blocking_limit/max_turns/"
                                "rapid_refill*); continue DESIGN→IMPLEMENT→BATCH via relaunch"
                            ),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                continue
            runtime.events.mark_run_failed(
                f"v5ao phase {phase} CC failed: {result.terminal_reason or result.returncode}"
            )
            return int(result.returncode or 1)
        runtime.events.mark_run_completed(
            "v5ao thin CC launcher: DESIGN→IMPLEMENT→BATCH_TEST "
            "(soft-continue blocking_limit/max_turns/rapid_refill*; raised max_turns)"
        )
        return 0
    except Exception as exc:
        runtime.events.mark_run_failed(str(exc))
        raise


def _signal_exit(code: int) -> None:
    cleanup()
    raise SystemExit(code)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: _signal_exit(143))
    signal.signal(signal.SIGINT, lambda *_: _signal_exit(130))
    try:
        raise SystemExit(main())
    finally:
        cleanup()