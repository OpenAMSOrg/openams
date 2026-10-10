"""oams_tip_forming.cfg renders the moves each profile style describes, and
every profile parks the tip where the unload's extrusion_unload_length starts.

The macro is rendered with jinja2 configured the way Klipper's gcode_macro
does, against a stub of the printer objects it reads.
"""

import ast
import configparser
import os
import re
import unittest

try:
    import jinja2
except ImportError:                                # pragma: no cover
    jinja2 = None

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
G1 = re.compile(r"^G1 E(-?[0-9.]+) F([0-9]+)$")


def load():
    parser = configparser.RawConfigParser(strict=False)
    with open(os.path.join(_ROOT, "oams_tip_forming.cfg")) as handle:
        parser.read_string(handle.read())
    raw = parser.get("gcode_macro _oams_tip_forming", "variable_profiles")
    profiles = ast.literal_eval("\n".join(
        line for line in raw.splitlines() if not line.strip().startswith("#")))
    gcode = parser.get("gcode_macro OAMS_FORM_TIP", "gcode")
    return profiles, gcode


class AttrDict(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


def render(gcode, profiles, material):
    env = jinja2.Environment("{%", "%}", "{", "}")
    printer = {
        "gcode_macro _oams_tip_forming": AttrDict(
            default_material="PLA", profiles=profiles),
        "toolhead": AttrDict(extruder="extruder"),
        "extruder": AttrDict(pressure_advance=0.005, target=210.0),
    }

    def raise_error(message):
        raise RuntimeError(message)
    text = env.from_string(gcode).render(
        printer=printer, params={"MATERIAL": material},
        action_raise_error=raise_error)
    return [line.strip() for line in text.splitlines() if line.strip()]


@unittest.skipIf(jinja2 is None, "jinja2 is not installed")
class TipFormingTest(unittest.TestCase):
    def setUp(self):
        self.profiles, self.gcode = load()

    def moves_after_ram(self, material):
        lines = render(self.gcode, self.profiles, material)
        moves = [G1.match(line) for line in lines]
        moves = [(float(m.group(1)), int(m.group(2))) for m in moves if m]
        ram = len(self.profiles[material]["ram"])
        return moves[:ram], moves[ram:]

    def test_u1_pla_follows_the_snapmaker_sequence_slowed_for_pla(self):
        ram, moves = self.moves_after_ram("PLA_U1")
        self.assertEqual(moves, [
            (-35.0, 3000), (-5.0, 30),          # out fast, crawl at 0.5 mm/s
            (25.0, 1002), (7.0, 1500),          # re-melt dip down to 8 mm
            (-27.0, 3300), (-5.0, 15),          # out fast, final crawl 0.25 mm/s
            (-10.0, 1500),                      # park at 50 mm
        ])

    def test_u1_abs_dips_slower_and_crawls_at_0_83(self):
        _, moves = self.moves_after_ram("ABS")
        self.assertEqual(moves[2], (25.0, 402))
        self.assertEqual(moves[5], (-5.0, 50))

    def test_every_profile_parks_the_tip_at_its_park_height(self):
        for material, profile in self.profiles.items():
            ram, moves = self.moves_after_ram(material)
            park = profile.get("park_to", profile.get("cool_to"))
            net = sum(e for e, _ in moves)
            self.assertAlmostEqual(-net, park, places=2, msg=material)

    def test_the_u1_style_has_no_cooling_strokes(self):
        _, moves = self.moves_after_ram("ABS")
        directions = [e > 0 for e, _ in moves]
        # one dip down (two moves) between the pulls, nothing else forward
        self.assertEqual(directions.count(True), 2)

    def test_pla_uses_the_classic_recipe(self):
        self.assertEqual(self.profiles["PLA"]["style"], "classic")
        self.assertEqual(self.profiles["PLA"]["temp"], 192)
        self.assertEqual(self.profiles["ABS"]["style"], "u1")

    def test_an_unknown_material_refuses(self):
        with self.assertRaises(RuntimeError):
            render(self.gcode, self.profiles, "TPU")


if __name__ == "__main__":
    unittest.main()
