#!/usr/bin/env python3
"""Validate the C2/C4 contract examples against their JSON schemas.

Checks, each of which fails the run (exit 1):
  * every schema is itself a valid Draft 2020-12 schema;
  * every example validates against the schema its filename selects;
  * every minified openams_ui example is at most 1024 bytes (UTF-8);
  * every JSON file under examples/c2 and examples/c4 is covered by a schema
    (an unrecognized name is an error, so nothing escapes validation);
  * every oams_manager_<scenario>.json has an openams_ui_<scenario>.json and
    the other way round, and the projected unit exists in that status.

Usage: python3 docs/contracts/check_examples.py   (from the repo root or anywhere)
Requires: jsonschema (tested with 4.10.3).
"""
import json
import pathlib
import sys

import jsonschema

HERE = pathlib.Path(__file__).resolve().parent
SCHEMAS = HERE / "schemas"
EXAMPLES = HERE / "examples"
UI_MAX_BYTES = 1024

# (examples subdirectory, filename prefix, schema file)
RULES = (
    ("c2", "oams_manager_", "oams_manager_status_v2.schema.json"),
    ("c2", "openams_ui_", "openams_ui_v1.schema.json"),
    ("c4", "openams_spoolman_", "openams_spoolman_status_v1.schema.json"),
)


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def minified_size(obj):
    return len(json.dumps(obj, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8"))


def main():
    errors = []
    validators = {}
    for _sub, _prefix, schema_name in RULES:
        if schema_name in validators:
            continue
        schema = load_json(SCHEMAS / schema_name)
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except jsonschema.exceptions.SchemaError as exc:
            errors.append("%s: invalid schema: %s" % (schema_name, exc.message))
            continue
        validators[schema_name] = jsonschema.Draft202012Validator(schema)

    checked = 0
    scenarios = {"oams_manager_": {}, "openams_ui_": {}}
    for sub in sorted({rule[0] for rule in RULES}):
        directory = EXAMPLES / sub
        files = sorted(directory.glob("*.json"))
        if not files:
            errors.append("examples/%s: no examples found" % sub)
        for path in files:
            rel = path.relative_to(HERE)
            rule = next((r for r in RULES
                         if r[0] == sub and path.name.startswith(r[1])), None)
            if rule is None:
                errors.append("%s: no schema covers this file name" % rel)
                continue
            try:
                instance = load_json(path)
            except ValueError as exc:
                errors.append("%s: not valid JSON: %s" % (rel, exc))
                continue
            validator = validators.get(rule[2])
            if validator is None:
                continue
            for err in sorted(validator.iter_errors(instance),
                              key=lambda e: list(e.absolute_path)):
                where = "/".join(str(p) for p in err.absolute_path) or "(root)"
                errors.append("%s: %s: %s" % (rel, where, err.message))
            if rule[1] == "openams_ui_":
                size = minified_size(instance)
                if size > UI_MAX_BYTES:
                    errors.append("%s: minified size %d B exceeds %d B"
                                  % (rel, size, UI_MAX_BYTES))
            if rule[1] in scenarios:
                scenarios[rule[1]][path.stem[len(rule[1]):]] = instance
            checked += 1

    status, ui = scenarios["oams_manager_"], scenarios["openams_ui_"]
    for name in sorted(set(status) - set(ui)):
        errors.append("examples/c2: oams_manager_%s.json has no"
                      " openams_ui_%s.json" % (name, name))
    for name in sorted(set(ui) - set(status)):
        errors.append("examples/c2: openams_ui_%s.json has no"
                      " oams_manager_%s.json" % (name, name))
    for name in sorted(set(status) & set(ui)):
        unit = ui[name].get("unit") if isinstance(ui[name], dict) else None
        devices = status[name].get("devices", {}) \
            if isinstance(status[name], dict) else {}
        if unit not in devices:
            errors.append("examples/c2: openams_ui_%s.json projects unit %r,"
                          " absent from oams_manager_%s.json devices"
                          % (name, unit, name))

    for line in errors:
        print("FAIL " + line)
    if errors:
        print("%d problem(s) in %d example(s)" % (len(errors), checked))
        return 1
    print("OK: %d example(s) valid" % checked)
    return 0


if __name__ == "__main__":
    sys.exit(main())
