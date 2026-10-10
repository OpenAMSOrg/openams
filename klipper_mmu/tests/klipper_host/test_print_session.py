# SPDX-License-Identifier: GPL-3.0-or-later
"""Tool-mapped load and follow, release of that gate, and management status."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from host.print_session import PrintSession, observation_from_capture
from host.protocol import Event, Identity
from host.requests import Requests

INSTANCE = 0x1020304050607080
SESSION = 4


class Completion:
    def __init__(self):
        self.value = None

    def test(self):
        return self.value is not None

    def complete(self, value):
        self.value = value

    def wait(self, deadline):
        return self.value


class Reactor:
    def __init__(self):
        self.now = 1.

    def completion(self):
        return Completion()

    def monotonic(self):
        return self.now

    def register_async_callback(self, callback):
        callback(self.now)


class Wrapper:
    def __init__(self, mcu, callback, name):
        self.mcu, self.callback, self.name = mcu, callback, name

    def unregister(self):
        self.mcu.listeners.remove(self)


class MCU:
    def __init__(self, respond):
        self.respond = respond
        self.listeners = []
        self.sent = []
        self.command = None

    def lookup_command(self, fmt):
        self.command = fmt
        return self

    def send(self, args):
        self.sent.append(args)
        self.respond(self)

    def register_serial_response(self, callback, fmt):
        wrapper = Wrapper(self, callback, fmt.split()[0])
        self.listeners.append(wrapper)
        return wrapper

    def emit(self, name, params):
        for wrapper in list(self.listeners):
            if wrapper.name == name:
                wrapper.callback(params)


def _identity_params(args, admission=0, fault=0):
    return dict(instance_hi=INSTANCE >> 32, instance_lo=INSTANCE & 0xffffffff,
                client=args[2], session=args[3], request=args[4],
                operation=args[5], lane=args[6], admission=admission, fault=fault)


def _result_params(query, command, result=0):
    return dict(instance_hi=INSTANCE >> 32, instance_lo=INSTANCE & 0xffffffff,
                command=command, query=query, result=result)


class LaneBoard:
    """Records routed km_start commands; lane counters are not motion evidence."""

    def __init__(self):
        self.steps = [0, 0, 0, 0]
        self.commands = []
        self.capture = {
            "encoder": {"configured": False, "valid": False, "present": False},
            "load": {"configured": True, "valid": False, "present": False},
        }
        self.mcu = MCU(self.respond)

    def respond(self, mcu):
        args = mcu.sent[-1]
        if mcu.command.startswith("km_heartbeat"):
            self.commands.append(("heartbeat", None))
            mcu.emit("km_command_result", _result_params(args[2], 3))
            return
        operation, lane = args[5], args[6]
        self.commands.append((operation, lane))
        self.steps[lane] += 4
        self.capture.update({
            "bay": {"configured": True, "valid": True, "present": True},
            "hub": {"configured": True, "valid": True, "present": True},
            "toolhead": {"configured": True, "valid": True, "present": True},
            "buffer": {"configured": True, "valid": True, "present": True},
        })
        mcu.emit("km_admission", _identity_params(args))

    def zero_steps(self):
        self.steps = [0, 0, 0, 0]


def _session(mapping, board, path):
    return PrintSession(mapping, Requests(INSTANCE, SESSION), board.mcu, Reactor(),
                        lambda: True, lambda: board.capture, path)


def drive(mapping, board, path, tool):
    before = len(board.commands)
    status = _session(mapping, board, path).request_tool(tool)
    moved = [index for index, count in enumerate(board.steps) if count]
    print("tool-map tool=%s gate=%s routed_lanes=%s command_counts=%s commands=%s"
          % (tool, status["gate"], moved, board.steps, board.commands[before:]))
    return status


def complete_load_and_follow(mapping, board, path, status):
    session = _session(mapping, board, path)
    identity = Identity(1, SESSION, status["pending_load"]["request"], 1, status["gate"])
    terminal = Event(INSTANCE, identity, 1, 100, 1, 0, 0, 0)
    assert session.reconcile_load_terminal(terminal, 1)
    return session.start_follow()


def run_motion(session_path, mapping_path):
    from host.tool_mapping import ToolMapping
    mapping = ToolMapping(mapping_path, 4)
    board = LaneBoard()
    mapping.set(0, 1)
    board.zero_steps()
    first = drive(mapping, board, session_path, 0)
    assert first["gate"] == 1 and not first["ready"]
    assert first["fault"] == "load_pending" and board.steps[1] > 0
    assert [item for item in board.commands if item[0] != "heartbeat"] == [(1, 1)]
    first = complete_load_and_follow(mapping, board, session_path, first)
    assert first["ready"]
    assert board.steps == [0, board.steps[1], 0, 0]
    assert [item for item in board.commands if item[0] != "heartbeat"][-2:] == [(1, 1), (3, 1)]
    held = len(board.commands)
    repeat = _session(mapping, board, session_path).request_tool(0)
    assert repeat["gate"] == 1 and repeat["ready"] and len(board.commands) == held

    mapping.set(0, 2)
    board.zero_steps()
    blocked = drive(mapping, board, session_path, 0)
    assert blocked["gate"] == 1 and not blocked["ready"]
    assert blocked["fault"] == "unload_pending"
    assert board.steps == [0, board.steps[1], 0, 0]
    session = _session(mapping, board, session_path)
    ident = Identity(1, SESSION, blocked["pending_unload"]["request"], 2, 1)
    event = Event(INSTANCE, ident, 1, 100, 4, 0, 0, 0)
    assert session.reconcile_unload_terminal(event, 1)
    board.zero_steps()
    second = drive(mapping, board, session_path, 0)
    assert second["fault"] == "load_pending" and not second["ready"]
    second = complete_load_and_follow(mapping, board, session_path, second)
    assert second["gate"] == 2 and second["ready"] and board.steps[2] > 0
    assert board.steps[1] == 0 and board.steps[0] == 0 and board.steps[3] == 0
    assert [item for item in board.commands if item[0] != "heartbeat"][-3:] == [(2, 1), (1, 2), (3, 2)]

    mapping.clear(0)
    board.zero_steps()
    before = list(board.commands)
    rejected = drive(mapping, board, session_path, 0)
    assert rejected["gate"] is None and rejected["fault"] == "unmapped"
    assert rejected["sensors"] == {}
    assert board.steps == [0, 0, 0, 0] and board.commands == before
    assert PrintSession.load(session_path).management_status()["gate"] == 2
    return PrintSession.load(session_path).management_status()


def run_release(session_path, mapping_path):
    from host.tool_mapping import ToolMapping
    mapping = ToolMapping(mapping_path, 4)
    board = LaneBoard()
    mapping.set(0, 1)
    ready = drive(mapping, board, session_path, 0)
    ready = complete_load_and_follow(mapping, board, session_path, ready)
    assert ready["gate"] == 1 and ready["ready"]
    session = _session(mapping, board, session_path)
    before = len(board.commands)
    steps = list(board.steps)
    released = session.release_tool()
    issued = board.commands[before:]
    print("release tool=%s gate=%s commands=%s steps=%s"
          % (released["tool"], released["gate"], issued, board.steps))
    assert released["tool"] == 0 and released["gate"] == 1
    assert released["ready"] is False and released["fault"] == "unload_pending"
    assert [item for item in issued if item[0] != "heartbeat"] == [(2, 1)]
    assert board.steps[1] > steps[1]
    assert [board.steps[0], board.steps[2], board.steps[3]] == [0, 0, 0]
    stored = PrintSession.load(session_path).management_status()
    assert stored["ready"] is False and stored["fault"] == "unload_pending" and stored["gate"] == 1
    idle_at = len(board.commands)
    idle = session.release_tool()
    assert idle["fault"] == "idle" and len(board.commands) == idle_at
    assert PrintSession.load(session_path).management_status() == stored
    board.zero_steps()
    identity = Identity(1, SESSION, stored["pending_unload"]["request"], 2, 1)
    terminal = Event(INSTANCE, identity, 1, 20, 4, 0, 0, 0)
    assert session.reconcile_unload_terminal(terminal, 1)
    again = session.request_tool(0)
    assert not again["ready"] and again["fault"] == "load_pending"
    again = complete_load_and_follow(mapping, board, session_path, again)
    assert again["ready"] and again["gate"] == 1
    assert board.steps == [0, board.steps[1], 0, 0]
    return stored


def run_status(session_path):
    status = PrintSession.load(session_path).management_status()
    print("management_status %s" % json.dumps(status, sort_keys=True))
    assert status["tool"] == 0 and status["gate"] == 2 and status["ready"] is True
    assert status["fault"] is None
    sensors = status["sensors"]
    assert sensors["bay"] is True and sensors["hub"] is True
    assert sensors["toolhead"] is True and sensors["buffer"] is True
    assert sensors["encoder"] == "unknown" and sensors["load"] == "unknown"
    return status


def run_rejected_retarget(session_path, mapping_path):
    from host.tool_mapping import ToolMapping
    mapping = ToolMapping(mapping_path, 4)
    board = LaneBoard()
    mapping.set(0, 1)
    loaded = drive(mapping, board, session_path, 0)
    assert complete_load_and_follow(mapping, board, session_path, loaded)["ready"]

    def reject_unload(mcu):
        if mcu.command.startswith("km_heartbeat"):
            LaneBoard.respond(board, mcu)
            return
        args = mcu.sent[-1]
        if args[5] == 2:
            board.commands.append((args[5], args[6]))
            mcu.emit("km_admission", _identity_params(args, admission=5))
            return
        LaneBoard.respond(board, mcu)

    board.mcu.respond = reject_unload
    mapping.set(0, 2)
    before = list(board.commands)
    blocked = _session(mapping, board, session_path).request_tool(0)
    print("retarget blocked fault=%s commands=%s" % (blocked["fault"], board.commands[len(before):]))
    assert blocked["gate"] == 1 and blocked["ready"] is True
    assert blocked["fault"] == "admission:5"
    assert [item for item in board.commands[len(before):] if item[0] != "heartbeat"] == [(2, 1)]
    assert PrintSession.load(session_path).management_status()["gate"] == 1
    before_retry = len(board.commands)
    retry = _session(mapping, board, session_path).request_tool(0)
    assert retry["ready"] is True and retry["fault"] == "admission:5"
    assert [item for item in board.commands[before_retry:] if item[0] != "heartbeat"] == [(2, 1)]


class PrintSessionTest(unittest.TestCase):
    def test_load_terminal_is_required_before_explicit_follow(self):
        from host.tool_mapping import ToolMapping
        with tempfile.TemporaryDirectory() as directory:
            mapping = ToolMapping(os.path.join(directory, "tools.json"), 4)
            mapping.set(0, 1)
            board = LaneBoard()
            path = os.path.join(directory, "session.json")
            session = _session(mapping, board, path)
            pending = session.request_tool(0)
            self.assertEqual(pending["fault"], "load_pending")
            starts = [item for item in board.commands if item[0] != "heartbeat"]
            self.assertEqual(starts, [(1, 1)])
            self.assertFalse(session.start_follow()["ready"])
            self.assertEqual([item for item in board.commands if item[0] != "heartbeat"], starts)
            identity = Identity(1, SESSION, pending["pending_load"]["request"], 1, 1)
            wrong = Event(INSTANCE, Identity(1, SESSION, identity.request + 1, 1, 1),
                          1, 10, 1, 0, 0, 0)
            cancelled = Event(INSTANCE, identity, 2, 11, 2, 0, 0, 0)
            failed = Event(INSTANCE, identity, 3, 12, 3, 1, 0, 0)
            success = Event(INSTANCE, identity, 4, 13, 1, 0, 0, 0)
            self.assertFalse(session.reconcile_load_terminal(wrong, 1))
            self.assertFalse(session.reconcile_load_terminal(cancelled, 1))
            self.assertFalse(session.reconcile_load_terminal(failed, 1))
            self.assertFalse(session.reconcile_load_terminal(success, 0))
            self.assertTrue(session.reconcile_load_terminal(success, 1))
            # Persisted terminal state does not authorize a cold-start FOLLOW.
            restarted = _session(mapping, board, path)
            before = len(board.commands)
            self.assertFalse(restarted.start_follow()["ready"])
            self.assertEqual(len(board.commands), before)
            ready = session.start_follow()
            self.assertTrue(ready["ready"])
            self.assertEqual([item for item in board.commands if item[0] != "heartbeat"][-1],
                             (3, 1))

    def test_unload_requires_exact_success_terminal_and_fails_closed_on_stale_or_ambiguous(self):
        from host.tool_mapping import ToolMapping
        with tempfile.TemporaryDirectory() as directory:
            mapping = ToolMapping(os.path.join(directory, "tools.json"), 4)
            mapping.set(0, 1)
            board = LaneBoard()
            path = os.path.join(directory, "session.json")
            session = _session(mapping, board, path)
            initial = session.request_tool(0)
            self.assertTrue(complete_load_and_follow(mapping, board, path, initial)["ready"])
            session = _session(mapping, board, path)
            pending = session.release_tool()
            self.assertEqual(pending["fault"], "unload_pending")
            identity = Identity(1, SESSION, pending["pending_unload"]["request"], 2, 1)
            reloaded = _session(mapping, board, path)
            wrong = Event(INSTANCE, Identity(1, SESSION, identity.request + 1, 2, 1),
                          1, 10, 4, 0, 0, 0)
            cancelled = Event(INSTANCE, identity, 2, 11, 5, 0, 0, 0)
            failed = Event(INSTANCE, identity, 3, 12, 6, 1, 0, 0)
            success = Event(INSTANCE, identity, 4, 13, 4, 0, 0, 0)
            self.assertFalse(reloaded.reconcile_unload_terminal(wrong, 1))
            self.assertFalse(reloaded.reconcile_unload_terminal(cancelled, 1))
            self.assertFalse(reloaded.reconcile_unload_terminal(failed, 1))
            self.assertFalse(reloaded.reconcile_unload_terminal(success, 0))
            mapping.set(0, 2)
            board.zero_steps()
            blocked = reloaded.request_tool(0)
            self.assertEqual(blocked["fault"], "unload_pending")
            self.assertEqual(board.steps, [0, 0, 0, 0])
            reloaded.requests.disconnect()
            self.assertFalse(reloaded.reconcile_unload_terminal(success, 1))
            self.assertIn("pending_unload", reloaded.management_status())

    def test_host_commands_route_by_assignment_and_status_hides_unobserved_sensors(self):
        with tempfile.TemporaryDirectory() as directory:
            mapping_path = os.path.join(directory, "tools.json")
            session_path = os.path.join(directory, "session.json")
            left = run_motion(session_path, mapping_path)
            status = run_status(session_path)
            self.assertEqual(status, left)
            self.assertRaises(ValueError, PrintSession.load(session_path).release_tool)
            release_path = os.path.join(directory, "release.json")
            run_release(release_path, mapping_path)
            run_rejected_retarget(os.path.join(directory, "blocked.json"),
                                  os.path.join(directory, "blocked-tools.json"))
            self.assertEqual(observation_from_capture({
                "encoder": {"configured": False, "valid": False, "present": False},
                "load": {"configured": True, "valid": False, "present": False},
            }), {"encoder": None, "load": None})


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("motion", "status"):
        action, session_path, mapping_path = sys.argv[1], None, None
        args = sys.argv[2:]
        while args:
            if args[0] == "--session":
                session_path = args[1]
                args = args[2:]
            elif args[0] == "--mapping":
                mapping_path = args[1]
                args = args[2:]
            else:
                raise SystemExit("unknown argument %s" % args[0])
        if action == "motion":
            run_motion(session_path, mapping_path)
        else:
            run_status(session_path)
    else:
        unittest.main()
