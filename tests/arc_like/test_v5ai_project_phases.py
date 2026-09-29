# -*- coding: utf-8 -*-
"""v5ai: project-wide DESIGN → IMPLEMENT → BATCH_TEST once; no pages; audit OFF."""
from __future__ import annotations

import importlib.util
import json
import os
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
SPEC = importlib.util.spec_from_file_location("arc_main_v5ai", ROOT / "main.py")
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
assert SPEC.loader is not None
SPEC.loader.exec_module(mod)


def _module(node_id: str, name: str = "Demo", index: int = 1, total: int = 3):
    return mod.RequirementModule(
        index=index,
        total=total,
        node_id=node_id,
        name=name,
        subtree={"id": node_id, "name": name, "description": "demo"},
    )


class V5aiProjectPhasesTests(unittest.TestCase):
    def test_phase_step_splits(self):
        self.assertEqual(
            [s.step_id for s in mod.design_steps()],
            ["prd", "spec", "test_dag"],
        )
        self.assertEqual(
            [s.step_id for s in mod.domain_dev_steps()],
            ["implement"],
        )
        self.assertEqual(
            [s.step_id for s in mod.wave_batch_steps()],
            ["batch_test"],
        )
        self.assertEqual(
            [s.step_id for s in mod.official_steps()],
            ["prd", "spec", "test_dag", "implement", "batch_test"],
        )
        self.assertNotIn("pages", [s.step_id for s in mod.official_steps()])

    def test_audit_default_off_no_pages(self):
        old = os.environ.pop("ARC_ENABLE_MCP_AUDIT_STEPS", None)
        try:
            self.assertFalse(mod.mcp_audit_steps_enabled())
            os.environ["ARC_ENABLE_MCP_AUDIT_STEPS"] = "1"
            self.assertTrue(mod.mcp_audit_steps_enabled())
            ids = [s.step_id for s in mod.official_steps()]
            self.assertEqual(
                ids,
                ["prd", "spec", "govern", "test_dag", "implement", "audit_refactor", "batch_test"],
            )
            self.assertEqual(
                [s.step_id for s in mod.design_steps()],
                ["prd", "spec", "govern", "test_dag"],
            )
            self.assertEqual(
                [s.step_id for s in mod.domain_dev_steps()],
                ["implement", "audit_refactor"],
            )
            self.assertNotIn("pages", ids)
        finally:
            if old is None:
                os.environ.pop("ARC_ENABLE_MCP_AUDIT_STEPS", None)
            else:
                os.environ["ARC_ENABLE_MCP_AUDIT_STEPS"] = old

    def test_main_source_has_three_phases_no_per_req_design(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("phase_design_started", src)
        self.assertIn("phase_implement_started", src)
        self.assertIn("phase_batch_test_started", src)
        self.assertIn("stamp_project_design_siblings", src)
        self.assertIn("v5ai_project_wide_design_implement_batch_phases", src)
        self.assertIn("v5ai_no_per_req_design_reentry", src)
        self.assertIn("v5ai_implement_no_soft_accept_max_turns", src)
        # Old per-REQ design re-entry pattern must be gone
        self.assertNotIn("pre_implement", src)
        self.assertNotIn(
            'for s in domain_dev_steps()\n                    if s.step_id not in ("implement", "audit_refactor")',
            src,
        )
        # Control flow must not run design STEPs inside DOMAIN loop after implement starts
        self.assertIn("NEVER re-run PRD/SPEC/TEST_DAG", src)
        self.assertIn("PHASE DESIGN once", src)

    def test_stamp_project_design_siblings(self):
        primary = _module("REQ-1", "One", 1, 3)
        sibs = [_module("REQ-2", "Two", 2, 3), _module("REQ-3", "Three", 3, 3)]
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            for step in mod.design_steps():
                acceptance = mod.StepAcceptance(
                    ok=True,
                    reason=f"design {step.step_id} ok",
                    soft_notes=("seed",),
                )
                mod.write_step_receipt(out, primary.node_id, step, acceptance)
            mod.stamp_project_design_siblings(
                output_dir=out, primary=primary, siblings=sibs
            )
            for s in sibs:
                for step in mod.design_steps():
                    self.assertTrue(mod.has_step_receipt(out, s.node_id, step.step_id))
                    payload = json.loads(
                        mod.step_receipt_json_path(out, s.node_id, step.step_id).read_text()
                    )
                    self.assertEqual(payload["shared_from"], "REQ-1")
                    self.assertTrue(payload.get("project_design_phase"))
                    self.assertIn("v5ai_project_design_stamp", payload.get("soft_notes") or [])

    def test_step_prompts_state_three_phases(self):
        m = _module("REQ-1")
        prd = next(s for s in mod.official_steps() if s.step_id == "prd")
        impl = next(s for s in mod.official_steps() if s.step_id == "implement")
        batch = next(s for s in mod.official_steps() if s.step_id == "batch_test")
        design_prompt = mod.step_prompt(
            m,
            Path("/tmp/req"),
            None,
            [],
            "web",
            prd,
            wave_ctx={
                "phase": "design",
                "phase_label": "DESIGN",
                "all_req_ids": ["REQ-1", "REQ-2"],
                "all_subtrees": [
                    {"id": "REQ-1", "name": "One"},
                    {"id": "REQ-2", "name": "Two"},
                ],
                "sibling_reqs": ["REQ-1", "REQ-2"],
                "plan_summary": "WAVE1: D[REQ-1,REQ-2]",
                "wave_index": 0,
                "wave_total": 1,
                "wave_domains": ["D"],
                "domain_id": "PROJECT",
                "worktree": "/tmp/out",
            },
        )
        self.assertIn("PHASE DESIGN", design_prompt)
        self.assertIn("DESIGN SCOPE", design_prompt)
        self.assertIn("REQ-2", design_prompt)
        self.assertIn("THREE PHASES once", design_prompt)
        self.assertIn("FORBIDDEN: per-REQ serial design re-init", design_prompt)

        impl_prompt = mod.step_prompt(
            m,
            Path("/tmp/req"),
            None,
            [],
            "web",
            impl,
            wave_ctx={
                "phase": "implement",
                "phase_label": "IMPLEMENT",
                "wave_index": 1,
                "wave_total": 1,
                "wave_domains": ["D"],
                "domain_id": "D",
                "worktree": "/tmp/wt",
                "sibling_reqs": ["REQ-1"],
                "all_req_ids": ["REQ-1", "REQ-2"],
                "plan_summary": "WAVE1: D[REQ-1,REQ-2]",
            },
        )
        self.assertIn("PHASE IMPLEMENT", impl_prompt)
        self.assertIn("NEVER re-run PRD/SPEC/TEST_DAG", impl_prompt)
        self.assertIn("CODING + TEST LOOP", impl_prompt)

        batch_prompt = mod.step_prompt(
            m,
            Path("/tmp/req"),
            None,
            [],
            "web",
            batch,
            wave_ctx={
                "phase": "batch_test",
                "phase_label": "BATCH_TEST",
                "centralized_once": True,
                "all_req_ids": ["REQ-1", "REQ-2", "REQ-3"],
                "sibling_reqs": ["REQ-1", "REQ-2", "REQ-3"],
                "wave_req_ids": ["REQ-1", "REQ-2", "REQ-3"],
                "wave_index": 1,
                "wave_total": 1,
                "wave_domains": ["D"],
                "domain_id": "PROJECT",
                "worktree": "/tmp/out",
                "plan_summary": "WAVE1: D[REQ-1,REQ-2,REQ-3]",
            },
        )
        self.assertIn("PHASE BATCH_TEST", batch_prompt)
        self.assertIn("ONE consolidated project test", batch_prompt)
        self.assertIn("serial per-REQ", batch_prompt.lower().replace("per-req", "per-REQ") or "serial")
        self.assertIn("REQ-1 PASS then REQ-2 then REQ-3", batch_prompt)

    def test_claude_md_footer_states_three_phases(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            mod.ensure_arc_spawn_gate_softener(root / "proj", home_dir=home)
            body = (root / "proj" / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn("Project phases (v5ai", body)
            self.assertIn("DESIGN once", body)
            self.assertIn("IMPLEMENT once", body)
            self.assertIn("BATCH_TEST once", body)
            self.assertIn("No PAGES stage", body)
            self.assertNotIn("5. **pages**", body)

    def test_soft_accept_theater_gone_v5aj(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        # v5aj: soft-accept@max_turns theater deleted entirely (not only skipped for implement).
        self.assertNotIn("soft_accept:max_turns", src)
        self.assertNotIn("soft_accept_denied_no_write", src)
        self.assertNotIn("in_attempt_write_progress", src)
        self.assertIn("v5aj_no_soft_accept_theater", src)
        self.assertIn("NO soft-accept@max_turns theater", src)


if __name__ == "__main__":
    unittest.main()
