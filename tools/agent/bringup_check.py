#!/usr/bin/env python3
"""Bring-up ladder for an OpenAMS printer: what works, in order, from the bottom up.

Read-only. The only command it ever sends is OAMSM_SELFTEST, and only with
--selftest. Every level prints `L<n> <NAME> <PASS|FAIL|WARN|SKIP> <detail>` plus
indented detail lines, then a final BRINGUP tally; lower-tier agents read that
output literally, so the format is a contract.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 10.0
DEFAULT_URL = "http://127.0.0.1:7125"
SELFTEST_MARKER = "// OpenAMS self-test:"
IDLE_STATES = ("standby", "complete", "cancelled", "error")
BUSY_STATES = ("printing", "paused")
REQUIRED_MACROS = (
    "OAMSM_LOAD_FILAMENT",
    "OAMSM_UNLOAD_FILAMENT",
    "OAMSM_SELFTEST",
    "SAFE_UNLOAD_FILAMENT",
    "CUT_FILAMENT",
    "PAUSE",
    "RESUME",
)
ADVISED_MACROS = ("CLEAN_NOZZLE",)
TEMP_MIN_C = -20.0
TEMP_MAX_C = 80.0
UNLOADED_FPS_LIMIT = 0.8

# Endpoint path -> recorded answer file, used by --fixture-dir.
FIXTURES = {
    "/server/info": "server_info.json",
    "/printer/info": "printer_info.json",
    "/printer/objects/list": "objects_list.json",
    "/printer/objects/query": "objects_query.json",
    "/printer/gcode/help": "gcode_help.json",
    "/server/gcode_store": "gcode_store.json",
    "/server/openams_spoolman/status": "openams_spoolman_status.json",
    "/machine/update/status": "update_status.json",
    "/printer/gcode/script": "selftest_reply.json",
}

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"


class HttpTransport(object):
    """Talks to a real Moonraker. A timeout or refusal is an answer, not a crash."""

    def __init__(self, url, api_key=None):
        self.url = url.rstrip("/")
        self.api_key = api_key

    def get(self, path, params=None):
        if params:
            # Moonraker's query string names each object as a key and lists its
            # fields as the value: ?oams_manager=ready&mcu%20fps=mcu_version
            path = path + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        return self._call(urllib.request.Request(self.url + path))

    def post(self, path, payload):
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.url + path, data=body, headers={"Content-Type": "application/json"}
        )
        return self._call(request)

    def _call(self, request):
        if self.api_key:
            request.add_header("X-Api-Key", self.api_key)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
            return None


class FixtureTransport(object):
    """Serves recorded answers by path, filtering the merged query file as Moonraker would."""

    def __init__(self, directory):
        self.dir = directory

    def get(self, path, params=None):
        payload = self._read(path)
        if payload is None or path != "/printer/objects/query":
            return payload
        return self._filter_query(payload, params or [])

    def post(self, path, payload):
        return self._read(path)

    def _read(self, path):
        name = FIXTURES.get(path)
        if name is None:
            return None
        target = os.path.join(self.dir, name)
        if not os.path.isfile(target):
            return None
        try:
            with open(target, "r") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _filter_query(payload, params):
        # Whole objects only: the recorded file is a merged dump, so trimming each
        # object to one level's `fields` would hide fields another level asks for.
        status = (payload or {}).get("result", {}).get("status", {})
        wanted = [name for name, _fields in params]
        out = {n: status[n] for n in wanted if n in status}
        return {"result": {"status": out}}


class Level(object):
    """One rung of the ladder and everything the rung found."""

    def __init__(self, level, name, result, detail, lines=None):
        self.level = level
        self.name = name
        self.result = result
        self.detail = detail
        self.lines = list(lines or ())

    @property
    def text(self):
        return "L%d %s %s %s" % (self.level, self.name, self.result, self.detail)

    def as_dict(self):
        return {
            "level": self.level,
            "name": self.name,
            "result": self.result,
            "detail": self.detail,
            "lines": self.lines,
        }


class Printer(object):
    """The Moonraker endpoints the ladder reads, with the query cache in one place."""

    def __init__(self, transport):
        self.transport = transport
        self._status = {}
        self._asked = {}
        self._objects = None

    def result(self, payload):
        if not isinstance(payload, dict):
            return None
        value = payload.get("result")
        return value if value is not None else None

    def server_info(self):
        return self.result(self.transport.get("/server/info"))

    def printer_info(self):
        return self.result(self.transport.get("/printer/info"))

    def objects(self):
        """Every Klipper object name, used to discover MCUs, units and sensors."""
        if self._objects is None:
            value = self.result(self.transport.get("/printer/objects/list"))
            self._objects = list(value.get("objects") or ()) if isinstance(value, dict) else []
        return self._objects

    def status(self, wanted, fields):
        """Query the objects and fields asked for; an object is re-asked for a new field.

        Klipper answers with only the requested fields, so a level that needs a
        field an earlier level did not ask for has to ask again.
        """
        missing = [n for n in wanted if not set(fields).issubset(self._asked.get(n, ()))]
        if missing:
            ask = set(fields)
            for name in missing:
                ask.update(self._asked.get(name, ()))
            params = [(name, ",".join(sorted(ask))) for name in missing]
            value = self.result(self.transport.get("/printer/objects/query", params))
            if isinstance(value, dict) and isinstance(value.get("status"), dict):
                value = value["status"]
            for name in missing:
                self._asked[name] = ask
                self._status[name] = (value or {}).get(name) if isinstance(value, dict) else None
        return dict((name, self._status[name]) for name in wanted)

    def gcode_help(self):
        value = self.result(self.transport.get("/printer/gcode/help"))
        if not isinstance(value, dict):
            return None
        # Moonraker nests the table under "help"; the recorded file is the table itself.
        help_table = value.get("help")
        return help_table if isinstance(help_table, dict) else value

    def gcode_store(self):
        value = self.result(self.transport.get("/server/gcode_store"))
        if not isinstance(value, dict):
            return []
        return list(value.get("gcode_store") or ())

    def run_script(self, script):
        return self.transport.post("/printer/gcode/script", {"script": script})

    def spoolman_status(self):
        return self.result(self.transport.get("/server/openams_spoolman/status"))

    def update_status(self):
        return self.result(self.transport.get("/machine/update/status"))


def named(objects, prefix):
    """Objects called `prefix` or `prefix <name>`, as Klipper names units and MCUs."""
    return [n for n in objects if n == prefix or n.startswith(prefix + " ")]


def matching(objects, prefix):
    """Objects whose name starts with `prefix `, e.g. every `aht3x *` sensor."""
    return [n for n in objects if n.startswith(prefix + " ")]


def level0(printer):
    info = printer.server_info()
    if not isinstance(info, dict):
        return Level(0, "MOONRAKER", FAIL, "/server/info did not answer")
    lines = ["version %s" % (info.get("moonraker_version") or "unknown")]
    components = info.get("components") or ()
    if "openams_spoolman" in components:
        return Level(0, "MOONRAKER", PASS, "serving with openams_spoolman", lines)
    lines.append("openams_spoolman is not in components (the OpenAMS UI endpoint will be missing)")
    return Level(0, "MOONRAKER", WARN, "serving without openams_spoolman", lines)


def level1(printer):
    info = printer.printer_info()
    if not isinstance(info, dict):
        return Level(1, "KLIPPY", FAIL, "/printer/info did not answer")
    state = info.get("state")
    message = info.get("state_message") or ""
    lines = [line for line in str(message).splitlines()[:3] if line.strip()]
    if state == "ready":
        return Level(1, "KLIPPY", PASS, "state=ready", lines)
    return Level(1, "KLIPPY", FAIL, "state=%s" % (state or "unknown"), lines)


def level2(printer):
    stats = printer.status(["print_stats"], ["state"]).get("print_stats")
    if not isinstance(stats, dict) or stats.get("state") is None:
        return Level(2, "IDLE", SKIP, "print_stats did not answer")
    state = str(stats.get("state"))
    if state in IDLE_STATES:
        return Level(2, "IDLE", PASS, "print_stats.state=%s" % state)
    if state in BUSY_STATES:
        return Level(2, "IDLE", FAIL, "printer busy: stop and ask the human")
    return Level(2, "IDLE", WARN, "print_stats.state=%s" % state)


def level3(printer):
    names = named(printer.objects(), "mcu")
    if not names:
        return Level(3, "MCUS", FAIL, "no mcu objects in printer.objects.list")
    statuses = printer.status(names, ["mcu_version", "last_stats"])
    busses = printer.status(
        matching(printer.objects(), "canbus_stats"), ["bus_state", "rx_error", "tx_error"]
    )
    lines, no_version, warnings = [], [], []
    for name in names:
        mcu = statuses.get(name)
        if not isinstance(mcu, dict):
            warnings.append("%s: listed by Klipper but absent from the status snapshot" % short(name))
            continue
        version = mcu.get("mcu_version")
        if not version:
            no_version.append(short(name))
            lines.append("%s: no mcu_version" % short(name))
            continue
        if not mcu.get("last_stats"):
            warnings.append("%s: last_stats is empty" % short(name))
        entry = "%s: %s" % (short(name), version)
        bus = busses.get("canbus_stats %s" % short(name))
        if isinstance(bus, dict):
            entry += " bus_state=%s rx_error=%s tx_error=%s" % (
                bus.get("bus_state"),
                bus.get("rx_error"),
                bus.get("tx_error"),
            )
            warnings.extend(can_warnings(short(name), bus))
        lines.append(entry)
    if no_version:
        return Level(3, "MCUS", FAIL, "no firmware version: %s" % ", ".join(no_version), lines)
    if warnings:
        return Level(
            3, "MCUS", WARN, "%d mcu(s), %d warning(s)" % (len(names), len(warnings)),
            lines + warnings,
        )
    return Level(3, "MCUS", PASS, "%d mcu(s) reporting" % len(names), lines)


def short(name):
    return name.split(" ", 1)[1] if " " in name else name


def can_warnings(name, bus):
    """A CAN bus is healthy when it is active and has burned no error counters."""
    state = bus.get("bus_state")
    if state is not None and str(state) not in ("0", "active"):
        return ["%s: can bus_state=%s is not active" % (name, state)]
    out = []
    for field in ("rx_error", "tx_error"):
        value = bus.get(field)
        if isinstance(value, (int, float)) and value > 0:
            out.append("%s: can %s=%s" % (name, field, value))
    return out


def level4(printer):
    objects = printer.objects()
    units = named(objects, "oams")
    uis = named(objects, "openams_ui")
    statuses = printer.status(
        ["oams_manager"]
        + units
        + uis,
        [
            "ready",
            "groups",
            "lanes",
            "units",
            "connected",
            "family",
            "bay_count",
            "online",
            "serial",
            "lane",
        ],
    )
    manager = statuses.get("oams_manager")
    if not isinstance(manager, dict):
        return Level(4, "OPENAMS", FAIL, "oams_manager is absent")
    if manager.get("ready") is not True:
        return Level(4, "OPENAMS", FAIL, "oams_manager ready=%s" % (manager.get("ready"),))
    lanes = dict(
        (unit.get("name"), unit.get("lane"))
        for unit in (manager.get("units") or ())
        if isinstance(unit, dict)
    )
    lines, broken = [], []
    for name in sorted(set([short(n) for n in units] + [short(n) for n in uis])):
        device = statuses.get("oams %s" % name)
        ui = statuses.get("openams_ui %s" % name)
        line = [name]
        if isinstance(device, dict):
            line.append("family=%s" % (device.get("family") or "?"))
            line.append("bays=%s" % (device.get("bay_count") or "?"))
            if device.get("connected") is not True:
                broken.append("%s is not connected" % name)
                line.append("NOT CONNECTED")
        if isinstance(ui, dict):
            line.append("serial=%s" % (ui.get("serial") or "?"))
            line.append("lane=%s" % lane_of(ui, lanes.get(name)))
            if ui.get("online") is not True:
                broken.append("%s UI is offline" % name)
                line.append("UI OFFLINE")
            if ui.get("ready") is not True:
                broken.append("%s UI is not ready" % name)
                line.append("UI NOT READY")
        lines.append(" ".join(line))
    if broken:
        return Level(4, "OPENAMS", FAIL, "; ".join(broken), lines)
    if not lines:
        return Level(4, "OPENAMS", FAIL, "oams_manager is ready but no oams unit objects exist")
    return Level(4, "OPENAMS", PASS, "%d unit(s) connected and ready" % len(lines), lines)


def lane_of(ui, manager_lane):
    lane = ui.get("lane")
    if isinstance(lane, dict) and lane.get("id"):
        return lane.get("id")
    return manager_lane or "?"


def level5(printer):
    objects = printer.objects()
    fps_objects = named(objects, "fps")
    units = named(objects, "oams")
    temps = (
        [n for n in objects if n.startswith("temperature_sensor oams")]
        + matching(objects, "aht3x")
        + matching(objects, "hdc1080")
    )
    rfids = matching(objects, "mfrc522")
    fields = ["fps_value", "f1s_hes_value", "hub_hes_value", "temperature", "readers", "lanes"]
    statuses = printer.status(["oams_manager"] + fps_objects + units + temps + rfids, fields)
    manager = statuses.get("oams_manager")
    unloaded = set()
    if isinstance(manager, dict):
        unloaded = set(
            str(lane.get("id"))
            for lane in (manager.get("lanes") or ())
            if isinstance(lane, dict) and str(lane.get("state") or "").lower() == "unloaded"
        )
    lines, bad_fps, warnings = [], [], []
    for name in fps_objects:
        sensor = statuses.get(name) or {}
        value = sensor.get("fps_value")
        entry = "%s: fps_value=%s" % (short(name), value)
        if not is_number(value) or not 0.0 <= value <= 1.0:
            bad_fps.append("%s=%s" % (short(name), value))
            entry += " OUT OF RANGE"
        elif short(name) in unloaded and value > UNLOADED_FPS_LIMIT:
            warnings.append(
                "%s: lane is unloaded but fps_value=%s, check fps_is_reversed (reversed)"
                % (short(name), value)
            )
            entry += " reversed?"
        lines.append(entry)
    for name in units:
        device = statuses.get(name) or {}
        entry = [short(name)]
        for field in ("f1s_hes_value", "hub_hes_value"):
            value = device.get(field)
            if value is None:
                warnings.append("%s: %s is not reported" % (short(name), field))
                entry.append("%s=none" % field)
            else:
                entry.append("%s=%s" % (field, value))
        lines.append(" ".join(entry))
    for name in temps:
        sensor = statuses.get(name) or {}
        value = sensor.get("temperature")
        entry = "%s: %s C" % (name, value)
        if not is_number(value):
            warnings.append("%s: no temperature reported" % name)
            entry = "%s: no temperature reported" % name
        elif not TEMP_MIN_C <= value <= TEMP_MAX_C:
            warnings.append("%s: %s C is outside %.0f..%.0f C" % (name, value, TEMP_MIN_C, TEMP_MAX_C))
            entry += " OUT OF RANGE"
        lines.append(entry)
    for name in rfids:
        sensor = statuses.get(name) or {}
        for index, reader in enumerate(sensor.get("readers") or ()):
            if not isinstance(reader, dict):
                continue
            label = "%s reader %s" % (name, reader.get("rfid_card", index))
            # The reader chip answers with its version; a reader that never did
            # is missing or dead (wiring, firmware older than 2.0.25). No tag in
            # front of a working reader is normal and not a warning.
            if reader.get("version") is None:
                warnings.append(
                    "%s: the reader chip does not answer (%s)"
                    % (label, reader.get("error") or reader.get("last_read_status") or "no version")
                )
            elif reader.get("present") is True:
                lines.append("%s: tag %s" % (label, reader.get("uid") or "present"))
            else:
                lines.append("%s: reader ok, no tag" % label)
    if bad_fps:
        return Level(5, "SENSORS", FAIL, "fps_value outside 0..1: %s" % ", ".join(bad_fps), lines)
    if warnings:
        return Level(
            5, "SENSORS", WARN, "%d warning(s)" % len(warnings), lines + warnings
        )
    return Level(5, "SENSORS", PASS, "%d fps, %d unit(s) reading" % (len(fps_objects), len(units)), lines)


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def level6(printer):
    help_table = printer.gcode_help()
    if not isinstance(help_table, dict):
        return Level(6, "MACROS", FAIL, "/printer/gcode/help did not answer")
    known = set(name.upper() for name in help_table)
    manager = printer.status(["oams_manager"], ["groups"])["oams_manager"]
    groups = []
    if isinstance(manager, dict):
        groups = [g.get("name") for g in (manager.get("groups") or ()) if isinstance(g, dict)]
    missing = [str(name) for name in groups if str(name).upper() not in known]
    missing += [name for name in REQUIRED_MACROS if name.upper() not in known]
    lines = ["%d commands registered" % len(known)]
    if missing:
        return Level(6, "MACROS", FAIL, "no macro for: %s" % ", ".join(missing), lines)
    advised = [name for name in ADVISED_MACROS if name.upper() not in known]
    if advised:
        lines.append("missing %s: the toolchange calls it" % ", ".join(advised))
        return Level(6, "MACROS", WARN, "%d group(s) and all required macros present" % len(groups), lines)
    return Level(6, "MACROS", PASS, "%d group(s) and all required macros present" % len(groups), lines)


def level7(printer, selftest):
    if not selftest:
        return Level(7, "SELFTEST", SKIP, "run with --selftest")
    reply = printer.run_script("OAMSM_SELFTEST")
    if reply is None:
        return Level(7, "SELFTEST", FAIL, "OAMSM_SELFTEST did not answer")
    if isinstance(reply, dict) and reply.get("error"):
        return Level(7, "SELFTEST", FAIL, "OAMSM_SELFTEST rejected: %s" % reply.get("error"))
    report = selftest_report(printer.gcode_store())
    if report is None:
        return Level(7, "SELFTEST", FAIL, "no '%s' response in gcode_store" % SELFTEST_MARKER)
    lines = [line for line in report.splitlines() if line.strip()]
    if "RESULT: PASS" in report:
        return Level(7, "SELFTEST", PASS, "RESULT: PASS", lines)
    return Level(7, "SELFTEST", FAIL, "self-test did not report RESULT: PASS", lines)


def selftest_report(store):
    """The newest self-test response among the last 100 store entries."""
    found = None
    for entry in store[-100:]:
        if not isinstance(entry, dict) or entry.get("type") != "response":
            continue
        message = entry.get("message") or ""
        if message.startswith(SELFTEST_MARKER):
            found = message
    return found


def level8(printer):
    status = printer.spoolman_status()
    if not isinstance(status, dict):
        return Level(8, "SPOOLMAN", WARN, "/server/openams_spoolman/status did not answer")
    if status.get("spoolman_online") is True:
        bays = status.get("bays") or {}
        linked = len([b for b in bays.values() if isinstance(b, dict) and b.get("spool_id")])
        return Level(8, "SPOOLMAN", PASS, "online, %d/%d bay(s) linked" % (linked, len(bays)))
    return Level(8, "SPOOLMAN", WARN, "spoolman_online=%s (OpenAMS works without it)" % (status.get("spoolman_online"),))


def level9(printer):
    status = printer.update_status()
    if not isinstance(status, dict):
        return Level(9, "UPDATES", WARN, "/machine/update/status did not answer")
    info = (status.get("version_info") or {}).get("openams")
    if not isinstance(info, dict):
        return Level(9, "UPDATES", WARN, "openams is not in version_info")
    version = info.get("version") or "unknown"
    flags = [f for f in ("is_dirty", "corrupt", "detached") if info.get(f) is True]
    # Moonraker could not validate the checkout (no remote, or a remote that
    # is not OpenAMS): the update button will not work for it.
    if info.get("is_valid") is False:
        flags.append("is_valid=false (remote %s/%s)" % (info.get("owner") or "?", info.get("repo_name") or "?"))
    if flags:
        return Level(9, "UPDATES", WARN, "openams %s: %s" % (version, ", ".join(flags)), ["version %s" % version])
    return Level(9, "UPDATES", PASS, "openams %s" % version, ["version %s" % version])


# Which levels a FAIL stops, so an agent is told what it may not conclude yet.
BLOCKS = {
    0: (1, 2, 3, 4, 5, 6, 7, 8, 9),
    1: (3, 4, 5, 6, 7),
    4: (5, 6, 7),
}


def run_ladder(printer, selftest=False):
    checks = [
        level0,
        level1,
        level2,
        level3,
        level4,
        level5,
        level6,
        lambda printer: level7(printer, selftest),
        level8,
        level9,
    ]
    names = [
        "MOONRAKER",
        "KLIPPY",
        "IDLE",
        "MCUS",
        "OPENAMS",
        "SENSORS",
        "MACROS",
        "SELFTEST",
        "SPOOLMAN",
        "UPDATES",
    ]
    levels, blocked_by = [], {}
    for number, check in enumerate(checks):
        blocker = blocked_by.get(number)
        if blocker is not None:
            levels.append(Level(number, names[number], SKIP, "blocked by L%d" % blocker))
            continue
        level = check(printer)
        levels.append(level)
        if level.result == FAIL:
            for later in BLOCKS.get(number, ()):
                blocked_by.setdefault(later, number)
    return levels


def summarize(levels):
    tally = {PASS: 0, FAIL: 0, WARN: 0, SKIP: 0}
    for level in levels:
        tally[level.result] = tally.get(level.result, 0) + 1
    return tally


def render(levels, tally):
    out = []
    for level in levels:
        out.append(level.text)
        for line in level.lines:
            out.append("  " + line)
    out.append(
        "BRINGUP pass=%d fail=%d warn=%d skip=%d"
        % (tally[PASS], tally[FAIL], tally[WARN], tally[SKIP])
    )
    return "\n".join(out)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="bringup_check.py",
        description="Report which bring-up layer of an OpenAMS printer works, in order.",
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="Moonraker base URL")
    parser.add_argument("--api-key", default=None, help="sent as X-Api-Key")
    parser.add_argument(
        "--selftest", action="store_true", help="send the read-only OAMSM_SELFTEST command"
    )
    parser.add_argument("--fixture-dir", default=None, help="read recorded answers instead of HTTP")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    if args.fixture_dir is not None and not os.path.isdir(args.fixture_dir):
        parser.error("--fixture-dir %s is not a directory" % args.fixture_dir)
    return args


def main(argv=None):
    args = parse_args(argv)
    transport = (
        FixtureTransport(args.fixture_dir)
        if args.fixture_dir is not None
        else HttpTransport(args.url, args.api_key)
    )
    levels = run_ladder(Printer(transport), args.selftest)
    tally = summarize(levels)
    if args.json:
        print(json.dumps({"levels": [l.as_dict() for l in levels], "summary": tally}, indent=2))
    else:
        print(render(levels, tally))
    return 1 if tally[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
