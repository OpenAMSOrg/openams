"""The C2/C4 contract examples stay in step with the generator and with src.

``docs/contracts/tools/gen_examples.py`` builds the examples; its ``project_ui``
repeats the derivation in ``src/openams_ui.py``. These tests fail when the
checked-in examples drift from a fresh generation, or when the generator's
projection drifts from the real one. Run with::

    python3 -m unittest discover -s test -p 'test_contract_examples.py' -v
"""

import importlib
import importlib.util
import json
import os
import sys
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
_GEN = os.path.join(_ROOT, "docs", "contracts", "tools", "gen_examples.py")

if "oamspkg" not in sys.modules:
    pkg = types.ModuleType("oamspkg")
    pkg.__path__ = [_SRC]
    sys.modules["oamspkg"] = pkg
openams_ui = importlib.import_module("oamspkg.openams_ui")

spec = importlib.util.spec_from_file_location("gen_examples", _GEN)
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)


def roundtrip(value):
    return json.loads(json.dumps(value))


class GeneratedExamplesTests(unittest.TestCase):
    def test_the_checked_in_examples_match_a_fresh_generation(self):
        self.assertEqual(gen.check(), [])

    def test_check_mode_detects_drift(self):
        rendered = gen.render()
        path = sorted(rendered)[0]
        with open(path) as handle:
            original = handle.read()
        try:
            with open(path, "w") as handle:
                handle.write(original.replace('"v"', '"vv"', 1)
                             if '"v"' in original else original + " ")
            self.assertEqual(gen.check(), [path])
        finally:
            with open(path, "w") as handle:
                handle.write(original)
        self.assertEqual(gen.check(), [])

    def test_the_generators_projection_is_the_real_one(self):
        checked = 0
        for name, (status, unit) in gen.scenarios().items():
            with self.subTest(scenario=name):
                self.assertEqual(
                    roundtrip(openams_ui.project(status, unit)),
                    roundtrip(gen.project_ui(status, unit)))
            checked += 1
        self.assertGreaterEqual(checked, 6)


class DocumentationLinkTests(unittest.TestCase):
    def test_every_relative_markdown_link_resolves(self):
        sys.path.insert(0, os.path.join(_ROOT, "tools"))
        try:
            import check_links
            import contextlib
            import io
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                status = check_links.main([])
        finally:
            sys.path.remove(os.path.join(_ROOT, "tools"))
        self.assertEqual(status, 0, out.getvalue())


if __name__ == "__main__":
    unittest.main()
