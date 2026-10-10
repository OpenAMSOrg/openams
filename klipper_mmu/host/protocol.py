# SPDX-License-Identifier: GPL-3.0-or-later
"""Typed responses from Klipper's existing message parser; no new wire framing.

This layer never completes an OpenAMS waiter or acknowledges an event. A device
adapter must validate controller/connection identity and process a retained event
before acknowledging it. Admission and status objects are not terminal events.
"""
from dataclasses import dataclass

from .wire import (DIAG_REPLIES, EVENT_TYPES, SENSOR_REPLIES, STATUS_REPLIES,
                   Binding, Command, CommandCode, Direction, EventFlag,
                   EventType, FollowerState, OpCode, OpType, payload_fields,
                   known)


def _integer(params, name, bits=32):
    value = params.get(name)
    if type(value) is not int or not 0 <= value < 1 << bits:
        raise ValueError("Invalid %s" % name)
    return value


def _signed(value, bits):
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def _instance(params):
    value = (_integer(params, "instance_hi") << 32) | _integer(params, "instance_lo")
    if not value:
        raise ValueError("Controller instance must be nonzero")
    return value


@dataclass(frozen=True)
class TransportState:
    challenge: int
    instance: int
    last_query: int
    state: int


def decode_transport_state(params):
    """An observation, not controller-instance negotiation or permission to move.

    Caller must match its challenge AND local connection generation before use.
    Zero instance means the firmware controller has not been attached yet.
    """
    instance = (_integer(params, "instance_hi") << 32) | _integer(params, "instance_lo")
    state = _integer(params, "state", 8)
    query = _integer(params, "last_query")
    if (state not in known(Binding)
            or (state in (Binding.ATTACHED, Binding.CLOCK_UNAVAILABLE)
                and not instance)
            or (state == Binding.UNBOUND and instance)):
        raise ValueError("Inconsistent transport state")
    if not instance and query:
        raise ValueError("Unbound controller has no snapshot history")
    return TransportState(_integer(params, "challenge"), instance, query, state)


@dataclass(frozen=True)
class CommandResult:
    instance: int
    command: int
    query: int
    result: int


def decode_command_result(params):
    """Administrative reply; it can never settle an operation waiter."""
    instance = (_integer(params, "instance_hi") << 32) | _integer(params, "instance_lo")
    command = _integer(params, "command", 8)
    result = _integer(params, "result", 8)
    if command not in known(Command) or result not in known(CommandCode):
        raise ValueError("Unsupported command result")
    return CommandResult(instance, command, _integer(params, "query"), result)


@dataclass(frozen=True)
class Identity:
    client: int
    session: int
    request: int
    operation: int
    lane: int


def _identity(params):
    # Rejections and unsolicited faults can carry otherwise invalid identities.
    # Decoding preserves them; matching to an admitted request is a separate step.
    return Identity(_integer(params, "client", 8), _integer(params, "session"),
                    _integer(params, "request"), _integer(params, "operation", 8),
                    _integer(params, "lane", 8))


@dataclass(frozen=True)
class Admission:
    instance: int
    identity: Identity
    admission: int
    fault: int


@dataclass(frozen=True)
class Event:
    instance: int
    identity: Identity
    seq: int
    timestamp_ms: int
    event_type: int
    result_code: int
    value: int
    flags: int


def decode_admission(params):
    return Admission(_instance(params), _identity(params),
                     _integer(params, "admission", 8), _integer(params, "fault", 8))


def decode_event(params):
    seq = _integer(params, "seq")
    if not seq:
        raise ValueError("Event sequence must be nonzero")
    return Event(_instance(params), _identity(params), seq,
                 _integer(params, "timestamp_ms"), _integer(params, "event_type", 8),
                 _integer(params, "result_code", 8), _integer(params, "value"),
                 _integer(params, "flags", 8))


def event_flags_supported(event):
    """Reject any flag this host cannot interpret for the event's type."""
    allowed = EventFlag.OVERFLOW
    if (event.event_type == EventType.PTFE_CAL_COMPLETE
            and event.identity.operation == OpType.CALIBRATE):
        allowed |= EventFlag.STEP_ESTIMATE
    return not event.flags & ~allowed


def event_supported(event):
    """A known event type, OpenAMS result code and flags for its type."""
    return (event.event_type in EVENT_TYPES
            and event.result_code in known(OpCode)
            and event_flags_supported(event))


def openams_terminal_value(event):
    """OpenAMS PTFE values are encoder lengths; its 0 means uncalibrated."""
    return 0 if event.flags & EventFlag.STEP_ESTIMATE else event.value


# Field widths are checked even for direct calls outside Klipper's parser.
_STATUS_FIELDS = {fmt.split()[0]: payload_fields(fmt) for fmt in STATUS_REPLIES}
STATUS_MESSAGES = tuple(_STATUS_FIELDS)
_STATUS_FLAGS = (
    "lane_active", "terminal_pending", "path_occupied", "position_uncertain",
    "buffer_valid", "measured_current_available", "host_connected", "motor_ready",
    "control_time_valid", "pending_fault_event",
)


@dataclass(frozen=True)
class Snapshot:
    instance: int
    query: int
    values: dict


class SnapshotAssembler:
    """One bounded query, with five parts in any order and no state inference.

    Create a new assembler for each query/connection. Never reuse query IDs for
    a controller instance, including reconnects: duplicates retransmit the
    original capture. Read the firmware query high-water mark before allocation.
    Missing parts produce no snapshot. Conflicting duplicates invalidate the query.
    The caller owns query timeout and the connection-generation guard.
    """

    def __init__(self, instance, query, selector_capable=False):
        if type(instance) is not int or not 0 < instance < 1 << 64:
            raise ValueError("Invalid controller instance")
        if type(query) is not int or not 0 < query < 1 << 32:
            raise ValueError("Invalid query ID")
        self.instance, self.query = instance, query
        self.selector_capable = selector_capable
        self.parts = {}
        self.failed = False
        self.delivered = False

    def add(self, name, params):
        if self.failed:
            raise ValueError("Snapshot query is invalid")
        expected_msgs = STATUS_MESSAGES if self.selector_capable else STATUS_MESSAGES[:-1]
        if name not in expected_msgs:
            raise ValueError("Not a status response: %s" % name)
        if _instance(params) != self.instance or _integer(params, "query") != self.query:
            return None
        try:
            part = {}
            for key, bits in _STATUS_FIELDS[name]:
                part[key] = _integer(params, key, bits)
            if name == "km_status_identity" and part["flags"] >> len(_STATUS_FLAGS):
                raise ValueError("Unsupported status flags")
            if name == "km_status_motion" and (
                    part["follower_state"] not in known(FollowerState)
                    or part["follower_direction"] not in known(Direction)):
                raise ValueError("Invalid follower controller status")
            if name in self.parts and self.parts[name] != part:
                raise ValueError("Conflicting snapshot fragment")
        except ValueError:
            self.failed = True
            raise
        self.parts[name] = part
        expected = len(_STATUS_FIELDS) if self.selector_capable else len(_STATUS_FIELDS) - 1
        if self.delivered or len(self.parts) != expected:
            return None
        values = {}
        for key in STATUS_MESSAGES[:3]:
            values.update(self.parts[key])
        if self.selector_capable:
            values.update(self.parts["km_status_selector"])
        flags = values.pop("flags")
        values.update((key, bool(flags & (1 << bit)))
                      for bit, key in enumerate(_STATUS_FLAGS))
        position = (values.pop("position_hi") << 32) | values.pop("position_lo")
        values["position"] = _signed(position, 64)
        values["velocity"] = _signed(values.pop("velocity_bits"), 32)
        values["buffer_position"] = _signed(values.pop("buffer_position_bits"), 32)
        for kind in ("sessions", "requests"):
            values["client_" + kind] = tuple(
                self.parts["km_status_" + kind]["client%d" % i] for i in range(4))
        self.delivered = True
        return Snapshot(self.instance, self.query, values)


_SENSOR_FIELDS = {fmt.split()[0]: payload_fields(fmt) for fmt in SENSOR_REPLIES}
_sensor_names = tuple(_SENSOR_FIELDS)
# Callers pick a subset of these by name (not by position) and look its wire
# format up in SENSOR_FORMATS: BAYS2/LOADS2 are gated independently of one
# another, so the requested names are not always a prefix of SENSOR_REPLIES.
SENSOR_FORMATS = dict(zip(_sensor_names, SENSOR_REPLIES))
# The firmware's km_sensor_part enum value for each message name (its fixed
# position in SENSOR_REPLIES/mcu/messages.h), independent of which subset a
# caller requests.
SENSOR_PART_INDEX = {name: i for i, name in enumerate(_sensor_names)}
SENSOR_MESSAGES = _sensor_names[:3]              # flags, bays0, bays1: always sent.
SENSOR_LOAD_MESSAGES = _sensor_names[3:5]        # loads0, loads1: load_capable.
SENSOR_WIDE_MESSAGES = _sensor_names[5:6]        # bays2: lane_count > 8.
SENSOR_WIDE_LOAD_MESSAGES = _sensor_names[6:]    # loads2: load_capable and lane_count > 8.


@dataclass(frozen=True)
class SensorSample:
    configured: bool
    present: bool
    valid: bool
    fresh: bool
    sample_ms: int


@dataclass(frozen=True)
class SensorSnapshot:
    instance: int
    query: int
    lane_count: int
    observed_ms: int
    bays: tuple
    hub: SensorSample
    toolhead: SensorSample
    loads: tuple = (SensorSample(False, False, False, False, 0),) * 8


def _bay_bit(lane):
    """Bit index of a BAY lane in the km_sensor_flags masks: lanes 0-7 keep
    bits 0-7; HUB/toolhead sit at 8/9; lanes 8-11 sit at bits 10-13."""
    return lane if lane < 8 else lane + 2


class SensorSnapshotAssembler:
    """Decode a retained capture (3, 4, 5, 6 or 7 parts) with no absence inference.

    A present value is an observation only when configured, valid and fresh
    are all true. A stale value remains visible so callers can explain why
    they have withheld a physical state from OpenAMS. wide_lanes selects the
    BAYS2/LOADS2 parts, sent only when the profile's lane_count exceeds 8.
    """

    def __init__(self, instance, query, load_capable=False, wide_lanes=False):
        if type(instance) is not int or not 0 < instance < 1 << 64:
            raise ValueError("Invalid controller instance")
        if type(query) is not int or not 0 < query < 1 << 32:
            raise ValueError("Invalid query ID")
        self.instance, self.query = instance, query
        self.load_capable, self.wide_lanes = load_capable, wide_lanes
        self.parts = {}
        self.failed = False
        self.delivered = False

    def add(self, name, params):
        if self.failed:
            raise ValueError("Sensor query is invalid")
        expected = (SENSOR_MESSAGES + (SENSOR_WIDE_MESSAGES if self.wide_lanes else ())
                   + (SENSOR_LOAD_MESSAGES if self.load_capable else ())
                   + (SENSOR_WIDE_LOAD_MESSAGES if self.load_capable and self.wide_lanes else ()))
        if name not in expected:
            raise ValueError("Not a sensor response: %s" % name)
        if _instance(params) != self.instance or _integer(params, "query") != self.query:
            return None
        try:
            part = {}
            for key, bits in _SENSOR_FIELDS[name]:
                part[key] = _integer(params, key, bits)
            if name in self.parts and self.parts[name] != part:
                raise ValueError("Conflicting sensor fragment")
            if name == "km_sensor_flags":
                count = part["lane_count"]
                # A non-wide assembler never receives BAYS2/LOADS2, so it has
                # no sample for lanes 8-11; bound count to what it can decode.
                if not 1 <= count <= (12 if self.wide_lanes else 8):
                    raise ValueError("Invalid sensor lane count")
                used = (((1 << min(count, 8)) - 1) | (3 << 8)
                       | (((1 << max(count - 8, 0)) - 1) << 10))
                if any(part[key] & ~used for key in
                       ("configured", "present", "valid", "fresh")):
                    raise ValueError("Unused sensor bits must be clear")
                if (part["valid"] & ~part["configured"]
                        or part["fresh"] & ~part["valid"]
                        or part["present"] & ~part["configured"]):
                    raise ValueError("Inconsistent sensor flags")
            if name in ("km_sensor_loads0", "km_sensor_loads2"):
                count = (self.parts["km_sensor_flags"]["lane_count"]
                        if "km_sensor_flags" in self.parts else 12)
                used = ((1 << min(count, 8)) - 1 if name == "km_sensor_loads0"
                        else (1 << max(min(count, 12) - 8, 0)) - 1)
                if any(part[key] & ~used for key in ("configured", "present", "valid", "fresh")):
                    raise ValueError("Unused load bits must be clear")
                if (part["valid"] & ~part["configured"] or
                        part["fresh"] & ~part["valid"] or
                        part["present"] & ~part["configured"]):
                    raise ValueError("Inconsistent load flags")
        except ValueError:
            self.failed = True
            raise
        self.parts[name] = part
        if self.delivered or len(self.parts) != len(expected):
            return None
        flags = self.parts["km_sensor_flags"]
        count = flags["lane_count"]
        stamps = dict(self.parts["km_sensor_bays0"])
        stamps.update(self.parts["km_sensor_bays1"])
        if self.wide_lanes:
            stamps.update(self.parts["km_sensor_bays2"])
        capacity = 12 if self.wide_lanes else 8
        if any(stamps["sample%d" % i] for i in range(count, capacity)):
            self.failed = True
            raise ValueError("Unused bay timestamps must be zero")
        pairs = [(_bay_bit(i), stamps["sample%d" % i]) for i in range(capacity)]
        pairs += [(8, stamps["hub_sample_ms"]), (9, stamps["toolhead_sample_ms"])]
        if any(time and not (flags["configured"] & (1 << bit)) for bit, time in pairs):
            self.failed = True
            raise ValueError("Unconfigured sensor timestamp must be zero")
        loads = (SensorSample(False, False, False, False, 0),) * 8
        if self.load_capable:
            load_flags = self.parts["km_sensor_loads0"]
            load_stamps = dict(load_flags)
            load_stamps.update(self.parts["km_sensor_loads1"])
            base = min(count, 8)
            used = (1 << base) - 1
            if any(load_flags[key] & ~used for key in ("configured", "present", "valid", "fresh")):
                self.failed = True
                raise ValueError("Unused load bits must be clear")
            if any(load_stamps["sample%d" % i] for i in range(base, 8)):
                self.failed = True
                raise ValueError("Unused load timestamps must be zero")
            if any(load_stamps["sample%d" % i] and not (load_flags["configured"] & (1 << i))
                   for i in range(base)):
                self.failed = True
                raise ValueError("Unconfigured load timestamp must be zero")
            loads = tuple(SensorSample(*(bool(load_flags[key] & (1 << i)) for key in
                                         ("configured", "present", "valid", "fresh")),
                                       load_stamps["sample%d" % i]) for i in range(8))
            if self.wide_lanes:
                wide_flags = self.parts["km_sensor_loads2"]
                wide_stamps = dict(wide_flags)
                wide_base = max(count - 8, 0)
                wide_used = (1 << wide_base) - 1
                if any(wide_flags[key] & ~wide_used for key in ("configured", "present", "valid", "fresh")):
                    self.failed = True
                    raise ValueError("Unused wide load bits must be clear")
                if any(wide_stamps["sample%d" % (8 + i)] for i in range(wide_base, 4)):
                    self.failed = True
                    raise ValueError("Unused wide load timestamps must be zero")
                if any(wide_stamps["sample%d" % (8 + i)] and not (wide_flags["configured"] & (1 << i))
                       for i in range(wide_base)):
                    self.failed = True
                    raise ValueError("Unconfigured wide load timestamp must be zero")
                loads = loads + tuple(SensorSample(*(bool(wide_flags[key] & (1 << i)) for key in
                                                     ("configured", "present", "valid", "fresh")),
                                                   wide_stamps["sample%d" % (8 + i)]) for i in range(4))

        def sample(bit, time):
            b = 1 << bit
            return SensorSample(*(bool(flags[key] & b) for key in
                                  ("configured", "present", "valid", "fresh")), time)

        self.delivered = True
        return SensorSnapshot(
            self.instance, self.query, count, flags["observed_ms"],
            tuple(sample(_bay_bit(i), stamps["sample%d" % i]) for i in range(count)),
            sample(8, stamps["hub_sample_ms"]),
            sample(9, stamps["toolhead_sample_ms"]), loads)


# Switch bounce/idle-edge and TMC gstat/drv_status counters (docs/diagnostics.md).
_DIAG_FIELDS = {fmt.split()[0]: payload_fields(fmt) for fmt in DIAG_REPLIES}
DIAG_MESSAGES = tuple(_DIAG_FIELDS)


@dataclass(frozen=True)
class DiagSnapshot:
    instance: int
    query: int
    bay_bounce: tuple
    load_bounce: tuple
    bay_idle_edges: tuple
    load_idle_edges: tuple
    hub_bounce: int
    hub_idle_edges: int
    toolhead_bounce: int
    gstat: tuple
    drv_status: tuple


class DiagSnapshotAssembler:
    """One bounded query, six parts in any order; see SnapshotAssembler.

    Raw saturating counters and TMC registers only: no validity/freshness
    semantics, and never authoritative for control or path ownership.
    """

    def __init__(self, instance, query):
        if type(instance) is not int or not 0 < instance < 1 << 64:
            raise ValueError("Invalid controller instance")
        if type(query) is not int or not 0 < query < 1 << 32:
            raise ValueError("Invalid query ID")
        self.instance, self.query = instance, query
        self.parts = {}
        self.failed = False
        self.delivered = False

    def add(self, name, params):
        if self.failed:
            raise ValueError("Diag query is invalid")
        if name not in _DIAG_FIELDS:
            raise ValueError("Not a diag response: %s" % name)
        if _instance(params) != self.instance or _integer(params, "query") != self.query:
            return None
        try:
            part = {key: _integer(params, key, bits) for key, bits in _DIAG_FIELDS[name]}
            if name in self.parts and self.parts[name] != part:
                raise ValueError("Conflicting diag fragment")
        except ValueError:
            self.failed = True
            raise
        self.parts[name] = part
        if self.delivered or len(self.parts) != len(_DIAG_FIELDS):
            return None
        bay_bounce, load_bounce = self.parts["km_diag_bay_bounce"], self.parts["km_diag_load_bounce"]
        bay_idle, load_idle = self.parts["km_diag_bay_idle"], self.parts["km_diag_load_idle"]
        tmc0, tmc1 = self.parts["km_diag_tmc0"], self.parts["km_diag_tmc1"]
        self.delivered = True
        return DiagSnapshot(
            self.instance, self.query,
            tuple(bay_bounce["count%d" % i] for i in range(8)),
            tuple(load_bounce["count%d" % i] for i in range(8)),
            tuple(bay_idle["edges%d" % i] for i in range(8)),
            tuple(load_idle["edges%d" % i] for i in range(8)),
            bay_idle["hub_bounce"], bay_idle["hub_edges"], bay_idle["toolhead_bounce"],
            tuple(tmc0["gstat%d" % i] for i in range(4))
            + tuple(tmc1["gstat%d" % i] for i in range(4, 8)),
            tuple(tmc0["drv_status%d" % i] for i in range(4))
            + tuple(tmc1["drv_status%d" % i] for i in range(4, 8)))
