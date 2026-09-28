# -*- coding: utf-8 -*-
"""v5aa: merge_domain_worktree aborts dirty index then merges with -X theirs."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_main():
    sys.path.insert(0, str(ROOT))
    if "arcbench_agent_runtime" not in sys.modules:
        import types

        stub = types.ModuleType("arcbench_agent_runtime")

        class AgentRuntime:
            @classmethod
            def from_env(cls, **kwargs):
                raise RuntimeError("stub")

        stub.AgentRuntime = AgentRuntime
        sys.modules["arcbench_agent_runtime"] = stub
    if "claude_agent_sdk" not in sys.modules:
        import types

        sys.modules["claude_agent_sdk"] = types.ModuleType("claude_agent_sdk")
    name = "arc_main_v5aa_merge"
    spec = importlib.util.spec_from_file_location(name, ROOT / "main.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "v5aa-test")
    env.setdefault("GIT_AUTHOR_EMAIL", "v5aa@test.local")
    env.setdefault("GIT_COMMITTER_NAME", "v5aa-test")
    env.setdefault("GIT_COMMITTER_EMAIL", "v5aa@test.local")
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
        env=env,
    )


def _init_mainline(tmp: Path) -> Path:
    main = tmp / "mainline"
    main.mkdir()
    _git(main, "init")
    _git(main, "config", "user.email", "v5aa@test.local")
    _git(main, "config", "user.name", "v5aa-test")
    (main / "shared.txt").write_text("base\n", encoding="utf-8")
    (main / "only_main.txt").write_text("main-only\n", encoding="utf-8")
    _git(main, "add", "-A")
    _git(main, "commit", "-m", "init")
    return main


class MergeDomainWorktreeV5aaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_main()

    def test_conflict_then_retry_succeeds_with_abort_and_theirs(self):
        """Dirty unmerged index must be aborted; domain tip wins overlapping paths."""
        with tempfile.TemporaryDirectory(prefix="v5aa-merge-") as td:
            tmp = Path(td)
            main = _init_mainline(tmp)
            domain_id = "REQ-2"
            # Create domain worktree/branch via harness helper
            wt = self.mod.ensure_domain_worktree(main, domain_id)
            self.assertTrue(wt.is_dir())

            # Divergent edits on same file
            (main / "shared.txt").write_text("mainline-version\n", encoding="utf-8")
            _git(main, "add", "-A")
            _git(main, "commit", "-m", "mainline diverges")

            (wt / "shared.txt").write_text("domain-version\n", encoding="utf-8")
            (wt / "domain_only.txt").write_text("from-domain\n", encoding="utf-8")
            _git(wt, "add", "-A")
            _git(wt, "commit", "-m", "domain diverges")

            # Reproduce the combat failure shape: leave mainline with unmerged index
            # by starting a merge WITHOUT -X / abort (old broken retry path).
            branch = f"arc-domain-{self.mod.safe_node_id(domain_id)}"
            dirty = _git(
                main,
                "merge",
                "--no-ff",
                "--no-commit",
                "-m",
                "dirty probe",
                branch,
                check=False,
            )
            self.assertNotEqual(
                dirty.returncode,
                0,
                msg="expected content conflict to dirty the index",
            )
            unmerged = self.mod._git_unmerged_paths(main)
            self.assertTrue(
                unmerged,
                msg="precondition: mainline must have unmerged paths before harness merge",
            )

            # Old bug: retry merge without abort → "Merging is not possible because
            # you have unmerged files". New code must abort then succeed with theirs.
            self.mod.merge_domain_worktree(main, domain_id, wt)

            self.assertEqual(
                (main / "shared.txt").read_text(encoding="utf-8"),
                "domain-version\n",
                msg="domain tip must win overlapping path",
            )
            self.assertEqual(
                (main / "domain_only.txt").read_text(encoding="utf-8"),
                "from-domain\n",
            )
            self.assertFalse(
                self.mod._git_unmerged_paths(main),
                msg="index must be clean after successful merge",
            )
            merge_head = _git(main, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False)
            self.assertNotEqual(merge_head.returncode, 0, msg="no in-progress merge left")
            # Worktree force-removed
            self.assertFalse(
                self.mod._worktree_registered(main, wt),
                msg="worktree should be removed after merge",
            )

    def test_abort_helper_clears_dirty_index_without_merge_retry(self):
        """_abort_merge_and_clean_index alone clears unmerged state (no silent leave-dirty)."""
        with tempfile.TemporaryDirectory(prefix="v5aa-abort-") as td:
            tmp = Path(td)
            main = _init_mainline(tmp)
            _git(main, "checkout", "-b", "side")
            (main / "shared.txt").write_text("side\n", encoding="utf-8")
            _git(main, "add", "-A")
            _git(main, "commit", "-m", "side")
            _git(main, "checkout", "master", check=False)
            # Some git inits use 'main' as default branch
            cur = _git(main, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            if cur == "side":
                # try main
                sw = _git(main, "checkout", "main", check=False)
                if sw.returncode != 0:
                    _git(main, "checkout", "-b", "main")
            (main / "shared.txt").write_text("trunk\n", encoding="utf-8")
            _git(main, "add", "-A")
            _git(main, "commit", "-m", "trunk")
            _git(main, "merge", "--no-ff", "--no-commit", "side", check=False)
            self.assertTrue(self.mod._git_unmerged_paths(main))
            self.mod._abort_merge_and_clean_index(main)
            self.assertFalse(
                self.mod._git_unmerged_paths(main),
                msg="abort helper must clear unmerged index",
            )

    def test_sequential_domain_merges_no_dirty_carryover(self):
        """Two domain merges in a row must not leave unmerged state for the second."""
        with tempfile.TemporaryDirectory(prefix="v5aa-seq-") as td:
            tmp = Path(td)
            main = _init_mainline(tmp)
            for did, content in (("REQ-1", "d1\n"), ("REQ-2", "d2\n")):
                wt = self.mod.ensure_domain_worktree(main, did)
                (wt / f"{did}.txt").write_text(content, encoding="utf-8")
                # overlapping edit so second would historically fail if dirty left behind
                (wt / "shared.txt").write_text(f"shared-{did}\n", encoding="utf-8")
                _git(wt, "add", "-A")
                _git(wt, "commit", "-m", f"{did} work")
                self.mod.merge_domain_worktree(main, did, wt)
                self.assertFalse(self.mod._git_unmerged_paths(main))
            self.assertEqual(
                (main / "shared.txt").read_text(encoding="utf-8"),
                "shared-REQ-2\n",
            )
            self.assertTrue((main / "REQ-1.txt").exists())
            self.assertTrue((main / "REQ-2.txt").exists())


if __name__ == "__main__":
    unittest.main()
