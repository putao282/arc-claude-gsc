# -*- coding: utf-8 -*-
"""v5an: strip blocking_limit/max_turns fail-closed after DESIGN; raise max_turns."""
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
SPEC = importlib.util.spec_from_file_location("arc_main_v5an", ROOT / "main.py")
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
assert SPEC.loader is not None
SPEC.loader.exec_module(mod)

SDK_SPEC = importlib.util.spec_from_file_location("arc_sdk_v5an", ROOT / "sdk_driver.py")
sdk = importlib.util.module_from_spec(SDK_SPEC)
sys.modules[SDK_SPEC.name] = sdk
assert SDK_SPEC.loader is not None
SDK_SPEC.loader.exec_module(sdk)


class V5anSoftContinueTests(unittest.TestCase):
    def test_policy_tags(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        for tag in (
            "v5an_no_blocking_limit_fail_closed",
            "v5an_no_max_turns_fail_closed",
            "v5an_phase_soft_continue",
            "v5an_raised_max_turns",
            "phase_soft_continue",
        ):
            self.assertIn(tag, src)
        # orchestration tag may be v5an or v5ao successor
        self.assertTrue(
            "v5an_thin_cc_launcher_soft_continue" in src
            or "v5ao_thin_cc_launcher_soft_continue" in src
        )
        # Must not revive forbidden Agent-OS soft_accept marker
        self.assertNotIn("soft_accept:max_turns", src)

    def test_soft_continue_reasons(self):
        # v5an core reasons must remain; v5ao may extend the set.
        self.assertTrue(
            {"blocking_limit", "max_turns"} <= set(mod.PHASE_SOFT_CONTINUE_REASONS)
        )
        for reason in ("blocking_limit", "max_turns"):
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

    def test_raised_max_turns(self):
        self.assertGreaterEqual(sdk.DEFAULT_MAX_TURNS, 250)
        self.assertGreaterEqual(sdk.max_turns_for_step("prd"), 250)
        self.assertGreaterEqual(sdk.max_turns_for_step("implement"), 300)
        self.assertGreaterEqual(sdk.max_turns_for_step("batch_test"), 250)

    def test_phase_loop_uses_soft_continue(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("phase_soft_continue_reason", src)
        self.assertIn('"event": "phase_soft_continue"', src)
        self.assertIn("idx < len(phases) - 1", src)
        # Still thin — no merge/vitest orchestration revived
        for forbidden in (
            "merge_domain_worktree",
            "run_module_validation",
            "evaluate_step_acceptance",
            "harness_supervisor",
        ):
            self.assertNotIn(forbidden, src)


if __name__ == "__main__":
    unittest.main()
