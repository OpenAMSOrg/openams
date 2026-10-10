#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run registered portable and optional Klipper-MMU checks with saved logs."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_MAKEFILES = (
    "klipper_adc", "klipper_clock", "klipper_controller", "klipper_sensors",
    "klipper_stepdir", "klipper_switch", "klipper_pwm_routes",
    "klipper_startup_composition", "klipper_stepper_local", "klipper_encoder",
)

DEFAULT_RUNNERS = {
    "tests/klipper_board_gpio_local/run_tests.py": ("--results", "board-gpio"),
    "tests/klipper_stepdir_gpio_local/run_tests.py": ("--results", "stepdir-gpio"),
    "tests/klipper_boot_identity/run_tests.py": ("--results", "boot-identity"),
    "tests/klipper_boot_identity_f407/run_tests.py": ("--results", "boot-identity-f4-native"),
    "tests/klipper_diagnostic_startup/run_tests.py": ("--results", "diagnostic-startup"),
    "tests/klipper_diff_probe/run_tests.py": ("--results", "diff-probe"),
    "tests/klipper_pwm_binding/run_fake.py": ("--results", "pwm-binding-fake.json"),
    "tests/klipper_pwm_pair/run_tests.py": ("positional", "pwm-pair"),
    # Host-gcc-only sim models; they take no arguments.
    "tests/linux_mcu/tmc2209_model_test/run.py": (None, "tmc2209-model"),
    "tests/linux_mcu/plant_model_test/run.py": (None, "plant-model"),
}

# These source-gated checks are intentionally the selected subset of the much
# larger installed-source and saved-image inventory below.
UPSTREAM_RUNNERS = {
    "tests/klipper_config_isolation/run_tests.py": "config-isolation",
    "tests/klipper_boot_identity_f407/run_tests.py": "boot-identity-f4-cmsis",
    "tests/klipper_boot_identity_h7/run_tests.py": "boot-identity-h7",
}
UPSTREAM_MODULES = {
    "tests/test_commands.py", "tests/test_config.py", "tests/test_messages.py",
    "tests/test_pin_config.py", "tests/klipper_emu_runtime/test_planned.py",
}
OPENAMS_MODULES = {
    "tests/test_requests.py",
    "tests/klipper_ams_family_contract/test_fault_hook.py",
    "tests/klipper_ams_family_contract/test_runtime_contract.py",
    "tests/klipper_event_cycle/test_cycle.py",
    "tests/klipper_fault_composition/test_fault_composition.py",
    "tests/klipper_openams_hook/test_runtime_hook.py",
    "tests/klipper_openams_manager_discovery/test_discovery.py",
    "tests/klipper_openams_operation_bridge/test_runtime.py",
    "tests/klipper_openams_retired_integration/test_integration.py",
    "tests/klipper_openams_retired_runtime/test_retired.py",
    "tests/klipper_openams_terminal_consumer/test_consumer.py",
    "tests/klipper_openams_terminal_integration/test_failed_terminal.py",
}
SAVED_MODULES = {"tests/klipper_host_calibration/test_delivery.py"}
DEFAULT_MODULES = {
    "tests/afc_facts/test_afc_facts.py",
    "tests/klipper_command_transport/test_command_transport.py",
    "tests/klipper_eject_host/test_eject.py",
    "tests/klipper_event_ack/test_ack.py",
    "tests/klipper_event_delivery/test_delivery.py", "tests/klipper_event_reader/test_reader.py",
    "tests/klipper_host/test_bowden_estimate.py",
    "tests/klipper_host/test_feeder_rotation.py",
    "tests/local_doc/test_local_doc.py",
    "tests/mcp_server/test_mcp_server.py",
    "tests/check_tool/test_check.py",
    "tests/premerge/test_premerge.py",
    "tests/pin_plan_check/test_pin_plan_check.py",
    "tests/device_index/test_device_index.py",
    "tests/test_integrity/test_test_integrity.py",
    "tests/masked_reach/test_masked_reach.py",
    "tests/thumb_cost/test_thumb_cost.py",
    "tests/context_pack/test_context_pack.py",
    "tests/site_browser/test_site_browser.py",
    "tests/site_package/test_site_package.py",
    "tests/gdb_probe/test_gdb_probe.py",
    "tests/driver_api_bundle/test_driver_api_bundle.py",
    "tests/klipper_host/test_cold_session.py",
    "tests/klipper_host/test_diagnose_command.py",
    "tests/klipper_host/test_diagnostics.py", "tests/klipper_host/test_event_entry.py",
    "tests/klipper_host/test_fault_ack_recovery.py", "tests/klipper_host/test_gate_metadata.py",
    "tests/klipper_host/test_host_lease_entry.py", "tests/klipper_host/test_host_recovery.py",
    "tests/klipper_host/test_load_report.py",
    "tests/klipper_host/test_maintenance_counters.py", "tests/klipper_host/test_print_session.py",
    "tests/klipper_host/test_recover_session_command.py",
    "tests/klipper_host/test_recover_waiter_command.py", "tests/klipper_host/test_retained_fault.py",
    "tests/klipper_host/test_switch_delay_query.py", "tests/klipper_host/test_tool_mapping.py",
    "tests/klipper_host/test_selector_features.py", "tests/klipper_host/test_sensor_wide_lanes.py",
    "tests/klipper_host_boot_transition/test_boot_transition.py",
    "tests/klipper_host_calibration/test_encoder.py",
    "tests/klipper_host_event/test_command.py",
    "tests/klipper_host_physical_command/test_command.py",
    "tests/klipper_host_sensor_command/test_command.py", "tests/klipper_host_terminal/test_command.py",
    "tests/klipper_openams_cancel_bridge/test_cancel.py",
    "tests/klipper_openams_command_device/test_command_device.py",
    "tests/klipper_openams_device/test_device.py",
    "tests/klipper_openams_device/test_managed_unit.py",
    "tests/klipper_openams_device_observation/test_observation.py",
    "tests/klipper_openams_device_state/test_state.py",
    "tests/klipper_openams_follow_bridge/test_follow.py",
    "tests/klipper_openams_follow_composition/test_composition.py",
    "tests/klipper_openams_host_lease/test_lease.py",
    "tests/klipper_openams_operation_bridge/test_calibrate.py",
    "tests/klipper_openams_operation_bridge/test_load.py",
    "tests/klipper_openams_operation_bridge/test_unload.py",
    "tests/klipper_openams_readonly_registration/test_registration.py",
    "tests/klipper_openams_sensor_projection/test_projection.py",
    "tests/klipper_openams_session_bootstrap/test_bootstrap.py",
    "tests/klipper_openams_verified_follower/test_verified.py",
    "tests/klipper_operation_stats/test_stats.py",
    "tests/klipper_physical_ownership/test_ownership.py",
    "tests/klipper_diag_reader/test_reader.py",
    "tests/klipper_physical_state_reader/test_reader.py", "tests/klipper_sensor_reader/test_reader.py",
    "tests/klipper_session_reconciliation/test_session.py",
    "tests/klipper_host/test_path_reconcile.py",
    "tests/klipper_host/test_driver_restart.py",
    "tests/klipper_host/test_selector_calibration.py",
    "tests/klipper_terminal_observer/test_terminal_observer.py",
    "tests/test_diff_probe_host.py",
    "tests/test_driver_install.py", "tests/test_host_install.py", "tests/test_install.py",
    "tests/test_report_migration.py", "tests/test_wire_enumerations.py",
    "tests/upstream_dossier/test_upstream_dossier.py",
}
UPSTREAM_OTHER_RUNNERS = {
    "tests/klipper_gpio_local/run_tests.py": "KLIPPER_SOURCE plus pinned archive for the F4 baseline",
    "tests/klipper_adc/run_composed.py": "KLIPPER_SOURCE",
    "tests/klipper_adc/run_setup_deadlines.py": "KLIPPER_SOURCE",
    "tests/klipper_pwm_binding/run_composed.py": "installed Klipper with the PWM bridge driver",
    "tests/klipper_calibration_receiver/run_tests.py": "KLIPPER_SOURCE",
    "tests/klipper_tmc_feeder/run_shared.py": "KLIPPER_SOURCE with installed driver",
    "tests/klipper_tmc_uart/run_ownership.py": "KLIPPER_SOURCE with installed driver",
    "tests/klipper_tmc_uart/run_shared.py": "KLIPPER_SOURCE with installed driver",
}
SAVED_RUNNERS = {
    "tests/host_h723_command_join/run_join.py": "saved H723 image and dictionary",
    "tests/klipper_board_startup/run_tests.py": "saved autoconf header",
    "tests/klipper_emu_runtime/run_image_composition.py": "saved EMU autoconf header",
    "tests/klipper_emu_runtime/run_runtime.py": "saved EMU autoconf header",
    "tests/klipper_f407_runtime/run_runtime.py": "saved F407 image and dictionary",
    "tests/klipper_host/run_connection.py": "pinned Klipper source and saved dictionary",
    "tests/klipper_host_calibration_integration/run_tests.py": "saved calibration dictionary",
    "tests/klipper_host_wire_pinned/run_tests.py": "saved message dictionary",
    "tests/klipper_nightowl_runtime/run_runtime.py": "saved NightOwl image and dictionary",
    "tests/klipper_passive_runtime/run_runtime.py": "saved passive-feeder image and dictionary",
    "tests/klipper_selector_runtime/run_runtime.py": "saved selector image and dictionary",
    "tests/klipper_terminal_pinned/run_tests.py": "saved pinned Klipper build",
    "tests/klipper_tmc_uart/run_f407.py": "pinned archive and F407 image",
    "tests/release_matrix/run.py": "pinned release archive; selected by --release",
    "tests/stm32_target_matrix/run.py": "pinned release archive",
}

# Runners that a default Makefile's test target invokes.
MAKEFILE_RUNNERS = {
    "tests/klipper_pwm_routes/run_tests.py": "tests/klipper_pwm_routes/Makefile",
    "tests/klipper_stepper_local/run_tests.py": "tests/klipper_stepper_local/Makefile",
}

OPTIONAL_MAKEFILES = {
    "linux_mcu": "pinned archive, arm-none-eabi-gcc and the integration klippy venv",
    "host_h723_command_join": "saved H723 image and dictionary",
    "klipper_board_startup": "saved autoconf header",
    "klipper_emu_runtime": "saved EMU autoconf header",
    "klipper_selector_runtime": "saved selector autoconf header",
    "klipper_tmc_feeder": "KLIPPER_SOURCE with installed driver",
    "klipper_tmc_uart": "KLIPPER_SOURCE with installed driver",
    "hh_harness": "Happy Hare harness: needs the extracted archive and a Python with greenlet+jinja2",
    "upstream_dossier": "Happy Hare dossier trace: needs the extracted archive and a Python with greenlet+jinja2",
}

KLIPPER_TEST_ENV = "KLIPPER_SOURCE"
OPENAMS_TEST_ENV = "OPENAMS_SOURCE"


# Exit code of the module loader when every test in a module was skipped with a
# reason (for example a missing optional tool). Zero tests without any skip is
# still a failure, so a module that silently loads nothing cannot pass.
SKIPPED_EXIT = 3
SKIP_MARKER = "CHECK-SKIPPED: "

# Python modules are deliberately loaded individually to isolate identical
# test basenames.
LOADER = (
    "import importlib.util,pathlib,sys,unittest; "
    "root=pathlib.Path(sys.argv[1]).resolve(); p=pathlib.Path(sys.argv[2]).resolve(); "
    "sys.path[:0]=[str(root),str(p.parent)]; "
    "name='_check_'+__import__('hashlib').sha1(str(p).encode()).hexdigest(); "
    "spec=importlib.util.spec_from_file_location(name,p); m=importlib.util.module_from_spec(spec); "
    "sys.modules[name]=m; spec.loader.exec_module(m); "
    "suite=unittest.defaultTestLoader.loadTestsFromModule(m); "
    "r=unittest.TextTestRunner(verbosity=2).run(suite); "
    "allskip=bool(r.skipped) and len(r.skipped)>=r.testsRun; "
    "reasons='; '.join(sorted({str(x[1]) for x in r.skipped})); "
    "print('" + SKIP_MARKER + "'+reasons) if allskip and r.wasSuccessful() else None; "
    "sys.exit(1 if not r.wasSuccessful() else (" + str(SKIPPED_EXIT) + " if allskip else "
    "(2 if r.testsRun==0 else 0)))"
)


def _skip_reason(stdout_path: Path) -> str:
    """The skip reason the module loader printed, or a generic one."""
    try:
        for line in stdout_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(SKIP_MARKER):
                return line[len(SKIP_MARKER):].strip() or "all tests skipped"
    except OSError:
        pass
    return "all tests skipped"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", action="store_true",
                        help="run the registered KLIPPER_SOURCE test subset")
    parser.add_argument("--openams", action="store_true",
                        help="run the registered OPENAMS_SOURCE test subset")
    parser.add_argument("--slow", action="store_true",
                        help="include source-dependent calibration and actuation fixtures")
    parser.add_argument("--release", action="store_true",
                        help="run the pinned release matrix")
    parser.add_argument("--all-targets", action="store_true",
                        help="with --release, build the release matrix for every "
                        "STM32 family instead of only the H723 (run before tagging)")
    args = parser.parse_args()

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    build_root = Path(os.environ.get("CHECK_BUILD_DIR", ROOT / "build/check")).resolve()
    run_dir = build_root / f"run-{stamp}-{os.getpid()}"
    logs = run_dir / "logs"
    logs.mkdir(parents=True, exist_ok=False)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    upstream = env.get(KLIPPER_TEST_ENV)
    # klipper_mmu/ is a directory inside the openams repository, so the parent
    # tree is the OpenAMS source unless the environment names another one.
    openams = env.get(OPENAMS_TEST_ENV) or str(ROOT.parent)
    summary: dict[str, object] = {
        "created_utc": stamp, "status": "running", "hardware_access": False,
        "run_dir": str(run_dir), "suites": [], "entrypoints": [], "omissions": [],
        "unclassified": [],
    }
    suites: list[dict[str, object]] = summary["suites"]  # type: ignore[assignment]
    entries: list[dict[str, object]] = summary["entrypoints"]  # type: ignore[assignment]
    omissions: list[dict[str, str]] = summary["omissions"]  # type: ignore[assignment]
    unclassified: list[str] = summary["unclassified"]  # type: ignore[assignment]
    all_ok = True
    serial = 0

    def command(name: str, argv: list[str], *, cwd: Path = ROOT,
                timeout: int = 180, extra_env: dict[str, str] | None = None,
                skip_code: int | None = None) -> tuple[bool, str]:
        nonlocal serial
        serial += 1
        result_id = f"{serial:03d}-{name}"
        record: dict[str, object] = {
            "id": result_id, "name": name, "status": "running", "argv": argv,
            "cwd": str(cwd), "timeout_seconds": timeout,
        }
        suites.append(record)
        proc_env = env.copy()
        if extra_env:
            proc_env.update(extra_env)
        out_path, err_path = logs / f"{result_id}.stdout", logs / f"{result_id}.stderr"
        try:
            with out_path.open("wb") as out, err_path.open("wb") as err:
                proc = subprocess.Popen(argv, cwd=cwd, env=proc_env, stdout=out,
                                         stderr=err, start_new_session=True)
                try:
                    code = proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    term_grace_expired = False
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        term_grace_expired = True
                    # The leader may exit on TERM while a child ignores it.
                    # Kill the process group regardless of the leader state.
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        cleanup_failed = True
                    else:
                        cleanup_failed = False
                    record["term_grace_expired"] = term_grace_expired
                    record["status"] = "cleanup_failed" if cleanup_failed else "timed_out"
                else:
                    record["returncode"] = code
                    if skip_code is not None and code == skip_code:
                        record["status"] = "skipped"
                    else:
                        record["status"] = "passed" if code == 0 else "failed"
        except OSError as exc:
            record["status"] = "unavailable"
            record["error"] = str(exc)
        record["stdout"], record["stderr"] = str(out_path), str(err_path)
        if record["status"] == "skipped":
            record["reason"] = _skip_reason(out_path)
            print(f"skipped: {name} ({record['reason']})")
        else:
            print(f"{record['status']}: {name}")
        return record["status"] in ("passed", "skipped"), result_id

    def inventory(path: str, gate: str, status: str, *, result: str | None = None,
                  reason: str | None = None) -> dict[str, object]:
        row: dict[str, object] = {"path": path, "gate": gate, "status": status}
        if result is not None:
            row["result_ref"] = result
        if reason:
            row["reason"] = reason
        entries.append(row)
        return row

    def run(path: str, argv: list[str], gate: str, *, timeout: int = 180,
            cwd: Path = ROOT, extra_env: dict[str, str] | None = None) -> bool:
        nonlocal all_ok
        if not (ROOT / path).is_file():
            row = inventory(path, gate, "unavailable", reason="entrypoint missing")
            omissions.append({"path": path, "status": "unavailable", "reason": "entrypoint missing"})
            all_ok = False
            return False
        passed, ref = command(Path(path).stem, argv, cwd=cwd, timeout=timeout,
                              extra_env=extra_env)
        inventory(path, gate, "passed" if passed else "failed", result=ref)
        all_ok &= passed
        return passed

    def omit(path: str, gate: str, reason: str) -> None:
        inventory(path, gate, "omitted", reason=reason)
        omissions.append({"path": path, "status": "omitted", "reason": reason})

    # Root native suite uses both output variables because TEST_BUILD_DIR is
    # intentionally overridable independently from BUILD_DIR.
    root_test_build = run_dir / "native-core"
    run("Makefile", ["make", "test", f"BUILD_DIR={root_test_build}",
                     f"TEST_BUILD_DIR={root_test_build / 'tests'}"],
        "default", timeout=900)
    # Material-model and causal firmware-loop simulator (sim/).
    run("sim/Makefile", ["make", "test", "test-loop", f"BUILD_DIR={run_dir / 'simulator'}"],
        "default", cwd=ROOT / "sim", timeout=300)

    for suite in DEFAULT_MAKEFILES:
        path = f"tests/{suite}/Makefile"
        if suite == "klipper_startup_composition":
            argv, cwd = ["make", "test", f"BUILD={run_dir / 'startup-composition'}"], ROOT / "tests" / suite
        else:
            argv, cwd = ["make", "-B", "-f", path, "test",
                         f"BUILD_DIR={run_dir / ('native-' + suite)}"], ROOT
        run(path, argv, "default", cwd=cwd, timeout=300)

    for path, (flag, result_name) in DEFAULT_RUNNERS.items():
        if args.upstream and path in UPSTREAM_RUNNERS:
            continue
        target = run_dir / result_name
        if flag is None:
            argv = [sys.executable, "-B", path]
        elif flag == "positional":
            argv = [sys.executable, "-B", path, str(target)]
        elif result_name == "pwm-binding-fake.json":
            argv = [sys.executable, "-B", path, "--results", str(target)]
        else:
            argv = [sys.executable, "-B", path, "--results", str(target)]
        run(path, argv, "default", timeout=300)
    f407_gate = "tests/klipper_diagnostic_startup/test_f407_gate.py"
    run(f407_gate, [sys.executable, "-B", f407_gate], "default", timeout=180)

    # Python modules are deliberately loaded individually to isolate identical
    # test basenames; zero discovered cases cause the helper process to fail.
    discovered_test_paths = {p.relative_to(ROOT).as_posix()
                             for p in (ROOT / "tests").rglob("test_*.py")}
    for rel in sorted(DEFAULT_MODULES & discovered_test_paths):
        p = ROOT / rel
        passed, ref = command("python-" + rel.removeprefix("tests/").replace("/", "-"),
                              [sys.executable, "-B", "-c", LOADER, str(ROOT), str(p)],
                              timeout=120, skip_code=SKIPPED_EXIT)
        record = suites[-1]
        if record["status"] == "skipped":
            inventory(rel, "default", "skipped", result=ref,
                      reason=str(record.get("reason", "")))
            continue
        inventory(rel, "default", "passed" if passed else "failed", result=ref)
        all_ok &= passed
    inventory("tests/test_math.py", "inventory-only", "not-a-suite",
              reason="legacy file contains no test cases")
    for rel in sorted(SAVED_MODULES & discovered_test_paths):
        omit(rel, "saved Klipper dictionary", "module includes a retained-build codec check")

    if args.upstream:
        if not upstream or not (Path(upstream) / "src/Kconfig").is_file():
            all_ok = False
            for path in sorted(UPSTREAM_MODULES):
                inventory(path, "KLIPPER_SOURCE", "unavailable", reason="valid source tree required")
            omissions.append({"path": "KLIPPER_SOURCE", "status": "unavailable",
                              "reason": "--upstream requires KLIPPER_SOURCE with src/Kconfig"})
        else:
            for rel in sorted(UPSTREAM_MODULES):
                p = ROOT / rel
                passed, ref = command("python-" + rel.removeprefix("tests/").replace("/", "-"),
                                      [sys.executable, "-B", "-c", LOADER, str(ROOT), str(p)],
                                      timeout=300, extra_env={"KLIPPER_SOURCE": upstream})
                inventory(rel, "KLIPPER_SOURCE", "passed" if passed else "failed", result=ref)
                all_ok &= passed
            for rel, label in UPSTREAM_RUNNERS.items():
                argv = [sys.executable, "-B", rel, "--klipper-source", upstream,
                        "--results", str(run_dir / label)]
                if not shutil.which("arm-none-eabi-gcc") and "boot_identity" in rel:
                    inventory(rel, "KLIPPER_SOURCE + arm-none-eabi-gcc", "unavailable",
                              reason="arm-none-eabi-gcc not installed")
                    omissions.append({"path": rel, "status": "unavailable",
                                      "reason": "arm-none-eabi-gcc not installed"})
                    all_ok = False
                    continue
                run(rel, argv, "KLIPPER_SOURCE", timeout=360)
            for rel, gate in UPSTREAM_OTHER_RUNNERS.items():
                if not (args.slow and rel == "tests/klipper_calibration_receiver/run_tests.py"):
                    omit(rel, gate, "outside the selected --upstream subset")
    else:
        for rel in sorted(UPSTREAM_MODULES):
            omit(rel, "KLIPPER_SOURCE", "pass --upstream with KLIPPER_SOURCE")
        for rel in sorted(UPSTREAM_RUNNERS):
            if rel in DEFAULT_RUNNERS:
                row = next(e for e in entries if e.get("path") == rel)
                row["additional_gate"] = "KLIPPER_SOURCE + arm-none-eabi-gcc"
                row["additional_status"] = "omitted"
                row["additional_reason"] = "pass --upstream for the CMSIS compile"
                omissions.append({"path": rel, "status": "omitted",
                                  "reason": "CMSIS compile requires --upstream"})
            else:
                omit(rel, "KLIPPER_SOURCE", "pass --upstream with KLIPPER_SOURCE")
        for rel, gate in UPSTREAM_OTHER_RUNNERS.items():
            if not (args.slow and rel == "tests/klipper_calibration_receiver/run_tests.py"):
                omit(rel, gate, "not part of the selected portable default")

    if args.openams:
        if not openams or not (Path(openams) / "src").is_dir():
            all_ok = False
            omissions.append({"path": "OPENAMS_SOURCE", "status": "unavailable",
                              "reason": "no OpenAMS source tree with src/ (looked in %s)"
                                        % (openams or "OPENAMS_SOURCE")})
            for rel in sorted(OPENAMS_MODULES):
                inventory(rel, "OPENAMS_SOURCE", "unavailable", reason="valid source tree required")
        else:
            for rel in sorted(OPENAMS_MODULES):
                p = ROOT / rel
                passed, ref = command("python-" + rel.removeprefix("tests/").replace("/", "-"),
                                      [sys.executable, "-B", "-c", LOADER, str(ROOT), str(p)],
                                      timeout=180, extra_env={"OPENAMS_SOURCE": openams})
                inventory(rel, "OPENAMS_SOURCE", "passed" if passed else "failed", result=ref)
                all_ok &= passed
    else:
        for rel in sorted(OPENAMS_MODULES):
            omit(rel, "OPENAMS_SOURCE", "pass --openams with OPENAMS_SOURCE")

    registered_modules = DEFAULT_MODULES | UPSTREAM_MODULES | OPENAMS_MODULES | SAVED_MODULES | {
        "tests/test_math.py", f407_gate}
    for rel in sorted(discovered_test_paths - registered_modules):
        unclassified.append(rel)
    if unclassified:
        all_ok = False
        for rel in unclassified:
            inventory(rel, "unclassified", "failed", reason="new test module lacks explicit dispatch")

    # Every discovered runner and Makefile must match an explicit registry.
    discovered_runners = {p.relative_to(ROOT).as_posix()
                          for p in (ROOT / "tests").rglob("run*.py")}
    covered_runners = set(DEFAULT_RUNNERS) | set(UPSTREAM_RUNNERS) | set(UPSTREAM_OTHER_RUNNERS) | set(SAVED_RUNNERS)
    covered_runners |= set(MAKEFILE_RUNNERS)
    for rel in sorted(discovered_runners - covered_runners):
        unclassified.append(rel)
        inventory(rel, "unclassified", "failed", reason="new runner lacks explicit dispatch or gate")
        all_ok = False
    for rel in sorted(covered_runners - discovered_runners):
        if rel in UPSTREAM_RUNNERS:
            continue
        # New main may add these entries after this fixture-repair branch was cut.
        gate = "KLIPPER_SOURCE" if rel in UPSTREAM_RUNNERS else "saved image / source gate"
        inventory(rel, gate, "not-present", reason="entrypoint is absent from this checkout")
    for rel in sorted(discovered_runners & set(SAVED_RUNNERS)):
        if not (args.release and rel == "tests/release_matrix/run.py"):
            omit(rel, SAVED_RUNNERS[rel], "requires saved artifact; no default image inference")
    for rel in sorted(discovered_runners & set(MAKEFILE_RUNNERS)):
        inventory(rel, "default", "covered-by", result=next(
            (e.get("result_ref") for e in entries
             if e.get("path") == MAKEFILE_RUNNERS[rel]), None),
            reason="the Makefile test target invokes this runner")

    discovered_makefiles = {p.relative_to(ROOT).as_posix()
                            for p in (ROOT / "tests").glob("*/Makefile")}
    default_mfs = {f"tests/{name}/Makefile" for name in DEFAULT_MAKEFILES}
    optional_mfs = {f"tests/{name}/Makefile": gate for name, gate in OPTIONAL_MAKEFILES.items()}
    for rel in sorted(discovered_makefiles - default_mfs - set(optional_mfs)):
        unclassified.append(rel)
        inventory(rel, "unclassified", "failed", reason="new Makefile lacks explicit dispatch or gate")
        all_ok = False
    for rel, gate in sorted(optional_mfs.items()):
        if rel in discovered_makefiles:
            omit(rel, gate, "outside the portable default gate")

    # Source-dependent tests/runners not selected by their opt-in flag remain
    # visible as individual omissions rather than inferred passing results.
    if args.slow:
        if not upstream:
            all_ok = False
            omissions.append({"path": "--slow", "status": "unavailable",
                              "reason": "requires KLIPPER_SOURCE"})
        else:
            run("tests/klipper_calibration_receiver/run_tests.py",
                [sys.executable, "-B", "tests/klipper_calibration_receiver/run_tests.py",
                 "--klipper", upstream], "KLIPPER_SOURCE", timeout=300)
            run("tests/klipper_diagnostic_startup/run_tests.py",
                [sys.executable, "-B", "tests/klipper_diagnostic_startup/run_tests.py",
                 "--mode", "actuation", "--results", str(run_dir / "diagnostic-actuation")],
                "KLIPPER_SOURCE + actuation fixture", timeout=300)
    else:
        omit("--slow", "KLIPPER_SOURCE", "pass --slow with KLIPPER_SOURCE")

    if args.release:
        # This package's build root, like every other build input here.
        archive = (Path(os.environ.get("KLIPPER_MMU_BUILD_ROOT") or ROOT / "build")
                   / "klipper-ce7002bedf37e938bb483572949f3703ac6476cb.tar.gz")
        if not archive.is_file():
            all_ok = False
            omissions.append({"path": "--release", "status": "unavailable",
                              "reason": f"required archive missing: {archive}"})
        else:
            # The v1 product is the BoxTurtle H723 image: merges build the H723
            # rows; --all-targets builds every STM32 family (before a release tag).
            run("tests/release_matrix/run.py",
                [sys.executable, "-B", "tests/release_matrix/run.py",
                 "--build-dir", str(run_dir / "release-matrix")]
                + ([] if args.all_targets else ["--targets", "h723"]),
                "pinned release archive", timeout=1800)
        omit("tests/stm32_target_matrix/run.py", "pinned release archive",
             "not included in --release")
    else:
        omit("--release", "pinned release archive",
             "pass --release to run the release matrix; target matrix is separate")

    summary["status"] = "passed" if all_ok else "failed"
    summary["scope"] = "Registered portable checks plus explicitly selected external gates; not full release qualification."
    summary_path = run_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"{summary['status']}: {summary_path}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
