# -*- coding: utf-8 -*-
"""v5ad: ONE-SHOT WAVE-central BATCH_TEST — no serial per-REQ after merge."""
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
SPEC = importlib.util.spec_from_file_location("arc_main_v5ad", ROOT / "main.py")
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
assert SPEC.loader is not None
SPEC.loader.exec_module(mod)


def _module(node_id: str, name: str = "Demo"):
    return mod.RequirementModule(
        index=1,
        total=3,
        node_id=node_id,
        name=name,
        subtree={"id": node_id, "name": name, "description": "demo"},
    )


class WaveCentralBatchV5adTests(unittest.TestCase):
    def test_batch_prompt_forbids_serial_and_says_one_shot(self):
        m = _module("REQ-1")
        step = next(s for s in mod.official_steps() if s.step_id == "batch_test")
        prompt = mod.step_prompt(
            m,
            Path("/tmp/req"),
            None,
            [],
            "web",
            step,
            wave_ctx={
                "wave_index": 1,
                "wave_total": 1,
                "wave_domains": ["D"],
                "domain_id": "D",
                "worktree": "/tmp/out",
                "sibling_reqs": ["REQ-1", "REQ-2", "REQ-3"],
                "wave_req_ids": ["REQ-1", "REQ-2", "REQ-3"],
                "plan_summary": "WAVE1: D[REQ-1,REQ-2,REQ-3]",
                "centralized_once": True,
                "phase": "wave_batch_test",
            },
        )
        self.assertIn("ONE-SHOT", prompt)
        self.assertIn("FORBIDDEN", prompt)
        self.assertIn("serial per-REQ", prompt.lower().replace("per-req", "per-REQ") or "serial")
        self.assertIn("v5ad", prompt)
        self.assertIn("REQ-1 PASS then REQ-2 then REQ-3", prompt)

    def test_stamp_wave_batch_siblings_writes_receipts_without_agent(self):
        primary = _module("REQ-1", "One")
        sibs = [_module("REQ-2", "Two"), _module("REQ-3", "Three")]
        events = types.SimpleNamespace(
            mark_implementation_done=mock.Mock(),
            mark_test_passed=mock.Mock(),
        )
        git = types.SimpleNamespace(commit=mock.Mock())
        runtime = types.SimpleNamespace(events=events, git=git, traceability=None)

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            # Seed primary green validation receipt
            validation = mod.ValidationResult(
                ok=True,
                exit_code=0,
                cmd=["npx", "--yes", "vitest", "run"],
                log_tail="ok",
                reason="harness local validation passed",
                project_dir=str(out / "frontend"),
            )
            mod.write_validation_receipt(out, primary.node_id, validation)
            completed: list[str] = [primary.node_id]
            mod.stamp_wave_batch_siblings(
                runtime=runtime,
                output_dir=out,
                primary=primary,
                siblings=sibs,
                wave_index=1,
                completed=completed,
            )
            for s in sibs:
                self.assertTrue(mod.has_step_receipt(out, s.node_id, "batch_test"))
                self.assertTrue(
                    (out / ".arc" / "validation" / f"{s.node_id}.ok").is_file()
                    or mod.validation_receipt_ok_path(out, s.node_id).is_file()
                )
            self.assertEqual(events.mark_test_passed.call_count, 2)
            self.assertEqual(events.mark_implementation_done.call_count, 2)
            self.assertEqual(set(completed), {"REQ-1", "REQ-2", "REQ-3"})
            # Receipt reason names primary (no serial agent)
            payload = json.loads(
                mod.step_receipt_json_path(out, "REQ-2", "batch_test").read_text()
            )
            self.assertIn("REQ-1", payload["reason"])
            self.assertIn("v5ad", " ".join(payload.get("soft_notes") or []))

    def test_source_has_one_shot_not_serial_loop(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("ONE-SHOT centralized BATCH_TEST after WAVE merge (v5ad)", src)
        self.assertIn("stamp_wave_batch_siblings", src)
        self.assertIn("v5ad_wave_central_one_shot_batch_test", src)
        # The forbidden serial pattern must not remain as the live path
        self.assertNotIn(
            'for module in wave_modules:\n                if module_already_passed',
            src,
        )
        self.assertIn("pending_batch", src)


if __name__ == "__main__":
    unittest.main()
