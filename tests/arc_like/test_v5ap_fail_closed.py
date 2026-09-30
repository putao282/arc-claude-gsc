# -*- coding: utf-8 -*-
"""v5ap: strip Python self-heal — fail closed mid-phase; Agent owns success."""
from __future__ import annotations

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
SPEC = importlib.util.spec_from_file_location("arc_main_v5ap", ROOT / "main.py")
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
assert SPEC.loader is not None
SPEC.loader.exec_module(mod)


class V5apFailClosedTests(unittest.TestCase):
    def test_policy_tags(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        for tag in (
            "v5ap_thin_cc_launcher_fail_closed",
            "v5ap_no_soft_continue",
            "v5ap_no_python_retry",
            "v5ap_no_degrade_mode",
            "v5ap_agent_owns_self_heal",
            "v5ap_fail_closed_mid_phase",
        ):
            self.assertIn(tag, src)
        for gone in (
            "PHASE_SOFT_CONTINUE_REASONS",
            "phase_soft_continue_reason",
            '"event": "phase_soft_continue"',
            "DEGRADED_SYSTEM_APPEND",
            "rapid_refill_needs_degrade",
            "rapid_refill_degrade_restart",
            "v5ao_soft_continue_rapid_refill",
            "v5an_phase_soft_continue",
            "soft_accept:max_turns",
        ):
            self.assertNotIn(gone, src)
        for forbidden in (
            "merge_domain_worktree",
            "run_module_validation",
            "evaluate_step_acceptance",
            "harness_supervisor",
            "rapid_refill_budget_exhausted",
        ):
            self.assertNotIn(forbidden, src)

    def test_no_soft_continue_helpers(self):
        self.assertFalse(hasattr(mod, "PHASE_SOFT_CONTINUE_REASONS"))
        self.assertFalse(hasattr(mod, "phase_soft_continue_reason"))
        self.assertFalse(hasattr(mod, "DEGRADED_SYSTEM_APPEND"))

    def test_rapid_refill_is_non_retryable(self):
        for reason, tail in (
            ("rapid_refill_breaker", "rapid_refill"),
            ("blocking_limit", "blocking_limit"),
            ("max_turns", "max_turns"),
        ):
            r = mod.ClaudeRunResult(
                returncode=1,
                is_error=True,
                terminal_reason=reason,
                subtype="error",
                api_error_status=None,
                tail=tail,
            )
            clf = mod.classify_claude_failure(r)
            if "rapid_refill" in reason or "rapid_refill" in tail:
                self.assertFalse(clf.retryable, msg=clf.reason)
                self.assertIn("rapid_refill", clf.reason)

    def test_execute_with_retry_does_not_burn_on_rapid_refill(self):
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

    def test_phase_loop_fail_closed_on_implement(self):
        """IMPLEMENT failure must stop before BATCH_TEST (no soft-continue)."""
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
        for phase, step_id in phases:
            result = outcomes[phase]
            launched.append(phase)
            failed = result.is_error or (
                result.returncode not in (0, None) and int(result.returncode) != 0
            )
            if not failed:
                continue
            exit_code = int(result.returncode or 1)
            break
        else:
            exit_code = 0
        self.assertEqual(launched, ["design", "implement"])
        self.assertEqual(exit_code, 1)

    def test_launch_is_single_cc_no_retry_wrapper(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        # _launch must call run_claude_via_sdk directly, not execute_with_retry
        start = src.find("def _launch(phase: str, step_id: str)")
        end = src.find("\n    phases = (", start)
        launch = src[start:end]
        self.assertIn("run_claude_via_sdk(", launch)
        self.assertNotIn("execute_with_retry(", launch)
        self.assertNotIn("degrade_mode", launch)
        self.assertIn('"attempts": 1', launch)

    def test_three_phase_prompts_kept(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("CURRENT PHASE: DESIGN", src)
        self.assertIn("CURRENT PHASE: IMPLEMENT", src)
        self.assertIn("CURRENT PHASE: BATCH_TEST", src)
        self.assertIn('"design", "prd"', src)
        self.assertIn('"implement", "implement"', src)
        self.assertIn('"batch_test", "batch_test"', src)


if __name__ == "__main__":
    unittest.main()
