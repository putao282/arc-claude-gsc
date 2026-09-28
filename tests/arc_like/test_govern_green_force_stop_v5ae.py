"""v5ae: govern accept already green → FORCE STOP; no supervisor fail_closed."""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]


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


def _load_main():
    _install_test_stubs()
    spec = importlib.util.spec_from_file_location("main_v5ae", ROOT / "main.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class GovernGreenDetectV5aeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = _load_main()

    def _result(self, **kw):
        base = dict(
            returncode=0,
            is_error=False,
            terminal_reason="completed",
            subtype="success",
            api_error_status=None,
            tail="",
            skills_loaded=(),
            mcp_tools_used=(),
            builtin_writes=(),
            thrash_hit=False,
            thrash_events=(),
            deny_events=(),
            thrash_counts=(),
        )
        base.update(kw)
        return self.main.ClaudeRunResult(**base)

    def test_green_via_deny_reason(self):
        r = self._result(
            mcp_tools_used=("mcp__arch__prd_govern", "mcp__arch__spec_govern"),
            deny_events=(
                {
                    "event": "mcp_thrash_pretool_deny",
                    "reason": "govern_accept_already_green",
                    "tool": "mcp__arch__prd_govern",
                },
            ),
            thrash_hit=True,
        )
        self.assertTrue(self.main.govern_accept_already_green(r))

    def test_green_via_tool_pair_alone(self):
        r = self._result(
            mcp_tools_used=("mcp__arch__prd_govern", "mcp__arch__spec_govern"),
            thrash_hit=True,
            thrash_events=(
                {"event": "mcp_thrash_guard", "level": "hard", "tool": "mcp__arch__prd_govern"},
            ),
        )
        self.assertTrue(self.main.govern_accept_already_green(r))

    def test_not_green_prd_only(self):
        r = self._result(
            mcp_tools_used=("mcp__arch__prd_govern",),
            thrash_hit=True,
            deny_events=(
                {"event": "mcp_thrash_pretool_deny", "tool": "mcp__arch__prd_govern", "identical_count": 3},
            ),
        )
        self.assertFalse(self.main.govern_accept_already_green(r))

    def test_green_via_thrash_level(self):
        r = self._result(
            mcp_tools_used=("mcp__arch__prd_govern", "mcp__arch__spec_govern"),
            thrash_events=(
                {"event": "mcp_thrash_guard", "level": "govern_accept_green_deny", "tool": "mcp__arch__prd_govern"},
            ),
        )
        self.assertTrue(self.main.govern_accept_already_green(r))

    def test_policy_tag_present(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("v5ae_govern_green_force_stop_no_supervisor_fail_closed", src)
        self.assertIn("govern_accept_already_green_force_stop", src)


class SupervisorGreenSilenceV5aeTests(unittest.TestCase):
    def setUp(self):
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import supervisor as harness_supervisor

        self.sup = harness_supervisor
        self.sup.reset_counters_for_tests()

    def test_should_call_silent_when_accept_met(self):
        # Exact Official failure shape: thrash_hit + green → must NOT call model
        self.assertFalse(
            self.sup.should_call_hook(
                "govern_thrash",
                {
                    "level": "hard",
                    "thrash_hit": True,
                    "govern_accept_met": True,
                    "deny_count": 3,
                },
            )
        )

    def test_should_call_hard_thrash_when_not_green(self):
        self.assertTrue(
            self.sup.should_call_hook(
                "govern_thrash",
                {
                    "level": "hard",
                    "thrash_hit": True,
                    "govern_accept_met": False,
                    "deny_count": 3,
                },
            )
        )

    def test_soft_still_silent(self):
        self.assertFalse(
            self.sup.should_call_hook(
                "govern_thrash",
                {"level": "soft", "thrash_hit": False, "govern_accept_met": False},
            )
        )

    def test_ask_supervisor_silent_continue_when_green(self):
        import os

        os.environ["ARC_HARNESS_SUPERVISOR"] = "1"
        # Even with flag ON, green → silent continue (no HTTP needed)
        obs = self.sup.build_observation(
            hook="govern_thrash",
            req_id="REQ-1",
            step_id="govern",
            extras={
                "level": "hard",
                "thrash_hit": True,
                "govern_accept_met": True,
                "deny_count": 3,
            },
        )
        d = self.sup.ask_supervisor(obs)
        self.assertEqual(d.action, self.sup.ACTION_CONTINUE)
        self.assertEqual(d.source, "silent")
        os.environ.pop("ARC_HARNESS_SUPERVISOR", None)


if __name__ == "__main__":
    unittest.main()
