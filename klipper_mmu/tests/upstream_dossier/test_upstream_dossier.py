# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/upstream_dossier.py that run under plain system Python
(no Happy Hare, no HH_PYTHON): --help, Markdown rendering from a fixture,
the self-consistency checker, and check's diff detection (with run_trace
mocked out, since a real trace needs the extracted archive)."""
import argparse
import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
import upstream_dossier as ud  # noqa: E402

FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'sample.json').read_text())


def raw_from_dossier(d):
    """Invert reduce_dossier(): rebuild the "raw" shape run_trace() would
    have returned, so check() can be tested with run_trace mocked."""
    operations = {}
    for name, op in d['operations'].items():
        end = op['end']
        operations[name] = {'moves': op['moves'], 'final_tip': end['tip'],
                            'filament_pos': end['filament_pos'], 'gate_status': end['gate_status'],
                            'errors': op['errors']}
    return {'ok': True, 'syms': d['source']['syms'], 'layout': d['source']['layout'],
            'hardware': d['hardware'], 'parameters': d['parameters'], 'device': d['device'],
            'steps_per_mm': d['steps_per_mm'], 'operations': operations}


class TestHelp(unittest.TestCase):
    def test_help(self):
        for args in (['--help'], ['trace', '--help'], ['md', '--help'], ['check', '--help']):
            proc = subprocess.run([sys.executable, '-B', str(ROOT / 'tools' / 'upstream_dossier.py'),
                                   *args], capture_output=True, text=True, timeout=20)
            self.assertEqual(proc.returncode, 0, proc.stderr)


class TestRenderMd(unittest.TestCase):
    def test_contains_operation_tables_and_mapping(self):
        md = ud.render_md(FIXTURE, 'fixture')
        self.assertIn('### PRELOAD', md)
        self.assertIn('### SELECT', md)
        self.assertIn('Initial homing to mmu_exit_0 sensor', md)
        self.assertIn('_No motion (already in the requested state)._', md)
        # every mapping status word appears somewhere in the rendered table
        for row in ud.MAPPING:
            self.assertIn(row[0], md)
        self.assertIn('mapped', md)
        self.assertIn('## Decisions', md)
        self.assertIn('fixture note for the Decisions section', md)


class TestSelfConsistency(unittest.TestCase):
    def test_accepts_the_fixture(self):
        self.assertEqual(ud.check_self_consistency(FIXTURE), [])

    def test_rejects_actual_off_by_one_mm(self):
        broken = copy.deepcopy(FIXTURE)
        broken['operations']['preload']['moves'][0]['actual'] += 1.0
        violations = ud.check_self_consistency(broken)
        self.assertTrue(any('tip delta' in v for v in violations), violations)

    def test_rejects_homing_endstop_not_in_layout(self):
        broken = copy.deepcopy(FIXTURE)
        move = broken['operations']['preload']['moves'][0]
        move['endstop'] = 'nonexistent_sensor'
        move['endstop_pos'] = None
        violations = ud.check_self_consistency(broken)
        self.assertTrue(any('not resolvable' in v for v in violations), violations)

    def test_rejects_both_in_one_fixture(self):
        broken = copy.deepcopy(FIXTURE)
        broken['operations']['preload']['moves'][0]['actual'] += 1.0
        move = broken['operations']['select'] = copy.deepcopy(FIXTURE['operations']['preload'])
        move['moves'][0]['endstop'] = 'nonexistent_sensor'
        move['moves'][0]['endstop_pos'] = None
        violations = ud.check_self_consistency(broken)
        self.assertTrue(any('tip delta' in v for v in violations), violations)
        self.assertTrue(any('not resolvable' in v for v in violations), violations)

    def test_accepts_a_compound_endstop_that_halted_on_a_leg(self):
        """ViViD's `preload_compound` is HH's first-wins gate-switch/NFC union
        (test/hh/klippy_root/extras/homing.py _expand), so it has no single
        layout name; its gate-switch leg (parameters.gate_homing_endstop) is
        where the tip is allowed to stop."""
        d = copy.deepcopy(FIXTURE)
        d['source']['layout']['mmu_nfc'] = -80.0
        d['parameters']['gate_homing_endstop'] = 'mmu_exit'
        move = d['operations']['preload']['moves'][0]
        move['endstop'] = 'preload_compound'
        move['endstop_pos'] = None
        self.assertEqual(ud.check_self_consistency(d), [])

    def test_rejects_a_compound_endstop_that_halted_off_its_legs(self):
        d = copy.deepcopy(FIXTURE)
        d['source']['layout']['mmu_nfc'] = -80.0
        d['parameters']['gate_homing_endstop'] = 'mmu_exit'
        move = d['operations']['preload']['moves'][0]
        move.update({'endstop': 'preload_compound', 'endstop_pos': None,
                     'tip_after': 50.0, 'actual': 90.0})
        violations = ud.check_self_consistency(d)
        self.assertTrue(any('not resolvable' in v for v in violations), violations)

    def test_accepts_an_already_satisfied_no_travel_homing(self):
        """ViViD parks FORWARD of its exit switch (PARAM_GATE_PARKING_DISTANCE
        default +10, vs -25 for Low Rider/Pico MMU), so LOAD's initial homing
        is a no-op and the tip stays at the park, 10 mm past mmu_exit_0."""
        d = copy.deepcopy(FIXTURE)
        move = d['operations']['preload']['moves'][0]
        move.update({'actual': 0.0, 'tip_before': 10.0, 'tip_after': 10.0})
        self.assertEqual(ud.check_self_consistency(d), [])

    def test_still_rejects_a_homing_move_that_travelled_and_missed(self):
        d = copy.deepcopy(FIXTURE)
        move = d['operations']['preload']['moves'][0]
        move.update({'tip_before': 10.0, 'tip_after': 20.0, 'actual': 10.0})
        violations = ud.check_self_consistency(d)
        self.assertTrue(any('homed to' in v for v in violations), violations)


class TestCheckDiff(unittest.TestCase):
    """check() reports a diff on a modified copy. run_trace() is mocked so
    this needs neither the extracted archive nor HH_PYTHON."""

    def _write_committed(self, devices_dir, dossier):
        devices_dir.mkdir(parents=True)
        (devices_dir / 'fixture.json').write_text(json.dumps(dossier, indent=2) + '\n')
        (devices_dir / 'fixture.md').write_text(ud.render_md(dossier, 'fixture'))

    def test_no_diff_then_a_diff_after_modification(self):
        with self.subTest('clean'):
            with patch.object(ud, 'run_trace', return_value=raw_from_dossier(FIXTURE)), \
                 patch.dict(ud.DEVICES, {'fixture': {'profile': 'fixture'}}), \
                 self._tmp_root() as root:
                self._write_committed(root / 'docs' / 'devices', FIXTURE)
                with patch.object(ud, 'ROOT', root):
                    rc = ud.cmd_check(argparse.Namespace(devices=['fixture']))
                self.assertEqual(rc, 0)

        modified = copy.deepcopy(FIXTURE)
        modified['steps_per_mm'] += 1.0
        with self.subTest('modified'):
            with patch.object(ud, 'run_trace', return_value=raw_from_dossier(FIXTURE)), \
                 patch.dict(ud.DEVICES, {'fixture': {'profile': 'fixture'}}), \
                 self._tmp_root() as root:
                self._write_committed(root / 'docs' / 'devices', modified)
                with patch.object(ud, 'ROOT', root):
                    rc = ud.cmd_check(argparse.Namespace(devices=['fixture']))
                self.assertEqual(rc, 1)

    @staticmethod
    def _tmp_root():
        import tempfile
        class _Ctx:
            def __enter__(self):
                self._td = tempfile.TemporaryDirectory()
                return Path(self._td.name)
            def __exit__(self, *exc):
                self._td.cleanup()
        return _Ctx()


if __name__ == '__main__':
    unittest.main()
