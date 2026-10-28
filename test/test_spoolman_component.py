#!/usr/bin/env python3
"""Unit tests for the openams_spoolman Moonraker component (C4 §1, §6-§8).

A minimal, self-contained stand-in for the pieces of a real Moonraker this
component touches (config, server, the klippy_apis and database components,
WebRequest) so the tests run without Moonraker installed; the real thing is
exercised end to end by the S3 gate.
"""

import asyncio
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "moonraker"))

from openams_spoolman import api as A  # noqa: E402
from openams_spoolman.component import OpenAmsSpoolman  # noqa: E402

_MISSING = object()


class ServerErrorStub(Exception):
    def __init__(self, message, status_code=400):
        Exception.__init__(self, message)
        self.status_code = status_code


class StubTimer(object):
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class StubEventLoop(object):
    def __init__(self):
        self.timers = []

    def create_task(self, coro):
        return asyncio.ensure_future(coro)

    def delay_callback(self, delay, callback, *args):
        """Moonraker's ``delay_callback``; recorded, fired by the test."""
        timer = StubTimer(delay, lambda: callback(*args))
        self.timers.append(timer)
        return timer


class StubConfig(object):
    """Stands in for a Moonraker ``ConfigHelper`` scoped to one section."""

    def __init__(self, server, options=None):
        self._server = server
        self._options = dict(options or {})

    def get_server(self):
        return self._server

    def get(self, key, default=None):
        return self._options.get(key, default)

    def getboolean(self, key, default=None):
        value = self._options.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        return value

    def has_section(self, name):
        return False   # tests inject ``api=`` directly; no [spoolman] lookup

    def getsection(self, name):
        raise AssertionError("not needed: tests inject api= directly")

    def error(self, message):
        return ServerErrorStub(message)


class StubServer(object):
    def __init__(self):
        self.components = {}
        self.event_handlers = {}
        self.endpoints = []
        self.notifications = []
        self.sent_events = []
        self.remote_methods = {}
        self._eventloop = StubEventLoop()

    def get_event_loop(self):
        return self._eventloop

    def lookup_component(self, name, default=_MISSING):
        if name in self.components:
            return self.components[name]
        if default is _MISSING:
            raise ServerErrorStub("Component (%s) not found" % name, 500)
        return default

    def register_notification(self, event_name, notify_name=None):
        self.notifications.append((event_name, notify_name))

    def register_event_handler(self, event, callback):
        self.event_handlers.setdefault(event, []).append(callback)

    def register_endpoint(self, endpoint, request_types, callback, **kwargs):
        self.endpoints.append((endpoint, request_types, callback))

    def register_remote_method(self, method_name, callback):
        # A test may build a second component on the same server; the
        # newest registration wins.
        self.remote_methods[method_name] = callback

    def send_event(self, event, *args):
        self.sent_events.append((event, args))

    def error(self, message, status_code=400):
        return ServerErrorStub(message, status_code)


class StubWebRequest(object):
    def __init__(self, args):
        self.args = args

    def _get(self, key, default, caster):
        if key not in self.args:
            if default is _MISSING:
                raise ServerErrorStub("No data for argument: %s" % key)
            return default
        return caster(self.args[key])

    def get_str(self, key, default=_MISSING):
        return self._get(key, default, str)

    def get_float(self, key, default=_MISSING):
        return self._get(key, default, float)


class StubKlippyApis(object):
    def __init__(self, subscribe_result=None, fail_subscribe=False):
        self.gcode = []
        self.requests = []
        self.answer = "stopping"
        self.callback = None
        self.subscribe_result = subscribe_result or {}
        self.fail_subscribe = fail_subscribe
        self.query_result = None

    async def subscribe_objects(self, objects, callback, default=None):
        self.callback = callback
        if self.fail_subscribe:
            raise RuntimeError("no such printer object: oams_manager")
        return self.subscribe_result

    async def query_objects(self, objects, default=None):
        if self.query_result is None:
            return {}
        return self.query_result

    async def run_gcode(self, script):
        self.gcode.append(script)
        return "ok"

    async def _send_klippy_request(self, method, params, default=None):
        self.requests.append((method, params))
        return {"result": self.answer}


def _done(value):
    future = asyncio.get_event_loop().create_future()
    future.set_result(value)
    return future


class StubNamespace(object):
    """A Moonraker ``NamespaceWrapper`` stand-in over a shared plain dict, so
    two components can be pointed at the "same" persisted database."""

    def __init__(self, store):
        self._store = store

    async def get(self, key, default=None):
        return self._store.get(key, default)

    def insert(self, key, value):
        self._store[key] = value

    def keys(self):
        return _done(list(self._store))

    def delete(self, key):
        self._store.pop(key, None)


class StubDatabase(object):
    def __init__(self, store=None):
        self._store = {} if store is None else store
        self.namespaces = {}
        self.registered = []

    def register_local_namespace(self, namespace, forbidden=False,
                                 parse_keys=False):
        self.registered.append(namespace)
        if namespace == "lane_data":
            return StubNamespace(self.namespaces.setdefault(namespace, {}))
        return StubNamespace(self._store)


class StubSpoolman(object):
    def __init__(self):
        self.active_calls = []

    def set_active_spool(self, spool_id):
        self.active_calls.append(spool_id)


BAMBU = {"manufacturer": "Bambu", "type": "PLA", "detailed": "PLA Basic",
         "color_argb": 0xFFFF0000, "weight_g": 1000, "diameter_mm": 1.75,
         "drying_temp_c": 55, "drying_time_h": 8, "bed_temp_c": 60,
         "hotend_max_c": 230, "hotend_min_c": 190, "nozzle_diameter": 0.4,
         "spool_width_mm": 66, "length_m": 330,
         "tray_uid": "a" * 32, "production": "240101"}


def bambu_tag(uid="0a0b0c0d", read_at=1.0):
    return {"uid": uid, "format": "bambu", "read_at": read_at,
            "spoolman_id": None, "bambu": dict(BAMBU)}


def bay(index=0, ready=True, tag=None, spool_id=None, link_source="none"):
    return {"index": index, "ready": ready, "loaded": False, "tag": tag,
            "spool_id": spool_id, "link_source": link_source}


def oams_status(bays, lane_op="UNLOADED", lane_unit=None, runout="idle",
               auto_create=True):
    return {"ready": True,
            "settings": {"auto_create_spoolman_spools": auto_create,
                         "apply_pa_on_load": False},
            "topology": {"oams": {"oams1": {"idx": 1, "lane": "fps1"}}},
            "lanes_by_fps": {"fps1": {"op": lane_op, "unit": lane_unit,
                                      "runout": runout, "extruder": "extruder"}},
            "devices": {"oams1": {"bays": bays}}}


class Harness(object):
    """One ``OpenAmsSpoolman`` over stub Moonraker pieces the test controls."""

    def __init__(self, api=None, with_spoolman=True, store=None,
                subscribe_result=None, options=None):
        self.api = api if api is not None else A.FakeSpoolman()
        self.server = StubServer()
        self.klippy_apis = StubKlippyApis(subscribe_result=subscribe_result)
        self.server.components["klippy_apis"] = self.klippy_apis
        self.server.components["database"] = StubDatabase(store)
        if with_spoolman:
            self.spoolman = StubSpoolman()
            self.server.components["spoolman"] = self.spoolman
        self.config = StubConfig(self.server, options)
        self.component = OpenAmsSpoolman(self.config, api=self.api)

    async def init(self):
        await self.component.component_init()
        return self

    async def ready(self, bays):
        self.klippy_apis.subscribe_result = {
            "oams_manager": oams_status(bays),
            "toolhead": {"extruder": "extruder"}}
        await self.component._handle_klippy_ready()
        await self.flush()
        return self

    def push(self, diff):
        self.component._handle_status_update(diff, 0.0)

    async def flush(self):
        if self.component._pass_task is not None:
            await self.component._pass_task

    def bay(self, key="oams1-0"):
        return self.component.linker.status()["bays"][key]


class RegistrationTest(unittest.IsolatedAsyncioTestCase):
    # Built inside a running loop, as Moonraker builds components: on Python
    # 3.8 and 3.9, asyncio.Lock() needs a current event loop.
    async def test_registers_endpoints_notification_and_klippy_ready(self):
        h = Harness()
        paths = [entry[0] for entry in h.server.endpoints]
        self.assertIn("/server/openams_spoolman/status", paths)
        self.assertIn("/server/openams_spoolman/confirm", paths)
        self.assertIn(("openams_spoolman:status_changed",
                       "openams_spoolman_status"), h.server.notifications)
        self.assertIn("server:klippy_ready", h.server.event_handlers)


class StopRouteTest(unittest.IsolatedAsyncioTestCase):
    def endpoint(self, h, path):
        for entry in h.server.endpoints:
            if entry[0] == path:
                return entry
        self.fail("no endpoint " + path)

    async def test_the_route_is_a_post_and_the_status_advertises_it(self):
        h = Harness()
        await h.init()
        path, methods, _ = self.endpoint(h, "/server/openams/stop")[:3]
        self.assertEqual(methods, ["POST"])
        self.assertIs(h.component.linker.status()["stop"], True)

    async def test_it_forwards_to_the_klipper_webhook_and_returns_its_answer(self):
        h = Harness()
        await h.init()
        handler = self.endpoint(h, "/server/openams/stop")[2]
        self.assertEqual(await handler(StubWebRequest({})),
                         {"result": "stopping"})
        self.assertEqual(await handler(StubWebRequest({"lane": "fps2"})),
                         {"result": "stopping"})
        self.assertEqual(h.klippy_apis.requests,
                         [("openams/stop", {}),
                          ("openams/stop", {"lane": "fps2"})])
        h.klippy_apis.answer = "nothing running"
        self.assertEqual(await handler(StubWebRequest({})),
                         {"result": "nothing running"})


class SubscriptionTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_bambu_tag_links_and_notifies(self):
        h = await Harness().init()
        await h.ready([bay(tag=bambu_tag())])
        self.assertTrue(any("OAMSM_SET_BAY_SPOOL" in g
                            for g in h.klippy_apis.gcode))
        self.assertEqual(len(h.server.sent_events), 1)
        event, args = h.server.sent_events[0]
        self.assertEqual(event, "openams_spoolman:status_changed")
        self.assertEqual(args[0]["bays"]["oams1-0"]["state"],
                         "pending_confirmation")


class DiffMergeTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_lanes_by_fps_only_diff_keeps_devices(self):
        h = await Harness().init()
        await h.ready([bay(tag=bambu_tag())])
        spool_id = h.bay()["spool_id"]

        # Only lanes_by_fps changes; devices is not repeated in the diff.
        h.push({"oams_manager": {"lanes_by_fps":
                                 {"fps1": {"op": "LOADING", "unit": None,
                                          "runout": "idle",
                                          "extruder": "extruder"}}}})
        await h.flush()
        devices = h.component._status["oams_manager"]["devices"]
        # The diff carried no "devices" key at all; the merge must have kept
        # the bay (and its tag) from the earlier full status, not dropped it.
        self.assertEqual(devices["oams1"]["bays"][0]["tag"]["uid"],
                         bambu_tag()["uid"])
        self.assertEqual(
            h.component._status["oams_manager"]["lanes_by_fps"]["fps1"]["op"],
            "LOADING")
        self.assertEqual(h.bay()["spool_id"], spool_id)


class RestartRepushTest(unittest.IsolatedAsyncioTestCase):
    """C4 §7.4: after a Klipper restart every bay's filament is pushed again,
    even when the unit came online after the status snapshot and Moonraker
    swallowed the diff that flipped ``ready`` (a second subscription's diff
    reaches no callback)."""

    def _filament_pushes(self, h):
        return [g for g in h.klippy_apis.gcode if "SET_BAY_FILAMENT" in g]

    async def test_an_unready_snapshot_is_reread_until_ready(self):
        api = A.FakeSpoolman()
        h = await Harness(api=api).init()
        vendor = await api.request("POST", "/v1/vendor", body={"name": "T"})
        filament = await api.request(
            "POST", "/v1/filament",
            body={"name": "T", "vendor_id": vendor["id"], "material": "PLA",
                  "settings_extruder_temp": 210})
        spool = await api.request(
            "POST", "/v1/spool", body={"filament_id": filament["id"],
                                       "initial_weight": 1000,
                                       "remaining_weight": 1000})
        linked = bay(spool_id=spool["id"], link_source="manual", tag=None)
        unready = oams_status([linked])
        unready["ready"] = False
        h.klippy_apis.subscribe_result = {
            "oams_manager": unready, "toolhead": {"extruder": "extruder"}}
        await h.component._handle_klippy_ready()
        await h.flush()
        self.assertEqual(self._filament_pushes(h), [])
        # Klipper is ready now, but no diff reached the callback.
        h.klippy_apis.query_result = {"oams_manager": oams_status([linked])}
        self.assertTrue(h.server.get_event_loop().timers)
        h.server.get_event_loop().timers[-1].callback()
        await h.flush()
        self.assertEqual(len(self._filament_pushes(h)), 1)


class NoOverlapTest(unittest.IsolatedAsyncioTestCase):
    async def test_two_quick_updates_the_pass_sees_the_latest(self):
        h = await Harness().init()
        h.klippy_apis.subscribe_result = {
            "oams_manager": oams_status([bay()]),
            "toolhead": {"extruder": "extruder"}}
        await h.component._handle_klippy_ready()
        await h.flush()   # settle the initial (unlinked) pass

        h.push({"oams_manager": {"devices":
                                 {"oams1": {"bays": [bay(ready=False)]}}}})
        task_a = h.component._pass_task
        h.push({"oams_manager":
                {"devices": {"oams1": {"bays": [
                    bay(spool_id=77, link_source="manual")]}}}})
        task_b = h.component._pass_task
        self.assertIs(task_a, task_b)          # one pass, not two overlapping
        await task_b
        self.assertEqual(h.bay()["spool_id"], 77)
        self.assertEqual(h.bay()["source"], "manual")


class ConfirmTest(unittest.IsolatedAsyncioTestCase):
    async def test_confirm_returns_the_bay_status_and_409_for_a_wrong_bay(self):
        h = await Harness().init()
        await h.ready([bay(tag=bambu_tag())])
        result = await h.component._handle_confirm(
            StubWebRequest({"bay": "oams1-0", "remaining_g": 500}))
        self.assertEqual(result["state"], "linked")
        self.assertEqual(result["spool"]["remaining_g"], 500)
        self.assertEqual(len(h.server.sent_events), 2)   # link, then confirm

        with self.assertRaises(ServerErrorStub) as ctx:
            await h.component._handle_confirm(
                StubWebRequest({"bay": "oams1-9", "remaining_g": 500}))
        self.assertEqual(ctx.exception.status_code, 409)


class ActiveSpoolTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_active_spool_follows_a_load(self):
        h = await Harness().init()
        await h.ready([bay(tag=bambu_tag())])
        spool_id = h.bay()["spool_id"]
        await h.component._handle_confirm(
            StubWebRequest({"bay": "oams1-0", "remaining_g": 1000}))

        h.klippy_apis.subscribe_result = {
            "oams_manager": oams_status(
                [bay(tag=bambu_tag(), spool_id=spool_id, link_source="tag")],
                lane_op="LOADED", lane_unit=[1, 0]),
            "toolhead": {"extruder": "extruder"}}
        await h.component._handle_klippy_ready()
        await h.flush()

        self.assertEqual(h.spoolman.active_calls[-1], spool_id)


class CachePersistenceTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_cache_is_saved_and_restored_by_a_new_component(self):
        store = {}
        api = A.FakeSpoolman()
        api.online = False
        h1 = await Harness(api=api, store=store).init()
        await h1.ready([bay(tag=bambu_tag())])
        queued = h1.component.linker.status()["queued_writes"]
        self.assertGreaterEqual(queued, 1)

        h2 = await Harness(api=api, store=store).init()
        self.assertEqual(h2.component.linker.status()["queued_writes"], queued)


class OptionsTest(unittest.IsolatedAsyncioTestCase):
    """``tag_vendor`` and ``refresh_interval`` (C4 §5.3, §6): a typo in either
    never stops the component."""

    def _component(self, options):
        return Harness(options=options).component

    async def test_defaults(self):
        component = self._component(None)
        self.assertEqual(component._tag_vendor, "Bambu Lab")
        self.assertEqual(component._refresh_interval, 60.0)

    async def test_tag_vendor_is_read_and_reaches_the_linker(self):
        h = Harness(options={"tag_vendor": " My Filaments "})
        await h.init()
        self.assertEqual(h.component._tag_vendor, "My Filaments")
        self.assertEqual(h.component.linker._tag_vendor, "My Filaments")
        await h.ready([bay(tag=bambu_tag())])
        self.assertEqual(h.bay()["spool"]["vendor"], "My Filaments")

    async def test_a_bad_tag_vendor_falls_back(self):
        with self.assertLogs(level="WARNING"):
            component = self._component({"tag_vendor": "x" * 80})
        self.assertEqual(component._tag_vendor, "Bambu Lab")

    async def test_refresh_interval_values(self):
        for text, seconds in (("120", 120.0), ("10", 10.0), ("0", 0.0),
                              (" 45.5 ", 45.5), ("", 60.0)):
            self.assertEqual(
                self._component({"refresh_interval": text})._refresh_interval,
                seconds, text)
        for text in ("often", "-5", "nan", "inf"):
            with self.assertLogs(level="WARNING"):
                self.assertEqual(self._component(
                    {"refresh_interval": text})._refresh_interval, 60.0, text)
        with self.assertLogs(level="WARNING"):
            self.assertEqual(self._component(
                {"refresh_interval": "3"})._refresh_interval, 10.0)

    async def test_zero_arms_no_timer(self):
        h = Harness(options={"refresh_interval": "0"})
        await h.init()
        self.assertEqual(h.server.get_event_loop().timers, [])


class SpoolmanEditsTest(unittest.IsolatedAsyncioTestCase):
    """A change made in Spoolman's web UI reaches the status, the
    notification, the host and the slicer's lane list (C4 §6, §8)."""

    async def _linked(self, **options):
        h = Harness(options=options or None)
        await h.init()
        await h.ready([bay(tag=bambu_tag())])
        self.spool_id = h.bay()["spool_id"]
        h.push({"oams_manager": {"devices": {"oams1": {"bays": [bay(
            tag=bambu_tag(), spool_id=self.spool_id, link_source="tag")]}}}})
        await h.flush()
        h.server.sent_events.clear()
        h.klippy_apis.gcode.clear()
        return h

    def _timer(self, h):
        timers = [t for t in h.server.get_event_loop().timers
                  if not t.cancelled and t.delay == 60.0]
        return timers[-1]

    async def _fire(self, h):
        self._timer(h).callback()
        for _ in range(5):
            await asyncio.sleep(0)
        await h.flush()

    def _events(self, h):
        return [args[0] for event, args in h.server.sent_events
                if event == "openams_spoolman:status_changed"]

    async def test_a_web_edit_is_notified_and_pushed(self):
        h = await self._linked()
        spool = h.api.spools[0]
        spool["filament"]["vendor"] = {"id": 9, "name": "Polymaker",
                                       "extra": {}}
        spool["filament"]["material"] = "ASA"
        await self._fire(h)
        events = self._events(h)
        self.assertEqual(len(events), 1)
        shown = events[0]["bays"]["oams1-0"]["spool"]
        self.assertEqual((shown["vendor"], shown["material"]),
                         ("Polymaker", "ASA"))
        pushes = [g for g in h.klippy_apis.gcode
                  if g.startswith("OAMSM_SET_BAY_FILAMENT")]
        self.assertEqual(len(pushes), 1)
        self.assertIn('MATERIAL="ASA"', pushes[0])
        self.assertEqual(h.bay()["spool"]["vendor"], "Polymaker")

    async def test_nothing_changed_nothing_is_sent_and_the_timer_re_arms(self):
        h = await self._linked()
        calls = len(h.api.calls)
        await self._fire(h)
        self.assertEqual(self._events(h), [])
        self.assertEqual(h.klippy_apis.gcode, [])
        # One request per refresh, however many bays are linked.
        self.assertEqual([c[:2] for c in h.api.calls[calls:]],
                         [("GET", "/v1/spool")])
        self.assertIsNotNone(h.component._refresh_handle)   # armed again

    async def test_the_lane_list_follows_a_web_edit(self):
        h = Harness()
        await h.init()
        status = oams_status([bay(tag=bambu_tag())])
        status["groups"] = [{"name": "T0", "lane": "fps1", "slots": [0],
                             "next_bay": [1, 0]}]
        h.klippy_apis.subscribe_result = {
            "oams_manager": status, "toolhead": {"extruder": "extruder"}}
        await h.component._handle_klippy_ready()
        await h.flush()
        lanes = h.server.components["database"].namespaces["lane_data"]
        self.assertEqual(lanes["lane1"]["vendor"], "Bambu Lab")
        h.push({"oams_manager": {"devices": {"oams1": {"bays": [bay(
            tag=bambu_tag(), spool_id=h.bay()["spool_id"],
            link_source="tag")]}}}})
        await h.flush()
        h.api.spools[0]["filament"]["vendor"] = {"id": 9, "name": "Polymaker",
                                                 "extra": {}}
        await self._fire(h)
        self.assertEqual(lanes["lane1"]["vendor"], "Polymaker")

    async def test_spoolman_going_away_is_notified(self):
        h = await self._linked()
        h.api.online = False
        await self._fire(h)
        self.assertFalse(self._events(h)[-1]["spoolman_online"])

    async def test_a_failing_refresh_re_arms_the_timer(self):
        h = await self._linked()

        async def fail():
            raise RuntimeError("boom")
        h.component.linker.refresh_linked = fail
        before = len(h.server.get_event_loop().timers)
        with self.assertLogs(level="ERROR"):
            await self._fire(h)
        self.assertEqual(len(h.server.get_event_loop().timers), before + 1)

    async def test_a_restart_with_the_stored_cache_shows_the_vendor(self):
        store = {}
        first = Harness(store=store)
        await first.init()
        await first.ready([bay(tag=bambu_tag())])
        spool_id = first.bay()["spool_id"]
        first.api.spools[0]["filament"]["vendor"] = {
            "id": 9, "name": "Polymaker", "extra": {}}
        first.push({"oams_manager": {"devices": {"oams1": {"bays": [bay(
            tag=bambu_tag(), spool_id=spool_id, link_source="tag")]}}}})
        await first.flush()
        await self._fire(first)
        second = Harness(api=A.FakeSpoolman(), store=store)
        second.api.online = False
        await second.init()
        await second.ready([bay(tag=bambu_tag(read_at=7.0),
                                spool_id=spool_id, link_source="tag")])
        self.assertEqual(second.bay()["spool"]["vendor"], "Polymaker")


class BayFilamentTest(unittest.IsolatedAsyncioTestCase):
    """What the component pushes to the host, and when (C4 §7.4)."""

    async def _spool(self, api, material="PETG", temp=None):
        vendor = await api.request("POST", "/v1/vendor", body={"name": "T"})
        filament = await api.request(
            "POST", "/v1/filament",
            body={"name": "T", "vendor_id": vendor["id"],
                  "material": material, "settings_extruder_temp": temp})
        spool = await api.request("POST", "/v1/spool",
                                  body={"filament_id": filament["id"],
                                        "initial_weight": 1000,
                                        "remaining_weight": 1000})
        return spool["id"]

    async def _linked(self, api, options=None, **kwargs):
        spool_id = await self._spool(api, **kwargs)
        h = Harness(api=api)
        h.config = StubConfig(h.server, options)
        h.component = OpenAmsSpoolman(h.config, api=api)
        await h.component.component_init()
        h.spool_id = spool_id
        await h.ready([bay(spool_id=spool_id, link_source="manual")])
        return h

    def _pushes(self, h):
        return [g for g in h.klippy_apis.gcode
                if g.startswith("OAMSM_SET_BAY_FILAMENT")]

    async def test_a_linked_bay_pushes_the_table_temperature_once(self):
        h = await self._linked(A.FakeSpoolman())
        pushes = self._pushes(h)
        self.assertEqual(len(pushes), 1)
        self.assertIn('MATERIAL="PETG"', pushes[0])
        self.assertIn("TEMP=240 SOURCE=table", pushes[0])
        # An unchanged pass pushes nothing more.
        for _ in range(2):
            h.push({"oams_manager": {"devices": {"oams1": {"bays": [
                bay(spool_id=h.spool_id, link_source="manual")]}}}})
            await h.flush()
        self.assertEqual(len(self._pushes(h)), 1)

    async def test_the_spools_own_temperature_wins(self):
        h = await self._linked(A.FakeSpoolman(), temp=232)
        self.assertIn("TEMP=232 SOURCE=spool", self._pushes(h)[0])

    async def test_a_spool_the_component_never_read_pushes_nothing(self):
        h = await Harness().init()
        await h.ready([bay(spool_id=4242, link_source="manual")])
        self.assertEqual(self._pushes(h), [])

    async def test_material_temps_is_read_in_the_constructor(self):
        api = A.FakeSpoolman()
        h = await self._linked(api, {"material_temps": "PLA=215, PCTG=250"})
        self.assertEqual(h.component._material_temps,
                         {"PLA": 215, "PCTG": 250})
        table = h.component.linker._material_temps
        self.assertEqual(table["PLA"], 215)        # replaced
        self.assertEqual(table["PETG"], 240)       # the built-in value
        # A bad entry is skipped, and the option itself never raises.
        other = await self._linked(A.FakeSpoolman(),
                                   {"material_temps": "PLA=hot"})
        self.assertEqual(other.component._material_temps, {})

    async def test_a_klipper_restart_makes_the_linker_push_again(self):
        h = await self._linked(A.FakeSpoolman())
        self.assertEqual(len(self._pushes(h)), 1)
        # Klippy came back and forgot everything it was told (C4 §7.4).
        await h.component._handle_klippy_ready()
        await h.flush()
        pushes = self._pushes(h)
        self.assertEqual(len(pushes), 2)
        self.assertEqual(pushes[0], pushes[1])


class LaneDataTest(unittest.IsolatedAsyncioTestCase):
    """The ``lane_data`` namespace OrcaSlicer syncs its filaments from."""

    async def _spool(self, api, material, temp=None, bed=None, color="26A269",
                     name="Filament", vendor="Acme"):
        v = await api.request("POST", "/v1/vendor", body={"name": vendor})
        f = await api.request(
            "POST", "/v1/filament",
            body={"name": name, "vendor_id": v["id"], "material": material,
                  "color_hex": color, "settings_extruder_temp": temp,
                  "settings_bed_temp": bed})
        s = await api.request("POST", "/v1/spool",
                              body={"filament_id": f["id"],
                                    "initial_weight": 1000,
                                    "remaining_weight": 1000})
        return s["id"]

    def _status(self, links, groups, lane=None):
        """Two units (a one-bay ``solo``, a two-bay ``duo``); ``links`` maps
        bay key -> spool id."""
        def entry(unit, index):
            sid = links.get("%s-%d" % (unit, index))
            return bay(index=index, spool_id=sid,
                       link_source="manual" if sid else "none")
        status = oams_status([])
        status["topology"] = {"oams": {"solo": {"idx": 1, "lane": "fps1"},
                                       "duo": {"idx": 2, "lane": "fps1"}}}
        status["devices"] = {
            "solo": {"bays": [entry("solo", 0)]},
            "duo": {"bays": [entry("duo", 0), entry("duo", 1)]}}
        status["groups"] = groups
        status["lanes_by_fps"] = {"fps1": lane or {
            "op": "UNLOADED", "group": None, "unit": None, "runout": "idle",
            "extruder": "extruder"}}
        return status

    async def _harness(self, status, options=None, store=None, api=None):
        api = api or A.FakeSpoolman()
        h = Harness(api=api)
        h.config = StubConfig(h.server, options)
        h.component = OpenAmsSpoolman(h.config, api=api)
        h.api = api
        await h.component.component_init()
        h.lanes = h.server.components["database"].namespaces.setdefault(
            "lane_data", {})
        if store:
            h.lanes.update(store)
        h.klippy_apis.subscribe_result = {
            "oams_manager": status, "toolhead": {"extruder": "extruder"}}
        await h.component._handle_klippy_ready()
        await h.flush()
        return h

    async def _basic(self, api):
        a = await self._spool(api, "PLA", temp=215, bed=55, color="FF0000",
                              name="Red PLA", vendor="Acme")
        b = await self._spool(api, "PETG", color="00FF00", name="Green")
        groups = [{"name": "T0", "lane": "fps1", "slots": [0],
                   "next_bay": [1, 0], "next_filament_temp_c": 250},
                  {"name": "T1", "lane": "fps1", "slots": [1],
                   "next_bay": [2, 0]},
                  {"name": "T2", "lane": "fps1", "slots": [2],
                   "next_bay": [2, 1]}]
        return a, b, groups

    async def test_lanes_are_built_from_groups_bays_and_spools(self):
        api = A.FakeSpoolman()
        a, b, groups = await self._basic(api)
        status = self._status({"solo-0": a, "duo-0": b}, groups)
        h = await self._harness(status, api=api)
        self.assertEqual(set(h.lanes), {"lane1", "lane2", "lane3"})
        self.assertEqual(h.lanes["lane1"], {
            "lane": "0", "color": "#FF0000", "material": "PLA",
            "bed_temp": 55, "nozzle_temp": 215, "name": "Red PLA",
            "vendor": "Acme", "spool_id": a})
        lane2 = h.lanes["lane2"]
        self.assertEqual(lane2["lane"], "1")          # a string, not a number
        self.assertEqual((lane2["material"], lane2["color"]),
                         ("PETG", "#00FF00"))
        self.assertEqual(lane2["nozzle_temp"], 240)   # the material table
        self.assertEqual(lane2["bed_temp"], 80)       # the bed table
        # A bay with no spool is a lane with no filament.
        self.assertEqual(h.lanes["lane3"]["material"], "")
        self.assertEqual(h.lanes["lane3"]["lane"], "2")

    async def test_the_loaded_bay_wins_over_the_next_bay(self):
        api = A.FakeSpoolman()
        a, b, groups = await self._basic(api)
        groups[0]["next_bay"] = [1, 0]
        status = self._status({"solo-0": a, "duo-0": b}, groups, lane={
            "op": "LOADED", "group": "T0", "unit": [2, 0],
            "runout": "idle", "extruder": "extruder"})
        h = await self._harness(status, api=api)
        self.assertEqual(h.lanes["lane1"]["spool_id"], b)
        # Another group's load does not redirect this one.
        status = self._status({"solo-0": a, "duo-0": b}, groups, lane={
            "op": "LOADED", "group": "T1", "unit": [2, 0],
            "runout": "idle", "extruder": "extruder"})
        h = await self._harness(status, api=api)
        self.assertEqual(h.lanes["lane1"]["spool_id"], a)

    async def test_a_group_without_a_number_uses_its_position(self):
        api = A.FakeSpoolman()
        a, b, groups = await self._basic(api)
        groups[0]["name"] = "left"
        groups[1]["name"] = "right"
        h = await self._harness(self._status({"solo-0": a}, groups[:2]),
                                api=api)
        self.assertEqual(h.lanes["lane1"]["lane"], "0")
        self.assertEqual(h.lanes["lane2"]["lane"], "1")

    async def test_stale_lanes_are_removed(self):
        api = A.FakeSpoolman()
        a, b, groups = await self._basic(api)
        h = await self._harness(
            self._status({"solo-0": a}, groups[:1]), api=api,
            store={"lane7": {"lane": "6", "material": "PLA"}})
        self.assertEqual(set(h.lanes), {"lane1"})

    async def test_nothing_is_rewritten_when_unchanged(self):
        api = A.FakeSpoolman()
        a, b, groups = await self._basic(api)
        status = self._status({"solo-0": a}, groups)
        h = await self._harness(status, api=api)
        writes = []
        original = StubNamespace.insert
        StubNamespace.insert = lambda self, k, v: (writes.append(k) if k != "cache" else None,
                                                   original(self, k, v))[1]
        try:
            for _ in range(2):
                h.push({"oams_manager": {"settings": {"x": 1}}})
                await h.flush()
            self.assertEqual(writes, [])
            groups[0]["next_filament_temp_c"] = 1   # not part of the lane
            # A changed spool changes only its lane.
            await api.request("PATCH", "/v1/filament/1",
                              body={"color_hex": "0000FF"})
            await h.component.linker._reload_summary(a)
            h.push({"oams_manager": {"settings": {"x": 2}}})
            await h.flush()
            self.assertEqual(writes, ["lane1"])
            self.assertEqual(h.lanes["lane1"]["color"], "#0000FF")
        finally:
            StubNamespace.insert = original

    async def test_matching_stored_content_is_not_written_after_a_start(self):
        api = A.FakeSpoolman()
        a, b, groups = await self._basic(api)
        status = self._status({"solo-0": a}, groups)
        first = await self._harness(status, api=api)
        stored = dict(first.lanes)
        writes = []
        original = StubNamespace.insert
        StubNamespace.insert = lambda self, k, v: (writes.append(k) if k != "cache" else None,
                                                   original(self, k, v))[1]
        try:
            await self._harness(status, api=api, store=stored)
        finally:
            StubNamespace.insert = original
        self.assertEqual(writes, [])

    async def test_the_option_off_writes_nothing(self):
        api = A.FakeSpoolman()
        a, b, groups = await self._basic(api)
        h = await self._harness(self._status({"solo-0": a}, groups),
                                options={"publish_lane_data": "False"},
                                api=api)
        self.assertEqual(h.lanes, {})
        self.assertIsNone(h.component._lane_db)

    async def test_a_database_failure_does_not_break_the_pass(self):
        api = A.FakeSpoolman()
        a, b, groups = await self._basic(api)
        original = StubNamespace.insert

        def boom(self, key, value):
            raise RuntimeError("database is locked")
        StubNamespace.insert = boom
        try:
            h = await self._harness(self._status({"solo-0": a}, groups),
                                    api=api)
            h.push({"oams_manager": {"settings": {"x": 3}}})
            await h.flush()
        finally:
            StubNamespace.insert = original
        # The pass still ran: the bay is linked and the status was sent.
        self.assertEqual(h.bay("solo-0")["state"], "linked")
        self.assertTrue(h.server.sent_events)


if __name__ == "__main__":
    unittest.main()
