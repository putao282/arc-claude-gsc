# -*- coding: utf-8 -*-
"""v5ao soft-continue rapid_refill SUPERSEDED by v5ap fail-closed."""
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
SPEC = importlib.util.spec_from_file_location("arc_main_v5ao_legacy", ROOT / "main.py")
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
assert SPEC.loader is not None
SPEC.loader.exec_module(mod)


class V5aoSoftContinueSupersededTests(unittest.TestCase):
    def test_soft_continue_and_degrade_gone(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        for gone in (
            "v5ao_soft_continue_rapid_refill",
            "v5ao_phase_soft_continue",
            "PHASE_SOFT_CONTINUE_REASONS",
            "DEGRADED_SYSTEM_APPEND",
            "rapid_refill_needs_degrade",
        ):
            self.assertNotIn(gone, src)
        self.assertIn("v5ap_fail_closed_mid_phase", src)
        self.assertIn("v5ap_agent_owns_self_heal", src)


if __name__ == "__main__":
    unittest.main()
