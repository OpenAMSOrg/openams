#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Happy Hare device-behaviour dossier: trace a device through the pinned,
extracted Happy Hare test harness (test/hh/) and reduce the recording to one
committed JSON "dossier" plus a human Markdown page per device.

Why: Happy Hare boots its own real code from its own real shipped config
templates over a 1-D filament model, so running it is cheaper and more
accurate than hand-reading its Python. See
the upstream dossier plan (sections 2 D1/D2 and 3) for the design and
the concept-mapping notes section 4.1/7 for the EMU known
answer and the concept-mapping table reproduced below.

Subcommands:
  trace DEVICE [--profile NAME | --syms K=V ...] [--out PATH]
      Boot DEVICE in the harness, run PRELOAD/SELECT/LOAD/UNLOAD/EJECT on
      gate 0, and write a dossier JSON. Refuses (exit 1) if the
      self-consistency check fails, printing the offending move.
  md DOSSIER.json
      Print the device's Markdown page to stdout.
  check [DEVICE ...]
      Re-trace every committed docs/devices/*.json (or the DEVICEs named)
      and diff both the regenerated JSON and the regenerated Markdown
      against what is committed. Exit 1 on any difference.

Needs the pinned Happy Hare archive extracted and an interpreter with
greenlet+jinja2 (HH_PYTHON, default the shared integration-env venv beside
the *main* checkout -- see tests/hh_harness/README.md, whose HH_ROOT/
HH_PYTHON defaults and extraction this tool reuses). `md` and the JSON/MD
diff in `check` need neither and run under plain system Python.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOL = 0.01  # mm; float noise from the harness is ~1e-6, real bugs are >=1mm.

# --------------------------------------------------------------------------
# Paths, derived the way tests/hh_harness/Makefile does (git common dir), so
# this works the same from the main checkout and from any worktree. Never
# hard-code a personal path.
# --------------------------------------------------------------------------

def _main_root() -> Path:
    out = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "--git-common-dir"], text=True).strip()
    p = Path(out).resolve()
    return p.parent if p.name == ".git" else p


MAIN_ROOT = _main_root()
SHARED_BUILD = MAIN_ROOT.parent / "build"
HH_ROOT = Path(os.environ.get("HH_ROOT", str(SHARED_BUILD / "upstream" / "happy-hare-c54c736")))
HH_PYTHON = Path(os.environ.get(
    "HH_PYTHON", str(SHARED_BUILD / "integration-env" / "klippy" / "bin" / "python")))
HH_REV = HH_ROOT.name.rsplit("-", 1)[-1] if "-" in HH_ROOT.name else HH_ROOT.name

# Symbols measured this session (the upstream dossier plan section 3) that
# render, boot and cycle PRELOAD..EJECT cleanly. `profile` names a shipped
# harness profile (test/hh/profiles.py); `sym` + `extra` build a bare
# MMU_TYPE_* profile the way build/b2/oracle-inputs/type_sweep.py does.
_MMB2 = {"BOARD_TYPE_MMB_2_0": True, "MMU_HAS_SENSOR_SHARED_EXIT": True}
# ERCF is a selector machine: MMU_FAMILY_ERCF is both the parent of the MMU_TYPE_ERCF_*
# version choice and the symbol that implies LINEAR_SERVO_SELECTOR (Kconfig.ercf:1-10).
_ERCF = {**_MMB2, "MMU_FAMILY_ERCF": True}
DEVICES = {
    # bare MMU_TYPE_* only
    "boxturtle": {"profile": "boxturtle"},
    "emu": {"profile": "emu"},
    "kms": {"sym": "MMU_TYPE_KMS_1_0"},
    "nightowl": {"sym": "MMU_TYPE_NIGHT_OWL_1_0"},
    "tradrack": {"profile": "tradrack"},
    # QIDI Box's extruder-entry switch lives on the printer's separate THR
    # toolhead MCU (installer/boards/custom/Kconfig.qidi_box: PIN_EXTRUDER_SENSOR
    # default "!THR:PA1"), which a bare sym render cannot resolve -- there is no
    # [mcu THR] section anywhere in HH's own installer templates. The shipped
    # "qidi" console profile (test/hh/profiles.py) carries a printer_stub with
    # exactly that stub MCU (test/hh/bootstrap.py:35-38), so use it by name; the
    # dossier is still keyed "qidi_box" to match the Kconfig type name.
    "qidi_box": {"profile": "qidi"},
    # shipped harness profiles
    "3ms": {"profile": "3ms"},
    "chameleon": {"profile": "chameleon"},
    "mmx": {"profile": "mmx"},
    "ercf_vvd": {"profile": "ercf_vvd"},
    "encoder": {"profile": "encoder"},
    # bare + BOARD_TYPE_MMB_2_0 + MMU_HAS_SENSOR_SHARED_EXIT
    "3d_chameleon": {"sym": "MMU_TYPE_3D_CHAMELEON_1_0", "extra": _MMB2},
    "angry_beaver": {"sym": "MMU_TYPE_ANGRY_BEAVER_1_0", "extra": _MMB2},
    # MMU_FAMILY_ERCF carries the ERCF selector: it implies LINEAR_SERVO_SELECTOR
    # (Kconfig.ercf:7) and its MMU_TYPE_ERCF_* choice is nested under it, so a bare
    # MMU_TYPE_*_ERCF_* profile without the family renders the "Safety" VirtualSelector
    # and Other/1.0 defaults instead of ERCF's LinearServoSelector.
    "ercf_1_1": {"sym": "MMU_TYPE_ERCF_1_1", "extra": _ERCF},
    "ercf_2_0": {"sym": "MMU_TYPE_ERCF_2_0", "extra": _ERCF},
    "ercf_3_0": {"sym": "MMU_TYPE_ERCF_3_0", "extra": _ERCF},
    "htlf": {"sym": "MMU_TYPE_HTLF_1_0", "extra": _MMB2},
    "mmx6": {"sym": "MMU_TYPE_MMX6_1_0", "extra": _MMB2},
    # Selector machines whose MMU_TYPE_* config itself selects the selector
    # class -- SERVO_SELECTOR for Low Rider and Pico MMU, INDEXED_SELECTOR for
    # ViViD -- so no family symbol is needed the way ERCF's nested version
    # choice needs MMU_FAMILY_ERCF. They still take the MMB_2_0/shared-exit
    # extras every bare (profile-less) type here does.
    "low_rider": {"sym": "MMU_TYPE_LOW_RIDER_1_0", "extra": _MMB2},
    "pico_mmu": {"sym": "MMU_TYPE_PICO_MMU_1_0", "extra": _MMB2},
    # ViViD is not on MMB_2_0 and not on MMB_2_0's shared exit: its own board
    # (BOARD_TYPE_VVD_1_0) is what supplies the hand-written per-gate NFC
    # reader sections the type's MMU_HAS_PER_GATE_NFC_READERS demands, and
    # OPTION_VVD_BUFFER adds the shared exit switch its gate homing homes to
    # (test/hh/profiles.py ERCF_VVD unit1 sets exactly these two). The board's
    # PARAM_MISC_HARDWARE hard-codes its reader sections as `unit1_nfc01`/
    # `unit1_nfc23` (installer/boards/custom/Kconfig.vvd:119-128), because the
    # shipped machine puts the ViViD on unit1; a single-unit trace boots it as
    # unit0, whose derived names would be `unit0_nfc*`, so the reader syms are
    # pointed at the sections the board actually emits.
    "vivid": {"sym": "MMU_TYPE_VVD_1_0",
              "extra": {"BOARD_TYPE_VVD_1_0": True, "OPTION_VVD_BUFFER": True,
                        "PARAM_NFC_READER_0": "unit1_nfc01",
                        "PARAM_NFC_READER_1": "unit1_nfc01",
                        "PARAM_NFC_READER_2": "unit1_nfc23",
                        "PARAM_NFC_READER_3": "unit1_nfc23"}},
    "quattrobox_1_0": {"sym": "MMU_TYPE_QUATTRO_BOX_1_0", "extra": _MMB2},
    "quattrobox_1_1": {"sym": "MMU_TYPE_QUATTRO_BOX_1_1", "extra": _MMB2},
    "quattrobox_2_0": {"sym": "MMU_TYPE_QUATTRO_BOX_2_0", "extra": _MMB2},
}
OPS = [("preload", "MMU_PRELOAD GATE=0"), ("select", "MMU_SELECT GATE=0"),
       ("load", "MMU_LOAD"), ("unload", "MMU_UNLOAD"), ("eject", "MMU_EJECT")]

# The concept-mapping table (the concept-mapping notes section 7):
# upstream concept | AFC key | Happy Hare key | klipper-mmu field/Kconfig
# symbol | mapped/decision/gap/host-only/not-applicable. One pipe-delimited
# row per line (fixed reference data, not per-device) so it stays short;
# rendered as-is on every device page.
_MAPPING_TSV = """
Lane / gate | [AFC_stepper laneN], unit: X:N | gate index, unit0_gear_<i> | lane_count, per-lane feeder driver | mapped
Prep / entry switch | prep: (TRG) | mmu_entry_<i> | has_bay_sensors, KLIPPER_MMU_PINS_BAY | mapped
Load / exit switch | load: (LOAD) | mmu_exit_<i> | has_load_sensors, KLIPPER_MMU_PINS_LOAD | mapped
Hub / shared exit | [AFC_hub] switch_pin | mmu_shared_exit | has_hub_sensor, PINS_HUB | mapped
Virtual hub | switch_pin: virtual + use_dist_hub | no shared exit + gate_homing_endstop: mmu_exit | load_is_virtual_hub | mapped
Bowden length | afc_bowden_length / dist_hub per lane | mmu_calibration_bowden_lengths (per gate if variable) | fps_calibrated_steps (one per unit) | gap: per-lane
Toolhead switch | pin_tool_start / pin_tool_end, tool_stn | [mmu_toolhead] toolhead_switch_pin, toolhead_sensor_to_nozzle | has_toolhead_sensor, PINS_TOOLHEAD; nozzle distances host-owned | mapped (boundary), host (distances)
Switched buffer | [AFC_buffer] advance_pin/trailing_pin + multipliers | [mmu_buffer] compression_pin/tension_pin | has_proportional_buffer=false, PINS_BUFFER_A/B switched | mapped for follow; gap: ramming arrival
Proportional buffer (PSF) | AFCFPSBuffer adc_pin, neutral/low/high, P+I | analog_pin, max_compression/tension, threshold; EKF/two-level | DiffA/DiffB pair only (km_diff_adc_config_t) | gap: single-ended ADC frontend
FPS (OpenAMS) | AFC_FPS.cfg | n/a | DiffA/DiffB + fps_arrival | mapped
Espooler / assist | AFC_assist.py, espooler.assist(pwm) | [mmu_espooler] burst/rewind | has_powered_rewind (KLIPPER_MMU_POWERED_REWIND), PWM duty, current regulator | mapped (on by default for BoxTurtle and Claymore); EMU none
Encoder | n/a in EMU | [mmu_encoder], encoder homing | has_encoder (KLIPPER_MMU_ENCODER), PINS_ENCODER_A, optional B: clog/stall and progress checks, CALIBRATE to a toolhead switch | mapped (on by default for ERCF); encoder homing not a mode
Selector | HTLF lobes, ViViD/Claymore selector switches | SELECTOR_REGISTRY classes | has_selector, KLIPPER_MMU_SELECTOR_KIND_LINEAR/ROTARY/SERVO/INDEXED; homes before first use; KLIPPER_MMU_CALIBRATE_SELECTOR | mapped
Grip / servo | n/a EMU | servo_* | KLIPPER_MMU_PINS_SERVO (Klipper software PWM, any GPIO): LINEAR grip angles, SERVO per-gate angles | mapped
Speeds | long/short/hub/calibration modes | load/unload/short/homing/virtual-sensor speeds | feed/rewind velocity, accel/decel, cruise/crawl | mapped in shape; per-device numbers vary
Currents | run_current, print_current (follow) | run/hold, sync_gear_current % | run/hold only | gap: follow current
Homing budget / attempts | homing_overshoot, homing_delta, attempt loops | gate_homing_max, *_attempts, bowden_homing_max | timeouts in ms, KM_LOAD_ATTEMPTS 3, arrival min/max steps | mapped as deadlines; distance vs time is a translation choice
Parking / clearance | hub_clear_move_dis, extruder_clear_dis, park_dist | gate_parking_distance, gate_final_eject_distance | unload_hub_clear_steps; EJECT to BAY-clear | partly mapped; decision
Tip forming, cut, purge, park | AFC macros, AFC_form_tip.py | _MMU_FORM_TIP, _MMU_PARK, purge | host/OpenAMS macros | host-only
Runout / endless spool | handle_prep_runout, _perform_infinite_runout | mmu_endless_spool.py | host reducer + firmware runout evidence | host-only (with firmware evidence)
LEDs, buttons, fans, environment, heater, NFC, Spoolman | many | many | out of scope for firmware | not-applicable
"""
MAPPING = [tuple(part.strip() for part in row.split("|"))
           for row in _MAPPING_TSV.strip("\n").splitlines()]

# The 13 concept names Brief B's afc_facts compare joins on (single source of
# truth, sonnet_oracle_amendments.txt item 1). A dossier's "decisions" entries
# use these names so a `differ` row there is never merged silently.
DECISION_CONCEPTS = (
    "lane_count", "entry_sensor", "exit_sensor", "hub_sensor", "sensor_polarity",
    "bowden", "preload_park", "unload_park", "eject_extra", "long_speed",
    "short_speed", "buffer_type", "arrival",
)


# --------------------------------------------------------------------------
# Config-text parsing (pure Python, no HH needed): Happy Hare's rendered
# `.cfg` text is Klipper's own format, "key : value  # comment" inside
# "[section name]" blocks.
# --------------------------------------------------------------------------

def parse_sections(text: str) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1]
            sections[current] = {}
            continue
        if current is None or ":" not in line:
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        if not key or key.startswith("#"):
            continue
        sections[current][key] = rest.split("#", 1)[0].strip()
    return sections


HARDWARE_PATTERNS = (
    re.compile(r"^mmu_unit\s"), re.compile(r"^mmu_sensors\s"),
    re.compile(r"^mmu_buffer\s"), re.compile(r"^mmu_toolhead\s"),
    re.compile(r"^mmu_stepper\s\S+_gear$"),
)


def pick_hardware(sections: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    return {name: body for name, body in sections.items()
            if any(pat.match(name) for pat in HARDWARE_PATTERNS)}


# A switched (tension/compression-switch) buffer's 'filament_compression'/
# 'filament_tension' is a TRAVEL threshold (contact then a relative
# distance), not a fixed tip coordinate: test/hh/filament.py only gives it a
# static absolute number (configure_buffers()'s one-time 'compression' calc)
# for the console's path legend, and homing against it can and does converge
# somewhere else. Only a proportional (PSF, 'analog_pin' in the rendered
# [mmu_buffer]) sensor's position is a real absolute target.
DYNAMIC_BUFFER_SENSORS = {"filament_compression", "filament_tension"}


def has_proportional_buffer(hardware: dict) -> bool:
    return any("analog_pin" in body for name, body in hardware.items()
               if name.startswith("mmu_buffer"))


def position_is_fixed(endstop: str | None, hardware: dict) -> bool:
    bare = (endstop or "").split(":")[-1]
    return bare not in DYNAMIC_BUFFER_SENSORS or has_proportional_buffer(hardware)


# HH can synthesize one endstop out of two sensors: MmuCompoundEndstop
# (extras/mmu/mmu_filament_movement.py _build_gate_nfc_compound) is the
# first-wins union of a gate's switch and that gate's NFC reader, registered
# on the gear rail as e.g. 'preload_compound'. test/hh/klippy_root/extras/
# homing.py _expand flattens it and picks whichever child trips first, so the
# compound has no single entry in the static layout -- the merge is a harness
# construct, not a third sensor.
COMPOUND_SUFFIX = "_compound"


def compound_legs(layout: dict[str, float], parameters: dict) -> list[float]:
    """Static positions a gate/NFC compound endstop may legitimately halt at:
    the unit's gate switch (`gate_homing_endstop`, the compound's first leg)
    and the gate's `mmu_nfc` entry (its NFC reader leg)."""
    names = [parameters.get("gate_homing_endstop")]
    names += [name for name in layout if "mmu_nfc" in name]
    return [p for p in (resolve_layout(layout, n) for n in names) if p is not None]


def resolve_layout(layout: dict[str, float], name: str | None):
    """Mirrors test/hh/filament.py FilamentPath.position()'s bare/head
    stripping, for STATIC layout entries only (not the dynamic PSF-derived
    'filament_compression'/'filament_tension', which the harness worker
    already resolved live as each move's endstop_pos)."""
    if name is None:
        return None
    bare = name.split(":")[-1]
    if bare in layout:
        return layout[bare]
    head = bare.rsplit("_", 1)[0]
    return layout.get(head)


# --------------------------------------------------------------------------
# The harness worker: runs ONLY under HH_PYTHON, inside the extracted HH
# root (cwd). Imports test.hh lazily so plain system Python never needs them.
# --------------------------------------------------------------------------

def _steps_per_mm(rendered_hardware_text: str) -> float:
    """Lifted from tests/hh_harness/hh_probe.py:steps_per_mm(), generalised
    to take the already-rendered [mmu_stepper *_gear] text for any profile
    instead of hard-coding 'boxturtle'. Klipper's stepper steps/mm is
    (full_steps_per_rotation * microsteps * gear_ratio) / rotation_distance;
    omitting gear_ratio is the 5x bug the harness review hit."""
    lines = rendered_hardware_text.splitlines()
    start = next(i for i, l in enumerate(lines)
                 if re.match(r"^\[mmu_stepper \S+_gear\]$", l.strip()))
    values: dict[str, str] = {}
    for line in lines[start + 1:]:
        if line.strip().startswith("["):
            break
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        values[key.strip()] = rest.split("#", 1)[0].strip()
    rotation_distance = float(values["rotation_distance"])
    microsteps = float(values["microsteps"])
    full_steps = float(values.get("full_steps_per_rotation", 200))
    num, _, den = values.get("gear_ratio", "1:1").partition(":")
    gear_ratio = float(num) / float(den)
    return (full_steps * microsteps * gear_ratio) / rotation_distance


def _harness_worker(payload: dict) -> dict:
    """Body of `trace`, run inside HH_ROOT under HH_PYTHON (see run_trace()
    below, which subprocesses into this same function). Boots DEVICE, runs
    PRELOAD/SELECT/LOAD/UNLOAD/EJECT on gate 0, records every move_filament
    call the way build/b2/oracle-inputs/type_sweep.py's record_moves/run do
    (lifted, not redesigned), and returns the raw trace."""
    sys.path.insert(0, os.getcwd())
    from test.hh import bootstrap, profiles  # noqa: PLC0415

    name = payload["profile"]
    if "syms" in payload:
        prof = profiles.Profile(name, syms=payload["syms"], description=payload.get("device", name))
    else:
        prof = profiles.get(name)

    hh = bootstrap.session(prof)
    try:
        hh.boot()
        boot_errors = [str(e) for e in hh.errors]
        if boot_errors:
            return {"ok": False, "boot_errors": boot_errors}
        hh.calibrate()
        for u in range(len(hh.mmu.mmu_machine.units)):
            hh.run_gcode("MMU_HOME UNIT=%d" % u)
        hh.heat_extruder(220)
        # test/hh/filament.py's FilamentPath.configure_buffers() only models the
        # 'filament_tension' landmark for a two-switch buffer (it requires BOTH a
        # compression_sensor and a tension_sensor -- filament.py:168-172) and
        # DEFAULT_LAYOUT has no plain entry for it either (filament.py:68-99), so a
        # TENSION-ONLY switched buffer (MMU_HAS_SENSOR_BUFFER_TENSION selected,
        # ..._COMPRESSION unselected -- e.g. installer/mmu_types/Kconfig.qidi_box)
        # leaves 'filament_tension' unresolvable and self-consistency refuses the
        # dossier. Supply the same reference point the two-switch case uses for that
        # landmark (filament.py:185,264: the buffer's zero-travel position is its
        # unit's 'extruder_entry'), computed the same way FilamentPath merges its
        # layout (DEFAULT_LAYOUT then the profile's filament_layout override).
        extra_layout = {}
        for unit in hh.mmu.mmu_machine.units:
            buffer = getattr(unit, "buffer", None)
            if (buffer is not None
                    and getattr(buffer, "buffer_spring_state", "none") == "tension"
                    and getattr(buffer, "tension_sensor", None) is not None
                    and getattr(buffer, "compression_sensor", None) is None):
                from test.hh.filament import DEFAULT_LAYOUT  # noqa: PLC0415
                merged = dict(DEFAULT_LAYOUT)
                merged.update(getattr(prof, "filament_layout", None) or {})
                extra_layout["filament_tension"] = merged.get("extruder_entry")
                break
        fil = hh.filament(layout=extra_layout)
        hh.place_filament(0, position=-40.0)

        mmu = hh.mmu
        orig_move = mmu.move_filament
        resolve_speed = mmu._resolve_filament_move_speed

        def wrapped(trace_str, dist, speed=None, accel=None, motor="gear",
                    homing_move=0, endstop_name="default", **kw):
            g = mmu.gate_selected
            tip_before = fil.tip[g] if g is not None and g >= 0 else None
            r = orig_move(trace_str, dist, speed=speed, accel=accel, motor=motor,
                          homing_move=homing_move, endstop_name=endstop_name, **kw)
            tip_after = fil.tip[g] if g is not None and g >= 0 else None
            # Read AFTER the move: a proportional buffer's virtual sensor
            # (e.g. filament_compression) is dynamic and only reflects the
            # settled geometry once the move (and any buffer state it
            # established) has completed -- reading it beforehand can return
            # a stale DEFAULT_LAYOUT fallback instead of where the homing
            # move actually converged.
            endstop_pos = fil.position(endstop_name) if homing_move else None
            try:
                s, a = resolve_speed(dist, motor, homing_move, speed, accel)
            except Exception:  # noqa: BLE001
                s, a = None, None
            moves.append({
                "label": trace_str, "dist": dist, "motor": motor, "homing": homing_move,
                "endstop": endstop_name if homing_move else None, "endstop_pos": endstop_pos,
                "speed": s, "accel": a, "actual": r[0], "homed": r[1],
                "tip_before": tip_before, "tip_after": tip_after,
            })
            return r

        moves: list[dict] = []
        mmu.move_filament = wrapped

        operations = {}
        for label, script in OPS:
            before = len(moves)
            hh.run_gcode(script)
            operations[label] = {
                "moves": moves[before:],
                "final_tip": fil.tip[0],
                "filament_pos": mmu.filament_pos,
                "gate_status": mmu.gate_status[0],
                "errors": [str(e) for e in hh.errors],
            }

        from test.hh import cfg  # noqa: PLC0415
        rendered = cfg.render(prof)
        sections = {}
        for text in rendered.values():
            sections.update(parse_sections(text))
        hw = pick_hardware(sections)
        unit_name = next((n for n in hw if n.startswith("mmu_unit ")), None)
        params = {}
        params.update(sections.get("mmu_parameters", {}))
        for sec_name, body in sections.items():
            if sec_name.startswith("mmu_unit_parameters "):
                params.update(body)
        unit = hw.get(unit_name, {}) if unit_name else {}
        selector_type = unit.get("selector_type") or next(
            (b.get("selector_type") for b in hw.values() if "selector_type" in b), None)
        return {
            "ok": True,
            "syms": dict(prof.syms),
            "layout": {k: v for k, v in fil.layout.items() if isinstance(v, (int, float))},
            "hardware": hw,
            "parameters": params,
            "device": {
                "vendor": unit.get("vendor"),
                "gates": int(unit["num_gates"]) if unit.get("num_gates") else None,
                "selector_type": selector_type,
                "unit_profile": {
                    k: unit.get(k) == "1" for k in
                    ("variable_bowden_lengths", "variable_rotation_distances",
                     "require_bowden_move", "filament_always_gripped")
                    if k in unit
                },
            },
            "steps_per_mm": _steps_per_mm(rendered.get("config/base/mmu_hardware.cfg", "")),
            "operations": operations,
        }
    finally:
        hh.close()


# --------------------------------------------------------------------------
# Outer process: plain system Python. Extracts the archive, subprocesses
# into _harness_worker via HH_PYTHON, reduces the raw trace into the
# committed dossier shape, and runs the self-consistency check.
# --------------------------------------------------------------------------

def parse_syms_args(kv_list: list[str]) -> dict:
    syms: dict = {}
    for kv in kv_list:
        k, _, v = kv.partition("=")
        if v.lower() in ("true", "y", "yes"):
            v = True
        elif v.lower() in ("false", "n", "no"):
            v = False
        syms[k] = v
    return syms


def device_spec(device: str, profile: str | None = None, syms: dict | None = None) -> dict:
    """Build the {"device", "profile", "syms"?} payload run_trace() sends to
    the harness worker. An explicit --profile/--syms wins over the DEVICES
    table; a table entry with "sym" builds a bare MMU_TYPE_* profile the way
    build/b2/oracle-inputs/type_sweep.py does, else it names a shipped
    test/hh/profiles.py profile directly."""
    if profile:
        return {"device": device, "profile": profile}
    if syms:
        return {"device": device, "profile": device, "syms": syms}
    table = DEVICES.get(device)
    if table is None:
        raise SystemExit(f"unknown device {device!r}; pass --profile or --syms, or add it to "
                          f"DEVICES (known: {', '.join(sorted(DEVICES))})")
    if "sym" in table:
        merged = {table["sym"]: True}
        merged.update(table.get("extra", {}))
        return {"device": device, "profile": device, "syms": merged}
    return {"device": device, "profile": table["profile"]}


def ensure_extracted() -> None:
    if HH_ROOT.is_dir():
        return
    hh_harness_dir = ROOT / "tests" / "hh_harness"
    subprocess.run(["make", "-C", str(hh_harness_dir), "extract"], check=True, timeout=120)


def run_trace(spec: dict) -> dict:
    if not HH_PYTHON.is_file():
        raise SystemExit(f"HH_PYTHON not found at {HH_PYTHON}; needs the integration-env "
                          "venv with greenlet+jinja2 (see tests/hh_harness/README.md)")
    ensure_extracted()
    code = ("import json,sys,importlib.util;"
            "p=importlib.util.spec_from_file_location('ud', sys.argv[1]);"
            "m=importlib.util.module_from_spec(p); p.loader.exec_module(m);"
            "print(json.dumps(m._harness_worker(json.loads(sys.argv[2]))))")
    proc = subprocess.run([str(HH_PYTHON), "-c", code, str(Path(__file__).resolve()),
                           json.dumps(spec)],
                          cwd=str(HH_ROOT), capture_output=True, text=True, timeout=90)
    if proc.returncode != 0:
        raise SystemExit(f"harness worker failed for {spec['device']}:\n{proc.stderr[-4000:]}")
    raw = json.loads(proc.stdout.strip().splitlines()[-1])
    if not raw.get("ok"):
        raise SystemExit(f"{spec['device']} failed to boot: {raw.get('boot_errors')}")
    return raw


def reduce_dossier(spec: dict, raw: dict, decisions: list[dict] | None = None) -> dict:
    layout = raw["layout"]
    hardware = raw["hardware"]
    operations = {}
    for op_name, op in raw["operations"].items():
        moves = op["moves"]
        last_homing = next((m for m in reversed(moves)
                            if m["homing"] and position_is_fixed(m["endstop"], hardware)), None)
        tip = op["final_tip"]
        if last_homing is not None and last_homing["endstop_pos"] is not None:
            end = {"tip": tip, "sensor": last_homing["endstop"],
                   "offset": tip - last_homing["endstop_pos"]}
        else:
            end = {"tip": tip, "sensor": None, "offset": None}
        end["filament_pos"] = op["filament_pos"]
        end["gate_status"] = op["gate_status"]
        operations[op_name] = {"moves": moves, "end": end, "errors": op["errors"]}
    dossier = {
        "schema": 1,
        "source": {"happy_hare": HH_REV, "profile": spec["profile"], "syms": raw["syms"],
                   "layout": layout},
        "device": raw["device"],
        "hardware": raw["hardware"],
        "parameters": raw["parameters"],
        "steps_per_mm": raw["steps_per_mm"],
        "operations": operations,
        "decisions": decisions or [],
    }
    return dossier


def check_self_consistency(dossier: dict) -> list[str]:
    """Q2 item 2 (the upstream dossier plan): every homed move's actual
    travel must match its recorded endpoints, and a homing move's endstop
    must resolve to a real position. Returns violation strings; empty means
    the dossier is internally consistent (not that it is *correct* -- that
    is `check`'s job, by diffing against a committed, reviewed dossier).

    Two harness constructs are accepted on their own terms, because both are
    real HH behaviour rather than a recording error:
      * a `*_compound` endstop halts at one of its legs (see compound_legs),
        so the tip is checked against those positions instead of a single
        layout name;
      * a homing move that made no travel is already satisfied -- the
        filament sits where the previous park left it, which for a machine
        that parks FORWARD of its switch (ViViD's PARAM_GATE_PARKING_DISTANCE
        default is +10, unlike Low Rider/Pico MMU's -25) is legitimately
        beyond the sensor, so the tip is not asserted to equal it."""
    violations = []
    layout = dossier.get("source", {}).get("layout", {})
    hardware = dossier.get("hardware", {})
    parameters = dossier.get("parameters", {})
    for op_name, op in dossier.get("operations", {}).items():
        last_tip = None
        for i, m in enumerate(op.get("moves", [])):
            label = f"{op_name}[{i}] {m.get('label')}"
            tb, ta, actual = m.get("tip_before"), m.get("tip_after"), m.get("actual")
            if tb is not None and ta is not None and actual is not None:
                if abs((ta - tb) - actual) > TOL:
                    violations.append(f"{label}: tip delta {ta - tb:.4f} != actual {actual:.4f}")
            if m.get("homing"):
                endstop, pos = m.get("endstop"), m.get("endstop_pos")
                if pos is None and resolve_layout(layout, endstop) is None:
                    legs = (compound_legs(layout, parameters)
                            if str(endstop).endswith(COMPOUND_SUFFIX) else [])
                    if not any(ta is not None and abs(ta - leg) <= TOL for leg in legs):
                        violations.append(f"{label}: homing endstop {endstop!r} not resolvable "
                                          "in the recorded layout")
                elif (position_is_fixed(endstop, hardware) and m.get("homed") and pos is not None
                      and ta is not None and abs(ta - pos) > TOL
                      and (actual is None or abs(actual) > TOL)):
                    violations.append(f"{label}: homed to {endstop} but tip {ta:.4f} != "
                                      f"endstop position {pos:.4f}")
            if ta is not None:
                last_tip = ta
        end = op.get("end") or {}
        if last_tip is not None and "tip" in end and abs(end["tip"] - last_tip) > TOL:
            violations.append(f"{op_name}: end.tip {end['tip']} does not match the last "
                              f"recorded move ({last_tip})")
    return violations


# --------------------------------------------------------------------------
# Markdown rendering (pure Python; no HH needed).
# --------------------------------------------------------------------------

def _kv_table(items) -> list[str]:
    """A 2-column '| Key | Value |' Markdown table for a (key, value) iterable."""
    return ["| Key | Value |", "|---|---|"] + [f"| `{k}` | `{v}` |" for k, v in items]


def render_md(dossier: dict, device: str) -> str:
    src, dev = dossier["source"], dossier["device"]
    lines = [f"# {device}", "",
             f"Vendor: **{dev.get('vendor')}**, {dev.get('gates')} gates, "
             f"selector `{dev.get('selector_type')}`. Traced against Happy Hare "
             f"`{src['happy_hare']}`, harness profile `{src['profile']}`.", ""]

    decisions = dossier.get("decisions") or []
    if decisions:
        lines += ["## Decisions", ""]
        for d in decisions:
            lines.append(f"- **{d['concept']}**: {d['note']}")
        lines.append("")

    lines += ["## Hardware (rendered)", ""]
    for name, body in dossier.get("hardware", {}).items():
        lines += [f"### `[{name}]`", ""] + _kv_table(body.items()) + [""]

    lines += ["## Parameters", ""] + _kv_table(dossier.get("parameters", {}).items())
    lines += ["", f"Steps/mm (gear): `{dossier.get('steps_per_mm')}`", ""]

    lines += ["## Operations", ""]
    for op_name, op in dossier.get("operations", {}).items():
        lines.append(f"### {op_name.upper()}")
        lines.append("")
        if op["moves"]:
            lines.append("| Phase | Motor | Homing | Endstop | Dist | Speed | Accel | Actual | Homed |")
            lines.append("|---|---|---|---|---:|---:|---:|---:|---|")
            for m in op["moves"]:
                lines.append(f"| {m['label']} | {m['motor']} | {m['homing']} | "
                             f"{m['endstop'] or ''} | {m['dist']:.2f} | {m['speed']} | "
                             f"{m['accel']} | {m['actual']:.2f} | {m['homed']} |")
        else:
            lines.append("_No motion (already in the requested state)._")
        end = op["end"]
        if end.get("sensor") is not None:
            lines.append("")
            lines.append(f"Ends at tip `{end['tip']:.2f}` = `{end['sensor']}` "
                         f"{'+' if end['offset'] >= 0 else '-'} `{abs(end['offset']):.2f}` mm "
                         f"(filament_pos={end['filament_pos']}, gate_status={end['gate_status']}).")
        if op["errors"]:
            lines.append("")
            lines.append(f"Errors: {op['errors']}")
        lines.append("")

    lines += ["## Concept mapping", "",
             "| Upstream concept | AFC | Happy Hare | klipper-mmu today | Status |",
             "|---|---|---|---|---|"]
    for concept, afc, hh, km, status in MAPPING:
        lines.append(f"| {concept} | {afc} | {hh} | {km} | {status} |")
    lines.append("")
    lines += ["_Not modelled by this dossier: motion timing/step generation, macro bodies, "
             "espooler and FlowGuard behaviour, real sensor electrics, and calibration "
             "(seeded, not measured)._"]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cmd_trace(args) -> int:
    syms = parse_syms_args(args.syms) if args.syms else None
    spec = device_spec(args.device, args.profile, syms)
    existing_decisions = []
    out_path = Path(args.out) if args.out else ROOT / "docs" / "devices" / f"{args.device}.json"
    if out_path.is_file():
        existing_decisions = json.loads(out_path.read_text()).get("decisions", [])
    raw = run_trace(spec)
    dossier = reduce_dossier(spec, raw, existing_decisions)
    for d in dossier["decisions"]:
        if d.get("concept") not in DECISION_CONCEPTS:
            print(f"note: decisions concept {d.get('concept')!r} is outside the 13 afc_facts "
                  f"compare uses ({', '.join(DECISION_CONCEPTS)})", file=sys.stderr)
    violations = check_self_consistency(dossier)
    if violations:
        print(f"refusing to write {out_path}: self-consistency check failed", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        return 1
    # NOT sort_keys: render_md() iterates "hardware"/"operations" in insertion
    # order (rendered-section order / PRELOAD..EJECT), and check() must diff
    # a freshly regenerated dossier's Markdown against this file's -- sorting
    # keys here would silently reorder both without changing dict equality.
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(dossier, indent=2) + "\n")
    print(f"wrote {out_path}")
    return 0


def cmd_md(args) -> int:
    path = Path(args.dossier)
    dossier = json.loads(path.read_text())
    device = path.stem
    print(render_md(dossier, device), end="")
    return 0


def cmd_check(args) -> int:
    devices_dir = ROOT / "docs" / "devices"
    if args.devices:
        json_paths = [devices_dir / f"{d}.json" for d in args.devices]
    else:
        json_paths = sorted(devices_dir.glob("*.json"))
    if not json_paths:
        print("no committed dossiers to check", file=sys.stderr)
        return 1
    ok = True
    for json_path in json_paths:
        if not json_path.is_file():
            print(f"missing: {json_path}", file=sys.stderr)
            ok = False
            continue
        committed = json.loads(json_path.read_text())
        spec = device_spec(json_path.stem)
        raw = run_trace(spec)
        regenerated = reduce_dossier(spec, raw, committed.get("decisions", []))
        if regenerated != committed:
            print(f"DIFFERS (json): {json_path}", file=sys.stderr)
            ok = False
            continue
        md_path = json_path.with_suffix(".md")
        regenerated_md = render_md(regenerated, json_path.stem)
        if not md_path.is_file() or md_path.read_text() != regenerated_md:
            print(f"DIFFERS (md): {md_path}", file=sys.stderr)
            ok = False
            continue
        print(f"ok: {json_path.stem}")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_trace = sub.add_parser("trace", help="boot DEVICE in the HH harness and write a dossier")
    p_trace.add_argument("device")
    p_trace.add_argument("--profile", help="a shipped test/hh/profiles.py profile name")
    p_trace.add_argument("--syms", nargs="*", help="Kconfig symbol overrides, K=V K=V ...")
    p_trace.add_argument("--out", help="dossier JSON path (default docs/devices/DEVICE.json)")
    p_trace.set_defaults(func=cmd_trace)

    p_md = sub.add_parser("md", help="print a dossier's Markdown page")
    p_md.add_argument("dossier")
    p_md.set_defaults(func=cmd_md)

    p_check = sub.add_parser("check", help="regenerate and diff committed dossiers")
    p_check.add_argument("devices", nargs="*")
    p_check.set_defaults(func=cmd_check)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
