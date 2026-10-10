"""Tests for tools/check_licenses.py, the guard on the license boundary.

Licensing here is per directory: ``klipper_mmu/`` is GPL-3.0-or-later, the rest
of the tree is MIT.  The guard must hold on this tree and must fail on a tree
that breaks the boundary in either direction.
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOLS = os.path.join(ROOT, "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import check_licenses  # noqa: E402

# This test file is part of the MIT half, which the guard scans too: a literal
# GPL tag in here would itself read as a GPL header in the MIT tree, so the tag
# is spelled in two pieces and the identifiers are borrowed from the checker.
TAG = "SPDX-License-" + "Identifier: "
GPL_OR_LATER = check_licenses.GPL_SOURCE_IDS[0].decode()
GPL_ONLY = check_licenses.GPL_SOURCE_IDS[1].decode()


def header(identifier):
    return "# %s%s\n" % (TAG, identifier)


def run_guard(files):
    """Check a temporary tree built from {relative path: text}; return (code, output)."""
    with tempfile.TemporaryDirectory() as tmp:
        for rel, text in files.items():
            path = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = check_licenses.main([tmp])
        return code, buffer.getvalue()


class CheckLicensesTest(unittest.TestCase):
    def test_repository_tree_is_in_bounds(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = check_licenses.main([])
        self.assertEqual(0, code, buffer.getvalue())
        self.assertIn("the license boundary holds", buffer.getvalue())

    def test_missing_gpl_header_in_klipper_mmu_fails(self):
        code, out = run_guard({"klipper_mmu/host/orphan.py": "import os\n"})
        self.assertEqual(1, code)
        self.assertIn("klipper_mmu/host/orphan.py", out)

    def test_non_gpl_header_in_klipper_mmu_fails(self):
        code, out = run_guard({"klipper_mmu/src/mit.c": header("MIT") + "void f(void) {}\n"})
        self.assertEqual(1, code)
        self.assertIn("klipper_mmu/src/mit.c", out)

    def test_gpl_header_outside_klipper_mmu_fails(self):
        code, out = run_guard({"src/leaked.py": header(GPL_OR_LATER) + "pass\n"})
        self.assertEqual(1, code)
        self.assertIn("src/leaked.py", out)

    def test_gpl_header_in_a_non_source_mit_file_fails(self):
        code, out = run_guard({"docs/copy.md": header(GPL_OR_LATER) + "# notes\n"})
        self.assertEqual(1, code)
        self.assertIn("docs/copy.md", out)

    def test_both_gpl_3_0_spellings_pass(self):
        files = {
            "klipper_mmu/host/later.py": header(GPL_OR_LATER) + "pass\n",
            "klipper_mmu/src/only.c": "// %s%s\n" % (TAG, GPL_ONLY),
            "src/mit_plugin.py": "# MIT licensed; the repository LICENSE covers it.\n",
        }
        code, out = run_guard(files)
        self.assertEqual(0, code, out)

    def test_pinned_upstream_fixtures_are_exempt(self):
        files = {
            "klipper_mmu/references/pinned.c": "// Verbatim upstream, no tag.\n",
            "klipper_mmu/tests/fixtures/shared_tmcuart/src/tmcuart.c": "// upstream\n",
        }
        code, out = run_guard(files)
        self.assertEqual(0, code, out)

    def test_klipper_patches_is_a_gpl_directory(self):
        # A source file there needs a GPL tag, like klipper_mmu/.
        code, out = run_guard({"klipper_patches/helper.py": "import os\n"})
        self.assertEqual(1, code)
        self.assertIn("klipper_patches/helper.py", out)
        code, out = run_guard({"klipper_patches/helper.py": header(GPL_OR_LATER) + "pass\n"})
        self.assertEqual(0, code, out)

    def test_klipper_patch_files_and_notices_need_no_header(self):
        files = {
            "klipper_patches/can/0001-demo.patch": "From 0000 Mon Sep 17 00:00:00 2001\n",
            "klipper_patches/can/series": "0001-demo.patch\n",
            "klipper_patches/NOTICE.md": "# notice\n",
        }
        code, out = run_guard(files)
        self.assertEqual(0, code, out)

    def test_non_source_files_in_klipper_mmu_need_no_header(self):
        code, out = run_guard({"klipper_mmu/README.md": "# klipper-mmu\n"})
        self.assertEqual(0, code, out)


if __name__ == "__main__":
    unittest.main()
