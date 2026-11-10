"""The tag attribution an openams_ui projection carries (C2 §6.2 `bays[].tg`,
`tc`, `tr`, `to`) and the `sp.src` rule that goes with it: a tag link shows
as a tag read only while the bay's tag is attributed."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import openams_ui  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "openams_ui",
                       "manager_status_idle.json")


def tag_state(state, candidates=(), reason=None, other_bay=None):
    return {"state": state, "reason": reason, "other_bay": other_bay,
            "reader": 0,
            "candidates": [{"spool_key": "bambu:" + "ab" * 16,
                            "uid": "11aa22bb", "material": material,
                            "color_argb": argb}
                           for material, argb in candidates]}


def project(state, link_source="tag"):
    with open(FIXTURE) as f:
        status = json.load(f)
    bay = status["devices"]["oams1"]["bays"][0]
    bay["link_source"] = link_source
    bay["spool_id"] = 5
    bay["tag"] = {"uid": "04a1b2c3", "format": "bambu"}
    if state is not None:
        bay["tag_state"] = state
    return openams_ui.project(status, "oams1")["bays"]


class TagStateProjectionTests(unittest.TestCase):

    def test_letters(self):
        for state, letter in (("attributed", "a"), ("candidate", "c"),
                              ("conflict", "x"), ("unknown", "u")):
            with self.subTest(state=state):
                self.assertEqual(project(tag_state(state))[0]["tg"], letter)

    def test_no_letter_for_none_or_without_a_tag_state(self):
        self.assertNotIn("tg", project(tag_state("none"))[0])
        self.assertNotIn("tg", project(None)[0])

    def test_no_letter_once_turns_showed_no_tag(self):
        bay = project(tag_state("unknown", reason="no_tag_after_turn"))[0]
        self.assertNotIn("tg", bay)
        self.assertEqual(bay["sp"]["src"], "m")

    def test_src_is_t_only_when_attributed(self):
        self.assertEqual(project(tag_state("attributed"))[0]["sp"]["src"], "t")
        for state in ("candidate", "conflict", "unknown", "none"):
            with self.subTest(state=state):
                self.assertEqual(project(tag_state(state))[0]["sp"]["src"],
                                 "m")
        self.assertEqual(project(None)[0]["sp"]["src"], "t")
        self.assertEqual(project(tag_state("attributed"), "manual")[0][
            "sp"]["src"], "m")

    def test_candidates_carry_material_and_colour(self):
        bay = project(tag_state("candidate", [("PLA", 0xFF0A2989)]))[0]
        self.assertEqual(bay["tc"], [{"m": "PLA", "c": "0A2989"}])
        bay = project(tag_state("candidate", [(None, None)]))[0]
        self.assertEqual(bay["tc"], [{"m": None, "c": None}])
        self.assertNotIn("tc", project(tag_state("attributed"))[0])

    def test_conflict_reason_and_other_slot(self):
        bay = project(tag_state("conflict", reason="ambiguous_pair"))[0]
        self.assertEqual(bay["tr"], "ambiguous_pair")
        self.assertNotIn("to", bay)
        bays = project(tag_state("conflict", reason="claimed_twice",
                                 other_bay=2))
        self.assertEqual(bays[0]["tr"], "claimed_twice")
        self.assertIsInstance(bays[0]["to"], int)
        self.assertEqual(bays[0]["to"], bays[2]["id"])
        self.assertNotIn("tr", project(tag_state("candidate"))[0])


if __name__ == "__main__":
    unittest.main()
