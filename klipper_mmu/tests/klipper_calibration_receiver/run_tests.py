#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build and run the receiver with pinned Klipper codec and allocator."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

CASES = ("max", "map", "count1", "count9", "short", "long", "raw",
         "vref", "tolerance", "cross", "oid256", "oid200", "duplicate", "late",
         "shutdown_reset")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--klipper", type=Path)
    args = parser.parse_args()
    package = Path(__file__).resolve().parents[2]
    klipper = args.klipper or package.parent / "build/klipper-menuconfig"
    here = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="km-calibration-") as tmp:
        binary = Path(tmp) / "fixture"
        cmd = ["gcc", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
               "-Wno-unused-parameter", "-Wno-unused-function", "-Wno-sign-compare",
               "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
               "-fno-pie", "-no-pie", "-ffunction-sections", "-fdata-sections",
               "-include", str(klipper / "src/compiler.h"),
               "-include", str(here / "shim/fixture_api.h"),
               "-I", str(here / "shim"), "-I", str(klipper / "src"),
               "-I", str(package / "include"), "-I", str(package / "mcu"),
               str(here / "fixture.c"), str(klipper / "src/command.c"),
               str(klipper / "src/basecmd.c"), str(package / "src/buffer_sensor.c"),
               "-Wl,--gc-sections", "-lm", "-o", str(binary)]
        subprocess.run(cmd, check=True, timeout=60)
        env = dict(os.environ, ASAN_OPTIONS="detect_leaks=0:halt_on_error=1")
        for case in CASES:
            subprocess.run([str(binary), case], check=True, timeout=10, env=env)
    print(f"passed: {len(CASES)} pinned Klipper receiver cases")
    return 0

if __name__ == "__main__":
    sys.exit(main())
