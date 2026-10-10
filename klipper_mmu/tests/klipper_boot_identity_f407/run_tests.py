#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise shared F207/F405/F407/F429/F765/G431/G474 RNG behavior and CMSIS gates."""

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


STATUS_BITS = (2, 4, 32, 64)
CASES = ["success", "repeat-first", "repeat-second", "wrong-pll",
         "clock-conflict", "timeout", "shutdown", "wrap"]
CASES += [f"error-{when}-{bit}" for when in ("before", "after")
          for bit in STATUS_BITS]
GATE_CASES = {
    "valid": ([], None),
    "f405-valid": (["#define CONFIG_MACH_STM32F407 0",
                    "#define CONFIG_MACH_STM32F405 1"], None),
    "f429-valid": (["#define CONFIG_MACH_STM32F407 0",
                    "#define CONFIG_MACH_STM32F429 1",
                    "#define CONFIG_STM32_USB_PA11_PA12 1"], None),
    "f429-wrong-usb": (["#define CONFIG_MACH_STM32F407 0",
                         "#define CONFIG_MACH_STM32F429 1",
                         "#define CONFIG_STM32_USB_PA11_PA12 0"],
                        "requires F207 USB at 120 MHz, F405/F407/F429 USB at 168 MHz, G431/G474 USB at 170 MHz, or F765 USB at 216 MHz"),
    "f207-valid": (["#define CONFIG_MACH_STM32F407 0",
                    "#define CONFIG_MACH_STM32F207 1",
                    "#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F207 1",
                    "#define CONFIG_STM32_USB_PA11_PA12 1",
                    "#define CONFIG_CLOCK_FREQ 120000000u"], None),
    "f765-valid": (["#define CONFIG_MACH_STM32F407 0",
                    "#define CONFIG_MACH_STM32F765 1",
                    "#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F765 1",
                    "#define CONFIG_STM32_USB_PA11_PA12 1",
                    "#define CONFIG_CLOCK_FREQ 216000000u"], None),
    "g431-valid": (["#define CONFIG_MACH_STM32F407 0",
                    "#define CONFIG_MACH_STM32G431 1",
                    "#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F4X5 0",
                    "#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_G4 1",
                    "#define CONFIG_STM32_USB_PA11_PA12 1",
                    "#define CONFIG_CLOCK_FREQ 170000000u"], None),
    "g474-valid": (["#define CONFIG_MACH_STM32F407 0",
                    "#define CONFIG_MACH_STM32G474 1",
                    "#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F4X5 0",
                    "#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_G4 1",
                    "#define CONFIG_STM32_USB_PA11_PA12 1",
                    "#define CONFIG_CLOCK_FREQ 170000000u"], None),
    "nightowl-four-lanes": (["#define CONFIG_KLIPPER_MMU_DEVICE_CUSTOM 0",
                              "#define CONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL 1",
                              "#define CONFIG_KLIPPER_MMU_LANE_COUNT 4"], None),
    "identity-disabled": (["#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F4X5 0"],
                           "requires F207 USB at 120 MHz, F405/F407/F429 USB at 168 MHz, G431/G474 USB at 170 MHz, or F765 USB at 216 MHz"),
    "g4-identity-disabled": (["#define CONFIG_MACH_STM32F407 0",
                              "#define CONFIG_MACH_STM32G431 1",
                              "#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F4X5 0",
                              "#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_G4 0",
                              "#define CONFIG_STM32_USB_PA11_PA12 1",
                              "#define CONFIG_CLOCK_FREQ 170000000u"],
                             "requires USB PA11/PA12 at 170 MHz with supported reference clock"),
    "serial-disabled": (["#define CONFIG_USBSERIAL 0"],
                        "requires USB on PA11/PA12"),
    "pb14-pb15-usb": (["#define CONFIG_STM32_USB_PB14_PB15 1"],
                      "requires USB on PA11/PA12"),
    "wrong-clock": (["#define CONFIG_CLOCK_FREQ 84000000u"],
                     "requires F207 USB at 120 MHz, F405/F407/F429 USB at 168 MHz, G431/G474 USB at 170 MHz, or F765 USB at 216 MHz"),
    "g4-no-pa-usb": (["#define CONFIG_MACH_STM32F407 0",
                      "#define CONFIG_MACH_STM32G431 1",
                      "#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F4X5 0",
                      "#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_G4 1",
                      "#define CONFIG_STM32_USB_PA11_PA12 0",
                      "#define CONFIG_CLOCK_FREQ 170000000u"],
                     "requires USB PA11/PA12 at 170 MHz with supported reference clock"),
    "g4-wrong-clock": (["#define CONFIG_MACH_STM32F407 0",
                        "#define CONFIG_MACH_STM32G431 1",
                        "#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F4X5 0",
                        "#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_G4 1",
                        "#define CONFIG_STM32_USB_PA11_PA12 1",
                        "#define CONFIG_CLOCK_FREQ 160000000u"],
                       "requires USB PA11/PA12 at 170 MHz with supported reference clock"),
    "g4-25mhz-ref": (["#define CONFIG_MACH_STM32F407 0",
                      "#define CONFIG_MACH_STM32G431 1",
                      "#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F4X5 0",
                      "#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_G4 1",
                      "#define CONFIG_STM32_USB_PA11_PA12 1",
                      "#define CONFIG_STM32_CLOCK_REF_25M 1",
                      "#define CONFIG_CLOCK_FREQ 170000000u"],
                     "requires USB PA11/PA12 at 170 MHz with supported reference clock"),
    "non-custom": (["#define CONFIG_KLIPPER_MMU_DEVICE_CUSTOM 0"],
                   "requires Custom/1 or NightOwl/4 passive switched-buffer configuration"),
    "two-lanes": (["#define CONFIG_KLIPPER_MMU_LANE_COUNT 2"],
                  "requires Custom/1 or NightOwl/4 passive switched-buffer configuration"),
    "powered-rewind": (["#define CONFIG_KLIPPER_MMU_POWERED_REWIND 1"],
                       "requires Custom/1 or NightOwl/4 passive switched-buffer configuration"),
    "diff-buffer": (["#define CONFIG_KLIPPER_MMU_DIFF_BUFFER 1"],
                    "requires Custom/1 or NightOwl/4 passive switched-buffer configuration"),
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compile_native(root, here, work, report, target):
    work.mkdir()
    if target in ("f207", "f429", "f765", "g431", "g474"):
        config = (here / "shim" / "autoconf.h").read_text()
        config = config.replace("#define CONFIG_MACH_STM32F407 1",
                                "#define CONFIG_MACH_STM32F407 0")
        config = config.replace(f"#define CONFIG_MACH_STM32{target.upper()} 0",
                                f"#define CONFIG_MACH_STM32{target.upper()} 1")
        if target in ("g431", "g474"):
            config = config.replace("#define CONFIG_MACH_STM32F407 1", "#define CONFIG_MACH_STM32F407 0")
            config = config.replace("#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F4X5 1", "#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F4X5 0")
            config = config.replace("#define CONFIG_CLOCK_FREQ 168000000u", "#define CONFIG_CLOCK_FREQ 170000000u")
            config = config.replace(f"#define CONFIG_MACH_STM32{target.upper()} 0",
                                    f"#define CONFIG_MACH_STM32{target.upper()} 1")
            config = config.replace("#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_G4 0",
                                    "#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_G4 1")
            config = config.replace("#define CONFIG_STM32_USB_PA11_PA12 0",
                                    "#define CONFIG_STM32_USB_PA11_PA12 1")
        else:
            config = config.replace("#define CONFIG_CLOCK_FREQ 168000000u",
                                    f"#define CONFIG_CLOCK_FREQ {120000000 if target == 'f207' else 216000000 if target == 'f765' else 168000000}u")
            if target in ("f207", "f765"):
                config += f"#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_{target.upper()} 1\n"
            config = config.replace("#define CONFIG_STM32_USB_PA11_PA12 0",
                                    "#define CONFIG_STM32_USB_PA11_PA12 1")
        (work / "autoconf.h").write_text(config)
    source = root / "mcu" / "boot_identity_rng_stm32.c"
    original = source.read_text()
    replacements = {"RNG->SR": "review_sr()", "RNG->DR": "review_dr()"}
    counts = {token: original.count(token) for token in replacements}
    if any(count < 1 for count in counts.values()):
        raise RuntimeError("provider changed its mock MMIO access points")
    candidate = original
    for token, replacement in replacements.items():
        candidate = candidate.replace(token, replacement)
    if any(token in candidate for token in replacements):
        raise RuntimeError("provider MMIO substitution was incomplete")
    provider = work / "candidate.c"
    provider.write_text(candidate)
    binary = work / "fixture"
    command = [
        "gcc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-Wconversion",
        "-Wsign-conversion", "-fsanitize=address,undefined",
        "-fno-sanitize-recover=all", "-fno-pie", "-no-pie", "-g",
        "-I", str(work), "-I", str(here / "shim"), "-I", str(root / "include"),
        "-I", str(root / "mcu"),
        str(here / "fixture.c"), "-o", str(binary),
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False,
                            timeout=45)
    report[f"native_build_{target}"] = {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "provider_sha256": sha(source),
        "fixture_sha256": sha(here / "fixture.c"),
        "substitution_counts": counts,
        "control_flow_modified": False,
    }
    if result.returncode:
        raise RuntimeError(f"native {target} identity fixture did not compile")
    return binary


def compile_target(root, here, work, klipper, report, target):
    if not klipper:
        report[f"{target}_compile"] = {"status": "skipped", "reason": "--klipper-source not supplied"}
        return
    klipper = Path(klipper).resolve()
    upstream_readme = root / "upstream" / "README.md"
    pin_match = re.search(r"Pinned source: Klipper `([0-9a-f]{40})`", upstream_readme.read_text())
    if not pin_match:
        raise RuntimeError("could not read the Klipper pin from upstream/README.md")
    pinned_revision = pin_match.group(1)
    internal_header = klipper / "src" / "stm32" / "internal.h"
    internal_hash = sha(internal_header)
    hashes = json.loads((root / "upstream" / "source-hashes.json").read_text())
    accepted_internal_hashes = {
        hashes["src/stm32/internal.h"]["before"],
        hashes["src/stm32/internal.h"]["after"],
    }
    if internal_hash not in accepted_internal_hashes:
        raise RuntimeError("Klipper source does not match the pinned internal.h hash")
    autoconf = work / "autoconf.h"
    f7 = target == "f765"
    f2 = target == "f207"
    g4 = target in ("g431", "g474")
    autoconf.write_text(
        "#pragma once\n"
        f"#define STM32{target.upper()}xx 1\n"
        f"#define CONFIG_MACH_STM32{'G4' if g4 else 'F7' if f7 else 'F2' if f2 else 'F4'} 1\n"
        f"#define CONFIG_MACH_STM32{target.upper()} 1\n"
        f"#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_{'G4' if g4 else target.upper() if f7 or f2 else 'F4X5'} 1\n"
        f"#define CONFIG_STM32_USB_PA11_PA12 {1 if f7 or f2 or g4 or target in ('f405', 'f407', 'f429') else 0}\n"
        "#define CONFIG_KLIPPER_MMU_DEVICE_CUSTOM 1\n"
        "#define CONFIG_KLIPPER_MMU_LANE_COUNT 1\n"
        "#define CONFIG_USBSERIAL 1\n"
        f"#define CONFIG_CLOCK_FREQ {170000000 if g4 else 216000000 if f7 else 120000000 if f2 else 168000000}u\n"
        "#define CONFIG_STM32_CLOCK_REF_INTERNAL 0\n"
        "#define CONFIG_STM32_CLOCK_REF_25M 0\n"
        "#define CONFIG_CLOCK_REF_FREQ 8000000u\n"
    )
    obj = work / f"boot_identity_{target}.o"
    command = [
        "arm-none-eabi-gcc", "-mcpu=cortex-m7" if f7 else "-mcpu=cortex-m3" if f2 else "-mcpu=cortex-m4", "-mthumb", "-ffreestanding",
        "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-Wconversion",
        "-Wsign-conversion", "-c", str(root / "mcu" / "boot_identity_rng_stm32.c"),
        "-o", str(obj), "-I", str(work), "-I", str(klipper / "src"),
        "-I", str(klipper / "src" / "stm32"),
        "-isystem" if f7 else "-I",
        str(klipper / "lib" / ("stm32g4" if g4 else "stm32f7" if f7 else "stm32f2" if f2 else "stm32f4") / "include"),
        "-isystem" if f7 else "-I", str(klipper / "lib" / "cmsis-core"),
        "-I", str(here / "shim"),
        "-I", str(root / "include"), "-I", str(root / "mcu"),
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False,
                            timeout=45)
    report[f"{target}_compile"] = {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "klipper_source": str(klipper),
        "project_pinned_revision": pinned_revision,
        "internal_header_sha256": internal_hash,
        "source_pin_match": True,
        "object_sha256": sha(obj) if obj.is_file() else None,
    }
    if result.returncode:
        raise RuntimeError(f"{target} provider did not compile against the real pinned CMSIS header")


def compile_gates(root, here, work, report):
    base = (here / "shim" / "autoconf.h").read_text()
    records = []
    report["compile_gates"] = records
    for case, (replacements, expected_error) in GATE_CASES.items():
        config = base
        for replacement in replacements:
            macro, value = replacement.removeprefix("#define ").split(" ", 1)
            prefix = f"#define {macro} "
            lines = config.splitlines()
            matches = [i for i, line in enumerate(lines) if line.startswith(prefix)]
            if len(matches) > 1:
                raise RuntimeError(f"expected one {macro} definition in F4x5 fixture config")
            if matches:
                lines[matches[0]] = f"{prefix}{value}"
            else:
                lines.append(f"{prefix}{value}")
            config = "\n".join(lines) + "\n"
        config_path = work / "autoconf.h"
        config_path.write_text(config)
        command = [
            "gcc", "-std=c11", "-fsyntax-only", "-Wall", "-Wextra", "-Werror",
            "-I", str(work), "-I", str(here / "shim"),
            "-I", str(root / "include"), "-I", str(root / "mcu"),
            str(root / "mcu" / "boot_identity_rng_stm32.c"),
        ]
        result = subprocess.run(command, text=True, capture_output=True,
                                check=False, timeout=15)
        passed = ((result.returncode == 0 and expected_error is None)
                  or (result.returncode != 0 and expected_error is not None
                      and expected_error in result.stderr))
        records.append({"case": case, "command": command,
                        "returncode": result.returncode,
                        "stdout": result.stdout, "stderr": result.stderr,
                        "passed": passed})
        if not passed:
            raise RuntimeError("F4x5 provider compile gate failed: " + case)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", required=True)
    parser.add_argument("--klipper-source", default=None,
                        help="optional pinned Klipper tree for F207/F405/F407/F429/F765/G431/G474 CMSIS compiles")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    here = Path(__file__).resolve().parent
    results = Path(args.results).resolve()
    if results.exists():
        raise SystemExit("results path already exists")
    results.mkdir(parents=True)
    report = {"hardware_access": False, "cases": [], "overall": "fail"}
    try:
        with tempfile.TemporaryDirectory(prefix="klipper-mmu-f4x5-identity-") as temp:
            work = Path(temp)
            compile_gates(root, here, work, report)
            for target in ("f207", "f405", "f407", "f429", "f765", "g431", "g474"):
                compile_target(root, here, work, args.klipper_source, report, target)
            for target in ("f207", "f407", "f429", "f765", "g431", "g474"):
                binary = compile_native(root, here, work / target, report, target)
                extra = (["wrong-sai", "wrong-source"] if target == "f765" else
                         ["wrong-pll-source", "wrong-pll-m", "wrong-pll-r",
                          "wrong-pll-ren", "wrong-mux", "wrong-mux-reserved-low",
                          "wrong-mux-reserved-high", "hsi48-off",
                          "hsi48-not-ready", "late-sws-timeout"]
                         if target in ("g431", "g474") else [])
                for case in CASES + extra:
                    command = [str(binary), case]
                    run = subprocess.run(command, text=True, capture_output=True,
                                         check=False, timeout=10,
                                         env={**os.environ, "ASAN_OPTIONS": "detect_leaks=0:halt_on_error=1"})
                    record = {"target": target, "case": case, "command": command,
                              "returncode": run.returncode,
                              "stdout": run.stdout, "stderr": run.stderr,
                              "passed": run.returncode == 0}
                    report["cases"].append(record)
                    if run.returncode:
                        raise RuntimeError(f"{target} fixture failed: {case}")
        report["overall"] = "pass"
    except Exception as exc:
        report["error"] = repr(exc)
    (results / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    gate_passed = sum(c["passed"] for c in report.get("compile_gates", []))
    print("overall", report["overall"], "passed", sum(c["passed"] for c in report["cases"]),
          "of", len(report["cases"]), "compile gates", gate_passed,
          "of", len(report.get("compile_gates", [])))
    if report["overall"] != "pass":
        print(report.get("error", "unknown failure"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
