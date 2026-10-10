# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the per-module loader that tools/check.py runs Python suites with."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import check  # noqa: E402


def load(source):
    """Run the loader on a module with this source; return (code, stdout)."""
    with tempfile.TemporaryDirectory() as tmp:
        module = Path(tmp) / "test_sample.py"
        module.write_text(source)
        proc = subprocess.run([sys.executable, "-B", "-c", check.LOADER, str(ROOT), str(module)],
                              capture_output=True, text=True, timeout=60)
        return proc.returncode, proc.stdout


class LoaderTest(unittest.TestCase):
    def test_passing_module_is_zero(self):
        code, _ = load("import unittest\nclass T(unittest.TestCase):\n"
                       "    def test_a(self): pass\n")
        self.assertEqual(code, 0)

    def test_failing_module_is_one(self):
        code, _ = load("import unittest\nclass T(unittest.TestCase):\n"
                       "    def test_a(self): self.fail('x')\n")
        self.assertEqual(code, 1)

    def test_module_with_no_tests_fails(self):
        code, _ = load("X = 1\n")
        self.assertEqual(code, 2)

    def test_module_level_skip_is_skipped_with_reason(self):
        code, out = load("import unittest\n"
                         "def setUpModule(): raise unittest.SkipTest('no gdb here')\n"
                         "class T(unittest.TestCase):\n    def test_a(self): pass\n")
        self.assertEqual(code, check.SKIPPED_EXIT)
        self.assertIn(check.SKIP_MARKER + "no gdb here", out)

    def test_every_test_skipped_is_skipped(self):
        code, out = load("import unittest\nclass T(unittest.TestCase):\n"
                         "    @unittest.skip('nope')\n    def test_a(self): pass\n"
                         "    @unittest.skip('nope')\n    def test_b(self): pass\n")
        self.assertEqual(code, check.SKIPPED_EXIT)
        self.assertIn("nope", out)

    def test_partly_skipped_module_passes(self):
        code, _ = load("import unittest\nclass T(unittest.TestCase):\n"
                       "    @unittest.skip('nope')\n    def test_a(self): pass\n"
                       "    def test_b(self): pass\n")
        self.assertEqual(code, 0)

    def test_failure_beside_skips_still_fails(self):
        code, _ = load("import unittest\nclass T(unittest.TestCase):\n"
                       "    @unittest.skip('nope')\n    def test_a(self): pass\n"
                       "    def test_b(self): self.fail('x')\n")
        self.assertEqual(code, 1)


class SkipReasonTest(unittest.TestCase):
    def test_reason_is_read_from_the_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "out"
            log.write_text("Ran 0 tests\n" + check.SKIP_MARKER + "no unicorn\n")
            self.assertEqual(check._skip_reason(log), "no unicorn")
            self.assertEqual(check._skip_reason(Path(tmp) / "missing"), "all tests skipped")


if __name__ == "__main__":
    unittest.main()
