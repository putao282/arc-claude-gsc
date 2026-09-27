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

        class HookMatcher:
            def __init__(self, matcher=None, hooks=None, timeout=None):
                self.matcher = matcher
                self.hooks = hooks or []
                self.timeout = timeout

        mod.ClaudeAgentOptions = ClaudeAgentOptions
        mod.ClaudeSDKClient = ClaudeSDKClient
        mod.ResultMessage = ResultMessage
        mod.AssistantMessage = AssistantMessage
        mod.SystemMessage = SystemMessage
        mod.ToolUseBlock = ToolUseBlock
        mod.HookMatcher = HookMatcher
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
            self.assertEqual(os.environ.get("ANTHROPIC_BASE_URL"), "https://api.arc-bench.com")
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
        self.assertIn("Skill", opts.allowed_tools)
        self.assertTrue(opts.strict_mcp_config)
        self.assertEqual(opts.setting_sources, ["user", "project"])
        self.assertEqual(opts.skills, "all")

    def test_build_options_official_skills_wiring(self):
        """Official Agent SDK Skills wiring: setting_sources + skills=all (no force-load)."""
        opts = sdk.build_agent_options(cwd="/tmp/out", model="sonnet")
        self.assertEqual(list(opts.setting_sources), list(sdk.DEFAULT_SETTING_SOURCES))
        self.assertEqual(opts.skills, sdk.DEFAULT_SKILLS)
        self.assertIn("Skill", opts.allowed_tools)
        prompt = sdk.contest_system_prompt_append(None)
        self.assertIn("Skill", prompt)
        self.assertNotIn("otherwise Read/Bash", prompt)
        self.assertNotIn("Read/Bash the skill", prompt)

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
        self.assertTrue(payload.get("anthropic_proxy") or payload.get("no_production_proxy"))
        self.assertIsNotNone(payload["gsc_plugin_note"])
        self.assertEqual(payload.get("setting_sources"), ["user", "project"])
        self.assertEqual(payload.get("skills"), "all")
        self.assertTrue(payload.get("skill_tool_expected"))


class ModelAliasTests(unittest.TestCase):
    def test_sdk_model_alias_for_contest_ids(self):
        self.assertEqual(sdk.sdk_model_for_options("deepseek-v4-flash"), "sonnet")
        self.assertEqual(sdk.sdk_model_for_options("sonnet"), "sonnet")
        self.assertEqual(sdk.sdk_model_for_options("claude-sonnet-4"), "claude-sonnet-4")

    def test_apply_contest_model_env(self):
        out = sdk.apply_contest_model_env({}, "deepseek-v4-flash")
        self.assertEqual(out["ANTHROPIC_DEFAULT_SONNET_MODEL"], "deepseek-v4-flash")
        self.assertEqual(out["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"], "1")



class MaxTurnsAndThrashTests(unittest.TestCase):
    def test_default_and_spec_max_turns(self):
        self.assertGreaterEqual(sdk.DEFAULT_MAX_TURNS, 100)
        self.assertEqual(sdk.max_turns_for_step("spec"), 140)
        self.assertEqual(sdk.max_turns_for_step("prd"), 100)
        self.assertEqual(sdk.max_turns_for_step("unknown_step"), sdk.DEFAULT_MAX_TURNS)

    def test_build_options_uses_raised_default_turns(self):
        opts = sdk.build_agent_options(cwd="/tmp/out", model="sonnet")
        self.assertEqual(opts.max_turns, sdk.DEFAULT_MAX_TURNS)

    def test_thrash_guard_soft_then_hard(self):
        guard = sdk.McpThrashGuard(soft_limit=2, hard_limit=3)
        inp = {"path": "SPEC/arcbench/REQ-1.html"}
        self.assertIsNone(guard.note("mcp__arch__spec_read", inp))
        soft = guard.note("mcp__arch__spec_read", inp)
        self.assertIsNotNone(soft)
        self.assertEqual(soft["level"], "soft")
        hard = guard.note("mcp__arch__spec_read", inp)
        self.assertEqual(hard["level"], "hard")
        self.assertTrue(guard.thrash_hit)
        # Different args do not trip the same fingerprint
        other = guard.note("mcp__arch__spec_read", {"path": "other.html"})
        self.assertIsNone(other)

    def test_fingerprint_stable(self):
        a = sdk._mcp_tool_fingerprint("mcp__arch__spec_read", {"b": 1, "a": 2})
        b = sdk._mcp_tool_fingerprint("mcp__arch__spec_read", {"a": 2, "b": 1})
        self.assertEqual(a, b)

    def test_system_prompt_mentions_anti_thrash(self):
        txt = sdk.contest_system_prompt_append(None)
        self.assertIn("ANTI-THRASH", txt)
        self.assertIn("Skill tool", txt)
        self.assertIn("setting_sources", txt)
        self.assertIn("do not Read/Bash/cat SKILL.md", txt)
        self.assertNotIn("otherwise Read/Bash", txt)



class PreToolUseThrashDenyTests(unittest.TestCase):
    def test_build_options_wires_pretooluse_hooks(self):
        opts = sdk.build_agent_options(cwd="/tmp/out", model="sonnet", step_id="implement")
        self.assertTrue(hasattr(opts, "hooks"))
        self.assertIn("PreToolUse", opts.hooks)
        self.assertTrue(opts.hooks["PreToolUse"])
        guard = getattr(opts, "_arc_thrash_guard", None)
        self.assertIsNotNone(guard)
        self.assertEqual(guard.step_id, "implement")

    def test_identical_mcp_read_denied_at_hard_limit(self):
        import asyncio

        guard = sdk.McpThrashGuard(soft_limit=2, hard_limit=3, step_id="spec")
        hooks = sdk.build_thrash_pretool_hooks(guard)
        matcher = hooks["PreToolUse"][0]
        cb = matcher.hooks[0]
        inp = {"path": "SPEC/arcbench/REQ-1.html"}

        async def call(n):
            return await cb(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "mcp__arch__spec_read",
                    "tool_input": inp,
                },
                None,
                None,
            )

        async def run():
            outs = []
            for _ in range(4):
                outs.append(await call(_))
            return outs

        outs = asyncio.run(run())
        # First two under hard_limit: allow (empty / no deny)
        for o in outs[:2]:
            decision = (o.get("hookSpecificOutput") or {}).get("permissionDecision")
            self.assertNotEqual(decision, "deny")
        # 3rd reaches hard_limit → deny
        self.assertEqual(outs[2]["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("identical", outs[2]["hookSpecificOutput"]["permissionDecisionReason"].lower())
        # 4th also deny
        self.assertEqual(outs[3]["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertTrue(guard.deny_events)
        # Soft/hard log events still recorded
        levels = [e.get("level") for e in guard.events]
        self.assertIn("soft", levels)
        self.assertIn("hard", levels)

    def test_read_streak_deny_on_implement(self):
        import asyncio

        guard = sdk.McpThrashGuard(
            soft_limit=2, hard_limit=3, read_streak_limit=3, step_id="implement"
        )
        hooks = sdk.build_thrash_pretool_hooks(guard)
        cb = hooks["PreToolUse"][0].hooks[0]

        async def read_once():
            return await cb(
                {"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {"file_path": "x"}},
                None,
                None,
            )

        async def run():
            outs = []
            for _ in range(3):
                outs.append(await read_once())
            return outs

        outs = asyncio.run(run())
        self.assertNotEqual((outs[0].get("hookSpecificOutput") or {}).get("permissionDecision"), "deny")
        self.assertNotEqual((outs[1].get("hookSpecificOutput") or {}).get("permissionDecision"), "deny")
        self.assertEqual(outs[2]["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("Write", outs[2]["hookSpecificOutput"]["permissionDecisionReason"])



class GovernThrashV5yTests(unittest.TestCase):
    """v5y: *_govern on thrash watch + accept-already-green PreToolUse deny."""

    def test_govern_tools_are_thrash_watched(self):
        self.assertIn("prd_govern", sdk.MCP_THRASH_WATCH_SUFFIXES)
        self.assertIn("spec_govern", sdk.MCP_THRASH_WATCH_SUFFIXES)
        self.assertTrue(sdk._is_thrash_watched_mcp("mcp__arch__prd_govern"))
        self.assertTrue(sdk._is_thrash_watched_mcp("mcp__arch__spec_govern"))

    def test_identical_govern_denied_at_hard_limit(self):
        import asyncio

        guard = sdk.McpThrashGuard(soft_limit=2, hard_limit=3, step_id="govern")
        hooks = sdk.build_thrash_pretool_hooks(guard)
        cb = hooks["PreToolUse"][0].hooks[0]
        inp = {"focus": "REQ-1"}

        async def call():
            return await cb(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "mcp__arch__prd_govern",
                    "tool_input": inp,
                },
                None,
                None,
            )

        async def run():
            return [await call() for _ in range(4)]

        outs = asyncio.run(run())
        for o in outs[:2]:
            self.assertNotEqual(
                (o.get("hookSpecificOutput") or {}).get("permissionDecision"), "deny"
            )
        self.assertEqual(outs[2]["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("identical", outs[2]["hookSpecificOutput"]["permissionDecisionReason"].lower())
        self.assertTrue(guard.deny_events)

    def test_accept_green_denies_further_govern_reaudit(self):
        import asyncio

        guard = sdk.McpThrashGuard(soft_limit=2, hard_limit=3, step_id="govern")
        hooks = sdk.build_thrash_pretool_hooks(guard)
        cb = hooks["PreToolUse"][0].hooks[0]

        async def call(tool, inp=None):
            return await cb(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": tool,
                    "tool_input": inp or {"n": 1},
                },
                None,
                None,
            )

        async def run():
            # Completing pair must be allowed
            a = await call("mcp__arch__prd_govern", {"k": "a"})
            b = await call("mcp__arch__spec_govern", {"k": "b"})
            # Further re-audit (even different args) denied
            c = await call("mcp__arch__prd_govern", {"k": "c-different"})
            d = await call("mcp__arch__spec_govern", {"k": "d-different"})
            return a, b, c, d

        a, b, c, d = asyncio.run(run())
        self.assertNotEqual((a.get("hookSpecificOutput") or {}).get("permissionDecision"), "deny")
        self.assertNotEqual((b.get("hookSpecificOutput") or {}).get("permissionDecision"), "deny")
        self.assertTrue(guard.govern_accept_met())
        self.assertEqual(c["hookSpecificOutput"]["permissionDecision"], "deny")
        reason = c["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("already met", reason.lower())
        self.assertIn("STOP", reason)
        self.assertEqual(d["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertTrue(
            any(e.get("reason") == "govern_accept_already_green" for e in guard.deny_events)
        )

    def test_accept_green_deny_only_on_govern_step(self):
        import asyncio

        guard = sdk.McpThrashGuard(soft_limit=2, hard_limit=3, step_id="spec")
        hooks = sdk.build_thrash_pretool_hooks(guard)
        cb = hooks["PreToolUse"][0].hooks[0]

        async def run():
            for tool in ("mcp__arch__prd_govern", "mcp__arch__spec_govern"):
                await cb(
                    {
                        "hook_event_name": "PreToolUse",
                        "tool_name": tool,
                        "tool_input": {"x": 1},
                    },
                    None,
                    None,
                )
            # On non-govern STEP, further govern calls are NOT accept-green-denied
            # (still subject to identical thrash). Different args → allow.
            return await cb(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "mcp__arch__prd_govern",
                    "tool_input": {"x": 2},
                },
                None,
                None,
            )

        out = asyncio.run(run())
        self.assertTrue(guard.govern_accept_met())
        self.assertNotEqual(
            (out.get("hookSpecificOutput") or {}).get("permissionDecision"), "deny"
        )

if __name__ == "__main__":
    unittest.main()
