#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read AFC (Armored Turtle Filament Changer) templates as plain INI facts, and
compare them against a Happy Hare dossier (tools/upstream_dossier.py schema 1).

No AFC or Klipper code is imported or executed: every fact is read straight
off the .cfg text, the same way tools/report_migration.py reads local
Klipper configs. `--mcu` and `--afc-cfg` are optional context files; without
them, pin references and global speeds are printed unresolved/absent rather
than guessed.

facts TEMPLATE [--mcu ALIAS.cfg] [--afc-cfg AFC.cfg]
    Print one JSON object: the unit class/name, lane count, per-lane pins
    (polarity, and, given --mcu, the alias resolved to a bare MCU pin), the
    hub, buffer and (if present) extruder sections, and the long/short move
    speeds (a unit-level override if the template sets one, else the global
    [AFC] values from --afc-cfg).

compare DOSSIER.json TEMPLATE [--mcu ALIAS.cfg] [--afc-cfg AFC.cfg]
    Join those facts with a Happy Hare dossier by a fixed concept list and
    print a Markdown table: agree / differ / afc-only / hh-only. Exits 1 if
    any `differ` row has no matching entry in the dossier's "decisions" list
    (schema-1: [{"concept": ..., "note": ...}], concept names exact).

The AFC archive this was written against is pinned in references/ and
extracts the same way tests/hh_harness/Makefile extracts Happy Hare's:
tar xzf references/AFC-Klipper-Add-On-<rev>.tar.gz -C <dest> (no single
root directory inside the tarball).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from report_migration import SECTION, KEY  # reuse the regexes only, per AGENTS.md

COMMENT = re.compile(r'\s+[#;].*$')
MODS = re.compile(r'^([~^!]*)(.*)$')

# Schema-1 amendment: "sensor_polarity" covers the entry+exit polarity row,
# so this fixed list has 13 names for the plan's 12-concept table.
CONCEPTS = (
    'lane_count', 'entry_sensor', 'exit_sensor', 'hub_sensor', 'sensor_polarity',
    'bowden', 'preload_park', 'unload_park', 'eject_extra', 'long_speed',
    'short_speed', 'buffer_type', 'arrival',
)


# --- plain INI reading -----------------------------------------------------

def strip_comment(line: str) -> str:
    return '' if line.lstrip().startswith(('#', ';')) else COMMENT.sub('', line)


def iter_sections(text: str):
    """Yield (name, body_lines) per top-level [section]; comments/blanks stripped."""
    name, body = None, []
    for raw in text.splitlines():
        m = SECTION.match(raw)
        if m:
            if name is not None:
                yield name, body
            name, body = m.group(1).strip(), []
            continue
        line = strip_comment(raw)
        if line.strip() and name is not None:
            body.append(line)
    if name is not None:
        yield name, body


def section_kv(body: list[str]) -> dict[str, str]:
    kv: dict[str, str] = {}
    for line in body:
        m = KEY.match(line)
        if m:
            kv.setdefault(m.group(1), m.group(2).strip())
    return kv


def split_name(section: str) -> tuple[str, str]:
    head, _, tail = section.partition(' ')
    return head, tail.strip()


def find_section(sections, prefix: str):
    """First (tail-name-or-full, kv) whose header word equals prefix, or (None, None)."""
    for name, body in sections:
        head, tail = split_name(name)
        if head == prefix:
            return tail or name, section_kv(body)
    return None, None


def parse_board_pins(body: list[str]) -> tuple[list[str], dict[str, str]]:
    """[board_pins X] bodies pack several `KEY=PIN,` pairs per line (AFC_Lite.cfg)
    or one per line (EMU_SLB.cfg); both read the same way: everything after
    the `mcu:` line is comma-joined and split on `=`."""
    mcu_names: list[str] = []
    alias_lines: list[str] = []
    past_mcu = False
    for line in body:
        m = KEY.match(line)
        if m and not past_mcu and m.group(1).lower() == 'mcu':
            mcu_names = [x.strip() for x in m.group(2).split(',') if x.strip()]
            past_mcu = True
        elif past_mcu:
            alias_lines.append(line)
    aliases: dict[str, str] = {}
    for token in ','.join(alias_lines).split(','):
        key, sep, value = token.strip().partition('=')
        if sep and key and value and key.strip().lower() != 'aliases':
            aliases[key.strip()] = value.strip()
    return mcu_names, aliases


def build_alias_table(mcu_path: Path | None) -> dict[str, dict[str, str]]:
    table: dict[str, dict[str, str]] = {}
    if mcu_path:
        for name, body in iter_sections(mcu_path.read_text(encoding='utf-8')):
            if split_name(name)[0] == 'board_pins':
                mcu_names, aliases = parse_board_pins(body)
                table.update({m: aliases for m in mcu_names})
    return table


def resolve_pin(raw: str, alias_table: dict[str, dict[str, str]]) -> dict[str, object]:
    """Split leading ^/~/! modifiers from an mcu:alias or bare pin, resolving
    the alias through --mcu's board_pins table when possible."""
    mods, rest = MODS.match(raw.strip()).groups()
    resolved, unresolved = rest, False
    if ':' in rest:
        mcu_name, alias = rest.split(':', 1)
        pin = alias_table.get(mcu_name, {}).get(alias)
        resolved, unresolved = (pin, False) if pin is not None else (rest, True)
    return {'raw': raw.strip(), 'pullup': '^' in mods, 'pulldown': '~' in mods,
            'inverted': '!' in mods, 'resolved': resolved, 'unresolved': unresolved}


def mods_of(raw) -> str:
    return MODS.match(str(raw).strip()).group(1)


def bare_pin(raw) -> str:
    return MODS.match(str(raw).strip()).group(2)


# --- facts -------------------------------------------------------------

def plain_fields(kv: dict[str, str], keys) -> dict[str, str]:
    return {k: kv[k] for k in keys if k in kv}


def pin_fields(kv: dict[str, str], keys, alias_table) -> dict[str, object]:
    return {k: resolve_pin(kv[k], alias_table) for k in keys if k in kv}


def default_afc_cfg(template_path: Path) -> Path | None:
    """AFC's layout puts config/AFC.cfg beside templates/DEVICE.cfg in the
    extracted archive; use that sibling unless --afc-cfg said otherwise."""
    candidate = template_path.resolve().parent.parent / 'config' / 'AFC.cfg'
    return candidate if candidate.is_file() else None


LANE_PINS = ('prep', 'load', 'step_pin', 'dir_pin', 'enable_pin',
             'afc_motor_rwd', 'afc_motor_fwd', 'afc_motor_enb')
LANE_VALUES = ('unit', 'dist_hub', 'park_dist', 'rotation_distance', 'microsteps',
               'extruder_clear_dis')
HUB_VALUES = ('afc_bowden_length', 'hub_clear_move_dis', 'move_dis', 'use_dist_hub')
BUFFER_PINS = ('advance_pin', 'trailing_pin', 'adc_pin')
BUFFER_VALUES = ('multiplier_high', 'multiplier_low', 'neutral_point', 'max_tension',
                 'max_compression', 'reversed')
EXTRUDER_VALUES = ('pin_tool_start', 'tool_stn', 'tool_stn_unload')
SPEED_KEYS = ('long_moves_speed', 'long_moves_accel', 'short_moves_speed',
              'short_moves_accel', 'short_move_dis')
NON_UNIT_PREFIXES = ('AFC_stepper', 'AFC_hub', 'AFC_buffer', 'AFC_button',
                     'AFC_led', 'AFC_extruder')


def build_facts(template_path: Path, mcu_path: Path | None, afc_cfg_path: Path | None) -> dict:
    sections = list(iter_sections(template_path.read_text(encoding='utf-8')))
    alias_table = build_alias_table(mcu_path)

    unit = None
    for name, body in sections:
        head, tail = split_name(name)
        if head.startswith('AFC_') and head not in NON_UNIT_PREFIXES:
            kv = section_kv(body)
            if 'hub' in kv or 'extruder' in kv:
                unit = {'class': head, 'name': tail, 'raw': kv}
                break

    lanes = []
    for name, body in sections:
        if split_name(name)[0] != 'AFC_stepper':
            continue
        kv = section_kv(body)
        lane = {'name': split_name(name)[1] or name}
        lane.update(pin_fields(kv, LANE_PINS, alias_table))
        lane.update(plain_fields(kv, LANE_VALUES))
        lanes.append(lane)

    # Shared-drive units (ViViD, Claymore) wire one drive_stepper and one
    # selector_stepper for the whole unit -- their two [AFC_stepper ...]
    # sections are those shared motors, not lanes; [AFC_lane laneN] is what
    # actually repeats once per lane there. Count those when present; the
    # classic one-stepper-per-lane templates (Turtle, NightOwl, QuattroBox)
    # have no [AFC_lane ...] sections, so this keeps counting AFC_stepper.
    lane_sections = sum(1 for name, _ in sections if split_name(name)[0] == 'AFC_lane')
    lane_count = lane_sections if lane_sections else len(lanes)

    hub_name, hub_kv = find_section(sections, 'AFC_hub')
    hub = None
    if hub_kv is not None:
        hub = {'name': hub_name, **plain_fields(hub_kv, HUB_VALUES)}
        if 'switch_pin' in hub_kv:
            raw = hub_kv['switch_pin']
            hub['switch_pin'] = ('virtual' if raw.strip().lower() == 'virtual'
                                 else resolve_pin(raw, alias_table))

    buf_name, buf_kv = find_section(sections, 'AFC_buffer')
    buffer = None
    if buf_kv is not None:
        btype = ('analog' if 'adc_pin' in buf_kv else
                'switched' if ('advance_pin' in buf_kv or 'trailing_pin' in buf_kv) else 'unknown')
        buffer = {'name': buf_name, 'type': btype,
                 **pin_fields(buf_kv, BUFFER_PINS, alias_table),
                 **plain_fields(buf_kv, BUFFER_VALUES)}

    ext_name, ext_kv = find_section(sections, 'AFC_extruder')
    extruder = {'name': ext_name, **plain_fields(ext_kv, EXTRUDER_VALUES)} if ext_kv is not None else None

    if afc_cfg_path is None:
        afc_cfg_path = default_afc_cfg(template_path)
    global_afc = {}
    if afc_cfg_path and afc_cfg_path.is_file():
        afc_sections = list(iter_sections(afc_cfg_path.read_text(encoding='utf-8')))
        _, global_afc = find_section(afc_sections, 'AFC')
        global_afc = global_afc or {}

    speeds = {}
    for key in SPEED_KEYS:
        if unit and key in unit['raw']:
            speeds[key] = {'value': unit['raw'][key], 'source': 'unit'}
        elif key in global_afc:
            speeds[key] = {'value': global_afc[key], 'source': str(afc_cfg_path)}

    return {
        'template': str(template_path), 'mcu': str(mcu_path) if mcu_path else None,
        'afc_cfg': str(afc_cfg_path) if afc_cfg_path else None,
        'unit': {'class': unit['class'], 'name': unit['name']} if unit else None,
        'lane_count': lane_count, 'lanes': lanes, 'hub': hub, 'buffer': buffer,
        'extruder': extruder, 'speeds': speeds,
        # extras/AFC_unit.py:111's UNIT-level extruder_clear_dis (a lane may
        # override it, extras/AFC_lane.py:192); None for all three pinned
        # templates today since neither level sets it -- a real fact, not a
        # guessed default (see eject_extra in concept_values() below).
        'unit_extruder_clear_dis': (unit['raw'].get('extruder_clear_dis') if unit else None),
    }


def cmd_facts(args: argparse.Namespace) -> int:
    facts = build_facts(Path(args.template), Path(args.mcu) if args.mcu else None,
                        Path(args.afc_cfg) if args.afc_cfg else None)
    print(json.dumps(facts, indent=2, sort_keys=True))
    return 0


# --- compare -------------------------------------------------------------
#
# Every HH-side value below is one exact schema-1 path off a single traced
# unit ("unit0", gate 0) -- no walking/searching the dossier (see
# docs/devices/emu.json for a worked example):
#
#   device.gates                                                -- lane_count
#   hardware["mmu_sensors unit0"]["mmu_entry_switch_pin_0"]      -- entry_sensor
#   hardware["mmu_sensors unit0"]["mmu_exit_switch_pin_0"]       -- exit_sensor
#   hardware["mmu_sensors unit0"]["mmu_shared_exit_switch_pin"]  -- hub_sensor
#   (the entry/exit switch pins above, modifiers only)           -- sensor_polarity
#   hardware["mmu_buffer unit0"]: "analog_pin" -> analog (PSF),
#     "compression_pin"/"tension_pin" -> switched                -- buffer_type
#   parameters["gear_load_speed"/"gear_load_accel"]              -- long_speed
#   parameters["gear_short_move_speed"/"gear_short_move_accel"]  -- short_speed
#   parameters["gate_final_eject_distance"]                      -- eject_extra
#   operations["preload"/"unload"].end.offset, as a magnitude    -- preload_park,
#                                                                    unload_park
#   operations["load"].moves[last homing move].endstop           -- arrival
#
# bowden is always compared against None (afc-only): the dossier's only
# "bowden"-named fields are booleans (unit_profile.variable_bowden_lengths/
# require_bowden_move) and its layout distances are synthetic harness
# geometry, not real bowden lengths -- there is no genuine HH number to put
# next to AFC's real afc_bowden_length/dist_hub millimetres.
#
# eject_extra deliberately does NOT use operations["eject"].end.offset: that
# is the eject END POSITION relative to mmu_entry (e.g. EMU's 48.999), not a
# distance. gate_final_eject_distance is the move length HH actually used.


def bare_hh_pin(raw: str | None) -> str | None:
    """Strip HH's modifiers (bare_pin) then its 'mcu-name:' prefix, so
    '^unit0_gate0:PA1' compares equal to AFC's resolved 'PA1'."""
    return None if raw is None else bare_pin(raw).rsplit(':', 1)[-1]


def hh_sensor(dossier: dict, key: str):
    """dossier['hardware']['mmu_sensors unit0'][key]."""
    return (dossier.get('hardware', {}).get('mmu_sensors unit0') or {}).get(key)


def hh_buffer_type(dossier: dict) -> str | None:
    """dossier['hardware']['mmu_buffer unit0']: analog_pin -> a proportional
    (PSF) buffer, compression_pin/tension_pin -> a switched one."""
    buf = dossier.get('hardware', {}).get('mmu_buffer unit0') or {}
    if 'analog_pin' in buf:
        return 'analog'
    if 'compression_pin' in buf or 'tension_pin' in buf:
        return 'switched'
    return None


def hh_arrival(dossier: dict) -> str | None:
    """The endstop of the LAST homing move in operations.load.moves: what
    the harness actually arrived on, not a config default."""
    endstop = None
    for move in dossier.get('operations', {}).get('load', {}).get('moves', []):
        if move.get('homing'):
            endstop = move.get('endstop')
    return endstop


def speed_str(speed, accel) -> str | None:
    """'speed/accel', '?' for a missing half; None if both are missing."""
    if speed is None and accel is None:
        return None
    return '%s/%s' % (speed if speed is not None else '?', accel if accel is not None else '?')


def hh_param_pair(dossier: dict, speed_key: str, accel_key: str) -> str | None:
    params = dossier.get('parameters', {})
    return speed_str(params.get(speed_key), params.get(accel_key))


def afc_speed_pair(speeds: dict, prefix: str) -> str | None:
    s, a = speeds.get(prefix + '_moves_speed'), speeds.get(prefix + '_moves_accel')
    return speed_str(s['value'] if s else None, a['value'] if a else None)


def op_offset_magnitude(dossier: dict, op: str):
    """abs(operations[op].end.offset). HH's offset is signed (negative =
    behind the sensor); AFC's park/clear fields are unsigned, so compare
    magnitudes and state direction in the decision text, not the number."""
    offset = ((dossier.get('operations', {}).get(op) or {}).get('end') or {}).get('offset')
    return None if offset is None else abs(offset)


def _num(value):
    try:
        return abs(float(value))
    except (TypeError, ValueError):
        return None


def values_agree(afc_value, hh_value) -> bool:
    if afc_value is None or hh_value is None:
        return False
    a, h = _num(afc_value), _num(hh_value)
    if a is not None and h is not None:
        return abs(a - h) < 1e-6
    return str(afc_value).strip() == str(hh_value).strip()


def concept_values(facts: dict, dossier: dict) -> dict[str, tuple[object, object]]:
    """concept -> (afc_value, hh_value); either side may be None. See the
    module comment above this section for every field's exact source."""
    lanes = facts.get('lanes') or []
    lane0 = lanes[0] if lanes else {}
    hub = facts.get('hub') or {}
    buf = facts.get('buffer') or {}
    speeds = facts.get('speeds') or {}

    def pin_of(entry):
        return entry.get('resolved') if isinstance(entry, dict) else entry

    entry_raw = hh_sensor(dossier, 'mmu_entry_switch_pin_0')
    exit_raw = hh_sensor(dossier, 'mmu_exit_switch_pin_0')
    hub_raw = hh_sensor(dossier, 'mmu_shared_exit_switch_pin')

    afc_pol = hh_pol = None
    if lane0.get('prep') and lane0.get('load'):
        afc_pol = 'entry=%s,exit=%s' % (mods_of(lane0['prep']['raw']), mods_of(lane0['load']['raw']))
    if entry_raw is not None and exit_raw is not None:
        hh_pol = 'entry=%s,exit=%s' % (mods_of(entry_raw), mods_of(exit_raw))

    eject_afc = lane0.get('extruder_clear_dis') or facts.get('unit_extruder_clear_dis')

    return {
        'lane_count': (facts.get('lane_count'), dossier.get('device', {}).get('gates')),
        'entry_sensor': (pin_of(lane0.get('prep')), bare_hh_pin(entry_raw)),
        'exit_sensor': (pin_of(lane0.get('load')), bare_hh_pin(exit_raw)),
        'hub_sensor': (pin_of(hub.get('switch_pin')), bare_hh_pin(hub_raw)),
        'sensor_polarity': (afc_pol, hh_pol),
        'bowden': (hub.get('afc_bowden_length') or lane0.get('dist_hub'), None),
        'preload_park': (_num(lane0.get('park_dist')), op_offset_magnitude(dossier, 'preload')),
        'unload_park': (_num(hub.get('hub_clear_move_dis') or lane0.get('park_dist')),
                        op_offset_magnitude(dossier, 'unload')),
        'eject_extra': (eject_afc, dossier.get('parameters', {}).get('gate_final_eject_distance')),
        'long_speed': (afc_speed_pair(speeds, 'long'), hh_param_pair(dossier, 'gear_load_speed', 'gear_load_accel')),
        'short_speed': (afc_speed_pair(speeds, 'short'),
                        hh_param_pair(dossier, 'gear_short_move_speed', 'gear_short_move_accel')),
        'buffer_type': (buf.get('type'), hh_buffer_type(dossier)),
        'arrival': ((facts.get('extruder') or {}).get('pin_tool_start'), hh_arrival(dossier)),
    }


def classify(afc_value, hh_value) -> str:
    if afc_value is None and hh_value is None:
        return 'n/a'
    if afc_value is None:
        return 'hh-only'
    if hh_value is None:
        return 'afc-only'
    return 'agree' if values_agree(afc_value, hh_value) else 'differ'


def cmd_compare(args: argparse.Namespace) -> int:
    dossier = json.loads(Path(args.dossier).read_text(encoding='utf-8'))
    facts = build_facts(Path(args.template), Path(args.mcu) if args.mcu else None,
                        Path(args.afc_cfg) if args.afc_cfg else None)
    decisions = {d['concept'] for d in dossier.get('decisions', [])}
    values = concept_values(facts, dossier)

    rows, undecided = [], []
    for concept in CONCEPTS:
        afc_value, hh_value = values.get(concept, (None, None))
        status = classify(afc_value, hh_value)
        if status == 'differ' and concept not in decisions:
            undecided.append(concept)
        rows.append((concept, afc_value, hh_value, status))

    print('| concept | afc | happy-hare | status |')
    print('|---|---|---|---|')
    for concept, afc_value, hh_value, status in rows:
        print('| %s | %s | %s | %s |' % (concept, afc_value, hh_value, status))
    if undecided:
        print()
        print('Undecided `differ` rows (no matching "decisions" entry): %s' % ', '.join(undecided))
    return 1 if undecided else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)

    p_facts = sub.add_parser('facts', help='print one AFC template as JSON facts')
    p_facts.add_argument('template')
    p_facts.add_argument('--mcu', help='board_pins alias file to resolve pins through')
    p_facts.add_argument('--afc-cfg', help='global AFC.cfg for long/short move speeds '
                         '(default: config/AFC.cfg beside the template)')
    p_facts.set_defaults(func=cmd_facts)

    p_compare = sub.add_parser('compare', help='diff a Happy Hare dossier against an AFC template')
    p_compare.add_argument('dossier')
    p_compare.add_argument('template')
    p_compare.add_argument('--mcu')
    p_compare.add_argument('--afc-cfg')
    p_compare.set_defaults(func=cmd_compare)

    args = parser.parse_args()
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
