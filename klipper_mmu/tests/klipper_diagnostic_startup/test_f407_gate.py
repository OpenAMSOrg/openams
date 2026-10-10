#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the constrained F407 hosted-startup compile gate."""

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "mcu" / "diagnostic_startup.c"
SHIM = ROOT / "tests" / "klipper_diagnostic_startup" / "shim"


def main():
    common = [
        "gcc", "-std=c11", "-fsyntax-only", "-Wall", "-Wextra", "-Werror",
        "-DCONFIG_MACH_STM32F407=1",
        "-DCONFIG_KLIPPER_MMU_DIAGNOSTIC_STARTUP=1",
        "-DCONFIG_KLIPPER_MMU_ACTUATION_OPT_IN=1",
        "-DCONFIG_USBSERIAL=1",
        "-DCONFIG_CLOCK_FREQ=168000000u",
        "-DCONFIG_KLIPPER_MMU_DIAGNOSTIC_TIMEOUT_US=1000000u",
        "-I", str(SHIM), "-I", str(ROOT / "mcu"), "-I", str(ROOT / "include"),
        str(SOURCE),
    ]
    command = list(common)
    result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=30)
    expected = "diagnostic_startup.c requires a verified target-specific boot-identity provider"
    if result.returncode == 0 or expected not in result.stderr:
        raise SystemExit(
            "F407 diagnostic startup did not fail without the explicit identity gate\n"
            + result.stdout + result.stderr
        )

    command = list(common)
    command.extend([
        "-DCONFIG_KLIPPER_MMU_BOOT_IDENTITY_F4X5=1",
        "-DCONFIG_STM32_USB_PB14_PB15=1",
    ])
    result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=30)
    expected = "diagnostic_startup.c requires a verified target-specific boot-identity provider"
    if result.returncode == 0 or expected not in result.stderr:
        raise SystemExit("F407 diagnostic startup accepted PB14/PB15 USB\n"
                         + result.stdout + result.stderr)

    profile = ["-DCONFIG_KLIPPER_MMU_DEVICE_CUSTOM=1",
               "-DCONFIG_KLIPPER_MMU_LANE_COUNT=1",
               "-DCONFIG_KLIPPER_MMU_POWERED_REWIND=0",
               "-DCONFIG_KLIPPER_MMU_DIFF_BUFFER=0"]
    command = list(common) + profile
    command.append("-DCONFIG_KLIPPER_MMU_BOOT_IDENTITY_F4X5=1")
    result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=30)
    if result.returncode:
        raise SystemExit("valid one-lane Custom PA11/PA12 F407 startup failed to compile\n"
                         + result.stdout + result.stderr)

    nightowl = ["-DCONFIG_KLIPPER_MMU_DEVICE_CUSTOM=0",
                "-DCONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL=1",
                "-DCONFIG_KLIPPER_MMU_LANE_COUNT=4",
                "-DCONFIG_KLIPPER_MMU_POWERED_REWIND=0",
                "-DCONFIG_KLIPPER_MMU_DIFF_BUFFER=0"]
    command = list(common) + nightowl + ["-DCONFIG_KLIPPER_MMU_BOOT_IDENTITY_F4X5=1"]
    result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=30)
    if result.returncode:
        raise SystemExit("valid four-lane NightOwl F407 startup failed to compile\n"
                         + result.stdout + result.stderr)

    f405 = ["-DCONFIG_MACH_STM32F405=1" if arg == "-DCONFIG_MACH_STM32F407=1"
            else arg for arg in common]
    for selected in (profile, nightowl):
        command = f405 + selected + ["-DCONFIG_KLIPPER_MMU_BOOT_IDENTITY_F4X5=1"]
        result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=30)
        if result.returncode:
            raise SystemExit("valid F405 passive startup failed to compile\n"
                             + result.stdout + result.stderr)

    f429 = ["-DCONFIG_MACH_STM32F429=1" if arg == "-DCONFIG_MACH_STM32F407=1"
            else arg for arg in common]
    f429.append("-DCONFIG_STM32_USB_PA11_PA12=1")
    for selected in (profile, nightowl):
        command = f429 + selected + ["-DCONFIG_KLIPPER_MMU_BOOT_IDENTITY_F4X5=1"]
        result = subprocess.run(command, text=True, capture_output=True,
                                check=False, timeout=30)
        if result.returncode:
            raise SystemExit("valid F429 passive startup failed to compile\n"
                             + result.stdout + result.stderr)

    f765 = ["-DCONFIG_MACH_STM32F765=1" if arg == "-DCONFIG_MACH_STM32F407=1"
            else "-DCONFIG_CLOCK_FREQ=216000000u" if arg == "-DCONFIG_CLOCK_FREQ=168000000u"
            else arg for arg in common]
    for selected in (profile, nightowl):
        command = f765 + selected + ["-DCONFIG_KLIPPER_MMU_BOOT_IDENTITY_F765=1",
                                      "-DCONFIG_STM32_USB_PA11_PA12=1"]
        result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=30)
        if result.returncode:
            raise SystemExit("valid F765 passive startup failed to compile\n"
                             + result.stdout + result.stderr)

    f207 = ["-DCONFIG_MACH_STM32F207=1" if arg == "-DCONFIG_MACH_STM32F407=1"
            else "-DCONFIG_CLOCK_FREQ=120000000u" if arg == "-DCONFIG_CLOCK_FREQ=168000000u"
            else arg for arg in common]
    for selected in (profile, nightowl):
        command = f207 + selected + ["-DCONFIG_KLIPPER_MMU_BOOT_IDENTITY_F207=1",
                                    "-DCONFIG_STM32_USB_PA11_PA12=1"]
        result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=30)
        if result.returncode:
            raise SystemExit("valid F207 passive startup failed to compile\n"
                             + result.stdout + result.stderr)

    for option in ("-DCONFIG_KLIPPER_MMU_LANE_COUNT=2",
                   "-DCONFIG_KLIPPER_MMU_POWERED_REWIND=1",
                   "-DCONFIG_KLIPPER_MMU_DIFF_BUFFER=1",
                   "-DCONFIG_KLIPPER_MMU_DEVICE_CUSTOM=0"):
        current = [arg for arg in profile
                   if arg.split("=", 1)[0] != option.split("=", 1)[0]]
        command = list(common) + current + ["-DCONFIG_KLIPPER_MMU_BOOT_IDENTITY_F4X5=1", option]
        result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=30)
        expected = "STM32 diagnostic startup requires Custom/1 or NightOwl/4 passive switched-buffer configuration"
        if result.returncode == 0 or expected not in result.stderr:
            raise SystemExit("F407 diagnostic startup accepted unsupported profile " + option
                             + "\n" + result.stdout + result.stderr)

    g4base = ["-DCONFIG_MACH_STM32G431=1" if arg == "-DCONFIG_MACH_STM32F407=1"
              else "-DCONFIG_CLOCK_FREQ=170000000u" if arg == "-DCONFIG_CLOCK_FREQ=168000000u"
              else arg for arg in common]
    g4base += ["-DCONFIG_STM32_USB_PA11_PA12=1"]
    for chip in ("G431", "G474"):
        target = ["-DCONFIG_MACH_STM32" + chip + "=1" if "CONFIG_MACH_STM32G431=" in arg
                  else arg for arg in g4base]
        for selected in (profile, nightowl):
            identity = "-DCONFIG_KLIPPER_MMU_BOOT_IDENTITY_G4=1"
            command = target + selected + [identity]
            result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=30)
            if result.returncode:
                raise SystemExit("valid " + chip + " passive USB startup failed to compile\n"
                                 + result.stdout + result.stderr)

    g4 = g4base + profile
    expected = "diagnostic_startup.c requires a verified target-specific boot-identity provider"
    result = subprocess.run(g4, text=True, capture_output=True, check=False, timeout=30)
    if result.returncode == 0 or expected not in result.stderr:
        raise SystemExit("G4 diagnostic startup compiled without its identity gate\n"
                         + result.stdout + result.stderr)
    for option in ("-DCONFIG_CLOCK_FREQ=160000000u",
                   "-DCONFIG_STM32_USB_PA11_PA12=0",
                   "-DCONFIG_STM32_CLOCK_REF_25M=1"):
        command = [arg for arg in g4base if arg.split("=", 1)[0] != option.split("=", 1)[0]]
        command += profile + ["-DCONFIG_KLIPPER_MMU_BOOT_IDENTITY_G4=1", option]
        result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=30)
        if result.returncode == 0 or expected not in result.stderr:
            raise SystemExit("G4 diagnostic startup accepted invalid clock/USB option "
                             + option + "\n" + result.stdout + result.stderr)
    for option in ("-DCONFIG_KLIPPER_MMU_LANE_COUNT=2",
                   "-DCONFIG_KLIPPER_MMU_POWERED_REWIND=1",
                   "-DCONFIG_KLIPPER_MMU_DIFF_BUFFER=1",
                   "-DCONFIG_KLIPPER_MMU_DEVICE_CUSTOM=0"):
        command = [arg for arg in g4base + profile
                   if arg.split("=", 1)[0] != option.split("=", 1)[0]]
        command += ["-DCONFIG_KLIPPER_MMU_BOOT_IDENTITY_G4=1", option]
        expected_profile = "STM32 diagnostic startup requires Custom/1 or NightOwl/4 passive switched-buffer configuration"
        result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=30)
        if result.returncode == 0 or expected_profile not in result.stderr:
            raise SystemExit("G4 diagnostic startup accepted unsupported profile " + option
                             + "\n" + result.stdout + result.stderr)
    missing = "diagnostic_startup.c requires a verified target-specific boot-identity provider"
    for mach, freq, wrong in (("H743", 400000000, 480000000),
                              ("H750", 480000000, 400000000)):
        h7 = [f"-DCONFIG_MACH_STM32{mach}=1" if arg == "-DCONFIG_MACH_STM32F407=1"
              else f"-DCONFIG_CLOCK_FREQ={freq}u" if arg == "-DCONFIG_CLOCK_FREQ=168000000u"
              else arg for arg in common]
        gate = ["-DCONFIG_KLIPPER_MMU_BOOT_IDENTITY_H743_H750=1",
                "-DCONFIG_STM32_USB_PA11_PA12=1"]
        for selected in (profile, nightowl):
            result = subprocess.run(h7 + selected + gate, text=True, capture_output=True,
                                    check=False, timeout=30)
            if result.returncode:
                raise SystemExit(f"valid {mach} passive startup failed to compile\n"
                                 + result.stdout + result.stderr)
        rejected = {
            "identity": gate[1:],
            "usb-route": gate[:1],
            "clock": [f"-DCONFIG_CLOCK_FREQ={wrong}u" if arg == f"-DCONFIG_CLOCK_FREQ={freq}u"
                      else arg for arg in h7] + profile + gate,
        }
        for name, extra in rejected.items():
            command = extra if name == "clock" else h7 + profile + extra
            result = subprocess.run(command, text=True, capture_output=True,
                                    check=False, timeout=30)
            if result.returncode == 0 or missing not in result.stderr:
                raise SystemExit(f"{mach} diagnostic startup accepted missing {name}\n"
                                 + result.stdout + result.stderr)
        two_lanes = ["-DCONFIG_KLIPPER_MMU_LANE_COUNT=2" if arg.startswith("-DCONFIG_KLIPPER_MMU_LANE_COUNT=")
                     else arg for arg in profile]
        result = subprocess.run(h7 + two_lanes + gate,
                                text=True, capture_output=True, check=False, timeout=30)
        if result.returncode == 0 or "Custom/1 or NightOwl/4" not in result.stderr:
            raise SystemExit(f"{mach} diagnostic startup accepted two lanes\n"
                             + result.stdout + result.stderr)

    for option in ("-DCONFIG_KLIPPER_MMU_LANE_COUNT=2",
                   "-DCONFIG_KLIPPER_MMU_POWERED_REWIND=1",
                   "-DCONFIG_KLIPPER_MMU_DIFF_BUFFER=1",
                   "-DCONFIG_KLIPPER_MMU_DEVICE_CUSTOM=0"):
        current = [arg for arg in profile
                   if arg.split("=", 1)[0] != option.split("=", 1)[0]]
        command = list(common) + current + ["-DCONFIG_KLIPPER_MMU_BOOT_IDENTITY_F4X5=1", option]
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        expected = "STM32 diagnostic startup requires Custom/1 or NightOwl/4 passive switched-buffer configuration"
        if result.returncode == 0 or expected not in result.stderr:
            raise SystemExit("F407 diagnostic startup accepted unsupported profile " + option
                             + "\n" + result.stdout + result.stderr)
    # Host epoch: admitted targets compile without an RNG option; profile,
    # target, and provider-exclusion gates still reject.
    host = ["-DCONFIG_MACH_STM32=1", "-DCONFIG_KLIPPER_MMU_BOOT_IDENTITY_HOST=1"]
    for mach, freq in (("F407", 168000000), ("F429", 168000000), ("F765", 216000000),
                       ("F207", 120000000), ("G474", 170000000), ("H743", 400000000),
                       ("H750", 480000000), ("G0B0", 64000000), ("G0B1", 64000000)):
        target = [f"-DCONFIG_MACH_STM32{mach}=1" if arg == "-DCONFIG_MACH_STM32F407=1"
                  else f"-DCONFIG_CLOCK_FREQ={freq}u" if arg == "-DCONFIG_CLOCK_FREQ=168000000u"
                  else arg for arg in common]
        gate = (["-DCONFIG_KLIPPER_MMU_G0B_BOOT_GATES=1",
                 "-DCONFIG_STM32_USB_PA11_PA12=1"] if mach.startswith("G0B") else [])
        for selected in (profile, nightowl):
            result = subprocess.run(target + selected + host + gate, text=True, capture_output=True,
                                    check=False, timeout=30)
            if result.returncode:
                raise SystemExit(f"valid {mach} host-epoch startup failed to compile\n"
                                 + result.stdout + result.stderr)
        two_lanes = ["-DCONFIG_KLIPPER_MMU_LANE_COUNT=2" if arg.startswith("-DCONFIG_KLIPPER_MMU_LANE_COUNT=")
                     else arg for arg in profile]
        result = subprocess.run(target + two_lanes + host + gate, text=True, capture_output=True,
                                check=False, timeout=30)
        if result.returncode == 0 or "Custom/1 or NightOwl/4" not in result.stderr:
            raise SystemExit(f"{mach} host-epoch startup accepted two lanes\n"
                             + result.stdout + result.stderr)
        if mach.startswith("G0B"):
            rejected = {
                "missing profile gate": target + profile + host + ["-DCONFIG_STM32_USB_PA11_PA12=1"],
                "missing USB route": target + profile + host + ["-DCONFIG_KLIPPER_MMU_G0B_BOOT_GATES=1"],
                "wrong clock": [f"-DCONFIG_CLOCK_FREQ=48000000u" if arg == f"-DCONFIG_CLOCK_FREQ={freq}u"
                                else arg for arg in target] + profile + host + gate,
            }
            for reason, command in rejected.items():
                result = subprocess.run(command, text=True, capture_output=True,
                                        check=False, timeout=30)
                if result.returncode == 0 or "G0B host-epoch startup requires" not in result.stderr:
                    raise SystemExit(f"{mach} host-epoch startup accepted {reason}\n"
                                     + result.stdout + result.stderr)
    result = subprocess.run(common + profile + host + ["-DCONFIG_KLIPPER_MMU_BOOT_IDENTITY_F4X5=1"],
                            text=True, capture_output=True, check=False, timeout=30)
    if result.returncode == 0 or "mutually exclusive" not in result.stderr:
        raise SystemExit("host epoch compiled with a hardware provider\n"
                         + result.stdout + result.stderr)
    f103 = ["-DCONFIG_MACH_STM32F103=1" if arg == "-DCONFIG_MACH_STM32F407=1"
            else "-DCONFIG_CLOCK_FREQ=72000000u" if arg == "-DCONFIG_CLOCK_FREQ=168000000u"
            else arg for arg in common]
    result = subprocess.run(f103 + profile + host, text=True, capture_output=True,
                            check=False, timeout=30)
    if result.returncode == 0 or "admitted target profile gate" not in result.stderr:
        raise SystemExit("host epoch admitted a target without a profile gate\n"
                         + result.stdout + result.stderr)
    print("PASS: F407/F405/F429/F765/F207, G431/G474, H743/H750, and G0B0/G0B1 startup gates compile/reject as expected")


if __name__ == "__main__":
    main()
