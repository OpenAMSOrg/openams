#!/usr/bin/env python3
"""Fit observed feeder travel, validate a separate capture, export a plant profile."""
# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
import copy
import csv
import hashlib
import io
import json
import math
from pathlib import Path

PARAMETERS = ('step_mm', 'forward_transfer', 'reverse_transfer', 'hub_mm',
              'arrival_mm', 'buffer_capacity_mm', 'switch_hysteresis_mm',
              'rewind_max_mm_s', 'rewind_time_constant_s')
COLUMNS = ('time_us', 'forward_steps', 'reverse_steps', 'filament_mm',
           'valid', 'saturated')
UINT64_MAX = (1 << 64) - 1
MAX_SWITCH_DELIVERY_US = 999  # Acquisition repeats every 1 ms.
HEADER_MARKER = '/* Generated simulation parameters. No firmware tuning. */\n'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def number(value, name, minimum=0):
    require(type(value) in (int, float) and math.isfinite(value)
            and value >= minimum, f'{name}: expected finite number >= {minimum}')
    return value


def label(value, name):
    require(isinstance(value, str) and value.strip(), f'{name}: missing description')
    return value


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_profile(path):
    data = Path(path).read_bytes()
    profile = json.loads(data)
    require(type(profile['schema_version']) is int and profile['schema_version'] == 1,
            'unsupported profile schema')
    label(profile['profile_id'], 'profile_id')
    p = profile['parameters']
    require(set(p) in (set(PARAMETERS), set(PARAMETERS) | {'load_mm'}),
            'profile parameter names do not match plant')
    for name in PARAMETERS:
        number(p[name], name)
        label(profile['parameter_sources'][name], 'parameter source: ' + name)
    require(p['step_mm'] > 0 and p['hub_mm'] > 0
            and p['arrival_mm'] > p['hub_mm'] and p['buffer_capacity_mm'] > 0
            and math.isfinite(p['arrival_mm'] + p['buffer_capacity_mm']),
            'invalid travel geometry')
    require(p['forward_transfer'] <= 1 and p['reverse_transfer'] <= 1,
            'transfer must be within 0..1')
    h = p['switch_hysteresis_mm']
    require(h < p['hub_mm'] and 2 * h < p['arrival_mm'] - p['hub_mm']
            and h < p['buffer_capacity_mm'], 'invalid switch hysteresis')
    if 'load_mm' in p:
        load = number(p['load_mm'], 'load_mm')
        label(profile['parameter_sources'].get('load_mm'), 'parameter source: load_mm')
        require(load == 0 or (load > h and load + 2 * h < p['hub_mm']),
                'invalid LOAD switch geometry')
    timing = profile.get('board_timing', {})
    require(type(timing) is dict and set(timing) <= {'switch_delivery_us'},
            'invalid board timing fields')
    timing_sources = profile.get('board_timing_sources', {})
    require(type(timing_sources) is dict
            and set(timing_sources) <= {'switch_delivery_us'}
            and set(timing_sources) <= set(timing), 'invalid board timing sources')
    if 'switch_delivery_us' in timing:
        delay = timing['switch_delivery_us']
        require(type(delay) is int and 0 <= delay <= MAX_SWITCH_DELIVERY_US,
                f'switch_delivery_us: expected integer in 0..{MAX_SWITCH_DELIVERY_US}')
        label(timing_sources.get('switch_delivery_us'),
              'board timing source: switch_delivery_us')
    return profile, digest(data)


def read_capture(directory):
    directory = Path(directory)
    return parse_capture((directory / 'metadata.json').read_bytes(),
                         (directory / 'travel.csv').read_bytes())


def parse_capture(metadata_bytes, trace_bytes):
    """Validate bytes before an importer publishes a normalized capture."""
    m = json.loads(metadata_bytes)
    require(type(m['schema_version']) is int and m['schema_version'] == 1,
            'unsupported capture schema')
    for field in ('capture_id', 'run_id', 'firmware_revision', 'configuration_id',
                  'board', 'mechanism', 'material', 'spool', 'conditions'):
        label(m[field], field)
    require(m['source_kind'] in ('synthetic', 'measured'), 'invalid source_kind')
    require(type(m['dropped_records']) is int and m['dropped_records'] == 0,
            'capture with dropped records cannot support this fit')
    clock = m['clock']
    require(clock['units'] == 'us' and clock['unwrapped'] is True,
            'explicit unwrapped microsecond time is required')
    for name in ('source', 'reconstruction', 'synchronization'):
        label(clock[name], 'clock ' + name)
    require(number(clock['resolution_us'], 'clock resolution') > 0,
            'clock resolution must be positive')
    number(clock['alignment_error_us'], 'clock alignment error')
    measurement = m['measurement']
    require(measurement['step_source'] == 'completed_physical_pulses',
            'commanded/estimated steps are not physical pulse observations')
    require(measurement['location'] == 'feeder_exit',
            'travel model requires measured transport at the feeder exit')
    label(measurement['method'], 'measurement method')
    number(measurement['position_error_mm'], 'position error')
    require(measurement['units'] == 'mm', 'filament positions must be millimeters')
    rows = []
    reader = csv.DictReader(io.StringIO(trace_bytes.decode('utf-8')))
    require(reader.fieldnames == list(COLUMNS), 'unexpected travel.csv columns')
    for line, raw in enumerate(reader, 2):
        require(line <= 100002 and None not in raw and None not in raw.values(),
                'trace too large or malformed row')
        integers = []
        for key in COLUMNS[:3]:
            require(raw[key].isascii() and raw[key].isdigit(), f'line {line}: invalid {key}')
            value = int(raw[key])
            require(value <= UINT64_MAX, f'line {line}: {key} exceeds uint64')
            integers.append(value)
        require(raw['valid'] in ('0', '1') and raw['saturated'] in ('0', '1'),
                f'line {line}: flags must be 0 or 1')
        position = float(raw['filament_mm']) if raw['filament_mm'] else None
        require(position is None or math.isfinite(position), f'line {line}: nonfinite position')
        require(raw['valid'] == '0' or position is not None, f'line {line}: missing valid position')
        row = (*integers, position, raw['valid'] == '1', raw['saturated'] == '1')
        if rows:
            previous = rows[-1]
            require(row[0] > previous[0] and row[1] >= previous[1] and row[2] >= previous[2],
                    f'line {line}: time/counters must already be unwrapped and monotonic')
            require(row[0] - previous[0] >= clock['resolution_us'],
                    f'line {line}: time spacing below stated resolution')
        rows.append(row)
    require(len(rows) >= 2, 'capture has insufficient observations')
    # Keep both file identity and normalized identity, so renaming/reformatting a
    # training trace cannot make it a held-out capture.
    identity = {'metadata': m, 'metadata_sha256': digest(metadata_bytes),
                'trace_sha256': digest(trace_bytes),
                'observations_sha256': digest(json.dumps(rows).encode())}
    intervals = {'forward': [], 'reverse': [], 'idle': []}
    excluded = {'invalid_or_saturated': 0, 'mixed_direction': 0}
    for a, b in zip(rows, rows[1:]):
        if not (a[4] and b[4]) or a[5] or b[5]:
            excluded['invalid_or_saturated'] += 1
            continue
        forward, reverse = b[1] - a[1], b[2] - a[2]
        if forward and reverse:
            excluded['mixed_direction'] += 1
            continue
        direction = 'forward' if forward else 'reverse' if reverse else 'idle'
        travel = b[3] - a[3]
        intervals[direction].append((forward + reverse, -travel if reverse else travel,
                                     b[0] - a[0]))
    for direction in ('forward', 'reverse'):
        require(len(intervals[direction]) >= 3, f'need three usable {direction} intervals')
    identity['excluded_intervals'] = excluded
    return identity, intervals, rows


def residuals(intervals, gain):
    errors = [gain * steps - travel for steps, travel, _ in intervals]
    require(all(math.isfinite(x) for x in errors), 'nonfinite prediction')
    if not errors:
        return {'intervals': 0, 'max_error_mm': 0, 'rms_error_mm': 0}
    return {'intervals': len(errors), 'max_error_mm': max(map(abs, errors)),
            'rms_error_mm': math.hypot(*errors) / math.sqrt(len(errors))}


def travel_residual(rows, forward, reverse):
    """Predict displacement through each contiguous usable segment, including reversals."""
    error = maximum = 0.0
    checked = 0
    for a, b in zip(rows, rows[1:]):
        if not (a[4] and b[4]) or a[5] or b[5]:
            error = 0.0
            continue
        error += forward * (b[1] - a[1]) - reverse * (b[2] - a[2]) - (b[3] - a[3])
        require(math.isfinite(error), 'nonfinite accumulated travel prediction')
        maximum = max(maximum, abs(error))
        checked += 1
    return {'intervals': checked, 'max_position_error_mm': maximum}


def fit_travel(base_path, training_path, validation_path, profile_id, tolerance_mm):
    number(tolerance_mm, 'tolerance_mm')
    label(profile_id, 'new profile id')
    base, base_hash = read_profile(base_path)
    require(profile_id != base['profile_id'], 'new profile needs a new identity')
    train, train_intervals, train_rows = read_capture(training_path)
    valid, valid_intervals, valid_rows = read_capture(validation_path)
    tm, vm = train['metadata'], valid['metadata']
    require(tm['capture_id'] != vm['capture_id'] and tm['run_id'] != vm['run_id']
            and train['observations_sha256'] != valid['observations_sha256'],
            'validation must use a separate run and trace excluded from fitting')
    for field in ('source_kind', 'mechanism', 'configuration_id'):
        require(tm[field] == vm[field], 'capture scope differs: ' + field)
    if 'scope' in base:
        require(base['scope'] == {k: tm[k] for k in ('mechanism', 'configuration_id')},
                'base profile is scoped to a different mechanism/configuration')
    result = copy.deepcopy(base)
    result['profile_id'] = profile_id
    result['parent'] = {'profile_id': base['profile_id'], 'sha256': base_hash}
    result['scope'] = {k: tm[k] for k in ('mechanism', 'configuration_id')}
    fits = {}
    for direction in ('forward', 'reverse'):
        intervals = train_intervals[direction]
        denominator = sum(n * n for n, _, _ in intervals)
        gain = math.fsum(n * distance for n, distance, _ in intervals) / denominator
        transfer = gain / base['parameters']['step_mm']
        require(math.isfinite(transfer) and 0 <= transfer <= 1,
                direction + ' gain incompatible with fixed step_mm and 0..1 transfer; do not clip')
        # Worst sensitivity to the declared endpoint position error, not a
        # statistical confidence interval or a bound on all future loads.
        sensitivity = (2 * tm['measurement']['position_error_mm']
                       * sum(n for n, _, _ in intervals) / denominator)
        fits[direction] = {'effective_mm_per_step': gain,
                           'position_error_sensitivity_mm_per_step': sensitivity,
                           'training': residuals(intervals, gain),
                           'validation': residuals(valid_intervals[direction], gain),
                           'training_rate_steps_s': [min(n * 1e6 / dt for n, _, dt in intervals),
                                                     max(n * 1e6 / dt for n, _, dt in intervals)],
                           'validation_rate_steps_s': [min(n * 1e6 / dt for n, _, dt in valid_intervals[direction]),
                                                       max(n * 1e6 / dt for n, _, dt in valid_intervals[direction])]}
        parameter = direction + '_transfer'
        result['parameters'][parameter] = transfer
        result['parameter_sources'][parameter] = (
            f"Conditional fit to {tm['source_kind']} capture {tm['capture_id']}; "
            'fixed step_mm inherited from parent, not an independent slip measurement')
    checks = [fits[d][group]['max_error_mm'] <= tolerance_mm
              for d in fits for group in ('training', 'validation')]
    idle = {'training': residuals(train_intervals['idle'], 0),
            'validation': residuals(valid_intervals['idle'], 0)}
    checks.extend(x['max_error_mm'] <= tolerance_mm for x in idle.values())
    travel = {group: travel_residual(rows, fits['forward']['effective_mm_per_step'],
                                    fits['reverse']['effective_mm_per_step'])
              for group, rows in (('training', train_rows), ('validation', valid_rows))}
    checks.extend(x['max_position_error_mm'] <= tolerance_mm for x in travel.values())
    result['travel_fit'] = {
        'status': 'validated' if all(checks) else 'rejected',
        'method': 'least squares through zero, separately by direction; validation never fits',
        'identifiability': 'Only step_mm * transfer is identified; step_mm stays fixed.',
        'tolerance_mm': tolerance_mm, 'training': train, 'validation': valid,
        'directions': fits, 'idle': idle, 'travel': travel, 'hardware_qualified': False}
    return result


def export_header(profile_path, output):
    profile, sha = read_profile(profile_path)
    if 'travel_fit' in profile:
        require(profile['travel_fit']['status'] == 'validated', 'rejected fit cannot be exported')
        for direction in ('forward', 'reverse'):
            gain = profile['travel_fit']['directions'][direction]['effective_mm_per_step']
            require(profile['parameters'][direction + '_transfer']
                    == gain / profile['parameters']['step_mm'], 'fitted parameters changed after validation')
    values = '\n'.join(f'    .{key} = {float(profile["parameters"].get(key, 0)):.17g},'
                       for key in (*PARAMETERS, 'load_mm'))
    output = Path(output)
    require(not output.exists() or output.read_text().startswith(HEADER_MARKER),
            'refusing to overwrite an input or an unrelated output file')
    output.write_text(HEADER_MARKER
                           + f'/* Profile SHA256: {sha} */\n'
                           + f'static const uint32_t mmu_sim_switch_delivery_us = '
                           + f'{profile.get("board_timing", {}).get("switch_delivery_us", 200)}u;\n'
                           'static const mmu_sim_parameters_t mmu_sim_profile = {\n'
                           + values + '\n};\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    fit = sub.add_parser('fit-travel')
    for option in ('base', 'training', 'validation', 'id', 'output'):
        fit.add_argument('--' + option, required=True)
    fit.add_argument('--tolerance-mm', required=True, type=float)
    export = sub.add_parser('export-header')
    export.add_argument('profile')
    export.add_argument('output')
    args = parser.parse_args()
    try:
        if args.command == 'export-header':
            export_header(args.profile, args.output)
            return 0
        result = fit_travel(args.base, args.training, args.validation, args.id, args.tolerance_mm)
        # Profiles are versioned outputs: never replace a previous fit or input.
        serialized = json.dumps(result, indent=2, allow_nan=False) + '\n'
        with Path(args.output).open('x') as output:
            output.write(serialized)
        print(result['travel_fit']['status'])
        return 0 if result['travel_fit']['status'] == 'validated' else 1
    except (OSError, ValueError, KeyError, TypeError, OverflowError, csv.Error) as exc:
        parser.exit(2, f'{exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
