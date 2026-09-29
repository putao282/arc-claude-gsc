# -*- coding: utf-8 -*-
"""v5ag: thin CC orchestrator — no pages STEP, audit default OFF, implement allows tests."""
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
SPEC = importlib.util.spec_from_file_location("arc_main_v5ag", ROOT / "main.py")
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
assert SPEC.loader is not None
SPEC.loader.exec_module(mod)


class V5agThinOrchestratorTests(unittest.TestCase):
    def test_pages_not_in_domain_dev_steps(self):
        ids = [s.step_id for s in mod.domain_dev_steps()]
        self.assertNotIn("pages", ids)
        self.assertEqual(ids, ["prd", "spec", "test_dag", "implement"])

    def test_pages_not_in_official_steps(self):
        ids = [s.step_id for s in mod.official_steps()]
        self.assertNotIn("pages", ids)
        self.assertEqual(ids, ["prd", "spec", "test_dag", "implement", "batch_test"])

    def test_mcp_audit_default_false(self):
        old = os.environ.pop("ARC_ENABLE_MCP_AUDIT_STEPS", None)
        try:
            self.assertFalse(mod.mcp_audit_steps_enabled())
            # enabling still inserts govern/audit_refactor (no pages)
            os.environ["ARC_ENABLE_MCP_AUDIT_STEPS"] = "1"
            self.assertTrue(mod.mcp_audit_steps_enabled())
            ids = [s.step_id for s in mod.official_steps()]
            self.assertIn("govern", ids)
            self.assertIn("audit_refactor", ids)
            self.assertNotIn("pages", ids)
        finally:
            if old is None:
                os.environ.pop("ARC_ENABLE_MCP_AUDIT_STEPS", None)
            else:
                os.environ["ARC_ENABLE_MCP_AUDIT_STEPS"] = old

    def test_implement_allows_mid_dev_tests(self):
        impl = next(s for s in mod.official_steps() if s.step_id == "implement")
        self.assertFalse(impl.forbid_mid_dev_tests)
        prompt = mod.step_prompt(
            mod.RequirementModule(1, 1, "REQ-1", "Demo", {"id": "REQ-1", "name": "Demo"}),
            Path("/tmp/req"),
            None,
            [],
            "web",
            impl,
        )
        self.assertIn("CODING + TEST LOOP", prompt)
        self.assertIn("Mid-dev tests ARE allowed", prompt)
        self.assertNotIn("FORBIDDEN this STEP: do not run vitest", prompt)

    def test_claude_md_has_no_pages_step(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mod.ensure_arc_spawn_gate_softener(root)
            body = (root / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn("thin CC orchestrator", body)
            self.assertIn("NO separate pages STEP", body)
            self.assertNotIn("5. **pages**", body)
            self.assertIn("CODING + TEST LOOP", body)


if __name__ == "__main__":
    unittest.main()
