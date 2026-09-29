# -*- coding: utf-8 -*-
"""v5af/v5aj: npm build hard gate kept; feature-wiring/pages helpers DELETED (v5aj)."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


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
SPEC = importlib.util.spec_from_file_location("arc_main_v5af", ROOT / "main.py")
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
assert SPEC.loader is not None
SPEC.loader.exec_module(mod)


def _module(node_id: str = "REQ-1", name: str = "Demo"):
    return mod.RequirementModule(
        index=1,
        total=1,
        node_id=node_id,
        name=name,
        subtree={"id": node_id, "name": name, "description": "demo"},
    )


class V5afBuildExportFeatureGates(unittest.TestCase):
    def test_run_project_build_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fe = root / "frontend"
            fe.mkdir()
            (fe / "package.json").write_text(
                json.dumps({"name": "frontend", "scripts": {"build": "vite build"}}),
                encoding="utf-8",
            )
            with mock.patch("subprocess.run") as run:
                run.return_value = mock.Mock(returncode=1, stdout="", stderr="build boom")
                result = mod.run_project_build(fe)
            self.assertFalse(result.ok)
            self.assertIn("npm run build failed", result.reason)

    def test_run_project_build_ok(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fe = root / "frontend"
            fe.mkdir()
            (fe / "package.json").write_text(
                json.dumps({"name": "frontend", "scripts": {"build": "vite build"}}),
                encoding="utf-8",
            )
            with mock.patch("subprocess.run") as run:
                run.return_value = mock.Mock(returncode=0, stdout="ok", stderr="")
                result = mod.run_project_build(fe)
            self.assertTrue(result.ok)

    def test_feature_wiring_helpers_deleted_v5aj(self):
        """v5aj: product-rule scanners deleted — CC owns coding; vitest+build end gate only."""
        self.assertFalse(hasattr(mod, "check_app_page_default_exports"))
        self.assertFalse(hasattr(mod, "check_pages_not_stub"))
        self.assertFalse(hasattr(mod, "check_test_dag_feature_wiring"))
        self.assertNotIn("pages", [s.step_id for s in mod.official_steps()])

    def test_implement_prompt_is_code_test_loop_v5ag(self):
        m = _module()
        impl = next(s for s in mod.official_steps() if s.step_id == "implement")
        batch = next(s for s in mod.official_steps() if s.step_id == "batch_test")
        ip = mod.step_prompt(m, Path("/tmp/req"), None, [], "web", impl)
        bp = mod.step_prompt(m, Path("/tmp/req"), None, [], "web", batch)
        self.assertIn("CODING + TEST LOOP", ip)
        self.assertNotIn("FEATURE WIRING HARD", ip)
        self.assertFalse(impl.forbid_mid_dev_tests)
        self.assertIn("npm run build", bp.lower())

    def test_policy_tags_include_v5ag_v5aj(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        for tag in (
            "v5af_npm_build_hard_gate_after_vitest",
            "v5ag_thin_cc_orchestrator",
            "v5ag_no_pages_step",
            "v5ag_mcp_audit_default_off",
            "v5ag_implement_code_test_loop",
            "v5ae_govern_green_force_stop_no_supervisor_fail_closed",
            "v5ad_wave_central_one_shot_batch_test",
            "v5ai_project_wide_design_implement_batch_phases",
            "v5aa_merge_domain_worktree_abort_theirs",
            "v5aj_thin_cc_launcher_shell_only",
            "v5aj_no_harness_supervisor",
            "v5aj_no_soft_accept_theater",
            "v5aj_no_feature_wiring_fail_closed",
            "v5aj_three_phase_once",
        ):
            self.assertIn(tag, src)


if __name__ == "__main__":
    unittest.main()
