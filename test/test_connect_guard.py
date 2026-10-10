"""H9a: the connect-time guard.

`_resolve_protocol` reads the firmware dictionary at connect. H9 turns an
unsupported protocol version, a capability schema below the floor, an
op-code/follower enum mismatch and too small a host-liveness window from log
lines into a refusal: the unit is not operated (no motion, a named reason),
though Klipper itself keeps running. Only keys the firmware actually publishes
are checked, so legacy firmware is untouched.
"""

import types
import unittest

from test_ams_ht_driver import make_driver

# A healthy AMS HT dictionary: protocol 3, schema 5, a 3000 ms window.
GOOD = {
    "OAMS_PROTOCOL_VERSION": 3,
    "AMS_CAPABILITY_SCHEMA_VERSION": 5,
    "HOST_WATCHDOG_MS": 3000,
    "BOARD_NAME": "ams-ht",
    "AMS_BAYS": 1,
}
# The op-code and follower values every host build compiles in.
_GOOD_ENUMS = {
    "OAMS_OP_CODE_SUCCESS": 0,
    "OAMS_OP_CODE_ERROR_UNSPECIFIED": 1,
    "OAMS_OP_CODE_ERROR_KLIPPER_CALL": 5,
    "OAMS_OP_CODE_CANCEL_LOAD_SPOOL": 6,
    "OAMS_OP_CODE_TIMEOUT": 7,
    "OAMS_FOLLOWER_REVERSE": 0,
    "OAMS_FOLLOWER_FORWARD": 1,
}


def resolve(consts):
    """A fresh driver whose MCU publishes `consts`, connected."""
    driver = make_driver()
    driver.mcu = types.SimpleNamespace(get_constants=lambda: dict(consts))
    driver._resolve_protocol()
    return driver


def dictionary(**bad):
    """The healthy base overrides, with the enum constants included."""
    enums = dict(_GOOD_ENUMS)
    enums.update(bad)
    return dict(GOOD, **enums)


class ConnectGuardTest(unittest.TestCase):

    def test_good_dictionary_is_not_refused(self):
        driver = resolve(dictionary())
        self.assertIsNone(driver.connect_refusal)
        self.assertEqual(driver.host_watchdog_ms, 3000)
        self.assertNotIn("connect_refusal", driver.capabilities)
        self.assertIsNot(driver.capabilities.get("normal_motion"), False)

    def test_protocol_too_new_is_refused(self):
        driver = resolve(dictionary(OAMS_PROTOCOL_VERSION=4))
        self.assertIn("unsupported OAMS_PROTOCOL_VERSION 4",
                      driver.connect_refusal)
        self.assertFalse(driver.capabilities["normal_motion"])
        self.assertTrue(driver.capabilities["connect_refusal"].startswith(
            "OpenAMS refused this unit: "))

    def test_protocol_too_old_is_refused(self):
        driver = resolve(dictionary(OAMS_PROTOCOL_VERSION=0))
        self.assertIn("unsupported OAMS_PROTOCOL_VERSION 0",
                      driver.connect_refusal)
        self.assertFalse(driver.capabilities["normal_motion"])

    def test_schema_below_floor_is_refused(self):
        driver = resolve(dictionary(AMS_CAPABILITY_SCHEMA_VERSION=3))
        self.assertIn("capability schema 3 is below the supported floor 4",
                      driver.connect_refusal)
        self.assertFalse(driver.capabilities["normal_motion"])

    def test_schema_above_host_is_only_a_warning(self):
        driver = resolve(dictionary(AMS_CAPABILITY_SCHEMA_VERSION=6))
        self.assertIsNone(driver.connect_refusal)
        self.assertNotIn("connect_refusal", driver.capabilities)

    def test_enum_mismatch_is_refused(self):
        driver = resolve(dictionary(OAMS_OP_CODE_TIMEOUT=9))
        self.assertIn("firmware protocol enum mismatch", driver.connect_refusal)
        self.assertIn("OAMS_OP_CODE_TIMEOUT: firmware=9 host=7",
                      driver.connect_refusal)
        self.assertFalse(driver.capabilities["normal_motion"])

    def test_short_watchdog_is_refused(self):
        driver = resolve(dictionary(HOST_WATCHDOG_MS=1500))
        self.assertEqual(driver.connect_refusal,
                         "HOST_WATCHDOG_MS=1500 is below 2000")
        self.assertFalse(driver.capabilities["normal_motion"])
        self.assertEqual(driver.host_watchdog_ms, 1500)

    def test_legacy_dictionary_is_not_refused(self):
        driver = resolve({})
        self.assertIsNone(driver.connect_refusal)
        self.assertIsNone(driver.host_watchdog_ms)
        self.assertNotIn("connect_refusal", driver.capabilities)

    def test_absent_watchdog_is_not_refused(self):
        consts = dictionary()
        del consts["HOST_WATCHDOG_MS"]
        driver = resolve(consts)
        self.assertIsNone(driver.connect_refusal)
        self.assertIsNone(driver.host_watchdog_ms)


if __name__ == "__main__":
    unittest.main()
