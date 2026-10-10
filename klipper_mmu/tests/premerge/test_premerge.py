#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/premerge.py: the merge checklist runs in a scratch repository."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parents[2] / "tools" / "premerge.py"

SAMPLE_TEST = ("import unittest\n"
               "\n"
               "class SampleTest(unittest.TestCase):\n"
               "    def test_ok(self):\n"
               "        self.assertTrue(True)\n")
STUBBED_TEST = SAMPLE_TEST.replace("self.assertTrue(True)", 'print("PASS: sample")')


def git(repo, *args, check=True):
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=60)
    if check and proc.returncode != 0:
        raise AssertionError("git %s failed: %s" % (" ".join(args), proc.stderr))
    return proc


class PremergeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "premerge@example.com")
        git(self.repo, "config", "user.name", "Premerge Test")
        self.write("README.md", "base\n")
        self.write("tests/sample/test_sample.py", SAMPLE_TEST)
        self.commit("base")
        self.branch_with("stale", "stale.txt")           # cut before main advances
        self.write("advance.txt", "advance\n")
        self.commit("advance main")
        self.branch_with("clean", "feature.txt")         # contains main
        self.fake_check = self.tmp / "fake_check.py"
        self.fake_check.write_text("import sys\nsys.exit(0)\n")
        self.failing_check = self.tmp / "failing_check.py"
        self.failing_check.write_text(
            "print('failed: make test')\n"
            "print('summary: %s')\n"
            "import sys\nsys.exit(1)\n" % (self.tmp / "build/check/summary.json"))

    def write(self, rel, text):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, message):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)

    def branch_with(self, name, rel):
        git(self.repo, "checkout", "-q", "-b", name, "main")
        self.write(rel, name + "\n")
        self.commit(name)
        git(self.repo, "checkout", "-q", "main")

    def premerge(self, *args, check_command=None):
        env = os.environ.copy()
        env["PREMERGE_BUILD_DIR"] = str(self.tmp / "build")
        env["PREMERGE_CHECK_CMD"] = "%s %s" % (
            sys.executable, check_command or self.fake_check)
        return subprocess.run([sys.executable, str(TOOL), "--repo", str(self.repo), *args],
                              capture_output=True, text=True, env=env, timeout=180)

    def main_head(self):
        return git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def assert_unmerged(self, before):
        self.assertEqual(self.main_head(), before)
        self.assertFalse((self.repo / ".git" / "MERGE_HEAD").exists())

    def assert_no_worktrees(self):
        listing = git(self.repo, "worktree", "list").stdout.strip().splitlines()
        self.assertEqual(len(listing), 1, listing)
        self.assertEqual(list((self.tmp / "build" / "premerge").glob("check-*")), [])

    def test_clean_branch_merges_with_the_message_as_given(self):
        before = self.main_head()
        result = self.premerge("clean", "-m", "merge the clean branch")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotEqual(self.main_head(), before)
        self.assertEqual(git(self.repo, "merge-base", "--is-ancestor", "clean", "main").returncode, 0)
        body = git(self.repo, "log", "-1", "--format=%B").stdout
        self.assertEqual(body.strip(), "merge the clean branch")
        self.assert_no_worktrees()

    def test_branch_without_main_is_refused(self):
        before = self.main_head()
        result = self.premerge("stale", "-m", "merge stale")
        self.assertEqual(result.returncode, 1)
        self.assertIn("rebase", result.stderr)
        self.assert_unmerged(before)

    def test_dirty_main_is_refused(self):
        before = self.main_head()
        self.write("README.md", "changed but uncommitted\n")
        result = self.premerge("clean", "-m", "merge clean")
        self.assertEqual(result.returncode, 1)
        self.assertIn("uncommitted", result.stderr)
        self.assert_unmerged(before)

    def test_untracked_build_dir_does_not_dirty_main(self):
        (self.repo / "build").mkdir()
        (self.repo / "build" / "junk").write_text("scratch\n")
        result = self.premerge("clean", "-m", "merge clean")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_integrity_failure_is_refused(self):
        before = self.main_head()
        git(self.repo, "checkout", "-q", "-b", "stubbed", "main")
        self.write("tests/sample/test_sample.py", STUBBED_TEST)
        self.commit("stub the sample test")
        git(self.repo, "checkout", "-q", "main")
        result = self.premerge("stubbed", "-m", "merge stubbed")
        self.assertEqual(result.returncode, 1)
        self.assertIn("integrity", result.stderr)
        self.assert_unmerged(before)

    def test_failing_check_is_refused(self):
        before = self.main_head()
        result = self.premerge("clean", "-m", "merge clean",
                               check_command=self.failing_check)
        self.assertEqual(result.returncode, 1)
        self.assertIn("check failed", result.stderr)
        self.assertIn("failed: make test", result.stdout)
        self.assertIn("summary.json", result.stdout)
        self.assert_unmerged(before)
        self.assert_no_worktrees()

    def test_dry_run_never_merges(self):
        before = self.main_head()
        result = self.premerge("clean", "-m", "merge clean", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assert_unmerged(before)
        self.assertEqual(git(self.repo, "merge-base", "--is-ancestor", "clean", "main",
                             check=False).returncode, 1)

    # -- Path scoping (tools/premerge.py firmware_affecting()) --------------

    def branch_touching(self, name, rel):
        """A branch containing main that adds one file at rel."""
        self.branch_with(name, rel)
        return name

    def test_default_path_uses_default_gate(self):
        self.branch_touching("docs-only", "docs/notes.md")
        result = self.premerge("docs-only", "-m", "merge docs-only")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("gate = tools/check.py (no firmware-affecting paths changed)",
                      result.stdout)

    def test_host_path_uses_release_gate(self):
        self.branch_touching("host-touch", "host/new_module.py")
        result = self.premerge("host-touch", "-m", "merge host-touch")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("gate = tools/check.py --release", result.stdout)

    def test_firmware_path_uses_release_gate(self):
        self.branch_touching("firmware-touch", "src/klipper_mmu/mcu/new_file.c")
        result = self.premerge("firmware-touch", "-m", "merge firmware-touch")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("gate = tools/check.py --release (firmware-affecting paths changed)",
                      result.stdout)
        self.assertIn("src/klipper_mmu/mcu/new_file.c", result.stdout)

    def test_release_flag_forces_full_gate_on_docs_only_branch(self):
        self.branch_touching("docs-only-forced", "docs/notes2.md")
        result = self.premerge("docs-only-forced", "-m", "merge docs-only-forced", "--release")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("gate = tools/check.py --release (--release was passed)", result.stdout)

    # -- Failed-gate pruning --------------------------------------------------

    def failing_check_with_build_tree(self):
        """A fake gate whose summary.json sits beside a big fake build tree,
        mimicking a release-matrix run so pruning can be checked."""
        run_dir = self.tmp / "build" / "check" / "run-x"
        (run_dir / "logs").mkdir(parents=True)
        (run_dir / "logs" / "001-sample.stdout").write_text("log line\n")
        (run_dir / "release-matrix" / "artifacts").mkdir(parents=True)
        (run_dir / "release-matrix" / "artifacts" / "big.bin").write_text("x" * 4096)
        summary = run_dir / "summary.json"
        summary.write_text("{}")
        script = self.tmp / "failing_check_tree.py"
        script.write_text(
            "print('failed: make test')\n"
            "print('summary: %s')\n"
            "import sys\nsys.exit(1)\n" % summary)
        return script

    def test_failed_gate_keeps_only_logs_and_summary(self):
        before = self.main_head()
        script = self.failing_check_with_build_tree()
        result = self.premerge("clean", "-m", "merge clean", check_command=script)
        self.assertEqual(result.returncode, 1)
        self.assert_unmerged(before)
        kept_dirs = list((self.tmp / "build" / "premerge").glob("logs-check-*"))
        self.assertEqual(len(kept_dirs), 1, kept_dirs)
        kept = kept_dirs[0]
        self.assertTrue((kept / "summary.json").is_file())
        self.assertTrue((kept / "logs" / "001-sample.stdout").is_file())
        self.assertFalse((kept / "release-matrix").exists())


if __name__ == "__main__":
    unittest.main()
