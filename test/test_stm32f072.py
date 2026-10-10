"""The STM32F072 OpenAMS mainboard (AMS 1 mechanics, mainboard firmware 2.0.x).

The board speaks the legacy protocol and publishes only MCU, MCU_VERSION and
CLOCK_FREQ. It has no ams_get_identity or ams_get_environment query, so the
host takes the serial from the MCU's CAN UUID and the environment from a host
[hdc1080] sensor. Any other unit must not change.
"""

import logging
from collections import deque
import os
import sys
import types
import unittest

from test_ams_ht_driver import FakeMCU, OAMS, make_driver

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _path in (os.path.join(_ROOT, "tools"), os.path.join(_ROOT, "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import ams_family
import openams_ui

STM32_CONSTANTS = {
    "MCU": "oams-stm32f072rbt6",
    "MCU_VERSION": "1.0",
    "CLOCK_FREQ": 48000000,
    "STATS_SUMSQ_BASE": 256,
    "BUS_PINS_i2c0": "PB6,PB7",
}
UUID = "0123456789ab"
# What the firmware does not have: the two F1/F3 queries, and everything the
# other units' telemetry adds.
MISSING = ("ams_get_identity", "ams_identity", "ams_get_environment",
           "ams_environment")


class Stm32MCU(FakeMCU):
    def __init__(self, constants):
        super().__init__(missing=MISSING)
        self.constants = constants

    def get_constants(self):
        return dict(self.constants)


class FakeHdc(object):
    """An object exposing what src/hdc1080.py's get_status() does."""

    def __init__(self, temperature=23.6, humidity=41.4):
        self.status = {"temperature": temperature, "humidity": humidity}

    def get_status(self, eventtime):
        return dict(self.status)


class FakeHDC1080(FakeHdc):
    pass


FakeHDC1080.__name__ = "HDC1080"


class FakePrinter(object):
    def __init__(self, **objects):
        self.objects = dict(objects)
        self.command_error = RuntimeError

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def lookup_objects(self, module=None):
        return list(self.objects.items())


def connected_driver(constants=None, canbus_uuid=UUID, sensors=None,
                     option=None):
    """A driver connected to a board with exactly `constants`."""
    driver = make_driver()
    driver.reactor = types.SimpleNamespace(
        monotonic=lambda: 12.5, NOW=0.0, NEVER=1e30,
        register_timer=lambda cb, when: object(),
        # Klipper's one-shot callback, run at once for the test.
        register_callback=lambda cb, when=0.0: cb(12.5),
        unregister_timer=lambda timer: None)
    driver.configured_family = "auto"
    driver.device_family = "ams1"
    driver.bay_count = 4
    driver.mcu = Stm32MCU(STM32_CONSTANTS if constants is None else constants)
    driver.printer = FakePrinter(**(sensors or {}))
    driver.canbus_uuid = canbus_uuid
    driver.environment_sensor_name = option
    driver.unit_serial = None
    driver._telemetry_commands = {}
    driver._telemetry_timer = None
    driver._telemetry_index = 0
    driver._pending_bay = None
    driver._gen_queue = deque()
    driver.on_action_complete = None
    driver.ams_ht_dryer_start_cmd = None
    driver.ams_ht_dryer_stop_cmd = None
    driver.ams_ht_clear_fault_cmd = None
    driver.handle_connect()
    driver._handle_ready()
    return driver


class Stm32ConnectTest(unittest.TestCase):

    def setUp(self):
        self.driver = connected_driver()

    def test_connects_and_is_not_refused(self):
        self.assertIsNone(self.driver.connect_refusal)
        self.assertNotIn("connect_refusal", self.driver.capabilities)
        self.assertIsNone(self.driver.protocol_version)   # legacy mode
        self.assertIsNot(self.driver.capabilities.get("normal_motion"), False)

    def test_family_is_ams1_and_the_board_is_identified(self):
        self.assertEqual(self.driver.device_family, "ams1")
        status = self.driver.get_webhook_status()
        self.assertEqual(status["family"], "ams1")
        self.assertEqual(status["identity"]["board"], "oams-stm32f072")
        self.assertEqual(status["identity"]["firmware"], "1.0")

    def test_the_family_comes_from_the_mcu_string_not_the_fallback(self):
        self.assertEqual(ams_family.detect_family("auto", STM32_CONSTANTS),
                         "ams1")
        # A bay-count heuristic would have said ams_ht; the MCU wins.
        self.assertEqual(
            ams_family.detect_family(
                "auto", dict(STM32_CONSTANTS, AMS_BAYS=1)), "ams1")
        self.assertTrue(ams_family.is_stm32f072(STM32_CONSTANTS))
        self.assertFalse(ams_family.is_stm32f072({"MCU": "stm32f072rb"}))

    def test_capabilities(self):
        caps = self.driver.capabilities
        self.assertTrue(caps["hub_hes_calibration"])
        for name in ("dryer", "heater_control"):
            self.assertFalse(caps[name], name)
        self.assertEqual((caps["heater_count"], caps["fan_count"],
                          caps["vent_count"]), (0, 0, 0))
        self.assertTrue(caps["ptfe_calibration"])
        self.assertTrue(caps["autonomous_follower"])
        self.assertEqual(caps["bays"], 4)

    def test_hub_hes_calibration_is_a_supported_action(self):
        actions = self.driver.get_webhook_status()["supported_actions"]
        self.assertIn("calibrate_hub_hes", actions)
        self.assertNotIn("dryer_start", actions)


class Stm32SerialTest(unittest.TestCase):

    def test_serial_is_the_can_uuid(self):
        driver = connected_driver()
        self.assertEqual(driver.get_webhook_status()["identity"]["serial"],
                         UUID)

    def test_no_exception_log_for_the_missing_query(self):
        with self.assertLogs(level="INFO") as logs:
            connected_driver()
        self.assertFalse([r for r in logs.records if r.exc_info])
        lines = [r.getMessage() for r in logs.records
                 if "identity query" in r.getMessage()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(
            [r.levelno for r in logs.records
             if "identity query" in r.getMessage()], [logging.INFO])

    def test_neither_query_nor_uuid_keeps_serial_null(self):
        driver = connected_driver(canbus_uuid=None)
        self.assertIsNone(driver.get_webhook_status()["identity"]["serial"])

    def test_read_canbus_uuid_from_the_mcu_section(self):
        sections = {"mcu oams_mcu1": {"canbus_uuid": " abc123 "},
                    "mcu": {}}

        class Config(object):
            def __init__(self, mcu="mcu"):
                self.mcu = mcu

            def get(self, name, default=None):
                return self.mcu

            def getsection(self, name):
                return types.SimpleNamespace(
                    get=lambda key, default=None: sections[name].get(
                        key, default))

        self.assertEqual(OAMS._read_canbus_uuid(Config("oams_mcu1")),
                         "abc123")
        self.assertIsNone(OAMS._read_canbus_uuid(Config("mcu")))
        self.assertIsNone(OAMS._read_canbus_uuid(object()))


class Stm32EnvironmentTest(unittest.TestCase):

    def env(self, **kw):
        return connected_driver(**kw).get_webhook_status()["environment"]

    def test_the_sole_sensor_is_used(self):
        self.assertEqual(
            self.env(sensors={"aht3x env": FakeHDC1080()}),
            {"temp_c": 23.6, "rh_pct": 41.4, "source": "hdc1080"})

    def test_a_hdc1080_named_object_is_used(self):
        self.assertIsNotNone(self.env(sensors={"hdc1080 env": FakeHdc()}))

    def test_the_option_chooses_between_sensors(self):
        sensors = {"aht3x a": FakeHDC1080(20.0, 30.0),
                   "aht3x b": FakeHDC1080(25.0, 50.0)}
        self.assertIsNone(self.env(sensors=sensors))   # ambiguous
        self.assertEqual(self.env(sensors=sensors, option="aht3x b")["temp_c"],
                         25.0)
        self.assertEqual(self.env(sensors=sensors, option="a")["rh_pct"],
                         30.0)

    def test_an_unknown_option_or_no_sensor_is_none(self):
        self.assertIsNone(self.env())
        self.assertIsNone(self.env(sensors={"aht3x a": FakeHDC1080()},
                                   option="missing"))

    def test_a_sensor_without_a_reading_is_none(self):
        self.assertIsNone(
            self.env(sensors={"aht3x a": FakeHDC1080(0.0, 0.0)}))

    def test_an_unrelated_object_is_not_a_sensor(self):
        self.assertIsNone(self.env(sensors={"gcode": object()}))


class OtherBoardsUnchangedTest(unittest.TestCase):
    """Any other unit keeps its firmware identity and environment."""

    def test_other_board_constants_keep_their_family(self):
        self.assertEqual(ams_family.detect_family(
            "auto", {"MCU": "example-mcu", "BOARD_NAME": "ams-ht"}), "ams_ht")
        caps, _ = ams_family.resolve_capabilities(
            "auto", {"MCU": "example-mcu", "BOARD_NAME": "ams1",
                     "AMS_HT_CAP_HUB_HES_CALIBRATION": 0})
        self.assertFalse(caps["hub_hes_calibration"])
        self.assertEqual(caps["board_name"], "ams1")


if __name__ == "__main__":
    unittest.main()
