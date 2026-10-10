#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native command-level tests for the receive-only raw Diff probe."""
import argparse
import os
import subprocess
from pathlib import Path

CASES = ("idle", "fresh", "invalid", "fault", "duplicate", "no_mutation", "clock_retry")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", required=True)
    parser.add_argument("--source", default=None)
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    source = Path(args.source).resolve() if args.source else here.parents[1]
    results = Path(args.results).resolve()
    results.mkdir(parents=True, exist_ok=False)
    exe = results / "test_diff_probe"
    cmd = ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-Wconversion",
           "-Wsign-conversion", "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
           "-I", str(here / "shim"), "-I", str(source / "mcu"),
           "-I", str(source / "include"), str(here / "test_probe.c"),
           str(source / "mcu" / "diff_probe.c"), "-o", str(exe)]
    built = subprocess.run(cmd, capture_output=True, text=True, check=False)
    (results / "compile.log").write_text(built.stdout + built.stderr)
    if built.returncode:
        print("compile failed")
        return 1
    for case in CASES:
        env = os.environ.copy()
        env.setdefault("ASAN_OPTIONS", "detect_leaks=0:halt_on_error=1")
        env.setdefault("UBSAN_OPTIONS", "halt_on_error=1")
        run = subprocess.run([str(exe), case], capture_output=True, text=True,
                             timeout=5, check=False, env=env)
        (results / f"{case}.log").write_text(run.stdout + run.stderr)
        if run.returncode:
            print(f"fail {case}: {run.stderr.strip()}")
            return 1
    print(f"pass {len(CASES)} cases")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
