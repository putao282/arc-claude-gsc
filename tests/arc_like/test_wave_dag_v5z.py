# -*- coding: utf-8 -*-
"""v5z: WAVE/DOMAIN DAG planning + domain_dev_steps exclude batch_test."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_main():
    # Avoid importing heavy runtime deps when possible — main imports them at top.
    # Tests that need helpers will import via path with stubs if needed.
    sys.path.insert(0, str(ROOT))
    # arcbench_agent_runtime may be missing on bare checkout; provide a tiny stub.
    if "arcbench_agent_runtime" not in sys.modules:
        import types

        stub = types.ModuleType("arcbench_agent_runtime")

        class AgentRuntime:  # noqa: D401
            @classmethod
            def from_env(cls, **kwargs):
                raise RuntimeError("stub")

        stub.AgentRuntime = AgentRuntime
        sys.modules["arcbench_agent_runtime"] = stub
    if "claude_agent_sdk" not in sys.modules:
        import types

        stub = types.ModuleType("claude_agent_sdk")
        sys.modules["claude_agent_sdk"] = stub
    spec = importlib.util.spec_from_file_location("arc_main_v5z", ROOT / "main.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["arc_main_v5z"] = mod
    spec.loader.exec_module(mod)
    return mod


class WaveDagV5zTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_main()

    def test_domain_dev_steps_exclude_batch_test(self):
        ids = [s.step_id for s in self.mod.domain_dev_steps()]
        self.assertNotIn("batch_test", ids)
        self.assertIn("implement", ids)
        self.assertIn("prd", ids)
        batch = [s.step_id for s in self.mod.wave_batch_steps()]
        self.assertEqual(batch, ["batch_test"])
        # Full catalog still includes batch_test for compatibility
        self.assertIn("batch_test", [s.step_id for s in self.mod.official_steps()])

    def test_group_by_domain_and_plan_waves(self):
        M = self.mod.RequirementModule
        mods = [
            M(1, 4, "auth-login", "Login", {"id": "auth-login", "domain": "auth"}),
            M(2, 4, "auth-logout", "Logout", {"id": "auth-logout", "domain": "auth"}),
            M(3, 4, "billing-pay", "Pay", {"id": "billing-pay", "domain": "billing", "depends_on": ["auth"]}),
            M(4, 4, "reports-dash", "Dash", {"id": "reports-dash", "domain": "reports"}),
        ]
        groups = self.mod.build_domain_groups(mods)
        by = {g.domain_id: g for g in groups}
        self.assertEqual(len(by["auth"].modules), 2)
        self.assertEqual(by["billing"].depends_on, ("auth",))
        waves = self.mod.plan_domain_waves(groups)
        # WAVE1 should contain auth + reports (no mutual conflict/deps); billing after auth
        self.assertGreaterEqual(len(waves), 2)
        w1_ids = {g.domain_id for g in waves[0].domains}
        self.assertIn("auth", w1_ids)
        self.assertIn("reports", w1_ids)
        self.assertNotIn("billing", w1_ids)
        later = {g.domain_id for w in waves[1:] for g in w.domains}
        self.assertIn("billing", later)

    def test_conflicts_force_separate_waves(self):
        M = self.mod.RequirementModule
        mods = [
            M(1, 2, "a1", "A", {"id": "a1", "domain": "A", "conflicts_with": ["B"]}),
            M(2, 2, "b1", "B", {"id": "b1", "domain": "B"}),
        ]
        waves = self.mod.plan_domain_waves(self.mod.build_domain_groups(mods))
        self.assertEqual(len(waves), 2)

    def test_step_prompt_forbids_per_req_batch_test(self):
        M = self.mod.RequirementModule
        mod = M(1, 1, "x", "X", {"id": "x", "domain": "D"})
        step = next(s for s in self.mod.official_steps() if s.step_id == "implement")
        prompt = self.mod.step_prompt(
            mod,
            Path("/tmp/req"),
            None,
            [],
            "web",
            step,
            wave_ctx={
                "wave_index": 1,
                "wave_total": 2,
                "wave_domains": ["D"],
                "domain_id": "D",
                "worktree": "/tmp/wt",
                "sibling_reqs": ["x"],
                "plan_summary": "WAVE1: D[x]",
            },
        )
        self.assertIn("FORBIDDEN: per-REQ serial BATCH_TEST", prompt)
        self.assertIn("WAVE/DOMAIN DAG", prompt)
        self.assertIn("DOMAIN worktree", prompt)

    def test_batch_test_goal_mentions_wave_merge(self):
        bt = next(s for s in self.mod.OFFICIAL_STEPS if s.step_id == "batch_test")
        self.assertIn("DOMAIN worktrees", bt.goal)
        self.assertIn("WAVE", bt.goal)


if __name__ == "__main__":
    unittest.main()
