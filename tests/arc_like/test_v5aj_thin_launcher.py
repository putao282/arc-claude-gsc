# -*- coding: utf-8 -*-
"""v5aj: main.py is ONLY a thin Claude Code launcher shell (three phases once)."""
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
SPEC = importlib.util.spec_from_file_location("arc_main_v5aj", ROOT / "main.py")
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
assert SPEC.loader is not None
SPEC.loader.exec_module(mod)


class V5ajThinLauncherTests(unittest.TestCase):
    def test_three_phases_once_no_pages(self):
        self.assertEqual([s.step_id for s in mod.design_steps()], ["prd", "spec", "test_dag"])
        self.assertEqual([s.step_id for s in mod.domain_dev_steps()], ["implement"])
        self.assertEqual([s.step_id for s in mod.wave_batch_steps()], ["batch_test"])
        ids = [s.step_id for s in mod.official_steps()]
        self.assertEqual(ids, ["prd", "spec", "test_dag", "implement", "batch_test"])
        self.assertNotIn("pages", ids)

    def test_audit_default_off(self):
        old = os.environ.pop("ARC_ENABLE_MCP_AUDIT_STEPS", None)
        try:
            self.assertFalse(mod.mcp_audit_steps_enabled())
        finally:
            if old is None:
                os.environ.pop("ARC_ENABLE_MCP_AUDIT_STEPS", None)
            else:
                os.environ["ARC_ENABLE_MCP_AUDIT_STEPS"] = old

    def test_no_per_req_design_reentry_markers(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("phase_design_started", src)
        self.assertIn("phase_implement_started", src)
        self.assertIn("phase_batch_test_started", src)
        self.assertIn("stamp_project_design_siblings", src)
        self.assertIn("NEVER re-run PRD/SPEC/TEST_DAG", src)
        self.assertIn("PHASE DESIGN once", src)
        self.assertNotIn("pre_implement", src)

    def test_agent_os_machinery_deleted(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("import supervisor", src)
        self.assertNotIn("harness_supervisor.", src)
        self.assertFalse((ROOT / "supervisor.py").exists())
        self.assertNotIn("soft_accept:max_turns", src)
        self.assertNotIn("in_attempt_write_progress", src)
        self.assertNotIn("check_test_dag_feature_wiring", src)
        self.assertNotIn("check_pages_not_stub", src)
        self.assertNotIn("check_app_page_default_exports", src)
        self.assertNotIn("supervisor_nudge_path", src)

    def test_kept_budget_merge_build_cc_config(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('ARC_MAX_BUDGET_USD", "150"', src)
        self.assertIn("merge_domain_worktree", src)
        self.assertIn("v5aa_merge_domain_worktree_abort_theirs", src)
        self.assertIn("run_project_build", src)
        self.assertIn("assert_cc_full_config", src)
        self.assertIn("setting_sources", src)
        self.assertTrue(hasattr(mod, "assert_cc_full_config"))
        self.assertTrue(hasattr(mod, "install_contest_claude_md"))

    def test_policy_tags_v5aj(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        for tag in (
            "v5aj_thin_cc_launcher_shell_only",
            "v5aj_no_harness_supervisor",
            "v5aj_no_soft_accept_theater",
            "v5aj_no_feature_wiring_fail_closed",
            "v5aj_three_phase_once",
            "v5aj_delete_agent_os_leftover",
            "v5ai_project_wide_design_implement_batch_phases",
        ):
            self.assertIn(tag, src)


if __name__ == "__main__":
    unittest.main()
