"""H16 driver side of per-bay autonomous positioning.

The firmware positions a freshly inserted spool on its own and answers
load/unload with Busy meanwhile.  The host cannot see the edge directly: it
infers it from the F1S presence snapshot, then holds the bay as positioning
until a domain sample received after the edge says the unit is not
positioning.
"""

import unittest

from test_ams_ht_driver import make_driver


def positioning_driver():
    """A 4-bay AMS2 driver with an empty telemetry cache."""
    driver = make_driver()
    driver.bay_count = 4
    driver.device_family = "ams2"
    driver.capabilities["telemetry"] = True
    driver.hardware_snapshot_ready = False
    driver._telemetry = {}
    return driver


class PositioningTest(unittest.TestCase):

    def setUp(self):
        self.driver = positioning_driver()

    def _sample(self, name, timestamp):
        self.driver._telemetry = {
            "domain": {"motion_state_name": name, "timestamp": timestamp}}

    def test_first_snapshot_is_not_an_edge(self):
        self.driver._note_f1s([1, 1, 0, 0], 9.0)
        self.assertEqual(self.driver.positioning_bays(9.5), frozenset())

    def test_edge_is_pending_until_a_newer_sample(self):
        self.driver.hardware_snapshot_ready = True
        self.driver._note_f1s([1, 1, 1, 0], 10.0)
        # No domain sample yet: hold the edge bay through the poll gap.
        self.assertEqual(self.driver.positioning_bays(10.1), frozenset((2,)))
        # A sample older than the edge has not seen it either.
        self._sample("idle", 9.5)
        self.assertEqual(self.driver.positioning_bays(10.6), frozenset((2,)))
        # The domain now reports the positioning itself.
        self._sample("positioning", 11.0)
        self.assertEqual(self.driver.positioning_bays(11.0), frozenset((2,)))
        # A sample after the edge that says not positioning clears it.
        self._sample("idle", 12.0)
        self.assertEqual(self.driver.positioning_bays(12.0), frozenset())

    def test_pending_expires_without_a_sample(self):
        self.driver.hardware_snapshot_ready = True
        self.driver._note_f1s([0, 0, 0, 0], 19.0)
        self.driver._note_f1s([0, 0, 1, 0], 20.0)
        self.assertEqual(self.driver.positioning_bays(22.9), frozenset((2,)))
        self.assertEqual(self.driver.positioning_bays(23.1), frozenset())

    def test_positioning_without_a_known_edge_is_conservative(self):
        # No edge was observed (e.g. the driver started mid-positioning): fall
        # back to every bay that reports filament present.
        self.driver.f1s_hes_value = [1, 1, 0, 0]
        self._sample("positioning", 11.0)
        self.assertEqual(
            self.driver.positioning_bays(11.5), frozenset((0, 1)))

    def test_normal_motion_false_never_positions(self):
        self.driver.hardware_snapshot_ready = True
        self.driver.capabilities["normal_motion"] = False
        self.driver._note_f1s([0, 0, 0, 0], 19.0)
        self.driver._note_f1s([0, 0, 1, 0], 20.0)
        self.assertEqual(self.driver.positioning_bays(20.1), frozenset())

    def test_status_entry_publishes_positioning(self):
        self.driver.hardware_snapshot_ready = True
        self.driver._note_f1s([1, 1, 1, 0], 10.0)
        entry = self.driver._bay_status_entry(2, True, False)
        self.assertTrue(entry["positioning"])
        other = self.driver._bay_status_entry(0, True, False)
        self.assertFalse(other["positioning"])


if __name__ == "__main__":
    unittest.main()
