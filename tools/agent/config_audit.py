#!/usr/bin/env python3
# Copyright (C) 2026 JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

"""Static audit of a printer's Klipper config for OpenAMS.

Run before and after an agent edits a printer's configuration:

    python3 tools/agent/config_audit.py --config-dir ~/printer_data/config

Findings are printed one per line as ``LEVEL CODE file:line message``; the last
line is a summary. Exit status is 0 without errors, 1 with errors and 2 for a
usage or I/O problem. ``--json`` prints the same findings as JSON and
``--commands`` cross-checks the macros against the commands a running Klipper
registers.
"""

import argparse
import glob
import json
import os
import re
import sys

EXIT_OK = 0
EXIT_ERRORS = 1
EXIT_USAGE = 2

ERROR = "ERROR"
WARN = "WARN"
MANUAL = "MANUAL"
INFO = "INFO"

LEVEL_ORDER = {ERROR: 0, WARN: 1, MANUAL: 2, INFO: 3}

# oams_macros.cfg identifies the OpenAMS macro file by name inside the config
# directory; the shipped copy is what other files must not redefine.
MACROS_FILE = "oams_macros.cfg"
# The gco-routines overlay: it redefines [gcode_macro _TX] on purpose, to add
# `render_mode: ordered` to the shipped macro (README, "Enabling concurrent
# toolchanges"), so its sections merge with the shipped ones by design.
ORDERED_OVERLAY_FILE = "oams_macros_ordered.cfg"


class UsageError(Exception):
    """Bad arguments or an unreadable config: exit code 2."""


class Finding(object):
    def __init__(self, level, code, file, line, message):
        self.level = level
        self.code = code
        self.file = file
        self.line = line
        self.message = message

    def as_dict(self):
        return {"level": self.level, "code": self.code, "file": self.file,
                "line": self.line, "message": self.message}

    def as_text(self):
        return "%s %s %s:%d %s" % (self.level, self.code, self.file, self.line,
                                   self.message)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="config_audit.py",
        description="Static audit of a printer's Klipper config for OpenAMS.")
    parser.add_argument("--config-dir", required=True,
                        help="Klipper config directory, e.g. ~/printer_data/config")
    parser.add_argument("--printer-cfg", default="printer.cfg",
                        help="main config file inside --config-dir (default printer.cfg)")
    parser.add_argument("--commands",
                        help="JSON file with a moonraker gcode/help answer")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="print findings as JSON")
    return parser.parse_args(argv)


SECTION_RE = re.compile(r"^\[\s*([^\]]*?)\s*\]")
INLINE_COMMENT_RE = re.compile(r"(?<=[ \t])[;#].*$")
OPTION_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(.*)$")
# Klipper's SAVE_CONFIG appends its sections to printer.cfg with this prefix.
AUTOSAVE_RE = re.compile(r"^\s*#\*#\s?")


class Option(object):
    __slots__ = ("name", "value", "file", "line", "lines")

    def __init__(self, name, value, file, line):
        self.name = name
        self.value = value
        self.file = file
        self.line = line
        # (line number, text) per physical line, so a finding inside a macro
        # body can name the line that holds the command.
        self.lines = [(line, value)]


class Section(object):
    """One ``[name]`` header. Repeated headers stay separate so that every
    definition keeps its own file:line for collision reports."""

    __slots__ = ("name", "kind", "arg", "raw_arg", "file", "line", "options")

    def __init__(self, name, file, line, raw=None):
        self.name = name
        parts = name.split(None, 1)
        self.kind = parts[0] if parts else ""
        self.arg = parts[1] if len(parts) > 1 else ""
        # The name as written ("T0", not "t0"): what messages show, since a
        # group's name is also the G-code command that loads it.
        raw_parts = (raw or name).split(None, 1)
        self.raw_arg = raw_parts[1] if len(raw_parts) > 1 else ""
        self.file = file
        self.line = line
        self.options = []

    def get(self, name):
        """The last definition of `name` in this header (Klipper: later wins)."""
        found = None
        for option in self.options:
            if option.name == name:
                found = option
        return found


def parse_file(path, rel):
    """Parse one file the way Klipper reads config.

    Returns ``(sections, includes)``: every ``[name]`` header as a Section
    with its options, and every ``[include path]`` as ``(path, file, line)``.
    """
    with open(path, "r", errors="replace") as handle:
        lines = handle.read().split("\n")
    sections = []
    includes = []
    current = None
    for index, raw in enumerate(lines):
        lineno = index + 1
        autosave = AUTOSAVE_RE.match(raw)
        if autosave:
            raw = raw[autosave.end():]
        text = INLINE_COMMENT_RE.sub("", raw).rstrip()
        stripped = text.strip()
        if not stripped or stripped[0] in "#;":
            continue
        if raw[:1] in (" ", "\t"):
            if current is not None and current.options:
                current.options[-1].value += "\n" + stripped
                current.options[-1].lines.append((lineno, stripped))
            continue
        header = SECTION_RE.match(stripped)
        if header:
            raw = " ".join(header.group(1).split())
            name = raw.lower()
            if name == "include" or name.startswith("include "):
                # Klipper keeps the path's case, so keep the raw text.
                includes.append((raw[len("include"):].strip(), rel, lineno))
                current = None
                continue
            current = Section(name, rel, lineno, raw)
            sections.append(current)
            continue
        option = OPTION_RE.match(text)
        if option and current is not None:
            current.options.append(
                Option(option.group(1).lower(), option.group(2).strip(), rel, lineno))
    return sections, includes


class ConfigTree(object):
    """Every parsed section of the whole config, plus include findings."""

    def __init__(self, config_dir, main_file):
        self.config_dir = config_dir
        self.main_file = main_file
        self.sections = []
        self.includes = []
        self.findings = []
        self.read_files = set()
        self._by_name = {}

    def add(self, section):
        self.sections.append(section)
        self._by_name.setdefault(section.name, []).append(section)

    def occurrences(self, name):
        """Every ``[name]`` header, in parse order."""
        return self._by_name.get(name.lower(), [])

    def has(self, name):
        return bool(self.occurrences(name))

    def merged(self, name):
        """Options of ``[name]`` with Klipper's merge rule: sections merge and
        a later option replaces an earlier one."""
        options = {}
        for section in self.occurrences(name):
            for option in section.options:
                options[option.name] = option
        return options

    def opts(self, section):
        return self.merged(section.name)

    def by_kind(self, kind):
        return [s for s in self.sections if s.kind == kind]

    def report(self, level, code, file, line, message):
        self.findings.append(Finding(level, code, file, line, message))


def relative_to_config(tree, path):
    """Config-relative path when inside the config dir, else the full path."""
    try:
        rel = os.path.relpath(path, tree.config_dir)
    except ValueError:
        return path
    return path if rel.startswith(os.pardir) else rel


def read_config_file(tree, path, stack, site):
    """Read one file and follow its includes. `stack` holds the ancestors, so
    a repeat is a cycle; `site` is the (file, line) of the include statement."""
    rel, lineno = site
    if path in stack:
        chain = " -> ".join([os.path.basename(p) for p in stack + [path]])
        tree.report(ERROR, "E_INCLUDE_CYCLE", rel, lineno,
                    "include cycle: %s" % (chain,))
        return
    if path in tree.read_files:
        # Including one file twice must not look like a second definition.
        return
    tree.read_files.add(path)
    try:
        sections, includes = parse_file(path, relative_to_config(tree, path))
    except (IOError, OSError):
        tree.report(ERROR, "E_INCLUDE_MISSING", rel, lineno,
                    "cannot read include '%s'" % (relative_to_config(tree, path),))
        return
    for section in sections:
        tree.add(section)
    for spec, include_rel, include_line in includes:
        tree.includes.append((spec, include_rel, include_line))
        resolve_include(tree, spec, include_rel, include_line, stack + [path])


def resolve_include(tree, spec, rel, lineno, stack):
    """Klipper resolves an include next to the including file and expands
    globs in sorted order."""
    spec = spec.replace("~", os.path.expanduser("~"))
    pattern = os.path.join(tree.config_dir, os.path.dirname(rel), spec)
    matches = sorted(glob.glob(pattern))
    if matches:
        for match in matches:
            read_config_file(tree, match, stack, (rel, lineno))
    elif not glob.has_magic(pattern):
        tree.report(ERROR, "E_INCLUDE_MISSING", rel, lineno,
                    "include file '%s' does not exist" % (spec,))


def load_tree(config_dir, printer_cfg):
    config_dir = os.path.abspath(os.path.expanduser(config_dir))
    if not os.path.isdir(config_dir):
        raise UsageError("config dir '%s' does not exist" % (config_dir,))
    main_path = os.path.join(config_dir, printer_cfg)
    if not os.path.isfile(main_path):
        raise UsageError("config file '%s' does not exist" % (main_path,))
    tree = ConfigTree(config_dir, printer_cfg)
    read_config_file(tree, main_path, [], (printer_cfg, 1))
    return tree


def load_commands(path):
    """Read a moonraker `GET /printer/gcode/help` answer into a set of
    upper-case command names, or None when the file is not used."""
    if not path:
        return None
    try:
        with open(os.path.expanduser(path), "r") as handle:
            data = json.load(handle)
        return set(name.upper() for name in data["result"])
    except (IOError, OSError, ValueError, KeyError, TypeError) as exc:
        raise UsageError("cannot read commands file '%s': %s" % (path, exc))


def summarize(findings):
    counts = {"errors": 0, "warnings": 0, "manual": 0}
    for finding in findings:
        if finding.level == ERROR:
            counts["errors"] += 1
        elif finding.level == WARN:
            counts["warnings"] += 1
        elif finding.level == MANUAL:
            counts["manual"] += 1
    return counts


def sort_findings(findings):
    return sorted(findings, key=lambda f: (LEVEL_ORDER[f.level], f.file, f.line,
                                           f.code, f.message))


def print_report(findings, counts):
    for finding in findings:
        print(finding.as_text())
    print("AUDIT errors=%d warnings=%d manual=%d"
          % (counts["errors"], counts["warnings"], counts["manual"]))


PLACEHOLDER_RE = re.compile(r"<[^<>\s][^<>\n]*>")
HEX12_RE = re.compile(r"^[0-9a-fA-F]{12}$")
DEVICE_KINDS = ("auto", "ams1", "ams_ht", "ams2")
UNIT_KINDS = ("oams", "follower")


class Unit(object):
    """One ``[oams NAME]`` or ``[follower NAME]``, with its resolved lane."""

    __slots__ = ("kind", "name", "section", "idx", "fps", "device_kind", "lane")

    def __init__(self, kind, name, section, idx, fps, device_kind):
        self.kind = kind
        self.name = name
        self.section = section
        self.idx = idx
        self.fps = fps
        self.device_kind = device_kind
        self.lane = None

    @property
    def key(self):
        return "%s %s" % (self.kind, self.name)

    @property
    def bay_count(self):
        """AMS HT units and followers carry a single bay."""
        return 1 if self.kind == "follower" or self.device_kind == "ams_ht" else 4


def check_placeholders(tree):
    for section in tree.sections:
        for option in section.options:
            # A macro body is Jinja and messages mention things like FPS=<name>.
            if section.kind == "gcode_macro" and option.name == "gcode":
                continue
            # Klipper's own pin aliases name the board's power pins <GND>, <5V>.
            if section.kind == "board_pins":
                continue
            if PLACEHOLDER_RE.search(option.value):
                tree.report(ERROR, "E_PLACEHOLDER", option.file, option.line,
                            "%s still holds a template placeholder"
                            % (option.name,))


def check_mcus(tree):
    for section in tree.by_kind("mcu"):
        serial = tree.opts(section).get("serial")
        canbus = tree.opts(section).get("canbus_uuid")
        if serial is None and canbus is None:
            tree.report(ERROR, "E_MCU_TRANSPORT", section.file, section.line,
                        "[%s] has neither serial nor canbus_uuid" % (section.name,))
        elif canbus is not None and not HEX12_RE.match(canbus.value.strip()):
            tree.report(ERROR, "E_MCU_TRANSPORT", canbus.file, canbus.line,
                        "canbus_uuid '%s' is not 12 hex digits" % (canbus.value,))


def collect_units(tree):
    """Units keyed by section name; a repeated section is the same unit with
    Klipper's merge rules applied."""
    units = {}
    for section in tree.sections:
        if section.kind not in UNIT_KINDS:
            continue
        options = tree.opts(section)
        idx_name = "oams_idx" if section.kind == "oams" else "unit_idx"
        device_kind = (options["device_kind"].value.strip().lower()
                       if "device_kind" in options else "auto")
        unit = Unit(section.kind, section.arg or section.kind, section,
                    options.get(idx_name), options.get("fps"), device_kind)
        units[unit.key] = unit
    return list(units.values())


def collect_lanes(tree):
    """FPS lanes by name; the bare ``[fps]`` is the lane ``fps``."""
    lanes = {}
    for section in tree.by_kind("fps"):
        lanes.setdefault(section.arg or "fps", section)
    return lanes


def resolve_lanes(tree, units, lanes):
    """A unit's lane is its ``fps:`` option; with a single lane that lane is
    implied, and with several lanes it is required."""
    only = list(lanes)[0] if len(lanes) == 1 else None
    for unit in units:
        if unit.fps is not None:
            lane = unit.fps.value.strip().lower()
            if lane not in lanes:
                tree.report(ERROR, "E_FPS_LANE", unit.fps.file, unit.fps.line,
                            "[%s] uses fps lane '%s' but no [fps %s] is defined"
                            % (unit.key, unit.fps.value.strip(), lane,))
            else:
                unit.lane = lane
        elif len(lanes) > 1:
            tree.report(ERROR, "E_FPS_LANE", unit.section.file, unit.section.line,
                        "[%s] has no fps: but %d fps lanes are defined"
                        % (unit.key, len(lanes)))
        else:
            unit.lane = only


def check_units(tree, units):
    indexes = {}
    # A unit names an mcu by its short name, so `mcu: mcu` means [mcu].
    mcus = set()
    for section in tree.by_kind("mcu"):
        mcus.add((section.arg or section.kind).lower())
    for unit in units:
        mcu = tree.opts(unit.section).get("mcu")
        if mcu is None:
            tree.report(ERROR, "E_OAMS_MCU", unit.section.file, unit.section.line,
                        "[%s] has no mcu:" % (unit.key,))
        elif mcu.value.strip().lower() not in mcus:
            tree.report(ERROR, "E_OAMS_MCU", mcu.file, mcu.line,
                        "[%s] names mcu '%s' but [mcu %s] is not defined"
                        % (unit.key, mcu.value.strip(), mcu.value.strip()))
        if unit.device_kind not in DEVICE_KINDS:
            option = tree.opts(unit.section).get("device_kind")
            where = (option.file, option.line) if option else (
                unit.section.file, unit.section.line)
            tree.report(ERROR, "E_DEVICE_KIND", where[0], where[1],
                        "[%s] device_kind '%s' is not one of %s"
                        % (unit.key, unit.device_kind, ", ".join(DEVICE_KINDS)))
        if unit.idx is not None:
            first = indexes.get(unit.idx.value.strip())
            if first is None:
                indexes[unit.idx.value.strip()] = unit
            else:
                tree.report(ERROR, "E_UNIT_IDX_DUP", unit.idx.file, unit.idx.line,
                            "[%s] reuses index %s of [%s] at %s:%d"
                            % (unit.key, unit.idx.value.strip(), first.key,
                               first.idx.file, first.idx.line))


def check_fps(tree, lanes):
    for name, section in sorted(lanes.items()):
        options = tree.opts(section)
        has_pin = "pin" in options
        has_source = "source_oams" in options
        if has_pin == has_source:
            tree.report(ERROR, "E_FPS_SOURCE", section.file, section.line,
                        "[%s] needs exactly one of pin: and source_oams:" % (name,))


def group_entries(option):
    return [entry.strip() for entry in option.value.split(",") if entry.strip()]


def collect_groups(tree):
    """One entry per filament group name, in first-seen order. Repeated
    headers merge into one group, so the members come from the merged options
    (Klipper's rule) while E_GROUP_NAME_DUP reports the repeat."""
    groups = []
    seen = set()
    for section in tree.sections:
        if section.kind != "filament_group" or not section.arg:
            continue
        if section.arg.lower() in seen:
            continue
        seen.add(section.arg.lower())
        groups.append((section.raw_arg or section.arg, section))
    return groups


def check_groups(tree, units):
    by_name = {}
    for unit in units:
        by_name.setdefault(unit.name.lower(), unit)
    claimed = {}
    for name, section in collect_groups(tree):
        option = tree.opts(section).get("group")
        if option is None:
            continue
        seen_here = []
        group_lanes = []
        for entry in group_entries(option):
            unit_name, sep, bay_text = entry.rpartition("-")
            unit = by_name.get(unit_name.lower()) if sep else None
            if unit is None:
                if not sep:
                    reason = "is not <unit>-<bay>"
                else:
                    reason = ("names no [oams %s] or [follower %s]"
                              % (unit_name, unit_name))
                tree.report(ERROR, "E_GROUP_MEMBER", option.file, option.line,
                            "[filament_group %s] member '%s' %s" % (name, entry, reason))
                continue
            bay = int(bay_text) if bay_text.isdigit() else None
            if bay is None or not 0 <= bay < unit.bay_count:
                tree.report(ERROR, "E_GROUP_MEMBER", option.file, option.line,
                            "[filament_group %s] member '%s' is out of range for "
                            "[%s] (0-%d)" % (name, entry, unit.key, unit.bay_count - 1))
                continue
            entry_key = "%s-%d" % (unit.name.lower(), bay)
            first = claimed.get(entry_key)
            if first is not None:
                tree.report(ERROR, "E_GROUP_BAY_DUP", option.file, option.line,
                            "bay '%s' is already in [filament_group %s] at %s:%d"
                            % (entry_key, first[0], first[1], first[2]))
            else:
                claimed[entry_key] = (name, option.file, option.line)
            if entry_key in seen_here:
                tree.report(ERROR, "E_GROUP_BAY_DUP", option.file, option.line,
                            "bay '%s' is listed twice in [filament_group %s]"
                            % (entry_key, name))
            else:
                seen_here.append(entry_key)
            if unit.lane not in group_lanes:
                group_lanes.append(unit.lane)
        if len(group_lanes) > 1:
            tree.report(ERROR, "E_GROUP_LANE_MIX", option.file, option.line,
                        "[filament_group %s] mixes fps lanes %s"
                        % (name, " and ".join(str(lane) for lane in group_lanes)))


CUTTER_PREFIXES = ("variable_pre_cut_", "variable_cut_", "variable_post_cut_")
FOREIGN_SECTIONS = ("mmu", "mmu_machine", "ercf", "afc")
FOREIGN_INCLUDE_HINTS = ("afc/", "mmu/", "ercf", "klipper_openams")


def is_macros_file(path):
    return os.path.basename(path) == MACROS_FILE


def check_tag_key(tree):
    sections = tree.by_kind("bambu_lab_tag_processor")
    if not sections:
        tree.report(INFO, "I_NO_TAG_KEY", tree.main_file, 1,
                    "no [bambu_lab_tag_processor]: Bambu tags will not decode")
    for section in sections:
        option = tree.opts(section).get("key")
        if option is None:
            tree.report(ERROR, "E_TAG_KEY", section.file, section.line,
                        "[%s] has no key:" % (section.name,))
            continue
        digits = re.sub(r"[^0-9a-fA-F]", "", option.value)
        if len(digits) != 32:
            tree.report(ERROR, "E_TAG_KEY", option.file, option.line,
                        "key has %d hex digits, not 32" % (len(digits),))


def check_hardware(tree):
    if not tree.has("hdc1080"):
        for section in tree.sections:
            option = tree.opts(section).get("sensor_type")
            if option is not None and option.value.strip().lower() == "hdc1080":
                tree.report(ERROR, "E_HDC1080", option.file, option.line,
                            "sensor_type: HDC1080 needs a [hdc1080] section")
    macros = tree.occurrences("gcode_macro _tx")
    if macros and not tree.has("pause_resume"):
        tree.report(ERROR, "E_PAUSE_RESUME", macros[0].file, macros[0].line,
                    "the OpenAMS macros need a [pause_resume] section")
    for section in tree.by_kind("mfrc522"):
        tree.report(MANUAL, "M_RFID_FIRMWARE", section.file, section.line,
                    "[%s] needs mainboard firmware 2.0.25 or later" % (section.name,))


def check_calibration(tree, units, lanes):
    for unit in units:
        options = tree.opts(unit.section)
        length_name = "ptfe_length" if unit.kind == "oams" else "path_length"
        option = options.get(length_name)
        if option is not None and option.value.strip() == "0":
            tree.report(MANUAL, "M_PTFE_ZERO", option.file, option.line,
                        "[%s] %s is 0: a runout pauses instead of auto-reloading "
                        "until the length is calibrated" % (unit.key, length_name))
        reversed_option = (options.get("fps_is_reversed") if unit.kind == "oams"
                           else options.get("reversed"))
        if reversed_option is not None:
            where = (reversed_option.file, reversed_option.line)
        else:
            where = (unit.section.file, unit.section.line)
        tree.report(MANUAL, "M_FPS_ORIENTATION", where[0], where[1],
                    "[%s] fps_is_reversed must be checked on the hardware"
                    % (unit.key,))
    for name, section in sorted(lanes.items()):
        option = tree.opts(section).get("reversed")
        where = (option.file, option.line) if option is not None else (
            section.file, section.line)
        tree.report(MANUAL, "M_FPS_ORIENTATION", where[0], where[1],
                    "[fps %s] reversed must be checked on the hardware" % (name,))


def check_cutter(tree):
    for section in tree.occurrences("gcode_macro _oams_macro_variables"):
        values = ["%s=%s" % (option.name, option.value)
                  for option in tree.opts(section).values()
                  if option.name.startswith(CUTTER_PREFIXES)]
        tree.report(MANUAL, "M_CUTTER", section.file, section.line,
                    "cutter values are printer-specific and must be confirmed by "
                    "a human: %s" % (", ".join(values) if values
                                     else "none set",))


def check_foreign(tree):
    for section in tree.sections:
        if section.kind in FOREIGN_SECTIONS or section.kind.startswith("afc_"):
            tree.report(WARN, "W_FOREIGN_MMU", section.file, section.line,
                        "[%s] configures another MMU system" % (section.name,))
    for spec, rel, lineno in tree.includes:
        lowered = spec.lower()
        if any(hint in lowered for hint in FOREIGN_INCLUDE_HINTS):
            tree.report(WARN, "W_FOREIGN_MMU", rel, lineno,
                        "include '%s' pulls in another MMU system" % (spec,))


def check_tool_macros(tree):
    shipped = set()
    for section in tree.sections:
        if section.kind == "gcode_macro" and is_macros_file(section.file):
            shipped.add(section.arg.lower())
    for section in tree.sections:
        if section.kind != "gcode_macro" or is_macros_file(section.file):
            continue
        arg = section.arg.upper()
        if len(arg) > 1 and arg[0] == "T" and arg[1:].isdigit() and 0 <= int(arg[1:]) <= 19:
            if arg.lower() not in shipped:
                tree.report(WARN, "W_TOOL_MACRO", section.file, section.line,
                            "[gcode_macro %s] is defined outside %s, which does not "
                            "define %s: move it into %s or remove it"
                            % (section.arg, MACROS_FILE, section.arg, MACROS_FILE))


JINJA_STATEMENT_RE = re.compile(r"\{%.*?%\}", re.DOTALL)
JINJA_COMMENT_RE = re.compile(r"\{#.*?#\}", re.DOTALL)
COMMAND_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(?=\s|$)")
MOVE_COMMAND_RE = re.compile(r"^[GM]\d+$")

# Klipper core commands the OpenAMS macros may call. Without --commands the
# tool cannot see a running Klipper, so this list is the fallback; with
# --commands the registered list decides, and this list only covers commands
# that need no runtime check.
BUILTIN_COMMANDS = frozenset([
    "SAVE_GCODE_STATE", "RESTORE_GCODE_STATE", "SET_GCODE_VARIABLE", "RESPOND",
    "PAUSE", "RESUME", "CANCEL_PRINT", "G28", "M400", "M83", "M82", "G90", "G91",
    "G92", "M104", "M109", "TURN_OFF_HEATERS", "SET_IDLE_TIMEOUT",
    "UPDATE_DELAYED_GCODE", "ACTION_RESPOND_INFO", "SET_FILAMENT_SENSOR",
    "QUERY_FILAMENT_SENSOR",
    # Used by oams_macros.cfg and defined by Klipper:
    "SET_HEATER_TEMPERATURE", "TEMPERATURE_WAIT", "SET_STEPPER_ENABLE",
])

# gco-routines commands: _TX calls them only under its HAS_GCO_ROUTINES guard,
# so a printer without [gco_routines] never reaches them.
GCO_COMMANDS = frozenset(["START", "END", "WAIT"])


def is_builtin(command, commands=None):
    """True when the tool cannot and need not check `command`.

    Any OAMS_/OAMSM_ command comes from the OpenAMS plugin itself. Klipper core
    commands are trusted in both modes; the gco-routines trio is only reached
    when the printer has the extension, which the tool cannot see.
    """
    if command.startswith(("OAMS_", "OAMSM_")) or command in GCO_COMMANDS:
        return True
    return command in BUILTIN_COMMANDS and commands is None


def extract_command(line):
    """The command a macro body line calls, or None."""
    text = JINJA_COMMENT_RE.sub("", line)
    text = JINJA_STATEMENT_RE.sub("", text)
    text = INLINE_COMMENT_RE.sub("", text).strip()
    if not text or text[0] == "{":
        return None
    match = COMMAND_RE.match(text)
    if not match:
        return None
    word = match.group(1).upper()
    return None if MOVE_COMMAND_RE.match(word) else word


def called_commands(tree):
    """Each command the shipped macros call, with the line that calls it."""
    calls = []
    order = []
    for section in tree.sections:
        if section.kind != "gcode_macro" or not is_macros_file(section.file):
            continue
        for option in section.options:
            if option.name != "gcode":
                continue
            for lineno, text in option.lines:
                command = extract_command(text)
                if command is None:
                    continue
                if command not in order:
                    order.append(command)
                    calls.append((command, option.file, lineno))
    return calls


def macro_names(tree):
    names = set()
    for section in tree.sections:
        if section.kind == "gcode_macro" and section.arg:
            names.add(section.arg.lower())
    return names


# The override or hook point that replaces a shipped macro the owner edited.
# A cutter belongs in OAMS_CUT, an unload step in one of the hook points that
# unload passes, and anything else has to move into a hook: the hooks are the
# only place a printer's own G-code can join a toolchange or an unload, and an
# update overwrites the shipped files either way.
MACRO_REPLACEMENTS = {
    "cut_filament": "OAMS_CUT",
    "safe_unload_filament": "_OAMS_HOOK_BEFORE_HEAT or _OAMS_HOOK_AFTER_CUT",
    "openams_unload": "_OAMS_HOOK_BEFORE_HEAT or _OAMS_HOOK_AFTER_CUT",
}
DEFAULT_REPLACEMENT = "a _OAMS_HOOK_* point"


def macro_body(section):
    """A macro's gcode body, one command per line, with the whitespace and the
    comments normalized away: a copy that only differs in formatting has the
    same body, so it is a stale copy rather than a customization."""
    option = section.get("gcode")
    if option is None:
        return ""
    lines = []
    for _, text in option.lines:
        text = JINJA_COMMENT_RE.sub("", text)
        text = INLINE_COMMENT_RE.sub("", text)
        text = " ".join(text.split())
        if text:
            lines.append(text)
    return "\n".join(lines)


def check_macro_collisions(tree):
    """A macro OpenAMS ships that another file defines is either a stale copy
    (the same body: comment it out) or a customization (an edited body: move
    the added lines into the override or hook point that replaces it)."""
    seen = {}
    for section in tree.sections:
        if section.kind != "gcode_macro" or not section.arg:
            continue
        seen.setdefault(section.arg.lower(), []).append(section)
    for name, sections in sorted(seen.items()):
        shipped = [s for s in sections if is_macros_file(s.file)]
        others = [s for s in sections if not is_macros_file(s.file)
                  and os.path.basename(s.file) != ORDERED_OVERLAY_FILE]
        for other in others:
            arg = other.raw_arg or other.arg
            for macro in shipped:
                if macro_body(other) == macro_body(macro):
                    tree.report(ERROR, "E_MACRO_COLLISION", macro.file, macro.line,
                                "[gcode_macro %s] is also defined at %s:%d with the "
                                "same body; comment out that copy"
                                % (arg, other.file, other.line))
                else:
                    tree.report(ERROR, "E_MACRO_CUSTOMIZED", macro.file, macro.line,
                                "[gcode_macro %s] is also defined at %s:%d with a "
                                "different body: move the added lines into %s in "
                                "your own file and comment out that copy"
                                % (arg, other.file, other.line,
                                   MACRO_REPLACEMENTS.get(name, DEFAULT_REPLACEMENT)))


def check_macro_calls(tree, commands):
    defined = macro_names(tree)
    for command, file, lineno in called_commands(tree):
        if command.lower() in defined or is_builtin(command, commands):
            continue
        if commands is None:
            tree.report(MANUAL, "M_MACRO_UNVERIFIED", file, lineno,
                        "%s is called by %s but not defined in the config; "
                        "verify at runtime" % (command, MACROS_FILE))
        elif command not in commands:
            tree.report(ERROR, "E_MACRO_MISSING", file, lineno,
                        "%s is called by %s but the running Klipper neither "
                        "registers nor defines it" % (command, MACROS_FILE))


def check_summary(tree, units, lanes):
    def info(message):
        tree.report(INFO, "I_SUMMARY", tree.main_file, 1, message)

    units_text = ", ".join(
        "%s idx=%s kind=%s lane=%s"
        % (unit.name, unit.idx.value.strip() if unit.idx else "unset",
           unit.device_kind, unit.lane if unit.lane else "unset")
        for unit in units)
    info("units: %s" % (units_text if units_text else "none",))
    groups_text = []
    for name, section in collect_groups(tree):
        option = tree.opts(section).get("group")
        members = ", ".join(group_entries(option)) if option else ""
        groups_text.append("%s=[%s]" % (section.raw_arg or name, members))
    info("groups: %s" % (", ".join(groups_text) if groups_text else "none",))
    info("lanes: %s" % (", ".join(sorted(lanes)) if lanes else "none",))


def check_group_names(tree):
    """Klipper refuses a config with two sections of the same filament_group."""
    seen = {}
    for section in tree.sections:
        if section.kind != "filament_group" or not section.arg:
            continue
        first = seen.get(section.arg.lower())
        if first is not None:
            tree.report(ERROR, "E_GROUP_NAME_DUP", section.file, section.line,
                        "[filament_group %s] is already defined at %s:%d"
                        % (section.raw_arg or section.arg, first.file, first.line))
        else:
            seen[section.arg.lower()] = section


def audit(tree, commands):
    """Run every check over the parsed tree."""
    lanes = collect_lanes(tree)
    units = collect_units(tree)
    resolve_lanes(tree, units, lanes)
    check_placeholders(tree)
    check_mcus(tree)
    check_fps(tree, lanes)
    check_units(tree, units)
    check_groups(tree, units)
    check_group_names(tree)
    check_tag_key(tree)
    check_hardware(tree)
    check_calibration(tree, units, lanes)
    check_cutter(tree)
    check_foreign(tree)
    check_tool_macros(tree)
    check_macro_collisions(tree)
    check_macro_calls(tree, commands)
    check_summary(tree, units, lanes)
    return sort_findings(tree.findings)


def main(argv=None):
    args = parse_args(argv)
    try:
        commands = load_commands(args.commands)
        tree = load_tree(args.config_dir, args.printer_cfg)
        findings = audit(tree, commands)
    except UsageError as exc:
        sys.stderr.write("config_audit.py: %s\n" % (exc,))
        return EXIT_USAGE
    counts = summarize(findings)
    if args.as_json:
        print(json.dumps({"findings": [f.as_dict() for f in findings],
                          "summary": counts}, indent=2, sort_keys=True))
    else:
        print_report(findings, counts)
    return EXIT_ERRORS if counts["errors"] else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())