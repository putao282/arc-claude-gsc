import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock



def _install_test_stubs():
    """Allow loading main.py when ARC/Claude runtime wheels are absent."""
    import sys
    import types
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        sys.modules["claude_agent_sdk"] = types.ModuleType("claude_agent_sdk")
    try:
        import arcbench_agent_runtime  # noqa: F401
    except ImportError:
        runtime = types.ModuleType("arcbench_agent_runtime")
        class AgentRuntime:  # noqa: D401
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
SPEC = importlib.util.spec_from_file_location("arc_submission_main_false_green", ROOT / "main.py")
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


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FalseGreenGateTests(unittest.TestCase):
    def tearDown(self):
        for key in (
            "ARC_FORCE_REVALIDATE",
            "ARC_VALIDATION_MAX_REPAIRS",
            "ARC_VALIDATION_TIMEOUT_SECONDS",
        ):
            mod.os.environ.pop(key, None)

    def test_claude_success_alone_is_not_pass_decision(self):
        """Gate never passes without a successful ValidationResult."""
        failed = mod.ValidationResult(
            ok=False,
            exit_code=1,
            cmd=["npm", "test"],
            log_tail="FAIL",
            reason="harness validation failed: exit=1",
            project_dir="/tmp/frontend",
        )
        decision = mod.decide_validation_gate(failed, validation_repair=0, max_validation_repairs=2)
        self.assertNotEqual(decision.action, "pass")
        self.assertEqual(decision.action, "repair")
        self.assertIn("non-retryable:validation", decision.reason)

    def test_validation_pass_yields_pass_decision(self):
        ok = mod.ValidationResult(
            ok=True,
            exit_code=0,
            cmd=["npx", "--yes", "vitest", "run"],
            log_tail="passed",
            reason="harness local validation passed",
            project_dir="/tmp/frontend",
        )
        decision = mod.decide_validation_gate(ok, validation_repair=0, max_validation_repairs=2)
        self.assertEqual(decision.action, "pass")

    def test_validation_fail_then_exhaust_repairs(self):
        failed = mod.ValidationResult(
            ok=False,
            exit_code=1,
            cmd=["npm", "test"],
            log_tail="boom",
            reason="harness validation failed",
        )
        mid = mod.decide_validation_gate(failed, validation_repair=0, max_validation_repairs=2)
        self.assertEqual(mid.action, "repair")
        last = mod.decide_validation_gate(failed, validation_repair=2, max_validation_repairs=2)
        self.assertEqual(last.action, "fail")
        self.assertIn("exhausted", last.reason)

    def test_validation_failure_not_api_retryable(self):
        """Validation is a separate loop; classify_claude_failure stays for Claude only."""
        # A successful Claude result must still be 'success' even if we later validate-fail.
        claude_ok = mod.ClaudeRunResult(
            returncode=0,
            is_error=False,
            terminal_reason="",
            subtype="success",
            api_error_status=None,
            tail="",
        )
        self.assertEqual(mod.classify_claude_failure(claude_ok).reason, "success")
        self.assertFalse(mod.classify_claude_failure(claude_ok).retryable)
        decision = mod.decide_validation_gate(
            mod.ValidationResult(False, 1, ["npm", "test"], "x", "nope"),
            validation_repair=0,
            max_validation_repairs=2,
        )
        self.assertTrue(decision.reason.startswith("non-retryable:validation"))

    def test_run_module_validation_fails_without_test_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frontend = root / "frontend"
            frontend.mkdir()
            (frontend / "package.json").write_text(
                json.dumps({"name": "demo", "scripts": {"test": "vitest run"}}),
                encoding="utf-8",
            )
            result = mod.run_module_validation(root, _module(), run_fn=lambda *a, **k: self.fail("must not run"))
            self.assertFalse(result.ok)
            self.assertIn("no test files", result.reason)

    def test_run_module_validation_fails_without_package_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = mod.run_module_validation(Path(tmp), _module(), run_fn=lambda *a, **k: self.fail("no"))
            self.assertFalse(result.ok)
            self.assertIn("no package.json", result.reason)

    def test_run_module_validation_ok_when_command_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frontend = root / "frontend"
            tests = frontend / "tests"
            tests.mkdir(parents=True)
            (frontend / "package.json").write_text(
                json.dumps({"name": "demo", "scripts": {"test": "vitest run"}}),
                encoding="utf-8",
            )
            (tests / "demo.test.ts").write_text("export {}", encoding="utf-8")

            calls = []

            def fake_run(cmd, **kwargs):
                calls.append((cmd, kwargs.get("cwd")))
                return FakeCompleted(0, stdout="ok\n", stderr="")

            result = mod.run_module_validation(root, _module(), run_fn=fake_run)
            self.assertTrue(result.ok)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(calls[0][0], ["npx", "--yes", "vitest", "run"])
            self.assertEqual(Path(calls[0][1]), frontend)

    def test_run_module_validation_fail_when_command_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frontend = root / "frontend"
            frontend.mkdir()
            (frontend / "package.json").write_text(
                json.dumps({"name": "demo", "scripts": {"test": "vitest run"}}),
                encoding="utf-8",
            )
            (frontend / "a.test.ts").write_text("x", encoding="utf-8")

            def fake_run(cmd, **kwargs):
                return FakeCompleted(1, stdout="", stderr="AssertionError")

            result = mod.run_module_validation(root, _module(), run_fn=fake_run)
            self.assertFalse(result.ok)
            self.assertEqual(result.exit_code, 1)
            self.assertIn("AssertionError", result.log_tail)

    def test_receipt_required_for_module_already_passed(self):
        class Traceability:
            @staticmethod
            def get_node_state(node_id):
                return {"req_id": node_id, "state": "PASSED"}

        class Runtime:
            traceability = Traceability()

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            # PASSED but no receipt => do not skip (anti false-green freeze)
            self.assertFalse(mod.module_already_passed(Runtime(), "REQ-1", output))
            ok = mod.ValidationResult(
                True, 0, ["npx", "--yes", "vitest", "run"], "ok", "harness local validation passed", str(output)
            )
            mod.write_validation_receipt(output, "REQ-1", ok)
            self.assertTrue(mod.has_validation_receipt(output, "REQ-1"))
            self.assertTrue(mod.module_already_passed(Runtime(), "REQ-1", output))

            mod.os.environ["ARC_FORCE_REVALIDATE"] = "1"
            self.assertFalse(mod.module_already_passed(Runtime(), "REQ-1", output))

    def test_module_prompt_states_harness_bar_not_self_stop(self):
        prompt = mod.module_prompt(
            _module(),
            Path("/req"),
            None,
            [],
            "web",
        )
        self.assertIn("Harness bar", prompt)
        self.assertIn("do NOT grant", prompt)
        self.assertNotIn("one small vitest", prompt.lower())
        self.assertNotIn("STOP early", prompt)  # only allowed in repair wording below
        # baseline prompt should not encourage early STOP
        self.assertNotRegex(prompt, r"\bSTOP\b")

        repair = mod.module_prompt(
            _module(),
            Path("/req"),
            None,
            [],
            "web",
            validation_failure="FAIL LOG HERE",
            validation_repair=1,
        )
        self.assertIn("VALIDATION REPAIR", repair)
        self.assertIn("FAIL LOG HERE", repair)
        self.assertIn("does NOT grant harness green", repair)

    def test_mark_test_passed_requires_validation_in_orchestrated_gate(self):
        """Simulate post-Claude path: success alone must not green; validation ok does."""
        events = []

        class Events:
            def mark_implementation_done(self, node_id, msg):
                events.append(("implementation_done", node_id, msg))

            def mark_test_passed(self, node_id, msg):
                events.append(("test_passed", node_id, msg))

            def mark_test_failed(self, node_id, msg):
                events.append(("test_failed", node_id, msg))

            def mark_implementation_failed(self, node_id, msg):
                events.append(("implementation_failed", node_id, msg))

            def mark_run_failed(self, msg):
                events.append(("run_failed", msg))

            def mark_run_resumed(self, msg):
                events.append(("run_resumed", msg))

        class Git:
            def commit(self, msg):
                events.append(("commit", msg))

        class Runtime:
            events = Events()
            git = Git()

        module = _module()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            runtime = Runtime()
            completed = []

            # Path A: Claude would have succeeded, but validation fails and repairs exhausted.
            failed = mod.ValidationResult(
                False, 1, ["npm", "test"], "nope", "harness validation failed", str(output)
            )
            gate = mod.decide_validation_gate(failed, validation_repair=2, max_validation_repairs=2)
            self.assertEqual(gate.action, "fail")
            runtime.events.mark_test_failed(module.node_id, f"Harness validation failed: {failed.reason}")
            runtime.events.mark_implementation_failed(module.node_id, "exhausted")
            self.assertFalse(any(e[0] == "test_passed" for e in events))
            self.assertTrue(any(e[0] == "test_failed" for e in events))

            events.clear()
            # Path B: validation ok => green with honest message + receipt
            ok = mod.ValidationResult(
                True, 0, ["npx", "--yes", "vitest", "run"], "ok", "harness local validation passed", str(output)
            )
            gate = mod.decide_validation_gate(ok, validation_repair=0, max_validation_repairs=2)
            self.assertEqual(gate.action, "pass")
            mod.write_validation_receipt(output, module.node_id, ok)
            runtime.events.mark_implementation_done(module.node_id, "done")
            runtime.events.mark_test_passed(module.node_id, "Harness local validation passed")
            runtime.git.commit(f"{module.node_id}: {module.name}")
            completed.append(module.node_id)
            self.assertTrue(any(e[0] == "test_passed" and "Harness local validation passed" in e[2] for e in events))
            self.assertTrue(mod.has_validation_receipt(output, module.node_id))
            self.assertEqual(completed, ["REQ-1"])


if __name__ == "__main__":
    unittest.main()
