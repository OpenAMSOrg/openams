"""A failed op names the unit's latched fault and the recovery steps."""

import importlib
import os
import sys
import types
import unittest

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if "oamspkg" not in sys.modules:
    pkg = types.ModuleType("oamspkg")
    pkg.__path__ = [_SRC]
    sys.modules["oamspkg"] = pkg
sys.modules.setdefault("mcu", types.ModuleType("mcu"))
S = importlib.import_module("oamspkg.oams_state")
OAMS = importlib.import_module("oamspkg.oams").OAMS

FPS = "fps1"
HINT = ("the unit reports fault motor_drive_fault; clear it with "
        "OAMS_CLEAR_FAULT OAMS=2, then OAMSM_CLEAR_ERRORS")


def unloading_failure(fault):
    lane = S.LaneState(op=S.OP_UNLOADING, unit=(2, 0), group="T0")
    system = S.SystemState(lanes={FPS: lane})
    action = S.OpCompleted(FPS, S.OAMS_OP_CODE_ERROR_UNSPECIFIED, gen=0,
                           fault=fault)
    system, effects = S.reduce(system, action, S.World(), now=1.0)
    settle = [e for e in effects if isinstance(e, S.Settle)][0]
    return system.lanes[FPS], settle.result


class ReducerTests(unittest.TestCase):
    def test_the_latched_fault_follows_the_refusal_text(self):
        lane, result = unloading_failure(HINT)
        self.assertEqual(
            result.message,
            "Spool unloading failed (the unit refused the operation (not "
            "ready or not permitted); %s)" % HINT)
        self.assertEqual(lane.fault_text, result.message)
        self.assertEqual(lane.message, "unload failed")

    def test_without_a_fault_the_text_is_unchanged(self):
        _, result = unloading_failure(None)
        self.assertEqual(
            result.message,
            "Spool unloading failed (the unit refused the operation (not "
            "ready or not permitted))")


class DriverTests(unittest.TestCase):
    def driver(self, domain):
        driver = OAMS.__new__(OAMS)
        driver.oams_idx = 2
        driver._telemetry = {"domain": domain} if domain is not None else {}
        return driver

    def test_a_latched_motion_fault_is_named_with_the_recovery(self):
        driver = self.driver({"motion_fault": 12,
                              "motion_fault_name": "motor_drive_fault"})
        self.assertEqual(driver._latched_fault_text(), HINT)

    def test_nothing_latched_or_no_telemetry_gives_none(self):
        self.assertIsNone(self.driver(
            {"motion_fault": 0, "motion_fault_name": "none"}
        )._latched_fault_text())
        self.assertIsNone(self.driver(None)._latched_fault_text())


if __name__ == "__main__":
    unittest.main()
