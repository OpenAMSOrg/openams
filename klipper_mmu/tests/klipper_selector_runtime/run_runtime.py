#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run one saved selector-machine STM32 image's startup and HOME/select test.

One parametrised runtime test covers every implemented shared-drive selector
device (klipper_mmu/selector.h: LINEAR, ROTARY, SERVO, INDEXED), not just one
representative per kind. The saved image's own profile (has_selector,
selector.kind, lane_count, has_bay_sensors, has_powered_rewind, and each
gate's own calibration state) drives the fixture's behaviour, so
tests/klipper_selector_runtime/test_selector_runtime.c needs no per-device
branch of its own -- adding another selector device here only means adding a
row to DEVICES below. Two rows exercise a real behavioural difference the
fixture reads from that same profile data: Claymore is the one row with
per-lane BAY switches and powered rewind (docs/selectors.md), and Pico MMU
ships no gate angles, so its own selection is refused until the runtime
gate-set path (km_selector_gate_set) calibrates one.

Every device here is an H723 illustrative-pin candidate except ERCF 2.0/3.0,
which are also this catalogue's one G0B0/G0B1 selector row (Happy Hare's own
real MMB 2.0 board default, mcu/Kconfig KLIPPER_MMU_G0B_BOOT_GATES). This
script derives the saved image's target from its .config (H723 vs G0B0/G0B1)
and picks the matching boot-gate Kconfig symbol and diagnostic-start command
(km_diagnostic_start vs the host-epoch km_diagnostic_start_host), the same way
tests/klipper_passive_runtime/run_runtime.py already does for Angry Beaver.
"""
# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

PACKAGE = Path(__file__).resolve().parents[2]

# device key (as used by tests/release_matrix/run.py) -> (Kconfig device
# symbol suffix, saved manifest "device" name, selector kind, lane count).
DEVICES = {
    "ercf_1_1": ("ERCF_1_1", "ERCF 1.1", "LINEAR", 9),
    "ercf_2_0": ("ERCF_2_0", "ERCF 2.0", "LINEAR", 8),
    "ercf_3_0": ("ERCF_3_0", "ERCF 3.0", "LINEAR", 8),
    "jack_rabbit": ("ERCF_3_0", "ERCF 3.0 Jack Rabbit", "LINEAR", 8),
    "tradrack": ("TRADRACK", "Tradrack", "LINEAR", 10),
    "chameleon_3d": ("CHAMELEON_3D", "3D Chameleon", "ROTARY", 4),
    "mmx6": ("MMX6", "MMX6", "ROTARY", 6),
    "htlf": ("HTLF", "HTLF", "ROTARY", 4),
    "mmx": ("MMX", "MMX", "SERVO", 4),
    "low_rider": ("LOW_RIDER", "Low Rider", "SERVO", 6),
    "pico_mmu": ("PICO_MMU", "Pico MMU", "SERVO", 4),
    "vivid": ("BTT_VIVID", "BTT ViViD", "INDEXED", 4),
    "claymore": ("CLAYMORE", "Claymore", "INDEXED", 4),
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", choices=sorted(DEVICES), required=True)
    parser.add_argument("--autoconf", type=Path, required=True)
    parser.add_argument("--dictionary", type=Path, required=True)
    parser.add_argument("--mode", choices=("safe", "actuation"), required=True)
    parser.add_argument("--build-dir", type=Path, required=True)
    args = parser.parse_args()
    device_symbol, manifest_name, kind, lane_count = DEVICES[args.device]
    autoconf, dictionary = args.autoconf.resolve(), args.dictionary.resolve()
    artifact = autoconf.parent
    manifest_path = artifact / "manifest.json"
    config_path = artifact / ".config"
    if not all(path.is_file() for path in (autoconf, dictionary, manifest_path, config_path)):
        parser.error("the saved autoconf, dictionary, .config, and manifest are required")
    manifest = json.loads(manifest_path.read_text())
    actuation = args.mode == "actuation"
    if (manifest.get("device") != manifest_name or manifest.get("lane_count") != lane_count
            or manifest.get("actuation_opt_in") is not actuation
            or manifest.get("transport") != "usb"
            or manifest.get("selector_kind") != kind):
        parser.error(f"saved image does not match the requested {manifest_name} mode")
    for filename, path in (("autoconf.h", autoconf), ("klipper.dict", dictionary),
                           (".config", config_path)):
        if manifest.get("sha256", {}).get(filename) != digest(path):
            parser.error(f"saved image hash mismatch: {path}")
    config = config_path.read_text().splitlines()
    # Every device except ERCF 2.0/3.0 is H723-only; those two also have a
    # G0B0/G0B1 row (Happy Hare's real MMB 2.0 board default).
    targets = [part for part in ("h723", "g0b0", "g0b1")
               if f"CONFIG_MACH_STM32{part.upper()}=y" in config]
    if len(targets) != 1:
        parser.error("saved image must select exactly one supported target")
    target = targets[0]
    if target != "h723" and args.device not in ("ercf_2_0", "ercf_3_0"):
        parser.error(f"{manifest_name} has no G0B row; only ERCF 2.0/3.0 do")
    family = "HOST" if target in ("g0b0", "g0b1") else "H723"
    required_config = {
        f"CONFIG_MACH_STM32{target.upper()}=y",
        f"CONFIG_KLIPPER_MMU_DEVICE_{device_symbol}=y",
        "CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y", "CONFIG_KLIPPER_MMU_PIN_CONFIG=y",
        f"CONFIG_KLIPPER_MMU_LANE_COUNT={lane_count}",
        f"CONFIG_KLIPPER_MMU_SELECTOR_KIND_{kind}=y",
        "CONFIG_KLIPPER_MMU_G0B_BOOT_GATES=y" if family == "HOST"
        else "CONFIG_KLIPPER_MMU_SELECTOR_BOOT_GATES=y",
        "CONFIG_WANT_GPIO_LOCAL=y",
        "CONFIG_WANT_TMCUART_LOCAL=y", "CONFIG_KLIPPER_MMU_TMC_UART_SHARED=y",
        f"CONFIG_KLIPPER_MMU_BOOT_IDENTITY_{family}=y",
        "CONFIG_KLIPPER_MMU_DIAGNOSTIC_STARTUP=y",
    }
    if family == "HOST":
        required_config.update({"CONFIG_STM32_USB_PA11_PA12=y", "CONFIG_CLOCK_FREQ=64000000"})
        if any(line.startswith("CONFIG_KLIPPER_MMU_BOOT_IDENTITY_")
               and not line.startswith("CONFIG_KLIPPER_MMU_BOOT_IDENTITY_HOST=")
               and line.endswith("=y") for line in config):
            parser.error("host-epoch image unexpectedly selects an MCU RNG provider")
    if kind != "SERVO":
        required_config.add("CONFIG_KLIPPER_MMU_SELECTOR_STEPPER=y")
    # Jack Rabbit is ERCF 3.0 without its grip servo.
    if kind == "SERVO" or (kind == "LINEAR" and args.device != "jack_rabbit"):
        required_config.add("CONFIG_KLIPPER_MMU_SERVO=y")
    if actuation:
        required_config.add("CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN=y")
    elif "CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN=y" in config:
        parser.error("safe image unexpectedly grants START")
    missing = required_config.difference(config)
    if missing:
        parser.error("saved image is missing: " + ", ".join(sorted(missing)))
    commands = json.loads(dictionary.read_text()).get("commands", {})
    start_command = ("km_diagnostic_start_host epoch_hi=%u epoch_lo=%u"
                     if family == "HOST" else "km_diagnostic_start")
    required_commands = {
        start_command,
        "km_diagnostic_query challenge=%u",
        "km_start instance_hi=%u instance_lo=%u client=%c session=%u request=%u operation=%c lane=%c direction=%c",
        "km_event_ack instance_hi=%u instance_lo=%u client=%c session=%u request=%u operation=%c lane=%c seq=%u",
    }
    missing = required_commands.difference(commands)
    if missing:
        parser.error("image dictionary lacks production commands: " + ", ".join(sorted(missing)))

    build_dir = args.build_dir.resolve()
    build_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "make", "-s", "-C", str(PACKAGE / "tests/klipper_selector_runtime"),
        f"PRODUCT_ROOT={PACKAGE}", f"IMAGE_AUTOCONF={autoconf}",
        f"BUILD_DIR={build_dir}", "test",
    ], check=True, timeout=150)
    print(f"PASS: {manifest_name} {args.mode} startup/runtime matches saved image {digest(dictionary)}")


if __name__ == "__main__":
    main()
