# SPDX-License-Identifier: MIT
"""moonraker.asvc: the services Moonraker may restart, one name per line.

Moonraker reads the file line by line. It writes its default list without a
final newline, and an earlier OpenAMS installer appended its relay with a
plain `echo >>`, so the relay's name was glued to the last service
("crowsnestcortex-bridge"): Moonraker then allowed neither, and a later run
appended the relay again. `repair` splits exactly that suffix back off,
keeps one name per line, drops repeats and keeps the order; `allow` repairs,
then adds a name only when it is missing, after a newline.

    python3 -m cortex_bridge.moonraker_asvc repair   <moonraker.asvc>
    python3 -m cortex_bridge.moonraker_asvc allow    <moonraker.asvc> NAME
    python3 -m cortex_bridge.moonraker_asvc disallow <moonraker.asvc> NAME
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cortex_bridge.config import write_with_backup  # noqa: E402

RELAY = "cortex-bridge"


def names(text: str) -> list:
    """The names in the file, in order (whitespace separates them)."""
    return text.split()


def repaired(text: str, suffix: str = RELAY) -> str:
    """`text` with a name glued in front of `suffix` split off, one name per
    line, repeats dropped, order kept; the same text when nothing needs
    it (a missing final newline alone is left as it is). Only the exact
    `suffix` is split: other glued names are never guessed at."""
    out = []
    for name in names(text):
        parts = [name]
        if name != suffix and name.endswith(suffix):
            parts = [name[:-len(suffix)], suffix]
        for part in parts:
            if part not in out:
                out.append(part)
    one_per_line = all(len(line.split()) <= 1 for line in text.splitlines())
    if one_per_line and out == names(text):
        return text
    return "".join(name + "\n" for name in out)


def _write(path: Path, old: str, new: str) -> dict:
    if new == old:
        return {"changed": False, "backup": None}
    return {"changed": True, "backup": write_with_backup(path, new)}


def repair(path) -> dict:
    """Repair the file at `path`: ``changed`` and ``backup`` (the copy kept
    first, or None). A missing file is left missing."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {"changed": False, "backup": None}
    return _write(path, text, repaired(text))


def allow(path, name: str = RELAY) -> dict:
    """Repair, then add `name` when it is not there yet; ``added`` says
    whether it was. A missing file is left missing (Moonraker writes it)."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {"changed": False, "backup": None, "added": False}
    new = repaired(text)
    added = name not in names(new)
    if added:
        if new and not new.endswith("\n"):
            new += "\n"
        new += name + "\n"
    return dict(_write(path, text, new), added=added)


def disallow(path, name: str = RELAY) -> dict:
    """Repair, then take every line naming `name` out."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {"changed": False, "backup": None}
    new = repaired(text)
    if name in names(new):
        new = "".join(line for line in new.splitlines(keepends=True)
                      if line.strip() != name)
    return _write(path, text, new)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    actions = {"repair": repair, "allow": allow, "disallow": disallow}
    if len(argv) not in (2, 3) or argv[0] not in actions or \
            (argv[0] == "repair") != (len(argv) == 2):
        print("usage: python3 -m cortex_bridge.moonraker_asvc "
              "repair|allow|disallow <moonraker.asvc> [NAME]",
              file=sys.stderr)
        return 2
    path = Path(argv[1])
    before = path.read_text(encoding="utf-8") if path.is_file() else None
    result = actions[argv[0]](*argv[1:])
    if before is not None and repaired(before) != before:
        print("moonraker.asvc: one service name per line again (the old "
              "file is kept as %s)." % result["backup"])
    if result.get("added"):
        print("Allowed Moonraker to manage the %s service." % argv[2])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
