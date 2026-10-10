# SPDX-License-Identifier: GPL-3.0-or-later
from dataclasses import replace
import unittest

from host.openams_sensor_projection import project_sensor_presence
from host.protocol import SensorSample, SensorSnapshot


INSTANCE = 0x123456789abcdef0


def sample(present):
    return SensorSample(True, present, True, True, 100)


def snapshot():
    return SensorSnapshot(INSTANCE, 7, 3, 110,
                          (sample(True), sample(False), sample(True)),
                          sample(True), sample(False))


# src/operation_core.c's virtual_hub_lane returns "unknown" while idle on a
# multi-lane virtual-hub image (no lane owns the shared path yet -- a fresh
# boot always starts this way), so km_core_get_sensor_snapshot reports the
# hub sample itself invalid: configured=True (has_hub_sensor is always
# true) but valid=False. This is the exact shape a real EMU cold boot sends.
def invalid_hub():
    return SensorSample(True, False, False, False, 0)


class ProjectionTest(unittest.TestCase):
    def test_reports_per_bay_and_shared_facts_without_loaded_inference(self):
        result = project_sensor_presence(snapshot(), INSTANCE, 3)
        self.assertEqual(result.bay_present, (True, False, True))
        self.assertEqual(result.load_present, (None, None, None))
        self.assertTrue(result.shared_hub_present)
        self.assertFalse(result.toolhead_present)
        self.assertEqual((result.instance, result.query, result.observed_ms),
                         (INSTANCE, 7, 110))
        self.assertFalse(hasattr(result, 'hub_hes_value'))
        self.assertFalse(hasattr(result, 'hardware_snapshot_ready'))
        self.assertFalse(hasattr(result, 'path_length_mm'))

    def test_withholds_missing_stale_or_invalid_required_reading(self):
        original = snapshot()
        for change in (dict(configured=False), dict(valid=False),
                       dict(fresh=False)):
            with self.subTest(change=change):
                bay = replace(original.bays[1], **change)
                changed = replace(original, bays=(original.bays[0], bay,
                                                   original.bays[2]))
                # An unconfigured BAY is tolerated (see
                # test_unconfigured_bay_is_none_not_absent); a stale or
                # invalid configured one still withholds everything.
                if 'configured' not in change:
                    self.assertIsNone(project_sensor_presence(changed, INSTANCE, 3))
                self.assertIsNone(project_sensor_presence(
                    replace(original, hub=replace(original.hub, **change)),
                    INSTANCE, 3))
                if 'configured' not in change:
                    self.assertIsNone(project_sensor_presence(
                        replace(original, toolhead=replace(original.toolhead, **change)),
                        INSTANCE, 3))

    def test_unconfigured_bay_is_none_not_absent(self):
        original = snapshot()
        bay = SensorSample(False, True, False, False, 0)  # unconfigured
        changed = replace(original, bays=(original.bays[0], bay, original.bays[2]))
        result = project_sensor_presence(changed, INSTANCE, 3)
        self.assertIsNotNone(result)
        self.assertEqual(result.bay_present, (True, None, True))
        # A configured BAY switch must still be valid and fresh.
        for change in (dict(valid=False), dict(fresh=False)):
            stale_bay = replace(original.bays[1], **change)
            self.assertIsNone(project_sensor_presence(
                replace(original, bays=(original.bays[0], stale_bay,
                                        original.bays[2])),
                INSTANCE, 3))

    def test_all_bays_unconfigured_still_projects_hub_and_toolhead(self):
        # Angry Beaver / 3MS / QuattroBox's own shape: no BAY sensor at all.
        original = snapshot()
        none_bay = SensorSample(False, False, False, False, 0)
        result = project_sensor_presence(
            replace(original, bays=(none_bay, none_bay, none_bay)),
            INSTANCE, 3)
        self.assertIsNotNone(result)
        self.assertEqual(result.bay_present, (None, None, None))
        self.assertTrue(result.shared_hub_present)

    def test_unconfigured_toolhead_is_none_not_absent(self):
        original = snapshot()
        # FPS arrival needs no toolhead switch; its stale flags are irrelevant.
        for present in (False, True):
            toolhead = SensorSample(False, present, False, False, 0)
            result = project_sensor_presence(replace(original, toolhead=toolhead),
                                             INSTANCE, 3)
            self.assertIsNotNone(result)
            self.assertIsNone(result.toolhead_present)
            self.assertTrue(result.shared_hub_present)
        # A configured switch must still be valid and fresh.
        for change in (dict(valid=False), dict(fresh=False)):
            self.assertIsNone(project_sensor_presence(
                replace(original, toolhead=replace(original.toolhead, **change)),
                INSTANCE, 3))

    def test_withholds_identity_and_topology_mismatch(self):
        current = snapshot()
        self.assertIsNone(project_sensor_presence(current, INSTANCE + 1, 3))
        self.assertIsNone(project_sensor_presence(current, INSTANCE, 4))
        self.assertIsNone(project_sensor_presence(replace(current, lane_count=2),
                                                   INSTANCE, 2))
        self.assertIsNone(project_sensor_presence(current, True, 3))
        self.assertIsNone(project_sensor_presence(current, INSTANCE, False))

    def test_virtual_hub_derives_shared_presence_from_loads_when_hub_unknown(self):
        # A virtual hub's own reading is invalid while idle (no owner), but
        # every LOAD switch is its own directly-read sensor, unaffected by
        # ownership -- all three clear proves the shared path empty without
        # ever consulting the (here, invalid) hub sample.
        original = replace(snapshot(), hub=invalid_hub(),
                           loads=(sample(False), sample(False), sample(False)))
        result = project_sensor_presence(original, INSTANCE, 3, virtual_hub=True)
        self.assertIsNotNone(result)
        self.assertFalse(result.shared_hub_present)
        self.assertEqual(result.load_present, (False, False, False))
        # The identical snapshot without virtual_hub still needs a valid hub.
        self.assertIsNone(project_sensor_presence(original, INSTANCE, 3))

    def test_virtual_hub_any_load_present_is_shared_hub_present(self):
        original = replace(snapshot(), hub=invalid_hub(),
                           loads=(sample(False), sample(True), sample(False)))
        result = project_sensor_presence(original, INSTANCE, 3, virtual_hub=True)
        self.assertTrue(result.shared_hub_present)
        self.assertEqual(result.load_present, (False, True, False))

    def test_virtual_hub_still_withholds_an_unknown_load(self):
        original = replace(snapshot(), hub=invalid_hub(),
                           loads=(sample(False), sample(False), sample(False)))
        for change in (dict(configured=False), dict(valid=False),
                       dict(fresh=False)):
            with self.subTest(change=change):
                loads = (original.loads[0], replace(original.loads[1], **change),
                         original.loads[2])
                self.assertIsNone(project_sensor_presence(
                    replace(original, loads=loads), INSTANCE, 3, virtual_hub=True))

    def test_configured_load_readings_require_freshness(self):
        original = snapshot()
        loads = (sample(False), sample(True), sample(True)) + original.loads[3:]
        result = project_sensor_presence(replace(original, loads=loads),
                                         INSTANCE, 3)
        self.assertEqual(result.bay_present, (True, False, True))
        self.assertEqual(result.load_present, (False, True, True))
        stale = loads[:2] + (replace(loads[2], fresh=False),) + loads[3:]
        self.assertIsNone(project_sensor_presence(
            replace(original, loads=stale), INSTANCE, 3))


if __name__ == '__main__':
    unittest.main()
