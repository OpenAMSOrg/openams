"""The spool key of a tag (C2 §4.6, src/tag_identity.py).

The vectors are the fixed ones both the host and the Moonraker component's
copy (moonraker/openams_spoolman/ops.py) are tested against.

    python3 -m unittest discover -s test -p 'test_tag_identity.py' -v
"""

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

import tag_identity                                     # noqa: E402

VALID = "0123456789abcdef0123456789abcdef"

VECTORS = (
    ({"format": "bambu", "uid": "04a1b2c3",
      "bambu": {"tray_uid": "0123456789ABCDEF0123456789abcdef"}},
     "bambu:" + VALID),
    ({"format": "bambu", "uid": "04a1b2c3",
      "bambu": {"tray_uid": " %s " % VALID}},
     "bambu:" + VALID),
    ({"format": "bambu", "uid": "04a1b2c3",
      "bambu": {"tray_uid": "0" * 32}}, "uid:04A1B2C3"),
    ({"format": "bambu", "uid": "04a1b2c3",
      "bambu": {"tray_uid": "f" * 32}}, "uid:04A1B2C3"),
    ({"format": "bambu", "uid": "04a1b2c3",
      "bambu": {"tray_uid": "0123"}}, "uid:04A1B2C3"),
    ({"format": "bambu", "uid": "04a1b2c3", "bambu": None}, "uid:04A1B2C3"),
    ({"format": "spoolman_link", "uid": "99887766", "spoolman_id": 12},
     "uid:99887766"),
    ({"format": "unit_summary", "uid": None,
      "summary": {"material": "ABS"}}, None),
    (None, None),
)


class SpoolKeyVectorTests(unittest.TestCase):

    def test_the_fixed_vectors(self):
        for tag, expected in VECTORS:
            with self.subTest(tag=tag):
                self.assertEqual(tag_identity.spool_key(tag), expected)

    def test_valid_tray(self):
        self.assertTrue(tag_identity.valid_tray(VALID))
        self.assertTrue(tag_identity.valid_tray(VALID.upper()))
        self.assertTrue(tag_identity.valid_tray("  %s\n" % VALID))
        for bad in ("0" * 32, "F" * 32, "f" * 32, "0123", VALID + "0",
                    "g" * 32, "", None, 12, b"0" * 32):
            with self.subTest(bad=bad):
                self.assertFalse(tag_identity.valid_tray(bad))

    def test_an_all_f_tray_in_mixed_case_is_blank(self):
        self.assertFalse(tag_identity.valid_tray("fFfF" * 8))

    def test_both_tags_of_one_spool_share_the_key(self):
        a = {"format": "bambu", "uid": "11aa22bb", "bambu": {"tray_uid": VALID}}
        b = {"format": "bambu", "uid": "33cc44dd",
             "bambu": {"tray_uid": VALID.upper()}}
        self.assertEqual(tag_identity.spool_key(a),
                         tag_identity.spool_key(b))

    def test_a_blank_uid_has_no_key(self):
        self.assertIsNone(tag_identity.spool_key(
            {"format": "unknown", "uid": "  "}))


if __name__ == "__main__":
    unittest.main()
