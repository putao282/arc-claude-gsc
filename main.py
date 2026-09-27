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
    mcp_lock = threading.Lock()

    def note_mcp_from_line(line: str) -> None:
        """Emit compact MCP audit events for Official log analysis (MCP value/role)."""
        if "mcp__" not in line:
            return
        name = None
        try:
            payload = json.loads(line)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            # stream-json assistant tool_use / content blocks
            for key in ("name", "tool_name"):
                val = payload.get(key)
                if isinstance(val, str) and val.startswith("mcp__"):
                    name = val
                    break
            if name is None:
                content = payload.get("message", {}).get("content") if isinstance(payload.get("message"), dict) else payload.get("content")
                blocks = content if isinstance(content, list) else []
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    val = block.get("name")
                    if isinstance(val, str) and val.startswith("mcp__"):
                        name = val
                        break
            if name is None:
                tool_use = payload.get("tool_use") or payload.get("toolUse")
                if isinstance(tool_use, dict):
                    val = tool_use.get("name")
                    if isinstance(val, str) and val.startswith("mcp__"):
                        name = val
        if name is None:
            # fallback: first mcp__ token in line
            for token in line.replace(",", " ").replace('"', " ").split():
                if token.startswith("mcp__"):
                    name = token.strip("[]{}() ")
                    break
        if not name:
            return
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

    def pump(stream, sink, tail: deque[str], *, watch_mcp: bool = False) -> None:
        if stream is None:
            return
        try:
            for line in iter(stream.readline, ""):
                tail.append(line.rstrip("\n"))
                sink.write(line)
                sink.flush()
                if watch_mcp:
                    note_mcp_from_line(line)
        finally:
            stream.close()

    threads = [
        threading.Thread(
            target=pump,
            args=(process.stdout, sys.stdout, stdout_tail),
            kwargs={"watch_mcp": True},
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
    """Soften GSC SPAWN-GATE without disabling MCP.

    GSC PreToolUse[Agent|Task] hard-denies coding spawns missing ## TASK-HEADER
    (REQ-AGENTGOV-2). On ARC smoke that burns turns/tokens and feeds autocompact
    rapid_refill. Official escape hatch: .claude/spawn-gate-off (requires CLAUDE.md
    so findProjectRoot resolves). Always (re)write CLAUDE.md with Tao MCP-first
    Official workflow so packaged runs follow PRD→SPEC→TEST DAG→PAGES(designer)→
    batch test; MCP stays ON (never ban WaitForMcpServers / ARC_ENABLE_MCP=0).
    """
    claude_md = output_dir / "CLAUDE.md"
    claude_md.write_text(
        "# ARC-Bench project — MCP-first Official workflow\n\n"
        "HARD: GSC MCP stays ON. Never disable WaitForMcpServers / never set "
        "ARC_ENABLE_MCP=0 / never ban MCP tools.\n\n"
        "## Mandated workflow (in order)\n"
        "1. MCP-first planning: from requirements build **PRD → SPEC → TEST cases** "
        "(API + UI test DAG) via GSC MCP (`prd` / `spec_read` / `spec_write` / "
        "`state_read` / `state_update` / related). Prefer HTML SPEC under "
        "`SPEC/arcbench/`; do not invent Markdown SPEC that forces migrate.\n"
        "2. **PAGES / UI**: before building any page or UI, load the UX-UI "
        "**`designer` skill** (Skill tool name: `designer`). Then implement UI.\n"
        "3. **Development**: implement in the main session with Bash/Edit/Write/Read "
        "+ MCP. Do **NOT** run tests continuously mid-development.\n"
        "4. **After all code** for the module/subtree is done: **batch / centralized "
        "testing** once (prefer `frontend` `npx vitest run` / `npm test`).\n"
        "5. Purpose of Official: rich MCP tool-use in logs for bug audit + MCP "
        "value analysis — prefer visible MCP calls over silent bypass.\n\n"
        "## Thrash / spawn controls\n"
        "- Prefer implementing in the main Claude session; do not spawn Agent/Task "
        "unless a full `## TASK-HEADER` block is included.\n"
        "- If WaitForMcpServers appears, wait once then continue; do not dump schemas.\n"
        "- Keep tool outputs small (no huge lockfiles/schemas) to avoid rapid_refill.\n"
        "- Harness local tests grant green — MCP planning alone does NOT.\n",
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
    completed_text = ", ".join(completed) if completed else "none"
    skills_text = f"ARC skills are installed at {skills_dir}." if skills_dir else "The adapter emits baseline ARC runtime states and checkpoints."
    recovery_text = ""
    if validation_failure:
        recovery_text = (
            f"VALIDATION REPAIR {validation_repair}: the harness local validation gate FAILED for this module. "
            "Claude session success alone does NOT grant green. Fix the implementation and/or tests so the harness "
            "command (prefer frontend `npx vitest run` / `npm test`) exits 0. Do not claim completion via a tiny "
            "self-written smoke test or STOP early. MCP SPEC status helps planning but does NOT grant harness green.\n\n"
            f"Harness validation failure log (tail):\n{validation_failure}"
        )
    elif attempt > 1:
        recovery_text = (
            f"RECOVERY ATTEMPT {attempt}: previous Claude ended on a transient failure (often autocompact "
            "rapid_refill). Workspace files were preserved. Inspect existing files first; continue this module; "
            "do not redo passed ROOT modules. Prefer Bash/Edit/Write/Read plus GSC MCP spec_read/spec_write/"
            "state_*; do not dump lockfiles or re-list huge tool schemas."
        )
    return textwrap.dedent(f"""
        You are implementing an ARC-Bench Agentic Software Factory task using original Claude Code with the GSC plugin and GSC MCP loaded.

        Target task type: {task_type}
        Implement ROOT module {module.index}/{module.total}: {module.node_id} - {module.name}
        Previously completed ROOT modules: {completed_text}
        Requirement source directory: {requirements_dir}
        {recovery_text}

        HARD RULES: GSC MCP stays ON. Never disable WaitForMcpServers / never set ARC_ENABLE_MCP=0 / never ban MCP.

        Mandated MCP-first workflow (do in order; encode progress via MCP so logs show MCP value):
        1) From requirements, use GSC MCP to build PRD → SPEC → TEST cases (API + UI test DAG). Prefer HTML SPEC under SPEC/arcbench/ via spec_read/spec_write/prd/state_*; do not invent Markdown SPEC that forces migrate.
        2) On PAGES / UI work: BEFORE building any page or UI, load the UX-UI designer skill (Skill tool name: `designer`). Then implement UI.
        3) During development: implement with Bash/Edit/Write/Read + MCP. Do NOT run tests continuously mid-development.
        4) After all code for this subtree is done: batch/centralized testing once (prefer frontend `npx vitest run` / `npm test`).
        5) Prefer visible MCP tool use (planning/state/SPEC) over silent bypass — Official audit needs rich MCP logs.

        The current working directory is the persistent generated project. Preserve working features from earlier modules.
        Use GSC actively (including GSC MCP SPEC / planning / validation tools) for requirements, implementation planning, coding, validation, and state tracking rather than bypassing it.
        If WaitForMcpServers appears, wait once for GSC MCP readiness then continue implementation; do not loop reconnecting or dump huge unrelated context.
        SPAWN thrash control: implement this module in the main Claude session. Do NOT spawn Agent/Task subagents. If a coding spawn is unavoidable, the prompt MUST start with a literal line "## TASK-HEADER" (no trailing colon) plus task/domain/archaeology/reuse-decision/scope/completion/retry/stop fields — otherwise GSC SPAWN-GATE fail-closes.
        Keep tool outputs small: read files in chunks, avoid pasting huge lockfiles/schemas into the conversation (prevents autocompact rapid_refill_breaker).

        {skills_text}
        If ARC skills are present, read the runtime-signals, traceability, and checkpoint skill instructions and record detailed requirement-to-interface/file/test traceability.
        Implement against the ROOT-child subtree below. Prefer adding/keeping real automated tests under frontend/ (or the project package.json) that the harness can run — write them during planning/DAG, run them in the final batch step (not continuously).
        Do not start a long-running server. Do not erase work from earlier modules.

        Harness bar (authoritative for mark_test_passed): after this Claude session returns, the harness itself runs local validation (prefer `frontend` `npx vitest run` / `npm test`). No test files => cannot green. Your session exit code alone never grants green. GSC MCP SPEC / planning tools help quality but do NOT grant harness green or platform score.

        Do not read the complete requirements.yaml. Work only from this complete ROOT-child subtree:
        ```json
        {json.dumps(module.subtree, ensure_ascii=False, indent=2)}
        ```

        Finish only after the subtree is implemented and ready for harness local validation. Summarize changed files, MCP tools used, and how batch tests cover the requirement.
    """).strip()


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
                "thrash_mitigations": [
                    "spawn_gate_off",
                    "disallow_Agent_Task_and_bloat_builtins",
                    "mcp_allowedTools_spec_subset",
                    "WaitForMcpServers_once_guidance",
                    "html_spec",
                    "disable_slash_commands",
                    "autocompact_200000",
                    "rapid_refill_retryable_capped",
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

            validation_failure_log: str | None = None
            validation_repair = 0
            last_claude_attempts = 0

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

            def run_attempt(attempt: int) -> ClaudeRunResult:
                attempt_base_url = base_url_for_attempt(base_urls, attempt)
                if attempt > 1 and validation_failure_log is None:
                    runtime.events.mark_run_resumed(
                        f"Retry attempt {attempt}/{max_retries + 1} for {module.node_id}; workspace preserved"
                    )
                runtime.events.mark_implementation_started(
                    module.node_id,
                    f"Implementing {module.name} (attempt {attempt}/{max_retries + 1}"
                    + (f"; validation_repair {validation_repair}" if validation_repair else "")
                    + ")",
                )
                command = [
                    str(claude_bin),
                    "-p",
                    module_prompt(
                        module,
                        requirements_dir,
                        skills_dir,
                        completed,
                        args.task_type,
                        attempt=attempt,
                        validation_failure=validation_failure_log,
                        validation_repair=validation_repair,
                    ),
                    "--plugin-dir",
                    str(gsc_dir),
                    *claude_mcp_cli_args(enabled=enable_mcp, mcp_config=mcp_config_path),
                    "--model",
                    ("sonnet" if gateway_proc is not None else model),
                    # Keep MCP ON; shrink builtin bloat. WaitForMcpServers NOT banned.
                    # Skill allowed so PAGES can load UX-UI `designer` skill (Tao workflow).
                    # MCP schema shrink is via mcp-config allowedTools (SPEC subset).
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
                return run_claude_streaming(
                    command,
                    cwd=output_dir,
                    env=attempt_env,
                    preexec_fn=privilege_dropper(identity),
                )

            # Outer loop: Claude (with API self-heal) then harness validation;
            # validation failures drive a separate repair Claude session (not API retry).
            while True:
                result, attempts = execute_with_retry(
                    run_attempt,
                    max_retries=max_retries,
                    base_seconds=retry_base_seconds,
                    max_seconds=retry_max_seconds,
                    on_retry=on_retry,
                )
                last_claude_attempts = attempts
                classification = classify_claude_failure(result)
                if result.returncode != 0 or result.is_error:
                    terminal = {
                        "event": "module_terminal_failure",
                        "req_id": module.node_id,
                        "attempts": attempts,
                        "max_retries": max_retries,
                        "classification": classification.reason,
                        "terminal_reason": result.terminal_reason or "unknown",
                        "returncode": result.returncode,
                        "api_error_status": result.api_error_status,
                        "upstream_host": host,
                        "validation_repair": validation_repair,
                    }
                    print(json.dumps(terminal, ensure_ascii=False), file=sys.stderr, flush=True)
                    runtime.events.mark_implementation_failed(
                        module.node_id,
                        f"Claude Code failed after {attempts} attempt(s): {classification.reason}",
                    )
                    runtime.events.mark_test_failed(module.node_id, "Module did not complete")
                    runtime.events.mark_run_failed(
                        f"Module {module.node_id} failed after {attempts} attempt(s): {classification.reason}"
                    )
                    return result.returncode or 1

                validation = run_module_validation(output_dir, module)
                validation_event = {
                    "event": "module_validation",
                    "req_id": module.node_id,
                    "ok": validation.ok,
                    "exit_code": validation.exit_code,
                    "cmd": validation.cmd,
                    "reason": validation.reason,
                    "project_dir": validation.project_dir,
                    "validation_repair": validation_repair,
                    "max_validation_repairs": max_validation_repairs,
                }
                print(json.dumps(validation_event, ensure_ascii=False), flush=True)

                gate = decide_validation_gate(
                    validation,
                    validation_repair=validation_repair,
                    max_validation_repairs=max_validation_repairs,
                )
                if gate.action == "pass":
                    write_validation_receipt(output_dir, module.node_id, validation)
                    runtime.events.mark_implementation_done(
                        module.node_id,
                        f"Implemented {module.name} after {last_claude_attempts} Claude attempt(s)"
                        + (f"; validation_repair {validation_repair}" if validation_repair else ""),
                    )
                    runtime.events.mark_test_passed(
                        module.node_id,
                        "Harness local validation passed",
                    )
                    runtime.git.commit(f"{module.node_id}: {module.name}")
                    completed.append(module.node_id)
                    break

                # Validation failed: never treat as API-retryable; mark failed and maybe repair.
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
                validation_failure_log = fail_msg[-6000:]
                runtime.events.mark_run_resumed(
                    f"Validation repair {validation_repair}/{max_validation_repairs} for {module.node_id}; injecting harness failure log"
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