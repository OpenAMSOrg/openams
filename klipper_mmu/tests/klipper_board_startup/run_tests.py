#!/usr/bin/env python3
"""Focused test runner for STM32H723 Klipper-MMU Board Startup Composition."""
# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-only", action="store_true", help="Compile test binary without running")
    parser.add_argument("--results", type=Path, default=None, help="File to write JSON report")
    args = parser.parse_args()

    here = Path(__file__).resolve().parent
    root = here.parents[2]

    report = {
        "status": "failed",
        "target": "klipper_board_startup",
        "timestamp": time.time(),
        "build": {},
        "execution": {},
    }

    # Step 1: Build
    print(f"[*] Building board startup test binary in {here}...")
    build_cmd = ["make", "-C", str(here), "build"]
    try:
        res_build = subprocess.run(
            build_cmd,
            cwd=str(here),
            capture_output=True,
            text=True,
            timeout=60
        )
        report["build"] = {
            "returncode": res_build.returncode,
            "stdout": res_build.stdout,
            "stderr": res_build.stderr,
        }
        if res_build.returncode != 0:
            print("[-] Build failed:")
            print(res_build.stderr)
            if args.results:
                args.results.write_text(json.dumps(report, indent=2))
            sys.exit(1)
        print("[+] Build succeeded.")
    except subprocess.TimeoutExpired:
        print("[-] Build timed out after 60s.")
        report["build"]["error"] = "timeout"
        if args.results:
            args.results.write_text(json.dumps(report, indent=2))
        sys.exit(1)

    if args.build_only:
        report["status"] = "build_ok"
        if args.results:
            args.results.write_text(json.dumps(report, indent=2))
        sys.exit(0)

    # Step 2: Execute Test
    print("[*] Running board startup tests with ASan & UBSan...")
    test_bin = here / "build/test_board_startup"
    test_env = dict(os.environ)
    test_env["ASAN_OPTIONS"] = "detect_leaks=0:halt_on_error=1"
    test_env["UBSAN_OPTIONS"] = "halt_on_error=1:abort_on_error=1"

    try:
        res_test = subprocess.run(
            [str(test_bin)],
            cwd=str(here),
            capture_output=True,
            text=True,
            env=test_env,
            timeout=60
        )
        report["execution"] = {
            "returncode": res_test.returncode,
            "stdout": res_test.stdout,
            "stderr": res_test.stderr,
        }
        print(res_test.stdout)
        if res_test.returncode != 0:
            print("[-] Test execution failed:")
            print(res_test.stderr)
            if args.results:
                args.results.write_text(json.dumps(report, indent=2))
            sys.exit(1)
        print("[+] All board startup tests passed cleanly!")
        report["status"] = "passed"
    except subprocess.TimeoutExpired:
        print("[-] Test execution timed out after 60s.")
        report["execution"]["error"] = "timeout"
        if args.results:
            args.results.write_text(json.dumps(report, indent=2))
        sys.exit(1)

    if args.results:
        args.results.write_text(json.dumps(report, indent=2))

    sys.exit(0)

if __name__ == "__main__":
    main()
