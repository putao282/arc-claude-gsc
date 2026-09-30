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
class DomainGroup:
    """One DOMAIN bucket: modules that share a worktree and implement batch."""
    domain_id: str
    modules: tuple[RequirementModule, ...]
    depends_on: tuple[str, ...] = ()
    conflicts_with: tuple[str, ...] = ()


@dataclass(frozen=True)
class WavePlan:
    """One WAVE: non-conflicting DOMAINs for parallel IMPLEMENT coding, then merge (BATCH is project-wide)."""
    wave_index: int
    domains: tuple[DomainGroup, ...]


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


@dataclass(frozen=True)
class ValidationResult:
    """Harness-owned local test gate result (Claude exit alone is never enough)."""
    ok: bool
    exit_code: int
    cmd: list[str]
    log_tail: str
    reason: str
    project_dir: str = ""


@dataclass(frozen=True)
class StepDef:
    """One Official MCP-first harness STEP with artifact/MCP acceptance gate."""
    step_id: str
    title: str
    required_skills: tuple[str, ...]
    goal: str
    exit_criteria: str
    forbid_mid_dev_tests: bool = False
    require_batch_test_run: bool = False


@dataclass(frozen=True)
class StepAcceptance:
    ok: bool
    reason: str
    skills_seen: tuple[str, ...] = ()
    missing_skills: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    mcp_required: tuple[str, ...] = ()
    mcp_optional_seen: tuple[str, ...] = ()
    commit_gate_status: str | None = None  # ok|fail|missing|skipped — soft; never alone fail-closed
    soft_notes: tuple[str, ...] = ()


# Official STEP loop (fail-closed). Advance only when artifact/MCP gates pass.
# Skills: official setting_sources+skills wiring only — NOT force-loaded / fail-closed.
OFFICIAL_STEPS: tuple[StepDef, ...] = (
    # v5ai: project-wide THREE PHASES once — DESIGN → IMPLEMENT → BATCH_TEST.
    # NOT per-REQ serial STEPs. NO pages STEP. govern/audit_refactor OFF by default.
    StepDef(
        step_id="prd",
        title="PRD (PHASE DESIGN — project-wide once)",
        required_skills=("architect",),
        goal=(
            "PHASE DESIGN (project-wide, once for ALL REQs): split demand into PRD covering "
            "the whole project when needed. Already-detailed leaf requirements may skip PRD "
            "ceremony and go straight into SPEC. Use GSC MCP `prd` / `state_*` as the Agent "
            "chooses. Do NOT invent features. Do NOT re-enter PRD after IMPLEMENT starts."
        ),
        exit_criteria=(
            "PRD artifact under PRD/ or .arc/steps/<id>/prd* OR agent-documented skip "
            "(leaves already detailed for SPEC)."
        ),
    ),
    StepDef(
        step_id="spec",
        title="SPEC (PHASE DESIGN — project-wide HTML)",
        required_skills=("architect",),
        goal=(
            "PHASE DESIGN (project-wide, once): write HTML SPEC under SPEC/arcbench covering "
            "ALL project REQs from PRD and/or already-detailed leaves via GSC MCP "
            "`spec_write` (HTML path; no Markdown migrate). One section per leaf (data-req). "
            "Do NOT invent features. Do NOT re-enter SPEC after IMPLEMENT starts."
        ),
        exit_criteria="SPEC/arcbench HTML exists (>50B). Prefer also seeing spec_write if used.",
    ),
    StepDef(
        step_id="test_dag",
        title="TEST_DAG (PHASE DESIGN — project-wide api+ui)",
        required_skills=("arcbench-traceability",),
        goal=(
            "PHASE DESIGN (project-wide, once): author API + UI test DAG covering ALL REQs. "
            "Write `.arc/steps/<id>/test_dag.json` with NON-EMPTY top-level keys `api` and "
            "`ui`. Optionally create matching test files. Coding+test loop happens in "
            "IMPLEMENT — not here. Do NOT re-enter TEST_DAG after IMPLEMENT starts."
        ),
        exit_criteria=(
            "`.arc/steps/<id>/test_dag.json` has non-empty `api` and `ui` arrays "
            "(or equivalent real api+ui test files)."
        ),
    ),
    StepDef(
        step_id="implement",
        title="IMPLEMENT (PHASE — all modules coding)",
        required_skills=("arcbench-checkpoint",),
        goal=(
            "PHASE IMPLEMENT (project-wide coding): write code, run tests, fix, repeat. "
            "CC Agent owns the loop. Implement API/UI/business logic under "
            "frontend|backend|src. DOMAIN worktrees OK for parallel coding ONLY — NEVER "
            "re-run PRD/SPEC/TEST_DAG. Soft-mention design_* MCP if UI needs them — "
            "NO separate pages STEP."
        ),
        exit_criteria=(
            "Business file Write|Edit under frontend|backend|src OR implement.json "
            "files_written evidence OR tests-green evidence. Gates MINIMAL — Agent owns judgment."
        ),
        forbid_mid_dev_tests=False,
    ),
    StepDef(
        step_id="batch_test",
        title="BATCH_TEST (PHASE — project-wide once)",
        required_skills=("arcbench-runtime-signals",),
        goal=(
            "PHASE BATCH_TEST (once, after ALL IMPLEMENT work is merged to mainline): run "
            "one consolidated project harness test (vitest/npm test + cheap npm run build). "
            "Never per-REQ serial batch. Never per-WAVE serial batch after a single DOMAIN."
        ),
        exit_criteria="Harness local validation (+ build) passes on mainline after project IMPLEMENT merge.",
        require_batch_test_run=True,
    ),
)

# Optional P1 audit STEPs (ARC_ENABLE_MCP_AUDIT_STEPS; default OFF for v5ag thin orchestrator).
GOVERN_STEP = StepDef(
    step_id="govern",
    title="PRD/SPEC govern audit",
    required_skills=("architect",),
    goal=(
        "HARD AUDIT (agent+tool chain, not single signal): run BOTH "
        "mcp__arch__prd_govern and mcp__arch__spec_govern (≥1 each) in this STEP, "
        "then STOP this STEP (do NOT re-call / re-audit). "
        "MCP tool use is required — receipt JSON alone does not pass. Demand SPEC "
        "coverage vs PRD + ROOT module.subtree atomic/leaf requirements. Mirror MCP "
        "govern output into `.arc/steps/<id>/prd_govern.json` and `spec_govern.json` "
        "(side receipts only; must match in-session tool calls). Do not invent a "
        "parallel file-only trace ceremony. Optional soft: mcp__arch__trace."
    ),
    exit_criteria=(
        "BOTH mcp__arch__prd_govern AND mcp__arch__spec_govern in-session tool use, "
        "then STOP. Receipt JSONs are side mirrors only — insufficient alone. "
        "Re-audit after coverage green is forbidden."
    ),
)

AUDIT_REFACTOR_STEP = StepDef(
    step_id="audit_refactor",
    title="Audit / refactor soft gate",
    required_skills=("arcbench-checkpoint",),
    goal=(
        "Soft: mcp__arch__arch_insight and/or mcp__arch__commit_gate; "
        "optional refactor_code/format_code. Write soft receipts under `.arc/steps/<id>/`."
    ),
    exit_criteria="Soft STEP: commit_gate/arch_insight optional (missing does not fail-closed).",
)


def official_steps() -> list[StepDef]:
    """Full STEP catalog (includes batch_test). Prefer design_steps + domain_dev + batch."""
    steps = list(OFFICIAL_STEPS)
    if not mcp_audit_steps_enabled():
        return steps
    out: list[StepDef] = []
    for step in steps:
        out.append(step)
        if step.step_id == "spec":
            out.append(GOVERN_STEP)
        elif step.step_id == "implement":
            out.append(AUDIT_REFACTOR_STEP)
    return out


def design_steps() -> list[StepDef]:
    """PHASE DESIGN once (project-wide): prd → spec → [govern] → test_dag.

    Never run these inside DOMAIN implement worktrees. Never re-enter after IMPLEMENT starts.
    """
    return [
        s
        for s in official_steps()
        if s.step_id not in ("implement", "audit_refactor", "batch_test")
    ]


def domain_dev_steps() -> list[StepDef]:
    """PHASE IMPLEMENT inside DOMAIN worktrees — coding ONLY (no PRD/SPEC/TEST_DAG)."""
    return [
        s for s in official_steps() if s.step_id in ("implement", "audit_refactor")
    ]


def wave_batch_steps() -> list[StepDef]:
    """PHASE BATCH_TEST once (project-wide after all IMPLEMENT merges)."""
    return [s for s in official_steps() if s.step_id == "batch_test"]


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
# them and trips rapid_refill_breaker. Keep MCP ON; advertise P0+P1 surface via
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


def mcp_audit_steps_enabled() -> bool:
    """Optional govern + audit_refactor STEPs. Default OFF (v5ag thin orchestrator)."""
    return env_bool("ARC_ENABLE_MCP_AUDIT_STEPS", False)


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
DEGRADED MODE (rapid_refill self-heal — MCP stays ON + 满配; Skills stay all; breaker still capped):
- Immediately Write or Edit business code under frontend/ / backend/ / src/.
- Do NOT re-call identical MCP reads (spec_read/state_read/artifact_read) with the same args.
- Do NOT re-load the same Skill. At most one Skill invocation if needed, then Write.
- Prefer: search_code once → Write skeleton → stop thrashing on reads.
- MCP allowlist is NOT narrowed on degrade (v5ah full perception).
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


def is_business_source_path(path: str | Path) -> bool:
    """True when path targets app code under frontend|backend|src (not only pages leftover)."""
    raw = str(path or "").replace("\\", "/")
    parts = [p for p in raw.split("/") if p]
    lowered = [p.lower() for p in parts]
    for i, part in enumerate(lowered):
        if part in {"frontend", "backend"} and i + 1 < len(lowered):
            # frontend/src/... or backend/src/... or frontend/pages etc. — require src|pages|app|lib|components|api|server|routes
            nxt = lowered[i + 1]
            if nxt in {"src", "pages", "app", "lib", "components", "api", "server", "routes", "services"}:
                return True
            # also accept frontend/*.ts(x) top-level? Prefer src for gate.
            if nxt.endswith((".ts", ".tsx", ".js", ".jsx", ".py")):
                return True
        if part == "src":
            return True
    return False


def implement_write_progress(
    output_dir: Path,
    sdir: Path,
    result: "ClaudeRunResult",
) -> tuple[bool, str]:
    """G1: in-STEP Write|Edit business paths OR implement.json files_written mtime≥step_start."""
    for path in result.builtin_writes:
        if is_business_source_path(path):
            return True, f"in_session_write:{path}"
    impl_receipt = sdir / "implement.json"
    step_start = result.step_started_at
    if impl_receipt.is_file() and impl_receipt.stat().st_size > 2:
        try:
            data = json.loads(impl_receipt.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if isinstance(data, dict):
            files = data.get("files_written") or data.get("written") or []
            if isinstance(files, str):
                files = [files]
            if not isinstance(files, list):
                files = []
            for item in files:
                rel = ""
                if isinstance(item, str):
                    rel = item
                elif isinstance(item, dict):
                    rel = str(item.get("path") or item.get("file") or "")
                if not rel:
                    continue
                p = Path(rel)
                if not p.is_absolute():
                    p = output_dir / rel
                if not is_business_source_path(str(p)):
                    continue
                if not p.is_file():
                    continue
                if step_start is None or p.stat().st_mtime >= float(step_start) - 1.0:
                    return True, f"implement_json_mtime:{p}"
    return False, "no_in_step_write_progress"




def mcp_short_name(tool: str) -> str:
    """mcp__arch__prd -> prd; bare short names pass through."""
    if not tool:
        return ""
    return tool.split("__")[-1]


def mcp_tools_matching(used: tuple[str, ...] | list[str], *shorts: str) -> list[str]:
    want = set(shorts)
    return [t for t in used if mcp_short_name(t) in want]



def govern_accept_already_green(result: "ClaudeRunResult | object") -> bool:
    """True when govern STEP acceptance is already satisfied (v5ae).

    Sources (any one):
    - deny_events reason == govern_accept_already_green (PreToolUse post-green)
    - thrash_events level == govern_accept_green_deny
    - both prd_govern AND spec_govern present in mcp_tools_used (≥1 each)
    """
    for e in getattr(result, "deny_events", ()) or ():
        if isinstance(e, dict) and e.get("reason") == "govern_accept_already_green":
            return True
    for e in getattr(result, "thrash_events", ()) or ():
        if isinstance(e, dict) and str(e.get("level") or "") == "govern_accept_green_deny":
            return True
    used = tuple(getattr(result, "mcp_tools_used", ()) or ())
    return bool(mcp_tools_matching(used, "prd_govern")) and bool(
        mcp_tools_matching(used, "spec_govern")
    )


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
                "audit_steps": mcp_audit_steps_enabled(),
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
                "audit_steps": mcp_audit_steps_enabled(),
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
            # v5x G3: signal degrade restart on next attempt (breaker/cap unchanged).
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
    total_attempts = max_retries + 1
    # Cap rapid_refill self-heal: schema refill usually repeats every attempt and
    # burned ~$2 on 6 doomed loops in v5e. Prefer tool-surface shrink; this is the
    # budget backstop (default 2 attempts = 1 + 1 retry).
    max_rapid_refill_attempts = env_int(
        "ARC_RAPID_REFILL_MAX_ATTEMPTS", 2, minimum=1, maximum=total_attempts
    )
    rapid_refill_hits = 0
    last_result: ClaudeRunResult | None = None
    for attempt in range(1, total_attempts + 1):
        result = run_attempt(attempt)
        last_result = result
        classification = classify_claude_failure(result)
        if not (result.returncode != 0 or result.is_error):
            return result, attempt
        if "rapid_refill" in classification.reason:
            rapid_refill_hits += 1
            if rapid_refill_hits >= max_rapid_refill_attempts:
                print(
                    json.dumps(
                        {
                            "event": "rapid_refill_budget_exhausted",
                            "attempt": attempt,
                            "rapid_refill_hits": rapid_refill_hits,
                            "max_rapid_refill_attempts": max_rapid_refill_attempts,
                            "classification": classification.reason,
                            "terminal_reason": result.terminal_reason or "unknown",
                            "note": "stopping self-heal to avoid doomed refill loops; MCP stays ON",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
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
                        "note": "full MCP满配 kept; degraded system append only; Skills=all; breaker/MCP stay ON",
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


def validation_receipt_dir(output_dir: Path) -> Path:
    return output_dir / ".arc" / "validation"


def validation_receipt_ok_path(output_dir: Path, node_id: str) -> Path:
    return validation_receipt_dir(output_dir) / f"{safe_node_id(node_id)}.ok"


def validation_receipt_json_path(output_dir: Path, node_id: str) -> Path:
    return validation_receipt_dir(output_dir) / f"{safe_node_id(node_id)}.json"


def has_validation_receipt(output_dir: Path, node_id: str) -> bool:
    return validation_receipt_ok_path(output_dir, node_id).is_file()


def write_validation_receipt(output_dir: Path, node_id: str, result: ValidationResult) -> None:
    dest = validation_receipt_dir(output_dir)
    dest.mkdir(parents=True, exist_ok=True)
    payload = {
        "ok": result.ok,
        "exit_code": result.exit_code,
        "cmd": result.cmd,
        "reason": result.reason,
        "project_dir": result.project_dir,
        "log_tail": result.log_tail[-4000:],
        "node_id": node_id,
    }
    validation_receipt_json_path(output_dir, node_id).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if result.ok:
        validation_receipt_ok_path(output_dir, node_id).write_text(
            f"ok exit={result.exit_code} cmd={' '.join(result.cmd)}\n",
            encoding="utf-8",
        )
    else:
        ok_path = validation_receipt_ok_path(output_dir, node_id)
        if ok_path.exists():
            ok_path.unlink()


def clear_validation_receipt(output_dir: Path, node_id: str) -> None:
    for path in (
        validation_receipt_ok_path(output_dir, node_id),
        validation_receipt_json_path(output_dir, node_id),
    ):
        if path.exists():
            path.unlink()


def module_already_passed(
    runtime: AgentRuntime,
    node_id: str,
    output_dir: Path | None = None,
) -> bool:
    """Skip only when PASSED *and* a harness validation receipt exists.

    Set ARC_FORCE_REVALIDATE=1 to ignore skip (forces re-run + re-validate).
    """
    if env_bool("ARC_FORCE_REVALIDATE", False):
        return False
    try:
        state = runtime.traceability.get_node_state(node_id)
    except Exception:
        return False
    if not (state and str(state.get("state") or "").upper() == "PASSED"):
        return False
    if output_dir is None:
        return False
    return has_validation_receipt(output_dir, node_id)


def discover_test_project(output_dir: Path) -> Path | None:
    """Prefer frontend/ with package.json; else output_dir package.json."""
    for candidate in (output_dir / "frontend", output_dir):
        if (candidate / "package.json").is_file():
            return candidate
    return None


def package_scripts(project_dir: Path) -> dict[str, str]:
    try:
        payload = json.loads((project_dir / "package.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
    scripts = payload.get("scripts") if isinstance(payload, dict) else None
    if not isinstance(scripts, dict):
        return {}
    return {str(k): str(v) for k, v in scripts.items() if isinstance(v, str)}



def run_project_build(
    project_dir: Path,
    *,
    timeout_seconds: int | None = None,
    run_fn=None,
) -> ValidationResult:
    """v5af hard gate: `npm run build` after vitest green. Fail closed on non-zero.

    Platform Official evaluate also runs `npm run build`; harness must not stamp
    test-passed when build is broken (GitHub v5ae App.tsx default-import miss).
    """
    if timeout_seconds is None:
        timeout_seconds = env_int("ARC_BUILD_TIMEOUT_SECONDS", 300, minimum=30, maximum=3600)
    scripts = package_scripts(project_dir)
    if "build" not in scripts:
        return ValidationResult(
            ok=True,
            exit_code=0,
            cmd=[],
            log_tail="",
            reason="no package.json build script; build gate skipped",
            project_dir=str(project_dir),
        )
    cmd = ["npm", "run", "build"]
    runner = run_fn or subprocess.run
    try:
        completed = runner(
            cmd,
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=os.environ.copy(),
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        exit_code = int(completed.returncode)
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        combined = (stdout + "\n" + stderr).strip()
        return ValidationResult(
            ok=False,
            exit_code=124,
            cmd=cmd,
            log_tail=combined[-8000:],
            reason=f"npm run build timed out after {timeout_seconds}s",
            project_dir=str(project_dir),
        )
    except FileNotFoundError as exc:
        return ValidationResult(
            ok=False,
            exit_code=127,
            cmd=cmd,
            log_tail=str(exc),
            reason=f"npm run build command not found: {exc}",
            project_dir=str(project_dir),
        )
    except Exception as exc:
        return ValidationResult(
            ok=False,
            exit_code=1,
            cmd=cmd,
            log_tail=str(exc),
            reason=f"npm run build runner error: {exc}",
            project_dir=str(project_dir),
        )
    combined = (stdout + "\n" + stderr).strip()
    log_tail = combined[-8000:]
    if exit_code == 0:
        return ValidationResult(
            ok=True,
            exit_code=0,
            cmd=cmd,
            log_tail=log_tail,
            reason="npm run build passed",
            project_dir=str(project_dir),
        )
    return ValidationResult(
        ok=False,
        exit_code=exit_code,
        cmd=cmd,
        log_tail=log_tail,
        reason=f"npm run build failed: exit={exit_code} (v5af hard gate)",
        project_dir=str(project_dir),
    )


def find_test_files(project_dir: Path) -> list[Path]:
    patterns = (
        "**/*.test.ts",
        "**/*.test.tsx",
        "**/*.test.js",
        "**/*.test.jsx",
        "**/*.test.mjs",
        "**/*.test.cjs",
        "**/*.spec.ts",
        "**/*.spec.tsx",
        "**/*.spec.js",
        "**/*.spec.jsx",
        "**/__tests__/**/*.ts",
        "**/__tests__/**/*.tsx",
        "**/__tests__/**/*.js",
        "**/__tests__/**/*.jsx",
    )
    found: list[Path] = []
    skip_parts = {"node_modules", ".git", "dist", "build", "coverage", ".next"}
    for pattern in patterns:
        for path in project_dir.glob(pattern):
            if not path.is_file():
                continue
            if any(part in skip_parts for part in path.parts):
                continue
            found.append(path)
    # de-dupe while preserving order
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in found:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def resolve_validation_command(project_dir: Path) -> list[str] | None:
    scripts = package_scripts(project_dir)
    # Prefer explicit vitest via npx when available or when scripts mention vitest.
    test_script = scripts.get("test", "")
    lower = test_script.lower()
    if "vitest" in lower or (project_dir / "node_modules" / ".bin" / "vitest").exists():
        return ["npx", "--yes", "vitest", "run"]
    if "test" in scripts:
        return ["npm", "test"]
    if (project_dir / "vitest.config.ts").exists() or (project_dir / "vitest.config.js").exists():
        return ["npx", "--yes", "vitest", "run"]
    if (project_dir / "node_modules" / ".bin" / "vitest").exists():
        return ["npx", "--yes", "vitest", "run"]
    return None


def run_module_validation(
    output_dir: Path,
    module: RequirementModule,
    *,
    timeout_seconds: int | None = None,
    run_fn=None,
) -> ValidationResult:
    """Harness-owned gate: Claude success alone must not mark_test_passed.

    Semantics:
    - Prefer frontend/ (or repo root) package.json tests.
    - No test files => FAIL (cannot green).
    - Non-zero test exit => FAIL.
    - Validation failures are never API-retryable; callers use a separate repair loop.
    """
    if timeout_seconds is None:
        timeout_seconds = env_int("ARC_VALIDATION_TIMEOUT_SECONDS", 300, minimum=30, maximum=3600)

    project_dir = discover_test_project(output_dir)
    if project_dir is None:
        return ValidationResult(
            ok=False,
            exit_code=1,
            cmd=[],
            log_tail="",
            reason="no package.json found under frontend/ or output root",
            project_dir="",
        )

    test_files = find_test_files(project_dir)
    if not test_files:
        return ValidationResult(
            ok=False,
            exit_code=1,
            cmd=[],
            log_tail="",
            reason=f"no test files found under {project_dir}",
            project_dir=str(project_dir),
        )

    cmd = resolve_validation_command(project_dir)
    if cmd is None:
        return ValidationResult(
            ok=False,
            exit_code=1,
            cmd=[],
            log_tail="",
            reason=f"no npm test / vitest command resolvable in {project_dir}",
            project_dir=str(project_dir),
        )

    runner = run_fn or subprocess.run
    try:
        completed = runner(
            cmd,
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=os.environ.copy(),
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        exit_code = int(completed.returncode)
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        combined = (stdout + "\n" + stderr).strip()
        return ValidationResult(
            ok=False,
            exit_code=124,
            cmd=cmd,
            log_tail=combined[-8000:],
            reason=f"validation timed out after {timeout_seconds}s",
            project_dir=str(project_dir),
        )
    except FileNotFoundError as exc:
        return ValidationResult(
            ok=False,
            exit_code=127,
            cmd=cmd,
            log_tail=str(exc),
            reason=f"validation command not found: {exc}",
            project_dir=str(project_dir),
        )
    except Exception as exc:
        return ValidationResult(
            ok=False,
            exit_code=1,
            cmd=cmd,
            log_tail=str(exc),
            reason=f"validation runner error: {exc}",
            project_dir=str(project_dir),
        )

    combined = (stdout + "\n" + stderr).strip()
    log_tail = combined[-8000:]
    if exit_code != 0:
        return ValidationResult(
            ok=False,
            exit_code=exit_code,
            cmd=cmd,
            log_tail=log_tail,
            reason=f"harness validation failed: exit={exit_code} cmd={' '.join(cmd)}",
            project_dir=str(project_dir),
        )

    # v5af: hard gate npm run build AFTER vitest green (before WAVE success / stamp).
    build = run_project_build(
        Path(project_dir),
        timeout_seconds=timeout_seconds,
        run_fn=runner,
    )
    if not build.ok:
        return ValidationResult(
            ok=False,
            exit_code=build.exit_code,
            cmd=list(build.cmd) or ["npm", "run", "build"],
            log_tail=(log_tail + "\n--- npm run build ---\n" + (build.log_tail or ""))[-8000:],
            reason=f"v5af build hard gate failed after vitest green: {build.reason}",
            project_dir=str(project_dir),
        )
    return ValidationResult(
        ok=True,
        exit_code=0,
        cmd=cmd + (["&&"] + list(build.cmd) if build.cmd else []),
        log_tail=(log_tail + "\n--- npm run build ---\n" + (build.log_tail or ""))[-8000:],
        reason="harness local validation+build passed (v5af)",
        project_dir=str(project_dir),
    )


@dataclass(frozen=True)
class ValidationGateDecision:
    """Policy for harness validation after Claude succeeds (never API-retryable)."""
    action: str  # "pass" | "repair" | "fail"
    reason: str


def decide_validation_gate(
    validation: ValidationResult,
    *,
    validation_repair: int,
    max_validation_repairs: int,
) -> ValidationGateDecision:
    """Map validation result + repair budget to pass/repair/fail.

    Claude success alone never yields "pass". Validation failures are
    classified non-retryable:validation (separate from API self-heal).
    """
    if validation.ok:
        return ValidationGateDecision("pass", validation.reason)
    if validation_repair >= max_validation_repairs:
        return ValidationGateDecision(
            "fail",
            f"non-retryable:validation exhausted after {validation_repair} repair(s): {validation.reason}",
        )
    return ValidationGateDecision(
        "repair",
        f"non-retryable:validation; schedule repair {validation_repair + 1}/{max_validation_repairs}: {validation.reason}",
    )


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


def _node_domain_id(node: dict[str, Any], fallback: str) -> str:
    """Extract DOMAIN id from a requirement node (domain / Domain / data-req-domain / req domain)."""
    for key in ("domain", "Domain", "DOMAIN", "req_domain", "domain_id"):
        raw = node.get(key)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return fallback


def module_domain_id(module: RequirementModule) -> str:
    """DOMAIN for a ROOT-child module; default = module.node_id (one DOMAIN per REQ)."""
    return _node_domain_id(module.subtree, module.node_id)


def _as_id_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        item = value.strip()
        return (item,) if item else ()
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for x in value:
            s = str(x).strip()
            if s and s not in out:
                out.append(s)
        return tuple(out)
    s = str(value).strip()
    return (s,) if s else ()


def module_depends_on(module: RequirementModule) -> tuple[str, ...]:
    """Dependency edges: domain or module ids this module/domain waits on."""
    node = module.subtree
    for key in ("depends_on", "dependencies", "depends", "requires", "blocked_by"):
        if key in node:
            return _as_id_tuple(node.get(key))
    return ()


def module_conflicts_with(module: RequirementModule) -> tuple[str, ...]:
    node = module.subtree
    for key in ("conflicts_with", "conflicts", "exclusive_with"):
        if key in node:
            return _as_id_tuple(node.get(key))
    return ()


def build_domain_groups(modules: list[RequirementModule]) -> list[DomainGroup]:
    """Group ROOT modules by DOMAIN; union depends_on / conflicts_with."""
    order: list[str] = []
    buckets: dict[str, list[RequirementModule]] = {}
    deps: dict[str, list[str]] = {}
    conf: dict[str, list[str]] = {}
    for mod in modules:
        did = module_domain_id(mod)
        if did not in buckets:
            buckets[did] = []
            order.append(did)
            deps[did] = []
            conf[did] = []
        buckets[did].append(mod)
        for d in module_depends_on(mod):
            # Map module-id deps to that module's domain when possible.
            target = d
            for m2 in modules:
                if m2.node_id == d:
                    target = module_domain_id(m2)
                    break
            if target != did and target not in deps[did]:
                deps[did].append(target)
        for c in module_conflicts_with(mod):
            target = c
            for m2 in modules:
                if m2.node_id == c:
                    target = module_domain_id(m2)
                    break
            if target != did and target not in conf[did]:
                conf[did].append(target)
    return [
        DomainGroup(
            domain_id=did,
            modules=tuple(buckets[did]),
            depends_on=tuple(deps[did]),
            conflicts_with=tuple(conf[did]),
        )
        for did in order
    ]


def plan_domain_waves(groups: list[DomainGroup]) -> list[WavePlan]:
    """Build WAVEs: Kahn-style topo on depends_on; conflict edges keep domains apart.

    Non-conflicting, dep-ready domains share a WAVE (prompt-level concurrent tracks).
    Runtime may still execute DOMAIN worktrees sequentially when single-threaded.
    """
    by_id = {g.domain_id: g for g in groups}
    remaining = set(by_id)
    # Symmetric conflict closure
    conflict: dict[str, set[str]] = {d: set() for d in by_id}
    for g in groups:
        for c in g.conflicts_with:
            if c in by_id:
                conflict[g.domain_id].add(c)
                conflict[c].add(g.domain_id)
    waves: list[WavePlan] = []
    safety = 0
    while remaining:
        safety += 1
        if safety > len(by_id) + 5:
            # Cycle / unsatisfiable deps — dump rest as one wave
            rest = tuple(by_id[d] for d in sorted(remaining))
            waves.append(WavePlan(wave_index=len(waves) + 1, domains=rest))
            break
        ready = []
        for d in sorted(remaining):
            g = by_id[d]
            if any(dep in remaining for dep in g.depends_on if dep in by_id):
                continue
            ready.append(d)
        if not ready:
            # dependency cycle among remaining
            rest = tuple(by_id[d] for d in sorted(remaining))
            waves.append(WavePlan(wave_index=len(waves) + 1, domains=rest))
            break
        # Pack ready domains into this wave without mutual conflicts
        wave_domains: list[str] = []
        for d in ready:
            if any(d in conflict[x] or x in conflict[d] for x in wave_domains):
                continue
            wave_domains.append(d)
        if not wave_domains:
            wave_domains = [ready[0]]
        waves.append(
            WavePlan(
                wave_index=len(waves) + 1,
                domains=tuple(by_id[d] for d in wave_domains),
            )
        )
        for d in wave_domains:
            remaining.discard(d)
    return waves


def write_wave_plan(output_dir: Path, waves: list[WavePlan]) -> Path:
    dest = output_dir / ".arc" / "waves" / "plan.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": "v5z",
        "policy": "domain_worktree_parallel_then_merge_then_batch_test",
        "waves": [
            {
                "wave_index": w.wave_index,
                "domains": [
                    {
                        "domain_id": g.domain_id,
                        "req_ids": [m.node_id for m in g.modules],
                        "depends_on": list(g.depends_on),
                        "conflicts_with": list(g.conflicts_with),
                    }
                    for g in w.domains
                ],
            }
            for w in waves
        ],
    }
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return dest


def domain_worktree_path(output_dir: Path, domain_id: str) -> Path:
    return output_dir.parent / f".arc-wt-{safe_node_id(domain_id)}"


def ensure_domain_worktree(output_dir: Path, domain_id: str) -> Path:
    """Create (or reuse) a git worktree for this DOMAIN branched from mainline HEAD."""
    wt = domain_worktree_path(output_dir, domain_id)
    branch = f"arc-domain-{safe_node_id(domain_id)}"
    if wt.is_dir() and (
        (wt / ".git").exists()
        or ((output_dir / ".git").exists() and _worktree_registered(output_dir, wt))
    ):
        return wt
    # Remove stale empty path
    if wt.is_dir() and not any(wt.iterdir()):
        wt.rmdir()
    elif wt.exists() and not _worktree_registered(output_dir, wt):
        # leftover dir without worktree — clear
        shutil.rmtree(wt, ignore_errors=True)
    # Ensure branch exists from HEAD
    list_br = subprocess.run(
        ["git", "branch", "--list", branch],
        cwd=str(output_dir),
        capture_output=True,
        text=True,
    )
    if not (list_br.stdout or "").strip():
        subprocess.run(
            ["git", "branch", branch],
            cwd=str(output_dir),
            check=False,
            capture_output=True,
            text=True,
        )
    proc = subprocess.run(
        ["git", "worktree", "add", str(wt), branch],
        cwd=str(output_dir),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        # Retry: worktree may already exist
        if wt.is_dir() and _worktree_registered(output_dir, wt):
            return wt
        # Fallback: soft copy via worktree add --force detached then checkout
        proc2 = subprocess.run(
            ["git", "worktree", "add", "-f", "-B", branch, str(wt), "HEAD"],
            cwd=str(output_dir),
            capture_output=True,
            text=True,
        )
        if proc2.returncode != 0:
            raise RuntimeError(
                f"git worktree add failed for domain {domain_id}: "
                f"{(proc.stderr or proc.stdout or '')[:500]} | "
                f"{(proc2.stderr or proc2.stdout or '')[:500]}"
            )
    return wt


def _worktree_registered(output_dir: Path, wt: Path) -> bool:
    proc = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=str(output_dir),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return False
    target = str(wt.resolve())
    for line in (proc.stdout or "").splitlines():
        if line.startswith("worktree "):
            if Path(line[len("worktree "):]).resolve() == Path(target):
                return True
    return False


def commit_domain_worktree(wt: Path, domain_id: str, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=str(wt), check=False, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", message, "--allow-empty"],
        cwd=str(wt),
        check=False,
        capture_output=True,
        text=True,
    )


def _git_unmerged_paths(cwd: Path) -> list[str]:
    """Return paths still unmerged in the index (empty when clean)."""
    proc = subprocess.run(
        ["git", "ls-files", "-u"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        p = parts[1].strip()
        if p and p not in seen:
            seen.add(p)
            paths.append(p)
    return paths


def _abort_merge_and_clean_index(output_dir: Path) -> None:
    """Abort any in-progress merge and ensure mainline index is not left unmerged."""
    subprocess.run(
        ["git", "merge", "--abort"],
        cwd=str(output_dir),
        check=False,
        capture_output=True,
        text=True,
    )
    # If abort was a no-op but unmerged entries remain, drop them via reset.
    if _git_unmerged_paths(output_dir):
        subprocess.run(
            ["git", "reset", "--merge"],
            cwd=str(output_dir),
            check=False,
            capture_output=True,
            text=True,
        )
    if _git_unmerged_paths(output_dir):
        subprocess.run(
            ["git", "read-tree", "--reset", "-u", "HEAD"],
            cwd=str(output_dir),
            check=False,
            capture_output=True,
            text=True,
        )


def _resolve_merge_conflicts_theirs(output_dir: Path, domain_id: str) -> bool:
    """Resolve conflicted paths with domain tip (--theirs). Return True if clean."""
    conflicts = _git_unmerged_paths(output_dir)
    if not conflicts:
        # May still be a failed merge with no unmerged paths; treat as not resolved.
        return False
    checkout = subprocess.run(
        ["git", "checkout", "--theirs", "--", *conflicts],
        cwd=str(output_dir),
        capture_output=True,
        text=True,
    )
    if checkout.returncode != 0:
        # Fallback path-by-path so one bad path does not block the rest.
        for p in conflicts:
            subprocess.run(
                ["git", "checkout", "--theirs", "--", p],
                cwd=str(output_dir),
                check=False,
                capture_output=True,
                text=True,
            )
    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(output_dir),
        check=False,
        capture_output=True,
        text=True,
    )
    still = _git_unmerged_paths(output_dir)
    if still:
        return False
    # Finish the merge commit if we are mid-merge with a clean index.
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(output_dir),
        capture_output=True,
        text=True,
    )
    merge_head = (output_dir / ".git" / "MERGE_HEAD").exists()
    if merge_head or (status.stdout or "").strip():
        commit = subprocess.run(
            [
                "git",
                "commit",
                "--no-edit",
                "-m",
                f"wave-merge domain {domain_id}",
            ],
            cwd=str(output_dir),
            capture_output=True,
            text=True,
        )
        if commit.returncode != 0 and merge_head:
            return False
    return not _git_unmerged_paths(output_dir) and not (output_dir / ".git" / "MERGE_HEAD").exists()


def _resolve_merge_conflicts_ours(output_dir: Path, domain_id: str) -> bool:
    """Resolve conflicted paths with mainline (--ours). Used only by supervisor retry_merge_ours."""
    conflicts = _git_unmerged_paths(output_dir)
    if not conflicts:
        return False
    checkout = subprocess.run(
        ["git", "checkout", "--ours", "--", *conflicts],
        cwd=str(output_dir),
        capture_output=True,
        text=True,
    )
    if checkout.returncode != 0:
        for pth in conflicts:
            subprocess.run(
                ["git", "checkout", "--ours", "--", pth],
                cwd=str(output_dir),
                check=False,
                capture_output=True,
                text=True,
            )
    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(output_dir),
        check=False,
        capture_output=True,
        text=True,
    )
    still = _git_unmerged_paths(output_dir)
    if still:
        return False
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(output_dir),
        capture_output=True,
        text=True,
    )
    merge_head = (output_dir / ".git" / "MERGE_HEAD").exists()
    if merge_head or (status.stdout or "").strip():
        commit = subprocess.run(
            [
                "git",
                "commit",
                "--no-edit",
                "-m",
                f"wave-merge domain {domain_id} (ours)",
            ],
            cwd=str(output_dir),
            capture_output=True,
            text=True,
        )
        if commit.returncode != 0 and merge_head:
            return False
    return not _git_unmerged_paths(output_dir) and not (output_dir / ".git" / "MERGE_HEAD").exists()



# Mainline keep-list: never strip these even though they start with ".".
_MAINLINE_DOT_KEEP = frozenset({".gitignore", ".gitattributes", ".gitmodules"})


def _is_mainline_excluded_path(rel: str) -> bool:
    """Paths that must never land on / stay on the mainline product tree.

    Tao v5ak policy: exclude ALL paths whose first component starts with ``.``
    (e.g. ``.claude/``, ``.gsc/``) and ``CLAUDE.md``. Keep core git meta
    (``.gitignore`` / ``.gitattributes`` / ``.gitmodules``).
    """
    p = (rel or "").replace("\\", "/").strip()
    if not p or p.startswith("/") or ".." in Path(p).parts:
        return False
    if p == "CLAUDE.md" or p.endswith("/CLAUDE.md"):
        return True
    first = p.split("/", 1)[0]
    if first.startswith(".") and first not in _MAINLINE_DOT_KEEP:
        return True
    return False


def _paths_in_tree(cwd: Path, ref: str) -> set[str]:
    """Return paths present in a git tree-ish (empty on failure)."""
    proc = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return set()
    return {ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()}


def _untracked_working_paths(cwd: Path) -> list[str]:
    """Untracked paths including ignored (anything that can block merge overwrite)."""
    proc = subprocess.run(
        ["git", "ls-files", "--others", "-z"],
        cwd=str(cwd),
        capture_output=True,
    )
    if proc.returncode != 0:
        return []
    raw = proc.stdout or b""
    out: list[str] = []
    for part in raw.split(b"\0"):
        if not part:
            continue
        try:
            out.append(part.decode("utf-8"))
        except UnicodeDecodeError:
            out.append(part.decode("utf-8", errors="replace"))
    return out


def _tracked_working_paths(cwd: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(cwd),
        capture_output=True,
    )
    if proc.returncode != 0:
        return []
    raw = proc.stdout or b""
    out: list[str] = []
    for part in raw.split(b"\0"):
        if not part:
            continue
        try:
            out.append(part.decode("utf-8"))
        except UnicodeDecodeError:
            out.append(part.decode("utf-8", errors="replace"))
    return out


def _parse_untracked_overwrite_paths(err_text: str) -> list[str]:
    """Extract paths from git 'untracked ... would be overwritten by merge' stderr."""
    if not err_text:
        return []
    low = err_text.lower()
    if "untracked" not in low or "overwritten" not in low:
        return []
    paths: list[str] = []
    seen: set[str] = set()
    in_block = False
    for line in err_text.splitlines():
        s = line.strip()
        if "untracked" in s.lower() and "overwritten" in s.lower():
            in_block = True
            continue
        if in_block:
            if not s or s.lower().startswith("please move") or s.lower().startswith("aborting"):
                break
            if s.startswith("error:") or s.startswith("fatal:"):
                break
            # git prints tab-indented paths; strip any leading noise
            p = s.lstrip("\t ").strip()
            if p and p not in seen and not p.endswith(":"):
                seen.add(p)
                paths.append(p)
    return paths


def _remove_working_path(output_dir: Path, rel: str) -> bool:
    """Best-effort remove an untracked file/dir/symlink under output_dir. Return True if gone."""
    if not rel or rel.startswith("/") or ".." in Path(rel).parts:
        return False
    target = (output_dir / rel).resolve()
    try:
        target.relative_to(output_dir.resolve())
    except ValueError:
        return False
    if not target.exists() and not target.is_symlink():
        return True
    try:
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target, ignore_errors=False)
        else:
            target.unlink(missing_ok=True)
    except OSError:
        return not target.exists() and not target.is_symlink()
    return not target.exists() and not target.is_symlink()


def _clear_untracked_blocking_merge(
    output_dir: Path,
    ref: str,
    *,
    domain_id: str = "",
    extra_paths: list[str] | None = None,
) -> list[str]:
    """Remove untracked/ignored paths that would block merge, plus excluded harness paths.

    Clear when:
    - path exists in incoming ref (would be overwritten), OR
    - path is mainline-excluded (``.claude/``, ``.gsc/``, other ``.*``, ``CLAUDE.md``)

    Product paths (e.g. ``SPEC/...``) may later land from domain tip via ``-X theirs``.
    Excluded paths are cleared here and stripped again after merge so they never
    promote onto the mainline product tree. Fail-closed if a blocker cannot be removed.
    """
    incoming = _paths_in_tree(output_dir, ref)
    candidates: list[str] = []
    seen: set[str] = set()
    for p in _untracked_working_paths(output_dir):
        if p in seen:
            continue
        if p in incoming or _is_mainline_excluded_path(p):
            seen.add(p)
            candidates.append(p)
    for p in extra_paths or []:
        p = (p or "").strip()
        if p and p not in seen:
            # Only clear if still untracked (not already in index as tracked)
            tracked = subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", p],
                cwd=str(output_dir),
                capture_output=True,
                text=True,
            )
            if tracked.returncode != 0:
                seen.add(p)
                candidates.append(p)
    if not candidates:
        return []
    cleared: list[str] = []
    failed: list[str] = []
    for p in candidates:
        if _remove_working_path(output_dir, p):
            cleared.append(p)
        else:
            failed.append(p)
    print(
        json.dumps(
            {
                "event": "domain_merge_clear_untracked",
                "domain_id": domain_id,
                "ref": ref,
                "cleared": cleared[:40],
                "failed": failed[:20],
                "excluded_policy": True,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    if failed:
        raise RuntimeError(
            f"merge domain {domain_id or '?'} preflight: cannot clear untracked "
            f"paths that would be overwritten: " + ",".join(failed[:40])
        )
    return cleared


def _strip_mainline_excluded_paths(output_dir: Path, *, domain_id: str = "") -> list[str]:
    """After domain merge: drop excluded paths so they never stay on mainline product tree."""
    removed: list[str] = []
    for p in list(_untracked_working_paths(output_dir)):
        if _is_mainline_excluded_path(p) and _remove_working_path(output_dir, p):
            removed.append(p)
    tracked_excluded = [p for p in _tracked_working_paths(output_dir) if _is_mainline_excluded_path(p)]
    if tracked_excluded:
        rm = subprocess.run(
            ["git", "rm", "-rf", "--ignore-unmatch", "--"] + tracked_excluded,
            cwd=str(output_dir),
            capture_output=True,
            text=True,
        )
        if rm.returncode != 0:
            raise RuntimeError(
                f"merge domain {domain_id or '?'}: cannot strip excluded paths: "
                + ((rm.stderr or rm.stdout or "")[:400])
            )
        removed.extend(tracked_excluded)
        dirty = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(output_dir),
            capture_output=True,
        )
        if dirty.returncode != 0:
            commit = subprocess.run(
                [
                    "git",
                    "commit",
                    "-m",
                    f"wave-merge domain {domain_id}: drop excluded harness/dot paths",
                ],
                cwd=str(output_dir),
                capture_output=True,
                text=True,
            )
            if commit.returncode != 0:
                raise RuntimeError(
                    f"merge domain {domain_id or '?'}: strip-excluded commit failed: "
                    + ((commit.stderr or commit.stdout or "")[:400])
                )
    seen: set[str] = set()
    out: list[str] = []
    for p in removed:
        if p not in seen:
            seen.add(p)
            out.append(p)
    if out:
        print(
            json.dumps(
                {
                    "event": "domain_merge_strip_excluded",
                    "domain_id": domain_id,
                    "removed": out[:40],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return out

def merge_domain_worktree(
    output_dir: Path,
    domain_id: str,
    wt: Path,
    *,
    strategy: str = "theirs",
) -> None:
    """Merge DOMAIN branch into mainline; remove worktree after successful merge.

    Hardened (v5aa): always abort dirty merges before retry; default
    ``git merge --no-ff -X theirs`` so domain tip wins overlapping paths;
    on conflict, checkout --theirs + add + commit; fail-closed with conflict
    file list if still dirty. Never runs ``git merge HEAD`` inside the worktree.

    Hardened (v5ak): before every merge attempt, clear untracked/ignored working
    paths that would be overwritten, and always clear mainline-excluded harness
    paths (``.claude/**``, ``.gsc/**``, other ``.*``, ``CLAUDE.md``). Product
    paths like ``SPEC/...`` may land from domain tip via ``-X theirs``; excluded
    paths are stripped after a successful merge so they never promote onto the
    mainline product tree. On residual untracked-overwrite stderr, clear + one
    same-ref retry. Fail-closed if clear/strip is impossible.

    strategy: "theirs" (default/v5aa) or "ours" (supervisor-only alternate).
    """
    strat = (strategy or "theirs").strip().lower()
    if strat not in {"theirs", "ours"}:
        strat = "theirs"
    branch = f"arc-domain-{safe_node_id(domain_id)}"
    # Commit any leftover WIP in worktree
    commit_domain_worktree(wt, domain_id, f"{domain_id}: domain accept pre-merge")

    rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(wt),
        capture_output=True,
        text=True,
    )
    sha = (rev.stdout or "").strip()
    merge_msg = f"wave-merge domain {domain_id}" + ("" if strat == "theirs" else f" ({strat})")
    # Prefer named branch; fall back to worktree tip SHA.
    candidates: list[str] = [branch]
    if sha and sha not in candidates:
        candidates.append(sha)

    last_err = ""
    merged_ok = False
    print(
        json.dumps(
            {
                "event": "domain_merge_attempt",
                "domain_id": domain_id,
                "strategy": strat,
                "branch": branch,
                "candidates": candidates[:4],
                "worktree": str(wt),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    for i, ref in enumerate(candidates):
        # Before ANY merge attempt (including the first retry after conflict): abort.
        had_dirty = bool(_git_unmerged_paths(output_dir)) or (output_dir / ".git" / "MERGE_HEAD").exists()
        _abort_merge_and_clean_index(output_dir)
        if had_dirty:
            print(
                json.dumps(
                    {
                        "event": "domain_merge_abort_before_retry",
                        "domain_id": domain_id,
                        "attempt_index": i,
                        "ref": ref,
                        "strategy": strat,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        if _git_unmerged_paths(output_dir):
            raise RuntimeError(
                f"merge domain {domain_id} failed: mainline index still unmerged after abort: "
                + ",".join(_git_unmerged_paths(output_dir)[:40])
            )
        # v5ak: clear untracked/ignored paths that incoming tree would overwrite.
        _clear_untracked_blocking_merge(output_dir, ref, domain_id=domain_id)
        merge = None
        for untracked_round in range(2):
            merge = subprocess.run(
                [
                    "git",
                    "merge",
                    "--no-ff",
                    "-X",
                    strat,
                    "-m",
                    merge_msg,
                    ref,
                ],
                cwd=str(output_dir),
                capture_output=True,
                text=True,
            )
            if merge.returncode == 0 and not _git_unmerged_paths(output_dir):
                merged_ok = True
                break
            err_blob = ((merge.stderr or "") + "\n" + (merge.stdout or "")).strip()
            overwrite_paths = _parse_untracked_overwrite_paths(err_blob)
            if overwrite_paths and untracked_round == 0:
                # Reactive clear + one same-ref retry (preflight may have missed a path).
                _abort_merge_and_clean_index(output_dir)
                _clear_untracked_blocking_merge(
                    output_dir,
                    ref,
                    domain_id=domain_id,
                    extra_paths=overwrite_paths,
                )
                print(
                    json.dumps(
                        {
                            "event": "domain_merge_untracked_overwrite_retry",
                            "domain_id": domain_id,
                            "ref": ref,
                            "paths": overwrite_paths[:40],
                            "attempt_index": i,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                continue
            break
        if merged_ok:
            break
        assert merge is not None
        # Conflict or soft failure: try to resolve with chosen strategy.
        if _git_unmerged_paths(output_dir) or (output_dir / ".git" / "MERGE_HEAD").exists():
            resolver = (
                _resolve_merge_conflicts_ours
                if strat == "ours"
                else _resolve_merge_conflicts_theirs
            )
            if resolver(output_dir, domain_id):
                print(
                    json.dumps(
                        {
                            "event": "domain_merge_conflict_resolved",
                            "domain_id": domain_id,
                            "strategy": strat,
                            "ref": ref,
                            "attempt_index": i,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                merged_ok = True
                break
        last_err = (merge.stderr or merge.stdout or "").strip()
        # Leave dirty state only long enough for the next iteration's abort.
        if i + 1 < len(candidates):
            continue

    if not merged_ok:
        conflicts = _git_unmerged_paths(output_dir)
        _abort_merge_and_clean_index(output_dir)
        detail = last_err[:400]
        if conflicts:
            detail = (detail + " | conflicts: " + ",".join(conflicts[:40])).strip(" |")
        print(
            json.dumps(
                {
                    "event": "domain_merge_failed_detail",
                    "domain_id": domain_id,
                    "strategy": strat,
                    "conflicts": conflicts[:40],
                    "detail": detail[:800],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        raise RuntimeError(f"merge domain {domain_id} failed: {detail}")

    print(
        json.dumps(
            {
                "event": "domain_merge_ok",
                "domain_id": domain_id,
                "strategy": strat,
                "branch": branch,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    # Tao v5ak: never promote excluded harness/dot paths onto mainline product tree.
    _strip_mainline_excluded_paths(output_dir, domain_id=domain_id)
    # Best-effort cleanup
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(wt)],
        cwd=str(output_dir),
        check=False,
        capture_output=True,
    )


def domain_dev_complete(
    output_dir: Path,
    group: DomainGroup,
    *,
    skip_req_ids: set[str] | None = None,
) -> tuple[bool, str]:
    """Per-DOMAIN accept: IMPLEMENT step receipts present (design already project-wide)."""
    skip = skip_req_ids or set()
    missing: list[str] = []
    checked = 0
    for mod in group.modules:
        if mod.node_id in skip:
            continue
        checked += 1
        for step in domain_dev_steps():
            if not has_step_receipt(output_dir, mod.node_id, step.step_id):
                missing.append(f"{mod.node_id}/{step.step_id}")
    if missing:
        return False, "missing_dev_receipts:" + ",".join(missing[:20])
    if checked == 0:
        return True, "domain_dev_accept_ok_all_skipped"
    return True, "domain_dev_accept_ok"


def wave_plan_summary(waves: list[WavePlan]) -> str:
    parts = []
    for w in waves:
        doms = ", ".join(
            f"{g.domain_id}[{','.join(m.node_id for m in g.modules)}]" for g in w.domains
        )
        parts.append(f"WAVE{w.wave_index}: {doms}")
    return " | ".join(parts)


def _html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )



def leaf_requirement_nodes(subtree: dict[str, Any]) -> list[dict[str, Any]]:
    """Return leaf/atomic requirement nodes under a ROOT-child subtree (DFS order).

    A node is a leaf when it has an id/req_id and either no `children` list or an empty one.
    Nested children are walked; the parent itself is not emitted when it has children.
    """
    leaves: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        children_raw = node.get("children")
        child_list = (
            [c for c in children_raw if isinstance(c, dict)]
            if isinstance(children_raw, list)
            else []
        )
        if child_list:
            for child in child_list:
                walk(child)
            return
        rid = str(node.get("id") or node.get("req_id") or "").strip()
        if rid:
            leaves.append(node)

    walk(subtree)
    return leaves


def leaf_requirement_ids(subtree: dict[str, Any]) -> list[str]:
    """Stable unique leaf/atomic requirement ids from subtree."""
    seen: set[str] = set()
    out: list[str] = []
    for node in leaf_requirement_nodes(subtree):
        rid = str(node.get("id") or node.get("req_id") or "").strip()
        if rid and rid not in seen:
            seen.add(rid)
            out.append(rid)
    return out


def html_has_data_req(html: str, req_id: str) -> bool:
    """True if HTML binds a section to req_id via data-req / data-req-id."""
    if not req_id:
        return False
    markers = (
        f'data-req="{req_id}"',
        f"data-req='{req_id}'",
        f'data-req-id="{req_id}"',
        f"data-req-id='{req_id}'",
    )
    return any(m in html for m in markers)


    for lid in leaf_ids:
        val = trace.get(lid)
        if val is None or (isinstance(val, str) and not val.strip()):
            return False
    return True


def ensure_gsc_spec(output_dir: Path, module: RequirementModule) -> Path:
    """Materialize the ARC requirement as GSC HTML 2.0 SPEC (never Markdown).

    Writing SPEC/*.md forces GSC's migrate path; ARC runners lack a migrate provider,
    which previously caused thrash. HTML 2.0 lets GSC MCP use spec_read/spec_write
    without banning MCP.

    v5v: seed one section per subtree leaf/atomic requirement (id/name/description)
    so the agent expands from PRD+atomic rather than inventing from a one-line stub.
    """
    spec_dir = output_dir / "SPEC" / "arcbench"
    spec_dir.mkdir(parents=True, exist_ok=True)
    safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in module.node_id)
    md_path = spec_dir / f"{safe_id}.md"
    if md_path.exists():
        md_path.unlink()
    html_path = spec_dir / f"{safe_id}.html"
    title = _html_escape(f"{module.node_id}: {module.name}")
    brief = _html_escape(str(module.subtree.get("description") or module.name)[:500])
    leaves = leaf_requirement_nodes(module.subtree)
    if not leaves:
        leaves = [
            {
                "id": module.node_id,
                "name": module.name,
                "description": module.subtree.get("description") or module.name,
            }
        ]
    section_parts: list[str] = []
    for idx, leaf in enumerate(leaves, start=1):
        lid = str(leaf.get("id") or leaf.get("req_id") or f"leaf-{idx}").strip()
        lname = str(leaf.get("name") or leaf.get("title") or lid).strip()
        ldesc = str(
            leaf.get("description")
            or leaf.get("brief")
            or leaf.get("summary")
            or lname
        ).strip()[:800]
        accessible = str(
            leaf.get("accessible_name")
            or leaf.get("accessibleName")
            or leaf.get("a11y_name")
            or lname
        ).strip()[:200]
        role = str(leaf.get("role") or leaf.get("ui_role") or "").strip()[:120]
        seed = str(leaf.get("seed_data") or leaf.get("seed") or leaf.get("fixtures") or "").strip()[:300]
        states = str(
            leaf.get("observable_states")
            or leaf.get("states")
            or leaf.get("ui_states")
            or ""
        ).strip()[:300]
        sec_id = "s-" + "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in lid)
        esc_lid = _html_escape(lid)
        esc_name = _html_escape(lname)
        esc_desc = _html_escape(ldesc)
        esc_acc = _html_escape(accessible)
        esc_role = _html_escape(role) if role else ""
        esc_seed = _html_escape(seed) if seed else ""
        esc_states = _html_escape(states) if states else ""
        extra_lis = ""
        if esc_role:
            extra_lis += f'        <li data-field="role">{esc_role}</li>\n'
        if esc_seed:
            extra_lis += f'        <li data-field="seed_data">{esc_seed}</li>\n'
        if esc_states:
            extra_lis += f'        <li data-field="observable_states">{esc_states}</li>\n'
        section_parts.append(
            f'    <section id="{sec_id}" data-section="requirements" '
            f'data-req="{esc_lid}" data-req-status="unknown" data-req-domain="arcbench">\n'
            f"      <h2>{esc_name}</h2>\n"
            f'      <p data-arc-brief>{esc_desc}</p>\n'
            f"      <ul data-arc-atomic>\n"
            f'        <li data-field="accessible_name">{esc_acc}</li>\n'
            f"{extra_lis}"
            f"      </ul>\n"
            f"    </section>"
        )
    sections_block = "\n".join(section_parts)
    html_path.write_text(
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN" data-spec-root>\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        f'  <meta name="spec-file" content="{safe_id}">\n'
        '  <meta name="spec-category" content="arcbench">\n'
        f'  <meta name="spec-title" content="{title}">\n'
        '  <script type="application/ld+json">\n'
        f'  {{ "@context":"https://spec.gsc.local/v1", "@type":"SpecDocument", '
        f'"id":"{safe_id}", "dependencies":[], "children":[] }}\n'
        "  </script>\n"
        f"  <title>{title}</title>\n"
        "</head>\n"
        "<body>\n"
        "  <header data-spec-header>\n"
        f"    <h1>{title}</h1>\n"
        f'    <p data-arc-module-brief>{brief}</p>\n'
        "  </header>\n"
        '  <main data-spec-content>\n'
        "    <p data-arc-spawn>ARC: derive SPEC from PRD + subtree atomic requirements; "
        "preserve exact English accessible names/roles/seed/states. "
        "Prefer GSC MCP SPEC tools. Expand seeded sections; do not invent features.</p>\n"
        f"{sections_block}\n"
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


def ensure_arc_spawn_gate_softener(output_dir: Path, *, home_dir: Path | None = None) -> None:
    """Install Tao contest CLAUDE.md (user + project) and SPAWN-GATE softener flag.

    v5ah: STOP overwriting CLAUDE.md with harness STEP theater. Use Tao's
    contest/CLAUDE.md as user+project guidance; append only a short ARC footer.
    """
    install_contest_claude_md(output_dir, home_dir=home_dir)

    off = output_dir / ".claude" / "spawn-gate-off"
    off.parent.mkdir(parents=True, exist_ok=True)
    if not off.exists():
        off.write_text(
            "# ARC packaged runtime: disable SPAWN-GATE hard deny (REQ-AGENTGOV-2).\n"
            "# MCP stays ON. Soft reminders may still appear if Agent is used.\n",
            encoding="utf-8",
        )


def step_dir(output_dir: Path, node_id: str) -> Path:
    return output_dir / ".arc" / "steps" / safe_node_id(node_id)


def step_receipt_ok_path(output_dir: Path, node_id: str, step_id: str) -> Path:
    return step_dir(output_dir, node_id) / f"{step_id}.ok"


def step_receipt_json_path(output_dir: Path, node_id: str, step_id: str) -> Path:
    return step_dir(output_dir, node_id) / f"{step_id}.json"


def has_step_receipt(output_dir: Path, node_id: str, step_id: str) -> bool:
    return step_receipt_ok_path(output_dir, node_id, step_id).is_file()


def clear_step_receipt(output_dir: Path, node_id: str, step_id: str) -> None:
    for path in (
        step_receipt_ok_path(output_dir, node_id, step_id),
        step_receipt_json_path(output_dir, node_id, step_id),
    ):
        if path.exists():
            path.unlink()


def clear_domain_implement_receipts(work_dir: Path, req_ids: list[str]) -> list[str]:
    """Clear implement(+batch_test) receipts so DOMAIN can re-implement. Never marks green."""
    cleared: list[str] = []
    for rid in req_ids:
        for sid in ("implement", "audit_refactor", "batch_test"):
            before = has_step_receipt(work_dir, rid, sid)
            clear_step_receipt(work_dir, rid, sid)
            if before:
                cleared.append(f"{rid}/{sid}")
    return cleared




def stamp_project_design_siblings(
    *,
    output_dir: Path,
    primary: "RequirementModule",
    siblings: list["RequirementModule"],
) -> None:
    """After project-wide PHASE DESIGN green on primary, stamp design receipts to all REQs.

    v5ai: DESIGN runs once for the whole project. Sibling REQs inherit prd/spec/test_dag
    (+ govern if enabled) receipts — never re-enter design STEPs per REQ.
    """
    if not siblings:
        return
    for step in design_steps():
        if not has_step_receipt(output_dir, primary.node_id, step.step_id):
            continue
        src_json = step_receipt_json_path(output_dir, primary.node_id, step.step_id)
        src_ok = step_receipt_ok_path(output_dir, primary.node_id, step.step_id)
        for module in siblings:
            if has_step_receipt(output_dir, module.node_id, step.step_id):
                continue
            dest_dir = step_dir(output_dir, module.node_id)
            dest_dir.mkdir(parents=True, exist_ok=True)
            if src_json.is_file():
                raw = src_json.read_text(encoding="utf-8")
                try:
                    payload = json.loads(raw)
                except Exception:
                    payload = {}
                if isinstance(payload, dict):
                    payload["req_id"] = module.node_id
                    payload["shared_from"] = primary.node_id
                    payload["project_design_phase"] = True
                    soft = list(payload.get("soft_notes") or [])
                    soft.append("v5ai_project_design_stamp")
                    payload["soft_notes"] = soft
                    step_receipt_json_path(output_dir, module.node_id, step.step_id).write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                else:
                    step_receipt_json_path(output_dir, module.node_id, step.step_id).write_text(
                        raw, encoding="utf-8"
                    )
            if src_ok.is_file():
                step_receipt_ok_path(output_dir, module.node_id, step.step_id).write_text(
                    src_ok.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
        print(
            json.dumps(
                {
                    "event": "project_design_stamped",
                    "step_id": step.step_id,
                    "primary_req": primary.node_id,
                    "stamped_siblings": [m.node_id for m in siblings],
                    "note": "PHASE DESIGN once — no per-REQ design re-entry",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )


def stamp_wave_batch_siblings(
    *,
    runtime: "AgentRuntime",
    output_dir: Path,
    primary: "RequirementModule",
    siblings: list["RequirementModule"],
    wave_index: int,
    completed: list[str],
) -> None:
    """After PHASE BATCH_TEST green on primary, stamp remaining project modules.

    v5ai/v5ad: FORBIDDEN serial per-REQ agent batch_test. Harness vitest is project-level;
    primary green applies to the whole project (or WAVE siblings list passed in).
    Never marks green without a primary validation receipt.
    """
    if not siblings:
        return
    # Prefer primary harness receipt; fall back to a fresh project-level run once.
    primary_receipt = validation_receipt_json_path(output_dir, primary.node_id)
    validation: ValidationResult | None = None
    if primary_receipt.is_file():
        try:
            payload = json.loads(primary_receipt.read_text(encoding="utf-8"))
            if payload.get("ok"):
                validation = ValidationResult(
                    ok=True,
                    exit_code=int(payload.get("exit_code") or 0),
                    cmd=list(payload.get("cmd") or []),
                    log_tail=str(payload.get("log_tail") or ""),
                    reason=str(payload.get("reason") or "harness local validation passed"),
                    project_dir=str(payload.get("project_dir") or ""),
                )
        except Exception:
            validation = None
    if validation is None:
        validation = run_module_validation(output_dir, primary)
    if not validation.ok:
        raise RuntimeError(
            f"WAVE{wave_index} central BATCH primary {primary.node_id} has no green "
            f"validation receipt to stamp siblings: {validation.reason}"
        )

    batch_step = next(s for s in official_steps() if s.step_id == "batch_test")
    acceptance = StepAcceptance(
        ok=True,
        reason=(
            f"WAVE-central one-shot BATCH_TEST stamped from primary {primary.node_id} "
            f"(v5ad; no serial per-REQ batch_test)"
        ),
        artifacts=(
            f"validation:{validation.reason}",
            f"cmd:{' '.join(validation.cmd)}" if validation.cmd else "cmd:",
            f"stamped_from:{primary.node_id}",
        ),
        soft_notes=("v5ad_wave_central_one_shot_stamp",),
    )
    for module in siblings:
        write_validation_receipt(output_dir, module.node_id, validation)
        write_step_receipt(output_dir, module.node_id, batch_step, acceptance)
        runtime.events.mark_implementation_done(
            module.node_id,
            f"WAVE BATCH_TEST stamped for {module.name} from primary {primary.node_id}",
        )
        runtime.events.mark_test_passed(
            module.node_id,
            "Harness local validation passed after WAVE-central one-shot BATCH_TEST",
        )
        runtime.git.commit(f"{module.node_id}: {module.name} (wave-batch stamp)")
        if module.node_id not in completed:
            completed.append(module.node_id)
        print(
            json.dumps(
                {
                    "event": "wave_batch_test_stamped_sibling",
                    "wave_index": wave_index,
                    "primary_req": primary.node_id,
                    "req_id": module.node_id,
                    "note": "ONE-SHOT central BATCH — no serial per-REQ agent batch_test",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

def write_step_receipt(
    output_dir: Path,
    node_id: str,
    step: StepDef,
    acceptance: StepAcceptance,
    *,
    claude: ClaudeRunResult | None = None,
) -> None:
    dest = step_dir(output_dir, node_id)
    dest.mkdir(parents=True, exist_ok=True)
    payload = {
        "step_id": step.step_id,
        "title": step.title,
        "req_id": node_id,
        "ok": acceptance.ok,
        "reason": acceptance.reason,
        "required_skills": list(step.required_skills),
        "skills_seen": list(acceptance.skills_seen),
        "missing_skills": list(acceptance.missing_skills),
        "artifacts": list(acceptance.artifacts),
        "mcp_tools_used": list(claude.mcp_tools_used) if claude else [],
        "mcp_required": list(acceptance.mcp_required),
        "mcp_optional_seen": list(acceptance.mcp_optional_seen),
        "commit_gate_status": acceptance.commit_gate_status,
        "soft_notes": list(acceptance.soft_notes),
    }
    step_receipt_json_path(output_dir, node_id, step.step_id).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if acceptance.ok:
        step_receipt_ok_path(output_dir, node_id, step.step_id).write_text(
            f"ok step={step.step_id} skills={','.join(acceptance.skills_seen)}\n",
            encoding="utf-8",
        )
    else:
        ok_path = step_receipt_ok_path(output_dir, node_id, step.step_id)
        if ok_path.exists():
            ok_path.unlink()


def _existing_paths(paths: list[Path]) -> list[str]:
    return [str(p) for p in paths if p.exists()]


def evaluate_step_acceptance(
    output_dir: Path,
    module: RequirementModule,
    step: StepDef,
    result: ClaudeRunResult,
) -> StepAcceptance:
    """Thin STEP gate (v5ag): minimal artifacts (+ batch vitest/build).

    Skills are NOT fail-closed (official CC Skills are model-invoked via setting_sources
    + skills=). Telemetry only: skills_seen / missing_skills recorded as soft_notes.

    Thin gates:
      - prd: PRD artifact OR documented skip (leaves already SPEC-ready)
      - spec: SPEC/arcbench HTML >50B (spec_write preferred soft)
      - test_dag: non-empty api+ui schema
      - implement: business Write|Edit OR implement.json OR tests-green evidence (MINIMAL)
      - batch_test: vitest (+ npm run build via run_module_validation)
    NO pages STEP. govern/audit only when ARC_ENABLE_MCP_AUDIT_STEPS=1.
    Soft: commit_gate_status on batch_test / audit_refactor.
    """
    skills_seen = tuple(result.skills_loaded)
    missing = tuple(s for s in step.required_skills if s not in skills_seen)
    # soft only — never fail-closed on missing Skill tool loads (v5u)

    sid = safe_node_id(module.node_id)
    sdir = step_dir(output_dir, module.node_id)
    artifacts: list[str] = []
    mcp_required: list[str] = []
    mcp_optional: list[str] = []
    soft_notes: list[str] = []
    commit_gate_status: str | None = None
    used = tuple(result.mcp_tools_used)
    if missing:
        soft_notes.append("soft_missing_skills:" + ",".join(missing))

    def _receipt_or_tool(filename: str, *tool_shorts: str) -> list[str]:
        found: list[str] = []
        path = sdir / filename
        if path.is_file() and path.stat().st_size > 2:
            found.append(str(path))
        found.extend(mcp_tools_matching(used, *tool_shorts))
        return found

    if step.step_id == "prd":
        candidates = [
            output_dir / "PRD",
            sdir / "prd",
            sdir / "prd.md",
            sdir / "prd.json",
            sdir / "prd.html",
            sdir / "prd.artifact.json",
            sdir / "prd_skip.json",
        ]
        found: list[str] = []
        prd_root = output_dir / "PRD"
        if prd_root.is_dir() and any(prd_root.rglob("*")):
            found.append(str(prd_root))
        found.extend(_existing_paths(candidates[1:]))
        found = list(dict.fromkeys(found))
        mcp_prd = mcp_tools_matching(used, "prd")
        mcp_optional.extend(mcp_tools_matching(used, "state_read", "architect", "discoverer", "prd_govern"))
        skip_ok = False
        skip_path = sdir / "prd_skip.json"
        if skip_path.is_file() and skip_path.stat().st_size > 2:
            try:
                skip_payload = json.loads(skip_path.read_text(encoding="utf-8"))
            except Exception:
                skip_payload = {}
            reason = ""
            if isinstance(skip_payload, dict):
                reason = str(skip_payload.get("reason") or skip_payload.get("skip") or "")
            else:
                reason = str(skip_payload)
            if "spec" in reason.lower() or "leaf" in reason.lower() or "skip" in reason.lower() or reason:
                skip_ok = True
                soft_notes.append(f"prd_skip:{reason[:120] or 'documented'}")
                if str(skip_path) not in found:
                    found.append(str(skip_path))
        # Also accept skip note in agent summary / prd.json
        for cand in (sdir / "prd.json", sdir / "prd.md"):
            if cand.is_file() and cand.stat().st_size > 2:
                try:
                    blob = cand.read_text(encoding="utf-8", errors="replace").lower()
                except OSError:
                    blob = ""
                if "skip" in blob and ("spec" in blob or "leaf" in blob or "already" in blob):
                    skip_ok = True
                    soft_notes.append(f"prd_skip_note:{cand.name}")
        if not found and not mcp_prd and not skip_ok:
            return StepAcceptance(
                ok=False,
                reason=(
                    "PRD artifact missing (PRD/ or .arc/steps/<id>/prd*) and no documented "
                    "skip for already-detailed leaves"
                ),
                skills_seen=skills_seen,
                artifacts=tuple(found),
                mcp_optional_seen=tuple(mcp_optional),
                soft_notes=tuple(soft_notes),
            )
        if not found and mcp_prd and not skip_ok:
            # MCP prd without file: still soft-pass if skip not needed — require a file OR skip.
            return StepAcceptance(
                ok=False,
                reason="MCP prd used but PRD artifact file/dir still missing (or write prd_skip.json)",
                skills_seen=skills_seen,
                artifacts=tuple(mcp_prd),
                mcp_optional_seen=tuple(mcp_optional),
                soft_notes=tuple(soft_notes),
            )
        artifacts = found + mcp_prd
        mcp_required = list(mcp_prd)

    elif step.step_id == "spec":
        spec_html = output_dir / "SPEC" / "arcbench" / f"{sid}.html"
        spec_dir = output_dir / "SPEC" / "arcbench"
        found = []
        if spec_html.is_file() and spec_html.stat().st_size > 50:
            found.append(str(spec_html))
        elif spec_dir.is_dir():
            htmls = [p for p in spec_dir.glob("*.html") if p.is_file() and p.stat().st_size > 50]
            found.extend(str(p) for p in htmls[:5])
        # v5r/v5v: require spec_write (read-only thrash / mcp prd must not satisfy acceptance)
        mcp_write = mcp_tools_matching(used, "spec_write")
        mcp_read = mcp_tools_matching(used, "spec_read")
        mcp_optional.extend(mcp_read)
        mcp_optional.extend(mcp_tools_matching(used, "state_read", "spec_govern", "spec_similarity"))
        if not mcp_tools_matching(used, "state_read"):
            soft_notes.append("soft_missing:state_read")
        if mcp_read and not mcp_write:
            soft_notes.append("soft:spec_read_without_write")
        if not found:
            return StepAcceptance(
                ok=False,
                reason="SPEC HTML missing/too small under SPEC/arcbench/",
                skills_seen=skills_seen,
                soft_notes=tuple(soft_notes),
            )
        # v5ag: spec_write preferred soft — do not fail-closed on missing MCP proof alone.
        if not mcp_write:
            soft_notes.append("soft:spec_write_missing_html_ok")
        leaf_ids = leaf_requirement_ids(module.subtree)
        prd_dir = output_dir / "PRD"
        prd_hits: list[str] = []
        if prd_dir.is_dir():
            prd_hits.extend(str(p) for p in prd_dir.rglob("*") if p.is_file())
        prd_hits.extend(str(p) for p in sdir.glob("prd*") if p.is_file())
        if not prd_hits and leaf_ids:
            soft_notes.append("soft:prd_missing_subtree_rich_ok")
        if leaf_ids:
            html_blob = ""
            for fp in found:
                try:
                    html_blob += Path(fp).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    pass
            missing_html = [lid for lid in leaf_ids if not html_has_data_req(html_blob, lid)]
            if missing_html:
                soft_notes.append(
                    "soft:spec_leaf_data_req_incomplete:" + ",".join(missing_html[:8])
                )
        artifacts = found + mcp_write + mcp_read
        mcp_required = list(mcp_write)

    elif step.step_id == "govern":
        # v5w hard audit = agent + tool chain (not single signal).
        # Require BOTH in-session MCP tools. Receipt JSON alone is insufficient;
        # receipts count only as side mirrors when the matching tool was used this STEP.
        prd_tools = mcp_tools_matching(used, "prd_govern")
        spec_tools = mcp_tools_matching(used, "spec_govern")
        prd_receipt = sdir / "prd_govern.json"
        spec_receipt = sdir / "spec_govern.json"
        receipt_paths: list[str] = []
        if prd_receipt.is_file() and prd_receipt.stat().st_size > 2:
            receipt_paths.append(str(prd_receipt))
            if not prd_tools:
                soft_notes.append("soft:prd_govern_receipt_without_in_session_tool")
        if spec_receipt.is_file() and spec_receipt.stat().st_size > 2:
            receipt_paths.append(str(spec_receipt))
            if not spec_tools:
                soft_notes.append("soft:spec_govern_receipt_without_in_session_tool")
        mcp_optional.extend(mcp_tools_matching(used, "trace", "state_read"))
        if not prd_tools:
            return StepAcceptance(
                ok=False,
                reason=(
                    "govern STEP requires in-session mcp__arch__prd_govern tool use "
                    "(receipt alone insufficient)"
                ),
                skills_seen=skills_seen,
                artifacts=tuple(receipt_paths),
                mcp_optional_seen=tuple(mcp_optional),
                soft_notes=tuple(soft_notes),
            )
        if not spec_tools:
            return StepAcceptance(
                ok=False,
                reason=(
                    "govern STEP requires in-session mcp__arch__spec_govern tool use "
                    "(receipt alone insufficient)"
                ),
                skills_seen=skills_seen,
                artifacts=tuple(list(prd_tools) + receipt_paths),
                mcp_optional_seen=tuple(mcp_optional),
                soft_notes=tuple(soft_notes),
            )
        artifacts = list(dict.fromkeys(list(prd_tools) + list(spec_tools) + receipt_paths))
        mcp_required = list(prd_tools) + list(spec_tools)

    elif step.step_id == "test_dag":
        dag = sdir / "test_dag.json"
        found: list[str] = []
        has_api = False
        has_ui = False
        key_note = "no test_dag.json"

        def _nonempty(value: object) -> bool:
            if value is None:
                return False
            if isinstance(value, (list, dict, tuple, set)):
                return len(value) > 0
            if isinstance(value, str):
                return bool(value.strip())
            return bool(value)

        def _extract_api_ui(payload: object) -> tuple[bool, bool, str]:
            if not isinstance(payload, dict):
                return False, False, f"payload_type={type(payload).__name__}"
            api = payload.get("api")
            if not _nonempty(api):
                api = payload.get("api_tests")
            ui = payload.get("ui")
            if not _nonempty(ui):
                ui = payload.get("ui_tests")
            tests_obj = payload.get("tests")
            if isinstance(tests_obj, dict):
                if not _nonempty(api):
                    api = tests_obj.get("api") or tests_obj.get("api_tests")
                if not _nonempty(ui):
                    ui = tests_obj.get("ui") or tests_obj.get("ui_tests")
            elif isinstance(tests_obj, list):
                api_items = [
                    t for t in tests_obj
                    if isinstance(t, dict)
                    and str(t.get("type") or t.get("kind") or "").lower() in {"api", "backend", "http"}
                ]
                ui_items = [
                    t for t in tests_obj
                    if isinstance(t, dict)
                    and str(t.get("type") or t.get("kind") or "").lower()
                    in {"ui", "e2e", "frontend", "playwright", "browser"}
                ]
                if not _nonempty(api) and api_items:
                    api = api_items
                if not _nonempty(ui) and ui_items:
                    ui = ui_items
            top_keys = sorted(str(k) for k in payload.keys())
            note = (
                f"keys={top_keys}; "
                f"api_nonempty={_nonempty(api)}; ui_nonempty={_nonempty(ui)}"
            )
            return _nonempty(api), _nonempty(ui), note

        if dag.is_file():
            try:
                payload = json.loads(dag.read_text(encoding="utf-8"))
            except Exception as exc:
                return StepAcceptance(
                    ok=False,
                    reason=f"test_dag.json unreadable: {exc}",
                    skills_seen=skills_seen,
                )
            has_api, has_ui, key_note = _extract_api_ui(payload)
            found.append(str(dag))

        project = discover_test_project(output_dir)
        test_files = find_test_files(project) if project else []
        api_files: list[str] = []
        ui_files: list[str] = []
        for p in test_files:
            low = str(p).lower().replace("\\", "/")
            name = Path(p).name.lower()
            if any(tok in low for tok in ("/api/", "api.", "api-", "_api", "backend")) or name.startswith("api"):
                api_files.append(str(p))
            if any(
                tok in low
                for tok in ("/ui/", "ui.", "ui-", "_ui", "e2e", "playwright", "frontend", "browser")
            ) or name.startswith("ui"):
                ui_files.append(str(p))
        if test_files:
            found.extend(str(p) for p in test_files[:8])
        if api_files:
            has_api = True
        if ui_files:
            has_ui = True

        mcp_optional.extend(mcp_tools_matching(used, "trace", "state_read"))
        if not (has_api and has_ui):
            return StepAcceptance(
                ok=False,
                reason=(
                    "test_dag.json must include both non-empty api and ui test entries "
                    f"({key_note}; api_files={len(api_files)}, ui_files={len(ui_files)})"
                ),
                skills_seen=skills_seen,
                artifacts=tuple(found[:8]) if found else (),
                mcp_optional_seen=tuple(mcp_optional),
            )
        if not found:
            return StepAcceptance(
                ok=False,
                reason="TEST DAG missing: need .arc/steps/<id>/test_dag.json (api+ui) and/or test files",
                skills_seen=skills_seen,
            )
        artifacts = found

    elif step.step_id == "implement":
        # v5ag thin: business writes OR implement.json OR tests-green evidence. MINIMAL gates.
        impl_receipt = sdir / "implement.json"
        found: list[str] = []
        if impl_receipt.is_file() and impl_receipt.stat().st_size > 2:
            found.append(str(impl_receipt))
        for root_name in ("frontend/src", "backend/src", "src", "frontend", "backend"):
            root = output_dir / root_name
            if not root.exists():
                continue
            for p in root.rglob("*"):
                if (
                    p.is_file()
                    and p.suffix in {".ts", ".tsx", ".js", ".jsx", ".py", ".vue"}
                    and "node_modules" not in p.parts
                    and p.stat().st_size >= 40
                ):
                    found.append(str(p))
                    if len(found) >= 6:
                        break
            if len(found) >= 2:
                break
        write_ok, write_proof = implement_write_progress(output_dir, sdir, result)
        if write_ok:
            soft_notes.append(f"write_progress:{write_proof}")
            found.append(write_proof)
        # tests-green evidence (Agent ran vitest mid-dev)
        tests_green = False
        for name in ("tests_green.json", "vitest_ok.json", "implement_tests.json"):
            tp = sdir / name
            if tp.is_file() and tp.stat().st_size > 2:
                try:
                    payload = json.loads(tp.read_text(encoding="utf-8"))
                except Exception:
                    payload = {}
                ok_flag = False
                if isinstance(payload, dict):
                    ok_flag = bool(payload.get("ok") or payload.get("passed") or payload.get("green"))
                if ok_flag or "ok" in tp.read_text(encoding="utf-8", errors="replace").lower()[:200]:
                    tests_green = True
                    found.append(str(tp))
                    soft_notes.append(f"tests_green:{tp.name}")
                    break
        search_hits = _receipt_or_tool("search_code.json", "search_code")
        if search_hits:
            soft_notes.append("soft:search_code_seen")
            mcp_optional.extend(search_hits)
        mcp_optional.extend(
            mcp_tools_matching(
                used, "kb_query", "kb_inject", "refactor_code", "format_code",
                "solver", "design_style", "design_asset",
            )
        )
        if not found and not write_ok and not tests_green:
            return StepAcceptance(
                ok=False,
                reason=(
                    "IMPLEMENT thin gate: need business file under frontend|backend|src, "
                    "or implement.json, or tests-green evidence"
                ),
                skills_seen=skills_seen,
                soft_notes=tuple(soft_notes),
            )
        artifacts = list(dict.fromkeys(found + search_hits))[:16]
        mcp_required = []  # thin: no hard MCP requirement

    elif step.step_id == "audit_refactor":
        # Soft STEP: skill already checked; never fail-closed on missing MCP.
        insight = _receipt_or_tool("arch_insight.json", "arch_insight")
        gate = _receipt_or_tool("commit_gate.json", "commit_gate")
        mcp_optional.extend(insight + gate)
        mcp_optional.extend(mcp_tools_matching(used, "refactor_code", "format_code"))
        if gate:
            commit_gate_status = "ok"
        elif mcp_tools_matching(used, "commit_gate"):
            commit_gate_status = "ok"
        else:
            commit_gate_status = "missing"
            soft_notes.append("soft_missing:commit_gate")
        if not insight and not mcp_tools_matching(used, "arch_insight"):
            soft_notes.append("soft_missing:arch_insight")
        artifacts = list(dict.fromkeys(insight + gate)) or ["audit_refactor:soft"]

    elif step.step_id == "batch_test":
        validation = run_module_validation(output_dir, module)
        artifacts = [f"validation:{validation.reason}", f"cmd:{' '.join(validation.cmd)}"]
        # Soft commit_gate: record status; never fail STEP for commit_gate alone.
        gate_files = _receipt_or_tool("commit_gate.json", "commit_gate")
        if gate_files:
            commit_gate_status = "ok"
            artifacts.extend(gate_files)
        else:
            commit_gate_status = "missing"
            soft_notes.append("soft_missing:commit_gate")
        mcp_optional.extend(mcp_tools_matching(used, "trace_failure", "lifecycle", "console_messages"))
        # Always persist validation receipt (pass or fail) so repair sees vitest output.
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "batch_validation.json").write_text(
            json.dumps(
                {
                    "ok": validation.ok,
                    "exit_code": validation.exit_code,
                    "cmd": validation.cmd,
                    "reason": validation.reason,
                    "project_dir": validation.project_dir,
                    "log_tail": validation.log_tail[-6000:],
                    "commit_gate_status": commit_gate_status,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        artifacts.append(str(sdir / "batch_validation.json"))
        if not validation.ok:
            log_snip = (validation.log_tail or "")[-4000:]
            soft_notes.append(f"vitest_log:{log_snip}")
            # Also write a dedicated repair feed the implement STEP will load.
            repair_feed = sdir / "repair_note.txt"
            repair_feed.write_text(
                (
                    f"batch_test harness validation failed: {validation.reason}\n"
                    f"cmd: {' '.join(validation.cmd)}\n"
                    f"project_dir: {validation.project_dir}\n"
                    f"VITEST OUTPUT:\n{log_snip}\n"
                ),
                encoding="utf-8",
            )
            return StepAcceptance(
                ok=False,
                reason=f"batch_test harness validation failed: {validation.reason}",
                skills_seen=skills_seen,
                artifacts=tuple(artifacts),
                mcp_optional_seen=tuple(mcp_optional),
                commit_gate_status=commit_gate_status,
                soft_notes=tuple(soft_notes),
            )
        # v5ag: vitest+build via run_module_validation is the thin platform gate.
        # No homemade feature-wiring / export-default scanners as Agent substitutes.
        soft_notes.append("v5ag_thin_batch_gate:vitest_and_build")
    else:
        return StepAcceptance(ok=False, reason=f"unknown step {step.step_id}", skills_seen=skills_seen)

    return StepAcceptance(
        ok=True,
        reason=f"step {step.step_id} acceptance passed",
        skills_seen=skills_seen,
        artifacts=tuple(artifacts),
        mcp_required=tuple(mcp_required),
        mcp_optional_seen=tuple(mcp_optional),
        commit_gate_status=commit_gate_status,
        soft_notes=tuple(soft_notes),
    )



def step_prompt(
    module: RequirementModule,
    requirements_dir: Path,
    skills_dir: Path | None,
    completed: list[str],
    task_type: str,
    step: StepDef,
    *,
    attempt: int = 1,
    prior_failure: str | None = None,
    validation_failure: str | None = None,
    validation_repair: int = 0,
    wave_ctx: dict[str, Any] | None = None,
) -> str:
    completed_text = ", ".join(completed) if completed else "none"
    skills_text = (
        f"ARC / project skills directory: {skills_dir}."
        if skills_dir
        else "Project skills may be under .claude/skills; GSC plugin skills also available."
    )
    receipt_hint = f".arc/steps/{safe_node_id(module.node_id)}/{step.step_id}.json"
    ok_hint = f".arc/steps/{safe_node_id(module.node_id)}/{step.step_id}.ok"
    recovery = ""
    if validation_failure:
        recovery = (
            f"VALIDATION REPAIR {validation_repair}: harness batch validation failed earlier.\n"
            f"{validation_failure}\n"
        )
    elif prior_failure:
        recovery = (
            f"STEP RETRY {attempt}: previous attempt failed acceptance (fail-closed).\n"
            f"Failure: {prior_failure}\n"
            "Fix the missing artifacts/MCP proofs, then finish this STEP only.\n"
        )
    elif attempt > 1:
        recovery = (
            f"RECOVERY ATTEMPT {attempt}: prior Claude ended on transient failure. "
            "Workspace preserved. Continue THIS STEP only; do not redo earlier STEP receipts.\n"
        )

    mid_dev = ""
    if step.forbid_mid_dev_tests:
        mid_dev = (
            "FORBIDDEN this STEP: do not run vitest/npm test / playwright continuously. "
            "Write/adjust code only; centralized BATCH_TEST runs only after WAVE DOMAIN merge.\n"
        )
    elif step.step_id == "implement":
        mid_dev = (
            "CODING + TEST LOOP (v5ai PHASE IMPLEMENT): write code, run tests (vitest/npm test), "
            "fix, repeat. CC Agent owns the loop. Mid-dev tests ARE allowed. Soft: design_* MCP "
            "if UI needs them (no separate pages STEP). NEVER re-run PRD/SPEC/TEST_DAG.\n"
        )
    batch = ""
    if step.require_batch_test_run:
        batch = (
            "REQUIRED this STEP (PHASE BATCH_TEST — project-wide ONCE after ALL IMPLEMENT "
            "merges — v5ai): run one consolidated project harness test on mainline "
            "(prefer frontend `npx vitest run` / `npm test` + cheap `npm run build`). "
            "FORBIDDEN: serial per-REQ BATCH_TEST (REQ-1 PASS then REQ-2 then REQ-3…). "
            "FORBIDDEN: per-WAVE serial batch while sibling DOMAIN/WAVE work remains. "
            "Harness stamps all project REQs from this one green.\n"
        )

    skill_lines = ", ".join(f"`{s}`" for s in step.required_skills)
    schema_extra = ""
    if step.step_id == "test_dag":
        schema_extra = (
            "\n"
            "HARD schema for THIS STEP — write this file BEFORE ending:\n"
            f"`{receipt_hint}` MUST be valid JSON with NON-EMPTY top-level arrays `api` and `ui`.\n"
            "Do NOT use only api_tests/ui_tests. Do NOT leave arrays empty. Do NOT run the full suite yet.\n"
            "Exact shape example:\n"
            "```json\n"
            "{\n"
            '  "api": [{"id": "api-1", "path": "tests/api/counter.test.ts", "asserts": ["increment"]}],\n'
            '  "ui": [{"id": "ui-1", "path": "tests/ui/counter.spec.ts", "asserts": ["button visible"]}]\n'
            "}\n"
            "```\n"
            "Also create the referenced test files when practical. Acceptance fails without both api and ui.\n"
        )

    mcp_extra = ""
    sid = safe_node_id(module.node_id)
    base = f".arc/steps/{sid}"
    if step.step_id == "prd":
        mcp_extra = (
            "\nPHASE DESIGN / PRD (v5ai — project-wide once for ALL REQs):\n"
            "- Call `mcp__arch__prd` when demand needs splitting; produce PRD under PRD/ or "
            f"`{base}/prd*` covering the whole project.\n"
            "- SKIP OK: if leaf requirements are already detailed for SPEC, write a short skip "
            f"note under `{base}/prd_skip.json` (reason=leaves_already_in_spec) and STOP.\n"
            "- Soft: `mcp__arch__state_read` at most once. ANTI-THRASH: no identical re-calls.\n"
            "- Do NOT invent features. Do NOT call account_manage / debug_binary.\n"
            "- FORBIDDEN: per-REQ re-init of PRD after this PHASE DESIGN completes.\n"
        )
    elif step.step_id == "spec":
        mcp_extra = (
            "\nPHASE DESIGN / SPEC (v5ai — project-wide HTML once for ALL REQs):\n"
            "- Write HTML SPEC under SPEC/arcbench covering ALL project REQs from PRD and/or "
            "already-detailed leaves. Prefer `mcp__arch__spec_write` once (HTML 2.0).\n"
            "- Expand leaf sections with data-req; preserve accessible names/roles/seed/states.\n"
            "- FORBIDDEN: invent features; rename UI strings; Markdown migrate as main path.\n"
            "- Soft: `mcp__arch__spec_read` / `state_read` at most once each. ANTI-THRASH.\n"
            "- Acceptance: SPEC/arcbench HTML (>50B). Prefer also seeing spec_write if used.\n"
            "- Do NOT invent homemade coverage/trace ceremonies. govern is OFF by default.\n"
            "- FORBIDDEN: per-REQ re-init of SPEC after this PHASE DESIGN completes.\n"
        )
    elif step.step_id == "govern":
        mcp_extra = (
            "\nMCP REQUIRED this STEP (fail-closed; HARD AUDIT = agent+tool chain; anti-thrash):\n"
            "- Coverage is only green when the SPEC write + this govern chain runs "
            "(spec_write → prd_govern + spec_govern). Single-signal receipts do not pass.\n"
            f"- Call `mcp__arch__prd_govern` ≥1 (REQUIRED in-session); mirror MCP output → "
            f"`{base}/prd_govern.json` (side receipt only — insufficient alone).\n"
            f"- Call `mcp__arch__spec_govern` ≥1 (REQUIRED in-session); mirror MCP output → "
            f"`{base}/spec_govern.json` (side receipt only — insufficient alone).\n"
            "- HARD: when calling spec_govern / prd_govern, demand coverage of SPEC vs PRD "
            "+ ROOT module.subtree atomic/leaf requirements (ids, accessible names, roles, "
            "seed data, observable states). Note gaps in the MCP-mirrored receipt.\n"
            "- After BOTH prd_govern ≥1 AND spec_govern ≥1 succeed coverage, "
            "then STOP this STEP. Do NOT re-call prd_govern/spec_govern/spec_read/state_read "
            "with the same or near-same args (harness thrash-denies re-audit).\n"
            "- FORBIDDEN: looping re-audit after coverage green; FORBIDDEN as main path: "
            "homemade spec_trace.json — govern MCP tools own chain/coverage.\n"
            "- Soft: `mcp__arch__trace` at most once.\n"
        )
    elif step.step_id == "test_dag":
        mcp_extra = (
            "\nMCP soft this STEP: `mcp__arch__trace` / `mcp__arch__state_read` to list test points. "
            "No MCP hard gate (harness checks api+ui schema).\n"
        )
    elif step.step_id == "implement":
        mcp_extra = (
            "\nPHASE IMPLEMENT (v5ai — CODING + TEST LOOP; Agent owns the loop):\n"
            "- Write|Edit business code under frontend/src, backend/src, or src/.\n"
            "- Run tests (vitest / npm test) as you develop; fix failures; repeat until green.\n"
            "- Soft: `mcp__arch__search_code` / `kb_query` / design_style|design_asset if UI needs them "
            "(NO separate pages STEP — MCP has no pages stage).\n"
            f"- Optional receipt: `{base}/implement.json` with files_written list.\n"
            "- On VALIDATION REPAIR: read repair_note / VITEST OUTPUT and Write|Edit until tests pass.\n"
            "- Do NOT invent features outside TEST_DAG / SPEC. Do NOT thrash identical MCP reads.\n"
            "- Prefer `export default` for App default-imports so `npm run build` stays green.\n"
            "- FORBIDDEN: re-run PRD / SPEC / TEST_DAG during IMPLEMENT (design already finished).\n"
        )
    elif step.step_id == "audit_refactor":
        mcp_extra = (
            "\nMCP soft this STEP (not fail-closed):\n"
            f"- Prefer `mcp__arch__arch_insight` and/or `mcp__arch__commit_gate` "
            f"(write `{base}/arch_insight.json` / `{base}/commit_gate.json`).\n"
            "- Optional: refactor_code / format_code.\n"
        )
    elif step.step_id == "batch_test":
        mcp_extra = (
            "\nPHASE BATCH_TEST (v5ai — ONE consolidated project test after ALL IMPLEMENT merges):\n"
            "- Harness runs vitest / npm test then cheap `npm run build`. Fix failures here.\n"
            "- FORBIDDEN: serial per-REQ BATCH_TEST; FORBIDDEN per-WAVE serial batch; "
            "FORBIDDEN commit_gate-only thrash.\n"
            f"- Soft once: `mcp__arch__commit_gate` → `{base}/commit_gate.json`.\n"
            "- On failure soft: `mcp__arch__trace_failure` then Write/Edit again.\n"
        )

    wave_ctx = wave_ctx or {}
    wave_lines = ""
    if wave_ctx:
        phase = str(wave_ctx.get("phase") or "")
        all_reqs = wave_ctx.get("all_req_ids") or wave_ctx.get("wave_req_ids") or wave_ctx.get("sibling_reqs") or []
        wave_lines = (
            f"PROJECT PHASE: {phase or wave_ctx.get('phase_label', 'unknown')}\n"
            f"WAVE {wave_ctx.get('wave_index', '?')}/{wave_ctx.get('wave_total', '?')}: "
            f"domains={wave_ctx.get('wave_domains', [])}\n"
            f"DOMAIN worktree: {wave_ctx.get('domain_id', '')} cwd={wave_ctx.get('worktree', '')}\n"
            f"Sibling REQs (share worktree / stamps): "
            f"{wave_ctx.get('sibling_reqs', [])}\n"
            f"ALL project REQs: {all_reqs}\n"
            f"WAVE plan: {wave_ctx.get('plan_summary', '')}\n"
            "ORCHESTRATION (v5ai): PHASE DESIGN once (prd→spec→test_dag for ALL REQs) → "
            "PHASE IMPLEMENT once (DOMAIN worktrees for parallel coding ONLY; never re-run "
            "PRD/SPEC/TEST_DAG) → PHASE BATCH_TEST once (one consolidated project test). "
            "No PAGES. Never serial per-REQ design or batch.\n"
        )
        if phase == "design" and wave_ctx.get("all_subtrees"):
            wave_lines += (
                "DESIGN SCOPE — cover ALL of these ROOT subtrees in this PHASE:\n"
                "```json\n"
                f"{json.dumps(wave_ctx.get('all_subtrees'), ensure_ascii=False, indent=2)}\n"
                "```\n"
            )

    return textwrap.dedent(f"""
        You are in an ARC-Bench Official harness STEP round (not a mega-prompt).
        GSC plugin + GSC MCP are loaded. MCP stays ON.

        Target task type: {task_type}
        ROOT module {module.index}/{module.total}: {module.node_id} - {module.name}
        Previously completed ROOT modules: {completed_text}
        Requirement source directory: {requirements_dir}
        Current STEP: {step.step_id} — {step.title}
        {recovery}

        {wave_lines}
        HARD RULES:
        - GSC MCP stays ON. Never disable WaitForMcpServers / never ARC_ENABLE_MCP=0 / never ban MCP.
        - Harness acceptance is fail-closed: only the harness marks green after artifact/MCP gates.
        - This round is ONLY for STEP `{step.step_id}`. Do not perform later STEPs.
        - THREE PHASES once (v5ai): DESIGN (all PRD+SPEC+TEST_DAG) → IMPLEMENT (all coding) → BATCH_TEST (one project test).
        - DOMAIN worktrees are for parallel IMPLEMENT coding ONLY — NEVER re-enter PRD/SPEC/TEST_DAG after DESIGN.
        - FORBIDDEN: per-REQ serial design re-init (REQ-1 IMPLEMENT then REQ-2 PRD…). FORBIDDEN: per-REQ serial BATCH_TEST (REQ-1 PASS→REQ-2→REQ-3…).
        - Prefer main session; do NOT spawn Agent/Task for DOMAIN parallelism (harness owns worktrees).
        - Keep tool outputs small (no huge lockfiles/schemas).
        - Do NOT Read/Bash/cat SKILL.md. Skills (if any) are model-invoked via the Skill tool.
        - Harness acceptance is artifact/MCP gates — not skill force-load.

        STEP goal: {step.goal}
        Exit criteria: {step.exit_criteria}
        {mid_dev}{batch}
        {skills_text}
        Optional related project skills (model-invoked, not required for acceptance): {skill_lines}
        Use only real `mcp__arch__*` tools from the allowlist (never invent names; never account_manage/debug_binary).
        Forbid migrate (`spec_migrate`/`grok_md_migrate`) as the main SPEC path — use HTML spec_write.
        {mcp_extra}{schema_extra}
        When exit criteria are met, write a short receipt JSON to `{receipt_hint}` with keys:
        step_id, artifacts (list of paths), summary.
        The harness writes `{ok_hint}` only after its own acceptance gate passes (artifacts + MCP proofs).

        Work only from this ROOT-child subtree:
        ```json
        {json.dumps(module.subtree, ensure_ascii=False, indent=2)}
        ```

        Finish this STEP only. Summarize MCP tools used and artifacts produced.
    """).strip()


def module_prompt(
    module: RequirementModule,
    requirements_dir: Path,
    skills_dir: Path | None,
    completed: list[str],
    task_type: str,
    *,
    attempt: int = 1,
    validation_failure: str | None = None,
    validation_repair: int = 0,
) -> str:
    """Backward-compatible wrapper; Official path uses step_prompt per STEP."""
    return step_prompt(
        module,
        requirements_dir,
        skills_dir,
        completed,
        task_type,
        OFFICIAL_STEPS[-1] if validation_failure else OFFICIAL_STEPS[0],
        attempt=attempt,
        validation_failure=validation_failure,
        validation_repair=validation_repair,
    )


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

    # ARC runners may mount the output directory with a host uid while the adapter starts as root.
    # Mark only this project as safe so AgentRuntime git operations work across that ownership boundary.
    subprocess.run(["git", "config", "--global", "--add", "safe.directory", str(output_dir)], check=False)
    runtime = AgentRuntime.from_env(project_dir=str(output_dir))
    runtime.traceability.init_store(reset=False)
    runtime.traceability.store_requirement_tree(requirement_tree)
    runtime.git.ensure_repo(create_initial_commit=True)
    runtime.events.mark_run_started("arc-claude-gsc Factory26 run started")

    artifacts_dir = Path(os.environ.get("ARCBENCH_ARTIFACTS_DIR", str(output_dir.parent / "artifacts"))).expanduser().resolve()
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
    # v5ah: install Tao contest CLAUDE.md to user+project before any SDK turn.
    ensure_arc_spawn_gate_softener(output_dir, home_dir=home_dir)
    env["GSC_ARC_PACKAGED_RUNTIME"] = "1"
    env["GSC_RUNTIME_SERVER_BIN"] = str(gsc_dir / "bin" / "gsc-spec-server")
    env["CLAUDE_PLUGIN_ROOT"] = str(gsc_dir)
    env["CLAUDE_PLUGIN_DATA"] = str(plugin_data)
    env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"
    # Claude -p waits for --mcp-config servers up to MCP_TIMEOUT before the first turn.
    # Prefer this over permanently banning WaitForMcpServers / MCP tools.
    env["MCP_TIMEOUT"] = str(mcp_timeout_ms)
    env["PATH"] = os.pathsep.join([str(gsc_dir / "bin"), str(gsc_dir / "lsp" / "web" / "node_modules" / ".bin"), env.get("PATH", "")])

    # Contest primary driver: ClaudeSDKClient + anthropic-proxy (Messages↔chat/completions).
    # Proxy auto-starts when runtime/gateway/anthropic-proxy is packed; opt-out ARC_DISABLE_ANTHROPIC_PROXY=1.
    gateway_proc = _arc_maybe_start_anthropic_proxy(base_url, api_key, model)
    claude_env = env.copy()
    if gateway_proc is not None:
        claude_env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:8787"
        claude_env["ANTHROPIC_API_KEY"] = "arc-local"
        for _k in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY_OLD", "CLAUDE_CODE_API_KEY"):
            claude_env.pop(_k, None)
        # Local CC allowlist: sonnet alias; proxy FORCE_MODEL remaps to contest deepseek.
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
        # Fallback: official OPENAI_* → ANTHROPIC_* mapping (no bridge).
        claude_env = apply_official_claude_env(env, base_url=base_url, api_key=api_key, model=model)
        claude_env["ANTHROPIC_API_KEY"] = ""
        if api_key:
            claude_env["ANTHROPIC_AUTH_TOKEN"] = api_key
    for key in ("SUDO_USER", "SUDO_UID", "SUDO_GID"):
        claude_env.pop(key, None)
    if identity is not None:
        _, _, username = identity
        claude_env["USER"] = username
        claude_env["LOGNAME"] = username

    max_retries = env_int("ARC_MODULE_MAX_RETRIES", 5, minimum=0, maximum=10)
    retry_base_seconds = env_int("ARC_RETRY_BASE_SECONDS", 5, minimum=1, maximum=300)
    retry_max_seconds = env_int("ARC_RETRY_MAX_SECONDS", 60, minimum=1, maximum=600)
    # Max repair Claude sessions AFTER the first harness validation failure.
    # Total validation attempts = 1 + ARC_VALIDATION_MAX_REPAIRS (default 1+4=5, v5ac).
    max_validation_repairs = env_int("ARC_VALIDATION_MAX_REPAIRS", 4, minimum=0, maximum=10)  # v5ac: more room to fix vitest
    max_budget_usd = os.environ.get("ARC_MAX_BUDGET_USD", "150").strip()
    base_urls = configured_base_urls(base_url)
    host = upstream_host(base_urls[0])

    sdk_plugins = sdk_driver.gsc_plugins(gsc_dir)
    print(
        json.dumps(
            {
                "event": "official_claude_env",
                "driver": "ClaudeSDKClient",
                "anthropic_base_url": claude_env.get("ANTHROPIC_BASE_URL"),
                "anthropic_api_key_empty": claude_env.get("ANTHROPIC_API_KEY") == "",
                "anthropic_auth_token_set": bool(claude_env.get("ANTHROPIC_AUTH_TOKEN")),
                "anthropic_model": claude_env.get("ANTHROPIC_MODEL"),
                "gateway_model_discovery": claude_env.get("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"),
                "contest_model": model,
                "sdk_model": ("sonnet" if gateway_proc is not None else sdk_driver.sdk_model_for_options(model)),
                "permission_mode": "acceptEdits",
                "anthropic_proxy": gateway_proc is not None,
                "gsc_plugins": sdk_plugins,
                "gsc_plugin_note": (
                    None
                    if sdk_plugins
                    else "GSC plugin attach via SDK plugins may be unavailable; MCP stays ON via mcp_servers"
                ),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    print(
        json.dumps(
            {
                "event": "arc_runtime_policy",
                "upstream_host": host,
                "fallback_upstream_hosts": [upstream_host(url) for url in base_urls[1:]],
                "model": model,
                "module_max_retries": max_retries,
                "retry_base_seconds": retry_base_seconds,
                "retry_max_seconds": retry_max_seconds,
                "validation_max_repairs": max_validation_repairs,
                "max_budget_usd": max_budget_usd,
                "resume": "workspace+traceability+validation_receipt",
                "mcp_enabled": enable_mcp,
                "mcp_config": str(mcp_config_path) if mcp_config_path else None,
                "mcp_timeout_ms": mcp_timeout_ms,
                "anthropic_proxy": gateway_proc is not None,
                "driver": "ClaudeSDKClient",
                "permission_mode": "acceptEdits",
                "mcp_allowed_tools": gsc_mcp_allowed_tools() if enable_mcp else [],
                "step_loop": [s.step_id for s in official_steps()],
                "design_steps": [s.step_id for s in design_steps()],
                "domain_dev_steps": [s.step_id for s in domain_dev_steps()],
                "wave_batch_steps": [s.step_id for s in wave_batch_steps()],
                "orchestration": "project_phases_design_implement_batch",
                "step_required_skills": {s.step_id: list(s.required_skills) for s in official_steps()},
                "mcp_audit_steps": mcp_audit_steps_enabled(),
                "mcp_n_allowed": len(gsc_mcp_allowed_tools()),
                "mcp_n_disallowed": len(gsc_mcp_disallowed_tool_names(prefixed=False)),
                "thrash_mitigations": [
                    "ClaudeSDKClient_official_driver",
                    "anthropic_proxy_messages_to_chat_completions",
                    "spawn_gate_off",
                    "disallow_Agent_Task_and_bloat_builtins",
                    "mcp_allowedTools_spec_subset",
                    "WaitForMcpServers_once_guidance",
                    "html_spec",
                    "disable_slash_commands",
                    "autocompact_200000",
                    "rapid_refill_retryable_capped",
                    "official_step_loop_fail_closed_artifact_mcp",
                    "v5r_spec_max_turns_140",
                    "v5r_mcp_identical_read_thrash_guard",
                    "v5r_spec_require_spec_write",
                    "v5r_max_turns_soft_accept_if_artifacts_ok",
                    "v5s_max_budget_usd_floor_150",
                    "v5t_official_skills_setting_sources_project_user",
                    "v5t_skills_all_enables_Skill_tool",
                    "v5u_delete_skill_force_load_fail_closed",
                    "v5u_no_skill_md_read_acceptance",
                    "v5u_no_STEP_prompts_forcing_skill_load",
                    "v5v_spec_derive_from_prd_atomic",
                    "v5w_hard_audit_agent_plus_tool_chain",
                    "v5x_phaseA_write_gate_thrash_deny_refill_degrade",
                    "v5y_govern_stop_thrash_accept_green_deny",
                    "v5z_wave_domain_worktree_dag_central_batch_test",
                    "v5aa_merge_domain_worktree_abort_theirs",
                    "v5ak_merge_domain_worktree_clear_untracked",
                    "v5ac_root_cure_write_skeleton_batch_repair",
                    "v5ac_soft_accept_requires_in_attempt_write",
                    "v5ac_batch_test_vitest_feed_implement_repair",
                    "v5ad_wave_central_one_shot_batch_test",
                    "v5ae_govern_green_force_stop_no_supervisor_fail_closed",
                    "v5af_npm_build_hard_gate_after_vitest",
                    "v5ag_thin_cc_orchestrator",
                    "v5ag_no_pages_step",
                    "v5ag_mcp_audit_default_off",
                    "v5ag_implement_code_test_loop",
                    "v5ag_thin_acceptance_no_feature_wiring_theater",
                    "v5ah_full_mcp_skills_perception",
                    "v5ah_contest_user_claude_md",
                    "v5ah_cc_full_config_fail_closed",
                    "v5ah_no_prepared_mcp_disallowed_leak",
                    "v5ai_project_wide_design_implement_batch_phases",
                    "v5ai_no_per_req_design_reentry",
                    "v5ai_no_pages_audit_default_off",
                    "v5ai_implement_no_soft_accept_max_turns",
                    "v5aj_thin_cc_launcher_shell_only",
                    "v5aj_no_harness_supervisor",
                    "v5aj_no_soft_accept_theater",
                    "v5aj_no_feature_wiring_fail_closed",
                    "v5aj_three_phase_once",
                    "v5aj_delete_agent_os_leftover",
                ],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    completed: list[str] = []
    domain_groups = build_domain_groups(modules)
    waves = plan_domain_waves(domain_groups)
    plan_path = write_wave_plan(output_dir, waves)
    print(
        json.dumps(
            {
                "event": "wave_plan",
                "path": str(plan_path),
                "summary": wave_plan_summary(waves),
                "n_waves": len(waves),
                "n_domains": len(domain_groups),
                "n_modules": len(modules),
                "note": (
                    "v5ai: DESIGN once → IMPLEMENT via DOMAIN worktrees (coding only) "
                    "→ BATCH_TEST once project-wide; never per-REQ design re-entry"
                ),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    def execute_steps_for_module(
        *,
        module: RequirementModule,
        steps_to_run: list[StepDef],
        work_dir: Path,
        wave_ctx: dict[str, Any],
        validation_repair: int = 0,
        do_post_validation: bool = False,
    ) -> int:
        """Run fail-closed STEPs for one module in work_dir. Returns 0 or error code.

        do_post_validation=False for DOMAIN DEV (no per-REQ BATCH_TEST).
        do_post_validation=True only for WAVE-central batch_test path.
        """
        max_step_retries = env_int("ARC_STEP_MAX_RETRIES", 2, minimum=0, maximum=8)
        local_repair = validation_repair

        def on_retry(
            attempt: int,
            result: ClaudeRunResult,
            classification: FailureClassification,
            delay: int,
        ) -> None:
            current_url = base_url_for_attempt(base_urls, attempt)
            next_url = base_url_for_attempt(base_urls, attempt + 1)
            payload = {
                "event": "module_retry",
                "req_id": module.node_id,
                "attempt": attempt,
                "next_attempt": attempt + 1,
                "max_retries": max_retries,
                "classification": classification.reason,
                "terminal_reason": result.terminal_reason or "unknown",
                "returncode": result.returncode,
                "api_error_status": result.api_error_status,
                "sleep_seconds": delay,
                "upstream_host": upstream_host(current_url),
                "next_upstream_host": upstream_host(next_url),
                "switch_base_url": current_url != next_url,
                "wave": wave_ctx.get("wave_index"),
                "domain": wave_ctx.get("domain_id"),
            }
            print(json.dumps(payload, ensure_ascii=False), flush=True)
            runtime.events.mark_run_paused(
                f"Transient failure on {module.node_id}: {classification.reason}; retry in {delay}s"
            )

        while True:
            force_repair_restart = False
            for step in steps_to_run:
                if has_step_receipt(work_dir, module.node_id, step.step_id):
                    print(
                        json.dumps(
                            {
                                "event": "step_skip",
                                "req_id": module.node_id,
                                "step_id": step.step_id,
                                "reason": "receipt_ok_present",
                                "work_dir": str(work_dir),
                                "wave": wave_ctx.get("wave_index"),
                                "domain": wave_ctx.get("domain_id"),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    continue

                prior_failure: str | None = None
                accepted = False
                degrade_state = {"active": False}
                repair_log = None
                repair_note = step_dir(work_dir, module.node_id) / "repair_note.txt"
                if local_repair > 0 and step.step_id in ("implement", "batch_test") and repair_note.is_file():
                    repair_log = repair_note.read_text(encoding="utf-8", errors="replace")[-6000:]

                def on_step_retry(
                    attempt: int,
                    result: ClaudeRunResult,
                    classification: FailureClassification,
                    delay: int,
                    _degrade=degrade_state,
                ) -> None:
                    if "rapid_refill" in classification.reason:
                        _degrade["active"] = True
                        print(
                            json.dumps(
                                {
                                    "event": "rapid_refill_needs_degrade",
                                    "req_id": module.node_id,
                                    "step_id": step.step_id,
                                    "attempt": attempt,
                                    "next_attempt": attempt + 1,
                                    "classification": classification.reason,
                                    "note": "next attempt uses degraded system + narrower MCP allow",
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                    on_retry(attempt, result, classification, delay)

                for step_attempt in range(1, max_step_retries + 2):
                    def run_attempt(
                        attempt: int,
                        _step=step,
                        _prior_ref=lambda: prior_failure,
                        _repair=repair_log,
                        _vrep=local_repair,
                        _degrade=degrade_state,
                    ) -> ClaudeRunResult:
                        runtime.events.mark_implementation_started(
                            module.node_id,
                            f"STEP {_step.step_id} ({_step.title}) attempt {attempt} for {module.name}",
                        )
                        prompt = step_prompt(
                            module,
                            requirements_dir,
                            skills_dir,
                            completed,
                            args.task_type,
                            _step,
                            attempt=attempt,
                            prior_failure=_prior_ref(),
                            validation_failure=_repair,
                            validation_repair=_vrep,
                            wave_ctx=wave_ctx,
                        )
                        attempt_base_url = base_url_for_attempt(base_urls, attempt)
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
                            attempt_env["OPENAI_BASE_URL"] = attempt_base_url
                            attempt_env = apply_official_claude_env(
                                attempt_env, base_url=attempt_base_url, api_key=api_key, model=model
                            )
                            attempt_env["ANTHROPIC_API_KEY"] = ""
                            if api_key:
                                attempt_env["ANTHROPIC_AUTH_TOKEN"] = api_key
                            attempt_env = sdk_driver.apply_contest_model_env(attempt_env, model)
                        print(
                            json.dumps(
                                {
                                    "event": "step_started",
                                    "req_id": module.node_id,
                                    "step_id": _step.step_id,
                                    "title": _step.title,
                                    "required_skills": list(_step.required_skills),
                                    "attempt": attempt,
                                    "prior_failure": _prior_ref(),
                                    "validation_repair": _vrep,
                                    "driver": "ClaudeSDKClient",
                                    "work_dir": str(work_dir),
                                    "wave": wave_ctx.get("wave_index"),
                                    "domain": wave_ctx.get("domain_id"),
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                        return run_claude_via_sdk(
                            prompt=prompt,
                            output_dir=work_dir,
                            model=model,
                            claude_bin=claude_bin,
                            gsc_dir=gsc_dir,
                            mcp_config=mcp_config_path,
                            enable_mcp=enable_mcp,
                            attempt_env=attempt_env,
                            skills_dir=skills_dir,
                            max_budget_usd=max_budget_usd,
                            max_turns=sdk_driver.max_turns_for_step(_step.step_id),
                            step_id=_step.step_id,
                            degrade_mode=bool(_degrade.get("active")),
                        )

                    result, api_attempts = execute_with_retry(
                        run_attempt,
                        max_retries=max_retries,
                        base_seconds=retry_base_seconds,
                        max_seconds=retry_max_seconds,
                        on_retry=on_step_retry,
                    )
                    classification = classify_claude_failure(result)
                    # v5aj: thin CC launcher — no harness_supervisor soft brain, no soft-accept theater.
                    # Optional govern (audit OFF by default): if accept already green, FORCE STOP STEP.
                    gov_green = step.step_id == "govern" and govern_accept_already_green(result)
                    if gov_green:
                        print(
                            json.dumps(
                                {
                                    "event": "govern_accept_already_green_force_stop",
                                    "req_id": module.node_id,
                                    "step_id": step.step_id,
                                    "thrash_hit": bool(getattr(result, "thrash_hit", False)),
                                    "mcp_tools_used": list(
                                        getattr(result, "mcp_tools_used", ()) or ()
                                    ),
                                    "wave": wave_ctx.get("wave_index"),
                                    "domain": wave_ctx.get("domain_id"),
                                    "note": "v5aj: evaluate acceptance and STOP STEP (no supervisor)",
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                        force_acc = evaluate_step_acceptance(work_dir, module, step, result)
                        if force_acc.ok:
                            force_acc = StepAcceptance(
                                ok=True,
                                reason=(
                                    f"{force_acc.reason} "
                                    "(v5aj force-stop after govern_accept_already_green)"
                                ),
                                skills_seen=force_acc.skills_seen,
                                missing_skills=force_acc.missing_skills,
                                artifacts=force_acc.artifacts,
                                mcp_required=force_acc.mcp_required,
                                mcp_optional_seen=force_acc.mcp_optional_seen,
                                commit_gate_status=force_acc.commit_gate_status,
                                soft_notes=tuple(
                                    list(force_acc.soft_notes)
                                    + ["v5aj_govern_green_force_stop"]
                                ),
                            )
                            write_step_receipt(
                                work_dir, module.node_id, step, force_acc, claude=result
                            )
                            print(
                                json.dumps(
                                    {
                                        "event": "step_acceptance",
                                        "req_id": module.node_id,
                                        "step_id": step.step_id,
                                        "ok": True,
                                        "reason": force_acc.reason,
                                        "govern_green_force_stop": True,
                                        "wave": wave_ctx.get("wave_index"),
                                        "domain": wave_ctx.get("domain_id"),
                                    },
                                    ensure_ascii=False,
                                ),
                                flush=True,
                            )
                            accepted = True
                            break
                    if result.returncode != 0 or result.is_error:
                        term_reason = (result.terminal_reason or "").lower()
                        max_turns_hit = "max_turns" in term_reason or term_reason.endswith("max_turns")
                        # v5aj: NO soft-accept@max_turns theater. CC owns the loop; fail → retry/exhaust.
                        terminal = {
                            "event": "step_terminal_failure",
                            "req_id": module.node_id,
                            "step_id": step.step_id,
                            "step_attempt": step_attempt,
                            "api_attempts": api_attempts,
                            "classification": classification.reason,
                            "terminal_reason": result.terminal_reason or "unknown",
                            "returncode": result.returncode,
                            "skills_loaded": list(result.skills_loaded),
                            "max_turns_hit": max_turns_hit,
                            "soft_accept_max_turns": False,
                        }
                        print(json.dumps(terminal, ensure_ascii=False), file=sys.stderr, flush=True)
                        prior_failure = classification.reason
                        if step_attempt >= max_step_retries + 1:
                            runtime.events.mark_implementation_failed(
                                module.node_id,
                                f"STEP {step.step_id} Claude failed: {classification.reason}",
                            )
                            runtime.events.mark_test_failed(module.node_id, f"STEP {step.step_id} did not complete")
                            runtime.events.mark_run_failed(
                                f"Module {module.node_id} STEP {step.step_id} failed: {classification.reason}"
                            )
                            return result.returncode or 1
                        continue

                    acceptance = evaluate_step_acceptance(work_dir, module, step, result)
                    write_step_receipt(work_dir, module.node_id, step, acceptance, claude=result)
                    print(
                        json.dumps(
                            {
                                "event": "step_acceptance",
                                "req_id": module.node_id,
                                "step_id": step.step_id,
                                "ok": acceptance.ok,
                                "reason": acceptance.reason,
                                "skills_seen": list(acceptance.skills_seen),
                                "missing_skills": list(acceptance.missing_skills),
                                "artifacts": list(acceptance.artifacts),
                                "step_attempt": step_attempt,
                                "mcp_tools_used": list(result.mcp_tools_used),
                                "wave": wave_ctx.get("wave_index"),
                                "domain": wave_ctx.get("domain_id"),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    if acceptance.ok:
                        accepted = True
                        break
                    prior_failure = acceptance.reason
                    # v5ac: attach vitest log to prior_failure so next attempt / repair sees it.
                    for _note in acceptance.soft_notes:
                        if isinstance(_note, str) and _note.startswith("vitest_log:"):
                            prior_failure = (
                                (prior_failure or "")
                                + "\nVITEST OUTPUT:\n"
                                + _note[len("vitest_log:") :]
                            )
                            break
                    # v5ac root cure: batch_test commit_gate thrash → escalate to implement repair
                    # with vitest output instead of burning step retries on commit_gate-only turns.
                    if (
                        step.step_id == "batch_test"
                        and "harness validation failed" in (acceptance.reason or "")
                        and local_repair < max_validation_repairs
                    ):
                        print(
                            json.dumps(
                                {
                                    "event": "batch_test_harness_fail_escalate_repair",
                                    "req_id": module.node_id,
                                    "step_attempt": step_attempt,
                                    "validation_repair": local_repair,
                                    "max_validation_repairs": max_validation_repairs,
                                    "reason": acceptance.reason,
                                    "wave": wave_ctx.get("wave_index"),
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                        # Ensure repair_note has vitest feed (evaluate_step_acceptance may have written it).
                        rn = step_dir(work_dir, module.node_id) / "repair_note.txt"
                        if not rn.is_file() or rn.stat().st_size < 20:
                            rn.parent.mkdir(parents=True, exist_ok=True)
                            rn.write_text((prior_failure or "")[-6000:], encoding="utf-8")
                        clear_step_receipt(work_dir, module.node_id, "implement")
                        clear_step_receipt(work_dir, module.node_id, "batch_test")
                        clear_step_receipt(work_dir, module.node_id, "audit_refactor")
                        local_repair += 1
                        steps_to_run = [
                            s
                            for s in official_steps()
                            if s.step_id in ("implement", "audit_refactor", "batch_test")
                        ]
                        runtime.events.mark_run_resumed(
                            f"WAVE batch_test→implement repair {local_repair}/{max_validation_repairs} "
                            f"for {module.node_id}; vitest failure fed to repair_note"
                        )
                        accepted = False
                        # Break out of step_attempt + step loops to restart with repair steps.
                        force_repair_restart = True
                        break
                    clear_step_receipt(work_dir, module.node_id, step.step_id)
                    write_step_receipt(work_dir, module.node_id, step, acceptance, claude=result)

                if force_repair_restart:
                    # v5ac: restart outer while with implement+batch_test repair steps.
                    break
                if not accepted:
                    runtime.events.mark_implementation_failed(
                        module.node_id,
                        f"STEP {step.step_id} acceptance failed (fail-closed): {prior_failure}",
                    )
                    runtime.events.mark_test_failed(
                        module.node_id,
                        f"STEP {step.step_id} acceptance failed",
                    )
                    runtime.events.mark_run_failed(
                        f"Module {module.node_id} stopped at STEP {step.step_id}: {prior_failure}"
                    )
                    print(
                        json.dumps(
                            {
                                "event": "step_acceptance_exhausted",
                                "req_id": module.node_id,
                                "step_id": step.step_id,
                                "reason": prior_failure,
                                "required_skills": list(step.required_skills),
                            },
                            ensure_ascii=False,
                        ),
                        file=sys.stderr,
                        flush=True,
                    )
                    return 1

            if force_repair_restart:
                continue
            if not do_post_validation:
                # DOMAIN DEV path: STEPs accepted; NO per-REQ BATCH_TEST / mark_test_passed.
                runtime.events.mark_implementation_done(
                    module.node_id,
                    f"DOMAIN DEV STEPs done for {module.name} (await WAVE merge + BATCH_TEST)",
                )
                return 0

            # WAVE-central batch_test confirmation
            validation = run_module_validation(work_dir, module)
            print(
                json.dumps(
                    {
                        "event": "module_validation",
                        "req_id": module.node_id,
                        "ok": validation.ok,
                        "exit_code": validation.exit_code,
                        "cmd": validation.cmd,
                        "reason": validation.reason,
                        "project_dir": validation.project_dir,
                        "validation_repair": local_repair,
                        "max_validation_repairs": max_validation_repairs,
                        "note": "WAVE-central post-merge BATCH_TEST confirmation",
                        "wave": wave_ctx.get("wave_index"),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            gate = decide_validation_gate(
                validation,
                validation_repair=local_repair,
                max_validation_repairs=max_validation_repairs,
            )
            if gate.action == "pass":
                write_validation_receipt(work_dir, module.node_id, validation)
                runtime.events.mark_implementation_done(
                    module.node_id,
                    f"WAVE BATCH_TEST passed for {module.name}"
                    + (f"; validation_repair {local_repair}" if local_repair else ""),
                )
                runtime.events.mark_test_passed(
                    module.node_id,
                    "Harness local validation passed after WAVE merge BATCH_TEST",
                )
                runtime.git.commit(f"{module.node_id}: {module.name}")
                if module.node_id not in completed:
                    completed.append(module.node_id)
                return 0

            clear_validation_receipt(work_dir, module.node_id)
            fail_msg = validation.reason
            if validation.log_tail:
                fail_msg = f"{validation.reason}\n{validation.log_tail[-2000:]}"
            runtime.events.mark_test_failed(
                module.node_id,
                f"Harness validation failed: {validation.reason}",
            )
            if gate.action == "fail":
                runtime.events.mark_implementation_failed(
                    module.node_id,
                    f"Harness validation exhausted after {local_repair} repair(s): {validation.reason}",
                )
                runtime.events.mark_run_failed(
                    f"Module {module.node_id} harness validation failed after "
                    f"{local_repair} repair(s): {validation.reason}"
                )
                print(
                    json.dumps(
                        {
                            "event": "module_validation_exhausted",
                            "req_id": module.node_id,
                            "validation_repair": local_repair,
                            "max_validation_repairs": max_validation_repairs,
                            "reason": validation.reason,
                            "classification": "non-retryable:validation",
                            "gate": gate.reason,
                        },
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                    flush=True,
                )
                return validation.exit_code or 1

            # v5aj: no harness_supervisor on batch_test_fail — harness rewind only.

            local_repair += 1
            for sid in ("implement", "batch_test"):
                clear_step_receipt(work_dir, module.node_id, sid)
            steps_to_run = [
                s for s in official_steps() if s.step_id in ("implement", "audit_refactor", "batch_test")
            ]
            repair_note = step_dir(work_dir, module.node_id) / "repair_note.txt"
            repair_note.parent.mkdir(parents=True, exist_ok=True)
            repair_note.write_text(fail_msg[-6000:], encoding="utf-8")
            runtime.events.mark_run_resumed(
                f"WAVE validation repair {local_repair}/{max_validation_repairs} for {module.node_id}; "
                "re-running implement+batch_test STEPs"
            )
            print(
                json.dumps(
                    {
                        "event": "step_repair_rewind",
                        "req_id": module.node_id,
                        "validation_repair": local_repair,
                        "replay_steps": [s.step_id for s in steps_to_run],
                        "wave": wave_ctx.get("wave_index"),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    try:
        all_req_ids = [m.node_id for m in modules]
        all_subtrees = [m.subtree for m in modules]

        # ========== PHASE DESIGN (project-wide, once) ==========
        # Finish ALL PRD + SPEC + TEST_DAG for the whole project BEFORE any IMPLEMENT.
        # Never per-REQ / per-DOMAIN re-entry of design STEPs.
        pending_design = [
            m for m in modules if not module_already_passed(runtime, m.node_id, output_dir)
        ]
        print(
            json.dumps(
                {
                    "event": "phase_design_started",
                    "req_ids": all_req_ids,
                    "pending_req_ids": [m.node_id for m in pending_design],
                    "steps": [s.step_id for s in design_steps()],
                    "note": (
                        "v5ai PHASE DESIGN once for ALL REQs — "
                        "no per-REQ PRD/SPEC/TEST_DAG re-init"
                    ),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if pending_design:
            for module in modules:
                ensure_gsc_spec(output_dir, module)
            primary_design = pending_design[0]
            design_ctx = {
                "wave_index": 0,
                "wave_total": len(waves),
                "wave_domains": [g.domain_id for g in domain_groups],
                "domain_id": "PROJECT",
                "worktree": str(output_dir),
                "sibling_reqs": [m.node_id for m in pending_design],
                "all_req_ids": all_req_ids,
                "all_subtrees": all_subtrees,
                "plan_summary": wave_plan_summary(waves),
                "phase": "design",
                "phase_label": "DESIGN",
                "centralized_once": True,
            }
            runtime.events.mark_design_started(
                primary_design.node_id,
                f"PHASE DESIGN project-wide for {len(pending_design)} REQs",
            )
            rc = execute_steps_for_module(
                module=primary_design,
                steps_to_run=list(design_steps()),
                work_dir=output_dir,
                wave_ctx=design_ctx,
                do_post_validation=False,
            )
            if rc != 0:
                return rc
            stamp_project_design_siblings(
                output_dir=output_dir,
                primary=primary_design,
                siblings=pending_design[1:],
            )
            # Mirror design receipts into every DOMAIN worktree later via merge/base;
            # also stamp on mainline for REQs already covered.
            for m in pending_design:
                runtime.events.mark_design_done(
                    m.node_id,
                    f"PHASE DESIGN done (project-wide; primary={primary_design.node_id})",
                )
            print(
                json.dumps(
                    {
                        "event": "phase_design_completed",
                        "primary_req": primary_design.node_id,
                        "stamped_siblings": [m.node_id for m in pending_design[1:]],
                        "steps": [s.step_id for s in design_steps()],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        # ========== PHASE IMPLEMENT (DOMAIN worktrees for parallel coding ONLY) ==========
        print(
            json.dumps(
                {
                    "event": "phase_implement_started",
                    "n_waves": len(waves),
                    "n_domains": len(domain_groups),
                    "req_ids": all_req_ids,
                    "steps": [s.step_id for s in domain_dev_steps()],
                    "note": (
                        "v5ai PHASE IMPLEMENT — coding only in DOMAIN worktrees; "
                        "NEVER re-run PRD/SPEC/TEST_DAG"
                    ),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        for wave in waves:
            wave_modules = [m for g in wave.domains for m in g.modules]
            print(
                json.dumps(
                    {
                        "event": "wave_started",
                        "wave_index": wave.wave_index,
                        "wave_total": len(waves),
                        "domains": [g.domain_id for g in wave.domains],
                        "req_ids": [m.node_id for m in wave_modules],
                        "phase": "implement",
                        "note": (
                            "IMPLEMENT coding tracks only; design already finished project-wide"
                        ),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            domain_worktrees: dict[str, Path] = {}

            for group in wave.domains:
                wt = ensure_domain_worktree(output_dir, group.domain_id)
                domain_worktrees[group.domain_id] = wt
                ensure_arc_spawn_gate_softener(wt, home_dir=home_dir)
                for module in group.modules:
                    ensure_gsc_spec(wt, module)
                    # Propagate project DESIGN receipts into DOMAIN worktree so
                    # domain_dev_complete / agents see design done (no re-init).
                    for step in design_steps():
                        if has_step_receipt(output_dir, module.node_id, step.step_id) and not has_step_receipt(
                            wt, module.node_id, step.step_id
                        ):
                            dest_dir = step_dir(wt, module.node_id)
                            dest_dir.mkdir(parents=True, exist_ok=True)
                            for src, dst_fn in (
                                (
                                    step_receipt_json_path(output_dir, module.node_id, step.step_id),
                                    step_receipt_json_path,
                                ),
                                (
                                    step_receipt_ok_path(output_dir, module.node_id, step.step_id),
                                    step_receipt_ok_path,
                                ),
                            ):
                                if src.is_file():
                                    dst_fn(wt, module.node_id, step.step_id).write_text(
                                        src.read_text(encoding="utf-8"),
                                        encoding="utf-8",
                                    )

                wave_ctx_base = {
                    "wave_index": wave.wave_index,
                    "wave_total": len(waves),
                    "wave_domains": [g.domain_id for g in wave.domains],
                    "domain_id": group.domain_id,
                    "worktree": str(wt),
                    "sibling_reqs": [m.node_id for m in group.modules],
                    "all_req_ids": all_req_ids,
                    "plan_summary": wave_plan_summary(waves),
                    "phase": "implement",
                    "phase_label": "IMPLEMENT",
                }
                print(
                    json.dumps(
                        {
                            "event": "domain_started",
                            "wave_index": wave.wave_index,
                            "domain_id": group.domain_id,
                            "worktree": str(wt),
                            "req_ids": [m.node_id for m in group.modules],
                            "depends_on": list(group.depends_on),
                            "conflicts_with": list(group.conflicts_with),
                            "phase": "implement",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

                pending_modules: list[RequirementModule] = []
                for module in group.modules:
                    if module_already_passed(runtime, module.node_id, output_dir):
                        print(
                            json.dumps(
                                {
                                    "event": "module_skip",
                                    "req_id": module.node_id,
                                    "reason": "PASSED_with_validation_receipt",
                                    "wave": wave.wave_index,
                                    "domain": group.domain_id,
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                        if module.node_id not in completed:
                            completed.append(module.node_id)
                        continue
                    pending_modules.append(module)

                # IMPLEMENT only — design already done project-wide (v5ai).
                implement_batch = list(domain_dev_steps())

                if pending_modules and implement_batch:
                    primary = pending_modules[0]
                    print(
                        json.dumps(
                            {
                                "event": "domain_implement_batch",
                                "wave_index": wave.wave_index,
                                "domain_id": group.domain_id,
                                "primary_req": primary.node_id,
                                "sibling_reqs": [m.node_id for m in pending_modules],
                                "steps": [s.step_id for s in implement_batch],
                                "note": "IMPLEMENT coding only — no PRD/SPEC/TEST_DAG",
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    for module in pending_modules:
                        print(
                            f"[arc-claude-gsc] WAVE{wave.wave_index} DOMAIN {group.domain_id} "
                            f"IMPLEMENT {module.index}/{module.total}: {module.node_id} - {module.name}",
                            flush=True,
                        )
                    rc = execute_steps_for_module(
                        module=primary,
                        steps_to_run=list(implement_batch),
                        work_dir=wt,
                        wave_ctx=wave_ctx_base,
                        do_post_validation=False,
                    )
                    if rc != 0:
                        return rc
                    for sibling in pending_modules[1:]:
                        for step in implement_batch:
                            if has_step_receipt(wt, primary.node_id, step.step_id) and not has_step_receipt(
                                wt, sibling.node_id, step.step_id
                            ):
                                src_json = step_receipt_json_path(wt, primary.node_id, step.step_id)
                                src_ok = step_receipt_ok_path(wt, primary.node_id, step.step_id)
                                dest_dir = step_dir(wt, sibling.node_id)
                                dest_dir.mkdir(parents=True, exist_ok=True)
                                if src_json.is_file():
                                    raw = src_json.read_text(encoding="utf-8")
                                    try:
                                        payload = json.loads(raw)
                                    except Exception:
                                        payload = {}
                                    if isinstance(payload, dict):
                                        payload["req_id"] = sibling.node_id
                                        payload["shared_from"] = primary.node_id
                                        payload["domain_implement_batch"] = True
                                        step_receipt_json_path(wt, sibling.node_id, step.step_id).write_text(
                                            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                                            encoding="utf-8",
                                        )
                                    else:
                                        step_receipt_json_path(wt, sibling.node_id, step.step_id).write_text(
                                            raw, encoding="utf-8"
                                        )
                                if src_ok.is_file():
                                    step_receipt_ok_path(wt, sibling.node_id, step.step_id).write_text(
                                        src_ok.read_text(encoding="utf-8"),
                                        encoding="utf-8",
                                    )
                        runtime.events.mark_implementation_done(
                            sibling.node_id,
                            f"DOMAIN IMPLEMENT batch shared from {primary.node_id}",
                        )

                ok, reason = domain_dev_complete(wt, group, skip_req_ids=set(completed))
                print(
                    json.dumps(
                        {
                            "event": "domain_accept",
                            "wave_index": wave.wave_index,
                            "domain_id": group.domain_id,
                            "ok": ok,
                            "reason": reason,
                            "worktree": str(wt),
                            "phase": "implement",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                if not ok:
                    runtime.events.mark_run_failed(
                        f"DOMAIN {group.domain_id} accept failed before merge: {reason}"
                    )
                    return 1
                commit_domain_worktree(
                    wt,
                    group.domain_id,
                    f"domain {group.domain_id}: IMPLEMENT accept (WAVE{wave.wave_index})",
                )

            # --- Merge all passed DOMAIN worktrees to mainline ---
            for group in wave.domains:
                wt = domain_worktrees[group.domain_id]
                print(
                    json.dumps(
                        {
                            "event": "domain_merge",
                            "wave_index": wave.wave_index,
                            "domain_id": group.domain_id,
                            "worktree": str(wt),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                try:
                    merge_domain_worktree(output_dir, group.domain_id, wt)
                except Exception as merge_exc:
                    merge_retry_done = False
                    re_implement_ids = [m.node_id for m in group.modules]
                    if merge_retry_done:
                        continue
                    runtime.events.mark_run_failed(
                        f"WAVE{wave.wave_index} merge DOMAIN {group.domain_id} failed: {merge_exc}"
                    )
                    print(
                        json.dumps(
                            {
                                "event": "domain_merge_failed",
                                "wave_index": wave.wave_index,
                                "domain_id": group.domain_id,
                                "error": str(merge_exc),
                            },
                            ensure_ascii=False,
                        ),
                        file=sys.stderr,
                        flush=True,
                    )
                    return 1

            ensure_arc_spawn_gate_softener(output_dir, home_dir=home_dir)
            # v5ai: NO per-WAVE BATCH_TEST — project-wide BATCH after all IMPLEMENT merges.
            print(
                json.dumps(
                    {
                        "event": "wave_implement_merged",
                        "wave_index": wave.wave_index,
                        "req_ids": [m.node_id for m in wave_modules],
                        "domains": [g.domain_id for g in wave.domains],
                        "note": "IMPLEMENT merge done; BATCH_TEST deferred to project phase",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        # ========== PHASE BATCH_TEST (project-wide, once) ==========
        pending_batch = [
            m for m in modules if not module_already_passed(runtime, m.node_id, output_dir)
        ]
        print(
            json.dumps(
                {
                    "event": "phase_batch_test_started",
                    "req_ids": all_req_ids,
                    "pending_req_ids": [m.node_id for m in pending_batch],
                    "primary_req": pending_batch[0].node_id if pending_batch else None,
                    "note": (
                        "v5ai PHASE BATCH_TEST once on mainline after ALL IMPLEMENT "
                        "merges (NOT serial per-REQ; NOT per-WAVE)"
                    ),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if pending_batch:
            primary = pending_batch[0]
            wave_ctx_batch = {
                "wave_index": len(waves),
                "wave_total": len(waves),
                "wave_domains": [g.domain_id for g in domain_groups],
                "domain_id": "PROJECT",
                "worktree": str(output_dir),
                "sibling_reqs": [m.node_id for m in modules],
                "wave_req_ids": all_req_ids,
                "all_req_ids": all_req_ids,
                "plan_summary": wave_plan_summary(waves),
                "phase": "batch_test",
                "phase_label": "BATCH_TEST",
                "centralized_once": True,
            }
            rc = execute_steps_for_module(
                module=primary,
                steps_to_run=list(wave_batch_steps()),
                work_dir=output_dir,
                wave_ctx=wave_ctx_batch,
                do_post_validation=True,
            )
            if rc != 0:
                return rc
            siblings = pending_batch[1:]
            if siblings:
                stamp_wave_batch_siblings(
                    runtime=runtime,
                    output_dir=output_dir,
                    primary=primary,
                    siblings=siblings,
                    wave_index=0,
                    completed=completed,
                )
            print(
                json.dumps(
                    {
                        "event": "phase_batch_test_completed",
                        "primary_req": primary.node_id,
                        "stamped_siblings": [m.node_id for m in siblings],
                        "note": "PHASE BATCH_TEST done; siblings stamped without serial agent batch",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        runtime.events.mark_run_completed("All project phases completed (DESIGN→IMPLEMENT→BATCH_TEST)")
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