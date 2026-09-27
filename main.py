#!/usr/bin/env python3
from __future__ import annotations

import argparse
import atexit
from collections import deque
import hashlib
import importlib.metadata
import json
import os
import pwd
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


SUBMISSION_DIR = Path(os.environ.get("ARCBENCH_SUBMISSION_DIR", Path(__file__).resolve().parent))
LOCK_PATH = SUBMISSION_DIR / "runtime.lock.json"
RUNTIME_DIR = SUBMISSION_DIR / "runtime"
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
    """One Official MCP-first harness STEP with force-loaded skills + acceptance gate."""
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


# Official STEP loop (fail-closed). Advance only when skill proof + artifacts pass.
OFFICIAL_STEPS: tuple[StepDef, ...] = (
    StepDef(
        step_id="prd",
        title="PRD",
        required_skills=("architect",),
        goal="Build / refine the product PRD for this ROOT module via GSC MCP (prd / state_*).",
        exit_criteria="PRD artifact under PRD/ or .arc/steps/<id>/prd/ plus skills_loaded proof.",
    ),
    StepDef(
        step_id="spec",
        title="SPEC",
        required_skills=("architect",),
        goal="Derive HTML SPEC under SPEC/arcbench via GSC MCP spec_read/spec_write (no Markdown migrate).",
        exit_criteria="SPEC/arcbench HTML for this module exists/updated plus skills_loaded proof.",
    ),
    StepDef(
        step_id="test_dag",
        title="TEST API+UI DAG",
        required_skills=("arcbench-traceability",),
        goal="Author API + UI test DAG / cases (files + traceability); do not run the full suite yet.",
        exit_criteria="Test DAG receipt JSON with api+ui entries and/or real test files; skill proof.",
    ),
    StepDef(
        step_id="pages",
        title="PAGES + UX-UI designer",
        required_skills=("designer",),
        goal="FORCE-LOAD UX-UI designer skill, then design/build pages/UI for this module.",
        exit_criteria="designer skill proof + UI/page files under frontend/src (or pages receipt).",
    ),
    StepDef(
        step_id="implement",
        title="DEV implement (no mid-dev tests)",
        required_skills=("arcbench-checkpoint",),
        goal="Implement remaining logic/API/UI wiring. Do NOT run tests continuously mid-development.",
        exit_criteria="Implementation receipt + checkpoint skill proof; no requirement to pass tests yet.",
        forbid_mid_dev_tests=True,
    ),
    StepDef(
        step_id="batch_test",
        title="BATCH test (after code done)",
        required_skills=("arcbench-runtime-signals",),
        goal="After code is done, run batch/centralized tests once (vitest/npm test).",
        exit_criteria="runtime-signals skill proof + harness local validation passes.",
        require_batch_test_run=True,
    ),
)


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




def _arc_maybe_start_anthropic_proxy(base_url: str, api_key: str, model: str) -> subprocess.Popen | None:
    """Claude Messages -> OpenAI chat. Client auth MUST be ANTHROPIC_API_KEY=arc-local."""
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
# them and trips rapid_refill_breaker. Keep MCP ON but advertise only SPEC/state
# essentials via Claude mcp-config allowedTools (server stays connected).
DEFAULT_GSC_MCP_ALLOWED_TOOLS = (
    "spec_read",
    "spec_write",
    "state_read",
    "state_update",
    "lifecycle",
    "query",
    "artifact_read",
    "artifact_grep",
    "commit_gate",
    "workflow_guard",
    "prd",
)


def gsc_mcp_allowed_tools() -> list[str]:
    """MCP tool allowlist (short names). Override with ARC_MCP_ALLOWED_TOOLS=a,b,c."""
    raw = os.environ.get("ARC_MCP_ALLOWED_TOOLS", "").strip()
    if raw:
        tools = [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]
        if tools:
            return tools
    return list(DEFAULT_GSC_MCP_ALLOWED_TOOLS)


def write_gsc_mcp_config(gsc_dir: Path, dest_dir: Path) -> Path:
    """Write a Claude --mcp-config that points at the packaged GSC bootstrap.

    Includes allowedTools so Claude loads SPEC/state schemas only — MCP stays
    connected (WaitForMcpServers still allowed) without the full ~70-tool surface.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    bootstrap = gsc_dir / "mcp" / "src" / "bootstrap.mjs"
    require_file(bootstrap, "GSC MCP bootstrap")
    config_path = dest_dir / "gsc-mcp.json"
    allowed = gsc_mcp_allowed_tools()
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
            return FailureClassification(True, f"retryable:{marker}")

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
        """Audit MCP tool_use + Skill loads for Official STEP acceptance gates."""
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
                # normalize path-ish skill refs to basename
                skill_name = skill_name.rstrip("/").split("/")[-1]
                with mcp_lock:
                    skill_loads.append(skill_name)
                print(
                    json.dumps(
                        {"event": "skill_loaded", "skill": skill_name},
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
    if exit_code == 0:
        return ValidationResult(
            ok=True,
            exit_code=0,
            cmd=cmd,
            log_tail=log_tail,
            reason="harness local validation passed",
            project_dir=str(project_dir),
        )
    return ValidationResult(
        ok=False,
        exit_code=exit_code,
        cmd=cmd,
        log_tail=log_tail,
        reason=f"harness validation failed: exit={exit_code} cmd={' '.join(cmd)}",
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


def _html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def ensure_gsc_spec(output_dir: Path, module: RequirementModule) -> Path:
    """Materialize the ARC requirement as GSC HTML 2.0 SPEC (never Markdown).

    Writing SPEC/*.md forces GSC's migrate path; ARC runners lack a migrate provider,
    which previously caused thrash. HTML 2.0 lets GSC MCP use spec_read/spec_write
    without banning MCP.
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
    html_path.write_text(
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN" data-spec-root>\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        f'  <meta name="spec-file" content="{safe_id}">\n'
        '  <meta name="spec-category" content="arcbench">\n'
        f'  <meta name="spec-title" content="{title}">\n'
        '  <script type="application/ld+json">\n'
        f'  {{ "@context":"https://spec.gsc.local/v1", "@type":"SpecDocument", "id":"{safe_id}", "dependencies":[], "children":[] }}\n'
        "  </script>\n"
        f"  <title>{title}</title>\n"
        "</head>\n"
        "<body>\n"
        "  <header data-spec-header>\n"
        f"    <h1>{title}</h1>\n"
        "  </header>\n"
        '  <main data-spec-content>\n'
        f'    <section id="s-req" data-section="requirements" data-req="{_html_escape(module.node_id)}" '
        'data-req-status="unknown" data-req-domain="arcbench">\n'
        f"      <h2>{title}</h2>\n"
        "      <p>Implement from the task JSON in the agent prompt. Prefer GSC MCP SPEC tools when available.</p>\n"        "      <p data-arc-spawn>ARC: implement in the main session; avoid Agent/Task spawn. If you must spawn a coding subagent, include a literal ## TASK-HEADER block (task/domain/archaeology/reuse-decision/scope/completion/retry/stop). WaitForMcpServers at most once. Harness vitest/npm test grants green.</p>\n"
        f"      <p data-arc-brief>{brief}</p>\n"
        "    </section>\n"
        "  </main>\n"
        "</body>\n"
        "</html>\n",
        encoding="utf-8",
    )
    return html_path


def ensure_arc_spawn_gate_softener(output_dir: Path) -> None:
    """Soften GSC SPAWN-GATE without disabling MCP; encode Official STEP loop."""
    claude_md = output_dir / "CLAUDE.md"
    claude_md.write_text(
        "# ARC-Bench project — MCP-first Official STEP loop\n\n"
        "HARD: GSC MCP stays ON. Never disable WaitForMcpServers / never set "
        "ARC_ENABLE_MCP=0 / never ban MCP tools.\n\n"
        "## Harness STEP loop (fail-closed)\n"
        "The harness runs discrete STEPs. Each STEP is its own Claude round.\n"
        "Do not advance yourself — the harness advances only after acceptance passes.\n"
        "Receipts live under `.arc/steps/<module-id>/`.\n\n"
        "STEPs in order:\n"
        "1. **prd** — FORCE-LOAD Skill `architect`, build PRD via MCP.\n"
        "2. **spec** — FORCE-LOAD Skill `architect`, write HTML SPEC via MCP.\n"
        "3. **test_dag** — FORCE-LOAD Skill `arcbench-traceability`, author API+UI test DAG "
        "(do not run full suite yet).\n"
        "4. **pages** — FORCE-LOAD Skill `designer` (UX-UI) BEFORE any page/UI work.\n"
        "5. **implement** — FORCE-LOAD Skill `arcbench-checkpoint`; implement WITHOUT "
        "mid-dev continuous tests.\n"
        "6. **batch_test** — FORCE-LOAD Skill `arcbench-runtime-signals`; run batch tests "
        "once after code is done.\n\n"
        "## Per-STEP rules\n"
        "- FIRST tool action in each STEP: use the Skill tool to load that STEP's required skill(s).\n"
        "- No skill load => STEP acceptance FAILS (fail-closed); harness will not advance.\n"
        "- Write receipt JSON to the path given in the STEP prompt when exit criteria are met.\n"
        "- Prefer visible GSC MCP calls (prd/spec_*/state_*) for planning.\n"
        "- Prefer main session; do not spawn Agent/Task.\n"
        "- If WaitForMcpServers appears, wait once then continue; keep outputs small.\n"
        "- Harness local tests grant final green — MCP alone does not.\n",
        encoding="utf-8",
    )
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
    """Fail-closed STEP gate: required Skill loads + step artifacts (+ batch tests)."""
    skills_seen = tuple(result.skills_loaded)
    missing = tuple(s for s in step.required_skills if s not in skills_seen)
    if missing:
        return StepAcceptance(
            ok=False,
            reason=f"skill force-load missing (fail-closed): {', '.join(missing)}",
            skills_seen=skills_seen,
            missing_skills=missing,
        )

    sid = safe_node_id(module.node_id)
    sdir = step_dir(output_dir, module.node_id)
    artifacts: list[str] = []

    if step.step_id == "prd":
        candidates = [
            output_dir / "PRD",
            sdir / "prd",
            sdir / "prd.md",
            sdir / "prd.json",
            sdir / "prd.html",
            sdir / "prd.artifact.json",
        ]
        found: list[str] = []
        prd_root = output_dir / "PRD"
        if prd_root.is_dir() and any(prd_root.rglob("*")):
            found.append(str(prd_root))
        found.extend(_existing_paths(candidates[1:]))
        found = list(dict.fromkeys(found))
        mcp_prd = [t for t in result.mcp_tools_used if t.endswith("__prd") or "prd" in t.split("__")[-1]]
        if not found and not mcp_prd:
            return StepAcceptance(
                ok=False,
                reason="PRD artifact missing under PRD/ or .arc/steps/<id>/prd* (and no MCP prd tool proof)",
                skills_seen=skills_seen,
                artifacts=tuple(found),
            )
        if not found and mcp_prd:
            # MCP prd used but no file yet — still fail-closed on artifact
            return StepAcceptance(
                ok=False,
                reason="MCP prd used but PRD artifact file/dir still missing",
                skills_seen=skills_seen,
                artifacts=tuple(mcp_prd),
            )
        artifacts = found + mcp_prd

    elif step.step_id == "spec":
        spec_html = output_dir / "SPEC" / "arcbench" / f"{sid}.html"
        spec_dir = output_dir / "SPEC" / "arcbench"
        found = []
        if spec_html.is_file() and spec_html.stat().st_size > 50:
            found.append(str(spec_html))
        elif spec_dir.is_dir():
            htmls = [p for p in spec_dir.glob("*.html") if p.is_file() and p.stat().st_size > 50]
            found.extend(str(p) for p in htmls[:5])
        mcp_spec = [t for t in result.mcp_tools_used if "spec_" in t or t.endswith("__prd") or "spec_write" in t or "spec_read" in t]
        if not found:
            return StepAcceptance(
                ok=False,
                reason="SPEC HTML missing/too small under SPEC/arcbench/",
                skills_seen=skills_seen,
            )
        if not mcp_spec:
            return StepAcceptance(
                ok=False,
                reason="SPEC STEP requires GSC MCP spec_read/spec_write (or prd) tool use proof",
                skills_seen=skills_seen,
                artifacts=tuple(found),
            )
        artifacts = found + mcp_spec

    elif step.step_id == "test_dag":
        dag = sdir / "test_dag.json"
        found = []
        if dag.is_file():
            try:
                payload = json.loads(dag.read_text(encoding="utf-8"))
            except Exception as exc:
                return StepAcceptance(
                    ok=False,
                    reason=f"test_dag.json unreadable: {exc}",
                    skills_seen=skills_seen,
                )
            has_api = bool(payload.get("api") or payload.get("api_tests"))
            has_ui = bool(payload.get("ui") or payload.get("ui_tests"))
            if not (has_api and has_ui):
                return StepAcceptance(
                    ok=False,
                    reason="test_dag.json must include both api and ui test entries",
                    skills_seen=skills_seen,
                    artifacts=(str(dag),),
                )
            found.append(str(dag))
        project = discover_test_project(output_dir)
        test_files = find_test_files(project) if project else []
        if test_files:
            found.extend(str(p) for p in test_files[:8])
        if not found:
            return StepAcceptance(
                ok=False,
                reason="TEST DAG missing: need .arc/steps/<id>/test_dag.json (api+ui) and/or test files",
                skills_seen=skills_seen,
            )
        artifacts = found

    elif step.step_id == "pages":
        pages_receipt = sdir / "pages.json"
        ui_roots = [
            output_dir / "frontend" / "src" / "pages",
            output_dir / "frontend" / "src" / "components",
            output_dir / "frontend" / "src",
        ]
        found = []
        if pages_receipt.is_file():
            found.append(str(pages_receipt))
        for root in ui_roots:
            if not root.exists():
                continue
            for pattern in ("**/*.tsx", "**/*.jsx", "**/*.vue", "**/*.html"):
                for p in root.glob(pattern):
                    if p.is_file() and "node_modules" not in p.parts and p.stat().st_size >= 20:
                        found.append(str(p))
                        break
                if len(found) > 1:
                    break
            if len(found) > 1:
                break
        found = list(dict.fromkeys(found))
        if not found:
            return StepAcceptance(
                ok=False,
                reason="PAGES artifacts missing under frontend/src (or pages.json receipt)",
                skills_seen=skills_seen,
            )
        artifacts = found[:12]

    elif step.step_id == "implement":
        impl_receipt = sdir / "implement.json"
        found = []
        if impl_receipt.is_file():
            found.append(str(impl_receipt))
        for root_name in ("frontend/src", "backend/src", "src"):
            root = output_dir / root_name
            if not root.exists():
                continue
            for p in root.rglob("*"):
                if p.is_file() and p.suffix in {".ts", ".tsx", ".js", ".jsx", ".py"} and p.stat().st_size >= 40:
                    found.append(str(p))
                    if len(found) >= 4:
                        break
            if len(found) >= 2:
                break
        if not found:
            return StepAcceptance(
                ok=False,
                reason="IMPLEMENT artifacts missing (implement.json and/or source files)",
                skills_seen=skills_seen,
            )
        artifacts = list(dict.fromkeys(found))[:12]

    elif step.step_id == "batch_test":
        validation = run_module_validation(output_dir, module)
        artifacts = [f"validation:{validation.reason}", f"cmd:{' '.join(validation.cmd)}"]
        if not validation.ok:
            return StepAcceptance(
                ok=False,
                reason=f"batch_test harness validation failed: {validation.reason}",
                skills_seen=skills_seen,
                artifacts=tuple(artifacts),
            )
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "batch_validation.json").write_text(
            json.dumps(
                {
                    "ok": validation.ok,
                    "exit_code": validation.exit_code,
                    "cmd": validation.cmd,
                    "reason": validation.reason,
                    "project_dir": validation.project_dir,
                    "log_tail": validation.log_tail[-4000:],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        artifacts.append(str(sdir / "batch_validation.json"))
    else:
        return StepAcceptance(ok=False, reason=f"unknown step {step.step_id}", skills_seen=skills_seen)

    return StepAcceptance(
        ok=True,
        reason=f"step {step.step_id} acceptance passed",
        skills_seen=skills_seen,
        artifacts=tuple(artifacts),
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
            "Fix the missing skill load and/or artifacts, then finish this STEP only.\n"
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
            "Write/adjust code only; batch testing is the next STEP.\n"
        )
    batch = ""
    if step.require_batch_test_run:
        batch = (
            "REQUIRED this STEP: after confirming code is done, run batch tests once "
            "(prefer frontend `npx vitest run` / `npm test`). Fix failures if needed within this STEP.\n"
        )

    skill_lines = ", ".join(f"`{s}`" for s in step.required_skills)
    return textwrap.dedent(f"""
        You are in an ARC-Bench Official harness STEP round (not a mega-prompt).
        GSC plugin + GSC MCP are loaded. MCP stays ON.

        Target task type: {task_type}
        ROOT module {module.index}/{module.total}: {module.node_id} - {module.name}
        Previously completed ROOT modules: {completed_text}
        Requirement source directory: {requirements_dir}
        Current STEP: {step.step_id} — {step.title}
        {recovery}

        HARD RULES:
        - GSC MCP stays ON. Never disable WaitForMcpServers / never ARC_ENABLE_MCP=0 / never ban MCP.
        - This round is ONLY for STEP `{step.step_id}`. Do not perform later STEPs.
        - FORCE-LOAD required skill(s) FIRST via the Skill tool before other work: {skill_lines}
        - If you skip Skill load, harness acceptance FAILS (fail-closed) and you will not advance.
        - Prefer main session; do NOT spawn Agent/Task.
        - Keep tool outputs small (no huge lockfiles/schemas).

        STEP goal: {step.goal}
        Exit criteria: {step.exit_criteria}
        {mid_dev}{batch}
        {skills_text}
        Also use GSC MCP (`prd` / `spec_read` / `spec_write` / `state_*`) where relevant so logs show MCP value.

        When exit criteria are met, write a short receipt JSON to `{receipt_hint}` with keys:
        step_id, skills_loaded, artifacts (list of paths), summary.
        The harness writes `{ok_hint}` only after its own acceptance gate passes (skill proof + artifacts).

        Work only from this ROOT-child subtree:
        ```json
        {json.dumps(module.subtree, ensure_ascii=False, indent=2)}
        ```

        Finish this STEP only. Summarize skills loaded, MCP tools used, and artifacts produced.
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
    if os.geteuid() != 0:
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
    env["GSC_ARC_PACKAGED_RUNTIME"] = "1"
    env["GSC_RUNTIME_SERVER_BIN"] = str(gsc_dir / "bin" / "gsc-spec-server")
    env["CLAUDE_PLUGIN_ROOT"] = str(gsc_dir)
    env["CLAUDE_PLUGIN_DATA"] = str(plugin_data)
    env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"
    # Claude -p waits for --mcp-config servers up to MCP_TIMEOUT before the first turn.
    # Prefer this over permanently banning WaitForMcpServers / MCP tools.
    env["MCP_TIMEOUT"] = str(mcp_timeout_ms)
    env["PATH"] = os.pathsep.join([str(gsc_dir / "bin"), str(gsc_dir / "lsp" / "web" / "node_modules" / ".bin"), env.get("PATH", "")])

    gateway_proc = _arc_maybe_start_anthropic_proxy(base_url, api_key, model)
    claude_env = env.copy()
    if gateway_proc is not None:
        claude_env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:8787"
        claude_env["ANTHROPIC_API_KEY"] = "arc-local"
        for _k in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY_OLD", "CLAUDE_CODE_API_KEY"):
            claude_env.pop(_k, None)
    else:
        claude_env["ANTHROPIC_BASE_URL"] = base_url
        claude_env["ANTHROPIC_AUTH_TOKEN"] = api_key
        claude_env["ANTHROPIC_API_KEY"] = ""
    if gateway_proc is not None:
        def _stop_gateway(proc=gateway_proc):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
        atexit.register(_stop_gateway)
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
    # Total validation attempts = 1 + ARC_VALIDATION_MAX_REPAIRS (default 1+2=3).
    max_validation_repairs = env_int("ARC_VALIDATION_MAX_REPAIRS", 2, minimum=0, maximum=10)
    max_budget_usd = os.environ.get("ARC_MAX_BUDGET_USD", "50").strip()
    base_urls = configured_base_urls(base_url)
    host = upstream_host(base_urls[0])

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
                "mcp_allowed_tools": gsc_mcp_allowed_tools() if enable_mcp else [],
                "step_loop": [s.step_id for s in OFFICIAL_STEPS],
                "step_required_skills": {s.step_id: list(s.required_skills) for s in OFFICIAL_STEPS},
                "thrash_mitigations": [
                    "spawn_gate_off",
                    "disallow_Agent_Task_and_bloat_builtins",
                    "Skill_allowed_for_STEP_force_load",
                    "mcp_allowedTools_spec_subset",
                    "WaitForMcpServers_once_guidance",
                    "html_spec",
                    "disable_slash_commands",
                    "autocompact_200000",
                    "rapid_refill_retryable_capped",
                    "official_step_loop_fail_closed",
                ],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    completed: list[str] = []
    try:
        for module in modules:
            if module_already_passed(runtime, module.node_id, output_dir):
                print(
                    json.dumps(
                        {
                            "event": "module_skip",
                            "req_id": module.node_id,
                            "reason": "PASSED_with_validation_receipt",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                completed.append(module.node_id)
                continue

            print(f"[arc-claude-gsc] module {module.index}/{module.total}: {module.node_id} - {module.name}", flush=True)
            spec_path = ensure_gsc_spec(output_dir, module)
            ensure_arc_spawn_gate_softener(output_dir)
            runtime.events.mark_design_started(module.node_id, f"Planning {module.name} from {spec_path.relative_to(output_dir)}")
            runtime.events.mark_design_done(module.node_id, f"Delegated {module.name} to Claude Code + GSC")

            max_step_retries = env_int("ARC_STEP_MAX_RETRIES", 2, minimum=0, maximum=8)
            validation_repair = 0

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
                }
                print(json.dumps(payload, ensure_ascii=False), flush=True)
                runtime.events.mark_run_paused(
                    f"Transient upstream/API failure in {module.node_id}; retry {attempt}/{max_retries} after {delay}s"
                )

            def build_claude_command(prompt: str, attempt: int) -> tuple[list[str], dict[str, str]]:
                attempt_base_url = base_url_for_attempt(base_urls, attempt)
                command = [
                    str(claude_bin),
                    "-p",
                    prompt,
                    "--plugin-dir",
                    str(gsc_dir),
                    *claude_mcp_cli_args(enabled=enable_mcp, mcp_config=mcp_config_path),
                    "--model",
                    ("sonnet" if gateway_proc is not None else model),
                    # Keep MCP ON; WaitForMcpServers NOT banned.
                    # Skill allowed so each STEP can FORCE-LOAD its required skills.
                    "--disallowedTools",
                    (
                        "Agent,Task,WebSearch,WebFetch,"
                        "CronCreate,CronDelete,CronList,NotebookEdit,"
                        "EnterWorktree,ExitWorktree,ListAgents,"
                        "ScheduleWakeup,SendMessage,Workflow,DesignSync,ReportFindings"
                    ),
                    "--disable-slash-commands",
                    "--autocompact",
                    "200000",
                    "--permission-mode",
                    "bypassPermissions",
                    "--no-session-persistence",
                    "--output-format",
                    "stream-json",
                    "--verbose",
                ]
                if max_budget_usd:
                    command.extend(["--max-budget-usd", max_budget_usd])
                attempt_env = claude_env.copy()
                if gateway_proc is None:
                    attempt_env["ANTHROPIC_BASE_URL"] = attempt_base_url
                return command, attempt_env

            # Fail-closed Official STEP loop: skill proof + artifacts before advance.
            steps_to_run: list[StepDef] = list(OFFICIAL_STEPS)
            while True:
                step_failed = False
                for step in steps_to_run:
                    if has_step_receipt(output_dir, module.node_id, step.step_id):
                        print(
                            json.dumps(
                                {
                                    "event": "step_skip",
                                    "req_id": module.node_id,
                                    "step_id": step.step_id,
                                    "reason": "receipt_ok_present",
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                        continue

                    prior_failure: str | None = None
                    accepted = False
                    last_result: ClaudeRunResult | None = None
                    # If repairing, inject harness failure log into implement/batch_test prompts.
                    repair_log = None
                    repair_note = step_dir(output_dir, module.node_id) / "repair_note.txt"
                    if validation_repair > 0 and step.step_id in ("implement", "batch_test") and repair_note.is_file():
                        repair_log = repair_note.read_text(encoding="utf-8", errors="replace")[-6000:]

                    for step_attempt in range(1, max_step_retries + 2):
                        def run_attempt(attempt: int, _step=step, _prior_ref=lambda: prior_failure, _repair=repair_log, _vrep=validation_repair) -> ClaudeRunResult:
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
                            )
                            command, attempt_env = build_claude_command(prompt, attempt)
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
                                    },
                                    ensure_ascii=False,
                                ),
                                flush=True,
                            )
                            return run_claude_streaming(
                                command,
                                cwd=output_dir,
                                env=attempt_env,
                                preexec_fn=privilege_dropper(identity),
                            )

                        result, api_attempts = execute_with_retry(
                            run_attempt,
                            max_retries=max_retries,
                            base_seconds=retry_base_seconds,
                            max_seconds=retry_max_seconds,
                            on_retry=on_retry,
                        )
                        last_result = result
                        classification = classify_claude_failure(result)
                        if result.returncode != 0 or result.is_error:
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

                        acceptance = evaluate_step_acceptance(output_dir, module, step, result)
                        write_step_receipt(output_dir, module.node_id, step, acceptance, claude=result)
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
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                        if acceptance.ok:
                            accepted = True
                            break
                        prior_failure = acceptance.reason
                        clear_step_receipt(output_dir, module.node_id, step.step_id)
                        # keep json for debug: rewrite failed receipt without .ok
                        write_step_receipt(output_dir, module.node_id, step, acceptance, claude=result)

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

                # All STEPs accepted. batch_test already ran harness validation; persist module receipt.
                validation = run_module_validation(output_dir, module)
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
                            "validation_repair": validation_repair,
                            "max_validation_repairs": max_validation_repairs,
                            "note": "post-STEP-loop confirmation",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                gate = decide_validation_gate(
                    validation,
                    validation_repair=validation_repair,
                    max_validation_repairs=max_validation_repairs,
                )
                if gate.action == "pass":
                    write_validation_receipt(output_dir, module.node_id, validation)
                    runtime.events.mark_implementation_done(
                        module.node_id,
                        f"Implemented {module.name} via Official STEP loop"
                        + (f"; validation_repair {validation_repair}" if validation_repair else ""),
                    )
                    runtime.events.mark_test_passed(
                        module.node_id,
                        "Harness local validation passed after STEP loop",
                    )
                    runtime.git.commit(f"{module.node_id}: {module.name}")
                    completed.append(module.node_id)
                    break

                clear_validation_receipt(output_dir, module.node_id)
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
                        f"Harness validation exhausted after {validation_repair} repair(s): {validation.reason}",
                    )
                    runtime.events.mark_run_failed(
                        f"Module {module.node_id} harness validation failed after "
                        f"{validation_repair} repair(s): {validation.reason}"
                    )
                    print(
                        json.dumps(
                            {
                                "event": "module_validation_exhausted",
                                "req_id": module.node_id,
                                "validation_repair": validation_repair,
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

                validation_repair += 1
                # Repair: clear implement + batch_test receipts and re-run those STEPs only.
                for sid in ("implement", "batch_test"):
                    clear_step_receipt(output_dir, module.node_id, sid)
                steps_to_run = [s for s in OFFICIAL_STEPS if s.step_id in ("implement", "batch_test")]
                # Seed prior failure into batch_test via a repair note file
                repair_note = step_dir(output_dir, module.node_id) / "repair_note.txt"
                repair_note.parent.mkdir(parents=True, exist_ok=True)
                repair_note.write_text(fail_msg[-6000:], encoding="utf-8")
                runtime.events.mark_run_resumed(
                    f"Validation repair {validation_repair}/{max_validation_repairs} for {module.node_id}; "
                    "re-running implement+batch_test STEPs"
                )
                print(
                    json.dumps(
                        {
                            "event": "step_repair_rewind",
                            "req_id": module.node_id,
                            "validation_repair": validation_repair,
                            "replay_steps": [s.step_id for s in steps_to_run],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

        runtime.events.mark_run_completed("All ROOT modules completed")
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