#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""One-shot gdb answers for native C tests, without editing the source.

Ask the debugger instead of adding printf, rebuilding and rerunning. This tool
runs a program (or reads an ELF) under gdb in batch mode and prints a short
prefixed report. It never modifies sources; reach for it whenever a test's
behavior, a crash or a memory layout needs a closer look.

    tools/gdb_probe.py why [--print EXPR]... [--follow-child] -- PROGRAM [ARGS...]
        Run until the first failure (SIGABRT/SIGSEGV/SIGFPE/SIGBUS or a
        nonzero exit). Print the signal, the assertion or sanitizer message,
        the backtrace limited to repository source files, the innermost
        repository frame's locals, and each --print value. A clean run prints
        the exit code and "no failure".
        Example: tools/gdb_probe.py why --print st.total -- ./fixture fail

    tools/gdb_probe.py trace --at LOC [--at LOC]... --show EXPR...
        [--if COND] [--max N] [--follow-child] -- PROGRAM [ARGS...]
        At every hit of any --at LOC (file:line or function) where --if holds,
        print "<loc> #<n>: <expr>=<value>; ..." and continue. After --max hits
        (default 50) tracing stops and the program is allowed to finish.
        Example: tools/gdb_probe.py trace --at fixture.c:23 --show i -- ./fixture

    tools/gdb_probe.py stop --at LOC [--if COND] [--hit K] --show EXPR...
        [--follow-child] -- PROGRAM [ARGS...]
        At the K-th hit (default 1), print the repository backtrace, the frame
        locals and the --show values, then kill the program.
        Example: tools/gdb_probe.py stop --at fixture.c:23 --hit 3 --show i -- ./fixture

    tools/gdb_probe.py inspect ELF --show EXPR [--show EXPR]...
        Evaluate expressions (or gdb commands: "ptype x", "info line f",
        "p/x CONST") statically, without running anything. Uses gdb-multiarch
        when ELF is not a host executable (for example ARM firmware).
        Example: tools/gdb_probe.py inspect firmware.elf --show 'sizeof(struct state)'

Common options: --timeout SEC (default 120), --env KEY=VALUE (repeatable),
--max-lines N (default 200). Every line this tool prints starts with "probe: ";
the program's own output is shown last, as its final 20 lines. ASAN_OPTIONS and
UBSAN_OPTIONS default to aborting on error unless --env overrides them. gdb
runs with -nx -batch and debuginfod, pagination and confirmation disabled.

Suites that run each scenario in a fork()ed child (run_isolated-style) are
invisible by default, because gdb follows the parent. Passing --follow-child to
why, trace or stop sets "follow-fork-mode child" and "detach-on-fork on": the
parent keeps running without the debugger and gdb follows the child, so --at,
--print and --show see the scenario body. It does not follow every child in
turn; the first child that hits a breakpoint is the one reported.
"""
import argparse
import os
import platform
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def default_root():
    """Source paths are reported relative to this package, not to whatever
    repository encloses it: klipper_mmu may be checked out on its own or
    nested inside another repository, and the enclosing top level would
    prefix every reported path with the package directory name."""
    return str(ROOT)
PROGRAM_OUTPUT_LINES = 20
DEFAULT_ASAN = "abort_on_error=1:halt_on_error=1:detect_leaks=0"
DEFAULT_UBSAN = "halt_on_error=1:abort_on_error=1:print_stacktrace=1"
MESSAGES = ("Assertion", "runtime error", "Sanitizer", "ERROR:", "SUMMARY:")
FOLLOW_CHILD_HELP = ("follow a fork()ed child instead of the parent: gdb sets "
                     "follow-fork-mode child and detach-on-fork on, so the parent "
                     "keeps running without the debugger and the first child that "
                     "hits a breakpoint is the one reported (default: follow the parent)")
HOST_MACHINES = {"x86_64": 62, "i386": 3, "i686": 3, "aarch64": 183, "armv7l": 40,
                 "armv6l": 40, "riscv32": 243, "riscv64": 243, "s390x": 22,
                 "ppc64le": 21, "mips": 8}

BODY = r'''
STATE = {"exited": None, "signal": None, "stop": None, "hit": 0, "hits": 0, "exit_fail": None}


def emit(text):
    for line in str(text).splitlines() or [""]:
        print("probe: " + line)


def repo_rel(symtab):
    try:
        filename = symtab.fullname() or symtab.filename
    except Exception:
        filename = symtab.filename
    if not filename or not os.path.isabs(filename):
        return None
    path = os.path.realpath(filename)
    if path == ROOT or path.startswith(ROOT + os.sep):
        return os.path.relpath(path, ROOT)
    return None


def symbol_value(frame, sym):
    try:
        return sym.value(frame)
    except Exception:
        pass
    try:
        return frame.read_var(str(sym))
    except Exception as exc:
        return "<%s>" % exc


def symbols(frame, kind):
    found, seen = [], set()
    try:
        block = frame.block()
    except gdb.error:
        return found
    while block is not None:
        for sym in block:
            name = str(sym)
            if name in seen:
                continue
            try:
                wanted = sym.is_argument if kind == "args" else sym.is_variable and not sym.is_argument
            except Exception:
                wanted = False
            if not wanted:
                continue
            seen.add(name)
            found.append("%s=%s" % (name, symbol_value(frame, sym)))
        block = block.superblock
    return found


def frame_source(frame):
    try:
        sal = frame.find_sal()
    except gdb.error:
        return None, None
    if sal.symtab is None:
        return None, None
    return repo_rel(sal.symtab), sal.line


def frame_name(frame):
    try:
        return frame.name() or "??"
    except gdb.error:
        return "??"


def backtrace():
    stack, frame, depth = [], gdb.newest_frame(), 0
    while frame is not None and depth < 200:
        rel, line = frame_source(frame)
        stack.append((frame, frame_name(frame), rel, line))
        frame = frame.older()
        depth += 1
    repo = [i for i, item in enumerate(stack) if item[2]]
    if not repo:
        emit("... (no repository frame in the backtrace)")
        return None
    gap = False
    for frame, name, rel, line in stack[:repo[-1] + 1]:
        if rel is None:
            if not gap:
                emit("...")
                gap = True
            continue
        gap = False
        emit("%s(%s) at %s:%s" % (name, ", ".join(symbols(frame, "args")), rel, line))
    return stack[repo[0]][0]


def print_locals(frame):
    # Nested structs print as {...}: a firmware struct can span pages; ask for
    # a field with --print/--show instead.
    gdb.execute("set print max-depth 1")
    try:
        items = symbols(frame, "args") + symbols(frame, "locals")
    finally:
        gdb.execute("set print max-depth unlimited")
    emit("locals: " + ("; ".join(items) if items else "(none)"))


def print_values(exprs):
    for expr in exprs:
        try:
            emit("%s = %s" % (expr, gdb.parse_and_eval(expr)))
        except Exception as exc:
            emit("%s = <%s>" % (expr, exc))


def failure_message():
    text = ""
    for path in (OUT, ERR):
        try:
            with open(path, "r", errors="replace") as handle:
                text += handle.read()
        except OSError:
            pass
    matches = [line for line in text.splitlines() if any(word in line for word in MESSAGES)]
    for line in matches[-4:]:
        emit(line)


def on_stop(event):
    if isinstance(event, gdb.SignalEvent):
        STATE["signal"] = event.stop_signal


def on_exited(event):
    try:
        STATE["exited"] = event.exit_code
    except Exception:
        pass


class Hit(gdb.Breakpoint):
    def __init__(self, spec):
        super().__init__(spec)
        self.spec = spec
        self.count = 0
        self.cond_broken = False

    def stop(self):
        if COND is not None:
            try:
                if not gdb.parse_and_eval(COND):
                    return False
            except Exception as exc:
                if not self.cond_broken:
                    self.cond_broken = True
                    emit("%s: cannot evaluate --if %r: %s (disabled)" % (self.spec, COND, exc))
                    self.enabled = False
                return False
        if MODE == "stop":
            self.count += 1
            if self.count >= HIT:
                STATE["stop"] = self.spec
                STATE["hit"] = self.count
                return True
            return False
        STATE["hits"] += 1
        values = []
        for expr in SHOWS:
            try:
                values.append("%s=%s" % (expr, gdb.parse_and_eval(expr)))
            except Exception as exc:
                values.append("%s=<%s>" % (expr, exc))
        emit("%s #%d: %s" % (self.spec, STATE["hits"], "; ".join(values)))
        if STATE["hits"] >= MAXHITS:
            emit("trace: stopped after %d hits" % MAXHITS)
            for bp in BREAKPOINTS:
                bp.enabled = False
        return False


class ExitBreak(gdb.Breakpoint):
    def stop(self):
        try:
            code = int(gdb.parse_and_eval("status")) & 0xFF
        except Exception:
            return False
        if code:
            STATE["exit_fail"] = code
            return True
        return False


def run_program():
    if FOLLOW_CHILD:
        gdb.execute("set follow-fork-mode child")
        gdb.execute("set detach-on-fork on")
    line = "run"
    if ARGS:
        line += " " + " ".join(shlex.quote(arg) for arg in ARGS)
    line += " > %s 2> %s" % (shlex.quote(OUT), shlex.quote(ERR))
    try:
        gdb.execute(line)
    except gdb.error as exc:
        emit("run failed: %s" % exc)


def inspect_expr(expr):
    try:
        emit("%s = %s" % (expr, gdb.parse_and_eval(expr)))
        return
    except gdb.error:
        pass
    try:
        text = gdb.execute(expr, to_string=True).rstrip("\n")
        emit("%s:" % expr)
        if text:
            emit(text)
    except gdb.error as exc:
        emit("%s: %s" % (expr, exc))


def ending():
    if STATE["signal"] is not None:
        return "signal %s" % STATE["signal"]
    if STATE["exited"] is not None:
        return "exit code %d" % STATE["exited"]
    return None


def report_failure(exprs):
    failure_message()
    frame = backtrace()
    if frame is None:
        return
    frame.select()
    print_locals(frame)
    print_values(exprs)


gdb.events.stop.connect(on_stop)
gdb.events.exited.connect(on_exited)

if MODE == "inspect":
    for expr in SHOWS:
        inspect_expr(expr)
else:
    BREAKPOINTS = []
    for spec in ATS:
        try:
            bp = Hit(spec)
        except Exception as exc:
            emit("%s: cannot set breakpoint: %s" % (spec, exc))
            continue
        if not bp.locations:
            emit("%s: no matching code location (unknown or not loaded yet)" % spec)
        BREAKPOINTS.append(bp)
    if MODE == "why":
        for name in ("exit", "_exit"):
            try:
                ExitBreak(name)
            except Exception:
                pass
    if MODE == "stop" and not BREAKPOINTS:
        emit("no usable breakpoint; not running")
    else:
        run_program()
        if MODE == "why":
            if STATE["signal"] is not None:
                emit("signal %s" % STATE["signal"])
                report_failure(PRINTS)
            elif STATE["exit_fail"] is not None:
                emit("exit code %d" % STATE["exit_fail"])
                report_failure(PRINTS)
            elif STATE["exited"] is not None:
                emit("exit code %d" % STATE["exited"])
                if STATE["exited"] == 0:
                    emit("no failure")
                else:
                    report_failure(PRINTS)
            else:
                emit("program did not exit")
        elif MODE == "stop":
            if STATE["stop"] is not None:
                emit("stopped at %s (hit %d)" % (STATE["stop"], STATE["hit"]))
                frame = backtrace()
                if frame is not None:
                    frame.select()
                    print_locals(frame)
                    print_values(SHOWS)
                try:
                    gdb.execute("kill")
                except gdb.error:
                    pass
            else:
                emit("breakpoint never hit: %s" % ", ".join(ATS))
                emit(ending() or "program did not exit")
        else:
            emit(ending() or "program did not exit")
            if STATE["signal"] is not None or STATE["exit_fail"]:
                report_failure(SHOWS)
            elif STATE["exited"] not in (None, 0):
                report_failure(SHOWS)
'''


def script_text(config):
    lines = ["import gdb", "import os", "import shlex", ""]
    for key in sorted(config):
        lines.append("%s = %r" % (key, config[key]))
    lines.append("")
    lines.append(BODY)
    return "\n".join(lines)


def is_host_executable(path):
    try:
        with open(path, "rb") as handle:
            head = handle.read(20)
    except OSError:
        return False
    if len(head) < 20 or head[:4] != b"\x7fELF" or head[5] != 1:
        return False
    machine = struct.unpack("<H", head[18:20])[0]
    return machine == HOST_MACHINES.get(platform.machine())


def gdb_binary(mode, target):
    if mode == "inspect" and not is_host_executable(target):
        return shutil.which("gdb-multiarch") or shutil.which("gdb")
    return shutil.which("gdb")


def build_env(extra):
    env = os.environ.copy()
    env.setdefault("ASAN_OPTIONS", DEFAULT_ASAN)
    env.setdefault("UBSAN_OPTIONS", DEFAULT_UBSAN)
    for item in extra:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ValueError("--env wants KEY=VALUE, got %r" % item)
        env[key] = value
    return env


def add_follow_child(parser):
    parser.add_argument("--follow-child", action="store_true", help=FOLLOW_CHILD_HELP)


def build_parser():
    parser = argparse.ArgumentParser(prog="gdb_probe.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=None, metavar="DIR",
                        help="source tree whose frames count as the repository's "
                             "(default: this package's own directory)")
    common.add_argument("--timeout", type=float, default=120.0, metavar="SEC",
                        help="kill gdb after SEC seconds (default 120)")
    common.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                        help="set an environment variable for gdb and the program")
    common.add_argument("--max-lines", type=int, default=200, metavar="N",
                        help="cap the tool's total output at N lines (default 200)")

    why = sub.add_parser("why", parents=[common], help="run until the first failure and explain it")
    why.add_argument("--print", dest="prints", action="append", default=[], metavar="EXPR",
                     help="evaluate EXPR in the innermost repository frame")
    add_follow_child(why)
    why.add_argument("program", nargs=argparse.REMAINDER, metavar="PROGRAM [ARGS...]")

    trace = sub.add_parser("trace", parents=[common], help="report values at every hit of a location")
    trace.add_argument("--at", dest="ats", action="append", default=[], metavar="LOC",
                       help="file:line or function to break at (repeatable)")
    trace.add_argument("--show", dest="shows", action="append", default=[], metavar="EXPR")
    trace.add_argument("--if", dest="cond", metavar="COND", help="only report hits where COND holds")
    trace.add_argument("--max", dest="maxhits", type=int, default=50, metavar="N",
                       help="stop tracing after N hits (default 50)")
    add_follow_child(trace)
    trace.add_argument("program", nargs=argparse.REMAINDER, metavar="PROGRAM [ARGS...]")

    stop = sub.add_parser("stop", parents=[common], help="stop and report at the K-th hit")
    stop.add_argument("--at", dest="ats", action="append", default=[], metavar="LOC")
    stop.add_argument("--show", dest="shows", action="append", default=[], metavar="EXPR")
    stop.add_argument("--if", dest="cond", metavar="COND", help="only count hits where COND holds")
    stop.add_argument("--hit", dest="hit", type=int, default=1, metavar="K")
    add_follow_child(stop)
    stop.add_argument("program", nargs=argparse.REMAINDER, metavar="PROGRAM [ARGS...]")

    insp = sub.add_parser("inspect", parents=[common], help="evaluate expressions in an ELF statically")
    insp.add_argument("--show", dest="shows", action="append", default=[], metavar="EXPR")
    insp.add_argument("elf")
    return parser


def read_tail(path):
    try:
        return Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return []


def gdb_noise(line):
    """Pending-breakpoint and missing-source notices that gdb prints by design."""
    return (line.startswith('Function "') and line.endswith('" not defined.')
            or line.startswith("No source file named ")
            or (line.startswith("warning: ") and line.endswith("No such file or directory")))


def assemble(stdout, stderr, out_path, err_path, max_lines):
    lines = [line for line in stdout.splitlines() if line.startswith("probe: ")]
    lines += ["probe: gdb: " + line for line in stderr.splitlines()
              if line.strip() and not gdb_noise(line)]
    program = read_tail(out_path) + read_tail(err_path)
    if program:
        program = program[-PROGRAM_OUTPUT_LINES:]
        lines.append("probe: program output (last %d lines):" % len(program))
        lines += program
    if max_lines >= 1 and len(lines) > max_lines:
        keep = max(1, max_lines - 1)
        lines = lines[:keep] + ["probe: ... %d lines truncated (--max-lines %d)"
                                % (len(lines) - keep, max_lines)]
    for line in lines:
        print(line)
    return len(lines)


def main(argv=None):
    args = build_parser().parse_args(argv)
    mode = args.mode
    if mode in ("trace", "stop") and not args.ats:
        print("probe: %s needs --at LOC" % mode)
        return 2
    if mode in ("trace", "inspect") and not args.shows:
        print("probe: %s needs --show EXPR" % mode)
        return 2
    try:
        env = build_env(args.env)
    except ValueError as exc:
        print("probe: %s" % exc)
        return 2

    program, prog_args = None, []
    if mode in ("why", "trace", "stop"):
        rest = list(args.program)
        if rest and rest[0] == "--":
            rest = rest[1:]
        if not rest:
            print("probe: %s needs -- PROGRAM [ARGS...]" % mode)
            return 2
        program, prog_args = rest[0], rest[1:]
        target = program
    else:
        target = args.elf

    gdb_bin = gdb_binary(mode, target)
    if not gdb_bin:
        print("probe: no gdb found for %s" % target)
        return 1

    with tempfile.TemporaryDirectory(prefix="gdb-probe-") as work:
        work = Path(work)
        out_path, err_path = str(work / "stdout"), str(work / "stderr")
        config = dict(ROOT=os.path.realpath(getattr(args, "root", None) or default_root()), MODE=mode, OUT=out_path, ERR=err_path,
                      ARGS=prog_args, PRINTS=list(getattr(args, "prints", [])),
                      SHOWS=list(getattr(args, "shows", [])), ATS=list(getattr(args, "ats", [])),
                      COND=getattr(args, "cond", None),
                      MAXHITS=max(1, getattr(args, "maxhits", 50)),
                      HIT=max(1, getattr(args, "hit", 1)), MESSAGES=list(MESSAGES),
                      FOLLOW_CHILD=bool(getattr(args, "follow_child", False)))
        script_path = work / "probe.py"
        script_path.write_text(script_text(config))
        cmd = [gdb_bin, "-nx", "-batch",
               "-iex", "set debuginfod enabled off",
               "-ex", "set pagination off", "-ex", "set confirm off",
               "-ex", "set width 0", "-ex", "set print elements 32",
               "-ex", "set breakpoint pending on",
               "-x", str(script_path), target]
        try:
            proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, start_new_session=True)
        except OSError as exc:
            print("probe: cannot run %s: %s" % (gdb_bin, exc))
            return 1
        try:
            stdout, stderr = proc.communicate(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            stdout, stderr = proc.communicate()
            printed = assemble(stdout, stderr, out_path, err_path, args.max_lines)
            print("probe: timeout after %gs; killed gdb" % args.timeout)
            return 1
        printed = assemble(stdout, stderr, out_path, err_path, args.max_lines)

    if printed == 0:
        print("probe: gdb produced no report; check the gdb binary and the program")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
