"""Fixture loading and validation for the mock Moonraker.

The format is a recorded-event log: one JSON object with `scenario`, `seed`, `family_map` and a flat, time-ordered
`events` list. Every event is `{"t", "kind", "unit", "payload"}` with `kind`
one of `openams_ui`, `component_status`, `c2_status`, `action_log`,
`spoolman_call`, `gcode_script`.

One extra top-level key, `spoolman_responses`, is optional and not part of the
recorded format: it answers Moonraker's `server.spoolman.proxy` requests, keyed
`"METHOD path"`, with the JSON the proxy would return. Another, `file_metadata`,
maps a G-code file name to what Moonraker's `server.files.metadata` returns for
it (e.g. `filament_weights`, one number per tool), for a UI's filament check.

`load()`/`loads()` raise `FixtureError` with the offending path in the message,
so a hand-written fixture fails loudly instead of halfway through a replay.
"""

import json
import os

# The event kinds the mock understands. `action_log`, `spoolman_call` and
# `gcode_script` are recorded outputs of a run (what the client did); they are
# carried in the fixture so a consumer sees the full log, but they change no
# server state.
KINDS = (
    "openams_ui",
    "component_status",
    "c2_status",
    "action_log",
    "spoolman_call",
    "gcode_script",
)

# Payload type per kind: the three status kinds and a proxy call are JSON
# objects, the two log kinds are plain strings.
_PAYLOAD_TYPES = {
    "openams_ui": dict,
    "component_status": dict,
    "c2_status": dict,
    "spoolman_call": dict,
    "action_log": str,
    "gcode_script": str,
}

_TOP_LEVEL = ("scenario", "seed", "family_map", "events")
# `schema_version`: the recorded c2_status revision.
_OPTIONAL_TOP_LEVEL = ("spoolman_responses", "file_metadata", "schema_version")


class FixtureError(ValueError):
    """A fixture file that does not match the format."""


class Event(object):
    """One entry of the fixture's flat event log."""

    __slots__ = ("t", "kind", "unit", "payload")

    def __init__(self, t, kind, unit, payload):
        self.t = t
        self.kind = kind
        self.unit = unit
        self.payload = payload

    def __repr__(self):  # pragma: no cover - debugging aid
        return "Event(t=%r, kind=%r, unit=%r)" % (self.t, self.kind, self.unit)


class Fixture(object):
    """A loaded, validated fixture."""

    def __init__(self, scenario, seed, family_map, events, spoolman_responses,
                 file_metadata=None):
        self.scenario = scenario
        self.seed = seed
        self.family_map = family_map
        self.events = events
        self.spoolman_responses = spoolman_responses
        self.file_metadata = file_metadata or {}

    @property
    def units(self):
        """The units named by `openams_ui` events, in first-seen order."""
        seen = []
        for event in self.events:
            if event.kind == "openams_ui" and event.unit not in seen:
                seen.append(event.unit)
        return seen

    def events_of_kind(self, kind):
        return [event for event in self.events if event.kind == kind]


def _fail(source, path, message):
    raise FixtureError("%s: %s: %s" % (source, path, message))


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_event(source, index, event):
    where = "events[%d]" % index
    if not isinstance(event, dict):
        _fail(source, where, "expected a JSON object, got %s" % type(event).__name__)
    for key in ("t", "kind", "unit", "payload"):
        if key not in event:
            _fail(source, where, "missing key %r" % key)
    t = event["t"]
    if not _is_number(t):
        _fail(source, where + ".t", "expected a number of seconds, got %r" % (t,))
    kind = event["kind"]
    if kind not in KINDS:
        _fail(source, where + ".kind", "unknown kind %r (expected one of %s)"
              % (kind, ", ".join(KINDS)))
    unit = event["unit"]
    if unit is not None and not isinstance(unit, str):
        _fail(source, where + ".unit", "expected a unit name or null, got %r" % (unit,))
    if kind == "openams_ui" and not unit:
        _fail(source, where + ".unit", "an openams_ui event must name its unit")
    expected = _PAYLOAD_TYPES[kind]
    if not isinstance(event["payload"], expected):
        _fail(source, where + ".payload", "kind %r takes a %s, got %s"
              % (kind, expected.__name__, type(event["payload"]).__name__))
    return Event(float(t), kind, unit, event["payload"])


def loads(text, source="<fixture>"):
    """Parse and validate fixture JSON from a string."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        _fail(source, "<json>", "not valid JSON (%s)" % exc)
    return validate(data, source=source)


def load(path):
    """Read, parse and validate a fixture file."""
    with open(path, "r") as handle:
        text = handle.read()
    source = os.path.basename(path)
    return loads(text, source=source)


def validate(data, source="<fixture>"):
    """Validate a parsed fixture object and return a `Fixture`."""
    if not isinstance(data, dict):
        _fail(source, "<root>", "expected a JSON object, got %s" % type(data).__name__)

    for key in _TOP_LEVEL:
        if key not in data:
            _fail(source, "<root>", "missing key %r" % key)
    for key in data:
        if key not in _TOP_LEVEL and key not in _OPTIONAL_TOP_LEVEL:
            _fail(source, key, "unknown top-level key (allowed: %s)"
                  % ", ".join(_TOP_LEVEL + _OPTIONAL_TOP_LEVEL))

    scenario = data["scenario"]
    if not isinstance(scenario, str) or not scenario:
        _fail(source, "scenario", "expected a non-empty string, got %r" % (scenario,))

    seed = data["seed"]
    if not isinstance(seed, int) or isinstance(seed, bool):
        _fail(source, "seed", "expected an integer, got %r" % (seed,))

    family_map = data["family_map"]
    if not isinstance(family_map, dict):
        _fail(source, "family_map", "expected an object, got %s" % type(family_map).__name__)
    for unit, family in family_map.items():
        if not isinstance(unit, str) or not unit:
            _fail(source, "family_map", "unit names must be non-empty strings, got %r" % (unit,))
        if not isinstance(family, str) or not family:
            _fail(source, "family_map." + unit, "expected a family name, got %r" % (family,))

    events = data["events"]
    if not isinstance(events, list):
        _fail(source, "events", "expected an array, got %s" % type(events).__name__)
    parsed = []
    previous_t = None
    for index, event in enumerate(events):
        parsed.append(_validate_event(source, index, event))
        if previous_t is not None and event["t"] < previous_t:
            _fail(source, "events[%d].t" % index,
                  "the event log must be time-ordered (%r follows %r)"
                  % (event["t"], previous_t))
        previous_t = event["t"]

    responses = data.get("spoolman_responses", {})
    if not isinstance(responses, dict):
        _fail(source, "spoolman_responses",
              "expected an object, got %s" % type(responses).__name__)
    for key in responses:
        if not isinstance(key, str) or not key:
            _fail(source, "spoolman_responses", "keys must be \"METHOD path\" strings")

    metadata = data.get("file_metadata", {})
    if not isinstance(metadata, dict) or not all(
            isinstance(key, str) and key and isinstance(value, dict)
            for key, value in metadata.items()):
        _fail(source, "file_metadata", "expected an object of file name -> object")

    return Fixture(scenario, seed, family_map, parsed, responses, metadata)
