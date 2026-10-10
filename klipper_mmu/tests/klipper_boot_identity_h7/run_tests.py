#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise the shared F207/F405/F407/F765/H743/H750 RNG provider against a deterministic MMIO model."""

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
TARGET_CASES = {
    "f765": ["wrong-sai", "wrong-source"],
    "h743": ["wrong-divm", "wrong-p1", "fracn", "wrong-hpre", "wrong-d1cpre",
             "hsi48-off", "rng-pll1q", "usb-pll3q", "hsi-source"],
}
TARGET_CASES["h750"] = TARGET_CASES["h743"]
TARGET_FREQ = {"f207": 120000000, "f765": 216000000,
               "h743": 400000000, "h750": 480000000}
TARGET_IDENTITY = {"f207": "F207", "f765": "F765",
                   "h743": "H743_H750", "h750": "H743_H750"}
H7_ERROR = "requires its crystal USB gate at 400/480 MHz"
H743 = ["#define CONFIG_MACH_STM32F407 0",
        "#define CONFIG_MACH_STM32H743 1",
        "#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H743_H750 1",
        "#define CONFIG_STM32_USB_PA11_PA12 1",
        "#define CONFIG_CLOCK_FREQ 400000000u"]
H750 = H743[:1] + ["#define CONFIG_MACH_STM32H750 1"] + H743[2:4] + [
    "#define CONFIG_CLOCK_FREQ 480000000u"]
GATE_CASES = {
    "valid": ([], None),
    "f405-valid": (["#define CONFIG_MACH_STM32F407 0",
                    "#define CONFIG_MACH_STM32F405 1"], None),
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
    "nightowl-four-lanes": (["#define CONFIG_KLIPPER_MMU_DEVICE_CUSTOM 0",
                              "#define CONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL 1",
                              "#define CONFIG_KLIPPER_MMU_LANE_COUNT 4"], None),
    "identity-disabled": (["#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F4X5 0"],
                           "requires F207 USB at 120 MHz, F405/F407/F429 USB at 168 MHz, G431/G474 USB at 170 MHz, or F765 USB at 216 MHz"),
    "serial-disabled": (["#define CONFIG_USBSERIAL 0"],
                        "requires USB on PA11/PA12"),
    "pb14-pb15-usb": (["#define CONFIG_STM32_USB_PB14_PB15 1"],
                      "requires USB on PA11/PA12"),
    "wrong-clock": (["#define CONFIG_CLOCK_FREQ 84000000u"],
                     "requires F207 USB at 120 MHz, F405/F407/F429 USB at 168 MHz, G431/G474 USB at 170 MHz, or F765 USB at 216 MHz"),
    "non-custom": (["#define CONFIG_KLIPPER_MMU_DEVICE_CUSTOM 0"],
                   "requires Custom/1 or NightOwl/4 passive switched-buffer configuration"),
    "two-lanes": (["#define CONFIG_KLIPPER_MMU_LANE_COUNT 2"],
                  "requires Custom/1 or NightOwl/4 passive switched-buffer configuration"),
    "powered-rewind": (["#define CONFIG_KLIPPER_MMU_POWERED_REWIND 1"],
                       "requires Custom/1 or NightOwl/4 passive switched-buffer configuration"),
    "diff-buffer": (["#define CONFIG_KLIPPER_MMU_DIFF_BUFFER 1"],
                    "requires Custom/1 or NightOwl/4 passive switched-buffer configuration"),
    "h743-valid": (H743, None),
    "h750-valid": (H750, None),
    "h750-nightowl-four-lanes": (H750 + ["#define CONFIG_KLIPPER_MMU_DEVICE_CUSTOM 0",
                                         "#define CONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL 1",
                                         "#define CONFIG_KLIPPER_MMU_LANE_COUNT 4"], None),
    "h743-identity-disabled": (H743 + ["#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H743_H750 0"],
                               H7_ERROR),
    "h743-f4x5-identity-only": (H743 + ["#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H743_H750 0",
                                        "#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F4X5 1"],
                                H7_ERROR),
    "h743-at-h750-clock": (H743 + ["#define CONFIG_CLOCK_FREQ 480000000u"], H7_ERROR),
    "h750-at-h743-clock": (H750 + ["#define CONFIG_CLOCK_FREQ 400000000u"], H7_ERROR),
    "h743-internal-clock": (H743 + ["#define CONFIG_STM32_CLOCK_REF_INTERNAL 1"], H7_ERROR),
    "h743-usb-route-missing": (H743 + ["#define CONFIG_STM32_USB_PA11_PA12 0"], H7_ERROR),
    "h743-pb14-pb15-usb": (H743 + ["#define CONFIG_STM32_USB_PA11_PA12 0",
                                   "#define CONFIG_STM32_USB_PB14_PB15 1"],
                           "requires USB on PA11/PA12"),
    "h743-serial": (H743 + ["#define CONFIG_USBSERIAL 0"], "requires USB on PA11/PA12"),
    "h743-diff-buffer": (H743 + ["#define CONFIG_KLIPPER_MMU_DIFF_BUFFER 1"],
                         "requires Custom/1 or NightOwl/4 passive switched-buffer configuration"),
    "h750-powered-rewind": (H750 + ["#define CONFIG_KLIPPER_MMU_POWERED_REWIND 1"],
                            "requires Custom/1 or NightOwl/4 passive switched-buffer configuration"),
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compile_native(root, here, work, report, target):
    work.mkdir()
    if target in TARGET_FREQ:
        config = (here / "shim" / "autoconf.h").read_text()
        config = config.replace("#define CONFIG_MACH_STM32F407 1",
                                "#define CONFIG_MACH_STM32F407 0")
        config = config.replace(f"#define CONFIG_MACH_STM32{target.upper()} 0",
                                f"#define CONFIG_MACH_STM32{target.upper()} 1")
        config = config.replace("#define CONFIG_CLOCK_FREQ 168000000u",
                                f"#define CONFIG_CLOCK_FREQ {TARGET_FREQ[target]}u")
        config += f"#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_{TARGET_IDENTITY[target]} 1\n"
        config += "#define CONFIG_STM32_USB_PA11_PA12 1\n"
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
        "-I", str(work), "-I", str(here / "shim"),
        "-I", str(root / "tests" / "klipper_boot_identity_f407" / "shim"),
        "-I", str(root / "include"),
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
    h7 = target in ("h743", "h750")
    f7 = target == "f765"
    f2 = target == "f207"
    family = "H7" if h7 else "F7" if f7 else "F2" if f2 else "F4"
    autoconf.write_text(
        "#pragma once\n"
        f"#define STM32{target.upper()}xx 1\n"
        f"#define CONFIG_MACH_STM32{family} 1\n#define CONFIG_MACH_STM32{target.upper()} 1\n"
        f"#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_{TARGET_IDENTITY.get(target, 'F4X5')} 1\n"
        f"#define CONFIG_STM32_USB_PA11_PA12 {1 if target in TARGET_FREQ or target in ("f405", "f407") else 0}\n"
        "#define CONFIG_KLIPPER_MMU_DEVICE_CUSTOM 1\n"
        "#define CONFIG_KLIPPER_MMU_LANE_COUNT 1\n"
        "#define CONFIG_USBSERIAL 1\n"
        f"#define CONFIG_CLOCK_FREQ {TARGET_FREQ.get(target, 168000000)}u\n"
        "#define CONFIG_STM32_CLOCK_REF_INTERNAL 0\n"
        "#define CONFIG_STM32_CLOCK_REF_25M 0\n"
        "#define CONFIG_CLOCK_REF_FREQ 8000000u\n"
    )
    obj = work / f"boot_identity_{target}.o"
    command = [
        "arm-none-eabi-gcc", "-mcpu=cortex-m7" if f7 or h7 else "-mcpu=cortex-m3" if f2 else "-mcpu=cortex-m4", "-mthumb", "-ffreestanding",
        "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-Wconversion",
        "-Wsign-conversion", "-c", str(root / "mcu" / "boot_identity_rng_stm32.c"),
        "-o", str(obj), "-I", str(work), "-I", str(klipper / "src"),
        "-I", str(klipper / "src" / "stm32"),
        "-isystem" if f7 or h7 else "-I",
        str(klipper / "lib" / ("stm32" + family.lower()) / "include"),
        "-isystem" if f7 or h7 else "-I", str(klipper / "lib" / "cmsis-core"),
        "-I", str(here / "shim"),
        "-I", str(root / "tests" / "klipper_boot_identity_f407" / "shim"),
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
            "-I", str(root / "tests" / "klipper_boot_identity_f407" / "shim"),
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
                        help="optional pinned Klipper tree for F207/F405/F407/F765/H743/H750 CMSIS compiles")
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
            for target in ("h743", "h750"):
                compile_target(root, here, work, args.klipper_source, report, target)
            for target in ("h743", "h750"):
                binary = compile_native(root, here, work / target, report, target)
                for case in CASES + TARGET_CASES.get(target, []):
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
