#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""One compile, then one fresh process per GPIO-local startup case."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPILE_TIMEOUT_S = 60
CASE_TIMEOUT_S = 10
PORTABLE = [
    "operation_core.c", "selector_home.c",
    "device_profile.c",
    "resource_plan.c",
    "event_queue.c",
    "buffer_sensor.c",
    "current_regulator.c",
    "follower.c",
    "rate.c",
    "trajectory.c",
    "step_segments.c",
    "protocol_wire.c",
    "step_dir_driver.c",
    "pwm_bridge_driver.c",
    "adc_pair.c",
    "tmc2209.c",
    "tmc_uart_codec.c",
    "tmc_uart_config.c",
]
CASES = [("order", 0), ("boundary", 0), ("validation", 0)]
CASES += [("mapping", n) for n in range(3)]
CASES += [("ordinary", n) for n in range(8)]
CASES += [("local", n) for n in range(8)]
CASES += [("deadline", n) for n in range(6)]
CASES += [("switch_busy", 0), ("switch_fail", 0), ("feeder_fail", 0)]
CASES += [("configured", 0), ("configured_ordinary", 0), ("configured_local", 0), ("configured_deadline", 0)]
CASES += [("adc_budget", n) for n in range(5)]

GPIO_LOCAL_H = """\
// SPDX-License-Identifier: GPL-3.0-or-later
/* Generated into the results tree. Not a package file. */
#ifndef STM32_GPIO_LOCAL_H
#define STM32_GPIO_LOCAL_H

#include <stdint.h>

enum {
    GPIO_LOCAL_OK = 0,
    GPIO_LOCAL_ERR_INVALID = -1,
    GPIO_LOCAL_ERR_BUSY = -2,
    GPIO_LOCAL_ERR_SHUTDOWN = -3
};

int gpio_local_reserve(const uint32_t *pins, uint32_t count);

#endif
"""


def sha256_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stream(data):
    if data is None:
        return b"", "none"
    if isinstance(data, str):
        return data.encode("utf-8", "replace"), "str"
    if isinstance(data, bytes):
        return data, "bytes"
    return bytes(data), "bytes"


def run_argv(cmd, timeout_s, env):
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        out, out_kind = stream(exc.stdout)
        err, err_kind = stream(exc.stderr)
        return None, True, out, err, out_kind, err_kind
    out, out_kind = stream(proc.stdout)
    err, err_kind = stream(proc.stderr)
    return proc.returncode, False, out, err, out_kind, err_kind


def failure_record(code, timed_out, limit, out_kind, err_kind):
    return {
        "returncode": code,
        "timed_out": timed_out,
        "timeout_s": limit if timed_out else None,
        "stdout": out_kind,
        "stderr": err_kind,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, default=HERE.parents[1])
    parser.add_argument("--source", type=Path, default=None)
    parser.add_argument("--results", type=Path, required=True)
    args = parser.parse_args()

    results = args.results.resolve()
    if results.exists():
        print("results path already exists", file=sys.stderr)
        return 2

    package = args.package.resolve()
    source_root = package if args.source is None else args.source.resolve()
    results.mkdir(parents=True, exist_ok=False)
    log_dir = results / "logs"
    work = results / "work"
    shim = work / "shim" / "stm32"
    log_dir.mkdir()
    shim.mkdir(parents=True)
    (shim / "gpio_local.h").write_text(GPIO_LOCAL_H, encoding="utf-8")

    labels = [f"{name}-{n}" for name, n in CASES]
    summary = {
        "hardware_access": False,
        "package": str(package),
        "source": str(source_root),
        "limits": {
            "compile_timeout_s": COMPILE_TIMEOUT_S,
            "case_timeout_s": CASE_TIMEOUT_S,
            "retries": 0,
        },
        "commands": [],
        "sources": [],
        "build": {"status": "failed", "failure": None},
        "cases": [{"name": name, "status": "not_run", "failure": None} for name in labels],
    }
    code = 1
    try:
        candidate_c = source_root / "mcu" / "board_startup.c"
        candidate_h = source_root / "mcu" / "board_startup.h"
        fixture = package / "tests" / "klipper_board_startup" / "test_board_startup.c"
        sources = [candidate_c]
        sources += [package / "src" / name for name in PORTABLE]
        sources += [package / "tests" / "mock_driver.c", HERE / "test_batch.c"]
        hashed = [candidate_c, candidate_h, HERE / "test_batch.c", fixture]
        hashed += [path for path in sources if path not in hashed]
        for path in hashed:
            entry = {"path": str(path)}
            if path.is_file():
                entry["sha256"] = sha256_file(path)
            else:
                entry["sha256"] = None
                entry["missing"] = True
            summary["sources"].append(entry)

        includes = [
            work / "shim",
            package / "tests" / "klipper_board_startup" / "shim",
            source_root / "mcu",
            package / "mcu",
            package / "include",
            package / "tests",
            package,
            HERE,
            package / "tests" / "klipper_board_startup",
        ]
        command = [
            os.environ.get("CC", "gcc"),
            "-std=c11", "-Wall", "-Wextra", "-Werror", "-pedantic",
            "-Wconversion", "-Wsign-conversion",
            "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
            "-g", "-O2", "-fno-pie", "-no-pie",
            "-DCONFIG_WANT_GPIO_LOCAL=1",
        ]
        for inc in includes:
            command.extend(["-I", str(inc)])
        command.extend(str(path) for path in sources)
        command.extend(["-lm", "-o", str(work / "test_batch")])
        summary["commands"].append(command)

        env = os.environ.copy()
        env["ASAN_OPTIONS"] = "detect_leaks=0:halt_on_error=1"
        env["UBSAN_OPTIONS"] = "halt_on_error=1:abort_on_error=1"
        status, timed_out, out, err, out_kind, err_kind = run_argv(
            command, COMPILE_TIMEOUT_S, env
        )
        (log_dir / "compile.stdout").write_bytes(out)
        (log_dir / "compile.stderr").write_bytes(err)
        if status != 0:
            summary["build"]["failure"] = failure_record(
                status, timed_out, COMPILE_TIMEOUT_S, out_kind, err_kind
            )
            return 1

        summary["build"]["status"] = "passed"
        binary = work / "test_batch"
        failed = False
        for case, (name, n) in zip(summary["cases"], CASES):
            cmd = [str(binary), name, str(n)]
            summary["commands"].append(cmd)
            status, timed_out, out, err, out_kind, err_kind = run_argv(
                cmd, CASE_TIMEOUT_S, env
            )
            (log_dir / f"{case['name']}.stdout").write_bytes(out)
            (log_dir / f"{case['name']}.stderr").write_bytes(err)
            if status == 0:
                case["status"] = "passed"
            else:
                case["status"] = "failed"
                case["failure"] = failure_record(
                    status, timed_out, CASE_TIMEOUT_S, out_kind, err_kind
                )
                failed = True
        code = 1 if failed else 0
        return code
    finally:
        (results / "summary.json").write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    sys.exit(main())
