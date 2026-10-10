#!/usr/bin/env python3
"""Run composed feeder tests against the package-installed shared UART driver."""
# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, default=Path(__file__).resolve().parents[2],
                        help="Package to test (defaults to this package)")
    parser.add_argument("--build-only", action="store_true", help="Compile test binary without running")
    parser.add_argument("--work-dir", type=Path, default=None, help="Explicit working directory")
    parser.add_argument("--results", type=Path, default=None, help="File to write JSON report")
    parser.add_argument("--image-autoconf", type=Path, default=None,
                        help="Exact clean H723 actuation image autoconf.h (optional)")
    args = parser.parse_args()

    package = args.package.resolve()
    here = Path(__file__).resolve().parent
    test_c = here / "test_tmc_feeder.c"
    if not test_c.is_file():
        raise FileNotFoundError(f"Missing test_tmc_feeder.c in {here}")

    report = {
        "status": "failed",
        "package": str(package),
        "cases": [],
        "compilation": {},
    }

    def execute_in_work(work):
        work = Path(work).resolve()
        checkout = work / "upstream"

        fixture_src = package / "tests/fixtures/shared_tmcuart"
        shutil.copytree(fixture_src, checkout)
        (checkout / "Makefile").write_text("# native fixture\n")
        (checkout / "klippy").mkdir(exist_ok=True)

        installer_path = package / "tools/install.py"
        spec = importlib.util.spec_from_file_location("installer", installer_path)
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        inst_res = installer.integrate_driver_api(checkout, "install", package=package)
        chk_res = installer.integrate_driver_api(checkout, "check", package=package)
        if inst_res != "installed" or chk_res != "installed":
            raise RuntimeError(f"Driver API install failed: install={inst_res}, check={chk_res}")

        upstream = checkout / "src"

        # Locate base shim
        shim_dir = package / "tests/klipper_tmc_uart"
        shutil.copytree(shim_dir / "shim_include", work / "shim")
        shutil.copytree(package / "tests/klipper_tmc_feeder/shim_include", work / "shim", dirs_exist_ok=True)

        if args.image_autoconf is not None:
            # pin_config.c only needs the H723 port-presence macros. Avoid
            # pulling the physical CMSIS headers into this virtual fixture.
            stm32_shim = work / "shim/stm32"
            stm32_shim.mkdir(exist_ok=True)
            (stm32_shim / "internal.h").write_text(
                "#ifndef TEST_SHIM_STM32_INTERNAL_H\n"
                "#define TEST_SHIM_STM32_INTERNAL_H\n"
                "#define GPIOA 1\n#define GPIOB 1\n#define GPIOC 1\n"
                "#define GPIOD 1\n#define GPIOE 1\n#define GPIOF 1\n"
                "#define GPIOG 1\n#define GPIOH 1\n#define GPIOI 1\n"
                "#endif\n")
            (stm32_shim / "gpio_local.h").write_text(
                "#ifndef TEST_SHIM_STM32_GPIO_LOCAL_H\n"
                "#define TEST_SHIM_STM32_GPIO_LOCAL_H\n"
                "#include <stdint.h>\n#include \"board/gpio.h\"\n"
                "int gpio_local_reserve(const uint32_t *pins, uint32_t count);\n"
                "int gpio_pin_is_local(uint32_t pin);\n"
                "struct gpio_out gpio_out_setup_local(uint32_t pin, uint32_t value);\n"
                "struct gpio_in gpio_in_setup_local(uint32_t pin, int32_t pull_up);\n"
                "#endif\n")
            (checkout / "src/stm32/gpio.h").write_text(
                '#include "board/gpio.h"\n')

        if args.image_autoconf is not None:
            image_config = args.image_autoconf.resolve()
            if not image_config.is_file():
                raise FileNotFoundError(f"IMAGE_AUTOCONF does not exist: {image_config}")
            # Use the supplied image configuration verbatim; only add the
            # upstream fixture's local-UART capacity if the image omits it.
            config_text = image_config.read_text()
            (work / "shim/autoconf.h").write_text(
                config_text + "\n#ifndef CONFIG_TMCUART_LOCAL_MAX_CHANNELS\n"
                "#define CONFIG_TMCUART_LOCAL_MAX_CHANNELS 8\n#endif\n")
            required = ("#define CONFIG_MACH_STM32H723 1",
                        "#define CONFIG_WANT_TMCUART_LOCAL 1",
                        "#define CONFIG_KLIPPER_MMU_TMC_UART_SHARED 1",
                        "#define CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN 1")
            missing = [macro for macro in required if macro not in config_text]
            if missing:
                raise ValueError("IMAGE_AUTOCONF is not the expected H723 shared-TMC "
                                 "actuation image; missing: " + ", ".join(missing))
            import re
            lane_match = re.search(r"^#define CONFIG_KLIPPER_MMU_LANE_COUNT (\d+)\s*$",
                                   config_text, re.MULTILINE)
            if not lane_match or int(lane_match.group(1)) != 4:
                raise ValueError("IMAGE_AUTOCONF must configure exactly four lanes")
        else:
            with (work / "shim/autoconf.h").open("a") as f:
                f.write("\n#define CONFIG_WANT_TMCUART_LOCAL 1\n"
                        "#define CONFIG_KLIPPER_MMU_TMC_UART_SHARED 1\n"
                        "#define CONFIG_TMCUART_LOCAL_MAX_CHANNELS 8\n"
                        # STEP/DIR run through Klipper's local stepper (B2).
                        "#define CONFIG_WANT_STEPPER_LOCAL 1\n"
                        "#define CONFIG_STEPPER_LOCAL_COUNT 8\n"
                        "#define CONFIG_STEPPER_LOCAL_MOVES 224\n")
        with (work / "shim/command.h").open("a") as f:
            f.write("\n#include <stddef.h>\n#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))\n"
                    "#define DIV_ROUND_CLOSEST(n,d) (((n)+(d)/2)/(d))\n"
                    "#define DIV_ROUND_UP(n,d) (((n) + (d) - 1) / (d))\n"
                    "#define likely(x) __builtin_expect(!!(x), 1)\n"
                    "#define unlikely(x) __builtin_expect(!!(x), 0)\n"
                    "#pragma GCC diagnostic ignored \"-Wunused-parameter\"\n"
                    "#pragma GCC diagnostic ignored \"-Wsign-compare\"\n"
                    "#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))\n"
                    "#define DECL_CONSTANT_STR(name,val) _Static_assert(sizeof(val)>1, name)\n"
                    "void *command_decode_ptr(uint32_t);\n")

        shutil.copy2(test_c, work / "test_tmc_feeder.c")

        sources = [
            package / "mcu/tmc_feeder.c",
            package / "mcu/stepdir_gpio.c",
            package / "mcu/tmc_uart_gpio.c",
            package / "mcu/clock.c",
            package / "src/step_dir_driver.c",
            package / "src/mcu_clock.c",
            package / "src/tmc_uart_config.c",
            package / "src/tmc_uart_codec.c",
            package / "src/tmc2209.c",
            package / "src/rate.c",
            package / "src/trajectory.c",
            package / "src/step_segments.c",
            package / "mcu/commands.c",
            package / "mcu/messages.c",
            package / "src/operation_core.c",
            package / "src/selector_home.c",
            package / "src/buffer_sensor.c",
            package / "src/follower.c",
            package / "src/current_regulator.c",
            package / "src/event_queue.c",
            package / "src/device_profile.c",
            package / "src/protocol_wire.c",
            upstream / "tmcuart.c",
            upstream / "stepper.c",   # B2: STEP/DIR through the local stepper
            shim_dir / "shim_tmc.c",
            package / "tests/klipper_tmc_feeder/shim_klipper.c",
            work / "test_tmc_feeder.c",
        ]
        if args.image_autoconf is not None:
            sources += [package / "mcu/pin_config.c",
                        package / "mcu/feeder_config.c",
                        package / "mcu/tmc_config.c",
                        package / "src/resource_plan.c"]

        target = work / "test_tmc_feeder"
        cc_cmd = [
            "gcc", "-std=gnu11", "-O2", "-g", "-Wall", "-Wextra", "-Werror",
            "-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-fno-pie", "-no-pie",
        ]
        if args.image_autoconf is not None:
            cc_cmd.append("-DKM_TEST_IMAGE_AUTOCONF=1")
        includes = [
            package / "mcu",
            work / "shim",
            shim_dir,
            upstream,
            package / "include",
            package / "src",
        ]
        cc_cmd += ["-I" + str(p) for p in includes]
        cc_cmd += [str(p) for p in sources]
        cc_cmd += [
            "-Wl,--wrap=sched_add_timer,--wrap=sched_del_timer,--wrap=timer_read_time",
            "-Wl,--wrap=gpio_out_setup,--wrap=gpio_out_reset,--wrap=gpio_out_write,--wrap=gpio_out_toggle_noirq",
            "-Wl,--wrap=gpio_in_setup,--wrap=gpio_in_reset,--wrap=km_stepdir_gpio_force_off",
            "-Wl,--wrap=command_sendf",
            "-lm", "-o", str(target),
        ]

        cc_res = subprocess.run(cc_cmd, capture_output=True, text=True, timeout=60)
        report["compilation"] = {
            "returncode": cc_res.returncode,
            "stdout": cc_res.stdout,
            "stderr": cc_res.stderr,
        }
        if cc_res.returncode != 0:
            print(f"Compilation failed with exit code {cc_res.returncode}:\n{cc_res.stderr}", file=sys.stderr)
            return False

        if args.build_only:
            report["status"] = "compiled"
            print("Compilation successful (build-only mode).")
            return True

        run_env = {
            **os.environ,
            "ASAN_OPTIONS": "detect_leaks=0:halt_on_error=1",
            "UBSAN_OPTIONS": "halt_on_error=1",
        }
        run_res = subprocess.run([str(target)], capture_output=True, text=True, timeout=120, env=run_env)
        print(run_res.stdout, end="")
        if run_res.stderr:
            print(run_res.stderr, end="", file=sys.stderr)

        for line in run_res.stdout.splitlines():
            line = line.strip()
            if line.startswith("PASS ") or line.startswith("FAIL "):
                parts = line.split()
                report["cases"].append({"status": parts[0], "name": parts[1] if len(parts) > 1 else ""})

        passed_count = sum(1 for c in report["cases"] if c["status"] == "PASS")
        report["returncode"] = run_res.returncode
        expected = {
            "validation_and_delayed_ready", "cross_claims_and_failed_startup",
            "current_mismatch", "shared_uart_pin_two_addresses_both_confirm",
            "shared_uart_pin_equal_address_refused",
            "shared_uart_pin_fault_on_one_does_not_fault_other",
            "fault_high_step_restart_and_isolation",
            "restart_command_and_reconcile", "global_shutdown",
        }
        if args.image_autoconf is not None:
            expected.add("image_configured_four_lane_startup")
        if (run_res.returncode == 0 and passed_count == len(expected) and
                len(report["cases"]) == len(expected) and {c["name"] for c in report["cases"]} == expected):
            report["status"] = "passed"
            return True
        return False

    success = False
    try:
        if args.work_dir:
            args.work_dir.mkdir(parents=True, exist_ok=True)
            success = execute_in_work(args.work_dir)
        else:
            with tempfile.TemporaryDirectory(prefix="km-shared-feeder-") as tmp:
                success = execute_in_work(tmp)
    finally:
        if args.results:
            args.results.parent.mkdir(parents=True, exist_ok=True)
            args.results.write_text(json.dumps(report, indent=2) + "\n")

    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
