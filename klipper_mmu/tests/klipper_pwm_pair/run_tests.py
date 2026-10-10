#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build native paired-PWM tests against the candidate's public installer."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
CASES = ("setup", "active_low", "mixed", "stale_ack", "missing_ack", "second_ack", "reversal",
         "endpoints", "tim12", "capacity", "ordinary", "ordinary_first", "parameters", "update_fault",
         "subunit", "raw", "shutdown_callback", "shutdown_af", "shutdown_clock")


def checked(command, **kwargs):
    return subprocess.run(command, check=True, timeout=30, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, default=HERE.parents[1])
    parser.add_argument("results", type=Path)
    parser.add_argument("--keep-work", action="store_true")
    args = parser.parse_args()
    package = args.package.resolve()

    report = {"status": "failed", "package": str(package), "hardware_access": False,
              "cases": {}}
    with tempfile.TemporaryDirectory(prefix="pwm-pair-native-") as temp:
        work = Path(temp)
        checkout = work / "checkout"
        shutil.copytree(package / "tests/fixtures/shared_tmcuart", checkout)
        (checkout / "Makefile").write_text("# native fixture\n")
        (checkout / "klippy").mkdir()
        spec = importlib.util.spec_from_file_location("installer", package / "tools/install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        assert installer.integrate_driver_api(checkout, "install", package) == "installed"
        assert installer.integrate_driver_api(checkout, "check", package) == "installed"
        driver = checkout / "src/stm32/hard_pwm.c"
        header = checkout / "src/stm32/pwm.h"
        manifest, _ = installer.load_driver_api_assets(package)
        report["source_sha256"] = hashlib.sha256(driver.read_bytes()).hexdigest()
        assert report["source_sha256"] == manifest["src/stm32/hard_pwm.c"]["after"]
        assert hashlib.sha256(header.read_bytes()).hexdigest() == manifest["src/stm32/pwm.h"]["after"]
        try:
            env = {**os.environ, "ASAN_OPTIONS": "detect_leaks=0:halt_on_error=1:detect_invalid_pointer_pairs=2",
                   "UBSAN_OPTIONS": "halt_on_error=1"}
            report["capacity_configs"] = {}
            for capacity in range(1, 9):
                build = work / ("capacity-" + str(capacity))
                shutil.copytree(HERE / "shim", build)
                shutil.copy2(driver, build / "hard_pwm.c")
                shutil.copy2(header, build / "pwm.h")
                with (build / "autoconf.h").open("a") as config:
                    config.write("#define CONFIG_HARD_PWM_LOCAL_MAX_PAIRS " + str(capacity) + "\n")
                common = ["gcc", "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-g", "-O1",
                          "-fsanitize=address,undefined,pointer-compare",
                          "-fno-sanitize-recover=all", "-fno-pie", "-I", str(build)]
                checked([*common, "-Wno-pointer-to-int-cast", "-c", str(build / "hard_pwm.c"),
                         "-o", str(build / "driver.o")], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                checked([*common, "-Wconversion", "-Wsign-conversion", "-c", str(HERE / "test_pair.c"),
                         "-o", str(build / "test.o")], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                checked(["gcc", "-fsanitize=address,undefined,pointer-compare", "-no-pie",
                         str(build / "driver.o"), str(build / "test.o"), "-o", str(build / "pair_test")],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                passed = []
                for case in CASES:
                    completed = checked([str(build / "pair_test"), case], stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, env=env)
                    passed.append(case)
                report["capacity_configs"][str(capacity)] = passed
            report["cases"] = {case: "passed under capacity 1..8" for case in CASES}
            report["status"] = "passed"
        except subprocess.CalledProcessError as error:
            report["failure"] = error.stdout.decode() if isinstance(error.stdout, bytes) else error.stdout
        except subprocess.TimeoutExpired as error:
            report["failure"] = "timeout: " + " ".join(error.cmd)
        if args.keep_work:
            kept = Path(tempfile.mkdtemp(prefix="pwm-pair-native-kept-")) / "fixture"
            shutil.copytree(work, kept)
            report["work"] = str(kept)
    args.results.mkdir(parents=True, exist_ok=True)
    (args.results / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if report["status"] != "passed": raise SystemExit(1)


if __name__ == "__main__":
    main()
