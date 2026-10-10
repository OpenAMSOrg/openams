# Firmware return-code handling: PTFE calibration error messages.
#
# This file predates the pure-core rewrite; its original runout/cancel cases
# (successful auto-reload, a non-success code not advancing current_spool,
# a cancelled load not marking the bay loaded) exercised the old monolithic
# OAMSManager directly. That logic now lives in the pure reducer and is
# covered there with more rigor: see test_oams_state.py's
# test_cancel_then_completed_is_not_loaded and its runout-reload tests
# (around RUNOUT_LOADING). What remains here — and is NOT covered elsewhere —
# is oams.OAMS._ptfe_calibration_error_message, ported from klipper_openams
# master with its exact wording.
#
#   cd openams && python3 -m unittest discover -s test

import importlib
import os
import sys
import types
import unittest

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if "oamspkg" not in sys.modules:
    _pkg = types.ModuleType("oamspkg")
    _pkg.__path__ = [_SRC]
    sys.modules["oamspkg"] = _pkg
sys.modules.setdefault("mcu", types.ModuleType("mcu"))

oams = importlib.import_module("oamspkg.oams")
OAMS = oams.OAMS


def make_calibration_unit(code, value=0):
    unit = OAMS.__new__(OAMS)
    unit.oams_idx = 1
    unit.name = "oams oams1"
    unit.action_status_code = code
    unit.action_status_value = value
    return unit


class PtfeCalibrationErrorMessageTests(unittest.TestCase):
    def test_no_filament_message(self):
        unit = make_calibration_unit(oams.OAMS_OP_CODE_NO_SPOOL_IN_BAY)
        message = unit._ptfe_calibration_error_message(2)
        self.assertIn("no filament was detected in oams1 Spool 3", message)

    def test_loaded_message_recommends_unload(self):
        unit = make_calibration_unit(oams.OAMS_OP_CODE_SPOOL_ALREADY_IN_BAY)
        message = unit._ptfe_calibration_error_message(0)
        self.assertIn("already loaded at the hub or follower mode is active",
                      message)
        self.assertIn("OAMS_UNLOAD_SPOOL OAMS=1", message)

    def test_timeout_reports_distance_and_recovery(self):
        unit = make_calibration_unit(oams.OAMS_OP_CODE_TIMEOUT, 114)
        message = unit._ptfe_calibration_error_message(0)
        self.assertIn("timed out in firmware after 60 s", message)
        self.assertIn("approximately 100.0 mm (114 encoder clicks)", message)
        self.assertIn("filament position is unknown", message)
        self.assertIn("OAMS_UNLOAD_SPOOL OAMS=1", message)

    def test_unknown_code_is_reported(self):
        unit = make_calibration_unit(99)
        message = unit._ptfe_calibration_error_message(0)
        self.assertEqual(message,
                         "PTFE calibration failed with firmware error code 99.")


if __name__ == "__main__":
    unittest.main()
