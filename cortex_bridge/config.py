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
    ] + ([
        "# Leave it at 0 and let the AMS measure each bay, with someone",
        "# watching: OAMS_CALIBRATE_PTFE_LENGTH OAMS=%d SPOOL=<bay>" % oams_idx,
    ] if unit.get("ptfe_calibration") else [
        "# Measure the path, then: python3 ~/openams/tools/cortex-bridge.py "
        "ptfe <millimetres>",
    ]) + [
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


# ---------------------------------------------------------------------------
# An oams.cfg that still describes OpenAMS 1 hardware
# ---------------------------------------------------------------------------

REPLACED_MARK = "# cortex_bridge config: "
_OPTION = re.compile(r"^([A-Za-z0-9_]+)\s*[:=]\s*(.*?)\s*$")
_GROUP_MEMBER = re.compile(r"^(.+)-(\d+)$")


def _sections(lines: list) -> list:
    """The active (uncommented) sections: dicts with `name`, `start` (header
    line index), `end` (exclusive) and `options` (first value of each)."""
    sections = []
    for index, line in enumerate(lines):
        match = re.match(r"^\[([^\]]+)\]\s*$", line)
        if match:
            if sections:
                sections[-1]["end"] = index
            sections.append({"name": " ".join(match.group(1).split()),
                             "start": index, "end": len(lines),
                             "options": {}})
            continue
        if sections and not line.startswith((" ", "\t", "#", ";")):
            option = _OPTION.match(line)
            if option:
                sections[-1]["options"].setdefault(
                    option.group(1).lower(), option.group(2))
    return sections


def openams1_hardware(text: str) -> dict:
    """What in `text` belongs to OpenAMS 1 rather than the bridge: `units`,
    (oams name, mcu name) pairs whose board is not a bridge port; `wired_fps`,
    FPS sections read from a pin; `bridge_units`, units already on the
    bridge."""
    sections = _sections(text.splitlines())
    mcus = {s["name"].split(None, 1)[1]: s for s in sections
            if s["name"].startswith("mcu ")}
    found = {"units": [], "wired_fps": [], "bridge_units": []}
    for section in sections:
        name = section["name"]
        if name.startswith("oams "):
            mcu = section["options"].get("mcu", "")
            board = mcus.get(mcu)
            serial = board["options"].get("serial", "") if board else ""
            if re.match(r"^/dev/(?:serial/by-id/)?oams/", serial):
                found["bridge_units"].append(name.split(None, 1)[1])
            else:
                found["units"].append((name.split(None, 1)[1], mcu))
        elif (name == "fps" or name.startswith("fps ")) \
                and "pin" in section["options"]:
            found["wired_fps"].append((name, section["options"]["pin"]))
    return found


def describe_openams1(found: dict) -> str:
    parts = ["[oams %s] (board %s, not on the bridge)" % unit
             for unit in found["units"]]
    parts += ["[%s] read from pin %s" % fps for fps in found["wired_fps"]]
    return ", ".join(parts)


def replace_openams1(text: str) -> tuple:
    """Comment out the OpenAMS 1 sections of `text` so the bridge's units can
    take their place. Returns (new_text, [(section, why), ...]). Never deletes
    a line: each section is commented and marked with REPLACED_MARK.

    Commented: each OpenAMS 1 `[oams]` and its `[mcu]`; every section that
    uses that board (`i2c_mcu:`/`mcu:` naming it, or a `<board>:` pin); FPS
    sections read from a pin; the filament groups made only of those units'
    bays (an empty group too, so the bridge's T0.. can take its name); and
    `[hdc1080]` once no HDC1080 sensor is left. Other boards' `[mcu]`
    sections, an FPS board's included, are left alone."""
    found = openams1_hardware(text)
    if found["bridge_units"] and (found["units"] or found["wired_fps"]):
        raise ConfigError(
            "oams.cfg has units on the bridge (%s) and OpenAMS 1 hardware "
            "(%s); a mixed setup is edited by hand"
            % (", ".join(found["bridge_units"]), describe_openams1(found)))
    lines = text.splitlines(keepends=True)
    sections = _sections([line.rstrip("\n") for line in lines])
    unit_names = {unit for unit, _mcu in found["units"]}
    boards = {mcu for _unit, mcu in found["units"] if mcu}
    why = {}
    for section in sections:
        name, options = section["name"], section["options"]
        kind, _, rest = name.partition(" ")
        if kind == "oams" and rest in unit_names:
            why[name] = "OpenAMS 1 unit, replaced by the Cortex Bridge"
        elif kind == "mcu" and rest in boards:
            why[name] = "the OpenAMS 1 unit's board"
        elif (name, options.get("pin")) in found["wired_fps"]:
            why[name] = ("the buffer is read through the AMS now ([fps] "
                         "source_oams below)")
        elif kind == "filament_group":
            members = [m.strip() for m in options.get("group", "").split(",")
                       if m.strip()]
            owners = set()
            for member in members:
                match = _GROUP_MEMBER.match(member)
                owners.add(match.group(1) if match else member)
            if owners - unit_names:
                raise ConfigError(
                    "[%s] mixes OpenAMS 1 bays with %s; edit it by hand"
                    % (name, ", ".join(sorted(owners - unit_names))))
            why[name] = "groups are rebuilt for the Cortex Bridge AMS below"
        elif any(value in boards or any(
                re.search(r"(?:^|[\s,!^~])%s:" % re.escape(board), value)
                for board in boards)
                for value in options.values()):
            why[name] = "uses the OpenAMS 1 board"
    hdc_left = any(s["options"].get("sensor_type", "").upper() == "HDC1080"
                   for s in sections if s["name"] not in why)
    if not hdc_left and any(s["name"] == "hdc1080" for s in sections):
        why["hdc1080"] = "no HDC1080 sensor is left"
    out = []
    replaced = []
    for index, line in enumerate(lines):
        section = next((s for s in sections
                        if s["start"] <= index < s["end"]), None)
        reason = why.get(section["name"]) if section else None
        if reason and index == section["start"]:
            out.append("%s%s\n" % (REPLACED_MARK, reason))
            replaced.append((section["name"], reason))
        if reason and line.strip() and not line.lstrip().startswith("#"):
            out.append("#" + line)
        else:
            out.append(line)
    return "".join(out), replaced


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
    found = openams1_hardware(text)
    if found["units"] or found["wired_fps"]:
        raise ConfigError(
            "oams.cfg still describes OpenAMS 1 hardware: %s. Adding the "
            "bridge's AMS beside it would mix the two setups. To replace the "
            "OpenAMS 1 setup with the Cortex Bridge, run python3 "
            "~/openams/tools/cortex-bridge.py config --write "
            "--replace-openams1: it comments those sections out and keeps a "
            "copy of the old file. To run both, add the bridge's AMS by hand "
            "on its own [fps NAME] lane." % describe_openams1(found))
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
