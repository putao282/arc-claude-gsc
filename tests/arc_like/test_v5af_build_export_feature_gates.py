# -*- coding: utf-8 -*-
"""v5af: npm build hard gate + page default-export + soft-accept off + feature wiring."""
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


def _seed_frontend(root: Path, *, with_default: bool = True, page_body: str | None = None):
    app = root / "frontend" / "src" / "App.tsx"
    page = root / "frontend" / "src" / "pages" / "HomePage.tsx"
    page.parent.mkdir(parents=True, exist_ok=True)
    app.parent.mkdir(parents=True, exist_ok=True)
    app.write_text(
        "import HomePage from './pages/HomePage';\n"
        "export default function App(){ return <HomePage />; }\n",
        encoding="utf-8",
    )
    if page_body is None:
        page_body = (
            "function HomePage(){\n"
            "  return (\n"
            "    <main className=\"min-h-screen\">\n"
            "      <h1>Home Dashboard Feature</h1>\n"
            "      <p>Welcome to the curated home experience.</p>\n"
            "    </main>\n"
            "  );\n"
            "}\n"
        )
        if with_default:
            page_body += "export default HomePage;\n"
    page.write_text(page_body, encoding="utf-8")
    pkg = root / "frontend" / "package.json"
    pkg.write_text(
        json.dumps(
            {
                "name": "frontend",
                "scripts": {"test": "vitest run", "build": "vite build"},
            }
        ),
        encoding="utf-8",
    )
    return app, page


class V5afBuildExportFeatureGates(unittest.TestCase):
    def test_check_app_page_default_exports_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed_frontend(root, with_default=False)
            ok, reason, _ = mod.check_app_page_default_exports(root)
            self.assertFalse(ok)
            self.assertIn("missing_default_export", reason)

    def test_check_app_page_default_exports_ok(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed_frontend(root, with_default=True)
            ok, reason, checked = mod.check_app_page_default_exports(root)
            self.assertTrue(ok)
            self.assertTrue(checked)

    def test_check_pages_not_stub_rejects_empty_shell(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _seed_frontend(
                root,
                with_default=True,
                page_body="export default function HomePage(){return <main/>}\n",
            )
            ok, reason, _ = mod.check_pages_not_stub(root)
            self.assertFalse(ok)
            self.assertIn("stub", reason)

    def test_run_project_build_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fe = root / "frontend"
            fe.mkdir()
            (fe / "package.json").write_text(
                json.dumps({"scripts": {"build": "vite build"}}), encoding="utf-8"
            )

            def fake_run(cmd, **kwargs):
                return types.SimpleNamespace(returncode=1, stdout="", stderr="build boom")

            result = mod.run_project_build(fe, run_fn=fake_run)
            self.assertFalse(result.ok)
            self.assertIn("build failed", result.reason)

    def test_run_module_validation_runs_build_after_vitest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fe = root / "frontend"
            fe.mkdir()
            (fe / "package.json").write_text(
                json.dumps({"scripts": {"test": "vitest run", "build": "vite build"}}),
                encoding="utf-8",
            )
            test_file = fe / "demo.test.js"
            test_file.write_text("test('x', () => {})", encoding="utf-8")
            calls = []

            def fake_run(cmd, **kwargs):
                calls.append(list(cmd))
                # vitest ok, build fail
                if cmd[:2] == ["npm", "run"] and cmd[2:] == ["build"]:
                    return types.SimpleNamespace(returncode=1, stdout="", stderr="TS2305")
                return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

            m = _module()
            result = mod.run_module_validation(root, m, run_fn=fake_run)
            self.assertFalse(result.ok)
            self.assertIn("build hard gate", result.reason)
            self.assertTrue(any(c[:3] == ["npm", "run", "build"] for c in calls))
            self.assertTrue(any("vitest" in " ".join(c).lower() or "test" in " ".join(c).lower() for c in calls))

    def test_run_module_validation_pass_includes_build(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fe = root / "frontend"
            fe.mkdir()
            (fe / "package.json").write_text(
                json.dumps({"scripts": {"test": "vitest run", "build": "vite build"}}),
                encoding="utf-8",
            )
            (fe / "demo.test.js").write_text("test('x', () => {})", encoding="utf-8")

            def fake_run(cmd, **kwargs):
                return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

            result = mod.run_module_validation(root, _module(), run_fn=fake_run)
            self.assertTrue(result.ok)
            self.assertIn("validation+build", result.reason)

    def test_feature_wiring_requires_source_hits(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sdir = root / ".arc" / "steps" / "REQ-1"
            sdir.mkdir(parents=True)
            (sdir / "test_dag.json").write_text(
                json.dumps(
                    {
                        "api": [{"path": "/api/sheets/rows", "method": "GET"}],
                        "ui": [{"page": "SheetEditor", "path": "/sheets"}],
                    }
                ),
                encoding="utf-8",
            )
            # empty source → fail
            ok, reason = mod.check_test_dag_feature_wiring(root, sdir)
            self.assertFalse(ok)

            # wire real handlers/pages
            api = root / "backend" / "src" / "routes.ts"
            api.parent.mkdir(parents=True)
            api.write_text(
                "export function sheetsRows(){ return fetch('/api/sheets/rows'); }\n",
                encoding="utf-8",
            )
            page = root / "frontend" / "src" / "pages" / "SheetEditor.tsx"
            page.parent.mkdir(parents=True)
            page.write_text(
                "export default function SheetEditor(){ return <div>/sheets SheetEditor</div>; }\n",
                encoding="utf-8",
            )
            ok2, reason2 = mod.check_test_dag_feature_wiring(root, sdir)
            self.assertTrue(ok2, reason2)
            self.assertIn("feature_wiring_ok", reason2)

    def test_pages_step_gone_helpers_remain_as_debt(self):
        """v5ag: pages STEP deleted; helper scanners may remain as unused debt."""
        self.assertNotIn("pages", [s.step_id for s in mod.official_steps()])
        self.assertTrue(hasattr(mod, "check_app_page_default_exports"))
        self.assertTrue(hasattr(mod, "check_pages_not_stub"))

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

    def test_policy_tags_include_v5ag(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        for tag in (
            "v5af_npm_build_hard_gate_after_vitest",
            "v5ag_thin_cc_orchestrator",
            "v5ag_no_pages_step",
            "v5ag_mcp_audit_default_off",
            "v5ag_implement_code_test_loop",
            "v5ae_govern_green_force_stop_no_supervisor_fail_closed",
            "v5ad_wave_central_one_shot_batch_test",
            "v5aa_merge_domain_worktree_abort_theirs",
        ):
            self.assertIn(tag, src)


if __name__ == "__main__":
    unittest.main()
