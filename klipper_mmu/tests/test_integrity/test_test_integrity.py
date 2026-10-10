#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/test_integrity.py."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "test_integrity.py"

C_BASE_ALPHA = r"""#include <assert.h>
#include <stdio.h>

static void test_keep(void)
{
    assert(1 + 1 == 2);
    printf("PASS: test_keep\n");
}

static void test_gone(void)
{
    assert(2 == 2);
    printf("PASS: test_gone\n");
}

static void test_stubbed(void)
{
    assert(3 == 3);
    assert(4 == 4);
    printf("PASS: test_stubbed\n");
}
"""

C_HEAD_ALPHA = r"""#include <assert.h>
#include <stdio.h>

/* assert(5 == 5); assert(6 == 6); */
static const char *banner = "assert(7 == 7)";

static void test_keep(void)
{
    assert(1 + 1 == 2); /* a comment cannot count */
    printf("PASS: %s\n", banner);
}

static void test_stubbed(void) { printf("PASS: test_stubbed\n"); }
"""

C_BASE_SHRINK = r"""#include <assert.h>
#include <stdio.h>

static void test_shrink(void)
{
    assert(1 == 1);
    assert(2 == 2);
    assert(3 == 3);
    printf("PASS: test_shrink\n");
}
"""

C_HEAD_SHRINK = r"""#include <assert.h>
#include <stdio.h>

static void test_shrink(void)
{
    assert(1 == 1);
    printf("PASS: test_shrink\n");
}
"""

C_BASE_THREE = r"""#include <assert.h>
#include <stdio.h>

static void test_removed_api(void)
{
    assert(9 == 9);
    assert(10 == 10);
    printf("PASS: test_removed_api\n");
}

static void test_other(void)
{
    assert(11 == 11);
}
"""

C_HEAD_THREE = r"""#include <assert.h>
#include <stdio.h>

static void test_other(void)
{
    assert(11 == 11);
}
"""

C_NEW_FILE = r"""#include <stdio.h>

static void helper(void)
{
    printf("nothing asserted here\n");
}
"""

C_WHOLE_GONE = r"""#include <assert.h>
#include <stdio.h>

static void test_whole_suite(void)
{
    assert(12 == 12);
    assert(13 == 13);
    printf("PASS: test_whole_suite\n");
}
"""

PY_BASE_ALPHA = r"""import unittest


class AlphaTests(unittest.TestCase):
    def test_kept(self):
        self.assertEqual(1, 1)
        self.assertTrue(True)

    def test_stubbed(self):
        self.assertEqual(2, 2)

    def test_gone(self):
        self.assertEqual(3, 3)
"""

PY_HEAD_ALPHA = r"""import unittest


class AlphaTests(unittest.TestCase):
    def test_kept(self):
        self.assertEqual(1, 1)
        self.assertTrue(True)

    def test_stubbed(self):
        # self.assertEqual(2, 2) used to be here
        print("PASS: test_stubbed")

    def test_skipped(self):
        self.skipTest("later")
"""

PY_NEW_FILE = r'''"""Placeholder for a suite that is still being written."""
'''

BASE_FILES = {
    "tests/c_suite/test_alpha.c": C_BASE_ALPHA,
    "tests/c_suite/test_shrink.c": C_BASE_SHRINK,
    "tests/c_suite/test_three.c": C_BASE_THREE,
    "tests/c_suite/test_whole_gone.c": C_WHOLE_GONE,
    "tests/py_suite/test_alpha.py": PY_BASE_ALPHA,
}

HEAD_FILES = {
    "tests/c_suite/test_alpha.c": C_HEAD_ALPHA,
    "tests/c_suite/test_shrink.c": C_HEAD_SHRINK,
    "tests/c_suite/test_three.c": C_HEAD_THREE,
    "tests/c_suite/test_brand_new.c": C_NEW_FILE,
    "tests/py_suite/test_alpha.py": PY_HEAD_ALPHA,
    "tests/py_suite/test_new.py": PY_NEW_FILE,
}

HEAD_KINDS = {"deleted", "deleted-file", "stub", "assertion-drop", "new-file"}


def git_env(repo: Path) -> dict:
    env = dict(os.environ)
    env.update({
        "HOME": str(repo),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "integrity",
        "GIT_AUTHOR_EMAIL": "integrity@example.invalid",
        "GIT_COMMITTER_NAME": "integrity",
        "GIT_COMMITTER_EMAIL": "integrity@example.invalid",
        "GIT_TERMINAL_PROMPT": "0",
    })
    return env


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True,
        text=True, env=git_env(repo),
    )
    if proc.returncode != 0:
        raise AssertionError("git %s failed: %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


def write_files(repo: Path, files: dict) -> None:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def run_tool(repo: Path, *args: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [sys.executable, str(TOOL), *args, "--repo", str(repo), "--json"],
        capture_output=True, text=True, env=git_env(repo), timeout=120,
    )
    return proc


def report_of(proc: subprocess.CompletedProcess) -> dict:
    if not proc.stdout.strip():
        raise AssertionError("no JSON output: %s" % proc.stderr)
    return json.loads(proc.stdout)


def keyed(report: dict) -> dict:
    return {(f["file"], f["kind"], f["name"]): f for f in report["findings"]}


def load_tool():
    spec = importlib.util.spec_from_file_location("test_integrity_tool", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestIntegrityReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="test-integrity-")
        cls.repo = Path(cls.tmp.name)
        git(cls.repo, "init", "-q")
        write_files(cls.repo, BASE_FILES)
        git(cls.repo, "add", "-A")
        git(cls.repo, "commit", "-q", "-m", "base")
        cls.base = git(cls.repo, "rev-parse", "HEAD")
        write_files(cls.repo, HEAD_FILES)
        for rel in set(BASE_FILES) - set(HEAD_FILES):
            (cls.repo / rel).unlink()
        git(cls.repo, "add", "-A")
        git(cls.repo, "commit", "-q", "-m", "head")
        cls.head = git(cls.repo, "rev-parse", "HEAD")
        cls.full = report_of(run_tool(cls.repo, cls.base, cls.head))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_flags_every_planted_change(self):
        self.assertEqual(self.full["flagged"], 14)
        self.assertEqual(self.full["allowed"], 0)
        self.assertEqual(self.full["files_compared"], 7)
        self.assertEqual({f["kind"] for f in self.full["findings"]}, HEAD_KINDS)

    def test_reports_deleted_and_stubbed_c_tests(self):
        keys = keyed(self.full)
        self.assertIn(("tests/c_suite/test_alpha.c", "deleted", "test_gone"), keys)
        self.assertIn(("tests/c_suite/test_alpha.c", "stub", "test_stubbed"), keys)
        stub = keys[("tests/c_suite/test_alpha.c", "stub", "test_stubbed")]
        self.assertEqual(stub["detail"], "no assertion call left (base had 2)")
        self.assertIn(("tests/c_suite/test_three.c", "deleted", "test_removed_api"), keys)
        self.assertIn(
            ("tests/c_suite/test_whole_gone.c", "deleted-file",
             "tests/c_suite/test_whole_gone.c"), keys)

    def test_reports_deleted_and_stubbed_python_tests(self):
        keys = keyed(self.full)
        self.assertIn(("tests/py_suite/test_alpha.py", "deleted", "test_gone"), keys)
        self.assertIn(("tests/py_suite/test_alpha.py", "stub", "test_stubbed"), keys)
        self.assertIn(("tests/py_suite/test_alpha.py", "stub", "test_skipped"), keys)
        skipped = keys[("tests/py_suite/test_alpha.py", "stub", "test_skipped")]
        self.assertEqual(skipped["detail"], "body only skips itself")

    def test_counts_ignore_comments_and_strings(self):
        files = {r["file"]: r for r in self.full["files"]}
        self.assertEqual(
            (files["tests/c_suite/test_alpha.c"]["base_assertions"],
             files["tests/c_suite/test_alpha.c"]["head_assertions"]), (4, 1))
        self.assertEqual(
            (files["tests/py_suite/test_alpha.py"]["base_assertions"],
             files["tests/py_suite/test_alpha.py"]["head_assertions"]), (4, 2))

    def test_reports_assertion_drops(self):
        keys = keyed(self.full)
        self.assertIn("3 -> 1", keys[("tests/c_suite/test_shrink.c",
                                      "assertion-drop", "")]["detail"])
        self.assertIn("4 -> 1", keys[("tests/c_suite/test_alpha.c",
                                      "assertion-drop", "")]["detail"])
        self.assertIn("4 -> 2", keys[("tests/py_suite/test_alpha.py",
                                      "assertion-drop", "")]["detail"])

    def test_reports_new_files_without_assertions(self):
        keys = keyed(self.full)
        self.assertIn(("tests/c_suite/test_brand_new.c", "new-file",
                       "tests/c_suite/test_brand_new.c"), keys)
        self.assertIn(("tests/py_suite/test_new.py", "new-file",
                       "tests/py_suite/test_new.py"), keys)

    def test_allow_excuses_a_deleted_test_and_its_drop(self):
        proc = run_tool(self.repo, self.base, self.head, "--allow", "test_removed_api")
        self.assertEqual(proc.returncode, 1)
        report = report_of(proc)
        self.assertEqual(report["flagged"], 12)
        three = [f for f in report["findings"] if f["file"] == "tests/c_suite/test_three.c"]
        self.assertEqual(len(three), 2)
        self.assertTrue(all(f["allowed"] for f in three))

    def test_allow_every_finding_passes(self):
        args = []
        for finding in self.full["findings"]:
            args += ["--allow", finding["name"] or finding["file"]]
        proc = run_tool(self.repo, self.base, self.head, *args)
        self.assertEqual(proc.returncode, 0)
        report = report_of(proc)
        self.assertEqual(report["flagged"], 0)
        self.assertEqual(report["allowed"], 14)

    def test_unchanged_revisions_report_nothing(self):
        proc = run_tool(self.repo, self.head, self.head)
        self.assertEqual(proc.returncode, 0)
        report = report_of(proc)
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["files_compared"], 6)

    def test_worktree_head_reports_nothing_when_clean(self):
        proc = run_tool(self.repo, self.head)
        self.assertEqual(proc.returncode, 0)
        report = report_of(proc)
        self.assertEqual(report["findings"], [])
        self.assertTrue(report["head_is_worktree"])

    def test_unknown_revision_is_a_usage_error(self):
        proc = run_tool(self.repo, "no-such-revision")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not a git revision", proc.stderr)


class TestCParsing(unittest.TestCase):
    SRC = r"""#include <assert.h>
/* assert(1); } */
static const char *banner = "assert(2); }";
static void test_outer(void)
{
    const char *s = "{ assert(3); }";
    if (banner) {
        assert(4);
    }
    printf("%s", s);
}
static void test_next(void) { assert(5); }
"""

    def test_brace_matching_skips_comments_and_strings(self):
        tool = load_tool()
        stripped = tool._strip_c(self.SRC)
        funcs = dict(tool._c_functions(stripped))
        self.assertEqual(set(funcs), {"test_outer", "test_next"})
        self.assertEqual(tool._c_assertions(funcs["test_outer"]), 1)
        self.assertEqual(tool._c_assertions(funcs["test_next"]), 1)
        self.assertEqual(tool._c_assertions(stripped), 2)

    def test_print_only_bodies(self):
        tool = load_tool()
        self.assertTrue(tool._c_print_only(r'  printf("PASS: x\n"); '))
        self.assertTrue(tool._c_print_only("return;"))
        self.assertFalse(tool._c_print_only("helper();"))
        self.assertFalse(tool._c_print_only('assert(1); printf("x");'))


class TestPythonParsing(unittest.TestCase):
    @staticmethod
    def _method(text):
        return ast.parse(text).body[0]

    def test_trivial_bodies(self):
        tool = load_tool()
        self.assertTrue(tool._py_trivial(self._method("def test_a(self):\n    pass\n")))
        self.assertTrue(tool._py_trivial(self._method("def test_a(self):\n    print('x')\n")))
        self.assertTrue(tool._py_trivial(self._method(
            'def test_a(self):\n    """doc"""\n    ...\n')))
        self.assertFalse(tool._py_trivial(self._method("def test_a(self):\n    helper()\n")))
        self.assertFalse(tool._py_trivial(self._method(
            "def test_a(self):\n    self.assertEqual(1, 1)\n")))

    def test_skip_decorator(self):
        tool = load_tool()
        skipped = self._method(
            '@unittest.skip("x")\ndef test_a(self):\n    self.assertEqual(1, 1)\n')
        self.assertTrue(tool._py_skipped(skipped))
        self.assertFalse(tool._py_skipped(self._method(
            "def test_a(self):\n    self.assertEqual(1, 1)\n")))

    def test_assertion_counting(self):
        tool = load_tool()
        tree = ast.parse(
            "def test_a(self):\n    assert 1\n    self.assertTrue(2)\n"
            "    self.skipTest('s')\n")
        self.assertEqual(tool._py_assertions(tree), 2)


if __name__ == "__main__":
    unittest.main()
