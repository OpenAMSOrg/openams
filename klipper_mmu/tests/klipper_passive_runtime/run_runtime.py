#!/usr/bin/env python3
"""Run one saved passive-feeder STM32 image's startup and operation tests.

One parametrised runtime test covers KMS, Angry Beaver, 3MS and QuattroBox
1.0/1.1/2: all six trace to the same four-lane, physical-HUB, no-PWM/ADC H723
shape (KMS is the only one with a per-lane LOAD/exit switch), so a single
compile-time scenario in tests/klipper_board_startup/test_boxturtle_boot.c
(guarded by KM_TEST_PASSIVE_RUNTIME) exercises all of them instead of one
scenario per device.
"""
# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

PACKAGE = Path(__file__).resolve().parents[2]

# device key (as used by tests/release_matrix/run.py) -> (Kconfig device
# symbol suffix, saved manifest "device" name, per-lane LOAD sensor present,
# per-lane BAY sensor present, virtual hub through the owning lane's LOAD).
# Only KMS has a per-lane LOAD switch; it and EMU are the only virtual-hub
# devices. Angry Beaver, 3MS and QuattroBox 1.0/1.1/2 have no per-lane BAY
# switch and use a real, separate physical HUB input.
DEVICES = {
    "kms": ("KMS", "KMS", True, True, True),
    "angry_beaver": ("ANGRY_BEAVER", "Angry Beaver", False, False, False),
    "3ms": ("THREE_MS", "3MS", False, False, False),
    "quattrobox_1_0": ("QUATTROBOX_1_0", "QuattroBox 1.0", False, False, False),
    "quattrobox_1_1": ("QUATTROBOX_1_1", "QuattroBox 1.1", False, False, False),
    "quattrobox_2": ("QUATTROBOX_2", "QuattroBox 2", False, False, False),
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
    device_symbol, manifest_name, has_load, has_bay, virtual_hub = DEVICES[args.device]
    autoconf, dictionary = args.autoconf.resolve(), args.dictionary.resolve()
    artifact = autoconf.parent
    manifest_path = artifact / "manifest.json"
    config_path = artifact / ".config"
    if not all(path.is_file() for path in (autoconf, dictionary, manifest_path, config_path)):
        parser.error("the saved autoconf, dictionary, .config, and manifest are required")
    manifest = json.loads(manifest_path.read_text())
    actuation = args.mode == "actuation"
    if (manifest.get("device") != manifest_name or manifest.get("lane_count") != 4
            or manifest.get("actuation_opt_in") is not actuation
            or manifest.get("transport") != "usb"):
        parser.error(f"saved image does not match the requested {manifest_name} mode")
    for filename, path in (("autoconf.h", autoconf), ("klipper.dict", dictionary),
                           (".config", config_path)):
        if manifest.get("sha256", {}).get(filename) != digest(path):
            parser.error(f"saved image hash mismatch: {path}")
    config = config_path.read_text().splitlines()
    # Every device except Angry Beaver is H723-only; Angry Beaver is also
    # this catalogue's one G0B0/G0B1 row (Happy Hare's real MMB 2.0 board,
    # mcu/Kconfig KLIPPER_MMU_G0B_BOOT_GATES), a host-epoch image like
    # NightOwl's own G0B row (tests/klipper_nightowl_runtime/run_runtime.py).
    targets = [part for part in ("h723", "g0b0", "g0b1")
               if f"CONFIG_MACH_STM32{part.upper()}=y" in config]
    if len(targets) != 1:
        parser.error("saved image must select exactly one supported target")
    target = targets[0]
    if target != "h723" and args.device != "angry_beaver":
        parser.error(f"{manifest_name} has no G0B row; only Angry Beaver does")
    family = "HOST" if target in ("g0b0", "g0b1") else "H723"
    required_config = {
        f"CONFIG_MACH_STM32{target.upper()}=y",
        f"CONFIG_KLIPPER_MMU_DEVICE_{device_symbol}=y",
        "CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y", "CONFIG_KLIPPER_MMU_PIN_CONFIG=y",
        "CONFIG_KLIPPER_MMU_G0B_BOOT_GATES=y" if target in ("g0b0", "g0b1")
        else "CONFIG_KLIPPER_MMU_PASSIVE_FEEDER_BOOT_GATES=y",
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
    if has_load != ("CONFIG_KLIPPER_MMU_LOAD_SENSORS=y" in config):
        parser.error(f"saved image LOAD-sensor setting does not match {manifest_name}'s profile")
    if has_bay != ("CONFIG_KLIPPER_MMU_BAY_SENSORS=y" in config):
        parser.error(f"saved image BAY-sensor setting does not match {manifest_name}'s profile")
    if virtual_hub != ("CONFIG_KLIPPER_MMU_VIRTUAL_HUB=y" in config):
        parser.error(f"saved image virtual-hub setting does not match {manifest_name}'s profile")
    if virtual_hub and any(line.startswith("CONFIG_KLIPPER_MMU_PINS_HUB=") for line in config):
        parser.error(f"{manifest_name} must not save a physical HUB pin")
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
        "make", "-s", "-C", str(PACKAGE / "tests/klipper_emu_runtime"),
        f"PRODUCT_ROOT={PACKAGE}", f"IMAGE_AUTOCONF={autoconf}",
        f"BUILD_DIR={build_dir}", "TARGET_NAME=test_passive_runtime",
        "RUNTIME_DEFINE=-DKM_TEST_HOSTED_RUNTIME=1 -DKM_TEST_PASSIVE_RUNTIME=1",
        "test",
    ], check=True, timeout=150)
    print(f"PASS: {manifest_name} {args.mode} startup/runtime matches saved image {digest(dictionary)}")


if __name__ == "__main__":
    main()
