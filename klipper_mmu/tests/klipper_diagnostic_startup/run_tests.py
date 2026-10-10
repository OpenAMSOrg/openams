#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Host orchestration runner for diagnostic startup. One fresh process per case."""
import argparse, hashlib, json, os, subprocess, sys
from pathlib import Path

CASES = [
    "idle", "ready", "pending", "prereq", "timeout_clock", "timeout_identity", "timeout_wrap",
    "pre_clock", "pre_ident", "pre_zero", "clock_owned", "foreign_wait", "foreign_busy",
    "foreign_err", "fail_service", "fail_core", "fail_attach", "cost_begin", "cost_service",
    "cost_attach", "shut_before", "shut_begin", "shut_service", "shut_attach",
]
# Host-epoch builds have no hardware provider, so provider-result cases do not apply.
PROVIDER_CASES = {"prereq", "timeout_identity", "pre_ident", "pre_zero"}
HOST_CASES = ["host_zero", "host_rebind"]
CLOCKS = (1000000, 12000000, 520000000)
TIMEOUT_US = 1000000

def dec(data):
    if data is None: return ""
    if isinstance(data, str): return data
    return bytes(data).decode("utf-8", "replace")

def run_cmd(cmd, timeout):
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False, env=child_env())
        return proc.returncode, False, dec(proc.stdout), dec(proc.stderr)
    except subprocess.TimeoutExpired as exc:
        return None, True, dec(exc.stdout), dec(exc.stderr)

def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None

def child_env():
    env = os.environ.copy()
    flag = "halt_on_error=1"
    for key in ("ASAN_OPTIONS", "UBSAN_OPTIONS"):
        cur = env.get(key, "")
        if "halt_on_error=" not in cur:
            env[key] = flag if not cur else cur + ":" + flag
    return env

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--source", default=None)
    parser.add_argument("--results", required=True)
    parser.add_argument("--mode", choices=("switched", "diff", "actuation", "actuation_diff",
                                           "host_epoch", "host_epoch_actuation"), default="switched")
    args = parser.parse_args()
    host = args.mode.startswith("host_epoch")
    case_names = (["missing_calibration"] if "diff" in args.mode else []) + (
        HOST_CASES + [c for c in CASES if c not in PROVIDER_CASES] if host else CASES)
    package = Path(args.package).resolve()
    source = Path(args.source).resolve() if args.source else package
    results = Path(args.results).resolve()
    if results.exists():
        print("fail results directory already exists", file=sys.stderr)
        return 1
    results.mkdir(parents=True, exist_ok=False)
    here = Path(__file__).resolve().parent
    prod_c, prod_h = source / "mcu" / "diagnostic_startup.c", source / "mcu" / "diagnostic_startup.h"
    shim_files = sorted(p for p in (here / "shim").rglob("*") if p.is_file())
    fixtures = [here / "run_tests.py", here / "test_startup.c", *shim_files]
    sources = {
        "c": {"path": str(prod_c), "sha256": sha256(prod_c)},
        "h": {"path": str(prod_h), "sha256": sha256(prod_h)},
        "fixtures": [{"path": str(p), "sha256": sha256(p)} for p in fixtures],
    }
    includes = ["-I", str(here / "shim"), "-I", str(source / "mcu"),
                "-I", str(package / "mcu"), "-I", str(package / "include")]
    passed = case_failures = compile_failures = cases_not_run = 0
    compiles, cases = [], []
    for freq in CLOCKS:
        exe = results / f"startup-{args.mode}-{freq}"
        cmd = ["gcc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-Wconversion", "-Wsign-conversion",
               "-fsanitize=address,undefined", "-fno-sanitize-recover=address,undefined", "-fno-pie", "-no-pie",
               "-DCONFIG_MACH_STM32H723=1",
               *(["-DCONFIG_MACH_STM32=1", "-DCONFIG_KLIPPER_MMU_BOOT_IDENTITY_HOST=1"] if host
                 else ["-DCONFIG_KLIPPER_MMU_BOOT_IDENTITY_H723=1"]),
               *(["-DCONFIG_KLIPPER_MMU_DIFF_BUFFER=1",
                  "-DCONFIG_WANT_ADC=1", "-DCONFIG_WANT_ADC_LOCAL=1"] if "diff" in args.mode else []),
               *(["-DCONFIG_KLIPPER_MMU_DIAGNOSTIC_STARTUP=1",
                  "-DCONFIG_KLIPPER_MMU_ACTUATION_OPT_IN=1"] if "actuation" in args.mode else []),
               f"-DCONFIG_CLOCK_FREQ={freq}u", f"-DCONFIG_KLIPPER_MMU_DIAGNOSTIC_TIMEOUT_US={TIMEOUT_US}u",
               *includes, str(here / "test_startup.c"), str(prod_c), "-o", str(exe)]
        rc, timed, out, err = run_cmd(cmd, 60)
        (results / f"compile-{freq}.log").write_text(out + err)
        compiles.append({"clock": freq, "command": cmd, "returncode": rc, "timed_out": timed, "stdout": out, "stderr": err})
        if timed or rc != 0 or not exe.is_file():
            compile_failures += 1
            cases_not_run += len(case_names)
            for name in case_names:
                cases.append({"name": name, "clock": freq, "status": "not_run", "command": None,
                              "returncode": None, "timed_out": False, "stdout": "", "stderr": ""})
            continue
        for name in case_names:
            case_cmd = [str(exe), name]
            rc, timed, out, err = run_cmd(case_cmd, 5)
            (results / f"{freq}-{name}.log").write_text(out + "\n" + err)
            ok = rc == 0 and not timed
            passed += int(ok)
            case_failures += int(not ok)
            cases.append({"name": name, "clock": freq, "status": "pass" if ok else "fail", "command": case_cmd,
                          "returncode": rc, "timed_out": timed, "stdout": out, "stderr": err})
    ran_ok = (compile_failures == 0 and case_failures == 0 and cases_not_run == 0
              and passed == len(case_names) * len(CLOCKS))
    overall = "pass" if ran_ok else "fail"
    (results / "summary.json").write_text(json.dumps({"sources": sources, "argv": sys.argv,
        "hardware_access": False, "mode": args.mode, "overall": overall, "compiles_failed": compile_failures,
        "cases_failed": case_failures, "cases_not_run": cases_not_run, "passed": passed,
        "compiles": compiles, "cases": cases}, indent=2) + "\n")
    print(f"overall {overall} pass {passed} compiles_failed {compile_failures} cases_failed {case_failures} cases_not_run {cases_not_run}")
    return 0 if ran_ok else 1

if __name__ == "__main__":
    try: sys.exit(main())
    except Exception as exc:
        print(f"fail runner {exc}", file=sys.stderr); sys.exit(1)
