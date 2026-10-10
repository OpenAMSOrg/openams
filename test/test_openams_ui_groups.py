"""The groups an openams_ui projection lists (C2 6.x `groups[]`): every group
on the unit's lane, with members only for groups holding one of its bays, and
every empty group by name, since an empty group is on no lane yet."""
import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import openams_ui  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "openams_ui",
                       "manager_status_idle.json")


def manager_status():
    """An idle one-unit printer's oams_manager status (recorded, sanitized)."""
    with open(FIXTURE) as f:
        return json.load(f)


class EmptyGroupTests(unittest.TestCase):
    def test_a_group_on_the_lane_lists_its_members(self):
        groups = openams_ui.project(manager_status(), "oams1")["groups"]
        self.assertEqual([g["n"] for g in groups], ["T0", "T1", "T2", "T3"])
        self.assertEqual(groups[2]["b"], ["oams1-2"])

    def test_an_emptied_group_stays_listed_by_name(self):
        # Bay 2 moved from T2 into T1: T2 now holds nothing and has no lane.
        st = copy.deepcopy(manager_status())
        st["topology"]["groups"]["T1"]["bays"] = ["oams1-1", "oams1-2"]
        st["topology"]["groups"]["T2"] = {"lane": None, "bays": []}
        groups = openams_ui.project(st, "oams1")["groups"]
        self.assertEqual([g["n"] for g in groups], ["T0", "T1", "T2", "T3"])
        self.assertEqual(groups[1]["b"], ["oams1-1", "oams1-2"])
        self.assertEqual(groups[2], {"n": "T2"})

    def test_a_group_on_another_lane_is_not_listed(self):
        st = copy.deepcopy(manager_status())
        st["topology"]["groups"]["T9"] = {"lane": "fps_other", "bays": ["oamsX-0"]}
        names = [g["n"] for g in openams_ui.project(st, "oams1")["groups"]]
        self.assertNotIn("T9", names)


if __name__ == "__main__":
    unittest.main()
