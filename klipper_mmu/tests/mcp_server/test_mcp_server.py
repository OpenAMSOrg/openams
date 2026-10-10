# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/mcp_server.py. Stdlib only, so the system python3 can run them:

    python3 -m unittest tests/mcp_server/test_mcp_server.py

The MCP smoke test is skipped unless the persistent SDK interpreter exists.
The C fixture is compiled into build/mcp_server_test/ and removed afterwards.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import mcp_server  # noqa: E402

SDK_PYTHON = Path(os.environ.get(
    "KLIPPER_MMU_MCP_PYTHON", "~/.local/share/klipper-mmu-mcp/bin/python")).expanduser()
CC = shutil.which("cc")
GDB = shutil.which("gdb")


def _jail_usable():
    """True when bwrap exists and can create the user namespace the jail needs.
    Some CI runners and sandboxes forbid that, so the probe runs once."""
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return False
    try:
        probe = subprocess.run([bwrap, "--unshare-user", "--ro-bind", "/", "/", "true"],
                               capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


JAIL_USABLE = _jail_usable()
NEEDS_JAIL = unittest.skipUnless(
    JAIL_USABLE, "bubblewrap is missing or cannot create user namespaces")
BUILD = ROOT / "build" / "mcp_server_test"
PROGRAM = BUILD / "mcp_fixture"
TOOL_NAMES = ["gdb_inspect", "gdb_stop", "gdb_trace", "gdb_why", "masked_reach",
              "run_suite", "run_test", "test_summary"]

C_SOURCE = """\
#include <assert.h>
#include <stdio.h>
#include <string.h>

int main(int argc, char **argv) {
    if (argc > 1 && strcmp(argv[1], "assert") == 0) {
        int value = 3;
        assert(value == 4);
    }
    if (argc > 1 && strcmp(argv[1], "many") == 0) {
        for (int i = 0; i < 1000; i++) {
            printf("line %d\\n", i);
        }
        return 0;
    }
    printf("hello from exit\\n");
    return 7;
}
"""

CLIENT_SOURCE = """\
import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    params = StdioServerParameters(command=sys.argv[1], args=[sys.argv[2], "--root", sys.argv[3]])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("TOOLS:" + ",".join(sorted(tool.name for tool in tools.tools)))


asyncio.run(main())
"""


def setUpModule():
    if not CC:
        raise unittest.SkipTest("cc is required")
    mcp_server.ROOT = ROOT
    shutil.rmtree(BUILD, ignore_errors=True)
    BUILD.mkdir(parents=True)
    source = BUILD / "mcp_fixture.c"
    source.write_text(C_SOURCE)
    subprocess.run([CC, "-g", "-O0", "-o", str(PROGRAM), str(source)], check=True, timeout=120)


def tearDownModule():
    shutil.rmtree(BUILD, ignore_errors=True)


class PathRulesTest(unittest.TestCase):
    def test_run_suite_refuses_unknown_and_lists_valid(self):
        out = mcp_server.run_suite("no-such-suite")
        self.assertIn("refused", out)
        for name in ("native", "sim", "klipper_adc"):
            self.assertIn(name, out)

    def test_run_test_refuses_outside_and_non_executable(self):
        outside = mcp_server.run_test("/bin/echo", [])
        self.assertIn("refused", outside)
        not_build = mcp_server.run_test(str(ROOT / "Makefile"), [])
        self.assertIn("refused", not_build)
        plain = BUILD / "not_executable"
        plain.write_text("x")
        plain.chmod(0o644)
        self.assertIn("refused", mcp_server.run_test(str(plain), []))

    def test_masked_reach_refuses_outside(self):
        out = mcp_server.masked_reach("/bin/ls", ["km_controller_timer_event"])
        self.assertIn("refused", out)

    def test_gdb_inspect_refuses_outside(self):
        out = mcp_server.gdb_inspect("/bin/ls", ["sizeof(int)"])
        self.assertIn("refused", out)


@NEEDS_JAIL
class RunTestTest(unittest.TestCase):
    def test_returns_exit_and_output(self):
        out = mcp_server.run_test(str(PROGRAM), [])
        self.assertTrue(out.startswith("exit 7"), out)
        self.assertIn("hello from exit", out)

    def test_output_is_capped(self):
        out = mcp_server.run_test(str(PROGRAM), ["many"])
        lines = out.splitlines()
        self.assertLessEqual(len(lines), 1 + 1 + mcp_server.OUTPUT_LINES)
        self.assertIn("omitted", out)
        self.assertIn("line 999", out)
        self.assertNotIn("line 0\n", out)


@unittest.skipUnless(GDB, "gdb is required")
@NEEDS_JAIL
class JailTest(unittest.TestCase):
    def test_code_run_for_an_agent_is_confined(self):
        """A probe run through the server cannot write outside the checkout, reach
        the network, see device nodes or read the user's SSH keys."""
        # Beside the checkout: the jail's throwaway home may accept the write,
        # but the host file system must not change.
        outside = ROOT.parent / ("mcp_jail_probe_%d" % os.getpid())
        # The shared build directory (release images), when one is configured,
        # is mounted read-only. Without one there is nothing to protect: probe a
        # path that cannot exist.
        shared_dir = mcp_server._project_build_dir()
        shared = (shared_dir or Path("/nonexistent")) / ("mcp_jail_probe_%d" % os.getpid())
        probe = BUILD / "jail_probe.sh"
        probe.write_text(
            "#!/bin/sh\n"
            "touch \"$1\" 2>/dev/null\n"
            "touch \"$3\" 2>/dev/null && echo 'VIOLATION wrote the shared build directory'\n"
            "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 53), 2)\" "
            "2>/dev/null && echo 'VIOLATION network'\n"
            "ls /dev | grep -q -E '^(ttyACM|ttyUSB|sd[a-z])' && echo 'VIOLATION devices'\n"
            "test -e \"$HOME/.ssh\" && echo 'VIOLATION ssh keys'\n"
            "touch \"$2\" && echo 'wrote inside'\n"
            "echo done\n")
        probe.chmod(0o755)
        try:
            out = mcp_server.run_test(str(probe), [str(outside), str(BUILD / "inside"), str(shared)])
            self.assertIn("done", out)
            self.assertIn("wrote inside", out)
            self.assertNotIn("VIOLATION", out)
            self.assertFalse(outside.exists())
            self.assertFalse(shared.exists())
        finally:
            for path in (outside, shared):
                if path.exists():
                    path.unlink()

    def test_a_configured_shared_build_directory_is_read_only(self):
        """KLIPPER_MMU_BUILD_ROOT names the shared release-image directory; code
        run for an agent can read it but not write it."""
        shared_dir = Path(tempfile.mkdtemp(prefix="mcp_shared_build_"))
        target = shared_dir / "jail_probe"
        probe = BUILD / "jail_shared_probe.sh"
        probe.write_text("#!/bin/sh\n"
                         "test -d \"$1\" && echo 'readable'\n"
                         "touch \"$1/jail_probe\" 2>/dev/null && echo 'VIOLATION wrote the shared build directory'\n"
                         "echo done\n")
        probe.chmod(0o755)
        saved = os.environ.get("KLIPPER_MMU_BUILD_ROOT")
        os.environ["KLIPPER_MMU_BUILD_ROOT"] = str(shared_dir)
        try:
            out = mcp_server.run_test(str(probe), [str(shared_dir)])
            self.assertIn("readable", out)
            self.assertIn("done", out)
            self.assertNotIn("VIOLATION", out)
            self.assertFalse(target.exists())
        finally:
            if saved is None:
                os.environ.pop("KLIPPER_MMU_BUILD_ROOT", None)
            else:
                os.environ["KLIPPER_MMU_BUILD_ROOT"] = saved
            if target.exists():
                target.unlink()
            shared_dir.rmdir()


    def test_main_checkout_is_read_only_except_build(self):
        """Agents work in worktrees; served from the maintainer's checkout, a
        probe (or a rewritten Makefile) can write build/ but no tracked path."""
        if not mcp_server._is_main_checkout():
            self.skipTest("served from a worktree, which is writable by design")
        tracked = ROOT / ("mcp_main_probe_%d" % os.getpid())
        probe = BUILD / "main_probe.sh"
        probe.write_text("#!/bin/sh\ntouch \"$1\" 2>/dev/null && echo 'VIOLATION wrote the checkout'\n"
                         "touch \"$2\" && echo 'wrote build'\n")
        probe.chmod(0o755)
        try:
            out = mcp_server.run_test(str(probe), [str(tracked), str(BUILD / "main_inside")])
            self.assertIn("wrote build", out)
            self.assertNotIn("VIOLATION", out)
            self.assertFalse(tracked.exists())
        finally:
            if tracked.exists():
                tracked.unlink()

    def test_root_from_environment(self):
        """A harness that starts one global server names the worktree to serve."""
        os.environ["KLIPPER_MMU_ROOT"] = str(BUILD)
        try:
            self.assertEqual(mcp_server._discover_root(), BUILD.resolve())
        finally:
            del os.environ["KLIPPER_MMU_ROOT"]


class NestedSandboxTest(unittest.TestCase):
    def test_marker_alone_does_not_disable_the_jail(self):
        """Outside a user namespace the environment marker is not trusted."""
        if mcp_server._in_user_namespace():
            self.skipTest("already inside a user namespace")
        os.environ["KLIPPER_MMU_SANDBOX"] = "bwrap"
        try:
            jailed = mcp_server._jail(["true"])
        finally:
            del os.environ["KLIPPER_MMU_SANDBOX"]
        self.assertTrue(jailed is None or jailed[0].endswith("bwrap"))


class GdbTest(unittest.TestCase):
    @unittest.skipUnless(GDB, "gdb is required")
    @NEEDS_JAIL
    def test_why_reports_the_assertion(self):
        out = mcp_server.gdb_why(str(PROGRAM), ["assert"])
        self.assertIn("Assertion", out)

    def test_why_refuses_outside(self):
        self.assertIn("refused", mcp_server.gdb_why("/bin/echo", []))


@unittest.skipUnless(SDK_PYTHON.exists(), "persistent MCP SDK interpreter is not installed")
class ServerSmokeTest(unittest.TestCase):
    def test_tools_list_names_all_eight(self):
        proc = subprocess.run(
            [str(SDK_PYTHON), "-c", CLIENT_SOURCE, str(SDK_PYTHON),
             str(ROOT / "tools" / "mcp_server.py"), str(ROOT)],
            cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = next(line for line in proc.stdout.splitlines() if line.startswith("TOOLS:"))
        self.assertEqual(sorted(line.split(":", 1)[1].split(",")), TOOL_NAMES)


if __name__ == "__main__":
    unittest.main()
