"""Harness soft-strategy supervisor (v5ab).

Hard gates stay in main.py / sdk_driver.py. This module only chooses among a
finite safe-retry action enum when ARC_HARNESS_SUPERVISOR=1 (or pack sentinel).
Default OFF → ask_supervisor returns continue with zero HTTP.

See /workspace/arc-hackathon-eval-v5/PLAN_harness_supervisor_v5ab.md
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


SCHEMA_VERSION = "v5ab.1"

# Finite action enum — executor must re-validate against hook allowlists.
ACTION_NUDGE_STEP_PROMPT = "nudge_step_prompt"
ACTION_RETRY_MERGE_THEIRS = "retry_merge_theirs"
ACTION_RETRY_MERGE_OURS = "retry_merge_ours"
ACTION_RE_IMPLEMENT_DOMAIN = "re_implement_domain"
ACTION_SKIP_SOFT_STALL_WAIT = "skip_soft_stall_wait"
ACTION_FAIL_CLOSED = "fail_closed"
ACTION_CONTINUE = "continue"

ALL_ACTIONS: frozenset[str] = frozenset(
    {
        ACTION_NUDGE_STEP_PROMPT,
        ACTION_RETRY_MERGE_THEIRS,
        ACTION_RETRY_MERGE_OURS,
        ACTION_RE_IMPLEMENT_DOMAIN,
        ACTION_SKIP_SOFT_STALL_WAIT,
        ACTION_FAIL_CLOSED,
        ACTION_CONTINUE,
    }
)

HOOK_ALLOWED: dict[str, frozenset[str]] = {
    "merge_fail": frozenset(
        {
            ACTION_RETRY_MERGE_THEIRS,
            ACTION_RETRY_MERGE_OURS,
            ACTION_RE_IMPLEMENT_DOMAIN,
            ACTION_FAIL_CLOSED,
            ACTION_CONTINUE,
        }
    ),
    "govern_thrash": frozenset(
        {
            ACTION_NUDGE_STEP_PROMPT,
            ACTION_SKIP_SOFT_STALL_WAIT,
            ACTION_FAIL_CLOSED,
            ACTION_CONTINUE,
        }
    ),
    "implement_soft_stall": frozenset(
        {
            ACTION_NUDGE_STEP_PROMPT,
            ACTION_RE_IMPLEMENT_DOMAIN,
            ACTION_SKIP_SOFT_STALL_WAIT,
            ACTION_FAIL_CLOSED,
            ACTION_CONTINUE,
        }
    ),
    "batch_test_fail": frozenset(
        {
            ACTION_NUDGE_STEP_PROMPT,
            ACTION_RE_IMPLEMENT_DOMAIN,
            ACTION_FAIL_CLOSED,
            ACTION_CONTINUE,
        }
    ),
}

_SYSTEM_PROMPT = (
    "You are ARC harness recovery supervisor. Reply with ONLY a JSON object: "
    '{"action":"<enum>","reason":"≤240 chars","nudge_text":"optional ≤500",'
    '"confidence":0.0}. Hard gates are code; pick one action from allowed_actions. '
    "Never mark green, disable MCP, invent skills, skip BATCH_TEST, or change budget."
)

_FORBIDDEN_NUDGE = re.compile(
    r"(mark[_\s-]?test[_\s-]?passed|mark[_\s-]?green|ARC_ENABLE_MCP\s*=\s*0|"
    r"disable\s+mcp|skip\s+batch[_\s-]?test|set_budget|rm\s+-rf|"
    r"curl\s+.+\|)",
    re.IGNORECASE,
)

# Process-local cost control (reset per process / run).
_calls_total = 0
_calls_per_hook: dict[str, int] = {}
_last_call_monotonic = 0.0
_last_fingerprint = ""
_event_ring: deque[dict[str, Any]] = deque(maxlen=40)


@dataclass(frozen=True)
class SupervisorDecision:
    action: str
    reason: str
    nudge_text: str = ""
    confidence: float = 0.0
    source: str = "continue"  # continue|model|parse_fail|disabled|budget|error|silent
    raw: dict[str, Any] | None = None


def note_event(event: dict[str, Any] | None) -> None:
    """Append a compact harness event to the in-process ring (no full transcripts)."""
    if not isinstance(event, dict):
        return
    slim: dict[str, Any] = {}
    for k in (
        "event",
        "level",
        "tool",
        "req_id",
        "step_id",
        "domain_id",
        "wave_index",
        "ok",
        "reason",
        "action",
        "identical_count",
        "validation_repair",
        "soft_accept_max_turns",
        "thrash_hit",
    ):
        if k in event:
            slim[k] = event[k]
    if not slim:
        return
    _event_ring.append(slim)


def recent_events_snapshot(limit: int = 20) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    return list(_event_ring)[-limit:]


def _pack_sentinel_on() -> bool:
    """Contest may not inject env; optional pack-local sentinel enables canary."""
    bases: list[Path] = [Path(__file__).resolve().parent]
    sub = (os.environ.get("ARCBENCH_SUBMISSION_DIR") or "").strip()
    if sub:
        bases.append(Path(sub))
    for base in bases:
        marker = base / "ARC_HARNESS_SUPERVISOR"
        if marker.is_file():
            try:
                raw = marker.read_text(encoding="utf-8", errors="replace").strip().lower()
            except OSError:
                return True
            if raw in {"", "1", "true", "yes", "on"}:
                return True
            if raw in {"0", "false", "no", "off"}:
                return False
            return True
    return False


def supervisor_enabled() -> bool:
    raw = (os.environ.get("ARC_HARNESS_SUPERVISOR") or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return _pack_sentinel_on()


def _env_int(name: str, default: int, *, minimum: int = 0, maximum: int = 10_000) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _chat_completions_url(base_url: str) -> str:
    u = (base_url or "").strip().rstrip("/")
    if not u:
        return ""
    if u.endswith("/chat/completions"):
        return u
    if u.endswith("/v1"):
        return u + "/chat/completions"
    return u + "/v1/chat/completions"


def _strip_json_fence(text: str) -> str:
    s = (text or "").strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    return s


def sanitize_nudge_text(text: str) -> str:
    """Truncate and drop nudge text that tries to bypass hard gates."""
    s = (text or "").strip()[:500]
    if not s:
        return ""
    if _FORBIDDEN_NUDGE.search(s):
        return ""
    # No shell fences / heredocs
    if "```" in s or "$(" in s or "`" in s:
        s = s.replace("```", "").replace("$(", "").replace("`", "")
    return s.strip()[:500]


def parse_supervisor_response(
    content: str,
    *,
    allowed_actions: frozenset[str],
) -> SupervisorDecision:
    """Validate model JSON; unknown/disallowed action → continue."""
    try:
        body = json.loads(_strip_json_fence(content))
    except Exception as exc:
        return SupervisorDecision(
            action=ACTION_CONTINUE,
            reason=f"parse_fail:{exc}",
            source="parse_fail",
        )
    if not isinstance(body, dict):
        return SupervisorDecision(
            action=ACTION_CONTINUE,
            reason="parse_fail:not_object",
            source="parse_fail",
        )
    action = str(body.get("action") or "").strip()
    reason = str(body.get("reason") or "")[:240]
    nudge = sanitize_nudge_text(str(body.get("nudge_text") or ""))
    try:
        confidence = float(body.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if action not in ALL_ACTIONS or action not in allowed_actions:
        return SupervisorDecision(
            action=ACTION_CONTINUE,
            reason=f"disallowed_action:{action or 'empty'}; {reason}".strip()[:240],
            source="parse_fail",
            raw=body,
        )
    if action != ACTION_NUDGE_STEP_PROMPT:
        nudge = ""
    return SupervisorDecision(
        action=action,
        reason=reason or "model",
        nudge_text=nudge,
        confidence=confidence,
        source="model",
        raw=body,
    )


def _observation_fingerprint(observation: dict[str, Any]) -> str:
    slim = {
        "hook": observation.get("hook"),
        "domain_id": observation.get("domain_id"),
        "req_id": observation.get("req_id"),
        "step_id": observation.get("step_id"),
        "attempt": observation.get("attempt"),
        "repair_index": observation.get("repair_index"),
        "error_snippet": (observation.get("error_snippet") or "")[:400],
    }
    try:
        return json.dumps(slim, sort_keys=True, ensure_ascii=False)
    except Exception:
        return str(slim)


def build_observation(
    *,
    hook: str,
    allowed_actions: frozenset[str] | None = None,
    wave_index: int | None = None,
    domain_id: str | None = None,
    req_id: str | None = None,
    step_id: str | None = None,
    attempt: int = 0,
    repair_index: int = 0,
    recent_events: list[dict[str, Any]] | None = None,
    error_snippet: str = "",
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    allowed = allowed_actions or HOOK_ALLOWED.get(hook, frozenset({ACTION_CONTINUE}))
    events = recent_events if recent_events is not None else recent_events_snapshot(20)
    return {
        "schema_version": SCHEMA_VERSION,
        "hook": hook,
        "allowed_actions": sorted(allowed),
        "wave_index": wave_index,
        "domain_id": domain_id,
        "req_id": req_id,
        "step_id": step_id,
        "attempt": attempt,
        "repair_index": repair_index,
        "supervisor_calls_so_far": _calls_total,
        "recent_events": list(events)[-20:],
        "error_snippet": (error_snippet or "")[:4000],
        "extras": extras or {},
    }


def should_call_hook(hook: str, extras: dict[str, Any] | None = None) -> bool:
    """Cost-control silence rules from the plan (even when flag ON)."""
    ex = extras or {}
    if hook == "govern_thrash":
        level = str(ex.get("level") or "")
        if level == "soft":
            return False
        if level in {"hard", "hard_repeat", "read_streak_deny", "govern_accept_deny"}:
            return True
        # Post-accept deny streak or hard thrash flag
        if ex.get("thrash_hit") or ex.get("govern_accept_met") and ex.get("deny_count", 0) >= 1:
            return True
        return bool(ex.get("deny_count", 0) >= 1)
    if hook == "implement_soft_stall":
        if ex.get("max_turns_hit") or ex.get("read_streak_deny") or (
            ex.get("acceptance_failed") and not ex.get("writes_this_step")
        ):
            return True
        return False
    if hook == "batch_test_fail":
        return str(ex.get("gate_action") or "") == "repair"
    if hook == "merge_fail":
        # Call once after v5aa candidates already exhausted (hook fires then).
        return True
    return True


def ask_supervisor(observation: dict[str, Any]) -> SupervisorDecision:
    """Return a safe action. Never raises; never marks green.

    When ARC_HARNESS_SUPERVISOR is off, returns continue with no HTTP.
    """
    global _calls_total, _last_call_monotonic, _last_fingerprint

    if not supervisor_enabled():
        return SupervisorDecision(
            action=ACTION_CONTINUE,
            reason="supervisor_disabled",
            source="disabled",
        )

    hook = str(observation.get("hook") or "").strip()
    allowed = HOOK_ALLOWED.get(hook, frozenset({ACTION_CONTINUE}))
    raw_allowed = observation.get("allowed_actions")
    if isinstance(raw_allowed, list) and raw_allowed:
        allowed = frozenset(str(a) for a in raw_allowed) & allowed
        if not allowed:
            allowed = frozenset({ACTION_CONTINUE})

    extras = observation.get("extras") if isinstance(observation.get("extras"), dict) else {}
    if not should_call_hook(hook, extras):
        return SupervisorDecision(
            action=ACTION_CONTINUE,
            reason="silent:hook_skip_rule",
            source="silent",
        )

    max_total = _env_int("ARC_SUPERVISOR_MAX_CALLS", 8, minimum=0, maximum=100)
    max_per_hook = _env_int("ARC_SUPERVISOR_MAX_CALLS_PER_HOOK", 2, minimum=0, maximum=20)
    min_interval = _env_int("ARC_SUPERVISOR_MIN_INTERVAL_S", 15, minimum=0, maximum=600)

    if _calls_total >= max_total:
        return SupervisorDecision(
            action=ACTION_CONTINUE,
            reason=f"budget:max_calls:{max_total}",
            source="budget",
        )
    if _calls_per_hook.get(hook, 0) >= max_per_hook:
        return SupervisorDecision(
            action=ACTION_CONTINUE,
            reason=f"budget:max_per_hook:{hook}:{max_per_hook}",
            source="budget",
        )

    now = time.monotonic()
    if min_interval and _last_call_monotonic and (now - _last_call_monotonic) < min_interval:
        return SupervisorDecision(
            action=ACTION_CONTINUE,
            reason="budget:min_interval",
            source="budget",
        )

    fp = _observation_fingerprint(observation)
    if fp and fp == _last_fingerprint:
        return SupervisorDecision(
            action=ACTION_CONTINUE,
            reason="budget:duplicate_observation",
            source="budget",
        )

    base_url = (os.environ.get("OPENAI_BASE_URL") or "").strip()
    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    model = (os.environ.get("MODEL") or os.environ.get("ARC_SUPERVISOR_MODEL") or "").strip()
    url = _chat_completions_url(base_url)
    if not url or not api_key or not model:
        return SupervisorDecision(
            action=ACTION_CONTINUE,
            reason="error:missing_OPENAI_or_MODEL",
            source="error",
        )

    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": _env_int("ARC_SUPERVISOR_MAX_TOKENS", 256, minimum=64, maximum=1024),
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {**observation, "allowed_actions": sorted(allowed)},
                    ensure_ascii=False,
                ),
            },
        ],
    }
    body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body_bytes,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "arc-claude-gsc-supervisor/v5ab",
        },
    )
    t0 = time.monotonic()
    try:
        timeout = _env_int("ARC_SUPERVISOR_TIMEOUT_S", 30, minimum=5, maximum=120)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        envelope = json.loads(raw)
        choices = envelope.get("choices") if isinstance(envelope, dict) else None
        content = ""
        if isinstance(choices, list) and choices:
            msg = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(msg, dict):
                content = str(msg.get("content") or "")
        decision = parse_supervisor_response(content, allowed_actions=allowed)
    except Exception as exc:
        decision = SupervisorDecision(
            action=ACTION_CONTINUE,
            reason=f"error:{type(exc).__name__}:{exc}"[:240],
            source="error",
        )

    _calls_total += 1
    _calls_per_hook[hook] = _calls_per_hook.get(hook, 0) + 1
    _last_call_monotonic = time.monotonic()
    _last_fingerprint = fp
    latency_ms = int((_last_call_monotonic - t0) * 1000)
    host = urlparse(url).netloc or ""
    evt = {
        "event": "harness_supervisor",
        "hook": hook,
        "action": decision.action,
        "reason": decision.reason,
        "source": decision.source,
        "calls_so_far": _calls_total,
        "latency_ms": latency_ms,
        "upstream_host": host,
        "ok": decision.source == "model",
    }
    print(json.dumps(evt, ensure_ascii=False), flush=True)
    note_event(evt)
    return decision


def apply_nudge(repair_note_path: Path, text: str) -> None:
    """Append sanitized recovery hint to repair_note / step prompt feed. Never skips gates."""
    nudge = sanitize_nudge_text(text)
    if not nudge:
        return
    path = Path(repair_note_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    block = f"\n--- harness_supervisor nudge ---\n{nudge}\n"
    prev = ""
    if path.is_file():
        try:
            prev = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            prev = ""
    path.write_text((prev + block)[-8000:], encoding="utf-8")


def reset_counters_for_tests() -> None:
    """Test helper only."""
    global _calls_total, _last_call_monotonic, _last_fingerprint
    _calls_total = 0
    _calls_per_hook.clear()
    _last_call_monotonic = 0.0
    _last_fingerprint = ""
    _event_ring.clear()
