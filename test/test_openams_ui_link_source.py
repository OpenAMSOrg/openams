"""The link source an openams_ui projection shows (C2 6.x `bays[].sp.src`): a
link made from a unit's stored setting is not shown as an RFID read."""

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import openams_ui  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "openams_ui",
                       "manager_status_idle.json")


def status_with_bay0(link_source, tag):
    with open(FIXTURE) as f:
        status = json.load(f)
    bay = status["devices"]["oams1"]["bays"][0]
    bay["link_source"] = link_source
    bay["spool_id"] = 5
    bay["tag"] = tag
    return status


def src_of_bay0(status):
    return openams_ui.project(status, "oams1")["bays"][0]["sp"]["src"]


class LinkSourceTests(unittest.TestCase):
    def test_a_link_from_a_unit_summary_is_shown_as_manual(self):
        tag = {"uid": None, "format": "unit_summary",
               "summary": {"code": "GFL99", "material": "PLA"}}
        self.assertEqual(src_of_bay0(status_with_bay0("tag", tag)), "m")

    def test_a_link_from_a_read_tag_stays_a_tag(self):
        tag = {"uid": "04a1b2c3d4e5f6", "format": "bambu"}
        self.assertEqual(src_of_bay0(status_with_bay0("tag", tag)), "t")

    def test_a_tag_link_without_a_tag_record_keeps_its_source(self):
        # A unit that keeps its tags elsewhere (klipper-mmu) publishes none.
        self.assertEqual(src_of_bay0(status_with_bay0("tag", None)), "t")

    def test_a_manual_link_stays_manual(self):
        self.assertEqual(src_of_bay0(status_with_bay0("manual", None)), "m")


if __name__ == "__main__":
    unittest.main()
