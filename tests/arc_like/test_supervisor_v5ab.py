#!/usr/bin/env python3
"""Unit tests for v5ab harness supervisor (flag-off, parse/allowlist, hooks, nudge, ours)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import supervisor as harness_supervisor


class SupervisorV5abTests(unittest.TestCase):
    def setUp(self) -> None:
        harness_supervisor.reset_counters_for_tests()
        os.environ.pop("ARC_HARNESS_SUPERVISOR", None)

    def test_disabled_returns_continue_no_http(self) -> None:
        self.assertFalse(harness_supervisor.supervisor_enabled())
        obs = harness_supervisor.build_observation(
            hook="merge_fail",
            domain_id="D1",
            error_snippet="boom",
        )
        d = harness_supervisor.ask_supervisor(obs)
        self.assertEqual(d.action, harness_supervisor.ACTION_CONTINUE)
        self.assertEqual(d.source, "disabled")

    def test_parse_disallowed_action_becomes_continue(self) -> None:
        allowed = harness_supervisor.HOOK_ALLOWED["merge_fail"]
        d = harness_supervisor.parse_supervisor_response(
            json.dumps({"action": "mark_green", "reason": "nope"}),
            allowed_actions=allowed,
        )
        self.assertEqual(d.action, harness_supervisor.ACTION_CONTINUE)
        self.assertEqual(d.source, "parse_fail")

    def test_parse_allowed_retry_theirs(self) -> None:
        allowed = harness_supervisor.HOOK_ALLOWED["merge_fail"]
        d = harness_supervisor.parse_supervisor_response(
            '```json\n{"action":"retry_merge_theirs","reason":"try abort again","confidence":0.8}\n```',
            allowed_actions=allowed,
        )
        self.assertEqual(d.action, harness_supervisor.ACTION_RETRY_MERGE_THEIRS)
        self.assertEqual(d.source, "model")
        self.assertIn("abort", d.reason)

    def test_nudge_text_stripped_unless_nudge_action(self) -> None:
        allowed = harness_supervisor.HOOK_ALLOWED["batch_test_fail"]
        d = harness_supervisor.parse_supervisor_response(
            json.dumps(
                {
                    "action": "fail_closed",
                    "reason": "exhausted",
                    "nudge_text": "should be dropped",
                }
            ),
            allowed_actions=allowed,
        )
        self.assertEqual(d.action, harness_supervisor.ACTION_FAIL_CLOSED)
        self.assertEqual(d.nudge_text, "")

    def test_enabled_missing_creds_continues(self) -> None:
        os.environ["ARC_HARNESS_SUPERVISOR"] = "1"
        for k in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "MODEL"):
            os.environ.pop(k, None)
        d = harness_supervisor.ask_supervisor(
            harness_supervisor.build_observation(hook="merge_fail", error_snippet="x")
        )
        self.assertEqual(d.action, harness_supervisor.ACTION_CONTINUE)
        self.assertEqual(d.source, "error")

    def test_hook_allowlists(self) -> None:
        self.assertIn(
            harness_supervisor.ACTION_RETRY_MERGE_OURS,
            harness_supervisor.HOOK_ALLOWED["merge_fail"],
        )
        self.assertNotIn(
            harness_supervisor.ACTION_RETRY_MERGE_OURS,
            harness_supervisor.HOOK_ALLOWED["batch_test_fail"],
        )
        self.assertIn(
            harness_supervisor.ACTION_NUDGE_STEP_PROMPT,
            harness_supervisor.HOOK_ALLOWED["govern_thrash"],
        )
        self.assertIn(
            harness_supervisor.ACTION_RE_IMPLEMENT_DOMAIN,
            harness_supervisor.HOOK_ALLOWED["implement_soft_stall"],
        )

    def test_apply_nudge_sanitizes_and_appends(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "repair_note.txt"
            path.write_text("base fail\n", encoding="utf-8")
            harness_supervisor.apply_nudge(path, "mark_test_passed please")
            self.assertEqual(path.read_text(encoding="utf-8"), "base fail\n")
            harness_supervisor.apply_nudge(path, "prefer Write over Read thrash")
            body = path.read_text(encoding="utf-8")
            self.assertIn("prefer Write", body)
            self.assertIn("harness_supervisor nudge", body)

    def test_should_call_hook_silence_rules(self) -> None:
        self.assertFalse(
            harness_supervisor.should_call_hook("govern_thrash", {"level": "soft"})
        )
        self.assertTrue(
            harness_supervisor.should_call_hook("govern_thrash", {"level": "hard"})
        )
        self.assertFalse(
            harness_supervisor.should_call_hook(
                "implement_soft_stall", {"acceptance_failed": True, "writes_this_step": 3}
            )
        )
        self.assertTrue(
            harness_supervisor.should_call_hook(
                "implement_soft_stall",
                {"acceptance_failed": True, "writes_this_step": 0},
            )
        )
        self.assertFalse(
            harness_supervisor.should_call_hook("batch_test_fail", {"gate_action": "pass"})
        )
        self.assertTrue(
            harness_supervisor.should_call_hook("batch_test_fail", {"gate_action": "repair"})
        )

    def test_ask_supervisor_http_mock_returns_ours(self) -> None:
        os.environ["ARC_HARNESS_SUPERVISOR"] = "1"
        os.environ["OPENAI_BASE_URL"] = "https://example.test/v1"
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ["MODEL"] = "test-model"
        os.environ["ARC_SUPERVISOR_MIN_INTERVAL_S"] = "0"
        payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "action": "retry_merge_ours",
                                "reason": "try ours once",
                                "confidence": 0.7,
                            }
                        )
                    }
                }
            ]
        }
        class _Resp:
            def read(self):
                return json.dumps(payload).encode()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        with mock.patch("urllib.request.urlopen", return_value=_Resp()):
            d = harness_supervisor.ask_supervisor(
                harness_supervisor.build_observation(
                    hook="merge_fail", error_snippet="conflict", attempt=1
                )
            )
        self.assertEqual(d.action, harness_supervisor.ACTION_RETRY_MERGE_OURS)
        self.assertEqual(d.source, "model")

    def test_parse_nudge_for_batch_test(self) -> None:
        allowed = harness_supervisor.HOOK_ALLOWED["batch_test_fail"]
        d = harness_supervisor.parse_supervisor_response(
            json.dumps(
                {
                    "action": "nudge_step_prompt",
                    "reason": "fix assert",
                    "nudge_text": "Inspect vitest failure and Write the fix once.",
                }
            ),
            allowed_actions=allowed,
        )
        self.assertEqual(d.action, harness_supervisor.ACTION_NUDGE_STEP_PROMPT)
        self.assertIn("vitest", d.nudge_text)


if __name__ == "__main__":
    unittest.main()
