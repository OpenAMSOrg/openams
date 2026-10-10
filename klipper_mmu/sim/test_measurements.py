# SPDX-License-Identifier: GPL-3.0-or-later
import copy
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import measurements as model

HERE = Path(__file__).resolve().parent
BASE = HERE / 'profiles/synthetic-lane.json'


def write_capture(path, name, counts=(10, 20, 30, 40), forward=0.109375, reverse=0.1171875):
    """Synthetic independent position observations; never a physical capture."""
    path.mkdir()
    metadata = {
        'schema_version': 1, 'capture_id': name, 'run_id': name + '-run',
        'source_kind': 'synthetic', 'firmware_revision': 'synthetic-fixture',
        'configuration_id': 'fixture-125um', 'board': 'native-fixture',
        'mechanism': 'one-lane-fixture', 'material': 'synthetic', 'spool': 'synthetic',
        'conditions': 'unconstrained feeder travel, no printer consumption',
        'dropped_records': 0,
        'clock': {'source': 'synthetic monotonic timeline', 'units': 'us',
                  'unwrapped': True, 'reconstruction': 'native uint64 timeline',
                  'synchronization': 'position and pulses sampled together',
                  'resolution_us': 1, 'alignment_error_us': 0},
        'measurement': {'step_source': 'completed_physical_pulses',
                        'location': 'feeder_exit', 'method': 'synthetic position oracle',
                        'units': 'mm', 'position_error_mm': 0.001}}
    (path / 'metadata.json').write_text(json.dumps(metadata, indent=2) + '\n')
    rows = [[0, 0, 0, 0.0, 1, 0]]
    for direction, gain in ((1, forward), (2, reverse)):
        for count in counts:
            row = rows[-1].copy()
            row[0] += count * 10000
            row[direction] += count
            row[3] += count * gain * (1 if direction == 1 else -1)
            rows.append(row)
    save_rows(path, rows)


def save_rows(path, rows):
    with (path / 'travel.csv').open('w', newline='') as output:
        writer = csv.writer(output)
        writer.writerow(model.COLUMNS)
        writer.writerows(rows)


class Measurements(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.train, self.valid = self.root / 'train', self.root / 'held-out'
        write_capture(self.train, 'train')
        write_capture(self.valid, 'held-out', (7, 13, 23, 37))

    def fit(self, **kw):
        return model.fit_travel(kw.get('base', BASE), self.train, self.valid,
                                'travel-v2', kw.get('tolerance', 1e-9))

    def metadata(self, path, change):
        file = path / 'metadata.json'
        data = json.loads(file.read_text())
        change(data)
        file.write_text(json.dumps(data))

    def rows(self, path):
        with (path / 'travel.csv').open() as input_file:
            rows = list(csv.reader(input_file))[1:]
        return [[int(x[0]), int(x[1]), int(x[2]), float(x[3]), int(x[4]), int(x[5])]
                for x in rows]

    def test_recovers_literal_gains_and_preserves_unobserved_parameters(self):
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = self.fit()
        self.assertEqual(result['travel_fit']['status'], 'validated')
        self.assertEqual(result['parameters']['forward_transfer'], 0.875)
        self.assertEqual(result['parameters']['reverse_transfer'], 0.9375)
        base = json.loads(BASE.read_text())
        for key in model.PARAMETERS:
            if key not in ('forward_transfer', 'reverse_transfer'):
                self.assertEqual(result['parameters'][key], base['parameters'][key])
                self.assertEqual(result['parameter_sources'][key], base['parameter_sources'][key])
        self.assertFalse(result['travel_fit']['hardware_qualified'])
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_holdout_never_changes_fit(self):
        rows = self.rows(self.valid)
        for row in rows:
            row[3] *= 0.5
        save_rows(self.valid, rows)
        result = self.fit(tolerance=0.01)
        self.assertEqual(result['travel_fit']['status'], 'rejected')
        self.assertEqual(result['parameters']['forward_transfer'], 0.875)
        self.assertEqual(result['parameters']['reverse_transfer'], 0.9375)

    def test_cumulative_error_cannot_hide_behind_small_interval_errors(self):
        rows = self.rows(self.valid)
        for row in rows:
            row[3] -= row[1] * .0001
        save_rows(self.valid, rows)
        fit = self.fit(tolerance=.004)['travel_fit']
        self.assertLess(fit['directions']['forward']['validation']['max_error_mm'], .004)
        self.assertGreater(fit['travel']['validation']['max_position_error_mm'], .004)
        self.assertEqual(fit['status'], 'rejected')

    def test_endpoint_error_sensitivity_is_not_claimed_as_confidence(self):
        fit = self.fit()['travel_fit']['directions']['forward']
        self.assertAlmostEqual(fit['position_error_sensitivity_mm_per_step'],
                               2 * .001 * 100 / 3000)
        self.assertEqual(fit['training_rate_steps_s'], [100, 100])

    def test_same_run_is_not_held_out(self):
        self.metadata(self.valid, lambda m: m.update(run_id='train-run'))
        with self.assertRaisesRegex(ValueError, 'separate run'):
            self.fit()

    def test_renamed_and_reformatted_data_is_not_held_out(self):
        text = (self.train / 'travel.csv').read_text().replace('\n', '\r\n')
        (self.valid / 'travel.csv').write_text(text)
        with self.assertRaisesRegex(ValueError, 'separate run'):
            self.fit()

    def test_commanded_steps_are_rejected(self):
        self.metadata(self.train, lambda m: m['measurement'].update(step_source='commanded'))
        with self.assertRaisesRegex(ValueError, 'not physical'):
            self.fit()

    def test_drop_and_scope_mismatch_are_rejected(self):
        for change in (lambda m: m.update(dropped_records=1),
                       lambda m: m.update(configuration_id='different-microsteps'),
                       lambda m: m.update(mechanism='another-device'),
                       lambda m: m.update(source_kind='measured')):
            original = (self.valid / 'metadata.json').read_bytes()
            self.metadata(self.valid, change)
            with self.assertRaises(ValueError):
                self.fit()
            (self.valid / 'metadata.json').write_bytes(original)

    def test_units_timebase_location_and_version_are_required(self):
        for change in (lambda m: m.update(schema_version=2),
                       lambda m: m['clock'].update(unwrapped=False),
                       lambda m: m['clock'].update(units='ticks'),
                       lambda m: m['clock'].update(resolution_us=0),
                       lambda m: m['measurement'].update(units='inches'),
                       lambda m: m['measurement'].update(location='motor_shaft')):
            original = (self.train / 'metadata.json').read_bytes()
            self.metadata(self.train, change)
            with self.assertRaises(ValueError):
                self.fit()
            (self.train / 'metadata.json').write_bytes(original)

    def test_counters_are_exact_above_float_integer_precision(self):
        rows = self.rows(self.train)
        for row in rows:
            for index in (0, 1, 2):
                row[index] += 1 << 63
        save_rows(self.train, rows)
        self.assertEqual(self.fit()['parameters']['forward_transfer'], .875)

    def test_bad_rows_fail_before_fitting(self):
        original = self.rows(self.train)
        for index, value in ((0, 0), (1, -1), (1, 1 << 64), (3, 'nan'), (3, 'inf'),
                             (3, ''), (4, 2), (5, -1)):
            rows = copy.deepcopy(original)
            rows[1][index] = value
            save_rows(self.train, rows)
            with self.assertRaises(ValueError):
                self.fit()
        rows = copy.deepcopy(original)
        rows[2][1] = 0
        save_rows(self.train, rows)
        with self.assertRaisesRegex(ValueError, 'monotonic'):
            self.fit()

    def test_invalid_rows_do_not_bridge_gaps(self):
        rows = self.rows(self.train)
        rows[0][4] = 0
        rows[0][3] = ''
        save_rows(self.train, rows)
        result = self.fit()
        self.assertEqual(result['travel_fit']['training']['excluded_intervals']['invalid_or_saturated'], 1)
        self.assertEqual(result['travel_fit']['directions']['forward']['training']['intervals'], 3)
        rows[2][5] = 1
        save_rows(self.train, rows)
        with self.assertRaisesRegex(ValueError, 'three usable'):
            self.fit()

    def test_mixed_directions_are_excluded(self):
        rows = self.rows(self.train)
        for row in rows[1:]:
            row[2] += 1
        save_rows(self.train, rows)
        result = self.fit()
        self.assertEqual(result['travel_fit']['training']['excluded_intervals']['mixed_direction'], 1)
        self.assertEqual(result['parameters']['forward_transfer'], .875)

    def test_idle_unexplained_motion_rejects_fit(self):
        rows = self.rows(self.valid)
        row = rows[-1].copy()
        row[0] += 100000
        row[3] += 2
        rows.append(row)
        save_rows(self.valid, rows)
        self.assertEqual(self.fit()['travel_fit']['status'], 'rejected')

    def test_incompatible_gain_is_not_clipped(self):
        rows = self.rows(self.train)
        for multiplier in (2, -1):
            changed = copy.deepcopy(rows)
            for row in changed:
                row[3] *= multiplier
            save_rows(self.train, changed)
            with self.assertRaisesRegex(ValueError, 'do not clip'):
                self.fit()

    def test_scoped_parent_cannot_silently_change_mechanism(self):
        parent = self.fit()
        parent['scope']['mechanism'] = 'another-device'
        parent_path = self.root / 'parent.json'
        parent_path.write_text(json.dumps(parent))
        with self.assertRaisesRegex(ValueError, 'different mechanism'):
            model.fit_travel(parent_path, self.train, self.valid, 'v3', .01)

    def test_export_refuses_rejected_fit(self):
        fitted = self.fit()
        fitted['travel_fit']['status'] = 'rejected'
        path = self.root / 'rejected.json'
        path.write_text(json.dumps(fitted))
        with self.assertRaisesRegex(ValueError, 'rejected fit'):
            model.export_header(path, self.root / 'out.h')
        self.assertFalse((self.root / 'out.h').exists())

    def test_export_preserves_input_and_rejects_changed_fit(self):
        path = self.root / 'fitted.json'
        fitted = self.fit()
        path.write_text(json.dumps(fitted))
        original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'overwrite'):
            model.export_header(path, path)
        self.assertEqual(path.read_bytes(), original)
        fitted['parameters']['forward_transfer'] = 1
        path.write_text(json.dumps(fitted))
        with self.assertRaisesRegex(ValueError, 'changed after validation'):
            model.export_header(path, self.root / 'changed.h')

    def test_header_export_can_refresh_only_its_own_generated_file(self):
        header = self.root / 'profile.h'
        model.export_header(BASE, header)
        model.export_header(BASE, header)
        header.write_text('unrelated user content')
        with self.assertRaisesRegex(ValueError, 'overwrite'):
            model.export_header(BASE, header)
        self.assertEqual(header.read_text(), 'unrelated user content')

    def test_profile_rejects_unrepresentable_geometry(self):
        for name, value in (('step_mm', 0), ('arrival_mm', 5),
                            ('hub_mm', float('inf')), ('switch_hysteresis_mm', 10),
                            ('forward_transfer', 1.01), ('rewind_max_mm_s', -1)):
            profile = json.loads(BASE.read_text())
            profile['parameters'][name] = value
            path = self.root / 'bad.json'
            path.write_text(json.dumps(profile))
            with self.assertRaises(ValueError):
                model.export_header(path, self.root / 'out.h')

    def test_optional_load_geometry_exports_to_c_plant(self):
        profile = json.loads(BASE.read_text())
        path = self.root / 'load.json'
        header = self.root / 'profile.h'
        model.export_header(BASE, header)
        self.assertIn('.load_mm = 0,', header.read_text())
        profile['parameters']['load_mm'] = 0
        profile['parameter_sources']['load_mm'] = 'No local LOAD switch fitted'
        path.write_text(json.dumps(profile))
        model.export_header(path, header)
        self.assertIn('.load_mm = 0,', header.read_text())
        profile['parameters']['load_mm'] = 7
        profile['parameter_sources']['load_mm'] = 'Synthetic 7 mm LOAD switch-edge fixture'
        path.write_text(json.dumps(profile))
        model.export_header(path, header)
        self.assertIn('.load_mm = 7,', header.read_text())
        source = self.root / 'load_replay.c'
        source.write_text('''
#include <math.h>
#include "plant.h"
#include "profile.h"
int main(void) {
    mmu_sim_plant_t plant;
    if (!mmu_sim_init(&plant, &mmu_sim_profile, 0, 0)) return 1;
    if (fabs(plant.parameters.load_mm - 7) > 1e-9) return 2;
    for (int i = 0; i < 59; ++i)
        if (!mmu_sim_step(&plant, 1)) return 3;
    return plant.load_present && !plant.hub_present ? 0 : 4;
}
''')
        executable = self.root / 'load_replay'
        subprocess.run([os.environ.get('CC', 'cc'), '-std=c11', '-Wall', '-Werror',
                        '-I', str(HERE), '-I', str(self.root), str(source),
                        str(HERE / 'plant.c'), '-lm', '-o', str(executable)],
                       check=True, capture_output=True, timeout=10)
        subprocess.run([str(executable)], check=True, capture_output=True, timeout=5)
        loop = subprocess.run(['make', '-C', str(HERE), 'test-profile',
                               'PROFILE=' + str(path),
                               'BUILD_DIR=' + str(self.root / 'loop-build')],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(loop.returncode, 0, loop.stdout + loop.stderr)
        self.assertIn('local LOAD preload/load', loop.stdout)
        fitted = model.fit_travel(path, self.train, self.valid, 'load-fit', 1e-9)
        self.assertEqual(fitted['parameters']['load_mm'], 7)
        self.assertEqual(fitted['parameter_sources']['load_mm'],
                         profile['parameter_sources']['load_mm'])

    def test_invalid_load_geometry_and_missing_source_are_rejected(self):
        profile = json.loads(BASE.read_text())
        path = self.root / 'bad-load.json'
        for load in (-1, float('inf'), .25, 9.5, 10, 11):
            profile['parameters']['load_mm'] = load
            profile['parameter_sources']['load_mm'] = 'Physical switch-edge measurement'
            path.write_text(json.dumps(profile))
            with self.assertRaises(ValueError):
                model.export_header(path, self.root / 'out.h')
        del profile['parameter_sources']['load_mm']
        profile['parameters']['load_mm'] = 7
        path.write_text(json.dumps(profile))
        with self.assertRaises(ValueError):
            model.export_header(path, self.root / 'out.h')

    def test_board_switch_delivery_changes_compiled_causal_loop(self):
        profile = json.loads(BASE.read_text())
        path = self.root / 'timing.json'
        observed = []
        for delay in (200, 800):
            profile['board_timing'] = {'switch_delivery_us': delay}
            profile['board_timing_sources'] = {
                'switch_delivery_us': 'Synthetic board scheduling assumption'}
            path.write_text(json.dumps(profile))
            build = self.root / ('timing-' + str(delay))
            run = subprocess.run(['make', '-C', str(HERE), 'test-profile',
                                  'PROFILE=' + str(path), 'BUILD_DIR=' + str(build)],
                                 capture_output=True, text=True, timeout=35)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            header = (build / 'profile.h').read_text()
            self.assertIn(f'mmu_sim_switch_delivery_us = {delay}u;', header)
            self.assertIn('Profile SHA256:', header)
            self.assertIn('switched first switch acquisition=12 delivery=' +
                          str(12 + delay), run.stderr)
            observed.append(run.stderr)
        self.assertNotEqual(observed[0], observed[1])

    def test_invalid_board_switch_delivery_rejected(self):
        profile = json.loads(BASE.read_text())
        path = self.root / 'bad-timing.json'
        for value in (-1, 1.5, True, '200', 1000):
            profile['board_timing'] = {'switch_delivery_us': value}
            profile['board_timing_sources'] = {
                'switch_delivery_us': 'Synthetic board scheduling assumption'}
            path.write_text(json.dumps(profile))
            with self.assertRaisesRegex(ValueError, 'switch_delivery_us'):
                model.export_header(path, self.root / 'out.h')
        del profile['board_timing_sources']
        profile['board_timing'] = {'switch_delivery_us': 200}
        path.write_text(json.dumps(profile))
        with self.assertRaisesRegex(ValueError, 'board timing source'):
            model.export_header(path, self.root / 'out.h')

    def test_cli_profile_is_versioned_and_header_has_only_numeric_parameters(self):
        output = self.root / 'result.json'
        argv = [sys.executable, '-B', str(HERE / 'measurements.py'), 'fit-travel',
                '--base', str(BASE), '--training', str(self.train),
                '--validation', str(self.valid), '--id', 'fit-v1',
                '--tolerance-mm', '.001', '--output', str(output)]
        self.assertEqual(subprocess.run(argv, capture_output=True, timeout=5).returncode, 0)
        before = output.read_bytes()
        self.assertEqual(subprocess.run(argv, capture_output=True, timeout=5).returncode, 2)
        self.assertEqual(output.read_bytes(), before)
        model.export_header(output, self.root / 'profile.h')
        header = (self.root / 'profile.h').read_text()
        self.assertIn('.forward_transfer = 0.875,', header)
        self.assertIn('.reverse_transfer = 0.9375,', header)

    def test_fitted_trace_drives_c_plant_and_incomplete_trace_is_rejected(self):
        """An exported fit must change actual material travel, not just JSON fields."""
        profile = self.root / 'fit.json'
        profile.write_text(json.dumps(self.fit()))
        header = self.root / 'profile.h'
        model.export_header(profile, header)
        source = self.root / 'replay.c'
        source.write_text('''
#include <math.h>
#include "plant.h"
#include "profile.h"
int main(void) {
    mmu_sim_plant_t plant;
    if (!mmu_sim_init(&plant, &mmu_sim_profile, 10.0, 0.0)) return 1;
    for (int i = 0; i < 100; ++i)
        if (!mmu_sim_step(&plant, 1)) return 2;
    for (int i = 0; i < 50; ++i)
        if (!mmu_sim_step(&plant, -1)) return 3;
    /* 10 + 100 * .109375 - 50 * .1171875, without capacity clipping. */
    return fabs(plant.material_mm - 15.078125) < 1e-9
        && plant.shaft_steps == 50
        && fabs(mmu_sim_material_error_mm(&plant)) < 1e-9 ? 0 : 4;
}
''')
        executable = self.root / 'replay'
        subprocess.run([os.environ.get('CC', 'cc'), '-std=c11', '-Wall', '-Werror',
                        '-I', str(HERE), '-I', str(self.root),
                        str(source), str(HERE / 'plant.c'), '-lm', '-o', str(executable)],
                       check=True, capture_output=True, timeout=10)
        subprocess.run([str(executable)], check=True, capture_output=True, timeout=5)

        # A run with too few reverse intervals cannot yield a plausible profile.
        save_rows(self.valid, self.rows(self.valid)[:6])
        with self.assertRaisesRegex(ValueError, 'three usable reverse intervals'):
            self.fit()


if __name__ == '__main__':
    unittest.main()
