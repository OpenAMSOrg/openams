#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Compile the portable PWM bridge with the frozen shared-pair MCU binding."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
# When installed as klipper-mmu/tests/klipper_pwm_binding, this is klipper-mmu.
DEFAULT_PACKAGE = HERE.parents[1]
CASES = ("normal", "failure", "clock", "clearclock", "subcount", "invalid", "malformed", "setupfail", "irq", "planner")


def run(command, **kwargs):
    return subprocess.run(command, check=True, timeout=30, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, default=DEFAULT_PACKAGE)
    parser.add_argument("--source", type=Path,
                        help="directory containing mcu/pwm_bridge_gpio.[ch]")
    parser.add_argument("--results", type=Path, default=HERE / "results.json")
    args = parser.parse_args()
    package = args.package.resolve()
    source = (args.source or package).resolve()
    if not (source / "mcu/pwm_bridge_gpio.c").is_file() or not (source / "mcu/pwm_bridge_gpio.h").is_file():
        parser.error("--source must contain mcu/pwm_bridge_gpio.[ch]")
    report = {"status": "failed", "source": str(source), "package": str(package), "hardware_access": False, "cases": {}}
    with tempfile.TemporaryDirectory(prefix="shared-pwm-binding-") as temp:
        work = Path(temp)
        shutil.copytree(HERE / "shim", work / "shim")
        shutil.copy2(HERE / "test_binding.c", work / "test_binding.c")
        shutil.copy2(source / "mcu/pwm_bridge_gpio.c", work / "pwm_bridge_gpio.c")
        shutil.copy2(source / "mcu/pwm_bridge_gpio.h", work / "pwm_bridge_gpio.h")
        report["binding_sha256"] = hashlib.sha256((work / "pwm_bridge_gpio.c").read_bytes()).hexdigest()
        common = ["gcc", "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-Wconversion", "-Wsign-conversion",
                  "-g", "-O1", "-fsanitize=address,undefined,pointer-compare", "-fno-sanitize-recover=all",
                  "-I", str(work / "shim"), "-I", str(work), "-I", str(package / "mcu"), "-I", str(package / "include")]
        try:
            test_sources = [str(work / "test_binding.c"), str(package / "src/pwm_bridge_driver.c"),
                            str(work / "pwm_bridge_gpio.c")]
            run([*common, *test_sources, "-o", str(work / "test")],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            env = {**os.environ, "ASAN_OPTIONS": "detect_leaks=0:halt_on_error=1:detect_invalid_pointer_pairs=2", "UBSAN_OPTIONS": "halt_on_error=1"}
            for case in CASES:
                completed = run([str(work / "test"), case], text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=env)
                report["cases"][case] = completed.stdout.strip()
            constrained = work / "capacity-shim"
            shutil.copytree(work / "shim", constrained)
            (constrained / "autoconf.h").write_text((constrained / "autoconf.h").read_text()
                .replace("#define CONFIG_HARD_PWM_LOCAL_MAX_PAIRS 8", "#define CONFIG_HARD_PWM_LOCAL_MAX_PAIRS 3"))
            capacity_common = [*common]
            capacity_common[capacity_common.index(str(work / "shim"))] = str(constrained)
            run([*capacity_common, "-DEXPECT_CAPACITY_REJECT", *test_sources, "-o", str(work / "capacity-test")],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            completed = run([str(work / "capacity-test"), "capacity"], text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env)
            report["cases"]["capacity"] = completed.stdout.strip()
            query_shim = work / "query-shim"
            shutil.copytree(work / "shim", query_shim)
            query_common = [*common]
            query_common[query_common.index(str(work / "shim"))] = str(query_shim)
            run([*query_common, "-DUSE_REAL_H723_RESOURCES", "-DCONFIG_KLIPPER_MMU_PIN_CONFIG=1",
                 "-DCONFIG_WANT_HARD_PWM_QUERY=0", *test_sources, str(package / "mcu/h723_resources.c"),
                 "-o", str(work / "query-test")], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            completed = run([str(work / "query-test"), "queryoff"], text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env)
            report["cases"]["query_unavailable"] = completed.stdout.strip()
            run([*common, "-DCONFIG_WANT_GPIO_LOCAL=1", "-DKM_TEST_CONFIGURED_ADMISSION",
                 *test_sources, "-o", str(work / "configured-test")],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            completed = run([str(work / "configured-test"), "configured"], text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env)
            report["cases"]["configured"] = completed.stdout.strip()
            report["status"] = "passed"
        except subprocess.CalledProcessError as error:
            report["failure"] = error.stdout.decode() if isinstance(error.stdout, bytes) else error.stdout
        except subprocess.TimeoutExpired as error:
            report["failure"] = "timeout: " + " ".join(error.cmd)
    args.results.parent.mkdir(parents=True, exist_ok=True)
    args.results.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if report["status"] != "passed": raise SystemExit(1)


if __name__ == "__main__":
    main()
