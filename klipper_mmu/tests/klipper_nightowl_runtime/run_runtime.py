#!/usr/bin/env python3
"""Run NightOwl startup and operation tests against one retained STM32 image."""
# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

PACKAGE = Path(__file__).resolve().parents[2]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--autoconf", type=Path, required=True)
    parser.add_argument("--dictionary", type=Path, required=True)
    parser.add_argument("--mode", choices=("safe", "actuation"), required=True)
    parser.add_argument("--build-dir", type=Path, required=True)
    args = parser.parse_args()
    autoconf, dictionary = args.autoconf.resolve(), args.dictionary.resolve()
    artifact = autoconf.parent
    manifest_path = artifact / "manifest.json"
    config_path = artifact / ".config"
    if not all(path.is_file() for path in (autoconf, dictionary, manifest_path, config_path)):
        parser.error("the saved autoconf, dictionary, .config, and manifest are required")
    manifest = json.loads(manifest_path.read_text())
    actuation = args.mode == "actuation"
    if (manifest.get("device") != "NightOwl" or manifest.get("lane_count") != 4
            or manifest.get("actuation_opt_in") is not actuation
            or manifest.get("transport") != "usb"):
        parser.error("saved image does not match the requested NightOwl mode")
    for filename, path in (("autoconf.h", autoconf), ("klipper.dict", dictionary),
                           (".config", config_path)):
        if manifest.get("sha256", {}).get(filename) != digest(path):
            parser.error(f"saved image hash mismatch: {path}")
    config = config_path.read_text().splitlines()
    targets = [part for part in ("f207", "f405", "f407", "f429", "f765", "g431", "g474", "h723", "h743", "h750", "g0b0", "g0b1")
               if f"CONFIG_MACH_STM32{part.upper()}=y" in config]
    if len(targets) != 1:
        parser.error("saved NightOwl image must select exactly one supported target")
    target = targets[0]
    family = {"f207": "F207", "f405": "F4X5", "f407": "F4X5", "f429": "F4X5",
              "f765": "F765", "g431": "G4", "g474": "G4", "h723": "H723",
              "h743": "H743_H750", "h750": "H743_H750",
              "g0b0": "HOST", "g0b1": "HOST"}[target]
    gate_family = "G0B" if target in ("g0b0", "g0b1") else family
    required_config = {
        f"CONFIG_MACH_STM32{target.upper()}=y",
        "CONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL=y",
        "CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y", "CONFIG_KLIPPER_MMU_PIN_CONFIG=y",
        "CONFIG_KLIPPER_MMU_NIGHTOWL_BOOT_GATES=y" if target == "h723"
        else f"CONFIG_KLIPPER_MMU_{gate_family}_BOOT_GATES=y",
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
        start_command, "km_diagnostic_query challenge=%u",
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
        f"BUILD_DIR={build_dir}", "TARGET_NAME=test_nightowl_runtime",
        "RUNTIME_DEFINE=-DKM_TEST_HOSTED_RUNTIME=1 -DKM_TEST_NIGHTOWL_RUNTIME=1",
        "test",
    ], check=True, timeout=150)
    print(f"PASS: NightOwl {args.mode} startup/runtime matches saved image {digest(dictionary)}")


if __name__ == "__main__":
    main()
