# SPDX-License-Identifier: GPL-3.0-or-later
import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import measurements
import normalize_travel as normalizer
from test_measurements import BASE, write_capture


def raw_capture(path, name='raw-training', counts=(10, 20, 30, 40), frequency=3333333):
    write_capture(path, name, counts)
    metadata = json.loads((path / 'metadata.json').read_text())
    metadata['clock'] = {
        'source': 'synthetic shared acquisition clock', 'units': 'ticks',
        'counter_bits': 32, 'frequency_hz': frequency, 'resolution_ticks': 1,
        'maximum_interval_ticks': 2000000000, 'alignment_error_us': 0.25,
        'continuous_clock': True,
        'interval_bound_evidence': 'Synthetic fixture bounds each interval at 400 ms',
        'synchronization': 'Position and completed pulse counters sampled together',
    }
    with (path / 'travel.csv').open() as source:
        rows = list(csv.DictReader(source))
    for index, row in enumerate(rows):
        ticks = (0xfffffff0 + int(row.pop('time_us')) * frequency // 1000000) % (1 << 32)
        row.update(sequence=str(100 + index), ticks=str(ticks))
    save_raw(path, metadata, rows)
    return metadata, rows


def save_raw(path, metadata, rows):
    (path / 'metadata.json').write_text(json.dumps(metadata, indent=2) + '\n')
    with (path / 'travel.csv').open('w', newline='') as out:
        writer = csv.DictWriter(out, fieldnames=normalizer.RAW_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


class NormalizeTravel(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source, self.output = self.root / 'raw', self.root / 'normalized'
        self.metadata, self.rows = raw_capture(self.source)

    def test_fractional_tick_clock_wrap_is_cumulative_and_input_preserved(self):
        before = {p.name: p.read_bytes() for p in self.source.iterdir()}
        report = normalizer.normalize(self.source, self.output)
        identity, _, rows = measurements.read_capture(self.output)
        # Literal rational results, different from separately flooring intervals.
        self.assertEqual([r[0] for r in rows],
                         [0, 99999, 299999, 599999, 1000000, 1099999, 1299999, 1599999, 2000000])
        self.assertEqual((report['observed_wraps'], report['elapsed_ticks']), (1, 6666666))
        self.assertEqual(identity['metadata']['clock']['alignment_error_us'], 1.25)
        self.assertEqual(report['first_ticks'], 0xfffffff0)
        for name, data in before.items():
            self.assertEqual((self.source / name).read_bytes(), data)
            self.assertEqual((self.output / 'raw' / name).read_bytes(), data)
        self.assertEqual(report['raw_trace_sha256'], measurements.digest(before['travel.csv']))

    def test_multiple_wraps_across_separately_bounded_intervals(self):
        path = self.root / 'fast-raw'
        raw_capture(path, frequency=4000000003)
        report = normalizer.normalize(path, self.output)
        self.assertEqual(report['observed_wraps'], 2)
        self.assertEqual(report['elapsed_ticks'], 8000000006)
        _, _, rows = measurements.read_capture(self.output)
        self.assertEqual(rows[-1][0], 2000000)

    def test_declared_resolution_survives_integer_time_quantization(self):
        self.metadata['clock']['resolution_ticks'] = 333333
        save_raw(self.source, self.metadata, self.rows)
        normalizer.normalize(self.source, self.output)
        identity, _, _ = measurements.read_capture(self.output)
        self.assertEqual(identity['metadata']['clock']['resolution_us'], 99999)
        self.assertEqual(identity['metadata']['normalization']['raw_clock']['resolution_ticks'], 333333)

    def test_normalized_runs_fit_and_export_without_firmware_tuning(self):
        raw_valid, valid = self.root / 'raw-valid', self.root / 'valid'
        raw_capture(raw_valid, 'raw-validation', (7, 13, 23, 37), frequency=170000003)
        normalizer.normalize(self.source, self.output)
        normalizer.normalize(raw_valid, valid)
        result = measurements.fit_travel(BASE, self.output, valid, 'normalized-fit-v1', 1e-9)
        self.assertEqual(result['travel_fit']['status'], 'validated')
        self.assertEqual(result['parameters']['forward_transfer'], .875)
        self.assertEqual(result['parameters']['reverse_transfer'], .9375)
        self.assertFalse(result['travel_fit']['hardware_qualified'])
        profile = self.root / 'fitted.json'
        profile.write_text(json.dumps(result))
        measurements.export_header(profile, self.root / 'profile.h')
        self.assertIn('.forward_transfer = 0.875,', (self.root / 'profile.h').read_text())

    def assert_rejected(self, pattern):
        save_raw(self.source, self.metadata, self.rows)
        with self.assertRaisesRegex((ValueError, KeyError), pattern):
            normalizer.normalize(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_missing_reordered_or_duplicate_sequence_rejected(self):
        for change in ('100', '105', str(1 << 64)):
            with self.subTest(sequence=change):
                self.rows[1]['sequence'] = change
                self.assert_rejected('record|sequence')

    def test_wrap_ambiguity_reset_and_duplicate_tick_rejected(self):
        for delta in (0, 0x80000000, 0xffffffff, 2000000001):
            with self.subTest(delta=delta):
                self.rows[1]['ticks'] = str((int(self.rows[0]['ticks']) + delta) % (1 << 32))
                self.assert_rejected('clock|interval')

    def test_clock_bounds_cannot_be_assumed(self):
        original = self.metadata['clock'].copy()
        for key, value in [('continuous_clock', False), ('interval_bound_evidence', ''),
                           ('frequency_hz', 0), ('frequency_hz', True),
                           ('maximum_interval_ticks', 1 << 31), ('resolution_ticks', 0),
                           ('counter_bits', 16), ('alignment_error_us', -1)]:
            with self.subTest(key=key, value=value):
                self.metadata['clock'] = {**original, key: value}
                self.assert_rejected('clock|interval|frequency|counter|32-bit|error')

    def test_capture_drop_or_estimated_steps_rejected(self):
        self.metadata['dropped_records'] = 1
        self.assert_rejected('dropped')
        self.metadata['dropped_records'] = 0
        self.metadata['measurement']['step_source'] = 'commanded'
        self.assert_rejected('not physical')

    def test_submicrosecond_rows_are_not_silently_dropped(self):
        self.rows[1]['ticks'] = str((int(self.rows[0]['ticks']) + 1) % (1 << 32))
        self.assert_rejected('monotonic')

    def test_position_flags_and_pulse_counters_use_existing_validation(self):
        for key, value in [('filament_mm', 'nan'), ('forward_steps', '-1'),
                           ('reverse_steps', str(1 << 64)), ('valid', 'true')]:
            with self.subTest(key=key):
                original = self.rows[1][key]
                self.rows[1][key] = value
                self.assert_rejected('nonfinite|invalid|uint64|flags')
                self.rows[1][key] = original

    def test_preexisting_output_and_inputs_are_never_replaced(self):
        self.output.mkdir()
        keep = self.output / 'user-data'
        keep.write_text('untouched')
        with self.assertRaises(FileExistsError):
            normalizer.normalize(self.source, self.output)
        self.assertEqual(keep.read_text(), 'untouched')
        before = (self.source / 'metadata.json').read_bytes()
        with self.assertRaises(FileExistsError):
            normalizer.normalize(self.source, self.source)
        self.assertEqual((self.source / 'metadata.json').read_bytes(), before)

    def test_cli_reports_error_without_output(self):
        self.metadata['clock']['continuous_clock'] = False
        save_raw(self.source, self.metadata, self.rows)
        result = subprocess.run([sys.executable, '-B', str(Path(normalizer.__file__)),
                                 str(self.source), str(self.output)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn('clock resets', result.stderr)
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
