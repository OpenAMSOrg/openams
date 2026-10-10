# SPDX-License-Identifier: MIT
"""The UI offers a unit's RFID scan when the unit can read its tags at all:
through raw RFID, or by decoding them itself (tray reads)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import openams_ui  # noqa: E402


def device(**caps):
    return {"connected": True, "capabilities": caps,
            "supported_actions": ["load", "unload", "rfid_scan"]}


class RfidScanGate(unittest.TestCase):
    def test_tray_reading_unit_without_raw_rfid_offers_the_scan(self):
        # A Cortex Bridge's AMS 2 Pro: no raw RFID transport, tray reads work.
        self.assertIn("rfid_scan", openams_ui._unit_actions(
            device(rfid=False, tray_info=True, tray_read=True), None))

    def test_unit_with_neither_hides_the_scan(self):
        self.assertNotIn("rfid_scan", openams_ui._unit_actions(
            device(rfid=False, tray_info=False, tray_read=False), None))

    def test_raw_rfid_unit_offers_the_scan(self):
        self.assertIn("rfid_scan", openams_ui._unit_actions(device(rfid=True), None))


if __name__ == "__main__":
    unittest.main()
