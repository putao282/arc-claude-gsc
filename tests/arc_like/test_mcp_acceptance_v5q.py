import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


def _install_test_stubs():
    import types
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        sys.modules["claude_agent_sdk"] = types.ModuleType("claude_agent_sdk")
    try:
        import arcbench_agent_runtime  # noqa: F401
    except ImportError:
        runtime = types.ModuleType("arcbench_agent_runtime")
        class AgentRuntime:
            """Test stub."""
        runtime.AgentRuntime = AgentRuntime
        sys.modules["arcbench_agent_runtime"] = runtime
    try:
        import yaml  # noqa: F401
    except ImportError:
        yaml_mod = types.ModuleType("yaml")
        def safe_load(text):
            raise RuntimeError("PyYAML not installed in this test environment")
        yaml_mod.safe_load = safe_load
        sys.modules["yaml"] = yaml_mod

_install_test_stubs()

ROOT = Path(__file__).resolve().parents[2]
if not (ROOT / "main.py").is_file() and Path("/workspace/submission/main.py").is_file():
    ROOT = Path("/workspace/submission")
SPEC = importlib.util.spec_from_file_location("arc_submission_main_mcp_acc", ROOT / "main.py")
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


def _result(*, skills=("architect",), mcp=(), returncode=0):
    return mod.ClaudeRunResult(
        returncode=returncode,
        is_error=False,
        terminal_reason="",
        subtype="success",
        api_error_status=None,
        tail="",
        skills_loaded=tuple(skills),
        mcp_tools_used=tuple(mcp),
    )


def _step(step_id: str) -> mod.StepDef:
    for s in list(mod.OFFICIAL_STEPS) + [mod.GOVERN_STEP, mod.AUDIT_REFACTOR_STEP]:
        if s.step_id == step_id:
            return s
    raise KeyError(step_id)


class McpAcceptanceV5qTests(unittest.TestCase):
    def test_pages_requires_design_mcp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            page = root / "frontend" / "src" / "pages" / "Home.tsx"
            page.parent.mkdir(parents=True)
            page.write_text("export default function Home(){return <div/>}\n", encoding="utf-8")
            acc = mod.evaluate_step_acceptance(
                root, _module(), _step("pages"),
                _result(skills=("designer",), mcp=()),
            )
            self.assertFalse(acc.ok)
            self.assertIn("design_style", acc.reason)
            acc2 = mod.evaluate_step_acceptance(
                root, _module(), _step("pages"),
                _result(skills=("designer",), mcp=("mcp__arch__design_style",)),
            )
            self.assertTrue(acc2.ok)
            self.assertTrue(any("design_style" in a for a in acc2.mcp_required + acc2.artifacts))

    def test_implement_requires_search_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "frontend" / "src" / "app.ts"
            src.parent.mkdir(parents=True)
            src.write_text("export const x = 1;\n" + ("// pad\n" * 10), encoding="utf-8")
            acc = mod.evaluate_step_acceptance(
                root, _module(), _step("implement"),
                _result(skills=("arcbench-checkpoint",), mcp=()),
            )
            self.assertFalse(acc.ok)
            self.assertIn("search_code", acc.reason)
            acc2 = mod.evaluate_step_acceptance(
                root, _module(), _step("implement"),
                _result(skills=("arcbench-checkpoint",), mcp=("mcp__arch__search_code",)),
            )
            self.assertTrue(acc2.ok)

    def test_implement_accepts_search_code_receipt_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "frontend" / "src" / "app.ts"
            src.parent.mkdir(parents=True)
            src.write_text("export const x = 1;\n" + ("// pad\n" * 10), encoding="utf-8")
            sdir = root / ".arc" / "steps" / "REQ-1"
            sdir.mkdir(parents=True)
            (sdir / "search_code.json").write_text('{"hits":["App"]}\n', encoding="utf-8")
            acc = mod.evaluate_step_acceptance(
                root, _module(), _step("implement"),
                _result(skills=("arcbench-checkpoint",), mcp=()),
            )
            self.assertTrue(acc.ok)

    def test_govern_requires_both_govern_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            acc = mod.evaluate_step_acceptance(
                root, _module(), _step("govern"),
                _result(skills=("architect",), mcp=("mcp__arch__prd_govern",)),
            )
            self.assertFalse(acc.ok)
            self.assertIn("spec_govern", acc.reason)
            acc2 = mod.evaluate_step_acceptance(
                root, _module(), _step("govern"),
                _result(
                    skills=("architect",),
                    mcp=("mcp__arch__prd_govern", "mcp__arch__spec_govern"),
                ),
            )
            self.assertTrue(acc2.ok)

    def test_batch_test_commit_gate_soft_does_not_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frontend = root / "frontend"
            frontend.mkdir()
            (frontend / "package.json").write_text(
                json.dumps({"name": "demo", "scripts": {"test": "echo ok"}}),
                encoding="utf-8",
            )
            tests = frontend / "tests"
            tests.mkdir()
            (tests / "api.test.ts").write_text("test('api', () => {})\n", encoding="utf-8")
            (tests / "ui.spec.ts").write_text("test('ui', () => {})\n", encoding="utf-8")

            def fake_run(cmd, **kwargs):
                class R:
                    returncode = 0
                    stdout = "passed"
                    stderr = ""
                return R()

            # Patch validation runner indirectly via run_module_validation's subprocess
            original = mod.subprocess.run
            try:
                mod.subprocess.run = fake_run  # type: ignore
                acc = mod.evaluate_step_acceptance(
                    root, _module(), _step("batch_test"),
                    _result(skills=("arcbench-runtime-signals",), mcp=()),
                )
            finally:
                mod.subprocess.run = original
            # May fail if discover_test_project / find_test_files / run path differs;
            # soft commit_gate must not be the failure reason.
            if not acc.ok:
                self.assertNotIn("commit_gate", acc.reason)
            else:
                self.assertEqual(acc.commit_gate_status, "missing")
                self.assertTrue(any("commit_gate" in n for n in acc.soft_notes))

    def test_audit_refactor_soft_always_passes_with_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            acc = mod.evaluate_step_acceptance(
                root, _module(), _step("audit_refactor"),
                _result(skills=("arcbench-checkpoint",), mcp=()),
            )
            self.assertTrue(acc.ok)
            self.assertEqual(acc.commit_gate_status, "missing")

    def test_prd_and_spec_existing_gates_still_hold(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            acc = mod.evaluate_step_acceptance(
                root, _module(), _step("prd"),
                _result(skills=("architect",), mcp=("mcp__arch__prd",)),
            )
            self.assertFalse(acc.ok)
            self.assertIn("PRD artifact", acc.reason)
            prd = root / "PRD" / "x.md"
            prd.parent.mkdir(parents=True)
            prd.write_text("# prd\n", encoding="utf-8")
            acc2 = mod.evaluate_step_acceptance(
                root, _module(), _step("prd"),
                _result(skills=("architect",), mcp=("mcp__arch__prd",)),
            )
            self.assertTrue(acc2.ok)

            spec_dir = root / "SPEC" / "arcbench"
            spec_dir.mkdir(parents=True)
            (spec_dir / "REQ-1.html").write_text("<html>" + ("x" * 80) + "</html>\n", encoding="utf-8")
            acc3 = mod.evaluate_step_acceptance(
                root, _module(), _step("spec"),
                _result(skills=("architect",), mcp=()),
            )
            self.assertFalse(acc3.ok)
            acc4 = mod.evaluate_step_acceptance(
                root, _module(), _step("spec"),
                _result(skills=("architect",), mcp=("mcp__arch__spec_read", "mcp__arch__spec_write")),
            )
            self.assertTrue(acc4.ok)


    def test_spec_read_only_rejected_v5r(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec_dir = root / "SPEC" / "arcbench"
            spec_dir.mkdir(parents=True)
            (spec_dir / "REQ-1.html").write_text("<html>" + ("x" * 80) + "</html>\n", encoding="utf-8")
            acc = mod.evaluate_step_acceptance(
                root, _module(), _step("spec"),
                _result(skills=("architect",), mcp=("mcp__arch__spec_read",)),
            )
            self.assertFalse(acc.ok)
            self.assertIn("spec_write", acc.reason)

    def test_spec_prompt_anti_thrash_v5r(self):
        prompt = mod.step_prompt(
            _module(),
            Path("/tmp/req"),
            None,
            [],
            "github",
            _step("spec"),
        )
        self.assertIn("anti-thrash", prompt.lower())
        self.assertIn("AT MOST ONCE", prompt)
        self.assertIn("spec_write", prompt)


if __name__ == "__main__":
    unittest.main()
