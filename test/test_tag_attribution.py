"""Tag attribution: which bay a tag belongs to (src/oams_tags.py, C2 §4.6).

The pure ``Attribution`` driven by scripted samples, ready edges and
turning, and the ``TagReader`` around it with a stub unit. No hardware.

    python3 -m unittest discover -s test -p 'test_tag_attribution.py' -v
"""

import importlib
import json
import os
import sys
import tempfile
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if "oamspkg" not in sys.modules:
    _pkg = types.ModuleType("oamspkg")
    _pkg.__path__ = [_SRC]
    sys.modules["oamspkg"] = _pkg
T = importlib.import_module("oamspkg.oams_tags")

TRAY_A = "aa" * 16
TRAY_B = "bb" * 16
TRAY_C = "cc" * 16


def bambu(tray, material="PLA", argb=0xFF112233):
    return T.tag_record("bambu", bambu={
        "manufacturer": "Bambu", "type": material, "detailed": material,
        "color_argb": argb, "weight_g": 1000, "diameter_mm": 1.75,
        "drying_temp_c": 55, "drying_time_h": 8, "bed_temp_c": 35,
        "hotend_max_c": 230, "hotend_min_c": 190, "nozzle_diameter": 0.4,
        "spool_width_mm": 66.25, "length_m": 330, "tray_uid": tray,
        "production": "2026_01_01_00_00"})


def two_readers(debounce=1, **options):
    """An AMS 1 layout: reader 0 covers bays 0,1 and reader 1 bays 2,3.
    Spool A has the tags a1/a2, B has b1/b2, C has c1."""
    cache = T.TagCache()
    for uid, tray in (("a1000001", TRAY_A), ("a2000002", TRAY_A),
                      ("b1000001", TRAY_B), ("b2000002", TRAY_B),
                      ("c1000001", TRAY_C)):
        cache.put(uid, bambu(tray, material="PETG" if tray == TRAY_B
                             else "PLA"))
    attr = T.Attribution(4, [[0, 1], [2, 3]], cache=cache,
                         debounce=debounce, **options)
    return attr


KEY_A = "bambu:" + TRAY_A
KEY_B = "bambu:" + TRAY_B
KEY_C = "bambu:" + TRAY_C


def settle(attr, bays, now=0.0):
    """Bays ready since before the host started (no edge seen)."""
    for bay in bays:
        attr.init_ready(bay, True)


def attribute_by_motion(attr, bay, uid, start, reader=None):
    """Turn ``bay`` with its tag passing the reader: attributed by motion."""
    reader = attr.reader_of(bay) if reader is None else reader
    field = attr.field(reader)
    attr.note_turning(bay, True, start)
    attr.note_field(reader, sorted(field | {uid}), start + 0.1)
    attr.note_field(reader, sorted(field - {uid}), start + 0.2)
    attr.note_turning(bay, False, start + 0.3)


class DesignTests(unittest.TestCase):
    """Design 10.2 items 1-13."""

    def test_01_tag_appearing_during_positioning_is_attributed_by_motion(self):
        attr = two_readers()
        attr.note_field(0, [], 0.0)
        attr.note_ready(0, True, 10.0)
        attr.note_turning(0, True, 10.0)       # the unit positions bay 0
        attr.note_field(0, ["a1000001"], 10.5)
        bay = attr.bays[0]
        self.assertEqual(bay.state, T.ATTRIBUTED)
        self.assertEqual(bay.evidence, T.MOTION)
        self.assertEqual(attr.tag(0)["spool_key"], KEY_A)
        self.assertEqual(attr.tag(0)["evidence"], "motion")
        self.assertEqual(attr.tag(0)["read_at"], 10.5)

    def test_02_tag_in_the_field_before_the_rise_next_to_a_settled_neighbour(self):
        attr = two_readers()
        settle(attr, [1])
        attr.note_field(0, [], 0.0)
        attribute_by_motion(attr, 1, "b1000001", 1.0)
        # The spool goes into bay 0, its tag in the field, before its
        # filament reaches the feeder: an orphan change, then the rise.
        attr.note_field(0, ["a1000001"], 100.0)
        self.assertEqual(attr.bays[0].state, T.NONE)
        attr.note_ready(0, True, 110.0)
        self.assertEqual(attr.bays[0].state, T.ATTRIBUTED)
        self.assertEqual(attr.bays[0].evidence, T.INSERTION)
        self.assertEqual(attr.bays[0].key, KEY_A)

    def test_03_neighbours_tag_constant_through_a_turn_is_not_this_bays(self):
        attr = two_readers(static_samples=8)
        settle(attr, [0, 1])
        attr.note_field(0, ["b1000001"], 0.0)   # bay 1's tag at rest
        attr.note_turning(0, True, 1.0)         # bay 0 loads
        for step in range(10):
            attr.note_field(0, ["b1000001"], 1.0 + 0.1 * step)
        self.assertEqual(attr.bays[0].state, T.UNKNOWN)
        self.assertEqual(attr.bays[1].state, T.CANDIDATE)
        self.assertEqual(attr.bays[1].candidates[0][0], KEY_B)
        self.assertIsNone(attr.tag(1))

    def test_04_two_uids_of_one_spool_on_one_bay_are_one_tag(self):
        attr = two_readers()
        settle(attr, [0])
        attr.note_field(0, [], 0.0)
        attribute_by_motion(attr, 0, "a1000001", 1.0)
        read_at = attr.tag(0)["read_at"]
        attr.note_turning(0, True, 5.0)
        attr.note_field(0, ["a2000002"], 5.1)   # the far flange's tag
        tag = attr.tag(0)
        self.assertEqual(attr.bays[0].state, T.ATTRIBUTED)
        self.assertEqual(tag["spool_key"], KEY_A)
        self.assertEqual(tag["read_at"], read_at)
        self.assertEqual(tag["uids"], ["A2000002", "A1000001"])

    def test_05_flip_gives_the_same_key_and_one_new_read_at(self):
        attr = two_readers()
        settle(attr, [0])
        attr.note_field(0, [], 0.0)
        attribute_by_motion(attr, 0, "a1000001", 1.0)
        first = attr.tag(0)["read_at"]
        attr.note_ready(0, False, 50.0)
        self.assertIsNone(attr.tag(0))
        attr.note_ready(0, True, 60.0)
        attribute_by_motion(attr, 0, "a2000002", 61.0)
        tag = attr.tag(0)
        self.assertEqual(tag["spool_key"], KEY_A)
        self.assertEqual(tag["uid"], "a2000002")
        self.assertGreater(tag["read_at"], first)
        again = tag["read_at"]
        attribute_by_motion(attr, 0, "a2000002", 120.0)
        self.assertEqual(attr.tag(0)["read_at"], again)

    def test_06_swap_of_two_bays_on_one_reader(self):
        attr = two_readers()
        settle(attr, [0, 1])
        attr.note_field(0, [], 0.0)
        attribute_by_motion(attr, 0, "a1000001", 1.0)
        attribute_by_motion(attr, 1, "b1000001", 2.0)
        for bay in (0, 1):
            attr.note_ready(bay, False, 10.0)
        for bay in (0, 1):
            attr.note_ready(bay, True, 12.0)
        attribute_by_motion(attr, 0, "b1000001", 60.0)
        attribute_by_motion(attr, 1, "a1000001", 70.0)
        self.assertEqual((attr.bays[0].state, attr.bays[0].key),
                         (T.ATTRIBUTED, KEY_B))
        self.assertEqual((attr.bays[1].state, attr.bays[1].key),
                         (T.ATTRIBUTED, KEY_A))
        self.assertIsNone(attr.bays[0].reason)
        self.assertIsNone(attr.bays[1].reason)
        self.assertEqual(attr.shared_pairs(), [])

    def test_07_an_empty_field_for_60_s_is_never_removal(self):
        attr = two_readers(debounce=2)
        settle(attr, [0])
        attr.note_field(0, [], 0.0)
        attr.note_turning(0, True, 1.0)
        attr.note_field(0, ["a1000001"], 1.1)
        attr.note_field(0, ["a1000001"], 1.2)
        attr.note_turning(0, False, 1.3)
        self.assertEqual(attr.bays[0].state, T.ATTRIBUTED)
        for step in range(120):
            attr.note_field(0, [], 2.0 + 0.5 * step)
        self.assertEqual(attr.bays[0].state, T.ATTRIBUTED)
        self.assertEqual(attr.tag(0)["spool_key"], KEY_A)

    def test_08_error_samples_change_nothing(self):
        attr = two_readers()
        settle(attr, [0, 1])
        attr.note_field(0, ["b1000001"], 0.0)
        attr.note_ready(0, True, 1.0)
        for step in range(5):
            attr.note_field(0, None, 1.1 + 0.1 * step)
        attr.note_field(0, ["b1000001"], 2.0)
        self.assertEqual(attr.bays[0].state, T.UNKNOWN)
        self.assertEqual(attr.field(0), {"b1000001"})
        self.assertEqual(attr.readers[0].errors, 5)

    def test_09_runout_keeps_the_tag_then_clears_it(self):
        attr = two_readers()
        settle(attr, [0])
        attr.note_field(0, [], 0.0)
        attribute_by_motion(attr, 0, "a1000001", 1.0)
        attr.note_ready(0, False, 30.0, keep=True)   # in use: a runout
        self.assertEqual(attr.tag(0)["spool_key"], KEY_A)
        self.assertEqual(attr.kept_bays(), [0])
        self.assertTrue(attr.clear_kept(0))          # the runout ended
        self.assertIsNone(attr.tag(0))
        self.assertEqual(attr.bays[0].state, T.NONE)

    def test_10_clones_on_two_bays_both_stand(self):
        attr = two_readers()
        attr.cache.put("c2000002", bambu(TRAY_C))   # a clone of spool C
        settle(attr, [0, 2])
        attr.note_field(0, [], 0.0)
        attr.note_field(1, [], 0.0)
        attribute_by_motion(attr, 0, "c1000001", 1.0)
        attribute_by_motion(attr, 2, "c2000002", 2.0)
        self.assertEqual(attr.bays[0].key, KEY_C)
        self.assertEqual(attr.bays[2].key, KEY_C)
        self.assertTrue(attr.tag(0)["shared_identity"])
        self.assertTrue(attr.tag(2)["shared_identity"])
        self.assertEqual(sorted(attr.shared_pairs()), [(0, 2), (2, 0)])

    def test_11_both_bays_handled_then_motion_resolves_both(self):
        attr = two_readers()
        attr.note_field(0, [], 0.0)
        attr.note_ready(0, True, 10.0)
        attr.note_ready(1, True, 12.0)
        attr.note_field(0, ["a1000001", "b1000001"], 13.0)
        for bay in (0, 1):
            self.assertEqual(attr.bays[bay].state, T.CONFLICT)
            self.assertEqual(attr.bays[bay].reason, T.AMBIGUOUS_PAIR)
            self.assertEqual(len(attr.tag_state(bay)["candidates"]), 2)
            self.assertIsNone(attr.tag(bay))
        attr.note_turning(0, True, 60.0)            # bay 0 loads
        attr.note_field(0, ["b1000001"], 60.2)       # a1 turns away
        self.assertEqual((attr.bays[0].state, attr.bays[0].key),
                         (T.ATTRIBUTED, KEY_A))
        self.assertEqual((attr.bays[1].state, attr.bays[1].key),
                         (T.ATTRIBUTED, KEY_B))
        self.assertEqual(attr.bays[1].evidence, T.ELIMINATION)

    def test_12_restart_restores_a_candidate_confirmed_without_new_read_at(self):
        attr = two_readers()
        settle(attr, [0])
        self.assertTrue(attr.restore(0, KEY_A, "a1000001", 5.0))
        self.assertEqual(attr.tag_state(0)["state"], "candidate")
        self.assertIsNone(attr.tag(0))
        attr.note_field(0, [], 100.0)
        attribute_by_motion(attr, 0, "a1000001", 101.0)
        tag = attr.tag(0)
        self.assertEqual(tag["read_at"], 5.0)
        self.assertEqual(tag["spool_key"], KEY_A)

    def test_13_explicit_read_with_two_keys_and_assert(self):
        attr = two_readers()
        settle(attr, [0])
        attr.note_field(0, ["a1000001", "b1000001"], 1.0)
        outcome = attr.explicit_read(0, ["a1000001", "b1000001"], 2.0)
        self.assertEqual(outcome["word"], "conflict")
        self.assertEqual(outcome["reason"], T.AMBIGUOUS_PAIR)
        self.assertIsNone(attr.tag(0))
        refused = attr.assert_bay(0, ["a1000001", "b1000001"], 3.0)
        self.assertEqual(refused["word"], "two")
        self.assertIsNone(attr.tag(0))
        outcome = attr.assert_bay(0, ["a1000001"], 4.0)
        self.assertEqual(outcome["word"], "attributed")
        self.assertEqual(attr.tag(0)["evidence"], "asserted")
        self.assertEqual(attr.tag(0)["read_at"], 4.0)


class RuleTests(unittest.TestCase):
    """The other rules of design 4.2 and 5.2-5.3."""

    def test_an_explicit_read_of_one_unclaimed_key_gives_a_candidate(self):
        attr = two_readers()
        settle(attr, [0])
        attr.note_field(0, ["a1000001"], 1.0)
        outcome = attr.explicit_read(0, ["a1000001"], 2.0)
        self.assertEqual(outcome["word"], "candidate")
        self.assertEqual(outcome["key"], KEY_A)
        state = attr.tag_state(0)
        self.assertEqual(state["candidates"][0]["material"], "PLA")
        self.assertEqual(state["candidates"][0]["color_argb"], 0xFF112233)
        self.assertEqual(state["reader"], 0)

    def test_an_explicit_reread_of_the_attributed_key_renews_read_at(self):
        attr = two_readers()
        settle(attr, [0])
        attr.note_field(0, [], 0.0)
        attribute_by_motion(attr, 0, "a1000001", 1.0)
        outcome = attr.explicit_read(0, ["a1000001"], 9.0)
        self.assertEqual(outcome["word"], "attributed")
        self.assertTrue(outcome["in_field"])
        self.assertEqual(attr.tag(0)["read_at"], 9.0)

    def test_a_neighbours_tag_is_reported_not_attributed(self):
        attr = two_readers()
        settle(attr, [0, 1])
        attr.note_field(0, [], 0.0)
        attribute_by_motion(attr, 1, "b1000001", 1.0)
        outcome = attr.explicit_read(0, ["b1000001"], 5.0)
        self.assertEqual(outcome["word"], "no_tag")
        self.assertEqual(outcome["elsewhere"], 1)
        self.assertEqual(attr.bays[0].state, T.UNKNOWN)

    def test_insertion_next_to_an_unsettled_neighbour_is_only_a_candidate(self):
        attr = two_readers()
        settle(attr, [1])
        attr.note_field(0, [], 0.0)
        attr.note_turning(1, True, 1.0)            # bay 1 loading now
        attr.note_turning(1, False, 9.0)
        attr.note_ready(0, True, 9.5)
        attr.note_field(0, ["a1000001"], 10.0)       # bay 0 handled
        # Bay 1 turned since the last sample before this one: its own tag
        # could have entered, so bay 0 only gets a candidate.
        self.assertEqual(attr.bays[0].state, T.CANDIDATE)

    def test_a_different_key_by_motion_on_a_motion_bay_is_a_conflict(self):
        attr = two_readers()
        settle(attr, [0])
        attr.note_field(0, [], 0.0)
        attribute_by_motion(attr, 0, "a1000001", 1.0)
        attr.note_turning(0, True, 20.0)
        attr.note_field(0, ["c1000001"], 20.1)
        self.assertEqual(attr.bays[0].state, T.CONFLICT)
        self.assertEqual(attr.bays[0].reason, T.CHANGED_IN_PLACE)
        self.assertIsNone(attr.tag(0))       # tag null with the conflict
        # Settles back on the same spool: the same tag and read_at again.
        attr.note_turning(0, False, 21.0)
        read_at = attr.bays[0].read_at
        attribute_by_motion(attr, 0, "a1000001", 40.0)
        self.assertEqual(attr.bays[0].state, T.ATTRIBUTED)
        self.assertEqual(attr.tag(0)["read_at"], read_at)

    def test_two_keys_moving_while_one_bay_turns_is_a_conflict(self):
        attr = two_readers()
        settle(attr, [0])
        attr.note_field(0, [], 0.0)
        attr.note_turning(0, True, 1.0)
        attr.note_field(0, ["a1000001"], 1.1)
        attr.note_field(0, ["a1000001", "c1000001"], 1.2)
        self.assertEqual(attr.bays[0].state, T.CONFLICT)
        self.assertEqual(attr.bays[0].reason, T.TWO_MOVING)
        attr.note_turning(0, False, 2.0)
        attribute_by_motion(attr, 0, "c1000001", 30.0)   # a clean motion
        self.assertEqual((attr.bays[0].state, attr.bays[0].key),
                         (T.ATTRIBUTED, KEY_C))

    def test_motion_moves_a_key_from_an_inferred_bay(self):
        attr = two_readers()
        settle(attr, [1, 2])
        attr.note_field(0, [], 0.0)
        attr.note_field(1, [], 0.0)
        assert attr.assert_bay(1, ["a1000001"], 1.0)["word"] == "attributed"
        attr.bays[1].evidence = T.INSERTION    # as if inferred
        attribute_by_motion(attr, 2, "a1000001", 5.0)
        self.assertEqual(attr.bays[2].key, KEY_A)
        self.assertEqual(attr.bays[1].state, T.UNKNOWN)

    def test_an_inferred_key_held_in_place_elsewhere_is_claimed_twice(self):
        # A reader that cannot enumerate (the read after an insertion is
        # taken for the inserted bay) meeting a key another bay holds.
        cache = T.TagCache()
        cache.put("a1000001", bambu(TRAY_A))
        attr = T.Attribution(4, [[0, 1, 2, 3]], cache=cache, enumerates=False)
        for bay in (0, 1):
            attr.init_ready(bay, True)
        attr.note_field(0, [], 0.0)
        attr.note_field(0, ["a1000001"], 1.0, trigger_bay=0)
        self.assertEqual(attr.bays[0].evidence, T.INSERTION)
        attr.note_field(0, ["a1000001"], 2.0, trigger_bay=1)
        self.assertEqual(attr.bays[1].state, T.CONFLICT)
        self.assertEqual(attr.bays[1].reason, T.CLAIMED_TWICE)
        self.assertEqual(attr.tag_state(1)["other_bay"], 0)
        self.assertEqual(attr.bays[0].state, T.ATTRIBUTED)

    def test_a_one_sample_dropout_is_not_a_change(self):
        attr = two_readers(debounce=2)
        settle(attr, [1])
        attr.note_field(0, ["b1000001"], 0.0)
        attr.note_ready(0, True, 10.0)                # bay 0 handled
        attr.note_field(0, [], 10.5)                  # b1 missed once
        attr.note_field(0, ["b1000001"], 11.0)
        self.assertEqual(attr.bays[0].state, T.UNKNOWN)
        self.assertEqual(attr.bays[0].candidates, [])

    def test_a_decode_arriving_rekeys_with_a_new_read_at(self):
        cache = T.TagCache()
        attr = T.Attribution(4, [[0, 1], [2, 3]], cache=cache, debounce=1)
        settle(attr, [0])
        attr.note_field(0, [], 0.0)
        cache.put("d1000001", T.tag_record("bambu"))   # seen while passing
        attribute_by_motion(attr, 0, "d1000001", 1.0)
        tag = attr.tag(0)
        self.assertEqual(tag["format"], "bambu")
        self.assertIsNone(tag["bambu"])
        self.assertEqual(tag["spool_key"], "uid:D1000001")
        cache.put("d1000001", bambu(TRAY_C))
        attr.note_record("d1000001", 7.0)
        tag = attr.tag(0)
        self.assertEqual(tag["spool_key"], KEY_C)
        self.assertEqual(tag["read_at"], 7.0)

    def test_single_bay_reader(self):
        cache = T.TagCache()
        cache.put("a1000001", bambu(TRAY_A))
        attr = T.Attribution(1, [[0]], cache=cache, enumerates=False)
        attr.init_ready(0, True)
        attr.note_field(0, [], 0.0)
        attr.note_field(0, ["a1000001"], 1.0)
        self.assertEqual(attr.bays[0].evidence, T.SINGLE)
        attr.note_field(0, [], 2.0)                   # no tag: not removal
        self.assertEqual(attr.bays[0].state, T.ATTRIBUTED)

    def test_single_bay_reader_takes_a_tag_already_in_its_field(self):
        # One bay under the reader: presence is enough (``single``), even in
        # the first sample after a restart.
        cache = T.TagCache()
        cache.put("a1000001", bambu(TRAY_A))
        attr = T.Attribution(1, [[0]], cache=cache, enumerates=False)
        attr.init_ready(0, True)
        attr.note_field(0, ["a1000001"], 1.0)
        self.assertEqual(attr.bays[0].evidence, T.SINGLE)
        attr.note_field(0, ["a1000001"], 2.0)
        self.assertEqual(attr.bays[0].confirmed_at, 1.0)  # no churn

    def test_published_shapes(self):
        attr = two_readers()
        settle(attr, [0])
        attr.note_field(0, [], 0.0)
        attribute_by_motion(attr, 0, "a1000001", 1.0)
        tag = attr.tag(0)
        self.assertEqual(
            set(tag), {"uid", "format", "read_at", "spoolman_id", "bambu",
                       "spool_key", "uids", "evidence", "confirmed_at"})
        state = attr.tag_state(0)
        self.assertEqual(set(state), {"state", "reason", "candidates",
                                      "other_bay", "reader"})


class RegressionTests(unittest.TestCase):
    """The defects the design's review lists."""

    def test_an_only_ready_covered_bay_does_not_take_a_tag(self):
        # Was: OpenAmsMfrc522Pair._pick_bay fell back to "the only covered
        # bay currently ready". A spool resting in the neighbour bay, its
        # filament not fed, has a tag in the same field.
        attr = two_readers()
        settle(attr, [0])
        attr.note_field(0, [], 0.0)
        attr.note_field(0, ["b1000001"], 200.0)   # bay 1's resting spool
        self.assertEqual(attr.bays[0].state, T.UNKNOWN)
        self.assertIsNone(attr.tag(0))

    def test_a_second_tag_entering_never_removes_the_first(self):
        # Was: two tags in one field gave a bad BCC, counted as a miss; two
        # misses reported the card removed and the bay's tag cleared.
        attr = two_readers()
        settle(attr, [0, 1])
        attr.note_field(0, [], 0.0)
        attribute_by_motion(attr, 0, "a1000001", 1.0)
        attr.note_field(0, None, 5.0)               # the collision error
        attr.note_field(0, None, 5.5)
        attr.note_field(0, ["b1000001"], 6.0)       # neighbour's tag
        self.assertEqual(attr.tag(0)["spool_key"], KEY_A)


class ReviewTests(unittest.TestCase):
    """Cases the design review added."""

    def test_motion_corrects_a_neighbour_under_the_same_reader(self):
        # Bay 0 holds spool A's key by inference only, and the tag is in
        # fact bay 1's: bay 1's own motion takes it (the move rule).
        attr = two_readers()
        settle(attr, [0, 1])
        attr.note_field(0, [], 0.0)
        assert attr.assert_bay(0, ["a1000001"], 1.0)["word"] == "attributed"
        attr.bays[0].evidence = T.INSERTION    # as if inferred
        attr.note_field(0, [], 2.0)
        attribute_by_motion(attr, 1, "a1000001", 5.0)
        self.assertEqual((attr.bays[1].state, attr.bays[1].key),
                         (T.ATTRIBUTED, KEY_A))
        self.assertEqual(attr.bays[1].evidence, T.MOTION)
        self.assertEqual(attr.bays[0].state, T.UNKNOWN)

    def test_a_dropout_of_a_neighbours_own_tag_is_not_this_bays_motion(self):
        attr = two_readers()
        settle(attr, [0, 1])
        attr.note_field(0, [], 0.0)
        attribute_by_motion(attr, 0, "a1000001", 1.0)
        attr.note_field(0, ["a1000001"], 3.0)      # at rest in the field
        attr.note_turning(1, True, 5.0)
        attr.note_field(0, [], 5.1)                # a missed read of it
        attr.note_field(0, ["a1000001"], 5.2)
        attr.note_turning(1, False, 5.3)
        self.assertEqual(attr.bays[1].state, T.UNKNOWN)
        self.assertEqual(attr.bays[0].key, KEY_A)
        self.assertNotIn("shared_identity", attr.tag(0))

    def test_clones_with_two_uids_under_one_reader_both_stand(self):
        attr = two_readers()
        attr.cache.put("c2000002", bambu(TRAY_C))
        settle(attr, [0, 1])
        attr.note_field(0, [], 0.0)
        attribute_by_motion(attr, 0, "c1000001", 1.0)
        attribute_by_motion(attr, 1, "c2000002", 2.0)
        self.assertEqual(attr.bays[0].key, KEY_C)
        self.assertEqual(attr.bays[1].key, KEY_C)
        self.assertTrue(attr.tag(1)["shared_identity"])

    def test_a_loaded_bay_that_follows_is_not_still(self):
        # Bay 0 prints (loaded, following) and is not attributed (a restart).
        # Its tag passing the reader must never make a freshly inserted
        # neighbour's attributed tag: a candidate at most.
        attr = two_readers()
        settle(attr, [0])
        attr.note_field(0, [], 0.0)
        attr.note_following(0, True, 0.0)
        attr.note_field(0, ["a1000001"], 50.0)     # bay 0's tag passes
        attr.note_ready(1, True, 60.0)             # a spool goes into bay 1
        self.assertNotEqual(attr.bays[1].state, T.ATTRIBUTED)
        attr.note_field(0, [], 61.0)
        attr.note_field(0, ["a1000001"], 62.0)
        self.assertNotEqual(attr.bays[1].state, T.ATTRIBUTED)
        self.assertIsNone(attr.tag(1))

    def test_a_still_unattributed_neighbour_still_lets_an_insertion_through(self):
        attr = two_readers()
        settle(attr, [0])
        attr.note_field(0, [], 0.0)
        attr.note_ready(1, True, 60.0)
        attr.note_field(0, ["b1000001"], 61.0)
        self.assertEqual((attr.bays[1].state, attr.bays[1].evidence),
                         (T.ATTRIBUTED, T.INSERTION))

    def test_a_candidate_is_kept_through_a_runout(self):
        attr = two_readers()
        settle(attr, [0])
        self.assertTrue(attr.restore(0, KEY_A, "a1000001", 7.0))
        attr.note_ready(0, False, 30.0, keep=True)
        self.assertEqual(attr.bays[0].state, T.CANDIDATE)
        self.assertEqual(attr.kept_bays(), [0])
        self.assertTrue(attr.clear_kept(0))
        self.assertEqual(attr.bays[0].state, T.NONE)

    def test_turns_that_show_no_tag_end_the_wait(self):
        attr = two_readers(static_samples=4, no_tag_turn_s=30.0)
        settle(attr, [0])
        attr.note_field(0, [], 0.0)
        for start in (10.0, 100.0):
            attr.note_turning(0, True, start)
            for step in range(8):
                attr.note_field(0, [], start + 2.0 * step)
            attr.note_turning(0, False, start + 16.0)
            if start == 10.0:
                self.assertIsNone(attr.tag_state(0)["reason"])
        state = attr.tag_state(0)
        self.assertEqual((state["state"], state["reason"]),
                         (T.UNKNOWN, T.NO_TAG_AFTER_TURN))
        # A tag seen later is still taken, and the reason goes.
        attribute_by_motion(attr, 0, "a1000001", 200.0)
        self.assertEqual(attr.bays[0].state, T.ATTRIBUTED)
        self.assertIsNone(attr.tag_state(0)["reason"])

    def test_a_reader_read_only_when_asked_has_nothing_to_wait_for(self):
        cache = T.TagCache()
        attr = T.Attribution(4, [[0, 1, 2, 3]], cache=cache, enumerates=False)
        attr.init_ready(0, True)                   # there before the start
        self.assertEqual(attr.tag_state(0)["reason"], T.NO_TAG_AFTER_TURN)
        attr.note_ready(1, True, 10.0)             # inserted: a read comes
        self.assertIsNone(attr.tag_state(1)["reason"])
        attr.note_field(0, [], 12.0, trigger_bay=1)    # and finds no tag
        self.assertEqual(attr.tag_state(1)["state"], T.UNKNOWN)
        self.assertEqual(attr.tag_state(1)["reason"], T.NO_TAG_AFTER_TURN)
        # A reader sampled while spools turn keeps waiting for the load.
        polled = two_readers()
        polled.init_ready(0, True)
        self.assertIsNone(polled.tag_state(0)["reason"])

    def test_an_unsampled_turn_says_nothing_about_a_tag(self):
        attr = two_readers(static_samples=4, no_tag_turn_s=30.0)
        settle(attr, [0])
        attr.note_field(0, [], 0.0)
        attr.note_turning(0, True, 10.0)
        attr.note_field(0, None, 20.0)             # the reader only errs
        attr.note_turning(0, False, 100.0)
        self.assertIsNone(attr.tag_state(0)["reason"])


class FakeReactor(object):
    def __init__(self):
        self.now = 100.0
        self.callbacks = []

    def monotonic(self):
        return self.now

    def register_async_callback(self, callback):
        callback(self.now)

    def register_callback(self, callback, when=None):
        self.callbacks.append((when, callback))


class FakeManager(object):
    def __init__(self, path):
        self.openams_config_path = path
        self.settings = {"read_tags_at_startup": True,
                         "read_tag_on_insertion": True}
        self.in_use = set()
        self.turning = set()

    def bay_in_use(self, idx, bay):
        return bay in self.in_use

    def unit_runout_active(self, idx):
        return False

    def turning_bays(self, idx):
        return set(self.turning)


class FakeOams(object):
    def __init__(self, manager=None):
        self.oams_idx = 1
        self.name = "oams oams1"
        self.bay_count = 4
        self.reactor = FakeReactor()
        self.f1s_hes_value = [0, 0, 0, 0]
        self.hardware_snapshot_ready = True
        self.capabilities = {"rfid_coverage": [[0, 1], [2, 3]]}
        self.tags = {}
        objects = {"oams_manager": manager}
        self.printer = types.SimpleNamespace(
            lookup_object=lambda name, default=None: objects.get(
                name, default))
        self.positioning = set()

    def positioning_bays(self, now=None):
        return frozenset(self.positioning)

    def spool_label(self, bay):
        return "oams1 Spool %d" % (bay + 1)


class FakeSource(object):
    def __init__(self, reader):
        self.reader = reader
        self.fields = {0: [], 1: []}

    def sample_reader(self, index):
        uids = list(self.fields[index])
        self.reader.note_field(index, uids)
        return uids


class TagReaderTests(unittest.TestCase):
    """The service around the attribution (C2 §4.5/§4.6, C3)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = os.path.join(self.tmp.name, "oams.cfg")
        with open(self.config, "w") as handle:
            handle.write("[oams oams1]\n")
        self.manager = FakeManager(self.config)
        self.oams = FakeOams(self.manager)
        self.reader = T.TagReader(self.oams)
        self.source = FakeSource(self.reader)
        self.reader.attach_readers(self.source, [[0, 1], [2, 3]], debounce=1)

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, bay, uid, record):
        self.reader.cache.put(uid, record)
        self.oams.f1s_hes_value[bay] = 1
        self.reader.note_ready_rise(bay, self.oams.reactor.now)
        self.manager.turning = {bay}
        self.reader.note_field(self.reader.attr.reader_of(bay), [uid])
        self.manager.turning = set()
        self.reader.update_turning()

    def test_no_blind_startup_read(self):
        # Was: with exactly one ready bay, the startup read attributed the
        # tag in the field to it, whichever bay's tag it was.
        oams = FakeOams(FakeManager(self.config))
        oams.f1s_hes_value = [1, 0, 0, 0]
        oams.hardware_snapshot_ready = False
        reader = T.TagReader(oams)
        source = FakeSource(reader)
        reader.attach_readers(source, [[0, 1], [2, 3]], debounce=1)
        oams.hardware_snapshot_ready = True
        reader.note_hardware_ready()
        reader.note_field(0, ["b1000001"])
        self.assertIsNone(reader.tag_for(0))
        self.assertEqual(reader.tag_state_for(0)["state"], "unknown")

    def test_publishes_tags_states_and_coverage(self):
        self.oams.f1s_hes_value = [0, 1, 0, 0]
        self.reader.attr.init_ready(1, True)
        self.reader.note_field(0, [])
        self.load(0, "a1000001", bambu(TRAY_A))
        self.assertEqual(self.oams.tags[0]["spool_key"], KEY_A)
        self.assertEqual(self.reader.tag_state_for(0)["state"], "attributed")
        self.assertEqual(self.reader.tag_state_for(1)["state"], "unknown")
        self.assertEqual(self.reader.tag_state_for(2)["state"], "none")
        self.assertEqual(self.reader.coverage(), [[0, 1], [2, 3]])
        self.assertTrue(self.reader.attributes())
        self.assertTrue(self.reader.has_host_reader())

    def test_scan_bay_outcomes_and_texts(self):
        self.oams.f1s_hes_value = [1, 1, 0, 0]
        for bay in (0, 1):
            self.reader.attr.init_ready(bay, True)
        self.reader.cache.put("a1000001", bambu(TRAY_A))
        self.reader.cache.put("b1000001", bambu(TRAY_B))
        self.reader.note_field(0, [])
        self.source.fields[0] = ["a1000001", "b1000001"]
        ok, text = self.reader.describe(
            0, self.reader.scan_bay(0, assert_=True))
        self.assertFalse(ok)
        self.assertEqual(
            text, "Two tags answer this reader; turn the other spool away")
        ok, text = self.reader.describe(0, self.reader.scan_bay(0))
        self.assertTrue(ok)
        self.assertEqual(text, "oams1 Spool 1: conflict ambiguous_pair")
        self.source.fields[0] = []
        ok, text = self.reader.describe(0, self.reader.scan_bay(
            0, assert_=True))
        self.assertFalse(ok)
        self.assertIn("No tag in the field", text)
        self.source.fields[0] = ["a1000001"]
        ok, text = self.reader.describe(0, self.reader.scan_bay(
            0, assert_=True))
        self.assertTrue(ok)
        self.assertEqual(text, "oams1 Spool 1: attributed " + KEY_A)
        ok, text = self.reader.describe(1, self.reader.scan_bay(1))
        self.assertEqual(text, "oams1 Spool 2: no tag in the field"
                               " (the tag in the field is Spool 1's)")
        # Bay 0 is accounted for by direct evidence: the other tag in the
        # field is bay 1's (elimination).
        self.source.fields[0] = ["a1000001", "b1000001"]
        ok, text = self.reader.describe(1, self.reader.scan_bay(1))
        self.assertEqual(text, "oams1 Spool 2: attributed " + KEY_B)
        self.assertEqual(self.oams.tags[1]["evidence"], "elimination")

    def test_the_clone_case_raises_an_info_fault(self):
        self.reader.note_field(0, [])
        self.reader.note_field(1, [])
        self.load(0, "c1000001", bambu(TRAY_C))
        self.load(2, "c2000002", bambu(TRAY_C))
        faults = self.reader.faults()
        self.assertEqual([f["bay"] for f in faults], [0, 2])
        self.assertEqual(faults[0]["severity"], "info")
        self.assertEqual(faults[0]["source"], "tag")
        self.assertEqual(faults[0]["code"], "shared_tag_identity")

    def test_the_cache_persists_and_a_restart_restores_a_candidate(self):
        self.reader.note_field(0, [])
        self.load(0, "a1000001", bambu(TRAY_A))
        read_at = self.oams.tags[0]["read_at"]
        path = os.path.join(self.tmp.name, T.CACHE_FILE)
        with open(path) as handle:
            saved = json.load(handle)
        unit = saved["units"]["oams1"]
        self.assertIn("a1000001", unit["records"])
        self.assertEqual(unit["bays"]["0"]["key"], KEY_A)

        oams = FakeOams(self.manager)
        oams.f1s_hes_value = [1, 0, 0, 0]
        reader = T.TagReader(oams)
        source = FakeSource(reader)
        reader.attach_readers(source, [[0, 1], [2, 3]], debounce=1)
        self.assertIsNone(reader.tag_for(0))
        self.assertEqual(reader.tag_state_for(0)["state"], "candidate")
        self.assertEqual(reader.tag_state_for(0)["candidates"][0]["spool_key"],
                         KEY_A)
        reader.note_field(0, [])
        oams.reactor.now += 50
        self.manager.turning = {0}
        reader.note_field(0, ["a1000001"])
        self.manager.turning = set()
        self.assertEqual(reader.tag_for(0)["read_at"], read_at)

    def test_no_cache_file_without_the_openams_config(self):
        oams = FakeOams(FakeManager(os.path.join(self.tmp.name, "none.cfg")))
        reader = T.TagReader(oams)
        reader.attach_readers(FakeSource(reader), [[0, 1], [2, 3]])
        reader.note_field(0, ["a1000001"])
        self.assertEqual(os.listdir(self.tmp.name), ["oams.cfg"])

    def test_external_removal_never_clears(self):
        self.reader.note_field(0, [])
        self.load(0, "a1000001", bambu(TRAY_A))
        self.reader.note_external_removed("a1000001")
        self.assertEqual(self.oams.tags[0]["spool_key"], KEY_A)

    def test_kept_tag_clears_after_the_runout(self):
        self.reader.note_field(0, [])
        self.load(0, "a1000001", bambu(TRAY_A))
        self.manager.in_use = {0}
        self.reader.note_ready_fall(0)
        self.assertIsNotNone(self.oams.tags[0])
        self.assertTrue(self.oams.reactor.callbacks)
        self.manager.in_use = set()
        _when, callback = self.oams.reactor.callbacks.pop()
        callback(self.oams.reactor.now)
        self.assertIsNone(self.oams.tags[0])

    def test_a_loaded_bay_is_fed_as_following(self):
        self.reader.note_field(0, [])
        self.load(0, "a1000001", bambu(TRAY_A))
        self.manager.in_use = {0}
        self.reader.update_turning()
        self.assertTrue(self.reader.attr.bays[0].following)
        self.assertFalse(self.reader.attr.bays[0].turning)
        self.manager.in_use = set()
        self.reader.update_turning()
        self.assertFalse(self.reader.attr.bays[0].following)

    def test_a_reconnect_with_the_spool_gone_clears_the_tag(self):
        self.reader.note_field(0, [])
        self.load(0, "a1000001", bambu(TRAY_A))
        self.assertIsNotNone(self.oams.tags[0])
        # The unit drops out and comes back with bay 0 empty.
        self.oams.f1s_hes_value[0] = 0
        self.reader.note_hardware_ready()
        self.assertIsNone(self.oams.tags[0])
        self.assertEqual(self.reader.tag_state_for(0)["state"], T.NONE)

    def test_a_pending_bambu_record_until_decoded_or_given_up(self):
        uid = "e1000001"
        self.assertTrue(self.reader.needs_record(uid))
        self.assertEqual(self.reader.pending_record(uid)["format"], "bambu")
        for _ in range(T.DECODE_ATTEMPTS):
            self.reader.note_decode_failed(uid)
        self.assertFalse(self.reader.needs_record(uid))
        self.assertEqual(self.reader.pending_record(uid)["format"], "unknown")
        self.reader.cache.put("f1000001", bambu(TRAY_A))
        self.reader.cache.put("f1000001", T.tag_record("bambu"))
        self.assertIsNotNone(self.reader.cache.record("f1000001")["bambu"])


if __name__ == "__main__":
    unittest.main()
