# SPDX-License-Identifier: GPL-3.0-or-later
import importlib.util
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('report_migration', Path(__file__).resolve().parents[1] / 'tools/report_migration.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class MigrationReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / '.config'
        self.target.write_text('KEEP=1\n')

    def run_report(self, source, contents):
        paths = []
        for i, content in enumerate(contents):
            path = self.root / ('input%d.cfg' % i)
            path.write_text(content)
            paths.append(path)
        original = [p.read_bytes() for p in paths]
        output = MODULE.report(paths, source)
        self.assertEqual(original, [p.read_bytes() for p in paths])
        self.assertEqual('KEEP=1\n', self.target.read_text())
        return output

    def test_happy_hare_installed_style(self):
        report = self.run_report('happy-hare', ['[mmu_unit unit0]\nvendor: BoxTurtle\nversion: 1.0\nnum_gates: 4\nstep_pin: !PA1\n'])
        self.assertEqual({'device': 'BoxTurtle', 'lane_count': 4}, report['recommendation'])
        self.assertEqual('!PA1', report['sections'][0]['entries'][3]['value'])

    def test_afc_complete_explicit_files(self):
        report = self.run_report('afc', ['[AFC_NightOwl Owl]\n',
                                         '[AFC_stepper a]\nunit: Owl:1\n[AFC_stepper b]\nunit: Owl:2\n'])
        self.assertEqual({'device': 'NightOwl', 'lane_count': 2}, report['recommendation'])
        self.assertFalse(report['includes_unresolved'])

    def test_unresolved_includes_withhold_recommendation(self):
        for source, data in (('afc', '[include extra.cfg]\n[AFC_BoxTurtle T]\n[AFC_stepper a]\nunit: T:1\n'),
                             ('happy-hare', '[include extra.cfg]\n[mmu_unit u]\nvendor: BoxTurtle\nversion: 1\nnum_gates: 4\n')):
            with self.subTest(source=source):
                report = self.run_report(source, [data])
                self.assertIsNone(report['recommendation'])
                self.assertEqual(1, len(report['includes_unresolved']))
                self.assertTrue(report['sections'])
                self.assertTrue(any('not expanded' in item for item in report['manual_review']))

    def test_fail_closed(self):
        cases = [
            ('happy-hare', '[mmu_unit u]\nvendor: Other\nversion: 1\nnum_gates: 4\n'),
            ('happy-hare', '[mmu_unit u]\nvendor: BoxTurtle\nversion: 1\nnum_gates: 9\n'),
            ('happy-hare', '[mmu_unit u]\nvendor: BoxTurtle\nversion: [[VERSION]]\nnum_gates: 4\n'),
            ('happy-hare', '[mmu_unit u]\nvendor: BoxTurtle\nvendor: NightOwl\nversion: 1\nnum_gates: 4\n'),
            ('happy-hare', '[mmu_unit u]\nvendor: BoxTurtle\nversion: 1\nnum_gates: 4\nBAD LINE\n'),
            ('afc', '[AFC_BoxTurtle T]\n[AFC_stepper a]\nunit: T:1\n[AFC_stepper b]\nunit: T:1\n'),
            ('afc', '[AFC_BoxTurtle T]\n[AFC_NightOwl N]\n[AFC_stepper a]\nunit: T:1\n'),
            ('afc', '[AFC_BoxTurtle T]\n[AFC_stepper a]\nunit: Other:1\n'),
            ('happy-hare', '[mmu_unit <UNIT_NAME>]\nvendor: BoxTurtle\nversion: 1\nnum_gates: 4\n'),
        ]
        for source, data in cases:
            with self.subTest(data=data):
                result = self.run_report(source, [data])
                self.assertIsNone(result['recommendation'])
                self.assertTrue(result['errors'])

    def test_template_section_reports_location(self):
        result = self.run_report('happy-hare', ['[mmu_unit <UNIT_NAME>]\nvendor: BoxTurtle\nversion: 1\nnum_gates: 4\n'])
        self.assertIsNone(result['recommendation'])
        self.assertEqual(1, result['placeholders_unresolved'][0]['line'])
        self.assertEqual('mmu_unit <UNIT_NAME>', result['placeholders_unresolved'][0]['section'])

    def test_legacy_alias_flag(self):
        result = self.run_report('afc', ['[AFC_BoxTurtle T]\n[mcu T]\npin_aliases: HUB=PC4, TRG1=PC5, SW1=PC4, SW2=PC5\n[AFC_stepper a]\nunit: T:1\n'])
        self.assertTrue(any('legacy HUB' in value for value in result['manual_review']))


if __name__ == '__main__':
    unittest.main()
