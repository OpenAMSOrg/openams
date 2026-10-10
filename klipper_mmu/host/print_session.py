# SPDX-License-Identifier: GPL-3.0-or-later
"""Issue one BoxTurtle load and follow, or unload the gate that follow used.

Motion stays in the firmware operation core. This object resolves the tool
through the saved assignment, rejects an unmapped tool before any command is
sent, then heartbeats and issues LOAD and FOLLOW on that gate. release_tool
heartbeats and admits UNLOAD on the gate a ready request recorded. A request
for a different gate unloads that gate first and does not load when the unload
is rejected. The gate already followed sends nothing. An idle session sends
nothing and does not rewrite the stored record. Sensor values
come only from the capture the caller supplies. An admission is not a terminal.
"""
import json
import os
import tempfile

from .command_transport import send_heartbeat, send_operation
from .protocol import Admission, CommandResult, Event, Identity
from .requests import FOLLOW, LOAD, UNLOAD, Requests
from .tool_mapping import ToolMapping
from .wire import (AdmissionResult, Client, CommandCode, Direction, EventFlag,
                   EventType, Fault, OpCode)

_WAITER = "print"


def observation_from_capture(capture):
    """Return observed samples. A missing, unconfigured, or stale sample is None."""
    if type(capture) is not dict:
        raise ValueError("sensor capture must be an object")
    observed = {}
    for key, sample in capture.items():
        if type(key) is not str or not key:
            raise ValueError("invalid sensor name")
        if sample is None:
            observed[key] = None
            continue
        if (type(sample) is not dict or sample.get("configured") is not True
                or sample.get("valid") is not True or "present" not in sample
                or type(sample["present"]) is not bool):
            observed[key] = None
            continue
        observed[key] = sample["present"]
    return observed


def _public_sensors(observed):
    sensors = {}
    for key, value in observed.items():
        sensors[key] = "unknown" if value is None else value
    return sensors


class PrintSession:
    """One host session that maps a tool to a gate and records firmware results."""

    def __init__(self, mapping, requests, mcu, reactor, is_current, capture,
                 session_path):
        if (not isinstance(mapping, ToolMapping) or not isinstance(requests, Requests)
                or not callable(is_current) or not callable(capture)):
            raise ValueError("print session is missing its assignment or transport")
        if not os.path.isabs(session_path):
            raise ValueError("print session path must be absolute")
        self.mapping = mapping
        self.requests = requests
        self.mcu = mcu
        self.reactor = reactor
        self.is_current = is_current
        self.capture = capture
        self.session_path = session_path
        self._status = None
        self._pending_unload = None
        self._pending_unload_instance = None
        self._pending_load = None
        self._pending_load_instance = None
        self._load_complete = None
        self._load_complete_instance = None
        self._load_complete_authorized = False
        if os.path.exists(session_path):
            self._status = _read_status(session_path)
            self._pending_unload = _read_pending(self._status)
            if self._pending_unload is not None:
                self._pending_unload_instance = self._status["pending_unload"]["instance"]
            self._pending_load = _read_identity(self._status, "pending_load", LOAD)
            self._load_complete = _read_identity(self._status, "load_complete", LOAD)
            for key in ("pending_load", "load_complete"):
                if key in self._status:
                    setattr(self, "_" + key + "_instance", self._status[key]["instance"])

    def request_tool(self, tool):
        """Admit LOAD for a mapped tool; FOLLOW requires terminal reconciliation."""
        gate = self.mapping.resolve(tool)
        if gate is None:
            return {"tool": tool, "gate": None, "ready": False,
                    "fault": "unmapped", "sensors": {}}
        if self._status is not None and self._status.get("ready") is True:
            if self._status.get("gate") == gate:
                return self.management_status()
            if type(self._status.get("gate")) is int:
                released = self.release_tool()
                if released.get("fault") or self._pending_unload is not None:
                    return released
        elif self._pending_unload is not None:
            return self.management_status()
        if self._pending_load is not None or self._load_complete is not None:
            return self.management_status()
        beat = send_heartbeat(self.mcu, self.reactor, self.requests.instance,
                              self.requests.session, self.is_current)
        if not isinstance(beat, CommandResult) or beat.result != CommandCode.OK:
            return self._record(tool, gate, False, "heartbeat", {})
        load = self.requests.issue(LOAD, gate, _WAITER, 0)
        load_reply = send_operation(self.mcu, self.reactor, self.requests.instance,
                                    load, self.is_current,
                                    direction=Direction.FORWARD)
        if not _admitted(load_reply, load):
            return self._record(tool, gate, False, _fault_name(load_reply),
                                observation_from_capture(self.capture()))
        self._pending_load = load
        self._pending_load_instance = self.requests.instance
        return self._record(tool, gate, False, "load_pending",
                            observation_from_capture(self.capture()))

    def reconcile_load_terminal(self, event, generation):
        """Unlock FOLLOW only after this request's exact successful LOAD terminal."""
        pending = self._pending_load
        if (pending is None or not isinstance(event, Event)
                or not self.requests.connected or generation != self.requests.generation
                or event.instance != self.requests.instance
                or event.instance != self._pending_load_instance
                or event.identity != pending or event.identity.session != self.requests.session
                or event.identity.operation != LOAD
                or event.event_type != EventType.LOAD_COMPLETE
                or event.result_code != OpCode.SUCCESS
                or event.flags & ~EventFlag.OVERFLOW):
            return False
        self._pending_load = None
        self._pending_load_instance = None
        self._load_complete = pending
        self._load_complete_instance = event.instance
        self._load_complete_authorized = True
        self._record(self._status["tool"], self._status["gate"], False,
                     "follow_pending", {key: None for key in self._status["sensors"]})
        return True

    def start_follow(self):
        """Send FOLLOW after a matching LOAD terminal was explicitly reconciled."""
        identity = self._load_complete
        if (identity is None or not self._load_complete_authorized
                or identity.lane != self._status.get("gate")
                or identity.session != self.requests.session
                or self._load_complete_instance != self.requests.instance
                or not self.requests.connected or not self.is_current()):
            return self.management_status()
        beat = send_heartbeat(self.mcu, self.reactor, self.requests.instance,
                              self.requests.session, self.is_current)
        if not isinstance(beat, CommandResult) or beat.result != CommandCode.OK:
            return self._record(self._status["tool"], self._status["gate"], False,
                                "heartbeat", {})
        follow = self.requests.issue(FOLLOW, identity.lane)
        reply = send_operation(self.mcu, self.reactor, self.requests.instance,
                               follow, self.is_current,
                               direction=Direction.FORWARD)
        if not _admitted(reply, follow):
            return self._record(self._status["tool"], self._status["gate"], False,
                                _fault_name(reply), observation_from_capture(self.capture()))
        self._load_complete = None
        self._load_complete_instance = None
        self._load_complete_authorized = False
        return self._record(self._status["tool"], self._status["gate"], True, None,
                            observation_from_capture(self.capture()))

    def release_tool(self):
        """Admit UNLOAD on the followed gate. An idle session sends nothing."""
        if not hasattr(self, "requests"):
            raise ValueError("print session cannot release a stored record")
        if self._status is None or self._status.get("ready") is not True:
            return _idle_release(self._status)
        tool, gate = self._status["tool"], self._status["gate"]
        if type(gate) is not int:
            return self._record(tool, None, False, "idle", {})
        beat = send_heartbeat(self.mcu, self.reactor, self.requests.instance,
                              self.requests.session, self.is_current)
        if not isinstance(beat, CommandResult) or beat.result != CommandCode.OK:
            return self._record(tool, gate, True, "heartbeat", {})
        unload = self.requests.issue(UNLOAD, gate, _WAITER, 0)
        reply = send_operation(self.mcu, self.reactor, self.requests.instance,
                               unload, self.is_current,
                               direction=Direction.REVERSE)
        admitted = _admitted(reply, unload)
        self._pending_unload = unload if admitted else None
        self._pending_unload_instance = self.requests.instance if admitted else None
        return self._record(tool, gate, not admitted,
                            "unload_pending" if admitted else _fault_name(reply),
                            observation_from_capture(self.capture()))

    def reconcile_unload_terminal(self, event, generation):
        """Clear an unload hold only for its exact successful retained terminal.

        The transport/event consumer remains responsible for retained-event
        acknowledgement. This method only reconciles this print-session record.
        """
        pending = self._pending_unload
        if (pending is None or not isinstance(event, Event)
                or getattr(self, "requests", None) is None):
            return False
        if (not self.requests.connected or generation != self.requests.generation
                or event.instance != self.requests.instance
                or event.instance != self._pending_unload_instance
                or event.identity != pending or event.identity.session != self.requests.session
                or event.identity.operation != UNLOAD
                or event.event_type != EventType.UNLOAD_COMPLETE):
            return False
        if (event.result_code != OpCode.SUCCESS
                or event.flags & ~EventFlag.OVERFLOW):
            return False
        self._pending_unload = None
        self._pending_unload_instance = None
        self._record(self._status["tool"], self._status["gate"], False, None,
                     {key: None for key in self._status["sensors"]})
        return True

    def management_status(self):
        """Tool, gate, and ready or latched fault. Unobserved sensors stay unknown."""
        if self._status is None:
            return {"tool": None, "gate": None, "ready": False,
                    "fault": None, "sensors": {}}
        status = dict(self._status)
        status["sensors"] = dict(self._status["sensors"])
        if self._pending_unload is not None:
            status["pending_unload"] = _identity_record(
                self._pending_unload, self._pending_unload_instance)
        if self._pending_load is not None:
            status["pending_load"] = _identity_record(
                self._pending_load, self._pending_load_instance)
        if self._load_complete is not None:
            status["load_complete"] = _identity_record(
                self._load_complete, self._load_complete_instance)
        return status

    def _record(self, tool, gate, ready, fault, observed):
        status = {"tool": tool, "gate": gate, "ready": ready, "fault": fault,
                  "sensors": _public_sensors(observed)}
        if self._pending_unload is not None:
            status["pending_unload"] = _identity_record(
                self._pending_unload, self._pending_unload_instance)
        if self._pending_load is not None:
            status["pending_load"] = _identity_record(
                self._pending_load, self._pending_load_instance)
        if self._load_complete is not None:
            status["load_complete"] = _identity_record(
                self._load_complete, self._load_complete_instance)
        _write_status(self.session_path, status)
        self._status = status
        return self.management_status()

    @classmethod
    def load(cls, session_path):
        """Read the session a previous request_tool call left on disk."""
        if not os.path.isabs(session_path):
            raise ValueError("print session path must be absolute")
        status = _read_status(session_path)
        loaded = cls.__new__(cls)
        loaded._status = status
        loaded._pending_unload = _read_pending(status)
        loaded._pending_unload_instance = (status["pending_unload"]["instance"]
                                           if loaded._pending_unload is not None else None)
        loaded._pending_load = _read_identity(status, "pending_load", LOAD)
        loaded._load_complete = _read_identity(status, "load_complete", LOAD)
        loaded._pending_load_instance = (status["pending_load"]["instance"]
                                         if loaded._pending_load is not None else None)
        loaded._load_complete_instance = (status["load_complete"]["instance"]
                                          if loaded._load_complete is not None else None)
        loaded._load_complete_authorized = False
        loaded.session_path = session_path
        return loaded


def _idle_release(status):
    if status is None:
        return {"tool": None, "gate": None, "ready": False,
                "fault": "idle", "sensors": {}}
    return {"tool": status["tool"], "gate": status["gate"], "ready": False,
            "fault": "idle", "sensors": {}}


def _identity_record(identity, instance):
    return {"instance": instance, "client": identity.client, "session": identity.session,
            "request": identity.request, "operation": identity.operation,
            "lane": identity.lane}


def _read_pending(status):
    record = status.get("pending_unload")
    if record is None:
        return None
    if (type(record) is not dict or set(record) !=
            {"instance", "client", "session", "request", "operation", "lane"}
            or type(record["instance"]) is not int or record["instance"] <= 0
            or record["client"] != Client.HOST
            or type(record["session"]) is not int or type(record["request"]) is not int
            or type(record["lane"]) is not int or record["operation"] != UNLOAD
            or record["session"] == 0 or record["request"] == 0):
        raise ValueError("print session pending unload is malformed")
    return Identity(record["client"], record["session"], record["request"],
                    record["operation"], record["lane"])


def _read_identity(status, key, operation):
    record = status.get(key)
    if record is None:
        return None
    if (type(record) is not dict or set(record) !=
            {"instance", "client", "session", "request", "operation", "lane"}
            or type(record["instance"]) is not int or record["instance"] <= 0
            or type(record["client"]) is not int or record["client"] != Client.HOST
            or type(record["session"]) is not int or not record["session"]
            or type(record["request"]) is not int or not record["request"]
            or type(record["operation"]) is not int or record["operation"] != operation
            or type(record["lane"]) is not int or not 0 <= record["lane"] < 8):
        raise ValueError("print session %s identity is malformed" % key)
    return Identity(record["client"], record["session"], record["request"],
                    record["operation"], record["lane"])


def _admitted(reply, identity):
    return (isinstance(reply, Admission) and reply.identity == identity
            and reply.admission == AdmissionResult.ACCEPTED
            and reply.fault == Fault.NONE)


def _fault_name(reply):
    if isinstance(reply, Admission):
        if reply.fault != Fault.NONE:
            return "fault:%d" % reply.fault
        return "admission:%d" % reply.admission
    if isinstance(reply, CommandResult):
        return "command:%d" % reply.result
    return "unavailable"


def _read_status(path):
    with open(path, "rb") as source:
        contents = source.read(4096)
    record = json.loads(contents.decode("utf-8"))
    base = {"tool", "gate", "ready", "fault", "sensors"}
    optional = {"pending_unload", "pending_load", "load_complete"}
    if (type(record) is not dict or not base.issubset(record)
            or set(record) - base - optional
            or type(record["tool"]) is not int or type(record["ready"]) is not bool
            or not (record["gate"] is None or type(record["gate"]) is int)
            or not (record["fault"] is None or type(record["fault"]) is str)
            or type(record["sensors"]) is not dict):
        raise ValueError("print session record is malformed")
    for key, value in record["sensors"].items():
        if type(key) is not str or not (type(value) is bool or value == "unknown"):
            raise ValueError("print session sensor was not observed")
    return record


def _write_status(path, status):
    _read_check = dict(status)
    _read_check["sensors"] = dict(status["sensors"])
    contents = (json.dumps(_read_check, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    directory = os.path.dirname(path)
    fd, temporary = tempfile.mkstemp(prefix=".print-session-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
