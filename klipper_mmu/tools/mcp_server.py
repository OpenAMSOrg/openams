#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Expose this checkout's reusable tools to an MCP client as typed tools.

An MCP client sees these as its own tools, so it builds, tests and debugs with
the repository's own helpers instead of ad hoc shell commands and temporary
prints; a client whose sandbox cannot run commands still can. Start the
server from the checkout it should serve -- one server, one checkout, and a
worktree starts its own -- with the persistent interpreter that has the
`mcp` 2.x SDK the system python3 lacks:

    ~/.local/share/klipper-mmu-mcp/bin/python tools/mcp_server.py [--root DIR]

Register it with each harness against that interpreter (the paths are
examples; substitute the real checkout path):

    qwen mcp add klipper-mmu <python> <checkout>/tools/mcp_server.py --root <checkout>
    claude mcp add klipper-mmu -- <python> <checkout>/tools/mcp_server.py --root <checkout>
    codex mcp add klipper-mmu -- <python> <checkout>/tools/mcp_server.py --root <checkout>
    agy mcp add klipper-mmu <python> <checkout>/tools/mcp_server.py --root <checkout>

The server speaks stdio. The plain functions below are testable on their own
with the system python3; only `main` imports the SDK.
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

BUILD_TIMEOUT = 900
DEFAULT_TIMEOUT = 300
OUTPUT_LINES = 200
ASAN_OPTIONS = "abort_on_error=1:halt_on_error=1:detect_leaks=0"
UBSAN_OPTIONS = "halt_on_error=1:abort_on_error=1:print_stacktrace=1"


def _discover_root():
    """KLIPPER_MMU_ROOT when set (harnesses that start one global server, such as
    agy, do not start it in the agent's worktree), else the git top level of the
    directory the server was started in, else that directory."""
    if os.environ.get("KLIPPER_MMU_ROOT"):
        return Path(os.environ["KLIPPER_MMU_ROOT"]).expanduser().resolve()
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, cwd=os.getcwd(), timeout=30)
        if top.returncode == 0 and top.stdout.strip():
            return Path(top.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.cwd().resolve()


ROOT = _discover_root()

INSTRUCTIONS = (
    "Use these tools instead of ad hoc shell commands, make invocations and "
    "temporary printf/rebuild cycles: they run the repository's own test and "
    "debug helpers with the right settings and return short, capped output. "
    "Start with test_summary to see which native tests fail, run_suite for a "
    "full suite, and run_test to rerun one built binary with sanitizers. Ask "
    "gdb_why when a test fails, gdb_trace or gdb_stop for a value or location, "
    "gdb_inspect for a static look at an ELF, and masked_reach for what an "
    "interrupt root reaches in a firmware image. Paths outside the served "
    "checkout (and, for ELFs, its sibling build directory) are refused."
)


def _list(value):
    """Normalize a repeatable argument: None, a newline-separated string, or a sequence."""
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.splitlines() if item.strip()]
    return [str(item) for item in value]


def _inside(path, base):
    if base is None:
        return False
    path, base = os.path.realpath(path), os.path.realpath(base)
    return path == base or path.startswith(base + os.sep)


def _cap(text):
    lines = (text or "").splitlines()
    if len(lines) <= OUTPUT_LINES:
        return lines
    return ["... %d earlier lines omitted" % (len(lines) - OUTPUT_LINES)] + lines[-OUTPUT_LINES:]


def _report(code, text):
    return "\n".join(["exit %s" % code] + _cap(text))


def _in_user_namespace():
    """True inside a user namespace (a sandbox), which a caller cannot fake."""
    try:
        with open("/proc/self/uid_map") as uid_map:
            return uid_map.read().split() != ["0", "0", "4294967295"]
    except OSError:
        return False


def _jail(argv):
    """The agent may have written the code this runs, and the server runs outside
    any agent sandbox, so every command is confined with bubblewrap: only the
    served checkout is writable, no network, no device nodes (no flashing), and
    $HOME hidden except ~/.local (tools). None when bwrap is missing."""
    if os.environ.get("KLIPPER_MMU_SANDBOX") == "bwrap" and _in_user_namespace():
        # Already inside a sandbox that confines everything (Ubuntu denies
        # nested user namespaces, so bwrap cannot run again).
        return list(argv)
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return None
    home = os.path.expanduser("~")
    jail = [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--tmpfs", "/tmp", "--tmpfs", home]
    readable = [os.path.join(home, ".local"), _project_build_dir()]
    common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            capture_output=True, text=True, cwd=str(ROOT)).stdout.strip()
    readable.append(common or None)  # git metadata, read-only, for worktrees
    for path in readable:
        if path and os.path.exists(path):
            jail += ["--ro-bind", str(path), str(path)]
    if _is_main_checkout():
        # Agents work in worktrees; in the maintainer's checkout only build/ is
        # writable, so a rewritten Makefile cannot change tracked files.
        (ROOT / "build").mkdir(exist_ok=True)
        jail += ["--ro-bind", str(ROOT), str(ROOT),
                 "--bind", str(ROOT / "build"), str(ROOT / "build")]
    else:
        jail += ["--bind", str(ROOT), str(ROOT)]
    return jail + ["--chdir", str(ROOT), "--unshare-net",
                   "--unshare-ipc", "--unshare-pid", "--die-with-parent"] + list(argv)


def _is_main_checkout():
    """True when ROOT is the repository's main working tree, not a worktree."""
    dirs = [subprocess.run(["git", "rev-parse", "--path-format=absolute", flag],
                           capture_output=True, text=True, cwd=str(ROOT)).stdout.strip()
            for flag in ("--git-dir", "--git-common-dir")]
    return bool(dirs[0]) and dirs[0] == dirs[1]


def _run(argv, timeout, env=None):
    """Run a fixed argv (no shell) in the jail with cwd = ROOT and report
    "exit <code>" plus output."""
    proc_env = None
    if env:
        proc_env = os.environ.copy()
        proc_env.update(env)
    jailed = _jail(argv)
    if jailed is None:
        return _report(126, "refused: bubblewrap (bwrap) is required to run code for an agent")
    try:
        proc = subprocess.run(jailed, cwd=str(ROOT), capture_output=True, text=True,
                              timeout=timeout, env=proc_env)
    except subprocess.TimeoutExpired as exc:
        return _report("timeout", _join(exc.stdout, exc.stderr))
    except OSError as exc:
        return _report(127, "%s: %s" % (argv[0], exc))
    return _report(proc.returncode, _join(proc.stdout, proc.stderr))


def _join(stdout, stderr):
    parts = [part.rstrip("\n") for part in (stdout or "", stderr or "") if part]
    return "\n".join(parts)


def _tool(name):
    return str(ROOT / "tools" / name)


def _resolve_program(program):
    """A program that run_test and the running gdb modes may execute, or an error."""
    if not program or not str(program).strip():
        return None, "refused: no program given"
    real = Path(program).expanduser().resolve()
    if not _inside(real, ROOT):
        return None, "refused: %s is outside the served checkout %s" % (program, ROOT)
    if "build" not in real.relative_to(ROOT).parts[:-1]:
        return None, ("refused: %s is not inside a build directory under %s "
                      "(build the test binaries there first)" % (program, ROOT))
    if not real.is_file():
        return None, "refused: %s is not a file" % program
    if not os.access(real, os.X_OK):
        return None, "refused: %s is not executable" % program
    return real, None


def _project_build_dir():
    """The shared build directory where release images live, outside this
    checkout: KLIPPER_MMU_BUILD_ROOT, or None when none is set. It is mounted
    read-only in the jail. This checkout's own build/ is a different place:
    the jail's one writable directory."""
    root = os.environ.get("KLIPPER_MMU_BUILD_ROOT")
    if not root:
        return None
    path = Path(root).expanduser().resolve()
    return None if path == (ROOT / "build").resolve() else path


def _resolve_elf(elf):
    """An ELF that gdb_inspect or masked_reach may read, or an error."""
    if not elf or not str(elf).strip():
        return None, "refused: no ELF given"
    real = Path(elf).expanduser().resolve()
    build = _project_build_dir()
    if not (_inside(real, ROOT) or _inside(real, build)):
        return None, ("refused: %s is outside the served checkout %s and the release "
                      "build directory %s" % (elf, ROOT, build))
    if not real.is_file():
        return None, "refused: %s is not a file" % elf
    return real, None


def _suite_names():
    names = ["native", "sim"]
    tests = ROOT / "tests"
    if tests.is_dir():
        names += sorted(entry.name for entry in tests.iterdir()
                        if entry.is_dir() and (entry / "Makefile").is_file())
    return names


def test_summary():
    """Build every native test and print one line per test (`ok` or its first failure).

    Use it first after a change: it is the shortest view of which native tests
    fail. Builds under the served checkout with a 900 s timeout and returns
    "exit <code>" plus the last 200 output lines."""
    return _run(["make", "test-summary"], BUILD_TIMEOUT)


def run_suite(name):
    """Run a whole test suite: `native` (`make test`), `sim` (`make -C sim test test-loop`),
    or a directory `tests/<name>` that has a Makefile (`make -f tests/<name>/Makefile test`).

    Use it after test_summary to confirm a suite is green. Any other name is
    refused with the list of valid names; build and simulation suites get a
    900 s timeout, and the result is "exit <code>" plus the last 200 lines."""
    name = (name or "").strip()
    if name == "native":
        argv = ["make", "test"]
    elif name == "sim":
        argv = ["make", "-C", "sim", "test", "test-loop"]
    elif name in _suite_names():
        argv = ["make", "-f", "tests/%s/Makefile" % name, "test"]
    else:
        return "refused: unknown suite %r; valid names: %s" % (name, ", ".join(_suite_names()))
    return _run(argv, BUILD_TIMEOUT)


def run_test(program, args=None):
    """Run one already-built test binary with ASAN and UBSAN set to abort on error.

    Use it to rerun a single failure quickly, with `args` passed to the program
    as written. `program` must resolve to an executable file inside a `build/`
    directory under the served checkout; other paths are refused, and the
    result is "exit <code>" plus the last 200 output lines (300 s timeout)."""
    real, error = _resolve_program(program)
    if error:
        return error
    argv = [str(real)] + _list(args)
    return _run(argv, DEFAULT_TIMEOUT,
                env={"ASAN_OPTIONS": ASAN_OPTIONS, "UBSAN_OPTIONS": UBSAN_OPTIONS})


def gdb_why(program, args=None, prints=None):
    """Answer why a native test fails in one gdb batch run: signal or exit code, the
    assertion or sanitizer message, the repository backtrace, locals and each printed value.

    Use it instead of adding printf and rebuilding. `args` go to the program and
    `prints` are repeatable expressions for the innermost repository frame;
    `program` follows run_test's build-directory rule (300 s timeout)."""
    real, error = _resolve_program(program)
    if error:
        return error
    argv = [sys.executable, _tool("gdb_probe.py"), "why", "--root", str(ROOT)]
    for expr in _list(prints):
        argv += ["--print", expr]
    return _run(argv + ["--", str(real)] + _list(args), DEFAULT_TIMEOUT)


def gdb_trace(program, args=None, at=None, show=None, condition=None, max_hits=50):
    """Print the named values at every hit of a location while the program runs.

    `at` lists `file:line` or function breakpoints and `show` the expressions to
    print; `condition` restricts hits, and tracing stops after `max_hits`.
    `program` follows run_test's build-directory rule (300 s timeout)."""
    real, error = _resolve_program(program)
    if error:
        return error
    argv = [sys.executable, _tool("gdb_probe.py"), "trace", "--root", str(ROOT)]
    for loc in _list(at):
        argv += ["--at", loc]
    for expr in _list(show):
        argv += ["--show", expr]
    if condition:
        argv += ["--if", str(condition)]
    argv += ["--max", str(int(max_hits))]
    return _run(argv + ["--", str(real)] + _list(args), DEFAULT_TIMEOUT)


def gdb_stop(program, args=None, at=None, condition=None, hit=1, show=None):
    """Stop at the K-th hit of a location and print the backtrace, locals and values.

    `at` lists `file:line` or function breakpoints, `hit` selects which hit
    (default 1), `condition` filters hits, and `show` lists expressions to
    print. `program` follows run_test's build-directory rule (300 s timeout)."""
    real, error = _resolve_program(program)
    if error:
        return error
    argv = [sys.executable, _tool("gdb_probe.py"), "stop", "--root", str(ROOT)]
    for loc in _list(at):
        argv += ["--at", loc]
    for expr in _list(show):
        argv += ["--show", expr]
    if condition:
        argv += ["--if", str(condition)]
    argv += ["--hit", str(int(hit))]
    return _run(argv + ["--", str(real)] + _list(args), DEFAULT_TIMEOUT)


def gdb_inspect(elf, show=None):
    """Evaluate expressions in an ELF statically, without running it (ARM firmware too).

    `show` lists gdb expressions or commands such as `ptype x` or `info line f`,
    and `elf` must be inside the served checkout or its sibling release build
    directory; other paths are refused. Returns "exit <code>" plus output (300 s)."""
    real, error = _resolve_elf(elf)
    if error:
        return error
    argv = [sys.executable, _tool("gdb_probe.py"), "inspect", "--root", str(ROOT)]
    for expr in _list(show):
        argv += ["--show", expr]
    return _run(argv + [str(real)], DEFAULT_TIMEOUT)


def masked_reach(elf, roots=None):
    """Measure what interrupt (masked) roots reach in a firmware image: functions,
    instructions, float/libm and VFP sites, 64-bit divides, indirect calls and the
    longest call chain.

    `roots` lists interrupt handler symbols to start from, and `elf` must be
    inside the served checkout or its sibling release build directory; other
    paths are refused. Returns "exit <code>" plus output (300 s)."""
    real, error = _resolve_elf(elf)
    if error:
        return error
    names = _list(roots)
    if not names:
        return "refused: masked_reach needs at least one root function name"
    return _run([sys.executable, _tool("masked_reach.py"), str(real)] + names, DEFAULT_TIMEOUT)


TOOL_FUNCTIONS = (test_summary, run_suite, run_test, gdb_why, gdb_trace, gdb_stop,
                  gdb_inspect, masked_reach)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="mcp_server.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", default=None, metavar="DIR",
                        help="checkout to serve (default: the git top level of the current directory)")
    args = parser.parse_args(argv)
    global ROOT
    if args.root:
        ROOT = Path(args.root).expanduser().resolve()
    if not ROOT.is_dir():
        print("mcp_server: %s is not a directory" % ROOT, file=sys.stderr)
        return 2
    from mcp.server.mcpserver import MCPServer
    server = MCPServer(name="klipper-mmu", instructions=INSTRUCTIONS)
    for function in TOOL_FUNCTIONS:
        server.tool()(function)
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
