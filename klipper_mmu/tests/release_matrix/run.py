#!/usr/bin/env python3
"""Bounded, software-only installed Klipper release smoke test."""
# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile

PACKAGE = Path(__file__).resolve().parents[2]
# The project directory holding build/ is beside the main checkout, also when
# this runs from a linked worktree.
_COMMON = subprocess.run(["git", "-C", str(PACKAGE), "rev-parse", "--path-format=absolute",
                          "--git-common-dir"], capture_output=True, text=True).stdout.strip()
PROJECT = (Path(_COMMON).parent if _COMMON else PACKAGE).parent
CONFIGS_DIR = PACKAGE / "configs"
PIN = "ce7002bedf37e938bb483572949f3703ac6476cb"
ARCHIVE = PROJECT / "build" / f"klipper-{PIN}.tar.gz"
ARCHIVE_SHA256 = "da1c6223d90652cda6e826e3cf896cf97d6e11aa13aa4c8feb5232dac068f70b"
INSTALLER = PACKAGE / "tools" / "install.py"
BOOT_FAMILIES = {"h723": "H723", "f207": "F207", "f405": "F4X5",
                 "f407": "F4X5", "f429": "F4X5", "f765": "F765", "g431": "G4", "g474": "G4",
                 "h743": "H743_H750", "h750": "H743_H750",
                 "g0b0": "HOST", "g0b1": "HOST"}
PASSIVE_TARGETS = tuple(target for target in BOOT_FAMILIES if target != "h723")
GATE_FAMILIES = {"g0b0": "G0B", "g0b1": "G0B"}
# KMS, Angry Beaver, 3MS and QuattroBox 1.0/1.1/2: H723 four-lane software
# candidates sharing NightOwl's compile gate (no PWM/ADC). Only KMS's traced
# Happy Hare profile renders a per-lane LOAD/exit switch; the others home to
# one shared sensor instead, like a device without the AFC Lite preset.
PASSIVE4_SYMS = {"kms": "KMS", "angry_beaver": "ANGRY_BEAVER", "3ms": "THREE_MS",
                 "quattrobox_1_0": "QUATTROBOX_1_0", "quattrobox_1_1": "QUATTROBOX_1_1",
                 "quattrobox_2": "QUATTROBOX_2"}
PASSIVE4_DEVICES = tuple(PASSIVE4_SYMS)
PASSIVE4_NAMES = {"kms": "KMS", "angry_beaver": "Angry Beaver", "3ms": "3MS",
                  "quattrobox_1_0": "QuattroBox 1.0", "quattrobox_1_1": "QuattroBox 1.1",
                  "quattrobox_2": "QuattroBox 2"}
# ERCF 1.1/2.0/3.0, Tradrack (LINEAR: stepper axis + grip servo), 3D Chameleon,
# MMX6, HTLF (ROTARY: stepper axis, per-gate gear direction), MMX, Low Rider,
# Pico MMU (SERVO: no selector stepper, one servo angle per gate -- Pico MMU
# ships uncalibrated, docs/selectors.md) and BTT ViViD, Claymore (INDEXED: one
# index switch per gate, no gate position table). One illustrative H723 pin
# map shape per kind (selector.h); KLIPPER_MMU_LANE_COUNT's own per-device
# Kconfig default already matches each dossier's gate count (mcu/Kconfig.devices),
# so configure() below never overrides it. tests/klipper_selector_runtime covers
# the hosted-runtime proof (boot to READY, HOME, one selection) for every one
# of these devices, not just one representative per kind.
SELECTOR_SYMS = {"ercf_1_1": "ERCF_1_1", "ercf_2_0": "ERCF_2_0", "ercf_3_0": "ERCF_3_0",
                 "jack_rabbit": "ERCF_3_0", "tradrack": "TRADRACK", "mmx": "MMX", "mmx6": "MMX6",
                 "chameleon_3d": "CHAMELEON_3D", "htlf": "HTLF", "low_rider": "LOW_RIDER",
                 "vivid": "BTT_VIVID", "claymore": "CLAYMORE", "pico_mmu": "PICO_MMU"}
SELECTOR_KIND = {"ercf_1_1": "LINEAR", "ercf_2_0": "LINEAR", "ercf_3_0": "LINEAR",
                 "jack_rabbit": "LINEAR",
                 "tradrack": "LINEAR", "chameleon_3d": "ROTARY", "mmx6": "ROTARY",
                 "htlf": "ROTARY", "mmx": "SERVO", "low_rider": "SERVO", "vivid": "INDEXED",
                 "claymore": "INDEXED", "pico_mmu": "SERVO"}
SELECTOR_GATES = {"ercf_1_1": 9, "ercf_2_0": 8, "ercf_3_0": 8, "jack_rabbit": 8, "tradrack": 10, "mmx6": 6,
                  "chameleon_3d": 4, "htlf": 4, "mmx": 4, "low_rider": 6, "vivid": 4,
                  "claymore": 4, "pico_mmu": 4}
SELECTOR_DEVICES = tuple(SELECTOR_SYMS)
SELECTOR_NAMES = {"ercf_1_1": "ERCF 1.1", "ercf_2_0": "ERCF 2.0", "ercf_3_0": "ERCF 3.0",
                  "jack_rabbit": "ERCF 3.0 Jack Rabbit",
                  "tradrack": "Tradrack", "mmx": "MMX", "mmx6": "MMX6",
                  "chameleon_3d": "3D Chameleon", "htlf": "HTLF", "low_rider": "Low Rider",
                  "vivid": "BTT ViViD", "claymore": "Claymore", "pico_mmu": "Pico MMU"}
# Claymore is the one SELECTOR_SYMS entry with per-lane BAY switches (its own
# LOAD1-4, see mcu/Kconfig.devices) and powered rewind; every other selector
# device has neither (klipper_mmu/selector.h; shared rules).
SELECTOR_BAY_AND_REWIND = "claymore"
# Jack Rabbit is ERCF 3.0 (same KLIPPER_MMU_DEVICE_ERCF_3_0 symbol above) plus
# the mod that clears the grip servo (mcu/Kconfig.devices' KLIPPER_MMU_ERCF_
# JACK_RABBIT); it needs its own image row because it changes the pin shape
# (no SERVO pin) that every other LINEAR/SERVO selector device claims.
SELECTOR_MOD_SYMS = {"jack_rabbit": "KLIPPER_MMU_ERCF_JACK_RABBIT"}
SELECTOR_NO_SERVO = ("jack_rabbit",)
# Every implemented selector device gets a hosted-runtime proof (tests/
# klipper_selector_runtime): boot to READY with the selector bound, HOME
# completes, one selection lands.
SELECTOR_RUNTIME_DEVICES = SELECTOR_DEVICES
DRIVER_SOURCES = ("src/tmcuart.c", "src/stm32/hard_pwm.c", "src/stm32/stm32h7_adc.c",
                  "src/stm32/stm32h7_gpio.c", "src/stm32/gpio.c", "src/stm32/gpioperiph.c")
DRIVER_OPTIONS = ("WANT_ADC_LOCAL", "WANT_HARD_PWM_QUERY", "WANT_HARD_PWM_LOCAL_PAIR",
                  "HARD_PWM_LOCAL_MAX_PAIRS", "WANT_TMCUART_LOCAL",
                  "TMCUART_LOCAL_MAX_CHANNELS", "WANT_GPIO_LOCAL")
# Every row in this matrix targets an STM32 part (configure() always sets
# MACH_STM32), so Klipper's Makefile always resolves CC to arm-none-eabi-gcc
# (src/stm32/Makefile: CROSS_PREFIX=arm-none-eabi-); wrapping that literal
# compiler in ccache is safe for every row. Verified on the h723/boxturtle
# target: with a cleared CCACHE_DIR, a ccache-wrapped build and a plain build
# of the same .config produced byte-identical linked code -- the only
# differing bytes in klipper.elf are the build-time Version blob embedded by
# scripts/make_version.py, which differs by the same two words between any
# two plain (non-ccache) builds too.
JOBS = f"-j{os.cpu_count() or 2}"
CC_ARGS = ["CC=ccache arm-none-eabi-gcc"] if shutil.which("ccache") else []


def run(*args, cwd=None, limit=120, env=None):
    result = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=limit, env=env)
    if result.returncode:
        raise RuntimeError(f"{' '.join(map(str, args))} failed:\n{result.stdout[-5000:]}")
    return result.stdout


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def save_image(root, build, name, **details):
    artifact = build / "artifacts" / name
    artifact.mkdir(parents=True)
    files = (".config", "klipper.elf", "klipper.bin", "klipper.dict", "autoconf.h")
    for filename in files:
        source = root / filename if filename == ".config" else root / "out" / filename
        if not source.is_file():
            raise AssertionError(f"{name} is missing {filename}")
        shutil.copy2(source, artifact / filename)
    (artifact / "manifest.json").write_text(json.dumps({
        "upstream_commit": PIN,
        "upstream_archive_sha256": ARCHIVE_SHA256,
        "product_commit": run("git", "rev-parse", "HEAD", cwd=PACKAGE).strip(),
        "product_dirty": bool(run("git", "status", "--porcelain", cwd=PACKAGE).strip()),
        "sha256": {filename: digest(artifact / filename) for filename in files},
        **details,
    }, indent=2, sort_keys=True) + "\n")
    return artifact


def driver_units(root, log):
    """Preprocess each compiled patched driver with its logged build command."""
    units = {}
    for line in log.splitlines():
        if " -c src/" not in line:
            continue
        words = shlex.split(line)
        source, output = words.index("-c") + 1, words.index("-o")
        if words[source] not in DRIVER_SOURCES:
            continue
        # Same compiler and flags. Without -MD no dependency file is written;
        # -g0 keeps -ggdb3 from emitting the (different) autoconf.h defines.
        command = [word for index, word in enumerate(words)
                   if index not in (source - 1, source, output, output + 1) and word != "-MD"]
        result = subprocess.run(command + ["-g0", "-E", "-P", words[source]], cwd=root,
                                text=True, capture_output=True, timeout=60)
        if result.returncode:
            raise RuntimeError(f"preprocessing {words[source]} failed:\n{result.stderr[-5000:]}")
        units[words[source]] = result.stdout
    if not {"src/tmcuart.c", "src/stm32/hard_pwm.c", "src/stm32/gpioperiph.c"} <= set(units):
        raise AssertionError(f"stock build did not compile the patched drivers: {sorted(units)}")
    return units


def stock_driver_units(root, target):
    """Build an image without the add-on menu; return its patched driver units."""
    configure(root, target, False, addon=False)
    log = run("make", JOBS, *CC_ARGS, "V=1", cwd=root, limit=180)
    header = (root / "out/autoconf.h").read_text()
    if any(f"CONFIG_{name} " in header for name in DRIVER_OPTIONS + ("KLIPPER_MMU",)):
        raise AssertionError("add-on or driver API options defined without the add-on menu")
    units = driver_units(root, log)
    run("make", "clean", cwd=root, limit=30)
    return units


def configure(root, target, enabled, diff_buffer=False, actuation=False,
              transport="usb", device="boxturtle", addon=True):
    boot_family = BOOT_FAMILIES[target]
    if enabled and addon and diff_buffer and target == "h723" and device == "boxturtle":
        # The analog boxturtle-h723-{usb,can}[-safe] images ship a maintained
        # defconfig fragment (also used by docs/boxturtle-quick-start.md);
        # copy it instead of re-deriving the same settings below.
        suffix = "" if actuation else "-safe"
        shutil.copy2(CONFIGS_DIR / f"boxturtle-h723-{transport}{suffix}.config",
                     root / ".config")
    else:
        # Native Klipper Kconfig engine, the same backend used by make menuconfig.
        script = '''import os, sys
sys.path.insert(0, os.path.abspath("lib/kconfiglib"))
import kconfiglib
k = kconfiglib.Kconfig("src/Kconfig")
def set_(name, value):
    symbol = k.syms[name]
    if not symbol.set_value(value): raise SystemExit("invalid " + name)
PASSIVE4_SYMS = {"kms": "KMS", "angry_beaver": "ANGRY_BEAVER", "3ms": "THREE_MS",
                 "quattrobox_1_0": "QUATTROBOX_1_0", "quattrobox_1_1": "QUATTROBOX_1_1",
                 "quattrobox_2": "QUATTROBOX_2"}
SELECTOR_SYMS = {"ercf_1_1": "ERCF_1_1", "ercf_2_0": "ERCF_2_0", "ercf_3_0": "ERCF_3_0",
                 "jack_rabbit": "ERCF_3_0", "tradrack": "TRADRACK", "mmx": "MMX", "mmx6": "MMX6",
                 "chameleon_3d": "CHAMELEON_3D", "htlf": "HTLF", "low_rider": "LOW_RIDER",
                 "vivid": "BTT_VIVID", "claymore": "CLAYMORE", "pico_mmu": "PICO_MMU"}
SELECTOR_KIND = {"ercf_1_1": "LINEAR", "ercf_2_0": "LINEAR", "ercf_3_0": "LINEAR",
                 "jack_rabbit": "LINEAR",
                 "tradrack": "LINEAR", "chameleon_3d": "ROTARY", "mmx6": "ROTARY",
                 "htlf": "ROTARY", "mmx": "SERVO", "low_rider": "SERVO", "vivid": "INDEXED",
                 "claymore": "INDEXED", "pico_mmu": "SERVO"}
SELECTOR_GATES = {"ercf_1_1": 9, "ercf_2_0": 8, "ercf_3_0": 8, "jack_rabbit": 8, "tradrack": 10, "mmx6": 6,
                  "chameleon_3d": 4, "htlf": 4, "mmx": 4, "low_rider": 6, "vivid": 4,
                  "claymore": 4, "pico_mmu": 4}
SELECTOR_BAY_AND_REWIND = "claymore"
SELECTOR_MOD_SYMS = {"jack_rabbit": "KLIPPER_MMU_ERCF_JACK_RABBIT"}
SELECTOR_NO_SERVO = ("jack_rabbit",)
set_("MACH_STM32", "y")
target = sys.argv[2]
boot_family = sys.argv[7]
set_("MACH_STM32" + target.upper(), "y")
if target == "h723":
    # AFC Lite manual: USB uses no bootloader; CAN on PB8/PB9 uses 128 KiB.
    set_("LOW_LEVEL_OPTIONS", "y")
    set_("STM32_FLASH_START_0000" if sys.argv[5] == "usb" else
         "STM32_FLASH_START_20000", "y")
    set_("STM32_CLOCK_REF_25M", "y")
    set_("USBSERIAL" if sys.argv[5] == "usb" else
         "STM32_MMENU_CANBUS_PB8_PB9", "y")
else:
    set_("STM32_FLASH_START_0000", "y")
    set_("USBSERIAL", "y")
    if target in ("f207", "f429", "f765", "g431", "g474", "h743", "h750", "g0b0", "g0b1"):
        set_("STM32_USB_PA11_PA12", "y")
if sys.argv[8] == "addon":
    set_("KLIPPER_MMU", "y" if sys.argv[1] == "on" else "n")
if sys.argv[1] == "on":
    device = sys.argv[6]
    if target == "h723" and device == "emu":
        set_("KLIPPER_MMU_DEVICE_EMU", "y")
        set_("KLIPPER_MMU_BOARD_CUSTOM", "y")
    elif device == "nightowl":
        set_("KLIPPER_MMU_DEVICE_NIGHTOWL", "y")
        set_("KLIPPER_MMU_BOARD_CUSTOM", "y")
    elif target == "h723" and device in PASSIVE4_SYMS:
        set_("KLIPPER_MMU_DEVICE_" + PASSIVE4_SYMS[device], "y")
        set_("KLIPPER_MMU_BOARD_CUSTOM", "y")
    elif target == "h723" and device in SELECTOR_SYMS:
        set_("KLIPPER_MMU_DEVICE_" + SELECTOR_SYMS[device], "y")
        set_("KLIPPER_MMU_BOARD_CUSTOM", "y")
        if device in SELECTOR_MOD_SYMS:
            set_(SELECTOR_MOD_SYMS[device], "y")
    elif target == "h723" and device == "qidi_box":
        set_("KLIPPER_MMU_DEVICE_QIDI_BOX", "y")
        set_("KLIPPER_MMU_BOARD_CUSTOM", "y")
    elif target in ("g0b0", "g0b1") and device == "angry_beaver":
        set_("KLIPPER_MMU_DEVICE_ANGRY_BEAVER", "y")
        set_("KLIPPER_MMU_BOARD_CUSTOM", "y")
    elif target in ("g0b0", "g0b1") and device in ("ercf_2_0", "ercf_3_0"):
        set_("KLIPPER_MMU_DEVICE_" + SELECTOR_SYMS[device], "y")
        set_("KLIPPER_MMU_BOARD_CUSTOM", "y")
    elif target != "h723" and device == "f4custom":
        set_("KLIPPER_MMU_DEVICE_CUSTOM", "y")
        set_("KLIPPER_MMU_BOARD_CUSTOM", "y")
    else:
        set_("KLIPPER_MMU_DEVICE_BOXTURTLE" if target == "h723" else "KLIPPER_MMU_DEVICE_CUSTOM", "y")
        set_("KLIPPER_MMU_BOARD_AFC_LITE" if target == "h723" else "KLIPPER_MMU_BOARD_CUSTOM", "y")
    set_("KLIPPER_MMU_PIN_CONFIG", "y")
    if target == "h723" and device == "emu":
        set_("KLIPPER_MMU_LANE_COUNT", "5")
        for name, pin in (("STEP", "PD0,PD1,PD2,PD3,PD4"),
                          ("DIR", "PD5,PD6,PD7,PD8,PD9"),
                          ("ENABLE", "PD10,PD11,PD12,PD13,PD14"),
                          ("UART", "PE0,PE1,PE2,PE3,PE4"),
                          ("BAY", "PE5,PE6,PE7,PE8,PE9"),
                          ("LOAD", "PE10,PE11,PE12,PE13,PE14"),
                          ("TOOLHEAD", "PC0"),
                          ("BUFFER_A", "PC1"), ("BUFFER_B", "PC2")):
            set_("KLIPPER_MMU_PINS_" + name, pin)
        # Explicit illustrative wiring values; no Custom-board electrical
        # defaults are inferred from the selected device name.
        for name, value in (("FEEDER_ENABLE_HIGH", "0,0,0,0,0"),
                            ("FEEDER_STEP_HIGH", "1,1,1,1,1"),
                            ("FEEDER_FORWARD_HIGH", "1,1,1,1,1"),
                            ("TMC_ADDRESSES", "0,0,0,0,0"),
                            ("TMC_UART_PULLUP", "1,1,1,1,1"),
                            ("LOAD_ACTIVE_HIGH", "0,0,0,0,0"),
                            ("LOAD_PULL", "1,1,1,1,1"),
                            ("BAY_ACTIVE_HIGH", "0,0,0,0,0"),
                            ("BAY_PULL", "1,1,1,1,1"),
                            ("BUFFER_ACTIVE_HIGH", "0,0"),
                            ("BUFFER_PULL", "1,1")):
            set_("KLIPPER_MMU_" + name, value)
        set_("KLIPPER_MMU_TMC_RSENSE", "110")
        set_("KLIPPER_MMU_TMC_CURRENT_CEILING", "800")
        set_("KLIPPER_MMU_RUN_CURRENT", "700")
        set_("KLIPPER_MMU_HOLD_CURRENT", "150")
        set_("KLIPPER_MMU_TOOLHEAD_ACTIVE_HIGH", "0")
        set_("KLIPPER_MMU_TOOLHEAD_PULL", "1")
        set_("KLIPPER_MMU_BUFFER_LOW_A", "y")
        set_("KLIPPER_MMU_BOOT_IDENTITY_H723", "y")
        set_("KLIPPER_MMU_DIAGNOSTIC_STARTUP", "y")
        if sys.argv[4] == "actuation":
            set_("KLIPPER_MMU_ACTUATION_OPT_IN", "y")
    elif device == "nightowl":
        set_("KLIPPER_MMU_LANE_COUNT", "4")
        pins = (("STEP", "PD0,PD1,PD2,PD3"),
                          ("DIR", "PD4,PD5,PD6,PD7"),
                          ("ENABLE", "PD8,PD9,PD10,PD11"),
                          ("UART", "PE0,PE1,PE2,PE3"),
                          ("BAY", "PE4,PE5,PE6,PE7"),
                          ("LOAD", "PE8,PE9,PE10,PE11"),
                          ("HUB", "PC0"), ("TOOLHEAD", "PC1"),
                          ("BUFFER_A", "PC2"), ("BUFFER_B", "PC3")) if target == "h723" else (
                          ("STEP", "PA1,PA2,PA3,PA4"),
                          ("DIR", "PA5,PA6,PA7,PA8"),
                          ("ENABLE", "PB0,PB1,PB2,PB3"),
                          ("UART", "PC4,PC5,PC6,PC7"),
                          ("BAY", "PB12,PB13,PB14,PB15"),
                          ("LOAD", "PB8,PB9,PB10,PB11"),
                          ("HUB", "PC0"), ("TOOLHEAD", "PC1"),
                          ("BUFFER_A", "PC2"), ("BUFFER_B", "PC3"))
        for name, pin in pins:
            set_("KLIPPER_MMU_PINS_" + name, pin)
        for name, value in (("FEEDER_ENABLE_HIGH", "0,0,0,0"),
                            ("FEEDER_STEP_HIGH", "1,1,1,1"),
                            ("FEEDER_FORWARD_HIGH", "1,1,1,1"),
                            ("TMC_ADDRESSES", "0,0,0,0"),
                            ("TMC_UART_PULLUP", "1,1,1,1"),
                            ("LOAD_ACTIVE_HIGH", "0,0,0,0"),
                            ("LOAD_PULL", "1,1,1,1"),
                            ("BAY_ACTIVE_HIGH", "0,0,0,0"),
                            ("BAY_PULL", "1,1,1,1"),
                            ("HUB_ACTIVE_HIGH", "0"), ("HUB_PULL", "1"),
                            ("TOOLHEAD_ACTIVE_HIGH", "0"), ("TOOLHEAD_PULL", "1"),
                            ("BUFFER_ACTIVE_HIGH", "0,0"), ("BUFFER_PULL", "1,1")):
            set_("KLIPPER_MMU_" + name, value)
        set_("KLIPPER_MMU_LOAD_SENSORS", "y")
        set_("KLIPPER_MMU_TMC_RSENSE", "110")
        set_("KLIPPER_MMU_TMC_CURRENT_CEILING", "800")
        set_("KLIPPER_MMU_RUN_CURRENT", "700")
        set_("KLIPPER_MMU_HOLD_CURRENT", "150")
        set_("KLIPPER_MMU_BUFFER_LOW_A", "y")
        set_("KLIPPER_MMU_BOOT_IDENTITY_" + boot_family, "y")
        set_("KLIPPER_MMU_DIAGNOSTIC_STARTUP", "y")
        if sys.argv[4] == "actuation":
            set_("KLIPPER_MMU_ACTUATION_OPT_IN", "y")
    elif target == "h723" and device in PASSIVE4_SYMS:
        # KMS, Angry Beaver, 3MS and QuattroBox 1.0/1.1/2 share NightOwl's
        # H723 pin map. Only KMS has per-lane BAY and LOAD switches, and its
        # LOAD is the virtual HUB; the others have one HUB and no BAY.
        has_load = device == "kms"
        pins = [("STEP", "PD0,PD1,PD2,PD3"), ("DIR", "PD4,PD5,PD6,PD7"),
                ("ENABLE", "PD8,PD9,PD10,PD11"), ("UART", "PE0,PE1,PE2,PE3"),
                ("TOOLHEAD", "PC1"), ("BUFFER_A", "PC2"), ("BUFFER_B", "PC3")]
        pins += ([("BAY", "PE4,PE5,PE6,PE7"), ("LOAD", "PE8,PE9,PE10,PE11")]
                 if has_load else [("HUB", "PC0")])
        for name, pin in pins:
            set_("KLIPPER_MMU_PINS_" + name, pin)
        for name, value in (("FEEDER_ENABLE_HIGH", "0,0,0,0"),
                            ("FEEDER_STEP_HIGH", "1,1,1,1"),
                            ("FEEDER_FORWARD_HIGH", "1,1,1,1"),
                            ("TMC_ADDRESSES", "0,0,0,0"),
                            ("TMC_UART_PULLUP", "1,1,1,1"),
                            ("TOOLHEAD_ACTIVE_HIGH", "0"), ("TOOLHEAD_PULL", "1"),
                            ("BUFFER_ACTIVE_HIGH", "0,0"), ("BUFFER_PULL", "1,1")):
            set_("KLIPPER_MMU_" + name, value)
        if not has_load:
            set_("KLIPPER_MMU_HUB_ACTIVE_HIGH", "0")
            set_("KLIPPER_MMU_HUB_PULL", "1")
        if has_load:
            set_("KLIPPER_MMU_BAY_ACTIVE_HIGH", "0,0,0,0")
            set_("KLIPPER_MMU_BAY_PULL", "1,1,1,1")
            set_("KLIPPER_MMU_LOAD_SENSORS", "y")
            set_("KLIPPER_MMU_LOAD_ACTIVE_HIGH", "0,0,0,0")
            set_("KLIPPER_MMU_LOAD_PULL", "1,1,1,1")
        set_("KLIPPER_MMU_TMC_RSENSE", "110")
        set_("KLIPPER_MMU_TMC_CURRENT_CEILING", "800")
        set_("KLIPPER_MMU_RUN_CURRENT", "700")
        set_("KLIPPER_MMU_HOLD_CURRENT", "150")
        set_("KLIPPER_MMU_BUFFER_LOW_A", "y")
        set_("KLIPPER_MMU_BOOT_IDENTITY_H723", "y")
        set_("KLIPPER_MMU_DIAGNOSTIC_STARTUP", "y")
        if sys.argv[4] == "actuation":
            set_("KLIPPER_MMU_ACTUATION_OPT_IN", "y")
    elif target == "h723" and device == "qidi_box":
        # QIDI Box: independent feeders like the passive four, but its own
        # shape (docs/devices/qidi_box.md), not theirs -- a REAL shared HUB
        # (KLIPPER_MMU_VIRTUAL_HUB stays unselected, unlike KMS/EMU), per-lane
        # BAY entry switches (KLIPPER_MMU_BAY_SENSORS' ordinary default),
        # no gear TMC UART chip (KLIPPER_MMU_GEAR_TMC_UART defaults off for
        # this device, so KLIPPER_MMU_PINS_UART/TMC_ADDRESSES/TMC_UART_PULLUP
        # are not offered), and a tension-only buffer (KLIPPER_MMU_
        # BUFFER_SINGLE_SWITCH, one pin, one BUFFER_ACTIVE_HIGH/PULL value).
        pins = [("STEP", "PD0,PD1,PD2,PD3"), ("DIR", "PD4,PD5,PD6,PD7"),
                ("ENABLE", "PD8,PD9,PD10,PD11"),
                ("BAY", "PE4,PE5,PE6,PE7"), ("HUB", "PC0"), ("TOOLHEAD", "PC1"),
                ("BUFFER_A", "PC2")]
        for name, pin in pins:
            set_("KLIPPER_MMU_PINS_" + name, pin)
        for name, value in (("FEEDER_ENABLE_HIGH", "0,0,0,0"),
                            ("FEEDER_STEP_HIGH", "1,1,1,1"),
                            ("FEEDER_FORWARD_HIGH", "1,1,1,1"),
                            ("BAY_ACTIVE_HIGH", "0,0,0,0"), ("BAY_PULL", "1,1,1,1"),
                            ("HUB_ACTIVE_HIGH", "0"), ("HUB_PULL", "1"),
                            ("TOOLHEAD_ACTIVE_HIGH", "0"), ("TOOLHEAD_PULL", "1"),
                            ("BUFFER_ACTIVE_HIGH", "0"), ("BUFFER_PULL", "1")):
            set_("KLIPPER_MMU_" + name, value)
        set_("KLIPPER_MMU_BUFFER_SINGLE_SWITCH", "y")
        set_("KLIPPER_MMU_BOOT_IDENTITY_H723", "y")
        # Hosted diagnostic startup's depends line now admits a gear with no
        # shared TMC UART via KLIPPER_MMU_QIDI_BOOT_GATES (mcu/Kconfig), so
        # this device reaches READY through the plain STEP/DIR path like the
        # other standalone devices above, still never selecting
        # WANT_TMCUART_LOCAL/KLIPPER_MMU_TMC_UART_SHARED (no chip to confirm).
        set_("KLIPPER_MMU_DIAGNOSTIC_STARTUP", "y")
        if sys.argv[4] == "actuation":
            set_("KLIPPER_MMU_ACTUATION_OPT_IN", "y")
    elif target == "h723" and device in SELECTOR_SYMS:
        # ERCF 1.1/2.0/3.0, Tradrack (LINEAR), 3D Chameleon, MMX6, HTLF
        # (ROTARY) and MMX (SERVO) share one illustrative H723 pin shape: a
        # single shared gear driver, a selector stepper axis for LINEAR/ROTARY,
        # a servo for LINEAR/SERVO, one shared HUB gate sensor, no BAY/LOAD
        # (selector.h; shared rules). A LINEAR/ROTARY selector's own TMC2209
        # talks over its own UART pin (KLIPPER_MMU_SELECTOR_UART defaults on).
        kind = SELECTOR_KIND[device]
        gates = SELECTOR_GATES[device]
        has_bay_and_rewind = device == SELECTOR_BAY_AND_REWIND
        # KLIPPER_MMU_BAY_SENSORS defaults to y for every device except the
        # no-BAY passive four; every selector device except Claymore has no
        # BAY/LOAD switches either (Claymore's own LOAD1-4 implement this
        # catalogue's BAY role instead -- mcu/Kconfig.devices), so it keeps
        # the ordinary default on rather than being turned off here.
        if not has_bay_and_rewind:
            set_("KLIPPER_MMU_BAY_SENSORS", "n")
        pins = [("STEP", "PD0"), ("DIR", "PD1"), ("ENABLE", "PD2"), ("UART", "PD3"),
                ("HUB", "PC0"), ("TOOLHEAD", "PC1"),
                ("BUFFER_A", "PC2"), ("BUFFER_B", "PC3")]
        has_encoder = device in ("ercf_1_1", "ercf_2_0", "ercf_3_0", "jack_rabbit")
        if has_encoder:
            # These three default KLIPPER_MMU_ENCODER on (Happy Hare's own
            # MMU_FAMILY_ERCF unconditionally selects MMU_HAS_ENCODER); give
            # the one wire it now requires -- Happy Hare's own encoder is a
            # single pin (installer/Kconfig.encoder:128), so no
            # KLIPPER_MMU_ENCODER_DUAL_PIN here either.
            pins.append(("ENCODER_A", "PC6"))
        if kind != "SERVO":
            pins += [("SELECTOR_STEP", "PD4"), ("SELECTOR_DIR", "PD5"),
                     ("SELECTOR_ENABLE", "PD6"), ("SELECTOR_UART", "PD7")]
        if kind in ("LINEAR", "ROTARY"):
            pins.append(("SELECTOR_ENDSTOP", "PC4"))
        if kind == "INDEXED":
            pins.append(("SELECTOR_GATE", "PE0,PE1,PE2,PE3"))
        if kind in ("LINEAR", "SERVO") and device not in SELECTOR_NO_SERVO:
            pins.append(("SERVO", "PC5"))
        if has_bay_and_rewind:
            # Rewind pins: the AFC Lite preset's validated timer pairs.
            pins += [("BAY", "PF0,PF1,PF2,PF3"),
                     ("REWIND_FORWARD", "PA1,PA7,PB15,PD13"),
                     ("REWIND_REVERSE", "PA0,PA6,PB14,PD12"),
                     ("REWIND_ENABLE", "PA2,PA5,PB13,PD11")]
        for name, pin in pins:
            set_("KLIPPER_MMU_PINS_" + name, pin)
        # The gear is one driver bound to every lane (KM_GEAR_DRIVERS), so its
        # level and TMC lists hold one entry, like its pin lists.
        for name, value in (("FEEDER_ENABLE_HIGH", "0"),
                            ("FEEDER_STEP_HIGH", "1"),
                            ("FEEDER_FORWARD_HIGH", "1"),
                            ("TMC_ADDRESSES", "0"),
                            ("TMC_UART_PULLUP", "1"),
                            ("HUB_ACTIVE_HIGH", "0"), ("HUB_PULL", "1"),
                            ("TOOLHEAD_ACTIVE_HIGH", "0"), ("TOOLHEAD_PULL", "1"),
                            ("BUFFER_ACTIVE_HIGH", "0,0"), ("BUFFER_PULL", "1,1")):
            set_("KLIPPER_MMU_" + name, value)
        if has_encoder:
            set_("KLIPPER_MMU_ENCODER_PULL", "1")
        if kind in ("LINEAR", "ROTARY"):
            set_("KLIPPER_MMU_SELECTOR_ENDSTOP_ACTIVE_HIGH", "0")
            set_("KLIPPER_MMU_SELECTOR_ENDSTOP_PULL", "1")
        if kind == "INDEXED":
            set_("KLIPPER_MMU_SELECTOR_GATE_ACTIVE_HIGH", "0,0,0,0")
            set_("KLIPPER_MMU_SELECTOR_GATE_PULL", "1,1,1,1")
        if has_bay_and_rewind:
            set_("KLIPPER_MMU_BAY_ACTIVE_HIGH", "1,1,1,1")
            set_("KLIPPER_MMU_BAY_PULL", "1,1,1,1")
            set_("KLIPPER_MMU_POWERED_REWIND", "y")
            set_("KLIPPER_MMU_REWIND_CYCLE_US", "50")
            set_("KLIPPER_MMU_REWIND_ENABLE_HIGH", "1,1,1,1")
            set_("KLIPPER_MMU_REWIND_FORWARD_HIGH", "1,1,1,1")
            set_("KLIPPER_MMU_REWIND_REVERSE_HIGH", "1,1,1,1")
            set_("KLIPPER_MMU_REWIND_BRIDGE_CEILING", "600")
            set_("KLIPPER_MMU_REWIND_DEADTIME_US", "50")
            set_("KLIPPER_MMU_REWIND_LATENESS_US", "50")
        set_("KLIPPER_MMU_TMC_RSENSE", "110")
        set_("KLIPPER_MMU_TMC_CURRENT_CEILING", "800")
        set_("KLIPPER_MMU_RUN_CURRENT", "700")
        set_("KLIPPER_MMU_HOLD_CURRENT", "150")
        set_("KLIPPER_MMU_BUFFER_LOW_A", "y")
        set_("KLIPPER_MMU_BOOT_IDENTITY_H723", "y")
        set_("KLIPPER_MMU_DIAGNOSTIC_STARTUP", "y")
        if sys.argv[4] == "actuation":
            set_("KLIPPER_MMU_ACTUATION_OPT_IN", "y")
    elif target in ("g0b0", "g0b1") and device == "angry_beaver":
        # Happy Hare's real MMB 2.0 board (installer/boards/Kconfig.mmb_2_0),
        # its MULTIGEAR block (:24-49): four independent gear drivers, no
        # per-lane BAY/LOAD switch (Angry Beaver's traced profile has
        # neither), one shared exit sensor doubling as this catalogue's HUB
        # role (PIN_SHARED_EXIT_SENSOR, :141-142; used because Angry Beaver
        # has no encoder). PIN_GEAR_DIAG (:20,:33,:38,:43) and PIN_ESPOOLER_
        # RWD (:45-49, powered rewind) have no klipper-mmu pin role and are
        # not claimed. Kconfig.mmb_2_0 wires no toolhead-arrival or buffer
        # switch for this composition; TOOLHEAD/BUFFER_A/BUFFER_B below are
        # illustrative, same shape as the H723 rows above, not board pins.
        pins = [("STEP", "PD4,PC9,PC15,PC13"), ("DIR", "PD3,PC8,PC11,PC12"),
                ("ENABLE", "PD5,PD2,PC10,PC14"), ("UART", "PB5,PB4,PB3,PD6"),
                ("HUB", "PA15"), ("TOOLHEAD", "PC1"),
                ("BUFFER_A", "PC2"), ("BUFFER_B", "PC3")]
        for name, pin in pins:
            set_("KLIPPER_MMU_PINS_" + name, pin)
        for name, value in (("FEEDER_ENABLE_HIGH", "0,0,0,0"),
                            ("FEEDER_STEP_HIGH", "1,1,1,1"),
                            ("FEEDER_FORWARD_HIGH", "1,1,1,1"),
                            ("TMC_ADDRESSES", "0,0,0,0"),
                            ("TMC_UART_PULLUP", "1,1,1,1"),
                            ("HUB_ACTIVE_HIGH", "0"), ("HUB_PULL", "1"),
                            ("TOOLHEAD_ACTIVE_HIGH", "0"), ("TOOLHEAD_PULL", "1"),
                            ("BUFFER_ACTIVE_HIGH", "0,0"), ("BUFFER_PULL", "1,1")):
            set_("KLIPPER_MMU_" + name, value)
        set_("KLIPPER_MMU_TMC_RSENSE", "110")
        set_("KLIPPER_MMU_TMC_CURRENT_CEILING", "800")
        set_("KLIPPER_MMU_RUN_CURRENT", "700")
        set_("KLIPPER_MMU_HOLD_CURRENT", "150")
        set_("KLIPPER_MMU_BUFFER_LOW_A", "y")
        set_("KLIPPER_MMU_BOOT_IDENTITY_" + boot_family, "y")
        set_("KLIPPER_MMU_DIAGNOSTIC_STARTUP", "y")
        if sys.argv[4] == "actuation":
            set_("KLIPPER_MMU_ACTUATION_OPT_IN", "y")
    elif target in ("g0b0", "g0b1") and device in ("ercf_2_0", "ercf_3_0"):
        # ERCF 2.0/3.0 are Happy Hare's other real MMB 2.0 default (installer/
        # mmu_types/Kconfig.ercf:198,242), the board's !MULTIGEAR branch
        # (installer/boards/Kconfig.mmb_2_0:15-120): one gear driver, a
        # LINEAR selector stepper on its own TMC UART wire, a grip servo and
        # an encoder -- all real board pins. That board wires no separate
        # HUB switch for this composition (ERCF's encoder claims the pin
        # MULTIGEAR/no-encoder devices use as PIN_SHARED_EXIT_SENSOR, :126-127);
        # HUB/TOOLHEAD/BUFFER_A/BUFFER_B stay illustrative, the same shape as
        # Angry Beaver's own row above and the H723 selector rows below.
        set_("KLIPPER_MMU_BAY_SENSORS", "n")
        pins = [("STEP", "PD4"), ("DIR", "PD3"), ("ENABLE", "PD5"), ("UART", "PB5"),
                ("SELECTOR_STEP", "PC9"), ("SELECTOR_DIR", "PC8"),
                ("SELECTOR_ENABLE", "PD2"), ("SELECTOR_UART", "PB4"),
                ("SELECTOR_ENDSTOP", "PA15"), ("SERVO", "PA1"),
                ("ENCODER_A", "PC2"),
                ("HUB", "PC0"), ("TOOLHEAD", "PC1"),
                ("BUFFER_A", "PC3"), ("BUFFER_B", "PC4")]
        for name, pin in pins:
            set_("KLIPPER_MMU_PINS_" + name, pin)
        for name, value in (("FEEDER_ENABLE_HIGH", "0"),
                            ("FEEDER_STEP_HIGH", "1"),
                            ("FEEDER_FORWARD_HIGH", "1"),
                            ("TMC_ADDRESSES", "0"),
                            ("TMC_UART_PULLUP", "1"),
                            ("HUB_ACTIVE_HIGH", "0"), ("HUB_PULL", "1"),
                            ("TOOLHEAD_ACTIVE_HIGH", "0"), ("TOOLHEAD_PULL", "1"),
                            ("BUFFER_ACTIVE_HIGH", "0,0"), ("BUFFER_PULL", "1,1"),
                            ("ENCODER_PULL", "1"),
                            ("SELECTOR_ENDSTOP_ACTIVE_HIGH", "0"),
                            ("SELECTOR_ENDSTOP_PULL", "1")):
            set_("KLIPPER_MMU_" + name, value)
        set_("KLIPPER_MMU_TMC_RSENSE", "110")
        set_("KLIPPER_MMU_TMC_CURRENT_CEILING", "800")
        set_("KLIPPER_MMU_RUN_CURRENT", "700")
        set_("KLIPPER_MMU_HOLD_CURRENT", "150")
        set_("KLIPPER_MMU_BUFFER_LOW_A", "y")
        set_("KLIPPER_MMU_BOOT_IDENTITY_" + boot_family, "y")
        set_("KLIPPER_MMU_DIAGNOSTIC_STARTUP", "y")
        if sys.argv[4] == "actuation":
            set_("KLIPPER_MMU_ACTUATION_OPT_IN", "y")
    elif target != "h723" and device == "f4custom":
        set_("KLIPPER_MMU_LANE_COUNT", "1")
        for name, pin in (("STEP", "PA1"), ("DIR", "PA2"), ("ENABLE", "PA3"),
                          ("UART", "PA4"), ("BAY", "PA5"), ("HUB", "PA6"),
                          ("TOOLHEAD", "PA7"), ("BUFFER_A", "PB0"),
                          ("BUFFER_B", "PB1"), ("LOAD", "PB2")):
            set_("KLIPPER_MMU_PINS_" + name, pin)
        set_("KLIPPER_MMU_LOAD_SENSORS", "y")
        for name, value in (("FEEDER_ENABLE_HIGH", "0"), ("FEEDER_STEP_HIGH", "1"),
                            ("FEEDER_FORWARD_HIGH", "1"), ("TMC_ADDRESSES", "0"),
                            ("TMC_UART_PULLUP", "1"), ("BAY_ACTIVE_HIGH", "0"),
                            ("BAY_PULL", "1"), ("LOAD_ACTIVE_HIGH", "0"),
                            ("LOAD_PULL", "1"), ("BUFFER_ACTIVE_HIGH", "0,0"),
                            ("BUFFER_PULL", "1,1")):
            set_("KLIPPER_MMU_" + name, value)
        set_("KLIPPER_MMU_TMC_RSENSE", "110")
        set_("KLIPPER_MMU_TMC_CURRENT_CEILING", "800")
        set_("KLIPPER_MMU_RUN_CURRENT", "700")
        set_("KLIPPER_MMU_HOLD_CURRENT", "150")
        for name, value in (("HUB_ACTIVE_HIGH", "0"), ("HUB_PULL", "1"),
                            ("TOOLHEAD_ACTIVE_HIGH", "0"), ("TOOLHEAD_PULL", "1")):
            set_("KLIPPER_MMU_" + name, value)
        set_("KLIPPER_MMU_BUFFER_LOW_A", "y")
        set_("KLIPPER_MMU_BOOT_IDENTITY_" + boot_family, "y")
        set_("KLIPPER_MMU_DIAGNOSTIC_STARTUP", "y")
        set_("KLIPPER_MMU_ACTUATION_OPT_IN",
             "y" if sys.argv[4] == "actuation" else "n")
    elif target == "h723":
        # The analog boxturtle-h723-{usb,can}[-safe] images never reach this
        # script (see the fragment branch in configure()); this is only the
        # switched-buffer h723 BoxTurtle image.
        if sys.argv[4] == "actuation":
            set_("KLIPPER_MMU_ACTUATION_OPT_IN", "y")
    else:
        set_("KLIPPER_MMU_LANE_COUNT", "1")
        for name, pin in (("STEP", "PA1"), ("DIR", "PA2"), ("ENABLE", "PA3"),
                          ("UART", "PA4"), ("BAY", "PA5"), ("HUB", "PA6"),
                          ("TOOLHEAD", "PA7"), ("BUFFER_A", "PB0"), ("BUFFER_B", "PB1")):
            set_("KLIPPER_MMU_PINS_" + name, pin)
    if sys.argv[1] == "on" and (
            (target == "h723" and device == "emu") or device == "nightowl"):
        set_("KLIPPER_MMU_ACTUATION_OPT_IN",
             "y" if sys.argv[4] == "actuation" else "n")
k.write_config(".config")
'''
        run(sys.executable, "-c", script, "on" if enabled else "off", target,
            "diff" if diff_buffer else "switched",
            "actuation" if actuation else "safe", transport, device, boot_family,
            "addon" if addon else "stock", cwd=root)
    run("make", "olddefconfig", cwd=root)
    config = (root / ".config").read_text()
    expected_stack = "4096" if enabled else "512"
    if f"CONFIG_STACK_SIZE={expected_stack}" not in config.splitlines():
        raise AssertionError("unexpected upstream stack reservation for MMU build mode")
    expected = [f"CONFIG_MACH_STM32{target.upper()}=y"]
    if target == "h723":
        expected += ["CONFIG_LOW_LEVEL_OPTIONS=y", "CONFIG_STM32_CLOCK_REF_25M=y",
                     "CONFIG_CLOCK_REF_FREQ=25000000"]
        if transport == "usb":
            expected += ["CONFIG_STM32_FLASH_START_0000=y",
                         "CONFIG_FLASH_APPLICATION_ADDRESS=0x8000000",
                         "CONFIG_USBSERIAL=y"]
        else:
            expected += ["CONFIG_STM32_FLASH_START_20000=y",
                         "CONFIG_FLASH_APPLICATION_ADDRESS=0x8020000",
                         "CONFIG_STM32_MMENU_CANBUS_PB8_PB9=y",
                         "CONFIG_STM32_CANBUS_PB8_PB9=y", "CONFIG_CANSERIAL=y",
                         "CONFIG_CANBUS_FREQUENCY=1000000"]
    elif target in ("f207", "f429", "f765", "g431", "g474"):
        expected += ["CONFIG_STM32_USB_PA11_PA12=y"]
    elif target in ("h743", "h750"):
        expected += ["CONFIG_STM32_USB_PA11_PA12=y", "CONFIG_CLOCK_REF_FREQ=8000000",
                     "CONFIG_CLOCK_FREQ=" + ("400000000" if target == "h743" else "480000000"),
                     "CONFIG_STM32_FLASH_START_0000=y",
                     "CONFIG_FLASH_APPLICATION_ADDRESS=0x8000000"]
    elif target in ("g0b0", "g0b1"):
        expected += ["CONFIG_CLOCK_FREQ=64000000", "CONFIG_STM32_USB_PA11_PA12=y"]
    if enabled:
        expected += ["CONFIG_KLIPPER_MMU=y", "CONFIG_KLIPPER_MMU_PIN_CONFIG=y"]
        if target == "h723" and device == "emu":
            expected += ["CONFIG_KLIPPER_MMU_DEVICE_EMU=y",
                         "CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y",
                         "CONFIG_KLIPPER_MMU_LANE_COUNT=5",
                         "CONFIG_KLIPPER_MMU_LOAD_SENSORS=y",
                         "CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H723=y",
                         "CONFIG_KLIPPER_MMU_DIAGNOSTIC_STARTUP=y",
                         'CONFIG_KLIPPER_MMU_PINS_STEP="PD0,PD1,PD2,PD3,PD4"',
                         'CONFIG_KLIPPER_MMU_PINS_DIR="PD5,PD6,PD7,PD8,PD9"',
                         'CONFIG_KLIPPER_MMU_PINS_ENABLE="PD10,PD11,PD12,PD13,PD14"',
                         'CONFIG_KLIPPER_MMU_PINS_UART="PE0,PE1,PE2,PE3,PE4"',
                         'CONFIG_KLIPPER_MMU_PINS_BAY="PE5,PE6,PE7,PE8,PE9"',
                         'CONFIG_KLIPPER_MMU_PINS_LOAD="PE10,PE11,PE12,PE13,PE14"',
                         'CONFIG_KLIPPER_MMU_PINS_TOOLHEAD="PC0"',
                         'CONFIG_KLIPPER_MMU_PINS_BUFFER_A="PC1"',
                         'CONFIG_KLIPPER_MMU_PINS_BUFFER_B="PC2"',
                         'CONFIG_KLIPPER_MMU_FEEDER_ENABLE_HIGH="0,0,0,0,0"',
                         'CONFIG_KLIPPER_MMU_FEEDER_STEP_HIGH="1,1,1,1,1"',
                         'CONFIG_KLIPPER_MMU_FEEDER_FORWARD_HIGH="1,1,1,1,1"',
                         'CONFIG_KLIPPER_MMU_TMC_ADDRESSES="0,0,0,0,0"',
                         'CONFIG_KLIPPER_MMU_TMC_UART_PULLUP="1,1,1,1,1"',
                         'CONFIG_KLIPPER_MMU_TMC_RSENSE=110',
                         'CONFIG_KLIPPER_MMU_TMC_CURRENT_CEILING=800',
                         'CONFIG_KLIPPER_MMU_LOAD_ACTIVE_HIGH="0,0,0,0,0"',
                         'CONFIG_KLIPPER_MMU_LOAD_PULL="1,1,1,1,1"',
                         'CONFIG_KLIPPER_MMU_BAY_ACTIVE_HIGH="0,0,0,0,0"',
                         'CONFIG_KLIPPER_MMU_BAY_PULL="1,1,1,1,1"',
                         'CONFIG_KLIPPER_MMU_TOOLHEAD_ACTIVE_HIGH=0',
                         'CONFIG_KLIPPER_MMU_TOOLHEAD_PULL=1',
                         'CONFIG_KLIPPER_MMU_BUFFER_ACTIVE_HIGH="0,0"',
                         'CONFIG_KLIPPER_MMU_BUFFER_PULL="1,1"',
                         'CONFIG_KLIPPER_MMU_BUFFER_LOW_A=y']
            if actuation:
                expected.append("CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN=y")
            elif "CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN=y" in config.splitlines():
                raise AssertionError("EMU safe image unexpectedly grants START")
            if "CONFIG_KLIPPER_MMU_DIFF_BUFFER=y" in config.splitlines():
                raise AssertionError("EMU safe image is not the switched-buffer candidate")
            if any(line.startswith("CONFIG_KLIPPER_MMU_PINS_HUB=")
                   for line in config.splitlines()):
                raise AssertionError("EMU must not save a physical HUB pin")
        elif device == "nightowl":
            expected += ["CONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL=y",
                         "CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y",
                         "CONFIG_KLIPPER_MMU_LANE_COUNT=4",
                         "CONFIG_KLIPPER_MMU_LOAD_SENSORS=y",
                         "CONFIG_WANT_GPIO_LOCAL=y", "CONFIG_WANT_TMCUART_LOCAL=y",
                         "CONFIG_KLIPPER_MMU_TMC_UART_SHARED=y",
                         "CONFIG_KLIPPER_MMU_DIAGNOSTIC_STARTUP=y",
                         'CONFIG_KLIPPER_MMU_FEEDER_ENABLE_HIGH="0,0,0,0"',
                         'CONFIG_KLIPPER_MMU_FEEDER_STEP_HIGH="1,1,1,1"',
                         'CONFIG_KLIPPER_MMU_FEEDER_FORWARD_HIGH="1,1,1,1"',
                         'CONFIG_KLIPPER_MMU_TMC_ADDRESSES="0,0,0,0"',
                         'CONFIG_KLIPPER_MMU_TMC_UART_PULLUP="1,1,1,1"',
                         "CONFIG_KLIPPER_MMU_TMC_RSENSE=110",
                         "CONFIG_KLIPPER_MMU_TMC_CURRENT_CEILING=800",
                         "CONFIG_KLIPPER_MMU_RUN_CURRENT=700",
                         "CONFIG_KLIPPER_MMU_HOLD_CURRENT=150",
                         'CONFIG_KLIPPER_MMU_LOAD_ACTIVE_HIGH="0,0,0,0"',
                         'CONFIG_KLIPPER_MMU_LOAD_PULL="1,1,1,1"',
                         'CONFIG_KLIPPER_MMU_BAY_ACTIVE_HIGH="0,0,0,0"',
                         'CONFIG_KLIPPER_MMU_BAY_PULL="1,1,1,1"',
                         "CONFIG_KLIPPER_MMU_HUB_ACTIVE_HIGH=0",
                         "CONFIG_KLIPPER_MMU_HUB_PULL=1",
                         "CONFIG_KLIPPER_MMU_TOOLHEAD_ACTIVE_HIGH=0",
                         "CONFIG_KLIPPER_MMU_TOOLHEAD_PULL=1",
                         'CONFIG_KLIPPER_MMU_BUFFER_ACTIVE_HIGH="0,0"',
                         'CONFIG_KLIPPER_MMU_BUFFER_PULL="1,1"',
                         "CONFIG_KLIPPER_MMU_BUFFER_LOW_A=y"]
            nightowl_pins = (("STEP", "PD0,PD1,PD2,PD3"),
                             ("DIR", "PD4,PD5,PD6,PD7"),
                             ("ENABLE", "PD8,PD9,PD10,PD11"),
                             ("UART", "PE0,PE1,PE2,PE3"),
                             ("BAY", "PE4,PE5,PE6,PE7"),
                             ("LOAD", "PE8,PE9,PE10,PE11")) if target == "h723" else (
                             ("STEP", "PA1,PA2,PA3,PA4"),
                             ("DIR", "PA5,PA6,PA7,PA8"),
                             ("ENABLE", "PB0,PB1,PB2,PB3"),
                             ("UART", "PC4,PC5,PC6,PC7"),
                             ("BAY", "PB12,PB13,PB14,PB15"),
                             ("LOAD", "PB8,PB9,PB10,PB11"))
            expected += [f'CONFIG_KLIPPER_MMU_PINS_{name}="{pins}"'
                         for name, pins in nightowl_pins]
            expected += ['CONFIG_KLIPPER_MMU_PINS_HUB="PC0"',
                         'CONFIG_KLIPPER_MMU_PINS_TOOLHEAD="PC1"',
                         'CONFIG_KLIPPER_MMU_PINS_BUFFER_A="PC2"',
                         'CONFIG_KLIPPER_MMU_PINS_BUFFER_B="PC3"',
                         "CONFIG_KLIPPER_MMU_NIGHTOWL_BOOT_GATES=y" if target == "h723"
                         else f"CONFIG_KLIPPER_MMU_{GATE_FAMILIES.get(target, boot_family)}_BOOT_GATES=y",
                         f"CONFIG_KLIPPER_MMU_BOOT_IDENTITY_{boot_family}=y"]
            if actuation:
                expected.append("CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN=y")
            elif "CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN=y" in config.splitlines():
                raise AssertionError("NightOwl safe image unexpectedly grants START")
        elif target == "h723" and device in PASSIVE4_SYMS:
            has_load = device == "kms"
            expected += [f"CONFIG_KLIPPER_MMU_DEVICE_{PASSIVE4_SYMS[device]}=y",
                         "CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y",
                         "CONFIG_KLIPPER_MMU_LANE_COUNT=4",
                         "CONFIG_KLIPPER_MMU_PASSIVE_FEEDER_BOOT_GATES=y",
                         "CONFIG_WANT_GPIO_LOCAL=y", "CONFIG_WANT_TMCUART_LOCAL=y",
                         "CONFIG_KLIPPER_MMU_TMC_UART_SHARED=y",
                         "CONFIG_KLIPPER_MMU_DIAGNOSTIC_STARTUP=y",
                         "CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H723=y",
                         'CONFIG_KLIPPER_MMU_PINS_STEP="PD0,PD1,PD2,PD3"',
                         'CONFIG_KLIPPER_MMU_PINS_DIR="PD4,PD5,PD6,PD7"',
                         'CONFIG_KLIPPER_MMU_PINS_ENABLE="PD8,PD9,PD10,PD11"',
                         'CONFIG_KLIPPER_MMU_PINS_UART="PE0,PE1,PE2,PE3"',
                         'CONFIG_KLIPPER_MMU_PINS_TOOLHEAD="PC1"',
                         'CONFIG_KLIPPER_MMU_PINS_BUFFER_A="PC2"',
                         'CONFIG_KLIPPER_MMU_PINS_BUFFER_B="PC3"',
                         'CONFIG_KLIPPER_MMU_FEEDER_ENABLE_HIGH="0,0,0,0"',
                         'CONFIG_KLIPPER_MMU_FEEDER_STEP_HIGH="1,1,1,1"',
                         'CONFIG_KLIPPER_MMU_FEEDER_FORWARD_HIGH="1,1,1,1"',
                         'CONFIG_KLIPPER_MMU_TMC_ADDRESSES="0,0,0,0"',
                         'CONFIG_KLIPPER_MMU_TMC_UART_PULLUP="1,1,1,1"',
                         "CONFIG_KLIPPER_MMU_TMC_RSENSE=110",
                         "CONFIG_KLIPPER_MMU_TMC_CURRENT_CEILING=800",
                         "CONFIG_KLIPPER_MMU_RUN_CURRENT=700",
                         "CONFIG_KLIPPER_MMU_HOLD_CURRENT=150",
                         "CONFIG_KLIPPER_MMU_TOOLHEAD_ACTIVE_HIGH=0",
                         "CONFIG_KLIPPER_MMU_TOOLHEAD_PULL=1",
                         'CONFIG_KLIPPER_MMU_BUFFER_ACTIVE_HIGH="0,0"',
                         'CONFIG_KLIPPER_MMU_BUFFER_PULL="1,1"',
                         "CONFIG_KLIPPER_MMU_BUFFER_LOW_A=y"]
            lines = config.splitlines()
            if has_load:
                if any(line.startswith("CONFIG_KLIPPER_MMU_PINS_HUB=") for line in lines):
                    raise AssertionError("KMS saved a HUB pin; its LOAD switch is the virtual HUB")
                expected += ["CONFIG_KLIPPER_MMU_BAY_SENSORS=y",
                             "CONFIG_KLIPPER_MMU_VIRTUAL_HUB=y",
                             'CONFIG_KLIPPER_MMU_PINS_BAY="PE4,PE5,PE6,PE7"',
                             'CONFIG_KLIPPER_MMU_BAY_ACTIVE_HIGH="0,0,0,0"',
                             'CONFIG_KLIPPER_MMU_BAY_PULL="1,1,1,1"',
                             "CONFIG_KLIPPER_MMU_LOAD_SENSORS=y",
                             'CONFIG_KLIPPER_MMU_PINS_LOAD="PE8,PE9,PE10,PE11"',
                             'CONFIG_KLIPPER_MMU_LOAD_ACTIVE_HIGH="0,0,0,0"',
                             'CONFIG_KLIPPER_MMU_LOAD_PULL="1,1,1,1"']
            else:
                if any(line.startswith(("CONFIG_KLIPPER_MMU_LOAD_SENSORS=",
                                        "CONFIG_KLIPPER_MMU_PINS_BAY="))
                       for line in lines):
                    raise AssertionError(f"{device} saved BAY or LOAD switches it does not have")
                expected += ['CONFIG_KLIPPER_MMU_PINS_HUB="PC0"',
                             "CONFIG_KLIPPER_MMU_HUB_ACTIVE_HIGH=0",
                             "CONFIG_KLIPPER_MMU_HUB_PULL=1"]
            if actuation:
                expected.append("CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN=y")
            elif "CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN=y" in config.splitlines():
                raise AssertionError(f"{device} safe image unexpectedly grants START")
        elif target == "h723" and device == "qidi_box":
            expected += ["CONFIG_KLIPPER_MMU_DEVICE_QIDI_BOX=y",
                         "CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y",
                         "CONFIG_KLIPPER_MMU_LANE_COUNT=4",
                         "CONFIG_KLIPPER_MMU_QIDI_BOOT_GATES=y",
                         "CONFIG_WANT_GPIO_LOCAL=y",
                         "CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H723=y",
                         "CONFIG_KLIPPER_MMU_BAY_SENSORS=y",
                         "CONFIG_KLIPPER_MMU_BUFFER_SINGLE_SWITCH=y",
                         'CONFIG_KLIPPER_MMU_PINS_STEP="PD0,PD1,PD2,PD3"',
                         'CONFIG_KLIPPER_MMU_PINS_DIR="PD4,PD5,PD6,PD7"',
                         'CONFIG_KLIPPER_MMU_PINS_ENABLE="PD8,PD9,PD10,PD11"',
                         'CONFIG_KLIPPER_MMU_PINS_BAY="PE4,PE5,PE6,PE7"',
                         'CONFIG_KLIPPER_MMU_PINS_HUB="PC0"',
                         'CONFIG_KLIPPER_MMU_PINS_TOOLHEAD="PC1"',
                         'CONFIG_KLIPPER_MMU_PINS_BUFFER_A="PC2"',
                         'CONFIG_KLIPPER_MMU_FEEDER_ENABLE_HIGH="0,0,0,0"',
                         'CONFIG_KLIPPER_MMU_FEEDER_STEP_HIGH="1,1,1,1"',
                         'CONFIG_KLIPPER_MMU_FEEDER_FORWARD_HIGH="1,1,1,1"',
                         'CONFIG_KLIPPER_MMU_BAY_ACTIVE_HIGH="0,0,0,0"',
                         'CONFIG_KLIPPER_MMU_BAY_PULL="1,1,1,1"',
                         "CONFIG_KLIPPER_MMU_HUB_ACTIVE_HIGH=0",
                         "CONFIG_KLIPPER_MMU_HUB_PULL=1",
                         "CONFIG_KLIPPER_MMU_TOOLHEAD_ACTIVE_HIGH=0",
                         "CONFIG_KLIPPER_MMU_TOOLHEAD_PULL=1",
                         'CONFIG_KLIPPER_MMU_BUFFER_ACTIVE_HIGH="0"',
                         'CONFIG_KLIPPER_MMU_BUFFER_PULL="1"',
                         "CONFIG_KLIPPER_MMU_DIAGNOSTIC_STARTUP=y"]
            if actuation:
                expected.append("CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN=y")
            elif "CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN=y" in config.splitlines():
                raise AssertionError("QIDI Box safe image unexpectedly grants START")
            lines = config.splitlines()
            # No gear TMC UART chip: never ask for or claim a UART pin, TMC
            # register setting or the shared local TMC UART driver binding
            # (mcu/Kconfig.control's KLIPPER_MMU_GEAR_TMC_UART) -- selecting
            # that would send board startup down a TMC confirmation wait
            # this device can never satisfy (mcu/board_startup.c). A
            # tension-only buffer has no second pin either, and this
            # device's own traced profile has no per-lane LOAD/exit switch.
            if any(line.startswith(("CONFIG_KLIPPER_MMU_PINS_UART=",
                                    "CONFIG_KLIPPER_MMU_PINS_BUFFER_B=",
                                    "CONFIG_KLIPPER_MMU_TMC_ADDRESSES=",
                                    "CONFIG_KLIPPER_MMU_TMC_UART_PULLUP=",
                                    "CONFIG_KLIPPER_MMU_LOAD_SENSORS=y",
                                    "CONFIG_KLIPPER_MMU_VIRTUAL_HUB=y"))
                   for line in lines):
                raise AssertionError("QIDI Box saved a UART/second-buffer/LOAD "
                                     "setting it does not have")
            if any(line in ("CONFIG_WANT_TMCUART_LOCAL=y",
                            "CONFIG_KLIPPER_MMU_TMC_UART_SHARED=y") for line in lines):
                raise AssertionError("QIDI Box selected TMC UART support it can never confirm")
            # KLIPPER_MMU_GEAR_TMC_UART is now a promptless, device-derived
            # symbol (mcu/Kconfig.control): kconfiglib's own .config writer
            # never persists a promptless symbol in either form (not even
            # "# ... is not set"), so its absence here -- not an explicit
            # off -- is the whole point of that fix (a saved .config can no
            # longer disagree with the device it names).
            if any("GEAR_TMC_UART" in line for line in lines):
                raise AssertionError("QIDI Box's promptless GEAR_TMC_UART "
                                     "was unexpectedly written to .config")
        elif target == "h723" and device in SELECTOR_SYMS:
            kind = SELECTOR_KIND[device]
            gates = SELECTOR_GATES[device]
            has_bay_and_rewind = device == SELECTOR_BAY_AND_REWIND
            has_stepper = kind != "SERVO"
            has_servo = kind in ("LINEAR", "SERVO") and device not in SELECTOR_NO_SERVO
            expected += [f"CONFIG_KLIPPER_MMU_DEVICE_{SELECTOR_SYMS[device]}=y",
                         "CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y",
                         f"CONFIG_KLIPPER_MMU_LANE_COUNT={gates}",
                         f"CONFIG_KLIPPER_MMU_SELECTOR_KIND_{kind}=y",
                         "CONFIG_KLIPPER_MMU_SELECTOR_BOOT_GATES=y",
                         "CONFIG_WANT_GPIO_LOCAL=y", "CONFIG_WANT_TMCUART_LOCAL=y",
                         "CONFIG_KLIPPER_MMU_TMC_UART_SHARED=y"]
            if device in SELECTOR_MOD_SYMS:
                expected.append(f"CONFIG_{SELECTOR_MOD_SYMS[device]}=y")
            expected.append("CONFIG_KLIPPER_MMU_BAY_SENSORS=y" if has_bay_and_rewind
                            else "# CONFIG_KLIPPER_MMU_BAY_SENSORS is not set")
            expected += ['CONFIG_KLIPPER_MMU_PINS_STEP="PD0"',
                         'CONFIG_KLIPPER_MMU_PINS_DIR="PD1"',
                         'CONFIG_KLIPPER_MMU_PINS_ENABLE="PD2"',
                         'CONFIG_KLIPPER_MMU_PINS_UART="PD3"',
                         'CONFIG_KLIPPER_MMU_PINS_HUB="PC0"',
                         'CONFIG_KLIPPER_MMU_PINS_TOOLHEAD="PC1"',
                         'CONFIG_KLIPPER_MMU_PINS_BUFFER_A="PC2"',
                         'CONFIG_KLIPPER_MMU_PINS_BUFFER_B="PC3"',
                         'CONFIG_KLIPPER_MMU_FEEDER_ENABLE_HIGH="0"',
                         'CONFIG_KLIPPER_MMU_FEEDER_STEP_HIGH="1"',
                         'CONFIG_KLIPPER_MMU_FEEDER_FORWARD_HIGH="1"',
                         'CONFIG_KLIPPER_MMU_TMC_ADDRESSES="0"',
                         'CONFIG_KLIPPER_MMU_TMC_UART_PULLUP="1"',
                         "CONFIG_KLIPPER_MMU_TMC_RSENSE=110",
                         "CONFIG_KLIPPER_MMU_TMC_CURRENT_CEILING=800",
                         "CONFIG_KLIPPER_MMU_RUN_CURRENT=700",
                         "CONFIG_KLIPPER_MMU_HOLD_CURRENT=150",
                         "CONFIG_KLIPPER_MMU_HUB_ACTIVE_HIGH=0", "CONFIG_KLIPPER_MMU_HUB_PULL=1",
                         "CONFIG_KLIPPER_MMU_TOOLHEAD_ACTIVE_HIGH=0",
                         "CONFIG_KLIPPER_MMU_TOOLHEAD_PULL=1",
                         'CONFIG_KLIPPER_MMU_BUFFER_ACTIVE_HIGH="0,0"',
                         'CONFIG_KLIPPER_MMU_BUFFER_PULL="1,1"',
                         "CONFIG_KLIPPER_MMU_BUFFER_LOW_A=y"]
            lines = config.splitlines()
            if any(line.startswith("CONFIG_KLIPPER_MMU_PINS_LOAD=") for line in lines):
                raise AssertionError(f"{device} saved a LOAD switch; selectors have none")
            if has_bay_and_rewind:
                expected += ['CONFIG_KLIPPER_MMU_PINS_BAY="PF0,PF1,PF2,PF3"',
                             'CONFIG_KLIPPER_MMU_BAY_ACTIVE_HIGH="1,1,1,1"',
                             'CONFIG_KLIPPER_MMU_BAY_PULL="1,1,1,1"',
                             "CONFIG_KLIPPER_MMU_POWERED_REWIND=y",
                             "CONFIG_WANT_HARD_PWM_LOCAL_PAIR=y",
                             "CONFIG_KLIPPER_MMU_REWIND_CYCLE_US=50",
                             'CONFIG_KLIPPER_MMU_REWIND_ENABLE_HIGH="1,1,1,1"',
                             'CONFIG_KLIPPER_MMU_REWIND_FORWARD_HIGH="1,1,1,1"',
                             'CONFIG_KLIPPER_MMU_REWIND_REVERSE_HIGH="1,1,1,1"',
                             'CONFIG_KLIPPER_MMU_PINS_REWIND_FORWARD="PA1,PA7,PB15,PD13"',
                             'CONFIG_KLIPPER_MMU_PINS_REWIND_REVERSE="PA0,PA6,PB14,PD12"',
                             'CONFIG_KLIPPER_MMU_PINS_REWIND_ENABLE="PA2,PA5,PB13,PD11"']
            elif any(line.startswith(("CONFIG_KLIPPER_MMU_BAY_SENSORS=",
                                      "CONFIG_KLIPPER_MMU_PINS_BAY=",
                                      "CONFIG_KLIPPER_MMU_POWERED_REWIND=")) for line in lines):
                raise AssertionError(f"{device} saved BAY or rewind pins; only Claymore has them")
            if has_stepper:
                expected += ["CONFIG_KLIPPER_MMU_SELECTOR_STEPPER=y",
                             'CONFIG_KLIPPER_MMU_PINS_SELECTOR_STEP="PD4"',
                             'CONFIG_KLIPPER_MMU_PINS_SELECTOR_DIR="PD5"',
                             'CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENABLE="PD6"',
                             "CONFIG_KLIPPER_MMU_SELECTOR_UART=y",
                             'CONFIG_KLIPPER_MMU_PINS_SELECTOR_UART="PD7"']
                if kind == "INDEXED":
                    expected += ['CONFIG_KLIPPER_MMU_PINS_SELECTOR_GATE="PE0,PE1,PE2,PE3"',
                                 'CONFIG_KLIPPER_MMU_SELECTOR_GATE_ACTIVE_HIGH="0,0,0,0"',
                                 'CONFIG_KLIPPER_MMU_SELECTOR_GATE_PULL="1,1,1,1"']
                else:
                    expected += ['CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENDSTOP="PC4"',
                                 "CONFIG_KLIPPER_MMU_SELECTOR_ENDSTOP_ACTIVE_HIGH=0",
                                 "CONFIG_KLIPPER_MMU_SELECTOR_ENDSTOP_PULL=1"]
            elif any(line.startswith("CONFIG_KLIPPER_MMU_PINS_SELECTOR_STEP=")
                     for line in lines):
                raise AssertionError(f"{device} (SERVO) unexpectedly saved a selector stepper pin")
            if has_servo:
                expected += ["CONFIG_KLIPPER_MMU_SERVO=y", 'CONFIG_KLIPPER_MMU_PINS_SERVO="PC5"']
            elif any(line.startswith("CONFIG_KLIPPER_MMU_PINS_SERVO=") for line in lines):
                raise AssertionError(f"{device} ({kind}) unexpectedly saved a servo pin")
            if actuation:
                expected.append("CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN=y")
            elif "CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN=y" in config.splitlines():
                raise AssertionError(f"{device} safe image unexpectedly grants START")
        elif target in ("g0b0", "g0b1") and device == "angry_beaver":
            expected += ["CONFIG_KLIPPER_MMU_DEVICE_ANGRY_BEAVER=y",
                         "CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y",
                         "CONFIG_KLIPPER_MMU_LANE_COUNT=4",
                         "CONFIG_KLIPPER_MMU_G0B_BOOT_GATES=y",
                         "CONFIG_WANT_GPIO_LOCAL=y", "CONFIG_WANT_TMCUART_LOCAL=y",
                         "CONFIG_KLIPPER_MMU_TMC_UART_SHARED=y",
                         "CONFIG_KLIPPER_MMU_DIAGNOSTIC_STARTUP=y",
                         f"CONFIG_KLIPPER_MMU_BOOT_IDENTITY_{boot_family}=y",
                         'CONFIG_KLIPPER_MMU_PINS_STEP="PD4,PC9,PC15,PC13"',
                         'CONFIG_KLIPPER_MMU_PINS_DIR="PD3,PC8,PC11,PC12"',
                         'CONFIG_KLIPPER_MMU_PINS_ENABLE="PD5,PD2,PC10,PC14"',
                         'CONFIG_KLIPPER_MMU_PINS_UART="PB5,PB4,PB3,PD6"',
                         'CONFIG_KLIPPER_MMU_PINS_HUB="PA15"',
                         'CONFIG_KLIPPER_MMU_PINS_TOOLHEAD="PC1"',
                         'CONFIG_KLIPPER_MMU_PINS_BUFFER_A="PC2"',
                         'CONFIG_KLIPPER_MMU_PINS_BUFFER_B="PC3"',
                         'CONFIG_KLIPPER_MMU_FEEDER_ENABLE_HIGH="0,0,0,0"',
                         'CONFIG_KLIPPER_MMU_FEEDER_STEP_HIGH="1,1,1,1"',
                         'CONFIG_KLIPPER_MMU_FEEDER_FORWARD_HIGH="1,1,1,1"',
                         'CONFIG_KLIPPER_MMU_TMC_ADDRESSES="0,0,0,0"',
                         'CONFIG_KLIPPER_MMU_TMC_UART_PULLUP="1,1,1,1"',
                         "CONFIG_KLIPPER_MMU_TMC_RSENSE=110",
                         "CONFIG_KLIPPER_MMU_TMC_CURRENT_CEILING=800",
                         "CONFIG_KLIPPER_MMU_RUN_CURRENT=700",
                         "CONFIG_KLIPPER_MMU_HOLD_CURRENT=150",
                         "CONFIG_KLIPPER_MMU_HUB_ACTIVE_HIGH=0", "CONFIG_KLIPPER_MMU_HUB_PULL=1",
                         "CONFIG_KLIPPER_MMU_TOOLHEAD_ACTIVE_HIGH=0",
                         "CONFIG_KLIPPER_MMU_TOOLHEAD_PULL=1",
                         'CONFIG_KLIPPER_MMU_BUFFER_ACTIVE_HIGH="0,0"',
                         'CONFIG_KLIPPER_MMU_BUFFER_PULL="1,1"',
                         "CONFIG_KLIPPER_MMU_BUFFER_LOW_A=y"]
            lines = config.splitlines()
            if any(line.startswith(("CONFIG_KLIPPER_MMU_LOAD_SENSORS=",
                                    "CONFIG_KLIPPER_MMU_PINS_BAY=",
                                    "CONFIG_KLIPPER_MMU_POWERED_REWIND=")) for line in lines):
                raise AssertionError("Angry Beaver saved BAY, LOAD or rewind pins it does not have")
            if actuation:
                expected.append("CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN=y")
            elif "CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN=y" in config.splitlines():
                raise AssertionError("Angry Beaver safe image unexpectedly grants START")
        elif target in ("g0b0", "g0b1") and device in ("ercf_2_0", "ercf_3_0"):
            expected += [f"CONFIG_KLIPPER_MMU_DEVICE_{SELECTOR_SYMS[device]}=y",
                         "CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y",
                         f"CONFIG_KLIPPER_MMU_LANE_COUNT={SELECTOR_GATES[device]}",
                         "CONFIG_KLIPPER_MMU_SELECTOR_KIND_LINEAR=y",
                         "CONFIG_KLIPPER_MMU_G0B_BOOT_GATES=y",
                         "CONFIG_WANT_GPIO_LOCAL=y", "CONFIG_WANT_TMCUART_LOCAL=y",
                         "CONFIG_KLIPPER_MMU_TMC_UART_SHARED=y",
                         "# CONFIG_KLIPPER_MMU_BAY_SENSORS is not set",
                         "CONFIG_KLIPPER_MMU_SELECTOR_STEPPER=y",
                         "CONFIG_KLIPPER_MMU_SERVO=y",
                         f"CONFIG_KLIPPER_MMU_BOOT_IDENTITY_{boot_family}=y",
                         "CONFIG_KLIPPER_MMU_DIAGNOSTIC_STARTUP=y",
                         'CONFIG_KLIPPER_MMU_PINS_STEP="PD4"',
                         'CONFIG_KLIPPER_MMU_PINS_DIR="PD3"',
                         'CONFIG_KLIPPER_MMU_PINS_ENABLE="PD5"',
                         'CONFIG_KLIPPER_MMU_PINS_UART="PB5"',
                         'CONFIG_KLIPPER_MMU_PINS_SELECTOR_STEP="PC9"',
                         'CONFIG_KLIPPER_MMU_PINS_SELECTOR_DIR="PC8"',
                         'CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENABLE="PD2"',
                         "CONFIG_KLIPPER_MMU_SELECTOR_UART=y",
                         'CONFIG_KLIPPER_MMU_PINS_SELECTOR_UART="PB4"',
                         'CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENDSTOP="PA15"',
                         "CONFIG_KLIPPER_MMU_SELECTOR_ENDSTOP_ACTIVE_HIGH=0",
                         "CONFIG_KLIPPER_MMU_SELECTOR_ENDSTOP_PULL=1",
                         'CONFIG_KLIPPER_MMU_PINS_SERVO="PA1"',
                         'CONFIG_KLIPPER_MMU_PINS_HUB="PC0"',
                         'CONFIG_KLIPPER_MMU_PINS_TOOLHEAD="PC1"',
                         'CONFIG_KLIPPER_MMU_PINS_BUFFER_A="PC3"',
                         'CONFIG_KLIPPER_MMU_PINS_BUFFER_B="PC4"',
                         'CONFIG_KLIPPER_MMU_FEEDER_ENABLE_HIGH="0"',
                         'CONFIG_KLIPPER_MMU_FEEDER_STEP_HIGH="1"',
                         'CONFIG_KLIPPER_MMU_FEEDER_FORWARD_HIGH="1"',
                         'CONFIG_KLIPPER_MMU_TMC_ADDRESSES="0"',
                         'CONFIG_KLIPPER_MMU_TMC_UART_PULLUP="1"',
                         "CONFIG_KLIPPER_MMU_TMC_RSENSE=110",
                         "CONFIG_KLIPPER_MMU_TMC_CURRENT_CEILING=800",
                         "CONFIG_KLIPPER_MMU_RUN_CURRENT=700",
                         "CONFIG_KLIPPER_MMU_HOLD_CURRENT=150",
                         "CONFIG_KLIPPER_MMU_HUB_ACTIVE_HIGH=0", "CONFIG_KLIPPER_MMU_HUB_PULL=1",
                         "CONFIG_KLIPPER_MMU_TOOLHEAD_ACTIVE_HIGH=0",
                         "CONFIG_KLIPPER_MMU_TOOLHEAD_PULL=1",
                         'CONFIG_KLIPPER_MMU_BUFFER_ACTIVE_HIGH="0,0"',
                         'CONFIG_KLIPPER_MMU_BUFFER_PULL="1,1"',
                         "CONFIG_KLIPPER_MMU_BUFFER_LOW_A=y",
                         "CONFIG_KLIPPER_MMU_SIZE_OPT_BUILD=y"]
            lines = config.splitlines()
            if any(line.startswith(("CONFIG_KLIPPER_MMU_PINS_LOAD=", "CONFIG_KLIPPER_MMU_PINS_BAY=",
                                    "CONFIG_KLIPPER_MMU_POWERED_REWIND=")) for line in lines):
                raise AssertionError(f"{device} saved BAY, LOAD or rewind pins; this G0B row has none")
            if actuation:
                expected.append("CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN=y")
            elif "CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN=y" in config.splitlines():
                raise AssertionError(f"{device} safe image unexpectedly grants START")
        elif target != "h723" and device == "f4custom":
            expected += ["CONFIG_KLIPPER_MMU_DEVICE_CUSTOM=y",
                         "CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y",
                         "CONFIG_KLIPPER_MMU_LANE_COUNT=1",
                         "CONFIG_KLIPPER_MMU_LOAD_SENSORS=y",
                         f"CONFIG_KLIPPER_MMU_{GATE_FAMILIES.get(target, boot_family)}_BOOT_GATES=y",
                         "CONFIG_WANT_GPIO_LOCAL=y", "CONFIG_WANT_TMCUART_LOCAL=y",
                         "CONFIG_KLIPPER_MMU_TMC_UART_SHARED=y",
                         f"CONFIG_KLIPPER_MMU_BOOT_IDENTITY_{boot_family}=y",
                         "CONFIG_KLIPPER_MMU_DIAGNOSTIC_STARTUP=y",
                         'CONFIG_KLIPPER_MMU_PINS_STEP="PA1"',
                         'CONFIG_KLIPPER_MMU_PINS_DIR="PA2"',
                         'CONFIG_KLIPPER_MMU_PINS_ENABLE="PA3"',
                         'CONFIG_KLIPPER_MMU_PINS_UART="PA4"',
                         'CONFIG_KLIPPER_MMU_PINS_BAY="PA5"',
                         'CONFIG_KLIPPER_MMU_PINS_HUB="PA6"',
                         'CONFIG_KLIPPER_MMU_PINS_TOOLHEAD="PA7"',
                         'CONFIG_KLIPPER_MMU_PINS_BUFFER_A="PB0"',
                         'CONFIG_KLIPPER_MMU_PINS_BUFFER_B="PB1"',
                         'CONFIG_KLIPPER_MMU_PINS_LOAD="PB2"',
                         'CONFIG_KLIPPER_MMU_FEEDER_ENABLE_HIGH="0"',
                         'CONFIG_KLIPPER_MMU_FEEDER_STEP_HIGH="1"',
                         'CONFIG_KLIPPER_MMU_FEEDER_FORWARD_HIGH="1"',
                         'CONFIG_KLIPPER_MMU_TMC_ADDRESSES="0"',
                         'CONFIG_KLIPPER_MMU_TMC_UART_PULLUP="1"',
                         "CONFIG_KLIPPER_MMU_TMC_RSENSE=110",
                         "CONFIG_KLIPPER_MMU_TMC_CURRENT_CEILING=800",
                         "CONFIG_KLIPPER_MMU_BUFFER_LOW_A=y",
                         'CONFIG_KLIPPER_MMU_BAY_ACTIVE_HIGH="0"',
                         'CONFIG_KLIPPER_MMU_BAY_PULL="1"',
                         'CONFIG_KLIPPER_MMU_LOAD_ACTIVE_HIGH="0"',
                         'CONFIG_KLIPPER_MMU_LOAD_PULL="1"',
                         "CONFIG_KLIPPER_MMU_HUB_ACTIVE_HIGH=0",
                         "CONFIG_KLIPPER_MMU_HUB_PULL=1",
                         "CONFIG_KLIPPER_MMU_TOOLHEAD_ACTIVE_HIGH=0",
                         "CONFIG_KLIPPER_MMU_TOOLHEAD_PULL=1",
                         'CONFIG_KLIPPER_MMU_BUFFER_ACTIVE_HIGH="0,0"',
                         'CONFIG_KLIPPER_MMU_BUFFER_PULL="1,1"']
            if actuation:
                expected.append("CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN=y")
            elif "CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN=y" in config.splitlines():
                raise AssertionError("F4x5 safe image unexpectedly grants START")
        elif target == "h723":
            expected += ["CONFIG_KLIPPER_MMU_DEVICE_BOXTURTLE=y",
                         "CONFIG_KLIPPER_MMU_FEED_SPEED_MM_S=26",
                         "CONFIG_KLIPPER_MMU_RETRACT_SPEED_MM_S=26",
                         "CONFIG_KLIPPER_MMU_UNLOAD_HUB_CLEAR_MM=65",
                         "CONFIG_KLIPPER_MMU_HUB_TIMEOUT=20000",
                         "CONFIG_KLIPPER_MMU_ARRIVAL_TIMEOUT=118750",
                         "CONFIG_KLIPPER_MMU_RETRACT_TIMEOUT=90000",
                         "CONFIG_KLIPPER_MMU_LOAD_SENSORS=y",
                         'CONFIG_KLIPPER_MMU_PINS_STEP="PD4,PC12,PE2,PD15"',
                         'CONFIG_KLIPPER_MMU_PINS_LOAD="PE8,PE9,PE10,PE11"',
                         'CONFIG_KLIPPER_MMU_LOAD_ACTIVE_HIGH="1,1,1,1"',
                         'CONFIG_KLIPPER_MMU_LOAD_PULL="1,1,1,1"']
            if diff_buffer:
                expected += ["CONFIG_KLIPPER_MMU_DIFF_BUFFER=y", "CONFIG_WANT_ADC=y",
                             "CONFIG_WANT_ADC_LOCAL=y",
                             "CONFIG_KLIPPER_MMU_ADC_CONVERSION_TIMEOUT_US=1000",
                             "CONFIG_KLIPPER_MMU_ADC_PAIR_SPAN_US=3000",
                             "CONFIG_KLIPPER_MMU_ADC_SAMPLE_AGE_US=20000",
                             "CONFIG_KLIPPER_MMU_ADC_SAMPLE_PERIOD_US=1000",
                             "CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H723=y",
                             "CONFIG_KLIPPER_MMU_DIAGNOSTIC_STARTUP=y",
                             'CONFIG_KLIPPER_MMU_PINS_BUFFER_A="PC4"',
                             'CONFIG_KLIPPER_MMU_PINS_BUFFER_B="PC5"',
                             'CONFIG_KLIPPER_MMU_PINS_HUB="PE12"',
                             'CONFIG_KLIPPER_MMU_PINS_BAY="PE13,PB0,PB1,PB2"',
                             "CONFIG_KLIPPER_MMU_FPS_ARRIVAL=y",
                             "# CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR is not set",
                             "CONFIG_KLIPPER_MMU_FPS_ARRIVAL_PERMILLE=700",
                             "CONFIG_KLIPPER_MMU_FPS_ARRIVAL_SAMPLES=3",
                             "CONFIG_KLIPPER_MMU_FPS_ARRIVAL_HOLD_MS=20",
                             "CONFIG_KLIPPER_MMU_FPS_ARRIVAL_MIN_MM=100",
                             "CONFIG_KLIPPER_MMU_FPS_ARRIVAL_MAX_MM=1900"]
                if "TOOLHEAD_ACTIVE_HIGH" in config or "PINS_TOOLHEAD" in config:
                    raise AssertionError("FPS arrival image saved toolhead switch settings")
                if actuation:
                    expected.append("CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN=y")
                elif "CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN=y" in config.splitlines():
                    raise AssertionError("safe analog image unexpectedly grants START")
            else:
                expected += ['CONFIG_KLIPPER_MMU_PINS_TOOLHEAD="PE7"',
                             "CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H723=y",
                             "CONFIG_KLIPPER_MMU_DIAGNOSTIC_STARTUP=y"]
                if actuation:
                    expected.append("CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN=y")
                elif "CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN=y" in config.splitlines():
                    raise AssertionError("safe switched image unexpectedly grants START")
        else:
            expected += ["CONFIG_KLIPPER_MMU_DEVICE_CUSTOM=y", "CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y",
                         "CONFIG_KLIPPER_MMU_LANE_COUNT=1"]
            expected += [f'CONFIG_KLIPPER_MMU_PINS_{name}="{pin}"'
                         for name, pin in (("STEP", "PA1"), ("DIR", "PA2"),
                                           ("ENABLE", "PA3"), ("UART", "PA4"),
                                           ("BAY", "PA5"), ("HUB", "PA6"),
                                           ("TOOLHEAD", "PA7"), ("BUFFER_A", "PB0"),
                                           ("BUFFER_B", "PB1"))]
    elif addon:
        expected += ["# CONFIG_KLIPPER_MMU is not set"]
    elif any("KLIPPER_MMU" in line or any(f"CONFIG_{name}" in line for name in DRIVER_OPTIONS)
             for line in config.splitlines()):
        raise AssertionError("stock configuration unexpectedly has add-on options")
    for line in expected:
        if line not in config.splitlines():
            raise AssertionError(f"native Kconfig did not retain {line}")
    if enabled and (target == "h723" or device in ("f4custom", "nightowl")
                    or device in PASSIVE4_DEVICES or device in SELECTOR_DEVICES):
        # Linking does not exercise startup's resource preflight. Catch a
        # saved pin alias before retaining a candidate firmware artifact.
        settings = dict(line.split("=", 1) for line in config.splitlines()
                        if line.startswith("CONFIG_KLIPPER_MMU_PINS_"))
        owners = {}
        for role in ("STEP", "DIR", "ENABLE", "UART", "BAY", "LOAD", "HUB",
                     "TOOLHEAD", "BUFFER_A", "BUFFER_B", "REWIND_FORWARD",
                     "REWIND_REVERSE", "REWIND_ENABLE", "SELECTOR_STEP",
                     "SELECTOR_DIR", "SELECTOR_ENABLE", "SELECTOR_ENDSTOP", "SERVO"):
            for pin in settings.get("CONFIG_KLIPPER_MMU_PINS_" + role, '""').strip('"').split(","):
                if not pin:
                    continue
                if pin in owners:
                    raise AssertionError(f"pin {pin} is both {owners[pin]} and {role}")
                owners[pin] = role
        if device == "f4custom":
            if set(owners) & {"PA11", "PA12", "PA13", "PA14"}:
                raise AssertionError("F4x5 candidate overlaps USB or SWD pins")
            if owners.get("PA1") != "STEP" or owners.get("PA2") != "DIR":
                raise AssertionError("F4x5 one-lane actuator pins drifted")
        elif device == "emu":
            forbidden = {"PA11", "PA12", "PA13", "PA14"}
            if set(owners) & forbidden:
                raise AssertionError("EMU candidate uses a USB or SWD pin")
            if "HUB" in owners.values():
                raise AssertionError("EMU candidate claims a HUB GPIO")
            if owners.get("PC0") != "TOOLHEAD" or owners.get("PC1") != "BUFFER_A" or owners.get("PC2") != "BUFFER_B":
                raise AssertionError("EMU candidate buffer or toolhead pins drifted")
        elif device == "nightowl":
            forbidden = {"PA11", "PA12", "PA13", "PA14"}
            if set(owners) & forbidden:
                raise AssertionError("NightOwl candidate uses a USB or SWD pin")
            if owners.get("PC0") != "HUB" or owners.get("PC1") != "TOOLHEAD":
                raise AssertionError("NightOwl must keep distinct physical HUB and toolhead inputs")
        elif target in ("g0b0", "g0b1") and device == "angry_beaver":
            # Happy Hare's real MMB 2.0 board has no PC0 wire at all for this
            # composition; its one shared exit sensor (this catalogue's HUB
            # role) is PA15 (installer/boards/Kconfig.mmb_2_0:141-142).
            forbidden = {"PA11", "PA12"}
            if set(owners) & forbidden:
                raise AssertionError("Angry Beaver candidate uses a USB pin")
            if owners.get("PA15") != "HUB" or owners.get("PC1") != "TOOLHEAD":
                raise AssertionError("Angry Beaver must keep its MMB 2.0 shared exit sensor as HUB")
            if "BAY" in owners.values() or "LOAD" in owners.values():
                raise AssertionError("Angry Beaver must have no BAY/LOAD pins")
        elif device in PASSIVE4_DEVICES:
            forbidden = {"PA11", "PA12", "PA13", "PA14"}
            if set(owners) & forbidden:
                raise AssertionError(f"{device} candidate uses a USB or SWD pin")
            kms = device == "kms"
            if owners.get("PC1") != "TOOLHEAD" or (owners.get("PC0") == "HUB") == kms:
                raise AssertionError(f"{device} HUB/toolhead inputs do not match its profile "
                                     "(KMS: virtual HUB, no HUB pin; others: physical HUB on PC0)")
            if kms != ("LOAD" in owners.values()) or kms != ("BAY" in owners.values()):
                raise AssertionError(f"{device} per-lane BAY/LOAD pin presence does not match its profile")
        elif device in SELECTOR_DEVICES:
            forbidden = {"PA11", "PA12", "PA13", "PA14"}
            if set(owners) & forbidden:
                raise AssertionError(f"{device} candidate uses a USB or SWD pin")
            if owners.get("PC0") != "HUB" or owners.get("PC1") != "TOOLHEAD":
                raise AssertionError(f"{device} must keep a physical HUB gate sensor and toolhead switch")
            if "LOAD" in owners.values():
                raise AssertionError(f"{device} selector candidate must have no LOAD pins")
            has_bay_and_rewind = device == SELECTOR_BAY_AND_REWIND
            if has_bay_and_rewind != ("BAY" in owners.values()):
                raise AssertionError(f"{device} per-lane BAY pin presence does not match its profile")
            if has_bay_and_rewind != ("REWIND_FORWARD" in owners.values()):
                raise AssertionError(f"{device} per-lane rewind pin presence does not match its profile")
            kind = SELECTOR_KIND[device]
            if (kind != "SERVO") != ("SELECTOR_STEP" in owners.values()):
                raise AssertionError(f"{device} selector stepper pins do not match its kind")
            expect_servo = kind in ("LINEAR", "SERVO") and device not in SELECTOR_NO_SERVO
            if expect_servo != ("SERVO" in owners.values()):
                raise AssertionError(f"{device} servo pin does not match its kind")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=ARCHIVE)
    parser.add_argument("--build-dir", type=Path, default=PACKAGE / "build" / "release-matrix")
    parser.add_argument("--targets", nargs="+", choices=tuple(BOOT_FAMILIES),
                        help="limit MCU rows and exact-image checks to these targets")
    args = parser.parse_args()
    selected_targets = tuple(args.targets or BOOT_FAMILIES)
    archive, build = args.archive.resolve(), args.build_dir.resolve()
    if build.exists():
        raise SystemExit(f"refusing existing output: {build}")
    if not archive.is_file():
        raise SystemExit(f"missing pinned archive: {archive}")
    if digest(archive) != ARCHIVE_SHA256:
        raise SystemExit(f"pinned archive hash mismatch: {archive}")
    run(sys.executable, PACKAGE / "tests/motion_budget/check_budget.py", cwd=PACKAGE,
        limit=10)
    build.mkdir(parents=True)
    root = build / "klipper"
    root.mkdir()
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        prefix = f"klipper-{PIN}/"
        if not members or any(not (m.name == prefix[:-1] and m.isdir() or m.name.startswith(prefix)) or m.issym() or m.islnk()
                              or not (m.isfile() or m.isdir()) for m in members):
            raise RuntimeError("archive contains unexpected paths or links")
        for member in members:
            member.name = member.name[len(prefix):]
            if member.name:
                source.extract(member, root, filter="data")
    manifest = json.loads((PACKAGE / "upstream/source-hashes.json").read_text())
    before = {name: digest(root / name) for name in manifest}
    for name, entry in manifest.items():
        if before[name] != entry["before"]:
            raise AssertionError(f"unpinned upstream source: {name}")
    # A file outside every installer-owned path proves removal preserves neighbors.
    sentinel = root / "klippy/extras/release_matrix_sentinel.txt"
    sentinel.write_text("preserve neighbor\n")
    sentinel_hash = digest(sentinel)
    hooks = {name: (root / "src" / name).read_bytes() for name in ("Kconfig", "Makefile")}
    run(sys.executable, INSTALLER, "install-driver-api", root, limit=20)
    if (root / "src/Kconfig").read_bytes() != hooks["Kconfig"]:
        raise AssertionError("driver API installation changed src/Kconfig")
    # Disabled-build equivalence: without the add-on hook the package-owned
    # driver options are undefined. Stock builds with the hook must match.
    # H723 and F407 cover both patched GPIO families.
    equivalence = [target for target in selected_targets if target in ("h723", "f407")]
    unhooked = {target: stock_driver_units(root, target)
                for target in equivalence or selected_targets[:1]}
    for action in ("install", "install-host"):
        run(sys.executable, INSTALLER, action, root, limit=20)
    for action in ("check-driver-api", "check", "check-host"):
        run(sys.executable, INSTALLER, action, root, limit=20)
    # Other chips and devices only in the full (--all-targets) matrix; the
    # default gate builds the BoxTurtle H723 images.
    if not args.targets:
        run(sys.executable, PACKAGE / "tests/klipper_tmc_uart/run_f407.py",
            "--upstream", root,
            "--results", build / "f407-tmcuart-local-ownership",
            cwd=PACKAGE, limit=45)
    for name, entry in manifest.items():
        if digest(root / name) != entry["after"]:
            raise AssertionError(f"installed driver mismatch: {name}")
    for name, original in hooks.items():
        # Only the optional add-on includes are added to upstream build files.
        block = (b"\n# BEGIN klipper-mmu add-on\n" +
                 {"Kconfig": b'osource "src/klipper_mmu/mcu/Kconfig"\n',
                  "Makefile": b"-include src/klipper_mmu/mcu/Makefile\n"}[name] +
                 b"# END klipper-mmu add-on\n")
        if (root / "src" / name).read_bytes() != original + block:
            raise AssertionError(f"unexpected installed hook: {name}")
    if not (root / "src/klipper_mmu").is_symlink() or not (root / "klippy/extras/klipper_mmu").is_symlink():
        raise AssertionError("missing installed package link")
    matrix = []
    if not args.targets or "h723" in selected_targets:
        matrix += [("h723", False, False, False, "usb"),
                   ("h723", True, False, False, "usb"),
                   ("h723", True, False, True, "usb"),
                   ("h723", True, True, False, "usb"),
                   ("h723", True, True, True, "usb"),
                   ("h723", True, True, False, "can"),
                   ("h723", True, True, True, "can")]
    for target in (target for target in selected_targets if target != "h723"):
        matrix += [(target, False, False, False, "usb"),
                   (target, True, False, False, "usb")]
        for device in ("f4custom", "nightowl"):
            for actuation in (False, True):
                matrix.append((target, True, False, actuation, "usb", device))
        if target in ("g0b0", "g0b1"):
            # Happy Hare's real board for both (mcu/Kconfig KLIPPER_MMU_G0B_
            # BOOT_GATES admits Angry Beaver and ERCF 2.0/3.0). ERCF 2.0/3.0
            # add a LINEAR selector, its own TMC UART, a grip servo and an
            # encoder on top of Angry Beaver's shape; KLIPPER_MMU_SIZE_OPT_
            # BUILD (mcu/Kconfig, this add-on's own smallest flash tier)
            # builds klipper-mmu's own objects -Os instead of upstream's
            # global -O2 on this 128 KiB part, which fits both with a wide
            # margin (docs/plan/device-and-stm32-rollout.md has the measured
            # bytes); no further code-size split (e.g. operation_core.c's
            # rewind path) was needed.
            for device in ("angry_beaver", "ercf_2_0", "ercf_3_0"):
                for actuation in (False, True):
                    matrix.append((target, True, False, actuation, "usb", device))
    for device in (("emu", "nightowl") + PASSIVE4_DEVICES + SELECTOR_DEVICES + ("qidi_box",)
                   if not args.targets else ()):
        for actuation in (False, True):
            matrix.append(("h723", True, False, actuation, "usb", device))
    for row in matrix:
        target, enabled, diff_buffer, actuation, transport = row[:5]
        device = row[5] if len(row) > 5 else "boxturtle"
        boot_family = BOOT_FAMILIES[target]
        configure(root, target, enabled, diff_buffer, actuation, transport, device)
        log = run("make", JOBS, *CC_ARGS, *(() if enabled else ("V=1",)), cwd=root, limit=180)
        if not (root / "out/klipper.elf").is_file():
            raise AssertionError("firmware did not link")
        if not enabled and target in unhooked and driver_units(root, log) != unhooked[target]:
            raise AssertionError(f"{target} stock drivers differ with the add-on hook")
        if target == "h723" and enabled and device == "boxturtle" and not diff_buffer:
            dictionary = root / "out/klipper.dict"
            autoconf = root / "out/autoconf.h"
            if not dictionary.is_file() or not autoconf.is_file():
                raise AssertionError("switched firmware dictionary or autoconf is missing")
            symbols = run("arm-none-eabi-nm", "--defined-only", "out/klipper.elf", cwd=root)
            if "km_boxturtle_boot_init" in symbols or "command_km_diagnostic_start" not in symbols:
                raise AssertionError("switched BoxTurtle image did not use hosted startup")
            if "command_km_start" not in symbols:
                raise AssertionError("switched image lacks the production START handler")
            name = "boxturtle-h723-usb-switched" + ("" if actuation else "-safe")
            save_image(root, build, name, device="BoxTurtle/AFC Lite",
                       transport="usb", buffer="switched", actuation_opt_in=actuation)
        elif diff_buffer:
            dictionary = root / "out/klipper.dict"
            autoconf = root / "out/autoconf.h"
            if not dictionary.is_file() or not autoconf.is_file():
                raise AssertionError("firmware dictionary or generated configuration is missing")
            header = run("arm-none-eabi-readelf", "-h", "out/klipper.elf", cwd=root)
            entry = next(line.split(":", 1)[1].strip() for line in header.splitlines()
                         if line.strip().startswith("Entry point address:"))
            if int(entry, 16) != (0x08000000 if transport == "usb" else 0x08020000):
                raise AssertionError("firmware entry does not match selected boot offset")
            symbols = run("arm-none-eabi-nm", "--defined-only", "out/klipper.elf", cwd=root)
            if "km_boxturtle_boot_init" in symbols or "command_km_diagnostic_start" not in symbols:
                raise AssertionError("analog image has the wrong startup entry point")
            probe_commands = ("command_km_diff_probe_start", "command_km_diff_probe_query")
            if actuation and any(name in symbols for name in probe_commands):
                raise AssertionError("actuation image contains raw ADC probe commands")
            if not actuation and not all(name in symbols for name in probe_commands):
                raise AssertionError("diagnostic image lacks raw ADC probe commands")
            constants = json.loads(dictionary.read_text())["config"]
            if ("command_config_km_fps_steps" not in symbols
                    or constants.get("KLIPPER_MMU_HOST_LEASE_MS") != 2000
                    or constants.get("KLIPPER_MMU_FPS_MIN_STEPS") != 72727
                    or constants.get("KLIPPER_MMU_FPS_MAX_STEPS") != 1381817
                    or constants.get("KLIPPER_MMU_FEEDER_STEPS_PER_M") != 727272
                    or "RESERVE_PINS_klipper_mmu_toolhead" in constants):
                raise AssertionError("analog image lacks FPS arrival bounds or kept a toolhead pin")
            name = f"boxturtle-h723-{transport}" + ("" if actuation else "-safe")
            save_image(root, build, name, device="BoxTurtle/AFC Lite",
                       transport=transport, actuation_opt_in=actuation,
                       arrival="FPS arrival on DiffA/DiffB, no toolhead switch")
        elif device == "f4custom":
            header = run("arm-none-eabi-readelf", "-h", "out/klipper.elf", cwd=root)
            entry = next(line.split(":", 1)[1].strip() for line in header.splitlines()
                         if line.strip().startswith("Entry point address:"))
            if int(entry, 16) != 0x08000000:
                raise AssertionError("Custom USB image entry is not 0x08000000")
            symbols = run("arm-none-eabi-nm", "--defined-only", "out/klipper.elf", cwd=root)
            start_symbol = ("command_km_diagnostic_start_host" if boot_family == "HOST"
                            else "command_km_diagnostic_start")
            if start_symbol not in symbols:
                raise AssertionError("Custom image lacks hosted diagnostic startup")
            defined_names = {line.split()[-1] for line in symbols.splitlines()
                             if line.split()}
            if boot_family == "HOST" and any(
                    symbol == stem or symbol.startswith(stem + ".")
                    for symbol in defined_names for stem in
                    ("command_km_diagnostic_start", "km_bi_rng_init", "km_bi_rng_fill")):
                raise AssertionError("host-epoch Custom image unexpectedly contains MCU RNG")
            save_image(root, build,
                       f"custom-{target}-usb" + ("" if actuation else "-safe"),
                       device=f"Custom/{target.upper()}", qualification="software-only candidate",
                       wiring="illustrative one-lane switched-buffer pin map",
                       transport="usb", lane_count=1, buffer="switched",
                       actuation_opt_in=actuation)
        elif device == "emu":
            header = run("arm-none-eabi-readelf", "-h", "out/klipper.elf", cwd=root)
            entry = next(line.split(":", 1)[1].strip() for line in header.splitlines()
                         if line.strip().startswith("Entry point address:"))
            if int(entry, 16) != 0x08000000:
                raise AssertionError("EMU USB image entry is not 0x08000000")
            symbols = run("arm-none-eabi-nm", "--defined-only", "out/klipper.elf", cwd=root)
            if "command_km_diagnostic_start" not in symbols:
                raise AssertionError("EMU safe image lacks hosted diagnostic startup")
            save_image(
                root, build, "emu-h723-usb" + ("" if actuation else "-safe"), device="EMU",
                qualification="software-only candidate",
                wiring="illustrative custom GPIO map; not qualified hardware wiring",
                logical_hub="virtual hub through LOAD; no HUB GPIO",
                transport="usb", lane_count=5, buffer="switched",
                actuation_opt_in=actuation)
        elif device == "nightowl":
            header = run("arm-none-eabi-readelf", "-h", "out/klipper.elf", cwd=root)
            entry = next(line.split(":", 1)[1].strip() for line in header.splitlines()
                         if line.strip().startswith("Entry point address:"))
            if int(entry, 16) != 0x08000000:
                raise AssertionError("NightOwl USB image entry is not 0x08000000")
            symbols = run("arm-none-eabi-nm", "--defined-only", "out/klipper.elf", cwd=root)
            start_symbol = ("command_km_diagnostic_start_host" if boot_family == "HOST"
                            else "command_km_diagnostic_start")
            if start_symbol not in symbols:
                raise AssertionError("NightOwl image lacks hosted diagnostic startup")
            defined_names = {line.split()[-1] for line in symbols.splitlines()
                             if line.split()}
            if boot_family == "HOST" and any(
                    symbol == stem or symbol.startswith(stem + ".")
                    for symbol in defined_names for stem in
                    ("command_km_diagnostic_start", "km_bi_rng_init", "km_bi_rng_fill")):
                raise AssertionError("host-epoch NightOwl image unexpectedly contains MCU RNG")
            save_image(
                root, build, f"nightowl-{target}-usb" + ("" if actuation else "-safe"),
                device="NightOwl", qualification="software-only candidate",
                wiring="illustrative Custom pin map; not verified hardware wiring",
                logical_hub="physical HUB input configured separately from each BAY/LOAD input",
                transport="usb", lane_count=4, buffer="switched",
                actuation_opt_in=actuation)
        elif target in ("g0b0", "g0b1") and device == "angry_beaver":
            header = run("arm-none-eabi-readelf", "-h", "out/klipper.elf", cwd=root)
            entry = next(line.split(":", 1)[1].strip() for line in header.splitlines()
                         if line.strip().startswith("Entry point address:"))
            if int(entry, 16) != 0x08000000:
                raise AssertionError("Angry Beaver USB image entry is not 0x08000000")
            symbols = run("arm-none-eabi-nm", "--defined-only", "out/klipper.elf", cwd=root)
            if "command_km_diagnostic_start_host" not in symbols:
                raise AssertionError("Angry Beaver image lacks hosted diagnostic startup")
            defined_names = {line.split()[-1] for line in symbols.splitlines()
                             if line.split()}
            if any(symbol == stem or symbol.startswith(stem + ".")
                   for symbol in defined_names for stem in
                   ("command_km_diagnostic_start", "km_bi_rng_init", "km_bi_rng_fill")):
                raise AssertionError("host-epoch Angry Beaver image unexpectedly contains MCU RNG")
            save_image(
                root, build, f"angry-beaver-{target}-usb" + ("" if actuation else "-safe"),
                device="Angry Beaver", qualification="software-only candidate",
                wiring="Happy Hare's real MMB 2.0 board (installer/boards/Kconfig.mmb_2_0)",
                logical_hub="MMB 2.0's shared exit sensor (PA15) doubles as this catalogue's HUB",
                transport="usb", lane_count=4, buffer="switched",
                actuation_opt_in=actuation, remaining_gaps=["no hardware qualification"])
        elif device in PASSIVE4_DEVICES:
            header = run("arm-none-eabi-readelf", "-h", "out/klipper.elf", cwd=root)
            entry = next(line.split(":", 1)[1].strip() for line in header.splitlines()
                         if line.strip().startswith("Entry point address:"))
            if int(entry, 16) != 0x08000000:
                raise AssertionError(f"{device} USB image entry is not 0x08000000")
            symbols = run("arm-none-eabi-nm", "--defined-only", "out/klipper.elf", cwd=root)
            if "command_km_diagnostic_start" not in symbols:
                raise AssertionError(f"{device} image lacks hosted diagnostic startup")
            save_image(
                root, build, f"{device.replace('_', '-')}-h723-usb" + ("" if actuation else "-safe"),
                device=PASSIVE4_NAMES[device],
                qualification="software-only candidate",
                wiring="illustrative Custom pin map; not verified hardware wiring",
                logical_hub="physical HUB input configured separately from BAY/LOAD",
                transport="usb", lane_count=4, buffer="switched",
                load_sensors=(device == "kms"), actuation_opt_in=actuation)
        elif device in SELECTOR_DEVICES:
            # KLIPPER_MMU_SELECTOR_BOOT_GATES (H723) or KLIPPER_MMU_G0B_
            # BOOT_GATES (ERCF 2.0/3.0 on G0B0/G0B1) selects WANT_GPIO_LOCAL,
            # WANT_TMCUART_LOCAL and KLIPPER_MMU_TMC_UART_SHARED for every
            # selector-stepper/servo device (mcu/Kconfig), and configure()
            # above always turns on KLIPPER_MMU_DIAGNOSTIC_STARTUP for this
            # branch, so these images reach the same hosted diagnostic
            # startup entry point as EMU/NightOwl/PASSIVE4.
            header = run("arm-none-eabi-readelf", "-h", "out/klipper.elf", cwd=root)
            entry = next(line.split(":", 1)[1].strip() for line in header.splitlines()
                         if line.strip().startswith("Entry point address:"))
            if int(entry, 16) != 0x08000000:
                raise AssertionError(f"{device} USB image entry is not 0x08000000")
            symbols = run("arm-none-eabi-nm", "--defined-only", "out/klipper.elf", cwd=root)
            is_g0b = target in ("g0b0", "g0b1")
            start_symbol = "command_km_diagnostic_start_host" if is_g0b else "command_km_diagnostic_start"
            if start_symbol not in symbols:
                raise AssertionError(f"{device} image lacks hosted diagnostic startup")
            if is_g0b:
                defined_names = {line.split()[-1] for line in symbols.splitlines() if line.split()}
                if any(symbol == stem or symbol.startswith(stem + ".")
                       for symbol in defined_names for stem in
                       ("command_km_diagnostic_start", "km_bi_rng_init", "km_bi_rng_fill")):
                    raise AssertionError(f"host-epoch {device} image unexpectedly contains MCU RNG")
            # tests/klipper_selector_runtime now covers every device in
            # SELECTOR_RUNTIME_DEVICES with a hosted runtime test (docs/plan/
            # device-and-stm32-rollout.md).
            gaps = ["no hardware qualification"]
            if device not in SELECTOR_RUNTIME_DEVICES:
                gaps.insert(0, "no hosted runtime test yet")
            save_image(
                root, build, f"{device.replace('_', '-')}-{target}-usb" + ("" if actuation else "-safe"),
                device=SELECTOR_NAMES[device], qualification="software-only candidate",
                wiring=("Happy Hare's real MMB 2.0 board for the gear, selector and encoder "
                        "(installer/boards/Kconfig.mmb_2_0); HUB/toolhead/buffer stay "
                        "illustrative, like Angry Beaver's own G0B row" if is_g0b else
                        "illustrative Custom pin map; not verified hardware wiring"),
                selector_kind=SELECTOR_KIND[device],
                logical_hub=("physical HUB gate sensor shared by every lane; per-lane "
                            "BAY (its own LOAD1-4) and powered rewind, no LOAD"
                            if device == SELECTOR_BAY_AND_REWIND else
                            "physical HUB gate sensor shared by every lane; no BAY/LOAD"),
                transport="usb", lane_count=SELECTOR_GATES[device], buffer="switched",
                actuation_opt_in=actuation,
                remaining_gaps=gaps)
        run("make", "clean", cwd=root, limit=30)
    # The optional hooks keep stock builds working if the package moves.
    link = root / "src/klipper_mmu"
    link.unlink()
    link.symlink_to(build / "moved-package", target_is_directory=True)
    try:
        target = next(iter(unhooked))
        if stock_driver_units(root, target) != unhooked[target]:
            raise AssertionError("stock drivers differ after the package moved")
    finally:
        link.unlink()
        link.symlink_to(PACKAGE, target_is_directory=True)
    run(sys.executable, INSTALLER, "check", root, limit=20)
    for action in ("remove-host", "remove", "remove-driver-api"):
        run(sys.executable, INSTALLER, action, root, limit=20)
    for name in manifest:
        if digest(root / name) != before[name]:
            raise AssertionError(f"removal changed upstream: {name}")
    for name, original in hooks.items():
        if (root / "src" / name).read_bytes() != original:
            raise AssertionError(f"removal changed hook: {name}")
    if digest(sentinel) != sentinel_hash:
        raise AssertionError("removal changed unrelated file")
    if (root / "src/klipper_mmu").exists() or (root / "klippy/extras/klipper_mmu").exists():
        raise AssertionError("removal left package links")
    for transport in (("usb", "can") if not args.targets or "h723" in selected_targets else ()):
        for suffix in ("-safe", ""):
            artifact = build / "artifacts" / f"boxturtle-h723-{transport}{suffix}"
            wire_env = dict(os.environ, KM_WIRE_KLIPPER_SOURCE=str(root),
                            KM_WIRE_DICTIONARY=str(artifact / "klipper.dict"),
                            KM_WIRE_EXPECT_DIFF_PROBE="1" if suffix == "-safe" else "0")
            run(sys.executable, "-B", PACKAGE / "tests/klipper_host_wire_pinned/run_tests.py",
                cwd=PACKAGE, limit=30, env=wire_env)
            config_env = dict(os.environ, KLIPPER_MMU_TEST_SOURCE=str(root),
                              KLIPPER_MMU_TEST_DICTIONARY=str(artifact / "klipper.dict"),
                              KLIPPER_MMU_TEST_EXPECT_PROBE="1" if suffix == "-safe" else "0")
            config_env["PYTHONPATH"] = os.pathsep.join(
                item for item in (str(PACKAGE), config_env.get("PYTHONPATH", "")) if item)
            run(sys.executable, "-B",
                PACKAGE / "tests/klipper_host_calibration_integration/run_tests.py",
                cwd=PACKAGE, limit=30, env=config_env)
    # EMU and NightOwl H723 images exist only in the full matrix (see rows).
    for suffix in (("-safe", "") if not args.targets else ()):
        emu_artifact = build / "artifacts" / f"emu-h723-usb{suffix}"
        emu_wire_env = dict(os.environ, KM_WIRE_KLIPPER_SOURCE=str(root),
                            KM_WIRE_DICTIONARY=str(emu_artifact / "klipper.dict"),
                            KM_WIRE_EXPECT_DIFF_PROBE="0")
        run(sys.executable, "-B", PACKAGE / "tests/klipper_host_wire_pinned/run_tests.py",
            cwd=PACKAGE, limit=30, env=emu_wire_env)
        run(sys.executable, "-B", PACKAGE / "tests/klipper_emu_runtime/run_image_composition.py",
            "--autoconf", emu_artifact / "autoconf.h",
            "--output", build / f"emu-image-composition{suffix}", cwd=PACKAGE, limit=90)
        if not suffix:
            run(sys.executable, "-B", PACKAGE / "tests/klipper_emu_runtime/run_runtime.py",
                "--autoconf", emu_artifact / "autoconf.h",
                "--dictionary", emu_artifact / "klipper.dict",
                "--build-dir", build / "emu-exact-image-runtime",
                cwd=PACKAGE, limit=180)
    for suffix in ("-safe", ""):
        for target in (t for t in selected_targets if t != "h723" or not args.targets):
            nightowl_artifact = build / "artifacts" / f"nightowl-{target}-usb{suffix}"
            nightowl_wire_env = dict(os.environ, KM_WIRE_KLIPPER_SOURCE=str(root),
                                     KM_WIRE_DICTIONARY=str(nightowl_artifact / "klipper.dict"),
                                     KM_WIRE_EXPECT_DIFF_PROBE="0")
            run(sys.executable, "-B", PACKAGE / "tests/klipper_host_wire_pinned/run_tests.py",
                cwd=PACKAGE, limit=30, env=nightowl_wire_env)
            run(sys.executable, "-B", PACKAGE / "tests/klipper_nightowl_runtime/run_runtime.py",
                "--autoconf", nightowl_artifact / "autoconf.h",
                "--dictionary", nightowl_artifact / "klipper.dict",
                "--mode", "safe" if suffix == "-safe" else "actuation",
                "--build-dir", build / f"nightowl-{target}-hosted-runtime{suffix}",
                cwd=PACKAGE, limit=180)
        # KMS, Angry Beaver, 3MS and QuattroBox 1.0/1.1/2 H723 images exist
        # only in the full matrix, like EMU and NightOwl's H723 row.
        for device in (PASSIVE4_DEVICES if not args.targets else ()):
            passive_artifact = (build / "artifacts"
                                / f"{device.replace('_', '-')}-h723-usb{suffix}")
            passive_wire_env = dict(os.environ, KM_WIRE_KLIPPER_SOURCE=str(root),
                                    KM_WIRE_DICTIONARY=str(passive_artifact / "klipper.dict"),
                                    KM_WIRE_EXPECT_DIFF_PROBE="0")
            run(sys.executable, "-B", PACKAGE / "tests/klipper_host_wire_pinned/run_tests.py",
                cwd=PACKAGE, limit=30, env=passive_wire_env)
            run(sys.executable, "-B", PACKAGE / "tests/klipper_passive_runtime/run_runtime.py",
                "--device", device,
                "--autoconf", passive_artifact / "autoconf.h",
                "--dictionary", passive_artifact / "klipper.dict",
                "--mode", "safe" if suffix == "-safe" else "actuation",
                "--build-dir", build / f"{device}-h723-hosted-runtime{suffix}",
                cwd=PACKAGE, limit=180)
        # Angry Beaver's own G0B0/G0B1 row (real MMB 2.0 board, host-epoch
        # identity) reuses this same passive-runtime harness cheaply: the
        # underlying scenario (tests/klipper_board_startup/test_boxturtle_
        # boot.c's KM_TEST_PASSIVE_RUNTIME) and run_runtime.py both now pick
        # the target's own boot gate and diagnostic-start command, the same
        # way klipper_nightowl_runtime already does for its own G0B row.
        for target in (t for t in selected_targets if t in ("g0b0", "g0b1")):
            beaver_artifact = build / "artifacts" / f"angry-beaver-{target}-usb{suffix}"
            beaver_wire_env = dict(os.environ, KM_WIRE_KLIPPER_SOURCE=str(root),
                                   KM_WIRE_DICTIONARY=str(beaver_artifact / "klipper.dict"),
                                   KM_WIRE_EXPECT_DIFF_PROBE="0")
            run(sys.executable, "-B", PACKAGE / "tests/klipper_host_wire_pinned/run_tests.py",
                cwd=PACKAGE, limit=30, env=beaver_wire_env)
            run(sys.executable, "-B", PACKAGE / "tests/klipper_passive_runtime/run_runtime.py",
                "--device", "angry_beaver",
                "--autoconf", beaver_artifact / "autoconf.h",
                "--dictionary", beaver_artifact / "klipper.dict",
                "--mode", "safe" if suffix == "-safe" else "actuation",
                "--build-dir", build / f"angry-beaver-{target}-hosted-runtime{suffix}",
                cwd=PACKAGE, limit=180)
        # ERCF 2.0/3.0's own G0B0/G0B1 row (real MMB 2.0 board, host-epoch
        # identity): the same cheap reuse as Angry Beaver just above, but
        # through klipper_selector_runtime (its LINEAR selector needs HOME
        # and a lane selection, not just boot-to-READY).
        for target in (t for t in selected_targets if t in ("g0b0", "g0b1")):
            for device in ("ercf_2_0", "ercf_3_0"):
                ercf_artifact = (build / "artifacts"
                                 / f"{device.replace('_', '-')}-{target}-usb{suffix}")
                ercf_wire_env = dict(os.environ, KM_WIRE_KLIPPER_SOURCE=str(root),
                                     KM_WIRE_DICTIONARY=str(ercf_artifact / "klipper.dict"),
                                     KM_WIRE_EXPECT_DIFF_PROBE="0")
                run(sys.executable, "-B", PACKAGE / "tests/klipper_host_wire_pinned/run_tests.py",
                    cwd=PACKAGE, limit=30, env=ercf_wire_env)
                run(sys.executable, "-B", PACKAGE / "tests/klipper_selector_runtime/run_runtime.py",
                    "--device", device,
                    "--autoconf", ercf_artifact / "autoconf.h",
                    "--dictionary", ercf_artifact / "klipper.dict",
                    "--mode", "safe" if suffix == "-safe" else "actuation",
                    "--build-dir", build / f"{device}-{target}-hosted-runtime{suffix}",
                    cwd=PACKAGE, limit=180)
        # One LINEAR/ROTARY/SERVO/INDEXED selector device (SELECTOR_RUNTIME_
        # DEVICES) also exists only in the full matrix, like the passive four.
        for device in (SELECTOR_RUNTIME_DEVICES if not args.targets else ()):
            selector_artifact = (build / "artifacts"
                                 / f"{device.replace('_', '-')}-h723-usb{suffix}")
            selector_wire_env = dict(os.environ, KM_WIRE_KLIPPER_SOURCE=str(root),
                                     KM_WIRE_DICTIONARY=str(selector_artifact / "klipper.dict"),
                                     KM_WIRE_EXPECT_DIFF_PROBE="0")
            run(sys.executable, "-B", PACKAGE / "tests/klipper_host_wire_pinned/run_tests.py",
                cwd=PACKAGE, limit=30, env=selector_wire_env)
            run(sys.executable, "-B", PACKAGE / "tests/klipper_selector_runtime/run_runtime.py",
                "--device", device,
                "--autoconf", selector_artifact / "autoconf.h",
                "--dictionary", selector_artifact / "klipper.dict",
                "--mode", "safe" if suffix == "-safe" else "actuation",
                "--build-dir", build / f"{device}-h723-hosted-runtime{suffix}",
                cwd=PACKAGE, limit=180)
        for target in (target for target in selected_targets if target != "h723"):
            f4_artifact = build / "artifacts" / f"custom-{target}-usb{suffix}"
            run(sys.executable, "-B", PACKAGE / "tests/klipper_f407_runtime/run_runtime.py",
                "--autoconf", f4_artifact / "autoconf.h",
                "--dictionary", f4_artifact / "klipper.dict",
                "--mode", "safe" if suffix == "-safe" else "actuation",
                "--build-dir", build / f"{target}-hosted-runtime{suffix}",
                cwd=PACKAGE, limit=180)
    if not args.targets or "h723" in selected_targets:
        run("make", "-s", "-C", PACKAGE / "tests/klipper_board_startup",
            f"BUILD_DIR={build / 'h723-native-board-startup'}", "build",
            cwd=PACKAGE, limit=120)
        run(build / "h723-native-board-startup/test_board_startup",
            cwd=PACKAGE, limit=60,
            env=dict(os.environ, ASAN_OPTIONS="detect_leaks=0:halt_on_error=1",
                     UBSAN_OPTIONS="halt_on_error=1:abort_on_error=1"))
        active_usb = build / "artifacts/boxturtle-h723-usb/klipper.dict"
        native_env = dict(os.environ, KLIPPER_SOURCE=str(root),
                          KM_WIRE_KLIPPER_SOURCE=str(root),
                          KM_WIRE_DICTIONARY=str(active_usb))
        run(sys.executable, "-B", PACKAGE / "tests/test_commands.py",
            "CommandsTest.test_host_to_native_load_terminal", cwd=PACKAGE,
            limit=30, env=native_env)
        for suffix, label in (("-safe", "safe"), ("", "actuation")):
            run("make", "-s", "-C", PACKAGE / "tests/klipper_board_startup",
                f"BUILD_DIR={build / f'h723-native-{label}'}",
                f"IMAGE_AUTOCONF={build / 'artifacts' / f'boxturtle-h723-usb{suffix}' / 'autoconf.h'}",
                "DIFF_EXTRA_CONFIG=", "test-diff", cwd=PACKAGE, limit=120)
        run("make", "-s", "-C", PACKAGE / "tests/klipper_board_startup",
            f"BUILD_DIR={build / 'h723-fps-composed'}",
            f"IMAGE_AUTOCONF={build / 'artifacts' / 'boxturtle-h723-usb' / 'autoconf.h'}",
            "test-fps-composed", cwd=PACKAGE, limit=240)
        for suffix in ("-safe", ""):
            switched = build / "artifacts" / f"boxturtle-h723-usb-switched{suffix}"
            switched_wire_env = dict(os.environ, KM_WIRE_KLIPPER_SOURCE=str(root),
                                     KM_WIRE_DICTIONARY=str(switched / "klipper.dict"),
                                     KM_WIRE_EXPECT_DIFF_PROBE="0")
            run(sys.executable, "-B", PACKAGE / "tests/klipper_host_wire_pinned/run_tests.py",
                cwd=PACKAGE, limit=30, env=switched_wire_env)
        switched_safe = build / "artifacts/boxturtle-h723-usb-switched-safe"
        switched_safe_test_env = dict(os.environ, KLIPPER_SOURCE=str(root),
                                      KLIPPER_MMU_TEST_SOURCE=str(root),
                                      KLIPPER_MMU_TEST_DICTIONARY=str(switched_safe / "klipper.dict"),
                                      KLIPPER_MMU_TEST_EXPECT_PROBE="0",
                                      KM_WIRE_KLIPPER_SOURCE=str(root),
                                      KM_WIRE_DICTIONARY=str(switched_safe / "klipper.dict"),
                                      PYTHONPATH=os.pathsep.join(
                                          item for item in (str(PACKAGE), os.environ.get("PYTHONPATH", ""))
                                          if item))
        run(sys.executable, "-B", PACKAGE / "tests/test_commands.py",
            "CommandsTest.test_host_to_native_load_terminal", cwd=PACKAGE,
            limit=30, env=switched_safe_test_env)
        switched_actuation = build / "artifacts/boxturtle-h723-usb-switched"
        run("make", "-s", "-C", PACKAGE / "tests/host_h723_command_join",
            f"BUILD_DIR={build / 'h723-host-join-switched'}", "MODE=switched",
            f"IMAGE_AUTOCONF={switched_actuation / 'autoconf.h'}",
            f"KLIPPER_DICTIONARY={switched_actuation / 'klipper.dict'}", "test",
            cwd=PACKAGE, limit=120,
            env=dict(os.environ, KLIPPER_SOURCE=str(root),
                     KM_WIRE_KLIPPER_SOURCE=str(root)))
        active_image = build / "artifacts/boxturtle-h723-usb"
        run("make", "-s", "-f", PACKAGE / "tests/klipper_stepdir/Makefile",
            f"BUILD_DIR={build / 'h723-stepdir-edges'}",
            f"IMAGE_AUTOCONF={active_image / 'autoconf.h'}", "test",
            cwd=PACKAGE, limit=120)
        run("make", "-s", "-f", PACKAGE / "tests/klipper_tmc_feeder/Makefile",
            f"BUILD_DIR={build / 'h723-tmc-feeder'}",
            f"IMAGE_AUTOCONF={active_image / 'autoconf.h'}", "test",
            cwd=PACKAGE, limit=120)
        # The switch-driven host join restores toolhead-switch arrival over
        # the image header; FPS arrival is the composed suite and FPS join.
        run("make", "-s", "-C", PACKAGE / "tests/host_h723_command_join",
            f"BUILD_DIR={build / 'h723-host-join'}",
            f"IMAGE_AUTOCONF={active_image / 'autoconf.h'}",
            f"KLIPPER_DICTIONARY={active_image / 'klipper.dict'}",
            f"EXTRA_CONFIG={PACKAGE / 'tests/klipper_board_startup/shim/switch_arrival_autoconf.h'}",
            "test", cwd=PACKAGE, limit=120,
            env=dict(os.environ, KM_WIRE_KLIPPER_SOURCE=str(root)))
        # The FPS join runs the image as built, and also through OpenAMS
        # when OPENAMS_SOURCE is set.
        openams = os.environ.get("OPENAMS_SOURCE")
        for source in ("", openams) if openams else ("",):
            run("make", "-s", "-C", PACKAGE / "tests/host_h723_command_join",
                f"BUILD_DIR={build / 'h723-host-join-fps'}", "MODE=fps",
                f"IMAGE_AUTOCONF={active_image / 'autoconf.h'}",
                f"KLIPPER_DICTIONARY={active_image / 'klipper.dict'}", "test",
                cwd=PACKAGE, limit=120,
                env=dict(os.environ, KM_WIRE_KLIPPER_SOURCE=str(root),
                         OPENAMS_SOURCE=source))
    artifacts = sorted((build / "artifacts").glob("*/manifest.json"))
    print(f"PASS: {len(matrix)} configurations, {len(artifacts)} saved images; "
          f"targets: {', '.join(selected_targets)}; installed-driver, disabled-build "
          "equivalence, moved-package stock build, selected exact-image runtime "
          "checks and clean removal passed")
    for manifest in artifacts:
        print(f"Artifact: {manifest.parent / 'klipper.elf'}")



if __name__ == "__main__":
    main()
