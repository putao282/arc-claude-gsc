import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path



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
SPEC = importlib.util.spec_from_file_location("arc_submission_main_mcp", ROOT / "main.py")
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
assert SPEC.loader is not None
SPEC.loader.exec_module(mod)


class McpRestoreTests(unittest.TestCase):
    def test_mcp_enabled_by_default(self):
        old = mod.os.environ.pop("ARC_ENABLE_MCP", None)
        try:
            self.assertTrue(mod.mcp_enabled())
        finally:
            if old is None:
                mod.os.environ.pop("ARC_ENABLE_MCP", None)
            else:
                mod.os.environ["ARC_ENABLE_MCP"] = old

    def test_mcp_can_be_disabled_explicitly(self):
        old = mod.os.environ.get("ARC_ENABLE_MCP")
        try:
            mod.os.environ["ARC_ENABLE_MCP"] = "0"
            self.assertFalse(mod.mcp_enabled())
            mod.os.environ["ARC_ENABLE_MCP"] = "false"
            self.assertFalse(mod.mcp_enabled())
            mod.os.environ["ARC_ENABLE_MCP"] = "1"
            self.assertTrue(mod.mcp_enabled())
        finally:
            if old is None:
                mod.os.environ.pop("ARC_ENABLE_MCP", None)
            else:
                mod.os.environ["ARC_ENABLE_MCP"] = old

    def test_write_gsc_mcp_config_points_at_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gsc = root / "gsc"
            bootstrap = gsc / "mcp" / "src" / "bootstrap.mjs"
            bootstrap.parent.mkdir(parents=True)
            bootstrap.write_text("// bootstrap\n", encoding="utf-8")
            (gsc / "bin").mkdir(parents=True)
            (gsc / "bin" / "gsc-spec-server").write_text("x", encoding="utf-8")
            config_path = mod.write_gsc_mcp_config(gsc, root / "mcp")
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            server = payload["mcpServers"]["arch"]
            self.assertEqual(server["command"], "node")
            self.assertEqual(server["args"], [str(bootstrap)])
            self.assertEqual(server["env"]["CLAUDE_PLUGIN_ROOT"], str(gsc))
            self.assertIn("allowedTools", server)
            self.assertIn("spec_read", server["allowedTools"])
            self.assertIn("spec_write", server["allowedTools"])
            self.assertLessEqual(len(server["allowedTools"]), 20)

    def test_claude_mcp_cli_args_default_enables_gsc(self):
        cfg = Path("/tmp/gsc-mcp.json")
        args = mod.claude_mcp_cli_args(enabled=True, mcp_config=cfg)
        self.assertEqual(args, ["--mcp-config", str(cfg), "--strict-mcp-config"])

    def test_claude_mcp_cli_args_escape_hatch_disables_without_prompt_ban(self):
        args = mod.claude_mcp_cli_args(enabled=False, mcp_config=None)
        self.assertEqual(args, ["--strict-mcp-config"])
        self.assertNotIn("--mcp-config", args)

    def test_ensure_gsc_spec_writes_html_not_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            module = mod.RequirementModule(
                index=1,
                total=1,
                node_id="REQ-1",
                name="Demo",
                subtree={"id": "REQ-1", "name": "Demo", "description": "hello <world>"},
            )
            # Leave a stale MD scaffold that must be removed.
            stale = output / "SPEC" / "arcbench" / "REQ-1.md"
            stale.parent.mkdir(parents=True)
            stale.write_text("# stale\n", encoding="utf-8")
            path = mod.ensure_gsc_spec(output, module)
            self.assertEqual(path.suffix, ".html")
            self.assertTrue(path.is_file())
            self.assertFalse(stale.exists())
            body = path.read_text(encoding="utf-8")
            self.assertIn("data-spec-root", body)
            self.assertIn("hello &lt;world&gt;", body)

    def test_prompt_encourages_gsc_mcp_without_hard_ban(self):
        module = mod.RequirementModule(1, 1, "REQ-1", "Demo", {"id": "REQ-1", "name": "Demo"})
        prompt = mod.module_prompt(module, Path("/tmp/reqs"), None, [], "web")
        self.assertIn("GSC MCP", prompt)
        self.assertNotIn("NEVER use MCP", prompt)
        self.assertNotIn("NEVER call WaitForMcpServers", prompt)
        self.assertNotIn("ANTI-THRASH", prompt)


if __name__ == "__main__":
    unittest.main()
