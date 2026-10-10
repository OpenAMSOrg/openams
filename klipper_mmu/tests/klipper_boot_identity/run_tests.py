#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Boot-identity regressions: one package provider, two sanitized binaries."""

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

COMPILE_TIMEOUT = 45
RUN_TIMEOUT = 10
ASAN_OPTIONS = "detect_leaks=0:halt_on_error=1"
NATIVE_MAP = {"RNG->SR": "review_sr()", "RNG->DR": "review_dr()"}
REPLAY_MAP = {"RNG->DR": "review_dr()"}
REPLAY_CORE = (
    "rate", "trajectory", "step_segments", "buffer_sensor", "follower", "operation_core", "selector_home",
    "event_queue", "device_profile", "protocol_wire", "current_regulator", "mcu_clock",
)
REPLAY_MCU = ("clock", "messages", "commands")
REPLAY_CASES = ("fresh", "reused", "zero", "fault", "foreign", "timeout", "shutdown")
INTERNAL_H = """#pragma once
#include <stdint.h>
struct rng_regs {volatile uint32_t CR, SR, DR, HTCR;};
struct rcc_regs {volatile uint32_t CR, D2CCIP2R;};
#define RNG (&rng_regs)
#define RCC (&rcc_regs)
#define RCC_CR_HSI48ON 0x1000u
#define RCC_CR_HSI48RDY 0x2000u
#define RCC_D2CCIP2R_RNGSEL 0x300u
#define RNG_CR_RNGEN (1u<<2)
#define RNG_CR_CONDRST (1u<<30)
#define RNG_SR_DRDY 1u
#define RNG_SR_CECS 2u
#define RNG_SR_SECS 4u
#define RNG_SR_CEIS 32u
#define RNG_SR_SEIS 64u
uint32_t is_enabled_pclock(uint32_t);
void enable_pclock(uint32_t);
"""
NATIVE_HEADERS = {
    "autoconf.h": "#define CONFIG_MACH_STM32H723 1\n#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H723 1\n",
    "board/irq.h": "#pragma once\ntypedef unsigned irqstatus_t; irqstatus_t irq_save(void); void irq_restore(irqstatus_t);\n",
    "board/misc.h": "#pragma once\n#include <stdint.h>\nuint32_t timer_read_time(void);uint32_t timer_from_us(uint32_t);uint8_t timer_is_before(uint32_t,uint32_t);\n",
    "sched.h": (
        "#pragma once\n#include <stdint.h>\n#define DECL_INIT(x)\n#define DECL_TASK(x)\n#define DECL_SHUTDOWN(x)\n"
        "struct timer {struct timer *next;uint_fast8_t (*func)(struct timer*);uint32_t waketime;};\n"
        "struct task_wake {uint8_t wake;};\nenum {SF_DONE=0,SF_RESCHEDULE=1};\n"
        "void sched_add_timer(struct timer*);void sched_del_timer(struct timer*);\n"
        "void sched_wake_task(struct task_wake*);uint8_t sched_check_wake(struct task_wake*);\n"
        "uint8_t sched_is_shutdown(void);\n"
    ),
    "stm32/internal.h": INTERNAL_H,
}


def native_cases():
    flags = (2, 4, 32, 64)
    cases = [[name] for name in (
        "success", "wrap", "usb", "foreign", "pre_shutdown", "zero",
        "read_crosses_deadline", "commit_crosses_deadline")]
    cases += [["shutdown", str(n)] for n in range(5)]
    cases += [[name, str(n)] for name in ("timeout", "late_ready") for n in range(4)]
    cases += [["error", str(word), str(after), str(flag)]
              for word in range(2) for after in range(2) for flag in flags]
    cases += [["commit_error", str(flag)] for flag in flags]
    return cases


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def substitute(text, mapping):
    """Replace only the named MMIO tokens. Any other drift fails the build."""
    for repl in mapping.values():
        if repl in text:
            raise RuntimeError("substitution name already in provider: %s" % repl)
    counts = {token: text.count(token) for token in mapping}
    missing = [token for token, count in counts.items() if count < 1]
    if missing:
        raise RuntimeError("provider is missing %s" % ", ".join(missing))
    updated = text
    for token, repl in mapping.items():
        updated = updated.replace(token, repl)
    restored = updated
    for token, repl in mapping.items():
        restored = restored.replace(repl, token)
    if restored != text or any(token in updated for token in mapping):
        raise RuntimeError("substitution was not limited to the named MMIO tokens")
    return updated, counts


def write_tree(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def log_run(cmd, log_path, timeout, env=None):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        log.write(("$ " + " ".join(shlex.quote(part) for part in cmd) + "\n").encode())
        log.flush()
        try:
            proc = subprocess.run(
                cmd, stdout=log, stderr=subprocess.STDOUT,
                timeout=timeout, env=env, check=False)
        except subprocess.TimeoutExpired:
            log.write(b"\nTIMEOUT\n")
            return None
    return proc.returncode


def mark_unrun(cases, reason):
    return [{"case": args, "passed": False, "ran": False, "reason": reason} for args in cases]


def run_cases(binary, cases, log_dir, rel_prefix, env):
    records = []
    for args in cases:
        name = "-".join(args)
        rel = rel_prefix + "/" + name + ".log"
        try:
            code = log_run([str(binary), *args], log_dir / (name + ".log"), RUN_TIMEOUT, env)
            records.append({
                "case": args, "log": rel,
                "exit_code": 124 if code is None else code,
                "passed": code == 0, "ran": True,
                "timed_out": code is None,
            })
        except Exception as exc:
            records.append({
                "case": args, "log": rel, "passed": False, "ran": False, "reason": repr(exc),
            })
    return records


def base_flags(include_dirs, extra):
    cmd = [
        "gcc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-Wno-pointer-to-int-cast",
        "-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-fno-pie", "-no-pie", "-g",
    ]
    cmd.extend(extra)
    for inc in include_dirs:
        cmd.extend(["-I", str(inc)])
    return cmd


def note_failure(log, exc):
    with log.open("a") as handle:
        handle.write("\n%s\n%s\n" % (exc, traceback.format_exc()))


def build_native(package, here, work, results, meta):
    cases = native_cases()
    log = results / "native-build.log"
    try:
        provider = package / "mcu" / "boot_identity_h723.c"
        raw = provider.read_bytes()
        updated, counts = substitute(raw.decode("utf-8"), NATIVE_MAP)
        include = work / "include"
        write_tree(include, NATIVE_HEADERS)
        (work / "candidate.c").write_text(updated)
        meta["native"] = {
            "provider": str(provider),
            "original_sha256": hashlib.sha256(raw).hexdigest(),
            "substituted_sha256": hashlib.sha256(updated.encode("utf-8")).hexdigest(),
            "fixture_sha256": digest(here / "fixture.c"),
            "replacements": NATIVE_MAP,
            "counts": counts,
            "control_flow_modified": False,
        }
        cmd = base_flags([include, work, package / "include", package / "mcu"], [])
        cmd += [str(here / "fixture.c"), "-o", str(work / "fixture")]
        code = log_run(cmd, log, COMPILE_TIMEOUT)
        if code != 0:
            raise RuntimeError("native compilation timed out" if code is None else "native compilation failed")
        return work / "fixture"
    except Exception as exc:
        note_failure(log, exc)
        meta["native_error"] = repr(exc)
        meta["native_cases"] = mark_unrun(cases, "native build failed")
        return None


def build_replay(package, here, work, results, meta):
    log = results / "replay-build.log"
    try:
        provider = package / "mcu" / "boot_identity_h723.c"
        raw = provider.read_bytes()
        updated, counts = substitute(raw.decode("utf-8"), REPLAY_MAP)
        sources = [package / "src" / (name + ".c") for name in REPLAY_CORE]
        sources += [package / "mcu" / (name + ".c") for name in REPLAY_MCU]
        sources += [package / "tests" / "mock_driver.c", package / "tests" / "stepper_reference.c",
                   here / "replay.c"]
        shim_c = package / "tests" / "klipper_clock" / "shim_klipper.c"
        shim_h = package / "tests" / "klipper_clock" / "shim_include" / "autoconf.h"
        capture = package / "tests" / "commands" / "capture.c"
        required = sources + [provider, shim_c, shim_h, capture]
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise RuntimeError("missing package sources: " + ", ".join(missing))
        include = work / "include"
        write_tree(include, {
            "autoconf.h":
                "#define CONFIG_MACH_STM32H723 1\n"
                "#define CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H723 1\n"
                "#include \"%s\"\n" % shim_h,
            "stm32/internal.h": INTERNAL_H,
        })
        (work / "provider-under-review.c").write_text(updated)
        meta["replay"] = {
            "provider": str(provider),
            "original_sha256": hashlib.sha256(raw).hexdigest(),
            "substituted_sha256": hashlib.sha256(updated.encode("utf-8")).hexdigest(),
            "replacements": REPLAY_MAP,
            "counts": counts,
            "control_flow_modified": False,
            "source_sha256": {str(path): digest(path) for path in required},
        }
        includes = [
            include, work,
            package / "tests" / "klipper_clock" / "shim_include",
            package / "tests" / "klipper_clock",
            package / "include", package / "mcu", package / "tests", package,
        ]
        flags = base_flags(includes, ["-pedantic", "-Wconversion", "-Wsign-conversion",
                                      "-DCONFIG_KLIPPER_MMU_ACTUATION_OPT_IN=1"])
        shim_o = work / "shim.o"
        code = log_run(
            flags + ["-Dcommand_sendf=unused_clock_sendf", "-c", str(shim_c), "-o", str(shim_o)],
            log, COMPILE_TIMEOUT)
        if code != 0:
            raise RuntimeError("clock shim compilation timed out" if code is None else "clock shim compilation failed")
        binary = work / "replay"
        code = log_run(
            flags + [str(path) for path in sources] + [str(shim_o), "-lm", "-o", str(binary)],
            log, COMPILE_TIMEOUT)
        if code != 0:
            raise RuntimeError("replay compilation timed out" if code is None else "replay compilation failed")
        return binary
    except Exception as exc:
        note_failure(log, exc)
        meta["replay_error"] = repr(exc)
        meta["replay_cases"] = mark_unrun([[name] for name in REPLAY_CASES], "replay build failed")
        return None


def execute(package, here, results):
    meta = {
        "hardware_access": False,
        "control_flow_modified": False,
        "scope": (
            "Simulated registers and drivers in fresh processes. "
            "No physical entropy, freshness, hardware, automatic startup, or actuation evidence."
        ),
    }
    env = dict(os.environ)
    env["ASAN_OPTIONS"] = ASAN_OPTIONS
    with tempfile.TemporaryDirectory(prefix="klipper-boot-identity-") as tmp:
        root = Path(tmp)
        native_bin = build_native(package, here, root / "native", results, meta)
        if native_bin:
            meta["native_cases"] = run_cases(
                native_bin, native_cases(), results / "logs" / "native", "logs/native", env)
        replay_bin = build_replay(package, here, root / "replay", results, meta)
        if replay_bin:
            meta["replay_cases"] = run_cases(
                replay_bin, [[name] for name in REPLAY_CASES],
                results / "logs" / "replay", "logs/replay", env)
    return meta


def tally(records):
    passed = sum(1 for item in records if item.get("passed"))
    failed = [" ".join(item["case"]) for item in records if not item.get("passed")]
    return passed, len(records), failed


def main():
    parser = argparse.ArgumentParser(description="Boot-identity regression fixtures")
    parser.add_argument("--results", required=True)
    parser.add_argument("--package")
    args = parser.parse_args()
    results = Path(args.results)
    if results.exists():
        sys.stderr.write("refusing existing results path: %s\n" % results)
        return 1
    package = Path(args.package).resolve() if args.package else Path(__file__).resolve().parents[2]
    here = Path(__file__).resolve().parent
    summary = {
        "status": "failed",
        "package": str(package),
        "hardware_access": False,
        "compile_timeout_s": COMPILE_TIMEOUT,
        "run_timeout_s": RUN_TIMEOUT,
        "native_cases": [],
        "replay_cases": [],
    }
    try:
        results.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        sys.stderr.write("refusing existing results path: %s\n" % results)
        return 1
    try:
        summary.update(execute(package, here, results))
    except Exception as exc:
        summary["status"] = "failed"
        summary["error"] = repr(exc)
        summary["traceback"] = traceback.format_exc()
    native_passed, native_total, native_failed = tally(summary.get("native_cases") or [])
    replay_passed, replay_total, replay_failed = tally(summary.get("replay_cases") or [])
    summary["native_passed"] = native_passed
    summary["native_total"] = native_total
    summary["native_failed"] = native_failed
    summary["replay_passed"] = replay_passed
    summary["replay_total"] = replay_total
    summary["replay_failed"] = replay_failed
    if "error" not in summary and "traceback" not in summary:
        ok = (
            native_total == 41 and replay_total == 7
            and not native_failed and not replay_failed
            and "native_error" not in summary and "replay_error" not in summary
        )
        summary["status"] = "passed" if ok else "failed"
    try:
        (results / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    except Exception as exc:
        sys.stderr.write("could not write results: %r\n" % (exc,))
        return 1
    print("%s: native %s/%s, replay %s/%s, results %s" % (
        summary["status"], native_passed, native_total, replay_passed, replay_total, results))
    failed = native_failed + replay_failed
    if failed:
        print("failed: %s" % ", ".join(failed))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
