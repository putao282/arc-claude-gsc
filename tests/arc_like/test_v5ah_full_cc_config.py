# -*- coding: utf-8 -*-
"""v5ah: full MCP+Skills perception + Tao contest user CLAUDE.md on every CC launch."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


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
SPEC = importlib.util.spec_from_file_location("arc_main_v5ah", ROOT / "main.py")
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
assert SPEC.loader is not None
SPEC.loader.exec_module(mod)

# Also load sdk_driver constants without requiring real ClaudeAgentOptions.
SDK_SPEC = importlib.util.spec_from_file_location("arc_sdk_v5ah", ROOT / "sdk_driver.py")
sdk = importlib.util.module_from_spec(SDK_SPEC)
sys.modules[SDK_SPEC.name] = sdk
assert SDK_SPEC.loader is not None
SDK_SPEC.loader.exec_module(sdk)


class V5ahFullCcConfigTests(unittest.TestCase):
    def test_contest_claude_md_asset_headings(self):
        path = ROOT / "contest" / "CLAUDE.md"
        self.assertTrue(path.is_file(), f"missing {path}")
        body = path.read_text(encoding="utf-8")
        self.assertIn("# 角色", body)
        self.assertIn("§1 故障根治 SOP", body)
        self.assertIn("技能索引", body)
        loaded = mod.load_contest_user_claude_md()
        self.assertEqual(loaded, body)

    def test_install_writes_user_and_project_claude_md(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = root / "project"
            home = root / "home"
            paths = mod.install_contest_claude_md(out, home_dir=home)
            user = home / ".claude" / "CLAUDE.md"
            project = out / "CLAUDE.md"
            self.assertTrue(user.is_file())
            self.assertTrue(project.is_file())
            self.assertEqual(paths["user_claude_md"], str(user))
            self.assertEqual(paths["project_claude_md"], str(project))
            user_body = user.read_text(encoding="utf-8")
            project_body = project.read_text(encoding="utf-8")
            self.assertIn("# 角色", user_body)
            self.assertIn("§1 故障根治 SOP", user_body)
            self.assertIn("技能索引", user_body)
            self.assertIn("# 角色", project_body)
            self.assertIn("MCP stays ON", project_body)
            self.assertIn("Skill tool only", project_body)
            # No harness STEP theater reintroduced
            self.assertNotIn("5. **pages**", project_body)
            self.assertNotIn("thin CC orchestrator (v5ag)", project_body)

    def test_softener_installs_tao_not_step_theater(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = root / "wt"
            home = root / "home"
            mod.ensure_arc_spawn_gate_softener(out, home_dir=home)
            body = (out / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn("# 角色", body)
            self.assertIn("MCP stays ON", body)
            self.assertTrue((out / ".claude" / "spawn-gate-off").is_file())
            self.assertTrue((home / ".claude" / "CLAUDE.md").is_file())

    def test_sdk_defaults_skills_and_setting_sources(self):
        self.assertEqual(sdk.DEFAULT_SKILLS, "all")
        self.assertEqual(sdk.DEFAULT_SETTING_SOURCES, ["user", "project"])
        self.assertIn("Skill", sdk.DEFAULT_ALLOWED_TOOLS)

    def test_assert_cc_full_config_ok_when_wired(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = root / "project"
            home = root / "home"
            mod.install_contest_claude_md(out, home_dir=home)
            options = SimpleNamespace(
                setting_sources=["user", "project"],
                skills="all",
                allowed_tools=list(sdk.DEFAULT_ALLOWED_TOOLS),
            )
            cfg = mod.assert_cc_full_config(
                options=options,
                enable_mcp=True,
                plugins=[{"type": "local", "path": "/tmp/gsc"}],
                home_dir=home,
                output_dir=out,
                mcp_servers={"arch": {"command": "node"}},
            )
            self.assertTrue(cfg["ok"], cfg)
            self.assertEqual(cfg["n_mcp_disallowed_prepared_leak"], 0)
            self.assertTrue(cfg["Skill_in_allowed"])
            self.assertEqual(cfg["skills"], "all")

    def test_assert_cc_full_config_fail_closed_missing_skills(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = root / "project"
            home = root / "home"
            mod.install_contest_claude_md(out, home_dir=home)
            options = SimpleNamespace(
                setting_sources=["user", "project"],
                skills=None,
                allowed_tools=["Read", "Write"],  # no Skill
            )
            cfg = mod.assert_cc_full_config(
                options=options,
                enable_mcp=True,
                plugins=[{"type": "local", "path": "/tmp/gsc"}],
                home_dir=home,
                output_dir=out,
                mcp_servers={"arch": {}},
            )
            self.assertFalse(cfg["ok"])
            self.assertIn("skills_not_all", cfg["issues"])
            self.assertIn("Skill_not_in_allowed_tools", cfg["issues"])

    def test_prepared_mcp_tools_not_disallowed(self):
        denied = set(mod.gsc_mcp_disallowed_tool_names(prefixed=False))
        for name in ("prd", "spec_write", "search_code", "design_asset"):
            self.assertNotIn(name, denied, f"{name} must stay perceptible")
        self.assertIn("account_manage", denied)
        self.assertIn("debug_binary", denied)
        self.assertEqual(mod.n_mcp_disallowed_prepared_leak(), 0)
        # Even with legacy degrade override, prepared tools must not leak into deny
        leak = mod.n_mcp_disallowed_prepared_leak(
            allow_override=list(mod.IMPLEMENT_DEGRADED_MCP_ALLOW)
        )
        self.assertEqual(leak, 0)
        denied_deg = set(
            mod.gsc_mcp_disallowed_tool_names(
                prefixed=False, allow_override=list(mod.IMPLEMENT_DEGRADED_MCP_ALLOW)
            )
        )
        for name in ("prd", "spec_write", "search_code", "design_asset"):
            self.assertNotIn(name, denied_deg)


if __name__ == "__main__":
    unittest.main()
