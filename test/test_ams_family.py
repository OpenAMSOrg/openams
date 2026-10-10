import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import ams_family as F
import oams_topology as T


class FamilyProfileTests(unittest.TestCase):
    def test_published_capabilities_override_family_defaults(self):
        caps, _ = F.resolve_capabilities("auto", {
            "BOARD_NAME": "ams1", "AMS_CAPABILITY_SCHEMA_VERSION": 4,
            "AMS_CAP_F1S_NORMAL_MOTION": 0,
            "AMS_CAP_HEATER_CONTROL": 0,
            "AMS_CAP_HOST_SENSOR_CONFIGURATION": 0,
            "AMS_CAP_RUNTIME_PID_TUNING": 0,
            "AMS_CAP_LOGGER_CONTROL": 0,
            "AMS_CAP_ERROR_LED_CONTROL": 0,
            "AMS_RFID_READERS": 2, "AMS_VENT_ACTUATORS": 0,
        })
        for key in ("normal_motion", "autonomous_follower", "ptfe_calibration",
                    "hub_hes_calibration", "heater_control"):
            self.assertFalse(caps[key], key)
        for key in ("host_sensor_configuration", "runtime_pid_tuning",
                    "logger_control", "error_led_control"):
            self.assertFalse(caps[key], key)
        self.assertEqual(caps["rfid_reader_count"], 2)

    def test_ams2_dryer_zones_do_not_imply_independent_controls(self):
        caps, _ = F.resolve_capabilities("ams2", {
            "AMS_CAPABILITY_SCHEMA_VERSION": 4, "AMS_DRYER_ZONES": 2,
            "AMS_CAP_F1S_NORMAL_MOTION": 1,
        })
        self.assertEqual(caps["dryer_zone_count"], 2)
        self.assertFalse(caps["dryer_independent_zones"])
        self.assertFalse(caps["spool_rotation_during_drying"])
        self.assertTrue(caps["normal_motion"])

    def test_profile_shapes(self):
        self.assertEqual(F.profile("ams1")["bays"], 4)
        self.assertEqual(F.profile("ams_ht")["bays"], 1)
        self.assertEqual(F.profile("ams2")["heater_count"], 2)
        self.assertEqual(F.profile("ams2")["dryer_module_count"], 2)
        self.assertIsNone(F.profile("ams2")["dryer_independent_zones"])
        self.assertTrue(F.profile("ams2")["spool_rotation_during_drying"])
        self.assertEqual(F.profile("AMS 2 Pro")["family"], "ams2")

    def test_auto_detects_a_one_bay_dryer_dictionary(self):
        caps, warnings = F.resolve_capabilities("auto", {
            "BOARD_NAME": "ams-ht", "AMS_BAYS": "1", "AMS_HT_DRYER": 1,
            "AMS_HT_CAP_PTFE_CALIBRATION": 1,
            "AMS_HT_CAP_HUB_HES_CALIBRATION": 0,
        })
        self.assertEqual(caps["family"], "ams_ht")
        self.assertEqual(caps["bays"], 1)
        self.assertTrue(caps["dryer"])
        self.assertTrue(caps["ptfe_calibration"])
        self.assertFalse(caps["hub_hes_calibration"])
        self.assertEqual(warnings, [])

    def test_motion_block_reason(self):
        self.assertEqual(
            F.motion_block_reason({"normal_motion": False}),
            "This firmware does not support normal filament motion")
        self.assertIsNone(F.motion_block_reason({"normal_motion": True}))
        self.assertIsNone(F.motion_block_reason({}))


    def test_auto_legacy_is_ams1(self):
        caps, warnings = F.resolve_capabilities("auto", {})
        self.assertEqual(caps["family"], "ams1")
        self.assertEqual(caps["bays"], 4)
        self.assertEqual(warnings, [])

    def test_ams2_shared_telemetry_and_explicit_no_rfid(self):
        caps, warnings = F.resolve_capabilities("auto", {
            "BOARD_NAME": "ams2", "AMS_BAYS": 4,
            "AMS_CAP_RFID_TRANSPORT": 0,
        })
        self.assertEqual(caps["family"], "ams2")
        self.assertTrue(caps["telemetry"])
        self.assertFalse(caps["rfid"])
        self.assertFalse(caps["rfid_uid_inventory"])
        self.assertEqual(warnings, [])

    def test_explicit_family_mismatch_is_reported(self):
        caps, warnings = F.resolve_capabilities(
            "ams2", {"AMS_BAYS": 1, "AMS_HT_DRYER": 1})
        self.assertEqual(caps["bays"], 4)
        self.assertEqual(len(warnings), 1)
        self.assertIn("AMS_BAYS=1", warnings[0])

class FamilyTopologyTests(unittest.TestCase):
    def test_ams_ht_has_one_valid_bay(self):
        topology = T.build_topology(
            ["fps"], [T.OamsSpec(
                name="ht", idx=1, family="ams_ht", bays=1)],
            [("T0", [("ht", 0)])])
        self.assertEqual(topology.family_of("ht"), "ams_ht")
        self.assertEqual(topology.bays_of("ht"), 1)
        with self.assertRaises(T.TopologyError):
            T.with_bay(T.with_group(topology, "BAD"), "BAD", "ht", 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
