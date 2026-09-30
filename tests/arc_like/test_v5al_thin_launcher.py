# -*- coding: utf-8 -*-
"""v5al: main.py is ONLY prompts + fixed CC launch (Agent owns merge/test)."""
from __future__ import annotations

import importlib.util
import os
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
SPEC = importlib.util.spec_from_file_location("arc_main_v5al", ROOT / "main.py")
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
assert SPEC.loader is not None
SPEC.loader.exec_module(mod)


class V5alThinLauncherTests(unittest.TestCase):
    def test_agent_os_deleted(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        for forbidden in (
            "merge_domain_worktree",
            "ensure_domain_worktree",
            "run_module_validation",
            "run_project_build",
            "evaluate_step_acceptance",
            "stamp_wave_batch_siblings",
            "stamp_project_design_siblings",
            "build_domain_groups",
            "plan_domain_waves",
            "ensure_arc_spawn_gate_softener",
            "OFFICIAL_STEPS",
            "domain_accept",
            "harness_supervisor",
            "soft_accept:max_turns",
            "check_test_dag_feature_wiring",
            "check_pages_not_stub",
        ):
            self.assertNotIn(forbidden, src, forbidden)
        self.assertFalse(hasattr(mod, "merge_domain_worktree"))
        self.assertFalse(hasattr(mod, "run_module_validation"))
        self.assertFalse(hasattr(mod, "evaluate_step_acceptance"))
        self.assertFalse(hasattr(mod, "ensure_domain_worktree"))

    def test_kept_cc_launch_surface(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('ARC_MAX_BUDGET_USD", "150"', src)
        self.assertIn("assert_cc_full_config", src)
        self.assertIn("install_contest_claude_md", src)
        self.assertIn("run_claude_via_sdk", src)
        self.assertIn("setting_sources", src)
        self.assertTrue(hasattr(mod, "assert_cc_full_config"))
        self.assertTrue(hasattr(mod, "install_contest_claude_md"))
        self.assertTrue(hasattr(mod, "run_claude_via_sdk"))
        self.assertTrue(hasattr(mod, "main"))

    def test_three_phase_prompts_in_main(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('f"phase_{phase}_started"', src)
        self.assertIn('f"phase_{phase}_completed"', src)
        self.assertIn('"design", "prd"', src)
        self.assertIn('"implement", "implement"', src)
        self.assertIn('"batch_test", "batch_test"', src)
        self.assertIn("CURRENT PHASE: DESIGN", src)
        self.assertIn("CURRENT PHASE: IMPLEMENT", src)
        self.assertIn("CURRENT PHASE: BATCH_TEST", src)
        self.assertIn("harness will NOT call git merge", src)
        self.assertIn("Harness will NOT run vitest", src)
        self.assertIn("v5al_thin_cc_launcher_prompts_only", src)

    def test_policy_tags_v5al(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        for tag in (
            "v5al_thin_cc_launcher_shell_only",
            "v5al_agent_owns_merge_test",
            "v5al_no_wave_worktree_orchestrator",
            "v5al_no_python_vitest",
            "v5al_no_merge_helpers",
            "v5al_no_stamp_receipt_theater",
            "v5al_delete_agent_os",
        ):
            self.assertIn(tag, src)

    def test_mcp_default_on(self):
        old = os.environ.pop("ARC_ENABLE_MCP", None)
        try:
            self.assertTrue(mod.mcp_enabled())
        finally:
            if old is None:
                os.environ.pop("ARC_ENABLE_MCP", None)
            else:
                os.environ["ARC_ENABLE_MCP"] = old


if __name__ == "__main__":
    unittest.main()
