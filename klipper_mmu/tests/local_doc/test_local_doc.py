# SPDX-License-Identifier: GPL-3.0-or-later
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
import local_doc  # noqa: E402

REFERENCE = (ROOT / 'docs/g-code-commands.md').read_text()


def section_of(md, cmd):
    """Span of the command's own section: its heading, or its table row."""
    m = (re.search(r'(?m)^#+ .*`KLIPPER_MMU_%s`.*$' % cmd, md)
         or re.search(r'(?m)^\|.*`KLIPPER_MMU_%s\b.*$' % cmd, md))
    if m.group(0).startswith('|'):
        return m.start(), m.end()
    end = md.find('\n#', m.end())
    return m.start(), end if end != -1 else len(md)


def mutate(cmd, old, new, count=1):
    """The reference with one change inside one command's section."""
    start, end = section_of(REFERENCE, cmd)
    section = REFERENCE[start:end]
    assert old in section, (cmd, old)
    return REFERENCE[:start] + section.replace(old, new, count) + REFERENCE[end:]


class ExtractionTest(unittest.TestCase):
    def test_facts_come_from_the_code(self):
        names = local_doc.commands()
        self.assertEqual(len(names), 28)
        self.assertIn("config.getboolean('path_reconciliation'", local_doc.registration('RECONCILE_PATH'))
        self.assertIn('Registered unconditionally', local_doc.registration('QUERY'))
        self.assertEqual(local_doc.config_key('TOOL_MAP'), 'tool_mapping_file')
        self.assertEqual(local_doc.config_key('EJECT'), 'openams_motion')
        self.assertIsNone(local_doc.config_key('DIAGNOSE'))
        self.assertEqual(local_doc.required('TOOL_MAP'), {'TOOL'})
        self.assertEqual(local_doc.required('COMMISSION'), set())
        self.assertIn('CANCEL does not take BAY', local_doc.rejections('EJECT'))
        self.assertEqual(local_doc.params('TERMINAL')['OPERATION'], (1, 4))
        self.assertTrue({'NAME', 'MATERIAL', 'COLOR', 'AVAILABLE'} <= set(local_doc.params('SET_GATE_METADATA')))
        self.assertIn('WARNING', local_doc.params('MAINTENANCE'))
        terminal = local_doc.facts('TERMINAL', names)
        self.assertIn('3=FOLLOW', terminal)
        self.assertIn('1=HOST', terminal)
        self.assertIn('0 <= bay < self.bay_count', local_doc.facts('PRELOAD', names))

    def test_fix_examples_touches_only_invocations(self):
        cases = [
            ('`KLIPPER_MMU_CALIBRATE KLIPPER_MMU_CALIBRATE MMU=box BAY=0`', '`KLIPPER_MMU_CALIBRATE MMU=box BAY=0`'),
            ('KLIPPER_MMU_EJECT BAY=2', 'KLIPPER_MMU_EJECT MMU=box BAY=2'),
            ('`KLIPPER_MMU_EJECT MMU=box BAY=2`', '`KLIPPER_MMU_EJECT MMU=box BAY=2`'),
            ('Run `KLIPPER_MMU_DIAGNOSE` first.', 'Run `KLIPPER_MMU_DIAGNOSE` first.'),
            ('### `KLIPPER_MMU_EJECT` — Everyday', '### `KLIPPER_MMU_EJECT` — Everyday'),
            ('**Example:** `KLIPPER_MMU_QUERY`', '**Example:** `KLIPPER_MMU_QUERY MMU=box`'),
            ('Use `KLIPPER_MMU_EJECT BAY=1` now.', 'Use `KLIPPER_MMU_EJECT MMU=box BAY=1` now.'),
            ('### `KLIPPER_MMU_CALIBRATE KLIPPER_MMU_CALIBRATE`', '### `KLIPPER_MMU_CALIBRATE`'),
        ]
        for given, want in cases:
            with self.subTest(given=given):
                self.assertEqual(local_doc.fix_examples(given, 'box'), want)


class CheckTest(unittest.TestCase):
    def test_committed_reference_matches_the_code(self):
        self.assertEqual(local_doc.check(REFERENCE), [])

    def test_each_error_class_is_caught(self):
        cases = [
            ('invented command', REFERENCE + '\n### `KLIPPER_MMU_DIAG_COUNTERS` — Advanced\n',
             'DIAG_COUNTERS', 'not a registered command'),
            ('missing command', REFERENCE.replace('KLIPPER_MMU_STATS', 'KLIPPER_MMU_STATUS'),
             'STATS', 'missing'),
            ('wrong enum value', mutate('TERMINAL', '3=FOLLOW', '3=CALIBRATE'),
             'TERMINAL', 'OPERATION 3=CALIBRATE is wrong'),
            ('example without MMU=', mutate('EJECT', 'MMU=box BAY=0', 'BAY=0'),
             'EJECT', 'no MMU='),
            ('unknown parameter', mutate('EJECT', 'MMU=box BAY=0', 'MMU=box LANE=0'),
             'EJECT', 'unknown parameter LANE'),
            ('value out of range', mutate('RESTART_DRIVER', 'LANE=2', 'LANE=9'),
             'RESTART_DRIVER', 'LANE=9 outside 0..7'),
            ('unquoted value with spaces', mutate('MAINTENANCE', 'INCR=1', 'WARNING=Replace blade'),
             'MAINTENANCE', "stray word 'blade'"),
            ('option not named', mutate('RECONCILE_PATH', '`path_reconciliation`', '`something_else`'),
             'RECONCILE_PATH', 'does not name its option path_reconciliation'),
            ('required shown optional', mutate('CALIBRATE', '- `BAY`: integer', '- `BAY`: optional integer'),
             'CALIBRATE', 'BAY shown as optional'),
            ('MMU shown optional', mutate('CALIBRATE', '**Parameters:**', '**Parameters:**\n- `MMU`: optional'),
             'CALIBRATE', 'MMU shown as optional'),
            ('forbidden term', mutate('RECOVER_LOCAL', 'Advanced', 'Advanced (retired)'),
             'RECOVER_LOCAL', 'forbidden term "retired"'),
        ]
        for label, doc, cmd, expected in cases:
            with self.subTest(label):
                found = [msg for c, msg in local_doc.check(doc) if c == cmd and expected in msg]
                self.assertTrue(found, local_doc.check(doc))


class PlainLanguageTest(unittest.TestCase):
    def test_rewrites_are_kept_only_when_checked(self):
        md = ('### `KLIPPER_MMU_RECOVER_LOCAL` — Advanced\n'
              'Clears a retired result for `KLIPPER_MMU_PRELOAD`.\n'
              'Drops the retired waiter.\n'
              'Nothing to change here.\n')
        answers = {
            'Clears a retired result for `KLIPPER_MMU_PRELOAD`.':
                'Clears a leftover result for `KLIPPER_MMU_PRELOAD`.',
            'Drops the retired waiter.': 'Drops the old retired request.',  # still forbidden
        }
        def fake(text, temperature=0.6):
            return answers[text.rsplit('\n', 1)[1]]
        with patch.object(local_doc, 'ask', fake):
            out = local_doc.plain_language(md).splitlines()
        self.assertEqual(out[0], '### `KLIPPER_MMU_RECOVER_LOCAL` — Advanced')
        self.assertEqual(out[1], 'Clears a leftover result for `KLIPPER_MMU_PRELOAD`.')
        self.assertEqual(out[2], 'Drops the retired waiter.')
        self.assertEqual(out[3], 'Nothing to change here.')


class DraftTest(unittest.TestCase):
    def test_keeps_the_run_with_fewest_failures_and_never_overwrites(self):
        bad = mutate('TERMINAL', '3=FOLLOW', '3=CALIBRATE')
        answers = iter([bad, REFERENCE, bad])
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(local_doc, 'ask', lambda text, temperature=0.6: next(answers)), \
                patch('builtins.print'):
            out = Path(directory) / 'draft.md'
            local_doc.draft(out, 3, 'box', force=False)
            self.assertEqual(out.read_text(), REFERENCE)
            report = out.with_name('draft.md.report').read_text()
            self.assertIn('check failures per run: [', report)  # runs finish in any order
            self.assertIn('kept run ', report)
            self.assertIn('no check failures', report)
            with self.assertRaises(SystemExit):
                local_doc.draft(out, 1, 'box', force=False)


if __name__ == '__main__':
    unittest.main()
