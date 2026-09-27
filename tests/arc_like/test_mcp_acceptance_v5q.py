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


def _result(*, skills=("architect",), mcp=(), returncode=0, writes=(), step_started_at=None):
    return mod.ClaudeRunResult(
        returncode=returncode,
        is_error=False,
        terminal_reason="",
        subtype="success",
        api_error_status=None,
        tail="",
        skills_loaded=tuple(skills),
        mcp_tools_used=tuple(mcp),
        builtin_writes=tuple(writes),
        step_started_at=step_started_at,
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
            # search_code alone without in-STEP write progress must fail (v5x G1)
            acc2 = mod.evaluate_step_acceptance(
                root, _module(), _step("implement"),
                _result(skills=("arcbench-checkpoint",), mcp=("mcp__arch__search_code",)),
            )
            self.assertFalse(acc2.ok)
            self.assertIn("write progress", acc2.reason.lower())
            # Write|Edit business path + search_code → pass
            acc3 = mod.evaluate_step_acceptance(
                root, _module(), _step("implement"),
                _result(
                    skills=("arcbench-checkpoint",),
                    mcp=("mcp__arch__search_code",),
                    writes=("frontend/src/app.ts",),
                ),
            )
            self.assertTrue(acc3.ok)

    def test_implement_leftover_pages_plus_search_code_fails(self):
        """G1: pages leftover files + search_code alone must NOT pass."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            page = root / "frontend" / "src" / "pages" / "Home.tsx"
            page.parent.mkdir(parents=True)
            page.write_text("export default function Home(){return <div/>}\n" + ("// pad\n" * 10), encoding="utf-8")
            acc = mod.evaluate_step_acceptance(
                root, _module(), _step("implement"),
                _result(skills=("arcbench-checkpoint",), mcp=("mcp__arch__search_code",)),
            )
            self.assertFalse(acc.ok)
            self.assertIn("write progress", acc.reason.lower())

    def test_implement_write_plus_search_code_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "frontend" / "src" / "app.ts"
            src.parent.mkdir(parents=True)
            src.write_text("export const x = 1;\n" + ("// pad\n" * 10), encoding="utf-8")
            acc = mod.evaluate_step_acceptance(
                root, _module(), _step("implement"),
                _result(
                    skills=("arcbench-checkpoint",),
                    mcp=("mcp__arch__search_code",),
                    writes=(str(src),),
                ),
            )
            self.assertTrue(acc.ok)

    def test_implement_json_files_written_mtime_gate(self):
        import time
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "backend" / "src" / "server.ts"
            src.parent.mkdir(parents=True)
            step_start = time.time()
            time.sleep(0.05)
            src.write_text("export const server = 1;\n" + ("// pad\n" * 10), encoding="utf-8")
            sdir = root / ".arc" / "steps" / "REQ-1"
            sdir.mkdir(parents=True)
            (sdir / "search_code.json").write_text('{"hits":["server"]}\n', encoding="utf-8")
            (sdir / "implement.json").write_text(
                json.dumps({"files_written": ["backend/src/server.ts"], "step_id": "implement"}),
                encoding="utf-8",
            )
            # Old mtime (before step_start) → fail
            import os
            os.utime(src, (step_start - 120, step_start - 120))
            acc_old = mod.evaluate_step_acceptance(
                root, _module(), _step("implement"),
                _result(skills=("arcbench-checkpoint",), mcp=(), step_started_at=step_start),
            )
            self.assertFalse(acc_old.ok)
            # Fresh mtime ≥ step_start → pass
            os.utime(src, None)
            acc_new = mod.evaluate_step_acceptance(
                root, _module(), _step("implement"),
                _result(skills=("arcbench-checkpoint",), mcp=(), step_started_at=step_start),
            )
            self.assertTrue(acc_new.ok)

    def test_implement_accepts_search_code_receipt_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "frontend" / "src" / "app.ts"
            src.parent.mkdir(parents=True)
            src.write_text("export const x = 1;\n" + ("// pad\n" * 10), encoding="utf-8")
            sdir = root / ".arc" / "steps" / "REQ-1"
            sdir.mkdir(parents=True)
            (sdir / "search_code.json").write_text('{"hits":["App"]}\n', encoding="utf-8")
            # receipt alone without write progress fails
            acc = mod.evaluate_step_acceptance(
                root, _module(), _step("implement"),
                _result(skills=("arcbench-checkpoint",), mcp=()),
            )
            self.assertFalse(acc.ok)
            acc2 = mod.evaluate_step_acceptance(
                root, _module(), _step("implement"),
                _result(skills=("arcbench-checkpoint",), mcp=(), writes=("frontend/src/app.ts",)),
            )
            self.assertTrue(acc2.ok)

    def test_implement_step_prompt_mentions_write_gate(self):
        prompt = mod.step_prompt(
            _module(), Path("/tmp/req"), None, [], "web", _step("implement")
        )
        self.assertIn("WRITE PROGRESS HARD GATE", prompt)
        self.assertIn("frontend/src", prompt)

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
            (spec_dir / "REQ-1.html").write_text(
                '<html><section data-req="REQ-1">' + ("x" * 80) + "</section></html>\n",
                encoding="utf-8",
            )
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
            (spec_dir / "REQ-1.html").write_text(
                '<html><section data-req="REQ-1">' + ("x" * 80) + "</section></html>\n",
                encoding="utf-8",
            )
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


    def test_no_skill_force_load_fail_closed_v5u(self):
        """v5u: missing Skill loads must NOT fail PRD/SPEC when artifacts+MCP ok."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prd = root / "PRD" / "x.md"
            prd.parent.mkdir(parents=True)
            prd.write_text("# prd\n", encoding="utf-8")
            acc = mod.evaluate_step_acceptance(
                root, _module(), _step("prd"),
                _result(skills=(), mcp=("mcp__arch__prd",)),
            )
            self.assertTrue(acc.ok, acc.reason)
            self.assertTrue(any(n.startswith("soft_missing_skills:") for n in acc.soft_notes))

            spec_dir = root / "SPEC" / "arcbench"
            spec_dir.mkdir(parents=True)
            (spec_dir / "REQ-1.html").write_text(
                '<html><section data-req="REQ-1">' + ("x" * 80) + "</section></html>\n",
                encoding="utf-8",
            )
            acc2 = mod.evaluate_step_acceptance(
                root, _module(), _step("spec"),
                _result(skills=(), mcp=("mcp__arch__spec_write",)),
            )
            self.assertTrue(acc2.ok, acc2.reason)

    def test_step_prompt_no_skill_force_load_v5u(self):
        prompt = mod.step_prompt(
            _module(),
            Path("/tmp/req"),
            None,
            [],
            "github",
            _step("prd"),
        )
        low = prompt.lower()
        self.assertNotIn("load required skill", low)
        self.assertNotIn("first tool action", low)
        self.assertNotIn("missing skill tool load", low)
        self.assertIn("not skill force-load", low)
        self.assertIn("do not read/bash/cat skill.md", low)
        self.assertIn("model-invoked", low)

    def test_audit_refactor_soft_passes_without_skill_v5u(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            acc = mod.evaluate_step_acceptance(
                root, _module(), _step("audit_refactor"),
                _result(skills=(), mcp=()),
            )
            self.assertTrue(acc.ok, acc.reason)



    def test_spec_stub_html_soft_not_fail_closed_when_leaves_v5v(self):
        """v5v MCP-first: incomplete data-req is soft; SPEC still passes with spec_write+HTML.
        Coverage is fail-closed on govern via spec_govern, not homemade trace/HTML matching.
        """
        module = mod.RequirementModule(
            index=1,
            total=1,
            node_id="REQ-MOD",
            name="Module",
            subtree={
                "id": "REQ-MOD",
                "name": "Module",
                "description": "parent",
                "children": [
                    {
                        "id": "REQ-A",
                        "name": "Add Item",
                        "accessible_name": "Add Item",
                        "role": "button",
                    },
                    {
                        "id": "REQ-B",
                        "name": "Remove Item",
                        "accessible_name": "Remove Item",
                        "role": "button",
                    },
                ],
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec_dir = root / "SPEC" / "arcbench"
            spec_dir.mkdir(parents=True)
            (spec_dir / "REQ-MOD.html").write_text(
                "<html>" + ("x" * 80) + "</html>\n", encoding="utf-8"
            )
            acc = mod.evaluate_step_acceptance(
                root, module, _step("spec"),
                _result(skills=("architect",), mcp=("mcp__arch__spec_write",)),
            )
            self.assertTrue(acc.ok, acc.reason)
            self.assertTrue(
                any(n.startswith("soft:spec_leaf_data_req_incomplete:") for n in acc.soft_notes),
                acc.soft_notes,
            )
            self.assertTrue(
                any("govern_via_spec_govern" in n for n in acc.soft_notes),
                acc.soft_notes,
            )

    def test_spec_prd_mcp_does_not_count_as_spec_write_v5v(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec_dir = root / "SPEC" / "arcbench"
            spec_dir.mkdir(parents=True)
            (spec_dir / "REQ-1.html").write_text(
                '<html><section data-req="REQ-1">' + ("x" * 80) + "</section></html>\n",
                encoding="utf-8",
            )
            acc = mod.evaluate_step_acceptance(
                root, _module(), _step("spec"),
                _result(skills=("architect",), mcp=("mcp__arch__prd",)),
            )
            self.assertFalse(acc.ok)
            self.assertIn("spec_write", acc.reason)

    def test_ensure_gsc_spec_seeds_leaf_sections_v5v(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            module = mod.RequirementModule(
                index=1,
                total=1,
                node_id="REQ-MOD",
                name="Module",
                subtree={
                    "id": "REQ-MOD",
                    "name": "Module",
                    "description": "parent",
                    "children": [
                        {
                            "id": "REQ-A",
                            "name": "Add Item",
                            "description": "Add button",
                            "accessible_name": "Add Item",
                            "role": "button",
                        },
                        {
                            "id": "REQ-B",
                            "name": "Remove Item",
                            "description": "Remove",
                        },
                    ],
                },
            )
            path = mod.ensure_gsc_spec(output, module)
            body = path.read_text(encoding="utf-8")
            self.assertIn('data-req="REQ-A"', body)
            self.assertIn('data-req="REQ-B"', body)
            self.assertIn("Add Item", body)
            self.assertGreater(path.stat().st_size, 50)
            self.assertFalse((output / "SPEC" / "arcbench" / "REQ-MOD.md").exists())

    def test_spec_prompt_mcp_write_derive_v5v(self):
        prompt = mod.step_prompt(
            _module(),
            Path("/tmp/req"),
            None,
            [],
            "github",
            _step("spec"),
        )
        low = prompt.lower()
        self.assertIn("prd", low)
        self.assertIn("spec_write", low)
        self.assertIn("atomic", low)
        self.assertIn("invent", low)
        self.assertIn("mcp is the spec write path", low)
        self.assertNotIn("spec_trace.json mapping", low)
        self.assertIn("spec_govern", low)

    def test_govern_prompt_coverage_via_mcp_v5v(self):
        prompt = mod.step_prompt(
            _module(),
            Path("/tmp/req"),
            None,
            [],
            "github",
            _step("govern"),
        )
        low = prompt.lower()
        self.assertIn("spec_govern", low)
        self.assertIn("prd_govern", low)
        self.assertIn("coverage", low)
        self.assertIn("subtree", low)
        self.assertIn("do not invent a homemade spec_trace", low)

    def test_govern_still_fail_closed_on_mcp_proof_v5v(self):
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
            self.assertTrue(acc2.ok, acc2.reason)

    def test_govern_receipt_alone_insufficient_v5w(self):
        """v5w hard audit: receipt JSON without matching in-session tool must fail."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sdir = root / ".arc" / "steps" / "REQ-1"
            sdir.mkdir(parents=True)
            (sdir / "prd_govern.json").write_text(
                '{"ok": true, "source": "forged-receipt"}\n', encoding="utf-8"
            )
            (sdir / "spec_govern.json").write_text(
                '{"ok": true, "source": "forged-receipt"}\n', encoding="utf-8"
            )
            acc = mod.evaluate_step_acceptance(
                root, _module(), _step("govern"),
                _result(skills=("architect",), mcp=()),
            )
            self.assertFalse(acc.ok, acc.reason)
            self.assertIn("prd_govern", acc.reason)
            self.assertIn("receipt alone insufficient", acc.reason)
            # One tool + both receipts still fails (need BOTH tools)
            acc_one = mod.evaluate_step_acceptance(
                root, _module(), _step("govern"),
                _result(skills=("architect",), mcp=("mcp__arch__prd_govern",)),
            )
            self.assertFalse(acc_one.ok, acc_one.reason)
            self.assertIn("spec_govern", acc_one.reason)
            self.assertIn("receipt alone insufficient", acc_one.reason)

    def test_govern_tools_plus_mirrored_receipts_pass_v5w(self):
        """Receipts OK as side mirrors when matching in-session tools ran."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sdir = root / ".arc" / "steps" / "REQ-1"
            sdir.mkdir(parents=True)
            (sdir / "prd_govern.json").write_text('{"mirrored": true}\n', encoding="utf-8")
            (sdir / "spec_govern.json").write_text('{"mirrored": true}\n', encoding="utf-8")
            acc = mod.evaluate_step_acceptance(
                root, _module(), _step("govern"),
                _result(
                    skills=("architect",),
                    mcp=("mcp__arch__prd_govern", "mcp__arch__spec_govern"),
                ),
            )
            self.assertTrue(acc.ok, acc.reason)
            self.assertTrue(any("prd_govern" in t for t in acc.mcp_required))
            self.assertTrue(any("spec_govern" in t for t in acc.mcp_required))

    def test_spec_file_only_rejected_v5w(self):
        """SPEC: HTML alone without spec_write must fail (no file-only single signal)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec_dir = root / "SPEC" / "arcbench"
            spec_dir.mkdir(parents=True)
            (spec_dir / "REQ-1.html").write_text(
                '<html><section data-req="REQ-1">' + ("x" * 80) + "</section></html>\n",
                encoding="utf-8",
            )
            acc = mod.evaluate_step_acceptance(
                root, _module(), _step("spec"),
                _result(skills=("architect",), mcp=()),
            )
            self.assertFalse(acc.ok)
            self.assertIn("spec_write", acc.reason)

    def test_govern_prompt_hard_audit_chain_v5w(self):
        prompt = mod.step_prompt(
            _module(),
            Path("/tmp/req"),
            None,
            [],
            "github",
            _step("govern"),
        )
        low = prompt.lower()
        self.assertIn("insufficient alone", low)
        self.assertIn("write + this govern chain", low)
        self.assertIn("hard audit", low)
        self.assertIn("single-signal", low)

    def test_spec_prompt_both_html_and_write_v5w(self):
        prompt = mod.step_prompt(
            _module(),
            Path("/tmp/req"),
            None,
            [],
            "github",
            _step("spec"),
        )
        low = prompt.lower()
        self.assertIn("both", low)
        self.assertIn("write + govern chain", low)
        self.assertIn("not file-only", low)


if __name__ == "__main__":
    unittest.main()
