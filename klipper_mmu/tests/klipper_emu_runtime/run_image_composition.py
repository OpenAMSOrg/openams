#!/usr/bin/env python3
"""Exercise production EMU factories against one saved firmware configuration."""
# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
import os
from pathlib import Path
import subprocess

PACKAGE = Path(__file__).resolve().parents[2]
SOURCES = (
    "mcu/config.c", "mcu/pin_config.c", "mcu/switch_config.c",
    "mcu/feeder_config.c", "mcu/tmc_config.c", "mcu/h723_resources.c",
    "src/device_profile.c", "src/resource_plan.c", "src/buffer_sensor.c",
    "src/step_dir_driver.c", "src/tmc_uart_config.c", "src/tmc2209.c",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--autoconf", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    autoconf = args.autoconf.resolve()
    if not autoconf.is_file():
        parser.error(f"missing image autoconf: {autoconf}")
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "gcc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-Wconversion",
        "-Wsign-conversion", "-fsanitize=address,undefined",
        "-fno-sanitize-recover=all", "-ffunction-sections", "-fdata-sections",
        "-Wl,--gc-sections", f"-I{autoconf.parent}",
        f"-I{PACKAGE / 'tests/klipper_board_startup/shim'}",
        f"-I{PACKAGE / 'mcu'}", f"-I{PACKAGE / 'include'}",
        str(PACKAGE / "tests/klipper_emu_runtime/test_image_composition.c"),
        *(str(PACKAGE / source) for source in SOURCES), "-o", str(output),
    ]
    subprocess.run(command, check=True, timeout=60)
    env = dict(os.environ, ASAN_OPTIONS="detect_leaks=0:halt_on_error=1",
               UBSAN_OPTIONS="halt_on_error=1:abort_on_error=1")
    subprocess.run([str(output)], check=True, timeout=15, env=env)
    print("PASS: EMU saved-image profile, five feeder/TMC lanes, virtual HUB switches and pin plan")


if __name__ == "__main__":
    main()
