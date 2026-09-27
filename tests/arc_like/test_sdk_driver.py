"""Unit tests for official ClaudeSDKClient driver wiring (no live network)."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


def _install_stubs() -> None:
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        mod = types.ModuleType("claude_agent_sdk")

        class ClaudeAgentOptions:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class ClaudeSDKClient:
            def __init__(self, options=None):
                self.options = options

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def query(self, prompt):
                self._prompt = prompt

            async def receive_response(self):
                if False:
                    yield None

        class ResultMessage:
            pass

        class AssistantMessage:
            pass

        class SystemMessage:
            pass

        class ToolUseBlock:
            pass

        mod.ClaudeAgentOptions = ClaudeAgentOptions
        mod.ClaudeSDKClient = ClaudeSDKClient
        mod.ResultMessage = ResultMessage
        mod.AssistantMessage = AssistantMessage
        mod.SystemMessage = SystemMessage
        mod.ToolUseBlock = ToolUseBlock
        sys.modules["claude_agent_sdk"] = mod


_install_stubs()

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("sdk_driver_under_test", ROOT / "sdk_driver.py")
sdk = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sdk
assert SPEC.loader is not None
SPEC.loader.exec_module(sdk)


class ClaudeEnvMappingTests(unittest.TestCase):
    def test_official_openai_to_anthropic_mapping(self):
        os.environ["OPENAI_API_KEY"] = "sk-test-key"
        os.environ["OPENAI_BASE_URL"] = "https://api.arc-bench.com/v1"
        # Ensure prior Anthropic values get overwritten then restored.
        os.environ["ANTHROPIC_API_KEY"] = "stale"
        os.environ["ANTHROPIC_BASE_URL"] = "https://stale.example"
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "stale-token"
        with sdk.claude_env_from_openai_env():
            self.assertEqual(os.environ.get("ANTHROPIC_API_KEY"), "")
            self.assertEqual(os.environ.get("ANTHROPIC_BASE_URL"), "https://api.arc-bench.com/v1")
            self.assertEqual(os.environ.get("ANTHROPIC_AUTH_TOKEN"), "sk-test-key")
        self.assertEqual(os.environ.get("ANTHROPIC_API_KEY"), "stale")
        self.assertEqual(os.environ.get("ANTHROPIC_BASE_URL"), "https://stale.example")
        self.assertEqual(os.environ.get("ANTHROPIC_AUTH_TOKEN"), "stale-token")


class OptionsBuilderTests(unittest.TestCase):
    def test_build_options_accept_edits_and_model(self):
        opts = sdk.build_agent_options(
            cwd="/tmp/out",
            model="deepseek-v4-flash",
            mcp_servers=None,
            permission_mode="acceptEdits",
        )
        self.assertEqual(opts.model, "deepseek-v4-flash")
        self.assertEqual(opts.permission_mode, "acceptEdits")
        self.assertIn("Read", opts.allowed_tools)
        self.assertIn("Bash", opts.allowed_tools)
        self.assertTrue(opts.strict_mcp_config)

    def test_mcp_servers_path_passthrough(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "gsc-mcp.json"
            cfg.write_text('{"mcpServers":{}}', encoding="utf-8")
            got = sdk.gsc_mcp_servers(gsc_dir=None, mcp_config=cfg, enable_mcp=True)
            self.assertEqual(got, cfg)
            self.assertIsNone(sdk.gsc_mcp_servers(gsc_dir=None, mcp_config=cfg, enable_mcp=False))

    def test_describe_driver_keeps_mcp_intent(self):
        payload = sdk.describe_driver_policy(
            model="deepseek-v4-flash",
            enable_mcp=True,
            mcp_config=Path("/tmp/x.json"),
            gsc_dir=None,
            plugins=[],
        )
        self.assertEqual(payload["driver"], "ClaudeSDKClient")
        self.assertTrue(payload["mcp_enabled"])
        self.assertTrue(payload["no_production_proxy"])
        self.assertIsNotNone(payload["gsc_plugin_note"])


class SkillMdParseTests(unittest.TestCase):
    def test_skill_md_path_extract(self):
        self.assertEqual(
            sdk._skill_name_from_skill_md_ref(".claude/skills/architect/SKILL.md"),
            "architect",
        )
        self.assertEqual(
            sdk._skill_name_from_skill_md_ref("skills/designer/SKILL.md"),
            "designer",
        )
        self.assertIsNone(sdk._skill_name_from_skill_md_ref("README.md"))


if __name__ == "__main__":
    unittest.main()
