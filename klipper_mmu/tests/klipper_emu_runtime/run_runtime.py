#!/usr/bin/env python3
"""Run the H723 EMU native operation fixture against one retained firmware image."""
# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

PACKAGE = Path(__file__).resolve().parents[2]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--autoconf", type=Path, required=True)
    parser.add_argument("--dictionary", type=Path, required=True)
    parser.add_argument("--build-dir", type=Path, required=True)
    args = parser.parse_args()
    autoconf = args.autoconf.resolve()
    dictionary = args.dictionary.resolve()
    artifact = autoconf.parent
    manifest_path = artifact / "manifest.json"
    if not autoconf.is_file() or not dictionary.is_file() or not manifest_path.is_file():
        parser.error("autoconf, dictionary, and their saved image manifest are required")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("device") != "EMU" or manifest.get("lane_count") != 5 \
            or manifest.get("actuation_opt_in") is not True:
        parser.error("saved image is not the five-lane EMU actuation candidate")
    expected_hashes = manifest.get("sha256", {})
    for filename, path in (("autoconf.h", autoconf), ("klipper.dict", dictionary)):
        if expected_hashes.get(filename) != digest(path):
            parser.error(f"saved image hash mismatch: {path}")
    dictionary_data = json.loads(dictionary.read_text())
    commands = dictionary_data.get("commands", {})
    required = {
        "km_diagnostic_start", "km_diagnostic_query challenge=%u",
        "km_start instance_hi=%u instance_lo=%u client=%c session=%u request=%u operation=%c lane=%c direction=%c",
        "km_event_ack instance_hi=%u instance_lo=%u client=%c session=%u request=%u operation=%c lane=%c seq=%u",
    }
    missing = required.difference(commands)
    if missing:
        parser.error("image dictionary lacks production commands: " + ", ".join(sorted(missing)))

    build_dir = args.build_dir.resolve()
    build_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "make", "-s", "-C", str(PACKAGE / "tests/klipper_emu_runtime"),
        f"PRODUCT_ROOT={PACKAGE}", f"IMAGE_AUTOCONF={autoconf}",
        f"BUILD_DIR={build_dir}", "test",
    ], check=True, timeout=150)
    print(f"PASS: native hosted operation cycle matches saved image {digest(dictionary)}")


if __name__ == "__main__":
    main()
