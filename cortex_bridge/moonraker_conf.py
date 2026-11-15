# SPDX-License-Identifier: MIT
"""The `[update_manager openams]` entry in moonraker.conf.

After an OpenAMS update Moonraker restarts the services in its
`managed_services`. It accepts only the entry's own name, `klipper` and
`moonraker` there, and warns about anything else: an earlier installer wrote
`cortex-bridge klipper`, so Moonraker warned and restarted Klipper only.
The entry wants `klipper moonraker`: Klipper for the OpenAMS Klipper code,
Moonraker for the OpenAMS Moonraker components. Moonraker refuses an update
while a print is printing, so this never restarts anything mid-print. The
bridge relay is restarted by the openams_bridge component instead, when its
code changed and the printer is idle.

    python3 -m cortex_bridge.moonraker_conf ~/printer_data/config/moonraker.conf
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cortex_bridge.config import write_with_backup  # noqa: E402

NAME = "openams"
SECTION = "update_manager " + NAME
WANTED = ("klipper", "moonraker")
ACCEPTED = (NAME,) + WANTED          # what Moonraker allows for this entry

_HEADER = re.compile(r"^\[([^\]]+)\]\s*$")
_OPTION = re.compile(r"^managed_services\s*[:=]\s*(.*)$")


def _services(value: str) -> list:
    return value.split("#")[0].split()


def _locate(lines: list):
    """(start, end, found, stop) of the entry: its header line, the line
    after its last, and its managed_services line(s) [found, stop), or None
    for found when it has none; None when there is no entry."""
    start = None
    for index, line in enumerate(lines):
        match = _HEADER.match(line.strip())
        if match and " ".join(match.group(1).split()) == SECTION:
            start = index
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if _HEADER.match(lines[index].strip()):
            end = index
            break
    for index in range(start + 1, end):
        if _OPTION.match(lines[index]):
            # The value may go on over indented lines.
            stop = index + 1
            while stop < end and lines[stop][:1] in (" ", "\t") and \
                    lines[stop].strip():
                stop += 1
            return start, end, index, stop
    return start, end, None, None


def fixed(text: str) -> str:
    """`text` with the entry's managed_services set to what Moonraker
    accepts, `klipper` and `moonraker` included; the same text when there is
    no entry or it is already right."""
    lines = text.splitlines(keepends=True)
    located = _locate(lines)
    if located is None:
        return text
    start, end, found, stop = located
    if found is None:
        # No line: Moonraker would restart a service named after the entry,
        # which does not exist. Add the line after the entry's last option.
        last = start
        for index in range(start + 1, end):
            if lines[index].strip() and not lines[index].lstrip().startswith(
                    ("#", ";")):
                last = index
        newline = "" if lines[last].endswith("\n") else "\n"
        lines.insert(last + 1, newline + "managed_services: %s\n"
                     % " ".join(WANTED))
        return "".join(lines)
    services = _services(_OPTION.match(lines[found]).group(1)) + [
        word for line in lines[found + 1:stop] for word in _services(line)]
    keep = [svc for svc in services if svc in ACCEPTED]
    keep += [svc for svc in WANTED if svc not in keep]
    keep = list(dict.fromkeys(keep))
    if keep == services:
        return text
    ending = "\n" if lines[stop - 1].endswith("\n") else ""
    lines[found:stop] = ["managed_services: %s%s" % (" ".join(keep), ending)]
    return "".join(lines)


def entry_line(text: str) -> str:
    """The entry's managed_services line, or "" (no entry, or no line)."""
    lines = text.splitlines(keepends=True)
    located = _locate(lines)
    if located is None or located[2] is None:
        return ""
    return " ".join("".join(lines[located[2]:located[3]]).split())


def fix_file(path) -> dict:
    """Correct the entry in the moonraker.conf at `path`. Returns
    ``changed``, ``backup`` (the dated copy written first, or None) and
    ``line`` (the entry's managed_services line now, or "")."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {"changed": False, "backup": None, "line": ""}
    new = fixed(text)
    if new == text:
        return {"changed": False, "backup": None, "line": entry_line(text)}
    backup = write_with_backup(path, new)
    return {"changed": True, "backup": backup, "line": entry_line(new)}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python3 -m cortex_bridge.moonraker_conf "
              "<moonraker.conf>", file=sys.stderr)
        return 2
    result = fix_file(argv[0])
    if result["changed"]:
        print("The OpenAMS update entry now has '%s' (the old file is kept "
              "as %s)." % (result["line"], result["backup"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
