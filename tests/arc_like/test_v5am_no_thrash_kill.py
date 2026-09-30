# -*- coding: utf-8 -*-
"""v5am: no rapid_refill_breaker / Read-streak thrash kill gates."""
from __future__ import annotations

import ast
import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _install_stubs():
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        sys.modules["claude_agent_sdk"] = types.ModuleType("claude_agent_sdk")
    try:
        import arcbench_agent_runtime  # noqa: F401
    except ImportError:
        runtime = types.ModuleType("arcbench_agent_runtime")

        class AgentRuntime:
            pass

        runtime.AgentRuntime = AgentRuntime
        sys.modules["arcbench_agent_runtime"] = runtime
    try:
        import yaml  # noqa: F401
    except ImportError:
        yaml_mod = types.ModuleType("yaml")
        yaml_mod.safe_load = lambda text: (_ for _ in ()).throw(RuntimeError("no yaml"))
        sys.modules["yaml"] = yaml_mod


_install_stubs()
ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("arc_main_v5am", ROOT / "main.py")
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
assert SPEC.loader is not None
SPEC.loader.exec_module(mod)

SDK_SPEC = importlib.util.spec_from_file_location("arc_sdk_v5am", ROOT / "sdk_driver.py")
sdk = importlib.util.module_from_spec(SDK_SPEC)
sys.modules[SDK_SPEC.name] = sdk
assert SDK_SPEC.loader is not None
SDK_SPEC.loader.exec_module(sdk)


class V5amNoThrashKillTests(unittest.TestCase):
    def test_main_has_v5am_policy_tags(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        for tag in (
            "v5am_no_rapid_refill_breaker",
            "v5am_no_read_streak_kill_gate",
            "v5am_thrash_observe_only",
            "v5am_thin_cc_launcher_no_thrash_kill",
        ):
            self.assertIn(tag, src)

    def test_no_rapid_refill_budget_exhausted_event(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("rapid_refill_budget_exhausted", src)
        self.assertNotIn("max_rapid_refill_attempts", src)
        self.assertNotIn("ARC_RAPID_REFILL_MAX_ATTEMPTS", src)

    def test_execute_with_retry_no_kill_gate_first_hit_return(self):
        # v5am: no rapid_refill_breaker kill gate.
        # v5ap: rapid_refill* is NON_RETRYABLE → single attempt, fail closed.
        calls = {"n": 0}

        def run_attempt(attempt):
            calls["n"] += 1
            return mod.ClaudeRunResult(
                returncode=1,
                is_error=True,
                terminal_reason="rapid_refill_breaker",
                subtype="success",
                api_error_status=None,
                tail="rapid_refill",
            )

        result, attempts = mod.execute_with_retry(
            run_attempt,
            max_retries=2,
            base_seconds=0,
            max_seconds=0,
            sleep_fn=lambda _s: None,
        )
        self.assertEqual(attempts, 1)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(result.terminal_reason, "rapid_refill_breaker")

    def test_sdk_thrash_hooks_never_deny(self):
        import asyncio

        guard = sdk.McpThrashGuard(step_id="implement", read_streak_limit=2)
        hooks = sdk.build_thrash_pretool_hooks(guard)
        matchers = hooks["PreToolUse"]
        self.assertTrue(matchers)
        hook_fn = matchers[0].hooks[0]

        async def drive():
            # Burn past read streak limit
            for _ in range(5):
                out = await hook_fn(
                    {"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {"file_path": "x"}},
                    None,
                    None,
                )
                self.assertEqual(out, {})
                self.assertNotIn("hookSpecificOutput", out)
            # Identical MCP beyond hard limit
            for _ in range(5):
                out = await hook_fn(
                    {
                        "hook_event_name": "PreToolUse",
                        "tool_name": "mcp__arch__spec_read",
                        "tool_input": {"path": "a"},
                    },
                    None,
                    None,
                )
                self.assertEqual(out, {})
            # No deny_events accumulated for read streak (observe-only)
            for ev in guard.deny_events:
                self.assertNotEqual(ev.get("level"), "read_streak_deny")

        asyncio.run(drive())
        levels = [e.get("level") for e in guard.events]
        self.assertTrue(any(lv == "read_streak_observe" for lv in levels))


if __name__ == "__main__":
    unittest.main()
