"""C5_klipper_mmu_wire.md must agree with klipper_mmu/host/wire.py.

The contract's §3 tables are the published copy of the wire's enumerated
values. If a value is renumbered in the host (which the wire's own rule
forbids) or a member is added, the document has to move with it or it drifts
silently -- so every table is parsed and compared here, name by name and
value by value, in both directions.
"""
import importlib.util
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACT = os.path.join(REPO, "docs", "contracts", "C5_klipper_mmu_wire.md")
WIRE = os.path.join(REPO, "klipper_mmu", "host", "wire.py")

# "### 3.7 `km_event_type` (`wire.EventType`)" -> (dictionary name, class name)
HEADING = re.compile(
    r"^###\s+3\.\d+\s+`(?P<enum>[a-z_]+)`\s+\(`wire\.(?P<cls>\w+)`\)\s*$")
ROW = re.compile(r"^\|\s*`(?P<name>[A-Z][A-Z0-9_]*)`\s*\|\s*(?P<value>-?\d+)\s*\|$")
# "### 3.20 Sentinel values" -- not an enumeration, so the heading pattern
# above must not claim it.
SENTINELS = {"NO_LANE", "NO_PIN"}


def load_wire():
    """wire.py without importing the klipper_mmu package (it needs Klipper)."""
    spec = importlib.util.spec_from_file_location("c5_sync_wire", WIRE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_tables(text):
    """Return {enum: (class name, {NAME: value})} for every §3 table."""
    tables = {}
    current = None
    for line in text.splitlines():
        heading = HEADING.match(line)
        if heading:
            current = (heading.group("enum"), heading.group("cls"), {})
            tables[heading.group("enum")] = current
            continue
        if current is None:
            continue
        if line.startswith("### ") or line.startswith("## "):
            current = None
            continue
        row = ROW.match(line)
        if row:
            current[2][row.group("name")] = int(row.group("value"))
    return tables


class ContractC5SyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(CONTRACT, encoding="utf-8") as handle:
            cls.text = handle.read()
        cls.tables = parse_tables(cls.text)
        cls.wire = load_wire()

    def test_contract_lists_every_wire_enumeration(self):
        self.assertEqual(sorted(self.tables), sorted(self.wire.ENUMERATIONS),
                         "C5 §3 must have one table per wire.ENUMERATIONS entry")

    def test_heading_names_the_class_it_documents(self):
        for enum, (named, cls_name, _) in sorted(self.tables.items()):
            self.assertEqual(self.wire.ENUMERATIONS[enum].__name__, cls_name,
                             "%s: heading names wire.%s" % (enum, cls_name))

    def test_member_tables_match_wire_exactly(self):
        for enum, (_, _, documented) in sorted(self.tables.items()):
            actual = self.wire.members(self.wire.ENUMERATIONS[enum])
            self.assertEqual(
                documented, actual,
                "C5 §3 %s differs from wire.%s: missing %s, extra %s, "
                "value differences %s"
                % (enum, self.wire.ENUMERATIONS[enum].__name__,
                   sorted(set(actual) - set(documented)),
                   sorted(set(documented) - set(actual)),
                   sorted(n for n in set(actual) & set(documented)
                          if actual[n] != documented[n])))

    def test_documented_member_counts_are_right(self):
        for enum, (_, _, documented) in sorted(self.tables.items()):
            match = re.search(
                r"^###\s+3\.\d+\s+`%s`[^\n]*\n\n(\d+) members"
                % re.escape(enum), self.text, re.M)
            self.assertIsNotNone(match, "%s: no member count" % enum)
            self.assertEqual(int(match.group(1)), len(documented), enum)

    def test_sentinel_values_match_wire(self):
        for name in sorted(SENTINELS):
            row = re.search(r"^\|\s*`wire\.%s`\s*\|\s*(\d+)\s*\|" % name,
                            self.text, re.M)
            self.assertIsNotNone(row, "no sentinel row for %s" % name)
            self.assertEqual(int(row.group(1)), getattr(self.wire, name), name)

    def test_terminal_event_table_matches_wire(self):
        op_names = {v: k for k, v in self.wire.members(self.wire.OpType).items()}
        event_names = {
            v: k for k, v in self.wire.members(self.wire.EventType).items()}
        for op_type, (success, cancelled, failed) in sorted(
                self.wire.TERMINAL_EVENTS.items()):
            row = re.search(
                r"^\|\s*`%s` \(%d\)\s*\|\s*`%s`\s*\|(?P<c>[^|]*)\|"
                r"\s*`%s`\s*\|$"
                % (op_names[op_type], op_type, event_names[success],
                   event_names[failed]),
                self.text, re.M)
            self.assertIsNotNone(
                row, "§5.3 has no row for OpType %d" % op_type)
            if cancelled is None:
                self.assertNotIn("`", row.group("c"),
                                 "OpType %d has no cancelled terminal"
                                 % op_type)
            else:
                self.assertIn(event_names[cancelled], row.group("c"),
                              "OpType %d cancelled terminal" % op_type)

    def test_admitted_set_is_the_one_documented(self):
        self.assertEqual(
            self.wire.ADMITTED,
            frozenset((self.wire.AdmissionResult.ACCEPTED,
                       self.wire.AdmissionResult.ALREADY_IN_PROGRESS,
                       self.wire.AdmissionResult.ALREADY_COMPLETED)))
        for name in ("ACCEPTED", "ALREADY_IN_PROGRESS", "ALREADY_COMPLETED"):
            self.assertIn("`%s`" % name, self.text,
                          "§5.2 does not name %s" % name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
