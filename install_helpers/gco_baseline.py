#!/usr/bin/env python3
"""Whether gco-routines accepts this Klipper's G-code dispatcher.

    python3 install_helpers/gco_baseline.py --klipper DIR --gco-routines DIR

gco-routines replaces part of Klipper's G-code dispatcher, so [gco_routines]
loads only when the SHA-256 of klippy/gcode.py is one it was tested against.
Any other version stops Klipper at startup with "Unsupported Klipper
GCodeDispatch baseline". Installing gco-routines is harmless either way; this
says whether enabling it is.

The accepted hashes are read from the gco-routines checkout, from
KNOWN_GCODE_SHA256 in klippy_extra/gco_routines/integration.py. That file is
parsed, never imported or run.

Prints two KEY=value lines and exits 0:
  GCODE_PY_SHA256=<hash, or empty when klippy/gcode.py is missing>
  GCO_BASELINE=yes | no | unknown
`unknown` means the checkout or its list could not be read.
"""

import argparse
import ast
import hashlib
import os
import re

LIST_FILE = os.path.join("klippy_extra", "gco_routines", "integration.py")
LIST_NAME = "KNOWN_GCODE_SHA256"
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def gcode_sha256(klipper):
    try:
        with open(os.path.join(klipper, "klippy", "gcode.py"), "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def accepted_hashes(gco_routines):
    """The hashes gco-routines accepts, or None when they cannot be read."""
    try:
        with open(os.path.join(gco_routines, LIST_FILE), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
    except (OSError, SyntaxError, ValueError):
        return None
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == LIST_NAME for t in node.targets)):
            continue
        value = node.value
        # frozenset({...}) or a bare literal
        if isinstance(value, ast.Call) and len(value.args) == 1:
            value = value.args[0]
        try:
            hashes = ast.literal_eval(value)
        except ValueError:
            return None
        if (isinstance(hashes, (set, frozenset, list, tuple)) and hashes
                and all(isinstance(h, str) and SHA256.match(h) for h in hashes)):
            return set(hashes)
        return None
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--klipper", required=True)
    parser.add_argument("--gco-routines", required=True)
    args = parser.parse_args(argv)
    sha = gcode_sha256(args.klipper)
    hashes = accepted_hashes(args.gco_routines)
    if sha is None or hashes is None:
        verdict = "unknown"
    else:
        verdict = "yes" if sha in hashes else "no"
    print("GCODE_PY_SHA256=%s" % (sha or ""))
    print("GCO_BASELINE=%s" % verdict)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
