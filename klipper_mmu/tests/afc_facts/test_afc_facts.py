# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/afc_facts.py: plain-INI AFC facts and the dossier compare.

No AFC or Klipper code runs here; only the synthetic fixtures beside this
file and tools/afc_facts.py itself.
"""
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2] / 'tools'
FIXTURES = Path(__file__).resolve().parent / 'fixtures'

SPEC = importlib.util.spec_from_file_location('afc_facts', TOOLS / 'afc_facts.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

TEMPLATE = FIXTURES / 'synthetic_template.cfg'
SHARED_DRIVE_TEMPLATE = FIXTURES / 'synthetic_shared_drive_template.cfg'
MCU = FIXTURES / 'synthetic_mcu.cfg'
DOSSIER = FIXTURES / 'fixture_dossier.json'
DOSSIER_NO_DECISION = FIXTURES / 'fixture_dossier_no_decision.json'


def run_cli(*args, timeout=20):
    return subprocess.run([sys.executable, '-B', str(TOOLS / 'afc_facts.py'), *args],
                          capture_output=True, text=True, timeout=timeout)


class HelpTests(unittest.TestCase):
    def test_help_exits_clean(self):
        result = run_cli('--help')
        self.assertEqual(0, result.returncode)
        self.assertIn('facts', result.stdout)
        self.assertIn('compare', result.stdout)


class FactsTests(unittest.TestCase):
    def facts(self):
        return MODULE.build_facts(TEMPLATE, MCU, None)

    def test_lane_count_and_class(self):
        facts = self.facts()
        self.assertEqual(2, facts['lane_count'])
        self.assertEqual({'class': 'AFC_BoxTurtle', 'name': 'Synth_1'}, facts['unit'])

    def test_alias_resolved_and_polarity_kept(self):
        facts = self.facts()
        prep = facts['lanes'][0]['prep']
        load = facts['lanes'][0]['load']
        # ^! kept: pullup and inverted both true, resolved through the mcu alias table.
        self.assertEqual('PB0', prep['resolved'])
        self.assertTrue(prep['pullup'])
        self.assertTrue(prep['inverted'])
        self.assertEqual('^!Synth_1:TRG1', prep['raw'])
        # ^ only: pullup true, not inverted.
        self.assertEqual('PB1', load['resolved'])
        self.assertTrue(load['pullup'])
        self.assertFalse(load['inverted'])

    def test_virtual_hub_detected(self):
        facts = self.facts()
        self.assertEqual('virtual', facts['hub']['switch_pin'])

    def test_switched_buffer_detected(self):
        facts = self.facts()
        self.assertEqual('switched', facts['buffer']['type'])

    def test_unit_level_speed_overrides_missing_afc_cfg(self):
        facts = self.facts()
        self.assertEqual({'value': '100', 'source': 'unit'}, facts['speeds']['long_moves_speed'])
        self.assertNotIn('short_moves_speed', facts['speeds'])

    def test_pin_without_mcu_stays_unresolved(self):
        facts = MODULE.build_facts(TEMPLATE, None, None)
        prep = facts['lanes'][0]['prep']
        self.assertTrue(prep['unresolved'])
        self.assertEqual('Synth_1:TRG1', prep['resolved'])

    def test_cli_prints_valid_json(self):
        result = run_cli('facts', str(TEMPLATE), '--mcu', str(MCU))
        self.assertEqual(0, result.returncode)
        data = json.loads(result.stdout)
        self.assertEqual(2, data['lane_count'])

    def test_shared_drive_lane_count_uses_afc_lane_sections(self):
        # ViViD/Claymore-shaped: two [AFC_stepper ...] sections are the
        # shared drive/selector motors, not lanes; the four [AFC_lane laneN]
        # sections are the real lanes, so lane_count must be 4, not 2.
        facts = MODULE.build_facts(SHARED_DRIVE_TEMPLATE, None, None)
        self.assertEqual(4, facts['lane_count'])
        self.assertEqual(2, len(facts['lanes']))  # the drive and selector motors


class RealArchiveFactsTests(unittest.TestCase):
    """Exercise real pinned-archive files when the extracted AFC tree is
    present; skip (not fail) otherwise, since extraction is on-demand."""

    def afc_root(self):
        common = subprocess.run(
            ['git', '-C', str(Path(__file__).resolve().parents[2]), 'rev-parse',
             '--path-format=absolute', '--git-common-dir'],
            capture_output=True, text=True, timeout=10).stdout.strip()
        main_root = Path(common).parent if common else Path(__file__).resolve().parents[2]
        return main_root.parent / 'build' / 'upstream' / 'afc-5ea84ea'

    def test_emu_template(self):
        root = self.afc_root()
        emu = root / 'templates' / 'EMU.cfg'
        if not emu.is_file():
            self.skipTest('extracted AFC archive not present at %s' % root)
        facts = MODULE.build_facts(emu, root / 'config' / 'mcu' / 'EMU_SLB.cfg', None)
        prep = facts['lanes'][0]['prep']
        load = facts['lanes'][0]['load']
        self.assertEqual('PA1', prep['resolved'])
        self.assertTrue(prep['inverted'])
        self.assertEqual('PA0', load['resolved'])
        self.assertFalse(load['inverted'])
        self.assertEqual('virtual', facts['hub']['switch_pin'])
        self.assertEqual({'value': '250', 'source': 'unit'}, facts['speeds']['long_moves_speed'])
        self.assertEqual('50', facts['speeds']['short_moves_speed']['value'])
        self.assertEqual('switched', facts['buffer']['type'])
        # Documented, evidenced known gap: the pinned EMU.cfg is a "single-lane
        # reference template" (its own header comment) -- the installer
        # substitutes lane numbers at install time, so a plain INI read of
        # this file sees 1 lane, not the 5 gates Happy Hare's PARAM_NUM_GATES
        # defines. See tools/afc_facts.py module docstring / the brief report.
        self.assertEqual(1, facts['lane_count'])


class CompareTests(unittest.TestCase):
    def test_marks_agree_differ_afc_only(self):
        facts = MODULE.build_facts(TEMPLATE, MCU, None)
        dossier = json.loads(DOSSIER.read_text())
        values = MODULE.concept_values(facts, dossier)
        statuses = {c: MODULE.classify(*values[c]) for c in MODULE.CONCEPTS}
        self.assertEqual('agree', statuses['lane_count'])
        self.assertEqual('agree', statuses['entry_sensor'])
        self.assertEqual('agree', statuses['exit_sensor'])
        self.assertEqual('agree', statuses['sensor_polarity'])
        self.assertEqual('agree', statuses['buffer_type'])
        self.assertEqual('differ', statuses['unload_park'])
        self.assertEqual('afc-only', statuses['hub_sensor'])
        self.assertEqual('afc-only', statuses['bowden'])
        # eject_extra must come from HH's gate_final_eject_distance parameter,
        # never from operations.eject.end.offset (an eject END POSITION
        # relative to mmu_entry, not a distance -- the bug this table fixes).
        self.assertEqual('hh-only', statuses['eject_extra'])
        self.assertEqual(100, int(values['eject_extra'][1]))

    def test_cli_exits_zero_when_differ_is_decided(self):
        result = run_cli('compare', str(DOSSIER), str(TEMPLATE), '--mcu', str(MCU))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('| unload_park |', result.stdout)
        self.assertIn('differ', result.stdout)

    def test_cli_exits_one_when_differ_is_undecided(self):
        result = run_cli('compare', str(DOSSIER_NO_DECISION), str(TEMPLATE), '--mcu', str(MCU))
        self.assertEqual(1, result.returncode)
        self.assertIn('Undecided', result.stdout)
        self.assertIn('unload_park', result.stdout)


class EmuDossierCompareTests(unittest.TestCase):
    """Compare the committed docs/devices/emu.json against the pinned AFC EMU
    template and assert the genuine differences (main-session problem report,
    build/b2/sonnet_oracle_integration.txt): once the mis-joins are fixed
    (MCU-prefixed pins, boolean 'bowden', the eject END POSITION mistaken for
    a distance, signed-vs-unsigned park offsets), these five concepts still
    genuinely differ and the rest genuinely agree/afc-only/hh-only. Skips if
    the dossier or the extracted AFC archive is not present."""

    def afc_root(self):
        checkout_root = Path(__file__).resolve().parents[2]
        common = subprocess.run(
            ['git', '-C', str(checkout_root), 'rev-parse', '--path-format=absolute',
             '--git-common-dir'], capture_output=True, text=True, timeout=10).stdout.strip()
        main_root = Path(common).parent if common else checkout_root
        return main_root.parent / 'build' / 'upstream' / 'afc-5ea84ea'

    def test_real_emu_dossier_genuine_differences(self):
        checkout_root = Path(__file__).resolve().parents[2]
        dossier_path = checkout_root / 'docs' / 'devices' / 'emu.json'
        if not dossier_path.is_file():
            self.skipTest('docs/devices/emu.json not yet produced by the upstream_dossier brief')
        afc_root = self.afc_root()
        emu_template = afc_root / 'templates' / 'EMU.cfg'
        if not emu_template.is_file():
            self.skipTest('extracted AFC archive not present at %s' % afc_root)
        dossier = json.loads(dossier_path.read_text())
        facts = MODULE.build_facts(emu_template, afc_root / 'config' / 'mcu' / 'EMU_SLB.cfg', None)
        values = MODULE.concept_values(facts, dossier)
        statuses = {c: MODULE.classify(*values[c]) for c in MODULE.CONCEPTS}
        self.assertEqual(set(MODULE.CONCEPTS), set(statuses))

        # Genuine differences that must still appear after the fix.
        self.assertEqual('differ', statuses['sensor_polarity'])  # ^! (AFC) vs ^ (HH)
        self.assertEqual('differ', statuses['short_speed'])      # 50/300 (AFC) vs 80/80 (HH)
        self.assertEqual('differ', statuses['unload_park'])      # 5mm (AFC) vs 1mm (HH), magnitude
        self.assertEqual('differ', statuses['buffer_type'])      # switched (AFC) vs analog (HH)
        self.assertEqual('differ', statuses['lane_count'])       # 1 (single-lane template) vs 5 (HH)

        # The mis-joins the brief reports must be gone: MCU-prefixed pins
        # resolve to the same bare pin (agree, not differ); a boolean/eject
        # end-position/signed-offset must not surface as a false differ.
        self.assertEqual('agree', statuses['entry_sensor'])   # PA1 both sides once the prefix is stripped
        self.assertEqual('agree', statuses['exit_sensor'])    # PA0 both sides
        self.assertEqual('agree', statuses['long_speed'])     # 250/120 both sides
        self.assertEqual('afc-only', statuses['bowden'])       # no genuine HH length to compare
        self.assertEqual('hh-only', statuses['eject_extra'])   # AFC's extruder_clear_dis unset in this template

        # compare's own decisions gate: every one of the above 'differ' rows
        # must have a matching entry in emu.json's "decisions" list, so the
        # CLI (and the Makefile gate that runs it) exits 0.
        result = run_cli('compare', str(dossier_path), str(emu_template),
                         '--mcu', str(afc_root / 'config' / 'mcu' / 'EMU_SLB.cfg'))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
