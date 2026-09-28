"""v5ac ONE root cure: early write + soft-accept write gate + batch_test vitest feed."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock


def _install_test_stubs():
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

        def safe_load(text):
            raise RuntimeError("PyYAML not installed")

        yaml_mod.safe_load = safe_load
        sys.modules["yaml"] = yaml_mod


_install_test_stubs()

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("arc_submission_main_v5ac", ROOT / "main.py")
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
assert SPEC.loader is not None
SPEC.loader.exec_module(mod)


def _module(node_id="REQ-1", name="Demo"):
    return mod.RequirementModule(
        index=1,
        total=1,
        node_id=node_id,
        name=name,
        subtree={"id": node_id, "name": name, "description": "demo"},
    )


def _result(**kwargs):
    base = dict(
        returncode=0,
        is_error=False,
        terminal_reason="",
        subtype="success",
        api_error_status=None,
        tail="",
        skills_loaded=(),
        mcp_tools_used=(),
        builtin_writes=(),
        step_started_at=time.time(),
        thrash_hit=False,
        thrash_events=(),
        deny_events=(),
        thrash_counts=(),
    )
    base.update(kwargs)
    return mod.ClaudeRunResult(**base)


class RootCureV5acTests(unittest.TestCase):
    def test_implement_write_progress_requires_business_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sdir = root / ".arc" / "steps" / "REQ-1"
            sdir.mkdir(parents=True)
            ok, proof = mod.implement_write_progress(
                root,
                sdir,
                _result(builtin_writes=("frontend/src/app.ts",)),
            )
            self.assertTrue(ok)
            self.assertIn("in_session_write", proof)
            bad, _ = mod.implement_write_progress(
                root, sdir, _result(builtin_writes=("README.md",))
            )
            self.assertFalse(bad)

    def test_pages_write_progress_rejects_stale_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sdir = root / ".arc" / "steps" / "REQ-1"
            sdir.mkdir(parents=True)
            # leftover file exists but no in-attempt write
            ui = root / "frontend" / "src" / "pages"
            ui.mkdir(parents=True)
            (ui / "Old.tsx").write_text("export default function Old(){return null}", encoding="utf-8")
            # force step_started_at in the future relative to file mtime
            started = time.time() + 10
            ok, proof = mod.pages_write_progress(
                root, sdir, _result(builtin_writes=(), step_started_at=started)
            )
            self.assertFalse(ok)
            self.assertIn("no_in_attempt", proof)
            ok2, proof2 = mod.pages_write_progress(
                root,
                sdir,
                _result(
                    builtin_writes=("frontend/src/pages/New.tsx",),
                    step_started_at=started,
                ),
            )
            self.assertTrue(ok2)
            self.assertIn("in_session_write", proof2)

    def test_in_attempt_write_progress_dispatch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sdir = root / ".arc" / "steps" / "REQ-1"
            sdir.mkdir(parents=True)
            ok, _ = mod.in_attempt_write_progress(
                "implement",
                root,
                sdir,
                _result(builtin_writes=("src/lib/x.ts",)),
            )
            self.assertTrue(ok)
            ok2, proof = mod.in_attempt_write_progress(
                "pages", root, sdir, _result(builtin_writes=())
            )
            self.assertFalse(ok2)

    def test_pages_acceptance_requires_in_attempt_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sdir = root / ".arc" / "steps" / "REQ-1"
            sdir.mkdir(parents=True)
            ui = root / "frontend" / "src" / "pages"
            ui.mkdir(parents=True)
            (ui / "Stale.tsx").write_text("export default function Stale(){ return null }\n", encoding="utf-8")
            step = next(s for s in mod.official_steps() if s.step_id == "pages")
            # leftover + design_mcp but no write this attempt
            result = _result(
                mcp_tools_used=("mcp__arch__design_style",),
                builtin_writes=(),
                step_started_at=time.time() + 5,
            )
            acc = mod.evaluate_step_acceptance(root, _module(), step, result)
            self.assertFalse(acc.ok)
            self.assertIn("in-attempt write", acc.reason.lower())

            result2 = _result(
                mcp_tools_used=("mcp__arch__design_style",),
                builtin_writes=("frontend/src/pages/Fresh.tsx",),
            )
            (ui / "Fresh.tsx").write_text("export default function Fresh(){ return null }\n", encoding="utf-8")
            acc2 = mod.evaluate_step_acceptance(root, _module(), step, result2)
            self.assertTrue(acc2.ok, acc2.reason)

    def test_batch_test_failure_writes_repair_note_with_vitest_log(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sdir = root / ".arc" / "steps" / "REQ-1"
            sdir.mkdir(parents=True)
            step = next(s for s in mod.official_steps() if s.step_id == "batch_test")
            fake = mod.ValidationResult(
                ok=False,
                exit_code=1,
                cmd=["npx", "--yes", "vitest", "run"],
                log_tail="FAIL  src/counter.test.ts > increments\nExpected 2 got 1\n",
                reason="harness validation failed: exit=1 cmd=npx --yes vitest run",
                project_dir=str(root / "frontend"),
            )
            with mock.patch.object(mod, "run_module_validation", return_value=fake):
                acc = mod.evaluate_step_acceptance(
                    root, _module(), step, _result(mcp_tools_used=("mcp__arch__commit_gate",))
                )
            self.assertFalse(acc.ok)
            notes = " ".join(acc.soft_notes)
            self.assertIn("vitest_log:", notes)
            self.assertIn("Expected 2 got 1", notes)
            repair = sdir / "repair_note.txt"
            self.assertTrue(repair.is_file())
            body = repair.read_text(encoding="utf-8")
            self.assertIn("VITEST OUTPUT", body)
            self.assertIn("Expected 2 got 1", body)
            bv = sdir / "batch_validation.json"
            self.assertTrue(bv.is_file())
            payload = json.loads(bv.read_text(encoding="utf-8"))
            self.assertFalse(payload["ok"])
            self.assertIn("Expected 2 got 1", payload["log_tail"])

    def test_early_write_append_constant_present(self):
        self.assertIn("EARLY WRITE SKELETON", mod.IMPLEMENT_EARLY_WRITE_APPEND)
        self.assertIn("frontend/src", mod.IMPLEMENT_EARLY_WRITE_APPEND)

    def test_implement_prompt_mentions_early_write_and_repair(self):
        step = next(s for s in mod.official_steps() if s.step_id == "implement")
        prompt = mod.step_prompt(
            _module(),
            Path("/tmp/reqs"),
            Path("/tmp/skills"),
            [],
            "web",
            step,
            validation_failure="VITEST OUTPUT:\nFAIL foo",
            validation_repair=1,
        )
        self.assertIn("EARLY WRITE SKELETON", prompt)
        self.assertIn("VALIDATION REPAIR", prompt)
        self.assertIn("do NOT only call commit_gate", prompt)

    def test_batch_test_prompt_forbids_commit_gate_only(self):
        step = next(s for s in mod.official_steps() if s.step_id == "batch_test")
        prompt = mod.step_prompt(
            _module(),
            Path("/tmp/reqs"),
            Path("/tmp/skills"),
            [],
            "web",
            step,
        )
        self.assertIn("commit_gate-only thrash", prompt)
        self.assertIn("FIX failures", prompt)

    def test_validation_max_repairs_default_raised(self):
        # env helper should default to 4 now
        mod.os.environ.pop("ARC_VALIDATION_MAX_REPAIRS", None)
        self.assertEqual(mod.env_int("ARC_VALIDATION_MAX_REPAIRS", 4, minimum=0, maximum=10), 4)


if __name__ == "__main__":
    unittest.main()
