"""What openams_ui offers of a klipper-mmu unit's own operations.

C2 §6.2's per-bay letters are append-only: `l u c a d` keep their order and
their meaning, and a klipper-mmu unit adds `p` preload, `q` unpreload and `e`
eject after them. `home` is the unit's own action, so it travels in `act`
and only where the family has a selector to move (C5 §7.1).

  python3 -m unittest discover -s test -p 'test_mmu_ops*.py' -v
"""

import importlib
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _path in (os.path.join(_ROOT, "tools"), os.path.join(_ROOT, "src"),
              _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from test_mmu_ops import MmuUnit, mmu_manager

ui = importlib.import_module("oamspkg.openams_ui")

# The five letters C2 §6.2 published, in order, and the three a klipper-mmu
# unit adds after them.
OLD_LETTERS = "l u c a d".split()
NEW_LETTERS = {"p": "preload", "q": "unpreload", "e": "eject"}


def view(unit, name="box"):
    return ui.project(mmu_manager(unit).get_status(0.0), name)


class BayLetterTests(unittest.TestCase):
    def letters(self, projected):
        return [bay["a"] for bay in projected["bays"]]

    def test_a_klipper_mmu_unit_adds_three_letters_after_the_five(self):
        for letters in self.letters(view(MmuUnit())):
            self.assertEqual(letters, "apqe")
            self.assertTrue(letters.startswith("a"), letters)

    def test_the_letters_keep_the_contract_order(self):
        """`l u c a d p q e` is the contract's order (C2 §6.2), and the five
        a UI already knows keep their meaning and their places."""
        order = [code for _name, code in ui._BAY_ACTION_CODES]
        self.assertEqual(order, OLD_LETTERS + list(NEW_LETTERS))
        for letters in self.letters(view(MmuUnit())):
            positions = [order.index(letter) for letter in letters]
            self.assertEqual(positions, sorted(positions), letters)


class UnitActionTests(unittest.TestCase):
    def test_home_is_offered_only_where_the_family_has_a_selector(self):
        for family, offered in (("claymore", True), ("ercf_1_1", True),
                                ("mmx", True), ("boxturtle", False),
                                ("nightowl", False)):
            with self.subTest(family=family):
                self.assertEqual(
                    "home" in view(MmuUnit(family=family))["act"], offered)


class CapabilityTests(unittest.TestCase):
    """C2 §4.4.1: the flags a UI gates on, published with the unit."""

    def capabilities(self, unit):
        return mmu_manager(unit).get_status(0.0)["devices"]["box"][
            "capabilities"]

    def test_a_selector_unit_publishes_all_four(self):
        caps = self.capabilities(MmuUnit(family="claymore"))
        self.assertEqual([caps[op] for op in ("preload", "unpreload", "eject",
                                              "home")],
                         [True, True, True, True])

    def test_a_feeder_per_lane_unit_publishes_three(self):
        caps = self.capabilities(MmuUnit(family="boxturtle"))
        self.assertEqual([caps[op] for op in ("preload", "unpreload", "eject",
                                              "home")],
                         [True, True, True, False])


if __name__ == "__main__":
    unittest.main(verbosity=2)
