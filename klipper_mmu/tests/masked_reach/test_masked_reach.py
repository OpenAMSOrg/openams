# SPDX-License-Identifier: GPL-3.0-or-later
"""tools/masked_reach.py on a hand-built objdump fixture: a call cycle, a tail
call (b.w), an indirect blx, a VFP instruction, soft-float and 64-bit divide
helpers, and an unreachable function that calls float code."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'tools'))
from masked_reach import analyze  # noqa: E402

FIXTURE = (HERE / 'fixture.dis').read_text()


class MaskedReachTest(unittest.TestCase):
    def test_fixture_metrics(self):
        self.assertEqual(analyze(FIXTURE, ['root_isr']), {
            'functions': 8, 'instructions': 27, 'float_sites': 3, 'div64_sites': 1,
            'hw_div': 1, 'indirect_sites': 1, 'longest_chain': 20,
            'reachable': ['__aeabi_ddiv', '__aeabi_dmul', '__aeabi_uldivmod', 'helper_b',
                          'helper_c', 'helper_d', 'helper_e', 'root_isr']})

    def test_other_root_and_tail_call(self):
        got = analyze(FIXTURE, ['helper_d'])
        self.assertEqual((got['reachable'], got['instructions'], got['float_sites'],
                          got['longest_chain']), (['helper_d', 'helper_e'], 4, 1, 4))

    def test_every_indirect_site_counts(self):
        two = FIXTURE.replace(' 8000108:\tblx\tr3\n', ' 8000108:\tblx\tr3\n 8000109:\tbx\tr2\n')
        self.assertEqual(analyze(two, ['root_isr'])['indirect_sites'], 2)

    def test_roots(self):
        with self.assertRaises(ValueError):
            analyze(FIXTURE, ['no_such_function'])
        clone = FIXTURE.replace('<helper_d>', '<helper_d.lto_priv.0>')
        self.assertEqual(analyze(clone, ['helper_d'])['reachable'],
                         ['helper_d.lto_priv.0', 'helper_e'])


if __name__ == '__main__':
    unittest.main()
