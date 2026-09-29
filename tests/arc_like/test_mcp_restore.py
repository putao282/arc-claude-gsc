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
            self.assertIn("prd", server["allowedTools"])
            self.assertIn("prd_govern", server["allowedTools"])
            self.assertIn("search_code", server["allowedTools"])
            self.assertIn("design_style", server["allowedTools"])
            self.assertNotIn("account_manage", server["allowedTools"])
            self.assertNotIn("debug_binary", server["allowedTools"])
            # §5.1-A+B ≈ 38 tools; leave headroom but stay well under full 70.
            self.assertGreaterEqual(len(server["allowedTools"]), 30)
            self.assertLessEqual(len(server["allowedTools"]), 45)
            meta = (root / "mcp" / "gsc-mcp-allowlist.json")
            self.assertTrue(meta.is_file())
            meta_payload = json.loads(meta.read_text(encoding="utf-8"))
            self.assertEqual(meta_payload["n_inventory"], 70)
            self.assertIn("account_manage", meta_payload["disallowed_short"])
            self.assertIn("debug_binary", meta_payload["disallowed_short"])

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
        # v5r: ANTI-THRASH guidance is allowed (limits identical re-reads; MCP stays ON)
        self.assertIn("anti-thrash", prompt.lower())
        self.assertNotIn("disable MCP", prompt.lower())


    def test_default_allowlist_is_a_plus_b_without_never_default(self):
        old = mod.os.environ.pop("ARC_MCP_ALLOWED_TOOLS", None)
        old_p2 = mod.os.environ.pop("ARC_MCP_P2_TOOLS", None)
        try:
            tools = mod.gsc_mcp_allowed_tools()
            self.assertIn("prd_govern", tools)
            self.assertIn("search_code", tools)
            self.assertIn("design_asset", tools)
            self.assertIn("kb_query", tools)
            self.assertIn("navigate", tools)
            self.assertNotIn("account_manage", tools)
            self.assertNotIn("debug_binary", tools)
            self.assertEqual(len(mod.GSC_MCP_INVENTORY_SHORT_NAMES), 70)
            denied = mod.gsc_mcp_disallowed_tool_names(prefixed=True)
            self.assertTrue(all(t.startswith("mcp__arch__") for t in denied))
            self.assertIn("mcp__arch__account_manage", denied)
            self.assertIn("mcp__arch__debug_binary", denied)
            # allow + deny covers inventory (never-default may appear only in deny)
            short_denied = mod.gsc_mcp_disallowed_tool_names(prefixed=False)
            self.assertEqual(set(tools) | set(short_denied), set(mod.GSC_MCP_INVENTORY_SHORT_NAMES))
            self.assertFalse(set(tools) & set(short_denied))
            csv = mod.claude_disallowed_tools_csv()
            self.assertIn("Agent", csv)
            self.assertIn("mcp__arch__pipeline", csv)
        finally:
            if old is None:
                mod.os.environ.pop("ARC_MCP_ALLOWED_TOOLS", None)
            else:
                mod.os.environ["ARC_MCP_ALLOWED_TOOLS"] = old
            if old_p2 is None:
                mod.os.environ.pop("ARC_MCP_P2_TOOLS", None)
            else:
                mod.os.environ["ARC_MCP_P2_TOOLS"] = old_p2

    def test_audit_steps_default_off_and_order_v5ag(self):
        old = mod.os.environ.pop("ARC_ENABLE_MCP_AUDIT_STEPS", None)
        try:
            self.assertFalse(mod.mcp_audit_steps_enabled())
            ids = [s.step_id for s in mod.official_steps()]
            self.assertEqual(
                ids,
                ["prd", "spec", "test_dag", "implement", "batch_test"],
            )
            self.assertNotIn("pages", ids)
            mod.os.environ["ARC_ENABLE_MCP_AUDIT_STEPS"] = "1"
            self.assertTrue(mod.mcp_audit_steps_enabled())
            self.assertEqual(
                [s.step_id for s in mod.official_steps()],
                ["prd", "spec", "govern", "test_dag", "implement", "audit_refactor", "batch_test"],
            )
        finally:
            if old is None:
                mod.os.environ.pop("ARC_ENABLE_MCP_AUDIT_STEPS", None)
            else:
                mod.os.environ["ARC_ENABLE_MCP_AUDIT_STEPS"] = old

    def test_step_prompt_names_required_mcp_tools(self):
        module = mod.RequirementModule(1, 1, "REQ-1", "Demo", {"id": "REQ-1", "name": "Demo"})
        for step in mod.official_steps():
            prompt = mod.step_prompt(module, Path("/tmp/reqs"), None, [], "web", step)
            self.assertIn("mcp__arch__", prompt)
            self.assertNotIn("NEVER use MCP", prompt)
            if step.step_id == "implement":
                self.assertIn("CODING + TEST LOOP", prompt)
                self.assertNotIn("FORBIDDEN this STEP: do not run vitest", prompt)
            if step.step_id == "govern":
                self.assertIn("prd_govern", prompt)
                self.assertIn("spec_govern", prompt)
            if step.step_id == "spec":
                self.assertIn("migrate", prompt.lower())



if __name__ == "__main__":
    unittest.main()
