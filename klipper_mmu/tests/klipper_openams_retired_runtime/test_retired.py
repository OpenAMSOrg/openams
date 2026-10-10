# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise retired terminal transactions in the OPENAMS_SOURCE runtime."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'klipper_openams_hook'))
from test_runtime_hook import Printer, Oam, S as state, runtime_module


class RetiredTerminal(unittest.TestCase):
    def make_load(self):
        oam = Oam()
        rt = runtime_module.Runtime(Printer(), ['fps'], lambda now: state.World(),
                                    lambda index: oam if index == 1 else None)
        waiter = rt.request('fps', state.LoadBay('fps', (1, 0)))
        return rt, oam, waiter, oam.gen

    def test_successful_load_reconciles_disarmed_follower(self):
        rt, oam, waiter, gen = self.make_load()
        lane = rt.system.lanes['fps']
        rt.system = state.replace(rt.system, lanes={
            'fps': state.replace(lane, following=True)})
        before = rt.system
        for args in (((1, 1), gen, True), ((1, 0), gen + 1, True),
                     ((1, 0), gen, False)):
            self.assertFalse(rt.accept_retired_terminal(
                'fps', (1, 0), gen, 0, 0, args))
            self.assertIs(rt.system, before)
            self.assertFalse(waiter.test())
        self.assertTrue(rt.accept_retired_terminal(
            'fps', (1, 0), gen, 0, 0, ((1, 0), gen, True)))
        self.assertTrue(waiter.wait().ok)
        self.assertEqual(rt.system.lanes['fps'].op, state.OP_LOADED)
        self.assertFalse(rt.system.lanes['fps'].following)
        self.assertEqual(oam.stops, [])
        self.assertFalse(rt.accept_retired_terminal(
            'fps', (1, 0), gen, 0, 0, ((1, 0), gen, True)))

    def test_failed_unload_suppresses_only_proved_stop(self):
        rt, oam, first, gen = self.make_load()
        self.assertTrue(rt.accept_retired_terminal(
            'fps', (1, 0), gen, 0, 0, ((1, 0), gen, True)))
        oam.start_unload_spool = lambda gen=None: setattr(oam, 'gen', gen)
        waiter = rt.request('fps', state.Unload('fps'))
        gen = oam.gen
        lane = rt.system.lanes['fps']
        rt.system = state.replace(rt.system, lanes={
            'fps': state.replace(lane, following=True)})
        self.assertTrue(rt.accept_retired_terminal(
            'fps', (1, 0), gen, 7, 0, ((1, 0), gen, True)))
        self.assertEqual(waiter.wait().code, 7)
        self.assertEqual(rt.system.lanes['fps'].op, state.OP_LOADED)
        self.assertFalse(rt.system.lanes['fps'].following)
        self.assertEqual(oam.stops, [])

    def test_calibration_and_settled_waiter(self):
        rt, oam, first, gen = self.make_load()
        rt.dispatch(state.Timeout('fps'))
        self.assertFalse(rt.accept_retired_terminal(
            'fps', (1, 0), gen, 0, 0, ((1, 0), gen, True)))
        oam.start_calibrate = lambda kind, bay, gen=None: setattr(oam, 'gen', gen)
        waiter = rt.request('fps', state.Calibrate('fps', 1, 0, 'ptfe'))
        gen = oam.gen
        self.assertTrue(rt.accept_retired_terminal(
            'fps', (1, 0), gen, 7, 42, ((1, 0), gen, True)))
        self.assertEqual(waiter.wait().value, 42)
        self.assertFalse(rt.system.lanes['fps'].following)
        self.assertEqual(oam.stops, [])


if __name__ == '__main__':
    unittest.main()
