# -*- coding: utf-8 -*-
"""v5ak: merge_domain_worktree clears untracked blockers; excludes .*/CLAUDE.md from mainline."""
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
    name = "arc_main_v5ak_merge"
    # reload-friendly unique name
    name = "arc_main_v5ak_merge_excl"
    spec = importlib.util.spec_from_file_location(name, ROOT / "main.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "v5ak-test")
    env.setdefault("GIT_AUTHOR_EMAIL", "v5ak@test.local")
    env.setdefault("GIT_COMMITTER_NAME", "v5ak-test")
    env.setdefault("GIT_COMMITTER_EMAIL", "v5ak@test.local")
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
    _git(main, "config", "user.email", "v5ak@test.local")
    _git(main, "config", "user.name", "v5ak-test")
    (main / "shared.txt").write_text("base\n", encoding="utf-8")
    _git(main, "add", "-A")
    _git(main, "commit", "-m", "init")
    return main


# Exact combat failure paths from Official v5aj dual-track domain_merge.
COMBAT_UNTRACKED = (
    ".claude/spawn-gate-off",
    ".gsc/project-state.json",
    "CLAUDE.md",
    "SPEC/arcbench/REQ-1.html",
)
EXCLUDED_FROM_MAINLINE = (
    ".claude/spawn-gate-off",
    ".gsc/project-state.json",
    "CLAUDE.md",
)
PRODUCT_FROM_DOMAIN = ("SPEC/arcbench/REQ-1.html",)


class MergeDomainWorktreeV5akTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_main()

    def test_helpers_present(self):
        self.assertTrue(callable(self.mod._clear_untracked_blocking_merge))
        self.assertTrue(callable(self.mod._parse_untracked_overwrite_paths))
        self.assertTrue(callable(self.mod._is_mainline_excluded_path))
        self.assertTrue(callable(self.mod._strip_mainline_excluded_paths))
        self.assertIn("v5ak_merge_domain_worktree_clear_untracked", (ROOT / "main.py").read_text(encoding="utf-8"))

    def test_is_mainline_excluded_path_policy(self):
        self.assertTrue(self.mod._is_mainline_excluded_path(".claude/spawn-gate-off"))
        self.assertTrue(self.mod._is_mainline_excluded_path(".gsc/project-state.json"))
        self.assertTrue(self.mod._is_mainline_excluded_path(".env"))
        self.assertTrue(self.mod._is_mainline_excluded_path("CLAUDE.md"))
        self.assertTrue(self.mod._is_mainline_excluded_path("subdir/CLAUDE.md"))
        self.assertFalse(self.mod._is_mainline_excluded_path("SPEC/arcbench/REQ-1.html"))
        self.assertFalse(self.mod._is_mainline_excluded_path("shared.txt"))
        self.assertFalse(self.mod._is_mainline_excluded_path(".gitignore"))
        self.assertFalse(self.mod._is_mainline_excluded_path(".gitattributes"))

    def test_parse_untracked_overwrite_paths(self):
        err = (
            "error: The following untracked working tree files would be overwritten by merge:\n"
            "\t.claude/spawn-gate-off\n"
            "\t.gsc/project-state.json\n"
            "\tCLAUDE.md\n"
            "\tSPEC/arcbench/REQ-1.html\n"
            "Please move or remove them before you merge.\n"
            "Aborting\n"
        )
        paths = self.mod._parse_untracked_overwrite_paths(err)
        self.assertEqual(
            paths,
            [
                ".claude/spawn-gate-off",
                ".gsc/project-state.json",
                "CLAUDE.md",
                "SPEC/arcbench/REQ-1.html",
            ],
        )

    def test_combat_untracked_cleared_product_lands_excluded_stripped(self):
        """Combat: clear blockers; SPEC lands; .*/CLAUDE.md never promote onto mainline."""
        with tempfile.TemporaryDirectory(prefix="v5ak-combat-") as td:
            tmp = Path(td)
            main = _init_mainline(tmp)
            domain_id = "REQ-1"
            wt = self.mod.ensure_domain_worktree(main, domain_id)

            for rel in COMBAT_UNTRACKED:
                p = wt / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(f"domain:{rel}\n", encoding="utf-8")
            (wt / "shared.txt").write_text("domain-shared\n", encoding="utf-8")
            _git(wt, "add", "-A")
            _git(wt, "commit", "-m", "domain adds combat paths")

            for rel in COMBAT_UNTRACKED:
                p = main / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(f"untracked-mainline:{rel}\n", encoding="utf-8")

            branch = f"arc-domain-{self.mod.safe_node_id(domain_id)}"
            probe = _git(
                main,
                "merge",
                "--no-ff",
                "-X",
                "theirs",
                "-m",
                "probe",
                branch,
                check=False,
            )
            self.assertNotEqual(probe.returncode, 0)
            blob = ((probe.stderr or "") + "\n" + (probe.stdout or "")).lower()
            self.assertIn("untracked", blob)
            self.assertIn("overwritten", blob)
            _git(main, "merge", "--abort", check=False)

            self.mod.merge_domain_worktree(main, domain_id, wt)

            for rel in PRODUCT_FROM_DOMAIN:
                self.assertEqual(
                    (main / rel).read_text(encoding="utf-8"),
                    f"domain:{rel}\n",
                    msg=f"product path must land from domain for {rel}",
                )
            for rel in EXCLUDED_FROM_MAINLINE:
                self.assertFalse(
                    (main / rel).exists(),
                    msg=f"excluded path must NOT promote onto mainline: {rel}",
                )
            self.assertEqual(
                (main / "shared.txt").read_text(encoding="utf-8"),
                "domain-shared\n",
            )
            self.assertFalse(self.mod._git_unmerged_paths(main))
            self.assertFalse(self.mod._worktree_registered(main, wt))

    def test_clear_helper_removes_intersection_and_excluded(self):
        with tempfile.TemporaryDirectory(prefix="v5ak-clear-") as td:
            tmp = Path(td)
            main = _init_mainline(tmp)
            _git(main, "checkout", "-b", "side")
            (main / "from_side.txt").write_text("side\n", encoding="utf-8")
            _git(main, "add", "-A")
            _git(main, "commit", "-m", "side file")
            cur = _git(main, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            if cur == "side":
                sw = _git(main, "checkout", "master", check=False)
                if sw.returncode != 0:
                    sw = _git(main, "checkout", "main", check=False)
                    if sw.returncode != 0:
                        _git(main, "checkout", "-b", "main")

            (main / "from_side.txt").write_text("untracked-blocker\n", encoding="utf-8")
            (main / "keep_untracked.txt").write_text("keep-me\n", encoding="utf-8")
            (main / ".claude").mkdir(parents=True, exist_ok=True)
            (main / ".claude" / "spawn-gate-off").write_text("x\n", encoding="utf-8")
            (main / "CLAUDE.md").write_text("untracked\n", encoding="utf-8")

            cleared = self.mod._clear_untracked_blocking_merge(main, "side", domain_id="T")
            self.assertIn("from_side.txt", cleared)
            self.assertIn(".claude/spawn-gate-off", cleared)
            self.assertIn("CLAUDE.md", cleared)
            self.assertFalse((main / "from_side.txt").exists())
            self.assertFalse((main / ".claude" / "spawn-gate-off").exists())
            self.assertFalse((main / "CLAUDE.md").exists())
            self.assertTrue((main / "keep_untracked.txt").exists())

    def test_v5aa_abort_theirs_still_works_excluded_stripped(self):
        """Regression: content theirs still works; CLAUDE.md excluded from mainline."""
        with tempfile.TemporaryDirectory(prefix="v5ak-v5aa-") as td:
            tmp = Path(td)
            main = _init_mainline(tmp)
            domain_id = "REQ-2"
            wt = self.mod.ensure_domain_worktree(main, domain_id)

            (main / "shared.txt").write_text("mainline-version\n", encoding="utf-8")
            _git(main, "add", "-A")
            _git(main, "commit", "-m", "mainline diverges")

            (wt / "shared.txt").write_text("domain-version\n", encoding="utf-8")
            (wt / "domain_only.txt").write_text("from-domain\n", encoding="utf-8")
            _git(wt, "add", "-A")
            _git(wt, "commit", "-m", "domain diverges")

            (main / "CLAUDE.md").write_text("untracked-claude\n", encoding="utf-8")
            (wt / "CLAUDE.md").write_text("domain-claude\n", encoding="utf-8")
            _git(wt, "add", "-A")
            _git(wt, "commit", "-m", "domain claude")

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
            _ = dirty
            if not self.mod._git_unmerged_paths(main):
                _git(main, "merge", "--abort", check=False)
                _git(
                    main,
                    "merge",
                    "--no-ff",
                    "--no-commit",
                    "-m",
                    "dirty2",
                    branch,
                    check=False,
                )

            self.mod.merge_domain_worktree(main, domain_id, wt)
            self.assertEqual(
                (main / "shared.txt").read_text(encoding="utf-8"),
                "domain-version\n",
            )
            self.assertFalse((main / "CLAUDE.md").exists())
            self.assertTrue((main / "domain_only.txt").exists())
            self.assertFalse(self.mod._git_unmerged_paths(main))


if __name__ == "__main__":
    unittest.main()
