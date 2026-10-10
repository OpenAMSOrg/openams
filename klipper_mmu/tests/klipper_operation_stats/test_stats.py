# SPDX-License-Identifier: GPL-3.0-or-later
import unittest

from host.operation_stats import OperationStats
from host.protocol import Event, Identity


class OperationStatsTest(unittest.TestCase):
    def event(self, seq, operation=1, event_type=1, lane=2, instance=7):
        return Event(instance, Identity(1, 13, seq, operation, lane),
                     seq, 100, event_type, 0, 0, 0)

    def test_exact_terminals_and_bounded_serial_dedup(self):
        stats = OperationStats()
        self.assertTrue(stats.record_acked(self.event(0xffffffff)))
        self.assertFalse(stats.record_acked(self.event(0xffffffff)))
        self.assertTrue(stats.record_acked(self.event(1, event_type=2)))
        self.assertFalse(stats.record_acked(self.event(0xfffffffe)))
        self.assertFalse(stats.record_acked(self.event(2, operation=3, event_type=7)))
        self.assertFalse(stats.record_acked(self.event(2, operation=2, event_type=1)))
        self.assertTrue(stats.record_acked(self.event(2, operation=2, event_type=6)))
        self.assertEqual(stats.snapshot(), {
            ('load', 2, 'success'): 1,
            ('load', 2, 'cancel'): 1,
            ('unload', 2, 'failure'): 1})
        self.assertLessEqual(len(stats._last_sequence), 8)
        for instance in range(10, 20):
            stats.record_acked(self.event(1, instance=instance))
        self.assertLessEqual(len(stats._last_sequence), 8)
        snapshot = stats.snapshot()
        snapshot.clear()
        self.assertTrue(stats.snapshot())


if __name__ == '__main__':
    unittest.main()
