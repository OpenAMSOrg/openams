# SPDX-License-Identifier: MIT
"""The Cortex Bridge firmware packages that ship with this OpenAMS checkout.

Bridge firmware is published as signed, encrypted `.oac2` packages in
`cortex_bridge/firmware/`, listed in `manifest.json`. Updating OpenAMS (the
Moonraker update button or `git pull`) therefore also brings new bridge
firmware, always alongside the host software it was released with. A package
is opaque to this computer: only the bridge's boot can decrypt it, and it
refuses anything not signed for it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

FIRMWARE_DIR = Path(__file__).resolve().parent / "firmware"
MANIFEST = FIRMWARE_DIR / "manifest.json"


class FirmwareError(RuntimeError):
    pass


def bundled_packages(directory: Path = FIRMWARE_DIR) -> list:
    """Every listed package, newest first, each with `path` and `ok`."""
    manifest = directory / "manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except ValueError as exc:
        raise FirmwareError("unreadable %s: %s" % (manifest, exc))
    packages = []
    for entry in data.get("packages", []):
        path = directory / str(entry.get("file", ""))
        item = dict(entry)
        item["path"] = str(path)
        item["ok"] = False
        item["problem"] = ""
        if not path.is_file():
            item["problem"] = "file missing"
        else:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != str(entry.get("sha256", "")).lower():
                item["problem"] = "checksum mismatch"
            else:
                item["ok"] = True
        packages.append(item)
    packages.sort(key=lambda item: int(item.get("version") or 0),
                  reverse=True)
    return packages


def latest_bundled(directory: Path = FIRMWARE_DIR):
    for package in bundled_packages(directory):
        if package["ok"]:
            return package
    return None
