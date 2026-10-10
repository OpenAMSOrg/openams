# SPDX-License-Identifier: MIT
"""Write the Klipper config for the AMS units behind a Cortex Bridge.

`render_units` turns discovered units into `[mcu]`, `[oams]`,
`[filament_group]` and `[fps]` sections. `add_units` appends only the units a
config file does not mention yet, so running it again after adding an AMS
never touches sections you have already calibrated.
"""

from __future__ import annotations

import datetime
from pathlib import Path
import re

# Encoder clicks per millimetre of filament (the AMS odometer's resolution).
# ptfe_length is configured in clicks.
CLICKS_PER_MM = 1.14

UNIT_BASE_NAMES = {"ams1": "ams1", "ams2": "ams2", "ams_ht": "ams_ht"}
CAN_SAMPLE_PLACEHOLDERS = ("<your_unique_FPS_UUID>",
                           "<your_unique_OAMS_MCU1_UUID>")
BEGIN_MARK = "# >>> Cortex Bridge units"
END_MARK = "# <<< Cortex Bridge units"

_SECTION = re.compile(r"^\s*\[([^\]]+)\]", re.MULTILINE)
_SERIAL = re.compile(
    r"^\s*serial\s*[:=]\s*/dev/(?:serial/by-id/)?oams/([0-9a-f]{12})\s*$",
    re.MULTILINE)
_OAMS_IDX = re.compile(r"^\s*oams_idx\s*[:=]\s*(\d+)", re.MULTILINE)
_TOOL_GROUP = re.compile(r"^\s*\[filament_group\s+T(\d+)\s*\]", re.MULTILINE)


class ConfigError(RuntimeError):
    pass


def mm_to_clicks(millimetres: float) -> int:
    if millimetres <= 0:
        raise ConfigError("the length must be more than 0 mm")
    return int(round(millimetres * CLICKS_PER_MM))


def unit_name(family: str, taken: set) -> str:
    base = UNIT_BASE_NAMES.get(family, "ams")
    name = base
    index = 2
    while name in taken:
        name = "%s_%d" % (base, index)
        index += 1
    return name


def _bay_list(value: str, bays: int) -> str:
    return ", ".join([value] * bays)


def render_unit(unit: dict, name: str, oams_idx: int) -> str:
    bays = max(1, int(unit.get("bays") or 1))
    family = unit.get("family") if unit.get("family") in UNIT_BASE_NAMES \
        else "auto"
    lines = [
        "# %s on the Cortex Bridge" % unit.get("name", "AMS"),
        "[mcu %s]" % name,
        "serial: /dev/oams/%s" % unit["uid"],
        "restart_method: command",
        "",
        "[oams %s]" % name,
        "mcu: %s" % name,
        "device_kind: %s" % family,
        "fps_upper_threshold: 0.75",
        "fps_lower_threshold: 0.25",
        "fps_is_reversed: false",
        "f1s_hes_on: %s" % _bay_list("0.5", bays),
        "f1s_hes_is_above: true",
        "hub_hes_on: %s" % _bay_list("0.5", bays),
        "hub_hes_is_above: true",
        "# Filament path from this AMS to the extruder gears, in encoder",
        "# clicks (millimetres x %.2f). Loads are refused while it is 0." %
        CLICKS_PER_MM,
        "# Measure the path, then: python3 ~/openams/tools/cortex-bridge.py "
        "ptfe <millimetres>",
        "ptfe_length: %d" % int(unit.get("ptfe_length") or 0),
        "current_target: 0.30",
        "current_kp: 0.375",
        "current_ki: 0.0",
        "current_kd: 0.0",
        "oams_idx: %d" % oams_idx,
        "",
    ]
    return "\n".join(lines)


def render_units(units: list, *, taken: set | None = None,
                 first_tool: int = 0, first_idx: int = 1,
                 with_fps: bool = True) -> tuple:
    """Return (text, names) for `units`; `taken` holds section names in use."""
    taken = set(taken or ())
    blocks = []
    groups = []
    names = []
    tool = first_tool
    for offset, unit in enumerate(units):
        name = unit_name(unit.get("family", ""), taken)
        taken.add(name)
        names.append(name)
        blocks.append(render_unit(unit, name, first_idx + offset))
        for bay in range(max(1, int(unit.get("bays") or 1))):
            groups.append("[filament_group T%d]\ngroup: %s-%d\n" %
                          (tool, name, bay))
            tool += 1
    parts = blocks + groups
    if with_fps and names:
        parts.append(
            "# The toolhead buffer: each AMS relays its reading.\n"
            "[fps]\n"
            "source_oams: %s\n"
            "reversed: false\n"
            "extruder: extruder\n" % names[0])
    return "\n".join(parts), names


def configured_uids(text: str) -> set:
    return set(_SERIAL.findall(text))


def add_units(text: str, units: list) -> tuple:
    """Return (new_text, added_names) with the missing units appended."""
    if any(placeholder in text for placeholder in CAN_SAMPLE_PLACEHOLDERS):
        raise ConfigError(
            "this oams.cfg is still the CAN sample (it has placeholder "
            "canbus_uuid lines); start from the Cortex Bridge sample with "
            "--fresh")
    known = configured_uids(text)
    missing = [unit for unit in units if unit["uid"] not in known]
    if not missing:
        return text, []
    sections = set()
    for section in _SECTION.findall(text):
        parts = section.split()
        if len(parts) == 2 and parts[0] in ("mcu", "oams"):
            sections.add(parts[1])
    tools = [int(n) for n in _TOOL_GROUP.findall(text)]
    indexes = [int(n) for n in _OAMS_IDX.findall(text)]
    has_fps = any(section.split()[0] == "fps"
                  for section in _SECTION.findall(text))
    block, names = render_units(
        missing, taken=sections,
        first_tool=max(tools) + 1 if tools else 0,
        first_idx=max(indexes) + 1 if indexes else 1,
        with_fps=not has_fps)
    stamp = datetime.date.today().isoformat()
    addition = "\n%s (added %s by cortex_bridge config)\n%s\n%s\n" % (
        BEGIN_MARK, stamp, block.rstrip("\n"), END_MARK)
    return text.rstrip("\n") + "\n" + addition, names


def write_with_backup(path: Path, text: str) -> Path | None:
    """Replace `path` with `text`, keeping the old file as a dated backup."""
    backup = None
    if path.exists():
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name("%s.bak-%s" % (path.name, stamp))
        backup.write_bytes(path.read_bytes())
    # Rewrite in place rather than rename over it, so a symlinked config
    # stays a symlink (as the installer does).
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return backup
