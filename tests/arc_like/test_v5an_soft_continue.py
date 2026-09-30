# -*- coding: utf-8 -*-
"""v5an soft-continue SUPERSEDED by v5ap fail-closed (Agent owns self-heal)."""
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
SPEC = importlib.util.spec_from_file_location("arc_main_v5an_legacy", ROOT / "main.py")
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
assert SPEC.loader is not None
SPEC.loader.exec_module(mod)

SDK_SPEC = importlib.util.spec_from_file_location("arc_sdk_v5an_legacy", ROOT / "sdk_driver.py")
sdk = importlib.util.module_from_spec(SDK_SPEC)
sys.modules[SDK_SPEC.name] = sdk
assert SDK_SPEC.loader is not None
SDK_SPEC.loader.exec_module(sdk)


class V5anSoftContinueSupersededTests(unittest.TestCase):
    def test_soft_continue_removed(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("PHASE_SOFT_CONTINUE_REASONS", src)
        self.assertNotIn("phase_soft_continue_reason", src)
        self.assertNotIn('"event": "phase_soft_continue"', src)
        self.assertIn("v5ap_thin_cc_launcher_fail_closed", src)
        self.assertIn("v5ap_no_soft_continue", src)

    def test_raised_max_turns_kept(self):
        self.assertGreaterEqual(sdk.DEFAULT_MAX_TURNS, 250)
        self.assertGreaterEqual(sdk.max_turns_for_step("prd"), 250)
        self.assertGreaterEqual(sdk.max_turns_for_step("implement"), 300)
        self.assertGreaterEqual(sdk.max_turns_for_step("batch_test"), 250)


if __name__ == "__main__":
    unittest.main()
