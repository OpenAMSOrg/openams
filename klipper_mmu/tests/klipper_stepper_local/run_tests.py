#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Build and run the native WANT_STEPPER_LOCAL stepper tests.
#
# Copies tests/fixtures/shared_tmcuart twice (one pinned upstream, one to
# install the driver API bundle into), compiles the patched src/stepper.c with
# test_stepper_local.c against tests/klipper_stepper_local/shim, runs every
# case, then checks that the extension preprocesses to the pinned upstream
# source when CONFIG_WANT_STEPPER_LOCAL is 0.
import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--package", type=Path,
                    default=Path(__file__).resolve().parents[2],
                    help="klipper-mmu checkout root (default: derived)")
parser.add_argument("results", type=Path,
                    help="directory for logs and results.json")
args = parser.parse_args()

here = Path(__file__).resolve().parent
package = args.package.resolve()
args.results.mkdir(parents=True, exist_ok=True)
report = {"status": "failed", "cases": [], "hardware_accessed": False}


def case_lines(text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("ok ") or line.startswith("FAIL "):
            out.append(line)
    return out


def preprocess(gcc, src, out, shim, src_dir):
    subprocess.run(
        [gcc, "-E", "-P", "-std=gnu11", "-DCONFIG_WANT_STEPPER_LOCAL=0",
         "-I" + str(shim), "-I" + str(src_dir), str(src), "-o", str(out)],
        capture_output=True, text=True, timeout=60, check=True)


try:
    fixture = package / "tests/fixtures/shared_tmcuart"
    if not fixture.is_dir():
        raise SystemExit("missing fixture: %s" % fixture)
    with tempfile.TemporaryDirectory(prefix="km-stepper-local-") as temp:
        work = Path(temp)
        pinned = work / "pinned"
        checkout = work / "upstream"
        shutil.copytree(fixture, pinned)
        shutil.copytree(fixture, checkout)
        # integrate_driver_api() expects a product checkout layout.
        (checkout / "Makefile").write_text("# native fixture\n")
        (checkout / "klippy").mkdir()

        spec = importlib.util.spec_from_file_location(
            "klipper_mmu_install", package / "tools/install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        action = installer.integrate_driver_api(
            checkout, "install", package=package)
        if action != "installed":
            raise SystemExit("driver API bundle install returned %r" % (action,))

        shim = here / "shim"
        src_dir = checkout / "src"
        gcc = os.environ.get("CC", "gcc")
        exe = work / "probe"
        build = [gcc, "-std=gnu11", "-O2", "-g", "-Wall", "-Wextra", "-Werror",
                 "-Wno-unused-parameter",
                 "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                 "-fno-pie", "-no-pie",
                 "-I" + str(shim), "-I" + str(src_dir),
                 str(src_dir / "stepper.c"), str(here / "test_stepper_local.c"),
                 "-lm", "-o", str(exe)]
        with (args.results / "build.log").open("w") as log:
            log.write(" ".join(build) + "\n")
            log.flush()
            done = subprocess.run(build, stdout=log, stderr=subprocess.STDOUT,
                                  timeout=120)
        report["build_returncode"] = done.returncode
        if done.returncode != 0:
            print("FAIL build: see %s" % (args.results / "build.log"))
            raise SystemExit(1)

        env = dict(os.environ,
                   ASAN_OPTIONS="detect_leaks=0:halt_on_error=1",
                   UBSAN_OPTIONS="halt_on_error=1")
        probe = subprocess.run([str(exe)], capture_output=True, text=True,
                               timeout=60, env=env)
        cases = case_lines(probe.stdout)
        for line in cases:
            print(line)
        report["probe_returncode"] = probe.returncode
        report["probe_stdout"] = probe.stdout
        report["probe_stderr"] = probe.stderr
        if probe.returncode != 0:
            print("FAIL probe: exit %d" % probe.returncode)
            sys.stderr.write(probe.stderr[-4000:])

        # Off case: with the feature disabled the patched source must
        # preprocess to the pinned upstream source, byte for byte.
        patched_i = work / "patched.i"
        pinned_i = work / "pinned.i"
        preprocess(gcc, src_dir / "stepper.c", patched_i, shim, src_dir)
        preprocess(gcc, pinned / "src/stepper.c", pinned_i, shim, src_dir)
        equivalent = patched_i.read_bytes() == pinned_i.read_bytes()
        if equivalent:
            print("ok   off_equivalence")
        else:
            print("FAIL off_equivalence: preprocessed sources differ")
            diff = subprocess.run(["diff", "-u", str(pinned_i), str(patched_i)],
                                  capture_output=True, text=True)
            sys.stderr.write(diff.stdout[:4000])
        cases.append("ok   off_equivalence" if equivalent
                     else "FAIL off_equivalence")

        passed = probe.returncode == 0 and equivalent
        report["status"] = "passed" if passed else "failed"
        report["cases"] = cases
except subprocess.TimeoutExpired as exc:
    print("FAIL timeout: %s" % exc)
    report["status"] = "failed"
except SystemExit:
    raise
except Exception as exc:  # noqa: BLE001 - report any harness failure
    print("FAIL harness: %s: %s" % (type(exc).__name__, exc))
    report["status"] = "failed"
finally:
    (args.results / "results.json").write_text(
        json.dumps(report, indent=2) + "\n")
    cases = report.get("cases", [])
    ok = sum(1 for c in cases if c.startswith("ok"))
    print(json.dumps({"status": report["status"], "passed": ok,
                      "total": len(cases)}))

sys.exit(0 if report["status"] == "passed" else 1)
