# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/gdb_probe.py: run the tool against the fixture.c binary.

Run with: python3 -m unittest tests/gdb_probe/test_gdb_probe.py
Skipped entirely when gdb or cc is not installed.
"""
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "gdb_probe.py"
FIXTURE_C = Path(__file__).resolve().parent / "fixture.c"
FORK_FIXTURE_C = Path(__file__).resolve().parent / "fixture_fork.c"
GDB = shutil.which("gdb")
CC = shutil.which("cc")

WORK = None
FIXTURE = None
TRACE_LINE = None
ASSERT_LINE = None
FORK_FIXTURE = None
FORK_TRACE_LINE = None


def setUpModule():
    global WORK, FIXTURE, TRACE_LINE, ASSERT_LINE, FORK_FIXTURE, FORK_TRACE_LINE
    if not GDB or not CC:
        raise unittest.SkipTest("gdb and cc are required")
    WORK = tempfile.mkdtemp(prefix="gdb-probe-test-")
    FIXTURE = str(Path(WORK) / "fixture")
    subprocess.run([CC, "-g", "-O0", "-o", FIXTURE, str(FIXTURE_C)],
                   check=True, timeout=120)
    source = FIXTURE_C.read_text().splitlines()
    TRACE_LINE = next(n for n, line in enumerate(source, 1) if "probe-trace" in line)
    ASSERT_LINE = next(n for n, line in enumerate(source, 1) if "probe-assert" in line)
    FORK_FIXTURE = str(Path(WORK) / "fixture_fork")
    subprocess.run([CC, "-g", "-O0", "-o", FORK_FIXTURE, str(FORK_FIXTURE_C)],
                   check=True, timeout=120)
    fork_source = FORK_FIXTURE_C.read_text().splitlines()
    FORK_TRACE_LINE = next(n for n, line in enumerate(fork_source, 1) if "probe-fork-trace" in line)


def tearDownModule():
    if WORK:
        shutil.rmtree(WORK, ignore_errors=True)


def probe(*args, timeout=120):
    return subprocess.run([sys.executable, str(TOOL), *args], cwd=str(ROOT),
                          capture_output=True, text=True, timeout=timeout)


def split_output(text):
    """(tool lines, program's own output lines) separated at the output header."""
    tool, program, in_program = [], [], False
    for line in text.splitlines():
        if line.startswith("probe: program output"):
            in_program = True
            tool.append(line)
        elif in_program and not line.startswith("probe: "):
            program.append(line)
        else:
            tool.append(line)
    return tool, program


def hit_lines(text, location):
    prefix = "probe: %s #" % location
    return [line for line in text.splitlines() if line.startswith(prefix)]


class GdbProbeTest(unittest.TestCase):
    def assert_tool_lines_prefixed(self, result):
        tool, _ = split_output(result.stdout)
        self.assertTrue(tool, "no output: " + result.stderr)
        for line in tool:
            self.assertTrue(line.startswith("probe: "), line)

    def test_why_reports_the_assertion(self):
        result = probe("why", "--print", "st.total", "--", FIXTURE, "fail")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_tool_lines_prefixed(result)
        self.assertIn("probe: signal SIGABRT", result.stdout)
        self.assertIn("Assertion", result.stdout)
        self.assertIn("st.total < 30", result.stdout)
        self.assertIn("probe: main(", result.stdout)
        self.assertIn("at tests/gdb_probe/fixture.c:%d" % ASSERT_LINE, result.stdout)
        self.assertIn("probe: locals: ", result.stdout)
        self.assertIn("i=8", result.stdout)
        self.assertIn("probe: st.total = 36", result.stdout)
        self.assertNotIn("no failure", result.stdout)
        self.assertNotIn("abort.c", result.stdout)

    def test_why_clean_run_says_no_failure(self):
        result = probe("why", "--", FIXTURE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_tool_lines_prefixed(result)
        self.assertIn("probe: exit code 0", result.stdout)
        self.assertIn("probe: no failure", result.stdout)
        _, program = split_output(result.stdout)
        self.assertIn("done total=45", program)

    def test_trace_reports_every_hit(self):
        location = "fixture.c:%d" % TRACE_LINE
        result = probe("trace", "--at", location, "--show", "i", "--show", "st.step",
                       "--", FIXTURE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_tool_lines_prefixed(result)
        hits = hit_lines(result.stdout, location)
        self.assertEqual(len(hits), 10, result.stdout)
        self.assertIn("i=0; st.step=0", hits[0])
        self.assertIn("i=9; st.step=9", hits[-1])
        self.assertIn("probe: exit code 0", result.stdout)
        _, program = split_output(result.stdout)
        self.assertIn("done total=45", program)

    def test_trace_if_limits_hits(self):
        location = "fixture.c:%d" % TRACE_LINE
        result = probe("trace", "--at", location, "--show", "i", "--if", "i >= 8",
                       "--", FIXTURE)
        hits = hit_lines(result.stdout, location)
        self.assertEqual(len(hits), 2, result.stdout)
        self.assertIn("i=8", hits[0])
        self.assertIn("i=9", hits[1])

    def test_trace_max_stops_tracing_but_finishes(self):
        location = "fixture.c:%d" % TRACE_LINE
        result = probe("trace", "--at", location, "--show", "i", "--max", "3",
                       "--", FIXTURE)
        self.assertEqual(len(hit_lines(result.stdout, location)), 3, result.stdout)
        self.assertIn("probe: trace: stopped after 3 hits", result.stdout)
        _, program = split_output(result.stdout)
        self.assertIn("done total=45", program)

    def test_trace_without_follow_child_misses_the_forked_child(self):
        location = "fixture_fork.c:%d" % FORK_TRACE_LINE
        result = probe("trace", "--at", location, "--show", "marker",
                       "--", FORK_FIXTURE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_tool_lines_prefixed(result)
        self.assertEqual(hit_lines(result.stdout, location), [], result.stdout)
        self.assertIn("probe: exit code 0", result.stdout)

    def test_trace_follow_child_reports_the_forked_child(self):
        location = "fixture_fork.c:%d" % FORK_TRACE_LINE
        result = probe("trace", "--at", location, "--show", "marker",
                       "--follow-child", "--", FORK_FIXTURE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_tool_lines_prefixed(result)
        hits = hit_lines(result.stdout, location)
        self.assertEqual(len(hits), 1, result.stdout)
        self.assertIn("marker=42", hits[0])

    def test_follow_child_is_documented(self):
        top = probe("--help")
        self.assertEqual(top.returncode, 0, top.stderr)
        self.assertIn("--follow-child", top.stdout)
        for mode in ("why", "trace", "stop"):
            result = probe(mode, "--help")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--follow-child", result.stdout)
            self.assertIn("follow-fork-mode child", result.stdout)

    def test_stop_at_the_third_hit(self):
        location = "fixture.c:%d" % TRACE_LINE
        result = probe("stop", "--at", location, "--hit", "3", "--show", "i",
                       "--", FIXTURE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_tool_lines_prefixed(result)
        self.assertIn("probe: stopped at %s (hit 3)" % location, result.stdout)
        self.assertIn("at tests/gdb_probe/fixture.c:%d" % TRACE_LINE, result.stdout)
        self.assertIn("probe: locals: ", result.stdout)
        self.assertIn("i=2", result.stdout)
        self.assertIn("probe: i = 2", result.stdout)
        self.assertNotIn("done total=45", result.stdout)

    def test_stop_at_a_function(self):
        result = probe("stop", "--at", "main", "--show", "argc", "--", FIXTURE)
        self.assert_tool_lines_prefixed(result)
        self.assertIn("probe: main(", result.stdout)
        self.assertIn("at tests/gdb_probe/fixture.c:", result.stdout)
        self.assertIn("probe: argc = 1", result.stdout)

    def test_inspect_reports_layout_statically(self):
        size = struct.calcsize("il")
        result = probe("inspect", FIXTURE, "--show", "sizeof(struct state)",
                       "--show", "ptype struct state")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_tool_lines_prefixed(result)
        self.assertIn("probe: sizeof(struct state) = %d" % size, result.stdout)
        self.assertIn("int step;", result.stdout)
        self.assertIn("long total;", result.stdout)

    def test_why_reports_a_nonzero_exit(self):
        result = probe("why", "--print", "code", "--", FIXTURE, "exit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_tool_lines_prefixed(result)
        self.assertIn("probe: exit code 3", result.stdout)
        self.assertIn("probe: finish(", result.stdout)
        self.assertIn("probe: main(", result.stdout)
        self.assertIn("probe: code = 3", result.stdout)
        self.assertNotIn("no failure", result.stdout)

    def test_unknown_location_is_reported(self):
        result = probe("trace", "--at", "fixture.c:99999", "--show", "i", "--", FIXTURE)
        self.assert_tool_lines_prefixed(result)
        self.assertIn("no matching code location", result.stdout)

    def test_timeout_is_reported(self):
        result = probe("why", "--timeout", "1", "--", FIXTURE, "spin", timeout=60)
        self.assertEqual(result.returncode, 1)
        self.assert_tool_lines_prefixed(result)
        self.assertIn("probe: timeout after 1s", result.stdout)

    def test_max_lines_caps_the_output(self):
        result = probe("why", "--max-lines", "2", "--", FIXTURE, "fail")
        self.assertIn("truncated", result.stdout)
        self.assertLessEqual(len(result.stdout.splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
