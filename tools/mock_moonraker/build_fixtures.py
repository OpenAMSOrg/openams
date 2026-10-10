#!/usr/bin/env python3
"""Build one mock-Moonraker fixture per C2 scenario from the contract examples.

    python3 tools/mock_moonraker/build_fixtures.py [--examples DIR] [--out DIR]

For scenario `X` the fixture is

  * `c2_status` at `t=0`   = `examples/c2/oams_manager_X.json`;
  * `openams_ui` at `t=0`  = `examples/c2/openams_ui_X.json` (its `unit` field
    names the unit the projection belongs to);
  * `component_status` at `t=0` = the C4 example this script's table selects.

The output is deterministic: the same inputs always produce byte-identical
files, so a re-run leaves `fixtures/` unchanged in a diff.
"""

import argparse
import glob
import hashlib
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import fixtures as fixtures_mod  # noqa: E402  (same directory, script mode)

_REPO = os.path.dirname(os.path.dirname(_HERE))
_EXAMPLES = os.path.join(_REPO, "docs", "contracts", "examples")
_OUT = os.path.join(_HERE, "fixtures")

# The C4 component status a C2 scenario shows. Every C2 scenario is a status
# scenario, not a spool-linking one, so `linked` is the default; a unit whose
# path length is not configured yet (`uncalibrated`) is still being set up, so
# its newest tag read is left awaiting confirmation. No C2 scenario name means
# an offline linker, so `offline` is not used.
C4_BY_SCENARIO = {
    "uncalibrated": "pending_confirmation",
}
C4_DEFAULT = "linked"

# Deterministic, scenario-specific seed (this is a recorded log; the seed only
# needs to be stable so regenerated fixtures never differ).
_SEED_EPOCH = 20260927


def scenario_names(examples_dir):
    """Every C2 scenario name, from the `oams_manager_*.json` examples."""
    pattern = os.path.join(examples_dir, "c2", "oams_manager_*.json")
    names = []
    for path in sorted(glob.glob(pattern)):
        base = os.path.basename(path)
        names.append(base[len("oams_manager_"):-len(".json")])
    return names


def seed_for(scenario):
    digest = hashlib.sha256(scenario.encode("utf-8")).hexdigest()
    return (_SEED_EPOCH + int(digest[:6], 16)) % 1000000007


def family_map_for(c2_status):
    units = c2_status.get("topology", {}).get("oams", {})
    return {unit: units[unit].get("family") for unit in sorted(units)}


def build(name, examples_dir):
    """Build the fixture object for scenario `name`."""
    with open(os.path.join(examples_dir, "c2", "oams_manager_%s.json" % name)) as handle:
        c2_status = json.load(handle)
    with open(os.path.join(examples_dir, "c2", "openams_ui_%s.json" % name)) as handle:
        ui = json.load(handle)

    c4_name = C4_BY_SCENARIO.get(name, C4_DEFAULT)
    with open(os.path.join(examples_dir, "c4",
                           "openams_spoolman_%s.json" % c4_name)) as handle:
        component_status = json.load(handle)

    return {
        "scenario": name,
        "seed": seed_for(name),
        "family_map": family_map_for(c2_status),
        "events": [
            {"t": 0.0, "kind": "c2_status", "unit": None, "payload": c2_status},
            {"t": 0.0, "kind": "openams_ui", "unit": ui["unit"], "payload": ui},
            {"t": 0.0, "kind": "component_status", "unit": None,
             "payload": component_status},
        ],
    }


def write(fixture, out_dir):
    path = os.path.join(out_dir, "%s.json" % fixture["scenario"])
    with open(path, "w") as handle:
        handle.write(json.dumps(fixture, indent=2) + "\n")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--examples", default=_EXAMPLES,
                        help="the contracts examples directory (default %s)" % _EXAMPLES)
    parser.add_argument("--out", default=_OUT,
                        help="fixture output directory (default %s)" % _OUT)
    args = parser.parse_args(argv)

    names = scenario_names(args.examples)
    if not names:
        sys.stderr.write("build_fixtures: no oams_manager_*.json under %s\n"
                         % os.path.join(args.examples, "c2"))
        return 1

    if not os.path.isdir(args.out):
        os.makedirs(args.out)

    for name in names:
        fixture = build(name, args.examples)
        # Validate what we are about to write, so a bad mapping fails here.
        fixtures_mod.validate(fixture, source=name)
        path = write(fixture, args.out)
        sys.stdout.write("%s -> %s (component_status: %s)\n"
                         % (name, os.path.relpath(path, _REPO),
                            C4_BY_SCENARIO.get(name, C4_DEFAULT)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
