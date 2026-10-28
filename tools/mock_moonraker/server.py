"""A mock Moonraker: a WebSocket JSON-RPC 2.0 server that replays a fixture.

The display and its tests need OpenAMS status without a printer. This module
serves Klipper's object API over Moonraker's `/websocket`:
`printer.objects.query`/`subscribe` answer from the fixture's current state,
`subscribe` registers the client for `notify_status_update`, the C4 component
is reachable as `server.openams_spoolman.status`, `.edit` and `.confirm`, and
pushes `notify_openams_spoolman_status`, and every request is recorded so a
test can assert on the commands a client sent.

Fixture events are applied at `t / speed` seconds after the first client
connects (`speed=0` applies everything at once; `speed=None` disables the
automatic timeline so a caller drives time itself with `apply_due()`).

Beyond the replay it holds the writable state a UI needs to be developed
against: an in-memory Spoolman (`spoolman.Spoolman`) that
`server.spoolman.proxy` serves and `server.openams_spoolman.edit` writes, and
`--gcode-mode`, which decides what `printer.gcode.script` answers (`ok`,
`refuse`, `simulate`).
"""

import asyncio
import copy
import inspect
import json
import time

from aiohttp import WSMsgType, web

from .spoolman import Spoolman, api, edit, ops
from .taglib import Taglib

# The keys of the status object's ``spool`` (C4 §8); the summary also carries
# working values the component keeps to itself.
SPOOL_STATUS_KEYS = ("material", "color_hex", "vendor", "remaining_g",
                     "remaining_pct", "initial_g", "flow_k")

JSONRPC_VERSION = "2.0"

# A plausible idle print_stats; nothing in the fixture format carries one, but
# a client subscribing to it must get an object rather than an error.
PRINT_STATS = {"state": "standby", "filename": None, "print_duration": 0.0}

PROXY_MISSING = -32000
UNKNOWN_OBJECT = -32000

# What `printer.gcode.script` answers, chosen by `gcode_mode` (the CLI's
# `--gcode-mode`).
GCODE_MODES = ("ok", "refuse", "simulate")
# The macro's own refusal (`oams_macros.cfg`, `_TX`), as Moonraker reports a
# failed script: the message is the error's text.
# A refusal the macros raise before any motion (oams_macros.cfg, _OAMS_HEAT):
# a configuration whose floor is above its ceiling. The macros heat a cold
# extruder themselves, so a cold one is no longer refused.
REFUSAL_MESSAGE = ("OpenAMS cannot heat extruder: minimum_extrude_temperature "
                   "260.0 is above the temperature ceiling 240.0. Check "
                   "minimum_extrude_temperature, max_temp and "
                   "max_operation_temperature")
# A simulated load or unload runs for this long, a step per plan name
# (C2 §5.3's plans).
GCODE_SECONDS = 3.0


class RpcError(Exception):
    """A JSON-RPC error response."""

    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code
        self.message = message


def gcode_params(script):
    """A script's `KEY=VALUE` words, uppercase keys as Klipper reads them."""
    words = script.split()
    params = {}
    for word in words[1:]:
        if "=" in word:
            key, _, value = word.partition("=")
            params[key.upper()] = value
    return params


def diff_status(old, new):
    """The changed top-level fields of `new` against `old` (Klipper's shape).

    A field absent from `new` is reported as `None`, as Klipper does when an
    object drops a key.
    """
    if old is None:
        return dict(new)
    diff = {}
    for key, value in new.items():
        if key not in old or old[key] != value:
            diff[key] = value
    for key in old:
        if key not in new:
            diff[key] = None
    return diff


class MockMoonraker(object):
    """Replays a `fixtures.Fixture` over `/websocket`."""

    def __init__(self, fixture, host="127.0.0.1", port=7125, speed=1.0,
                 loop=False, record_path=None, gcode_mode="ok", taglib=None):
        self.fixture = fixture
        self.host = host
        self.port = port
        self.speed = speed
        self.loop = loop
        self.record_path = record_path
        if gcode_mode not in GCODE_MODES:
            raise ValueError("gcode_mode must be one of %s, not %r"
                             % (", ".join(GCODE_MODES), gcode_mode))
        self.gcode_mode = gcode_mode

        self.c2_status = None
        self.units = {}
        self.component_status = None
        self.print_stats = dict(PRINT_STATS)
        self.spoolman = self._seed_spoolman()

        self.eventtime = 0.0
        self.calls = []
        self.record_file = None

        self._events = list(fixture.events)
        self._next = 0
        self._t0 = None
        self._loop = None
        self._runner = None
        self._timeline_task = None
        self._gcode_tasks = set()
        self._subscribers = {}
        self._object_names = self._collect_object_names()

        self.app = web.Application()
        self.app.router.add_get("/websocket", self._websocket)
        # The tag library service (`--taglib <path>`): plain HTTP routes.
        self.taglib = Taglib(taglib) if taglib else None
        if self.taglib is not None:
            self.taglib.index()
            self.taglib.add_routes(self.app.router)

    # -- public API ------------------------------------------------------

    @property
    def url(self):
        return "http://%s:%d/websocket" % (self.host, self.port)

    async def start(self):
        """Start listening. With `port=0` the bound port lands in `self.port`."""
        self._loop = asyncio.get_running_loop()
        if self.record_path and self.record_file is None:
            self.record_file = open(self.record_path, "a")
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        addresses = getattr(self._runner, "addresses", None) or []
        if addresses:
            self.port = addresses[0][1]
        return self

    async def stop(self):
        """Stop the timeline, the HTTP server and the recording file."""
        if self._timeline_task is not None:
            self._timeline_task.cancel()
            try:
                await self._timeline_task
            except asyncio.CancelledError:
                pass
            self._timeline_task = None
        for task in list(self._gcode_tasks):
            task.cancel()
        self._gcode_tasks.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self.record_file is not None:
            self.record_file.close()
            self.record_file = None

    async def serve_forever(self):
        """Run until cancelled (`python3 -m mock_moonraker`'s entry point)."""
        await self.start()
        await asyncio.Event().wait()

    def apply_due(self, upto=None):
        """Apply every pending event with `t <= upto` (`None`: all of them)."""
        applied = 0
        while self._next < len(self._events):
            event = self._events[self._next]
            if upto is not None and event.t > upto:
                break
            self._next += 1
            self._apply_event(event)
            applied += 1
        return applied

    # -- fixture bookkeeping --------------------------------------------

    def _seed_spoolman(self):
        """The Spoolman the mock serves, loaded with the fixture's data.

        The collections the fixture answers over the proxy come first, then
        one spool per bay the fixture's initial component status links, so a
        linked bay already has something to edit.
        """
        spoolman = Spoolman()
        spoolman.seed_responses(self.fixture.spoolman_responses)
        component = self._initial_payload("component_status")
        units = {}
        for event in self.fixture.events:
            if event.kind == "openams_ui" and event.unit not in units:
                units[event.unit] = event.payload
        spoolman.seed_bays(component or {}, units)
        return spoolman

    def _initial_payload(self, kind):
        events = self.fixture.events_of_kind(kind)
        return events[0].payload if events else None

    def _collect_object_names(self):
        names = []
        if self.fixture.events_of_kind("c2_status"):
            names.append("oams_manager")
        for unit in self.fixture.units:
            names.append("openams_ui %s" % unit)
        names.append("print_stats")
        return names

    def _apply_event(self, event):
        if event.kind == "c2_status":
            diff = diff_status(self.c2_status, event.payload)
            self.c2_status = event.payload
            self.eventtime = event.t
            # The diff holds `oams_manager`'s own fields, so it must be nested
            # under the object name the subscribers asked for.
            if diff:
                self._push_status_update({"oams_manager": diff}, event.t)
        elif event.kind == "openams_ui":
            old = self.units.get(event.unit)
            # A copy: the mock writes here too (a lane playback, a bay link),
            # and a `--loop` pass must start from the recorded state.
            self.units[event.unit] = copy.deepcopy(event.payload)
            diff = diff_status(old, self.units[event.unit])
            self.eventtime = event.t
            if diff:
                self._push_status_update({"openams_ui %s" % event.unit: diff}, event.t)
        elif event.kind == "component_status":
            # A copy, like the unit status: the mock edits a bay entry.
            self.component_status = copy.deepcopy(event.payload)
            self.eventtime = event.t
            self._push({"jsonrpc": JSONRPC_VERSION,
                        "method": "notify_openams_spoolman_status",
                        "params": [self.component_status]})
        # action_log / spoolman_call / gcode_script are recorded outputs of a
        # run, not server state: the fixture keeps them, the mock ignores them.

    # -- state access ----------------------------------------------------

    def _status(self, name):
        if name == "oams_manager":
            return dict(self.c2_status) if self.c2_status is not None else {}
        if name == "print_stats":
            return dict(self.print_stats)
        if name.startswith("openams_ui "):
            unit = name[len("openams_ui "):]
            payload = self.units.get(unit)
            return dict(payload) if payload is not None else {}
        raise RpcError(UNKNOWN_OBJECT, "Unknown object: %s" % name)

    def _select(self, status, fields):
        if fields is None:
            return status
        if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
            raise RpcError(-32602, "Object field filters must be arrays of names")
        return {key: status[key] for key in fields if key in status}

    def _status_response(self, objects):
        if not isinstance(objects, dict):
            raise RpcError(-32602, "Expected an 'objects' object")
        status = {}
        for name in objects:
            if name not in self._object_names:
                raise RpcError(UNKNOWN_OBJECT, "Unknown object: %s" % name)
            status[name] = self._select(self._status(name), objects[name])
        return {"eventtime": self.eventtime, "status": status}

    # -- notifications ---------------------------------------------------

    def _push(self, message):
        if self._loop is None or not self._subscribers:
            return
        self._loop.create_task(self._send_all(message))

    def _push_status_update(self, diff, eventtime):
        if not diff or self._loop is None or not self._subscribers:
            return
        self._loop.create_task(self._send_status_update(diff, eventtime))

    async def _send_all(self, message):
        for ws in list(self._subscribers):
            try:
                await ws.send_json(message)
            except (ConnectionResetError, RuntimeError, asyncio.CancelledError):
                pass

    async def _send_status_update(self, diff, eventtime):
        for ws, names in list(self._subscribers.items()):
            subset = {key: value for key, value in diff.items() if key in names}
            if not subset:
                continue
            try:
                await ws.send_json({"jsonrpc": JSONRPC_VERSION,
                                    "method": "notify_status_update",
                                    "params": [subset, eventtime]})
            except (ConnectionResetError, RuntimeError, asyncio.CancelledError):
                pass

    # -- recording -------------------------------------------------------

    def _record(self, method, params):
        call = {"t": time.monotonic(), "method": method, "params": params}
        self.calls.append(call)
        if self.record_file is not None:
            self.record_file.write(json.dumps(call, sort_keys=True) + "\n")
            self.record_file.flush()

    # -- WebSocket -------------------------------------------------------

    async def _websocket(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._subscribers[ws] = set()
        if self._t0 is None:
            self._t0 = time.monotonic()
            self.apply_due(0.0)
            if self.speed == 0:
                self.apply_due()
            elif self.speed is not None:
                self._timeline_task = asyncio.ensure_future(self._run_timeline())
        try:
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    await self._dispatch(ws, message.data)
                elif message.type == WSMsgType.ERROR:
                    break
        finally:
            self._subscribers.pop(ws, None)
        return ws

    async def _run_timeline(self):
        while True:
            while self._next < len(self._events):
                event = self._events[self._next]
                delay = event.t / self.speed - (time.monotonic() - self._t0)
                if delay > 0:
                    await asyncio.sleep(delay)
                self._next += 1
                self._apply_event(event)
            if not self.loop:
                return
            # A loop restarts from an empty state so the first event of the new
            # pass pushes the full status again instead of an empty diff.
            self.c2_status = None
            self.units = {}
            self.component_status = None
            self._next = 0
            self._t0 = time.monotonic()

    async def _dispatch(self, ws, raw):
        try:
            message = json.loads(raw)
        except ValueError:
            await self._reply(ws, None, RpcError(-32700, "Parse error"))
            return
        if not isinstance(message, dict):
            await self._reply(ws, None, RpcError(-32600, "Invalid Request"))
            return
        ident = message.get("id")
        method = message.get("method")
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(method, str):
            await self._reply(ws, ident, RpcError(-32600, "Invalid Request: no method"))
            return
        self._record(method, params)
        try:
            result = self._handle(ws, method, params)
            if inspect.isawaitable(result):
                result = await result
        except RpcError as exc:
            await self._reply(ws, ident, exc)
            return
        if result is not None:
            await self._reply(ws, ident, None, result=result)

    async def _reply(self, ws, ident, error=None, result=None):
        if error is not None:
            message = {"jsonrpc": JSONRPC_VERSION, "id": ident,
                       "error": {"code": error.code, "message": error.message}}
        else:
            message = {"jsonrpc": JSONRPC_VERSION, "id": ident, "result": result}
        try:
            await ws.send_json(message)
        except (ConnectionResetError, RuntimeError):
            pass

    def _handle(self, ws, method, params):
        if not isinstance(params, dict):
            raise RpcError(-32602, "Expected an object of parameters")

        if method == "server.info":
            return {"klippy_connected": True, "klippy_state": "ready",
                    "components": ["spoolman", "openams_spoolman"]}
        if method == "printer.info":
            return {"state": "ready"}
        if method == "printer.objects.list":
            return {"objects": list(self._object_names)}
        if method == "printer.objects.query":
            return self._status_response(params.get("objects"))
        if method == "printer.objects.subscribe":
            response = self._status_response(params.get("objects"))
            self._subscribers[ws].update(params["objects"].keys())
            return response
        if method == "printer.gcode.script":
            return self._gcode_script(params.get("script"))
        if method == "server.spoolman.proxy":
            return self._proxy(params)
        if method == "server.openams_spoolman.status":
            return self.component_status if self.component_status is not None else {}
        if method == "server.openams_spoolman.confirm":
            return "ok"
        if method == "server.openams_spoolman.edit":
            return self._edit(params)
        if method == "server.files.metadata":
            filename = params.get("filename")
            if filename in self.fixture.file_metadata:
                return dict(self.fixture.file_metadata[filename], filename=filename)
            raise RpcError(-32000, "Metadata not available for %s" % filename)
        raise RpcError(-32601, "Method not found: %s" % method)

    def _proxy(self, params):
        request_method = params.get("request_method")
        path = params.get("path")
        if not isinstance(request_method, str) or not isinstance(path, str):
            raise RpcError(-32602, "The proxy needs 'request_method' and 'path'")
        served = self._spoolman_view(request_method, path, params.get("query"))
        if served is not None:
            return served
        key = "%s %s" % (request_method.upper(), path)
        if key in self.fixture.spoolman_responses:
            return self.fixture.spoolman_responses[key]
        raise RpcError(PROXY_MISSING, "The fixture has no response for %s" % key)

    def _spoolman_view(self, request_method, path, query=None):
        """The in-memory Spoolman's answer, or None when it serves no such
        request (the fixture's `spoolman_responses` answers then, as before)."""
        if request_method.upper() != "GET":
            return None
        parts = [part for part in path.split("/") if part]
        if parts[:1] != ["v1"]:
            return None
        query = query if isinstance(query, dict) else {}
        if parts[1:] == ["spool"]:
            return self.spoolman.spools_view()
        if parts[1:] == ["vendor"]:
            return self.spoolman.vendors_view(query.get("name"))
        if len(parts) == 3 and parts[1] == "spool" and parts[2].isdigit():
            try:
                return self.spoolman.spool_view(parts[2])
            except api.SpoolmanError:
                return None
        return None

    # -- G-code ----------------------------------------------------------

    def _gcode_script(self, script):
        if not isinstance(script, str) or not script.strip():
            raise RpcError(-32602, "printer.gcode.script needs a 'script' string")
        if self.gcode_mode == "ok":
            return "ok"
        command = script.split()[0].upper()
        if self.gcode_mode == "refuse":
            if command in ("OPENAMS_LOAD", "OPENAMS_UNLOAD") \
                    or (command[:1] == "T" and command[1:].isdigit()):
                raise RpcError(400, REFUSAL_MESSAGE)
            return "ok"
        if command == "OPENAMS_LOAD":
            self._play_load(script)
        elif command == "OPENAMS_UNLOAD":
            self._play_unload()
        return "ok"

    def _play_load(self, script):
        """A load of `GROUP=<g> SLOT=<n>` on that slot's unit lane (C2 §5.3).

        A load into an empty toolhead is the `feed` plan; a switch is
        `cut, retract, feed`.
        """
        params = gcode_params(script)
        target = self._slot_unit(params.get("SLOT"))
        if target is None:
            return
        unit, bay = target
        lane = (self.units.get(unit) or {}).get("lane") or {}
        key = "%s-%d" % (unit, bay)
        group = params.get("GROUP")
        plan = ["feed"] if lane.get("op") in (None, "UNLOADED") \
            else ["cut", "retract", "feed"]
        frames = [{"op": "LOADING", "group": group, "bay": key, "from": lane.get("bay"),
                   "to": key, "ms": False, "stage": [list(plan), index, None, None, {}]}
                  for index in range(len(plan))]
        frames.append({"op": "LOADED", "group": group, "bay": key, "from": None,
                       "to": None, "ms": True, "stage": None})
        self._play_lane(unit, frames)

    def _play_unload(self):
        """An unload (C2 §5.3's `cut, retract` plan) on the loaded unit's lane."""
        for unit, status in self.units.items():
            lane = status.get("lane") or {}
            if lane.get("op") in ("LOADED", "LOADING"):
                key = lane.get("bay")
                plan = ["cut", "retract"]
                frames = [{"op": "UNLOADING", "group": lane.get("group"), "bay": key,
                           "from": key, "to": None, "ms": False,
                           "stage": [list(plan), index, None, None, {}]}
                          for index in range(len(plan))]
                frames.append({"op": "UNLOADED", "group": None, "bay": None,
                               "from": None, "to": None, "ms": True, "stage": None})
                self._play_lane(unit, frames)
                return

    def _slot_unit(self, slot):
        """The (unit, bay id) a `SLOT=` names, or None when no bay has it."""
        if slot is None or not str(slot).isdigit():
            return None
        for unit, status in self.units.items():
            for bay in status.get("bays") or []:
                if str(bay.get("id")) == str(int(slot)):
                    return unit, int(bay.get("id"))
        return None

    def _play_lane(self, unit, frames):
        if self._loop is None:
            return
        step = GCODE_SECONDS / len(frames)
        task = self._loop.create_task(self._run_lane(unit, frames, step))
        self._gcode_tasks.add(task)
        task.add_done_callback(self._gcode_tasks.discard)

    async def _run_lane(self, unit, frames, step):
        for index, frame in enumerate(frames):
            if index:
                self.eventtime += step
                await asyncio.sleep(step)
            self._set_lane(unit, frame)
            self._push_lane(unit)

    def _set_lane(self, unit, frame):
        status = self.units.get(unit)
        if status is None:
            return
        lane = status.setdefault("lane", {})
        lane.update(frame)
        if "runout" not in lane:
            lane["runout"] = "idle"

    def _push_lane(self, unit):
        lane = (self.units.get(unit) or {}).get("lane")
        if lane is not None:
            # A copy: the next frame edits this same dict before the send runs.
            self._push_status_update(
                {"openams_ui %s" % unit: {"lane": copy.deepcopy(lane)}},
                self.eventtime)

    # -- the spool edit (C4 §9) ------------------------------------------

    async def _edit(self, params):
        try:
            unit, bay, fields = edit.parse(params)
            entry = self._edit_bay(unit, bay)
            before = copy.deepcopy(self.units.get(unit))
            spool, created = await edit.apply(
                self.spoolman, self._run_gcode, unit, bay, fields,
                self._bay_spool_id(unit, bay, entry))
        except edit.EditError as error:
            raise RpcError(error.status, str(error))
        return self._after_edit(unit, bay, entry, spool, created, before)

    def _edit_bay(self, unit, bay):
        """The host's entry for a bay, or a 404/409 naming what is unknown."""
        status = self.units.get(unit)
        if status is None:
            raise edit.EditError("unknown unit '%s'" % unit, 404)
        for entry in status.get("bays") or []:
            if entry.get("id") != bay:
                continue
            if entry.get("s") == "e" and not (entry.get("sp") or {}).get("id"):
                raise edit.EditError("no spool in %s Spool %d" % (unit, bay + 1),
                                     409)
            return entry
        raise edit.EditError("%s has no Spool %d" % (unit, bay + 1), 404)

    def _bay_spool_id(self, unit, bay, entry):
        """The spool the bay holds: the host's link, else the component's."""
        linked = (entry.get("sp") or {}).get("id")
        if linked is not None:
            return linked
        return ((self.component_status or {}).get("bays") or {}
                ).get("%s-%d" % (unit, bay), {}).get("spool_id")

    def _after_edit(self, unit, bay, entry, spool, created, before):
        key = "%s-%d" % (unit, bay)
        source = "manual" if created else self._link_source(entry)
        bay_status = {"state": "linked", "spool_id": spool["id"],
                      "source": source, "pending_confirmation": False,
                      "error": None,
                      "spool": {key: value for key, value in
                                ops.spool_summary(spool).items()
                                if key in SPOOL_STATUS_KEYS}}
        status = self.component_status
        if status is None:
            status = self.component_status = {"schema_version": 1,
                                              "spoolman_online": True,
                                              "edit": True, "stop": True,
                                              "queued_writes": 0,
                                              "bays": {}}
        status.setdefault("bays", {})[key] = bay_status
        self._push({"jsonrpc": JSONRPC_VERSION,
                    "method": "notify_openams_spoolman_status",
                    "params": [copy.deepcopy(status)]})
        after = self.units.get(unit)
        if after is not None and before is not None:
            self._push_status_update(
                {"openams_ui %s" % unit: diff_status(before, copy.deepcopy(after))},
                self.eventtime)
        return {"spool_id": spool["id"], "created": created, "bay": bay_status}

    @staticmethod
    def _link_source(entry):
        return {"t": "tag", "m": "manual"}.get(
            (entry.get("sp") or {}).get("src"), "manual")

    # -- the host the component drives ----------------------------------

    async def _run_gcode(self, script):
        """The host commands the component sends (C4 §7.1), applied to the
        mock's own `openams_ui` status."""
        words = script.split()
        if not words:
            return
        command = words[0].upper()
        params = gcode_params(script)
        if command == "OAMSM_SET_BAY_SPOOL":
            self._set_bay_link(params.get("OAMS"), int(params["BAY"]),
                               int(params["SPOOL"]), params.get("SOURCE", "manual"))
        elif command == "OAMSM_CLEAR_BAY_SPOOL":
            self._set_bay_link(params.get("OAMS"), int(params["BAY"]), None, "none")

    def _set_bay_link(self, unit, bay, spool_id, source):
        status = self.units.get(unit)
        if status is None:
            return
        for entry in status.get("bays") or []:
            if entry.get("id") == bay:
                entry["sp"] = None if spool_id is None else {
                    "id": spool_id,
                    "src": {"tag": "t", "manual": "m"}.get(source, "m")}
                return
