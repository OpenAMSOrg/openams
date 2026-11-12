"""A lane's own fault on a shared lane (an AMS 1 and an AMS 2 Pro feeding one
lane): every unit carries a copy, but an openams_ui projection carries it
once, attributed to the unit whose bay failed (seen on voron350, where an AMS
2 Pro unload failure showed as `ams1` on both units' screens)."""
import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import openams_ui  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "openams_ui",
                       "manager_status_idle.json")


def lane_fault(bay):
    return {"severity": "pause", "code": "unload failed",
            "text": "Spool unloading failed (the AMS reported error 0x12)",
            "bay": bay, "actions": ["clear_errors"], "source": "lane"}


def two_unit_status(oams1_faults, oams2_faults):
    """The recorded idle printer with a second unit on the same lane."""
    with open(FIXTURE) as f:
        st = json.load(f)
    st["topology"]["oams"]["oams2"] = copy.deepcopy(
        st["topology"]["oams"]["oams1"])
    st["topology"]["oams"]["oams2"]["idx"] = 2
    st["devices"]["oams2"] = copy.deepcopy(st["devices"]["oams1"])
    st["devices"]["oams1"]["faults"] = oams1_faults
    st["devices"]["oams2"]["faults"] = oams2_faults
    return st


class SharedLaneFaultTests(unittest.TestCase):
    def test_the_failed_units_copy_is_carried_on_every_unit(self):
        # The second unit's bay 2 failed: the first unit's copy names no bay.
        st = two_unit_status([lane_fault(None)], [lane_fault(2)])
        for name in ("oams1", "oams2"):
            view = openams_ui.project(st, name)
            self.assertEqual(view["fault"]["unit"], "oams2", name)
            self.assertEqual(view["fault"]["bay"], 2, name)
            self.assertEqual(view["nf"], 1, name)

    def test_the_first_units_own_failure_stays_its_own(self):
        st = two_unit_status([lane_fault(3)], [lane_fault(None)])
        view = openams_ui.project(st, "oams2")
        self.assertEqual((view["fault"]["unit"], view["fault"]["bay"]),
                         ("oams1", 3))
        self.assertEqual(view["nf"], 1)

    def test_a_lane_fault_naming_no_bay_is_still_carried_once(self):
        st = two_unit_status([lane_fault(None)], [lane_fault(None)])
        view = openams_ui.project(st, "oams2")
        self.assertEqual(view["fault"]["code"], "unload failed")
        self.assertEqual(view["nf"], 1)

    def test_a_units_own_fault_is_counted_beside_the_lane_fault(self):
        own = {"severity": "pause", "code": "motion_timeout",
               "text": "motion_timeout", "bay": None,
               "actions": ["clear_fault"], "source": "firmware"}
        st = two_unit_status([lane_fault(None)], [own, lane_fault(2)])
        view = openams_ui.project(st, "oams1")
        self.assertEqual(view["nf"], 2)


if __name__ == "__main__":
    unittest.main()
