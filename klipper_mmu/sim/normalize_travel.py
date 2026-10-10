#!/usr/bin/env python3
"""Normalize a continuous 32-bit tick capture for the existing travel fitter."""
# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
import csv
import io
import json
from pathlib import Path

from measurements import COLUMNS, UINT64_MAX, digest, label, number, parse_capture, require

RAW_COLUMNS = ('sequence', 'ticks', *COLUMNS[1:])
TICK_MODULUS = 1 << 32


def integer(value, name, minimum, maximum):
    require(type(value) is int and minimum <= value <= maximum,
            f'{name}: expected integer within {minimum}..{maximum}')
    return value


def csv_integer(value, name, maximum):
    require(value.isascii() and value.isdigit(), f'invalid {name}')
    return integer(int(value), name, 0, maximum)


def normalize(source, output):
    """Read and validate fully before creating a new output; never edit inputs."""
    source, output = Path(source), Path(output)
    metadata_bytes = (source / 'metadata.json').read_bytes()
    trace_bytes = (source / 'travel.csv').read_bytes()
    metadata = json.loads(metadata_bytes)
    raw_clock = metadata['clock']
    require(raw_clock['units'] == 'ticks' and raw_clock['counter_bits'] == 32
            and type(raw_clock['counter_bits']) is int,
            'expected an explicit 32-bit tick clock')
    require(raw_clock['continuous_clock'] is True,
            'clock resets require separate captures')
    label(raw_clock['interval_bound_evidence'], 'interval bound evidence')
    label(raw_clock['synchronization'], 'clock synchronization')
    frequency = integer(raw_clock['frequency_hz'], 'clock frequency', 1, TICK_MODULUS - 1)
    maximum_gap = integer(raw_clock['maximum_interval_ticks'], 'maximum interval', 1,
                          TICK_MODULUS // 2 - 1)
    resolution = integer(raw_clock['resolution_ticks'], 'clock resolution', 1, maximum_gap)
    alignment = number(raw_clock['alignment_error_us'], 'clock alignment error')

    reader = csv.DictReader(io.StringIO(trace_bytes.decode('utf-8')))
    require(reader.fieldnames == list(RAW_COLUMNS), 'unexpected raw travel.csv columns')
    normalized = io.StringIO(newline='')
    writer = csv.writer(normalized, lineterminator='\n')
    writer.writerow(COLUMNS)
    previous = first = None
    elapsed = wraps = count = 0
    for line, row in enumerate(reader, 2):
        require(line <= 100002 and None not in row and None not in row.values(),
                'trace too large or malformed row')
        sequence = csv_integer(row['sequence'], f'line {line} sequence', UINT64_MAX)
        ticks = csv_integer(row['ticks'], f'line {line} ticks', TICK_MODULUS - 1)
        if previous is None:
            first = (sequence, ticks)
        else:
            require(sequence == previous[0] + 1, f'line {line}: missing/reordered record')
            delta = (ticks - previous[1]) % TICK_MODULUS
            require(resolution <= delta <= maximum_gap,
                    f'line {line}: ambiguous/reset clock or violated interval bound')
            elapsed += delta
            wraps += ticks < previous[1]
        # Convert total elapsed ticks, never rounded individual intervals.
        time_us = elapsed * 1_000_000 // frequency
        require(time_us <= UINT64_MAX, 'normalized timestamp exceeds uint64')
        writer.writerow((time_us, *(row[key] for key in COLUMNS[1:])))
        previous = (sequence, ticks)
        count += 1
    require(count >= 2, 'capture has insufficient observations')

    metadata['clock'] = {
        'source': raw_clock['source'], 'units': 'us', 'unwrapped': True,
        'resolution_us': max(1, resolution * 1_000_000 // frequency),
        'alignment_error_us': alignment + 1,
        'reconstruction': 'Consecutive records; bounded modulo-2^32 intervals; '
                          'floor(total elapsed ticks * 1000000 / frequency_hz); '
                          'first observation defines time zero',
        'synchronization': raw_clock['synchronization'],
    }
    metadata['normalization'] = {
        'method': 'bounded-tick-travel-v1', 'raw_clock': raw_clock,
        'raw_metadata_sha256': digest(metadata_bytes),
        'raw_trace_sha256': digest(trace_bytes),
        'raw_metadata_file': 'raw/metadata.json', 'raw_trace_file': 'raw/travel.csv',
        'first_sequence': first[0], 'first_ticks': first[1],
        'observations': count, 'observed_wraps': wraps,
        'elapsed_ticks': elapsed, 'time_quantization_bound_us': 1,
        'clock_rate_calibrated_by_importer': False,
    }
    normalized_bytes = normalized.getvalue().encode()
    final_metadata = (json.dumps(metadata, indent=2, allow_nan=False) + '\n').encode()
    # Reuse the real fitter's schema, position, counter and sample checks.
    # No incomplete or malformed capture is published as successful input.
    parse_capture(final_metadata, normalized_bytes)
    output.mkdir()  # Exclusive destination: preserve any existing capture/output.
    (output / 'raw').mkdir()
    (output / 'raw/metadata.json').write_bytes(metadata_bytes)
    (output / 'raw/travel.csv').write_bytes(trace_bytes)
    (output / 'travel.csv').write_bytes(normalized_bytes)
    (output / 'metadata.json').write_bytes(final_metadata)
    return metadata['normalization']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture', help='raw capture directory')
    parser.add_argument('output', help='new normalized capture directory')
    args = parser.parse_args()
    try:
        print(json.dumps(normalize(args.capture, args.output)))
    except (OSError, ValueError, KeyError, TypeError, OverflowError, csv.Error) as exc:
        parser.exit(2, f'{exc}\n')


if __name__ == '__main__':
    main()
