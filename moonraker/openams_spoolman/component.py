# The openams_spoolman Moonraker component: the glue between a real Moonraker
# and the linker/cache/active-spool classes (C4 §1, §6-§8).
#
# It subscribes to the host's ``oams_manager`` status and ``toolhead.extruder``
# (Moonraker's Klippy host API, read-only reference: ``server.py``'s
# ``register_endpoint``/``register_notification``/``register_event_handler``/
# ``send_event``/``lookup_component``/``get_event_loop``,
# ``components/klippy_apis.py``'s ``subscribe_objects``/``run_gcode``,
# ``components/database.py``'s ``register_local_namespace``, and
# ``components/spoolman.py``'s ``set_active_spool`` and ``server`` option),
# drives ``Linker``/``ActiveSpool`` with it, and serves the C4 §8 status object.

import asyncio
import copy
import inspect
import logging
import time

from .active import ActiveSpool
from .api import DirectSpoolmanAPI, SpoolmanError, SpoolmanUnavailable
from . import edit
from . import lane_data
from . import ops
from .cache import SpoolCache
from .linker import Linker

_LOGGER = logging.getLogger(__name__)

DB_NAMESPACE = "openams_spoolman"
CACHE_KEY = "cache"
EVENT_NAME = "openams_spoolman:status_changed"
NOTIFY_NAME = "openams_spoolman_status"
STARTUP_RETRY_SECONDS = 30.0
READY_POLL_SECONDS = 2.0
# How often the linked spools are re-read from Spoolman (C4 §6): its web UI
# tells nobody about an edit. ``refresh_interval`` overrides it; 0 turns the
# re-read off, and anything else is kept at least this long, since each one is
# a ``GET /v1/spool``.
REFRESH_SECONDS = 60.0
MIN_REFRESH_SECONDS = 10.0
# The remote method the host's OAMSM_SET_TRAY_INFO calls with SPOOL= (C3 §4):
# only this component reads Spoolman.
TRAY_WRITE_METHOD = "openams_spoolman_tray_write"


def _merge(dst, src):
    """Merge a Klippy status diff into ``dst`` in place (nested dicts merge;
    anything else in ``src`` replaces the value in ``dst``)."""
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _merge(dst[key], value)
        else:
            dst[key] = value


class OpenAmsSpoolman(object):
    def __init__(self, config, api=None):
        self.server = config.get_server()
        self.eventloop = self.server.get_event_loop()
        self.klippy_apis = self.server.lookup_component("klippy_apis")
        self._api = api if api is not None else \
            DirectSpoolmanAPI(self._resolve_spoolman_url(config))
        self.linker = None
        self.active = None
        self._db = None
        self._status = {}
        self._last_sent = None
        self._pass_running = False
        self._pass_pending = False
        self._pass_task = None
        self._warned_no_oams = False
        # Set at Klippy ready: the status snapshot may predate the units
        # (ready False) and Moonraker can swallow the diff that flips it, so
        # an unready status is re-read on a clock (C4 §7.4).
        self._repush_pending = False
        self._warned_no_spoolman = False
        self._edit_lock = asyncio.Lock()
        # The one pending timed pass (C4 §7.5) and when it is due.
        self._wake_handle = None
        self._wake_due = None
        # The extruder temperature table the linker works from, with the
        # user's material_temps entries merged over it (C4 §7.4).
        self._material_temps = ops.parse_material_temps(
            config.get("material_temps", None))
        # The vendor a Bambu tag's new spools are filed under (C4 §5.3).
        self._tag_vendor = ops.parse_tag_vendor(config.get("tag_vendor", None))
        self._refresh_interval = self._parse_refresh_interval(
            config.get("refresh_interval", None))
        self._refresh_handle = None

        # ``publish_lane_data`` (default on): keep the ``lane_data`` database
        # namespace, which OrcaSlicer's "sync filaments from printer" reads,
        # in step with each tool's filament.
        self._publish_lane_data = config.getboolean("publish_lane_data", True)
        self._lane_db = None
        self._lane_known = None      # what the namespace holds, once read
        self._warned_lane_data = False

        database = self.server.lookup_component("database", None)
        if database is not None:
            self._db = database.register_local_namespace(DB_NAMESPACE)
            if self._publish_lane_data:
                self._lane_db = self._lane_namespace(database)

        self.server.register_notification(EVENT_NAME, NOTIFY_NAME)
        self.server.register_event_handler(
            "server:klippy_ready", self._handle_klippy_ready)
        self._register_endpoints()
        # Moonraker accepts remote methods only while components load.
        self.server.register_remote_method(
            TRAY_WRITE_METHOD, self._handle_tray_write)

    @staticmethod
    def _parse_refresh_interval(text):
        """The ``refresh_interval`` option in seconds (C4 §6): unset is 60, 0
        is off, a number under 10 is raised to 10, and anything that is not a
        number is logged and ignored: one typo must not stop the component."""
        if text is None or not str(text).strip():
            return REFRESH_SECONDS
        try:
            seconds = float(str(text).strip())
        except ValueError:
            seconds = None
        if seconds is None or seconds != seconds or seconds < 0 \
                or seconds == float("inf"):
            _LOGGER.warning(
                "openams_spoolman: refresh_interval '%s' is not a number of "
                "seconds; using %d", text, REFRESH_SECONDS)
            return REFRESH_SECONDS
        if 0 < seconds < MIN_REFRESH_SECONDS:
            _LOGGER.warning(
                "openams_spoolman: refresh_interval %g is under %d; using %d",
                seconds, MIN_REFRESH_SECONDS, MIN_REFRESH_SECONDS)
            return MIN_REFRESH_SECONDS
        return seconds

    def _lane_namespace(self, database):
        """The ``lane_data`` namespace. A registered local namespace is
        protected (HTTP clients read it, only components write it); one that
        another component already registered is wrapped instead."""
        try:
            return database.register_local_namespace(lane_data.NAMESPACE)
        except Exception:
            try:
                return database.wrap_namespace(lane_data.NAMESPACE,
                                               parse_keys=False)
            except Exception as error:
                self._warn_lane_data(error)
                return None

    def _warn_lane_data(self, error):
        if not self._warned_lane_data:
            self._warned_lane_data = True
            _LOGGER.warning("openams_spoolman: cannot publish lane_data: %s",
                            error)

    @staticmethod
    def _resolve_spoolman_url(config):
        """``spoolman_url``, else the ``[spoolman]`` section's ``server`` (C4 §5)."""
        url = config.get("spoolman_url", None)
        if url:
            return url.rstrip("/")
        root = config.get_server().config
        if root.has_section("spoolman"):
            base = root.getsection("spoolman").get("server", None)
            if base:
                return base.rstrip("/") + "/api"
        raise config.error(
            "[openams_spoolman] needs spoolman_url, or a [spoolman] section "
            "with a server option")

    def _register_endpoints(self):
        # A plain string list is Moonraker's other accepted form for
        # ``request_types`` (``server.py``'s ``register_endpoint``), so this
        # needs no import of Moonraker's own ``RequestType`` enum (GPLv3) and
        # stays importable outside a real Moonraker for the unit tests.
        self.server.register_endpoint(
            "/server/openams_spoolman/status", ["GET"], self._handle_status)
        self.server.register_endpoint(
            "/server/openams_spoolman/confirm", ["POST"], self._handle_confirm)
        self.server.register_endpoint(
            "/server/openams_spoolman/edit", ["POST"], self._handle_edit)
        # Moonraker also serves this as the JSON-RPC method
        # ``server.openams.stop``.
        self.server.register_endpoint(
            "/server/openams/stop", ["POST"], self._handle_stop)

    async def component_init(self):
        """Moonraker's post-load hook: restore the cache, build the linker and
        the active-spool tracker, then wait for Spoolman (C4 §6)."""
        state = None
        if self._db is not None:
            state = await self._db.get(CACHE_KEY, None)
        cache = SpoolCache(load=lambda: state, save=self._save_cache)
        self.linker = Linker(self._api, self._run_gcode, cache=cache,
                             material_temps=self._material_temps,
                             wake=self._wake, tag_vendor=self._tag_vendor)
        spoolman = self.server.lookup_component("spoolman", None)
        seed = getattr(spoolman, "spool_id", None) \
            if spoolman is not None else None
        self.active = ActiveSpool(self._api, self._run_gcode,
                                  self._set_active_spool, seed=seed)
        self.eventloop.create_task(self._startup_retry())
        self._arm_refresh()

    def _save_cache(self, state):
        if self._db is not None:
            self._db.insert(CACHE_KEY, state)   # fire and forget (C4 §6)

    async def _startup_retry(self):
        """``linker.start()`` once Spoolman answers; retried every 30 s until
        then (offline afterwards is the linker's own job, C4 §6)."""
        while True:
            await self.linker.start()
            if self.linker.spoolman_online:
                return
            await asyncio.sleep(STARTUP_RETRY_SECONDS)

    def _arm_refresh(self):
        if self._refresh_interval > 0:
            self._refresh_handle = self.eventloop.delay_callback(
                self._refresh_interval, self._refresh_due)

    def _refresh_due(self):
        self._refresh_handle = None
        self.eventloop.create_task(self._refresh())

    async def _refresh(self):
        """Re-read the linked spools (C4 §6); a change goes through a status
        pass, which pushes what depends on a summary (C4 §7.4, §7.4.1, §7.5)
        and notifies exactly as any status change does."""
        try:
            if await self.linker.refresh_linked():
                self._schedule_pass()
        except Exception:
            _LOGGER.exception("openams_spoolman: the spool refresh failed")
        finally:
            self._arm_refresh()

    async def _run_gcode(self, script):
        return await self.klippy_apis.run_gcode(script)

    async def _set_active_spool(self, spool_id):
        spoolman = self.server.lookup_component("spoolman", None)
        if spoolman is None:
            if not self._warned_no_spoolman:
                _LOGGER.info(
                    "openams_spoolman: no [spoolman] component; the active "
                    "spool will not be tracked")
                self._warned_no_spoolman = True
            return
        spoolman.set_active_spool(spool_id)

    async def _handle_klippy_ready(self):
        try:
            result = await self.klippy_apis.subscribe_objects(
                {"oams_manager": None, "toolhead": ["extruder"]},
                self._handle_status_update)
        except Exception as error:
            self._log_no_oams(error)
            return
        if not isinstance(result, dict) or "oams_manager" not in result:
            self._log_no_oams(None)
            return
        self._status = result
        self._repush_pending = True
        if self.linker is not None:
            # Klipper restarted and kept nothing of what it was told, so the
            # next pass pushes every bay's filament again (C4 §7.4).
            self.linker.forget_filament()
        self._schedule_pass()

    def _log_no_oams(self, error):
        if not self._warned_no_oams:
            _LOGGER.info(
                "openams_spoolman: this host has no oams_manager%s",
                "" if error is None else (": %s" % error))
            self._warned_no_oams = True

    def _handle_status_update(self, status, eventtime=None):
        _merge(self._status, status or {})
        self._schedule_pass()

    def _wake(self, seconds):
        """Run a status pass ``seconds`` from now (C4 §7.5): a unit's
        filament setting waits and retries on a clock. One timer at most;
        an earlier one already pending covers a later request, since every
        pass asks again for what it still needs."""
        due = time.monotonic() + seconds
        if self._wake_handle is not None:
            if self._wake_due <= due:
                return
            self._wake_handle.cancel()
        self._wake_due = due
        self._wake_handle = self.eventloop.delay_callback(seconds, self._woken)

    def _woken(self):
        self._wake_handle = None
        self._wake_due = None
        self._schedule_pass()

    def _schedule_pass(self):
        if self._pass_running:
            self._pass_pending = True
            return self._pass_task
        self._pass_running = True
        self._pass_task = self.eventloop.create_task(self._run_pass())
        return self._pass_task

    async def _run_pass(self):
        try:
            while True:
                self._pass_pending = False
                await self._one_pass(copy.deepcopy(self._status))
                if not self._pass_pending:
                    return
        finally:
            self._pass_running = False

    async def _one_pass(self, status):
        oams = status.get("oams_manager")
        if not isinstance(oams, dict) or self.linker is None:
            return   # no host yet, or component_init has not built the linker
        if not oams.get("ready") and self._repush_pending:
            # Moonraker applies the diff of a second subscription (any other
            # component's subscribe at Klippy ready) without telling this
            # callback, so a unit that came online meanwhile would never be
            # seen: ask again until Klipper reports ready.
            self._wake(READY_POLL_SECONDS)
            fresh = await self._query_oams()
            if fresh is not None:
                _merge(self._status, {"oams_manager": fresh})
                status = copy.deepcopy(self._status)
                oams = status["oams_manager"]
        toolhead = status.get("toolhead") or {}
        try:
            await self.linker.on_status(oams)
            await self.active.on_status(oams, extruder=toolhead.get("extruder"))
        except Exception:
            # e.g. Klipper refused a G-code: log it; the next update runs a
            # fresh pass instead of the task dying unobserved.
            _LOGGER.exception("openams_spoolman: a status pass failed")
            return
        if oams.get("ready") and self._repush_pending:
            self._repush_pending = False
            _LOGGER.info("openams_spoolman: Klipper is ready after a restart; "
                         "pushed the bay filaments again")
        await self._publish_lanes(oams)
        new_status = self.linker.status()
        if new_status != self._last_sent:
            self._last_sent = new_status
            self.server.send_event(EVENT_NAME, new_status)

    @staticmethod
    async def _settled(result):
        if inspect.isawaitable(result):
            await result

    async def _publish_lanes(self, oams):
        """Write the ``lane_data`` lanes that differ from what the namespace
        holds, and delete the lanes that no longer exist. Never raises."""
        if self._lane_db is None or not oams.get("ready") \
                or not isinstance(oams.get("groups"), list):
            return
        try:
            wanted = lane_data.build(oams, self.linker.bay_summary,
                                     self.linker._material_temps)
            if self._lane_known is None:
                known = {}
                for key in await self._lane_db.keys():
                    known[key] = await self._lane_db.get(key, None)
                self._lane_known = known
            for key in sorted(set(self._lane_known) - set(wanted)):
                await self._settled(self._lane_db.delete(key))
                del self._lane_known[key]
            for key, value in wanted.items():
                if self._lane_known.get(key) != value:
                    await self._settled(self._lane_db.insert(key, value))
                    self._lane_known[key] = value
        except Exception as error:
            self._lane_known = None   # unsure what is stored: read it again
            self._warn_lane_data(error)

    async def _query_oams(self):
        try:
            result = await self.klippy_apis.query_objects(
                {"oams_manager": None})
        except Exception:
            return None
        fresh = result.get("oams_manager") if isinstance(result, dict) else None
        return fresh if isinstance(fresh, dict) else None

    async def _handle_status(self, web_request):
        return self.linker.status()

    async def _handle_confirm(self, web_request):
        bay = web_request.get_str("bay")
        remaining_g = web_request.get_float("remaining_g")
        if remaining_g < 0:
            raise self.server.error("remaining_g must not be negative", 400)
        result = await self.linker.confirm(bay, remaining_g)
        if result is None:
            raise self.server.error(
                "No bay '%s' is awaiting confirmation" % bay, 409)
        status_obj = self.linker.status()
        self._last_sent = status_obj
        self.server.send_event(EVENT_NAME, status_obj)
        return result

    def _edit_bay(self, unit, bay):
        """The host's entry for a bay (C2 §4), or a 404 naming what is unknown."""
        oams = self._status.get("oams_manager")
        devices = oams.get("devices") if isinstance(oams, dict) else None
        if not isinstance(devices, dict) or self.linker is None:
            raise edit.EditError("the printer host has not reported its "
                                 "spools yet", 503)
        device = devices.get(unit)
        if not isinstance(device, dict):
            raise edit.EditError("unknown unit '%s'" % unit, 404)
        for entry in device.get("bays") or []:
            if entry.get("index") == bay:
                return entry
        raise edit.EditError("%s has no Spool %d" % (unit, bay + 1), 404)

    def _console(self, text):
        """A line in every client's console, as Klipper's own output."""
        self.server.send_event("server:gcode_response", text)

    def _host_bay(self, unit, bay):
        """The host's C2 entry of a bay, or None."""
        oams = self._status.get("oams_manager")
        devices = oams.get("devices") if isinstance(oams, dict) else None
        device = devices.get(unit) if isinstance(devices, dict) else None
        for entry in (device or {}).get("bays") or []:
            if entry.get("index") == bay:
                return entry
        return None

    async def _handle_tray_write(self, unit=None, bay=None, spool_id=None,
                                 **_ignored):
        """``OAMSM_SET_TRAY_INFO OAMS= BAY= [SPOOL=]`` (C3 §4): write a
        spool's filament into a bay's unit. The host's own command reports
        the write; only a failure here is reported to the console."""
        try:
            bay = int(bay)
            spool_id = int(spool_id)
        except (TypeError, ValueError):
            _LOGGER.info("openams_spoolman: bad tray write request %s %s %s",
                         unit, bay, spool_id)
            return
        label = "%s Spool %d" % (unit, bay + 1)
        try:
            if self.linker is None:
                raise RuntimeError("the component has not started yet")
            entry = self._host_bay(unit, bay) or {}
            await self.linker.write_tray(unit, bay, spool_id,
                                         entry.get("tag"))
        except SpoolmanUnavailable:
            self.linker.spoolman_online = False
            reason = "Spoolman is offline"
        except SpoolmanError as error:
            reason = ("Spoolman has no spool %d" % spool_id
                      if error.status == 404 else str(error))
        except Exception as error:
            reason = str(error)
        else:
            return
        _LOGGER.info("openams_spoolman: spool %d not written to %s: %s",
                     spool_id, label, reason)
        self._console("!! The filament of spool %d was not written to %s: %s"
                      % (spool_id, label, reason))

    async def _handle_stop(self, web_request):
        """Stop the running load, unload or toolchange sequence (C3): forward
        to Klipper's ``openams/stop`` webhook, which a queued G-code could not
        do (it would wait behind the running macro), and return its answer
        (``{"result": "stopping"}`` or ``{"result": "nothing running"}``).
        Moonraker's KlippyAPI has no public generic request; its
        ``_send_klippy_request`` is what every public call there goes
        through."""
        lane = web_request.get_str("lane", None)
        params = {} if lane is None else {"lane": lane}
        return await self.klippy_apis._send_klippy_request(
            "openams/stop", params)

    async def _handle_edit(self, web_request):
        try:
            unit, bay, fields = edit.parse(dict(web_request.get_args()))
            return await self._edit(unit, bay, fields)
        except edit.EditError as error:
            raise self.server.error(str(error), error.status)

    async def _edit(self, unit, bay, fields):
        async with self._edit_lock:
            entry = self._edit_bay(unit, bay)
            spool_id = entry.get("spool_id")
            if spool_id is None and not entry.get("ready"):
                raise edit.EditError(
                    "no spool in %s Spool %d" % (unit, bay + 1), 409)
            try:
                spool, created = await edit.apply(
                    self._api, self._run_gcode, unit, bay, fields, spool_id)
            except edit.EditError as error:
                if error.status == 503:
                    self.linker.spoolman_online = False
                raise
            key = "%s-%d" % (unit, bay)
            bay_status = self.linker.apply_edit(
                key, spool["id"], spool,
                source=("manual" if created else
                        (entry.get("link_source")
                         if entry.get("link_source") in ("tag", "manual")
                         else "manual")),
                confirmed="remaining_weight" in fields)
            # The host learns the edited spool's temperature before the edit
            # is answered: a display that loads right after saving (an unknown
            # spool's "Save and load") must not start the load before it.
            await self.linker.push_bay(key, unit, bay, spool["id"])
            status_obj = self.linker.status()
            self._last_sent = status_obj
            self.server.send_event(EVENT_NAME, status_obj)
            # A changed filament may have to reach the unit's stored setting
            # (C4 §7.5), which the next pass decides; do not wait for the
            # host's status to change first.
            self._schedule_pass()
            return {"spool_id": spool["id"], "created": created,
                    "bay": bay_status}
