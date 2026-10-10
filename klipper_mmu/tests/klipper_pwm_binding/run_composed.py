#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the binding through the package-installed paired-PWM extension."""
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
# When installed as klipper-mmu/tests/klipper_pwm_binding, this is klipper-mmu.
DEFAULT_PACKAGE = HERE.parents[1]


def run(command, **kwargs):
    return subprocess.run(command, check=True, timeout=30, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, default=DEFAULT_PACKAGE)
    parser.add_argument("--source", type=Path,
                        help="directory containing mcu/pwm_bridge_gpio.[ch]")
    parser.add_argument("--checkout", type=Path,
                        help="pinned Klipper fixture to copy before installation")
    parser.add_argument("--results", type=Path, default=HERE / "composed-results.json")
    args = parser.parse_args()
    package = args.package.resolve()
    source = (args.source or package).resolve()
    if not (source / "mcu/pwm_bridge_gpio.c").is_file() or not (source / "mcu/pwm_bridge_gpio.h").is_file():
        parser.error("--source must contain mcu/pwm_bridge_gpio.[ch]")
    fixture = (args.checkout or package / "tests/fixtures/shared_tmcuart").resolve()
    pair_tests = package / "tests/klipper_pwm_pair"
    report = {"status": "failed", "hardware_access": False,
              "scope": "one composed normal lifecycle; failure injection is run_fake.py",
              "binding_source": str(source), "checkout_fixture": str(fixture)}
    with tempfile.TemporaryDirectory(prefix="km-pwm-binding-composed-") as temporary:
        work = Path(temporary)
        checkout = work / "checkout"
        shutil.copytree(fixture, checkout)
        if not (checkout / "Makefile").exists():
            (checkout / "Makefile").write_text("# native fixture\n")
        (checkout / "klippy").mkdir(exist_ok=True)
        spec = importlib.util.spec_from_file_location("installer", package / "tools/install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        try:
            assert installer.integrate_driver_api(checkout, "install", package) == "installed"
            assert installer.integrate_driver_api(checkout, "check", package) == "installed"
            installed = checkout / "src/stm32"
            manifest, _ = installer.load_driver_api_assets(package)
            assert hashlib.sha256((installed / "hard_pwm.c").read_bytes()).hexdigest() == manifest["src/stm32/hard_pwm.c"]["after"]
            assert hashlib.sha256((installed / "pwm.h").read_bytes()).hexdigest() == manifest["src/stm32/pwm.h"]["after"]
            shim = work / "shim"
            shutil.copytree(pair_tests / "shim", shim)
            shutil.copy2(installed / "pwm.h", shim / "pwm.h")
            (shim / "stm32").mkdir()
            shutil.copy2(installed / "pwm.h", shim / "stm32/pwm.h")
            autoconf = shim / "autoconf.h"
            autoconf.write_text(autoconf.read_text() +
                "\n#define CONFIG_MACH_STM32H723 1\n#define CONFIG_KLIPPER_MMU_POWERED_REWIND 1"
                "\n#define CONFIG_HARD_PWM_LOCAL_MAX_PAIRS 8\n#define CONFIG_KLIPPER_MMU_LANE_COUNT 4\n")
            (shim / "board/gpio.h").write_text('#include "../gpio.h"\nvoid gpio_out_write(struct gpio_out output, uint32_t value);\n')
            for name in ("composed.c",):
                shutil.copy2(HERE / name, work / name)
            for name in ("pwm_bridge_gpio.c", "pwm_bridge_gpio.h"):
                shutil.copy2(source / "mcu" / name, work / name)
            shutil.copy2(installed / "hard_pwm.c", work / "hard_pwm.c")
            report["binding_sha256"] = hashlib.sha256((work / "pwm_bridge_gpio.c").read_bytes()).hexdigest()
            report["hard_pwm_sha256"] = hashlib.sha256((work / "hard_pwm.c").read_bytes()).hexdigest()
            common = ["gcc", "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-Wconversion", "-Wsign-conversion",
                      "-g", "-O1", "-fno-pie", "-fsanitize=address,undefined,pointer-compare", "-fno-sanitize-recover=all",
                      "-I", str(shim), "-I", str(work), "-I", str(package / "mcu"), "-I", str(package / "include")]
            driver_warnings = [flag for flag in common if flag not in ("-Wconversion", "-Wsign-conversion")]
            run([*driver_warnings, "-Wno-pointer-to-int-cast", "-c", str(work / "hard_pwm.c"), "-o", str(work / "hard_pwm.o")],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            run([*common, "-Dmain=pair_harness_main", "-c", str(pair_tests / "test_pair.c"), "-o", str(work / "pair_harness.o")],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            run([*common, "-c", str(work / "composed.c"), "-o", str(work / "composed.o")], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            run([*common, "-c", str(package / "src/pwm_bridge_driver.c"), "-o", str(work / "bridge.o")], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            run([*common, "-c", str(work / "pwm_bridge_gpio.c"), "-o", str(work / "binding.o")], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            executable = work / "composed"
            run(["gcc", "-no-pie", "-fsanitize=address,undefined,pointer-compare", str(work / "hard_pwm.o"),
                 str(work / "pair_harness.o"), str(work / "composed.o"), str(work / "bridge.o"), str(work / "binding.o"),
                 "-Wl,--wrap=gpio_out_setup,--wrap=gpio_pwm_pair_setup", "-o", str(executable)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            env = {**os.environ, "ASAN_OPTIONS": "detect_leaks=0:halt_on_error=1:detect_invalid_pointer_pairs=2",
                   "UBSAN_OPTIONS": "halt_on_error=1"}
            result = run([str(executable)], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
            report["case"] = result.stdout.strip() or "pass"
            report["status"] = "passed"
        except subprocess.CalledProcessError as error:
            report["failure"] = error.stdout.decode() if isinstance(error.stdout, bytes) else error.stdout
        except (AssertionError, RuntimeError) as error:
            report["failure"] = str(error)
        except subprocess.TimeoutExpired as error:
            report["failure"] = "timeout: " + " ".join(error.cmd)
    args.results.parent.mkdir(parents=True, exist_ok=True)
    args.results.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
