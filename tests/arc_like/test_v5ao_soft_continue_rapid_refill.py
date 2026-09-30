# -*- coding: utf-8 -*-
"""v5ao: soft-continue rapid_refill_breaker mid-phase; no 3-retry burn."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import types
import unittest
from contextlib import redirect_stdout
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
SPEC = importlib.util.spec_from_file_location("arc_main_v5ao", ROOT / "main.py")
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
assert SPEC.loader is not None
SPEC.loader.exec_module(mod)


class V5aoSoftContinueRapidRefillTests(unittest.TestCase):
    def test_policy_tags(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        for tag in (
            "v5ao_thin_cc_launcher_soft_continue",
            "v5ao_soft_continue_rapid_refill",
            "v5ao_no_rapid_refill_retry_burn",
            "v5ao_phase_soft_continue",
            "v5an_phase_soft_continue",
            "v5am_no_rapid_refill_breaker",
            "phase_soft_continue",
        ):
            self.assertIn(tag, src)
        self.assertNotIn("soft_accept:max_turns", src)
        for forbidden in (
            "merge_domain_worktree",
            "run_module_validation",
            "evaluate_step_acceptance",
            "harness_supervisor",
            "rapid_refill_budget_exhausted",
        ):
            self.assertNotIn(forbidden, src)

    def test_soft_continue_includes_rapid_refill(self):
        self.assertTrue(
            {"blocking_limit", "max_turns", "rapid_refill_breaker", "rapid_refill"}
            <= set(mod.PHASE_SOFT_CONTINUE_REASONS)
        )
        for reason in ("rapid_refill_breaker", "rapid_refill", "blocking_limit", "max_turns"):
            r = mod.ClaudeRunResult(
                returncode=1,
                is_error=True,
                terminal_reason=reason,
                subtype="error",
                api_error_status=None,
                tail="",
            )
            self.assertEqual(mod.phase_soft_continue_reason(r), reason)
        hard = mod.ClaudeRunResult(
            returncode=1,
            is_error=True,
            terminal_reason="budget_exhausted",
            subtype="error",
            api_error_status=None,
            tail="",
        )
        self.assertIsNone(mod.phase_soft_continue_reason(hard))

    def test_rapid_refill_does_not_burn_retries(self):
        calls = {"n": 0}

        def run_attempt(attempt):
            calls["n"] += 1
            return mod.ClaudeRunResult(
                returncode=1,
                is_error=True,
                terminal_reason="rapid_refill_breaker",
                subtype="error",
                api_error_status=None,
                tail="autocompact is thrashing / rapid_refill",
            )

        buf = io.StringIO()
        with redirect_stdout(buf):
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
        soft = mod.phase_soft_continue_reason(result)
        self.assertEqual(soft, "rapid_refill_breaker")

    def test_implement_rapid_refill_soft_continues_to_batch_test(self):
        """Simulate thin phase loop: IMPLEMENT rapid_refill_breaker → BATCH_TEST, not exit 1."""
        phases = (
            ("design", "prd"),
            ("implement", "implement"),
            ("batch_test", "batch_test"),
        )
        outcomes = {
            "design": mod.ClaudeRunResult(
                returncode=0, is_error=False, terminal_reason="success",
                subtype="success", api_error_status=None, tail="",
            ),
            "implement": mod.ClaudeRunResult(
                returncode=1, is_error=True, terminal_reason="rapid_refill_breaker",
                subtype="error", api_error_status=None, tail="rapid_refill",
            ),
            "batch_test": mod.ClaudeRunResult(
                returncode=0, is_error=False, terminal_reason="success",
                subtype="success", api_error_status=None, tail="",
            ),
        }
        launched = []
        exit_code = None
        soft_events = []

        for idx, (phase, step_id) in enumerate(phases):
            # Mimic execute_with_retry first-hit return for rapid_refill
            result = outcomes[phase]
            launched.append(phase)
            if "rapid_refill" in (result.terminal_reason or ""):
                # would return attempt=1 from execute_with_retry
                pass
            failed = result.is_error or (
                result.returncode not in (0, None) and int(result.returncode) != 0
            )
            if not failed:
                continue
            soft = mod.phase_soft_continue_reason(result)
            if soft is not None and idx < len(phases) - 1:
                soft_events.append(
                    {"phase": phase, "terminal_reason": soft, "next_phase": phases[idx + 1][0]}
                )
                continue
            exit_code = int(result.returncode or 1)
            break
        else:
            exit_code = 0

        self.assertEqual(launched, ["design", "implement", "batch_test"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(soft_events), 1)
        self.assertEqual(soft_events[0]["phase"], "implement")
        self.assertEqual(soft_events[0]["next_phase"], "batch_test")
        self.assertEqual(soft_events[0]["terminal_reason"], "rapid_refill_breaker")


if __name__ == "__main__":
    unittest.main()
