#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Adapter-contract runner for CONFIG_WANT_GPIO_LOCAL stepdir GPIO tests."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_PACKAGE = HERE.parents[1]

CASES = (
    "reject_step_unclaimed",
    "reject_step_ordinary",
    "reject_dir_unclaimed",
    "reject_dir_ordinary",
    "reject_enable_unclaimed",
    "reject_enable_ordinary",
    "local_setup_polarities",
    "physical_fail_step",
    "physical_fail_dir",
    "missing_claim_distinct_slots",
)

GPIO_LOCAL_H = """\
// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_STM32_GPIO_LOCAL_H
#define TEST_STM32_GPIO_LOCAL_H

#include <stdint.h>
#include "board/gpio.h"

int gpio_pin_is_local(uint32_t pin);
struct gpio_out gpio_out_setup_local(uint32_t pin, uint32_t val);

#endif
"""

# mcu/stepdir_gpio.c now sets up a WANT_STEPPER_LOCAL local stepper
# (upstream/drivers-local.patch); this suite tests claims and polarities, not
# real stepping, so stub the whole surface: stepper_local_setup() returns a
# dummy handle and the rest (queue/room/position/active/stop) are no-ops.
STEPPER_H = """\
// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_STEPPER_H
#define TEST_STEPPER_H

#include <stdint.h>
#include "board/gpio.h"

struct stepper;
struct stepper *stepper_local_setup(struct gpio_out step_pin, struct gpio_out dir_pin,
                                    int_fast8_t invert_step, uint32_t step_pulse_ticks,
                                    uint8_t max_moves);
int stepper_local_queue(struct stepper *s, uint32_t start_clock, uint32_t interval,
                        uint16_t count, int16_t add, uint8_t dir);
int stepper_local_room(struct stepper *s);
int32_t stepper_local_position(struct stepper *s);
int stepper_local_active(struct stepper *s);
void stepper_local_stop(struct stepper *s);

#endif
"""

STEPPER_LOCAL_SETUP_STUB_C = """\
// SPDX-License-Identifier: GPL-3.0-or-later
#include "stepper.h"

struct stepper *stepper_local_setup(struct gpio_out step_pin, struct gpio_out dir_pin,
                                    int_fast8_t invert_step, uint32_t step_pulse_ticks,
                                    uint8_t max_moves)
{
    (void)step_pin; (void)dir_pin; (void)invert_step; (void)step_pulse_ticks; (void)max_moves;
    static int dummy_stepper_tag;
    return (struct stepper *)&dummy_stepper_tag;
}
"""

COMPILE_TIMEOUT_S = 60
CASE_TIMEOUT_S = 10


def as_bytes(data):
    if data is None:
        return b""
    if isinstance(data, str):
        return data.encode("utf-8", errors="replace")
    return data


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def run_logged(cmd, log_path, timeout_s, cwd):
    timed_out = False
    code = None
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            check=False,
        )
        out = completed.stdout
        err = completed.stderr
        code = completed.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        out = exc.stdout
        err = exc.stderr
    text = b"--- stdout ---\n" + as_bytes(out) + b"\n--- stderr ---\n" + as_bytes(err)
    if timed_out:
        text += b"\n--- timeout ---\n"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_bytes(text)
    return code, timed_out


def write_summary(results, payload):
    (results / "summary.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, default=DEFAULT_PACKAGE)
    parser.add_argument("--source", type=Path, default=None)
    parser.add_argument("--results", type=Path, required=True)
    return parser.parse_args()


def main():
    args = parse_args()
    package = args.package.resolve()
    source = args.source.resolve() if args.source is not None else package
    results = args.results.resolve()
    if results.exists():
        sys.stderr.write("refusing to overwrite existing results path: %s\n" % results)
        return 2
    results.mkdir(parents=True)
    logs = results / "logs"
    work = results / "work"
    shim = results / "shim"
    logs.mkdir()
    work.mkdir()

    needed = {
        "source/mcu/stepdir_gpio.c": source / "mcu" / "stepdir_gpio.c",
        "source/mcu/stepdir_gpio.h": source / "mcu" / "stepdir_gpio.h",
        "mcu/clock.c": package / "mcu" / "clock.c",
        "src/step_dir_driver.c": package / "src" / "step_dir_driver.c",
        "src/mcu_clock.c": package / "src" / "mcu_clock.c",
        "src/rate.c": package / "src" / "rate.c",
        "src/trajectory.c": package / "src" / "trajectory.c",
        "src/step_segments.c": package / "src" / "step_segments.c",
        "tests/klipper_stepdir/shim_klipper.c": package / "tests" / "klipper_stepdir" / "shim_klipper.c",
        "tests/stub_stepper_local/stepper_local_stub.c":
            package / "tests" / "stub_stepper_local" / "stepper_local_stub.c",
        "tests/klipper_stepdir_gpio_local/test_claims.c": HERE / "test_claims.c",
    }
    sources = {}
    missing = []
    for key, path in needed.items():
        if path.is_file():
            sources[key] = file_sha256(path)
        else:
            missing.append(str(path))
    shim_include = package / "tests" / "klipper_stepdir" / "shim_include"
    if not shim_include.is_dir():
        missing.append(str(shim_include))

    binary = work / "test_claims"
    compile_cmd = [
        "gcc",
        "-std=c11",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-pedantic",
        "-Wconversion",
        "-Wsign-conversion",
        "-g",
        "-O1",
        "-fno-pie",
        "-no-pie",
        "-fsanitize=address,undefined",
        "-fno-sanitize-recover=all",
        "-DCONFIG_WANT_GPIO_LOCAL=1",
        "-DCONFIG_STEPPER_LOCAL_COUNT=8",
        "-DCONFIG_STEPPER_LOCAL_MOVES=224",
        "-I", str(shim),
        "-I", str(shim_include),
        "-I", str(package / "tests" / "klipper_stepdir"),
        "-I", str(work),
        "-I", str(package / "mcu"),
        "-I", str(package / "include"),
        str(work / "stepdir_gpio.c"),
        str(package / "mcu" / "clock.c"),
        str(package / "src" / "step_dir_driver.c"),
        str(package / "src" / "mcu_clock.c"),
        str(package / "src" / "rate.c"),
        str(package / "src" / "trajectory.c"),
        str(package / "src" / "step_segments.c"),
        str(package / "tests" / "klipper_stepdir" / "shim_klipper.c"),
        str(package / "tests" / "stub_stepper_local" / "stepper_local_stub.c"),
        str(work / "stepper_local_setup_stub.c"),
        str(HERE / "test_claims.c"),
        "-lm",
        "-Wl,--wrap=gpio_out_setup",
        "-o", str(binary),
    ]
    summary = {
        "hardware_access": False,
        "passed": 0,
        "failed": 0,
        "not_run": 0,
        "sources": sources,
        "compile": {
            "status": "not_run",
            "command": compile_cmd,
            "log": "logs/compile.log",
            "timeout_s": COMPILE_TIMEOUT_S,
            "returncode": None,
        },
        "cases": [],
    }

    if missing:
        (logs / "compile.log").write_text("missing inputs:\n" + "\n".join(missing) + "\n", encoding="utf-8")
        summary["compile"]["status"] = "failed"
        for name in CASES:
            summary["cases"].append({
                "name": name,
                "status": "not_run",
                "command": [str(binary), name],
                "log": "logs/%s.log" % name,
                "returncode": None,
                "timed_out": False,
            })
        summary["not_run"] = len(CASES)
        write_summary(results, summary)
        return 1

    shutil.copyfile(source / "mcu" / "stepdir_gpio.c", work / "stepdir_gpio.c")
    shutil.copyfile(source / "mcu" / "stepdir_gpio.h", work / "stepdir_gpio.h")
    shutil.copytree(shim_include, shim)
    (shim / "stm32").mkdir(exist_ok=True)
    (shim / "stm32" / "gpio_local.h").write_text(GPIO_LOCAL_H, encoding="utf-8")
    # mcu/stepdir_gpio.c now #include "stepper.h" (WANT_STEPPER_LOCAL); shim
    # is the real, complete klipper_stepdir shim_include (has struct
    # gpio_out), so this stub's own "board/gpio.h" include resolves there.
    (shim / "stepper.h").write_text(STEPPER_H, encoding="utf-8")
    (work / "stepper_local_setup_stub.c").write_text(STEPPER_LOCAL_SETUP_STUB_C, encoding="utf-8")

    code, timed_out = run_logged(compile_cmd, logs / "compile.log", COMPILE_TIMEOUT_S, results)
    summary["compile"]["returncode"] = code
    summary["compile"]["timed_out"] = timed_out
    if timed_out or code != 0:
        summary["compile"]["status"] = "failed"
        for name in CASES:
            summary["cases"].append({
                "name": name,
                "status": "not_run",
                "command": [str(binary), name],
                "log": "logs/%s.log" % name,
                "returncode": None,
                "timed_out": False,
            })
        summary["not_run"] = len(CASES)
        write_summary(results, summary)
        return 1
    summary["compile"]["status"] = "passed"

    for name in CASES:
        cmd = [str(binary), name]
        log_rel = "logs/%s.log" % name
        case_code, case_timeout = run_logged(cmd, results / log_rel, CASE_TIMEOUT_S, results)
        status = "passed" if case_code == 0 and not case_timeout else "failed"
        summary["cases"].append({
            "name": name,
            "status": status,
            "command": cmd,
            "log": log_rel,
            "returncode": case_code,
            "timed_out": case_timeout,
        })
        summary[status] += 1

    write_summary(results, summary)
    return 0 if summary["failed"] == 0 and summary["not_run"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
