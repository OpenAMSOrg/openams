# Tests for toolheads/cut_defaults.json and tools/toolhead_defaults.py.
#
#   cd openams && python3 -m unittest discover -s test -p test_toolhead_defaults.py -v

import io
import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import toolhead_defaults as T

SETTINGS = ("retract_length", "extrusion_unload_length",
            "extrusion_reload_length", "hotend_meltzone_compensation")


class DatabaseTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.db = T.load()

    def test_every_combo_is_complete(self):
        self.assertTrue(self.db["combos"])
        ids = set()
        for c in self.db["combos"]:
            self.assertNotIn(c["id"], ids, c["id"])
            ids.add(c["id"])
            for part in T.PARTS:
                self.assertTrue(c[part], (c["id"], part))
            # an entry has every setting, or none when its melt zone is unknown
            if c["openams"]:
                for key in SETTINGS:
                    self.assertIn(key, c["openams"], (c["id"], key))
            else:
                self.assertIsNone(c["distances"].get("meltzone_top"), c["id"])
        self.assertTrue(any(c["openams"] for c in self.db["combos"]))

    def test_geometry_is_in_order(self):
        # the nozzle tip, then the melt zone, then the blade, then the gears
        for c in self.db["combos"]:
            d = c["distances"]
            self.assertGreater(d["nip_to_tip"], d["blade_to_tip"], c["id"])
            self.assertAlmostEqual(d["nip_to_blade"] + d["blade_to_tip"], d["nip_to_tip"], delta=0.05)
            if d.get("meltzone_top") is not None:
                self.assertLess(d["meltzone_top"], d["blade_to_tip"], c["id"])

    def test_starting_values_follow_the_geometry(self):
        for c in (c for c in self.db["combos"] if c["openams"]):
            d, o = c["distances"], c["openams"]
            # the reload reaches the stub the cut left; the unload clears the gears
            self.assertAlmostEqual(o["extrusion_reload_length"], d["nip_to_blade"], delta=0.5)
            self.assertGreater(o["extrusion_unload_length"], d["nip_to_blade"] + 3)
            # the retract takes the tip out of the melt zone and stays under the blade
            if d.get("meltzone_top") is not None:
                self.assertGreaterEqual(o["retract_length"], d["meltzone_top"])
            self.assertLess(o["retract_length"], d["blade_to_tip"] - 5)
            for key in SETTINGS:
                self.assertTrue(math.isfinite(o[key]))


class CommandTest(unittest.TestCase):

    def run_main(self, *argv):
        out = io.StringIO()
        rc = T.main(list(argv), out=out)
        return rc, out.getvalue()

    def test_list(self):
        rc, text = self.run_main("list")
        self.assertEqual(rc, 0)
        self.assertIn("sb-cw2-filamatrix-dragonhf", text)

    def test_show_by_parts_prints_settings(self):
        rc, text = self.run_main("show", "--toolhead", "stealthburner", "--extruder", "clockwork 2",
                                 "--cutter", "filamatrix", "--hotend", "dragon hf")
        self.assertEqual(rc, 0)
        self.assertIn("[gcode_macro _oams_macro_variables]", text)
        self.assertIn("variable_extrusion_reload_length:", text)

    def test_show_lists_choices_when_ambiguous(self):
        rc, text = self.run_main("show", "--toolhead", "stealthburner")
        self.assertEqual(rc, 0)
        self.assertIn("--hotend", text)

    def test_show_without_starting_values(self):
        db = T.load()
        bare = [c for c in db["combos"] if not c["openams"]]
        if not bare:
            self.skipTest("every entry has starting values")
        rc, text = self.run_main("show", bare[0]["id"])
        self.assertEqual(rc, 0)
        self.assertIn("no starting values", text)
        self.assertNotIn("[gcode_macro", text)

    def test_show_without_a_match(self):
        rc, text = self.run_main("show", "--hotend", "no such hotend")
        self.assertEqual(rc, 1)
        self.assertIn("No entry matches", text)


if __name__ == "__main__":
    unittest.main()
