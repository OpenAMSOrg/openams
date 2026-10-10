# The per-bay linker: one state machine per bay over the host's status (C4 §3).
#
# The host (``oams_manager``, C2) owns the bay <-> spool link and publishes it in
# its status; this class watches that status, resolves tag reads through the
# ``ops`` tag -> Spoolman policy (C4 §4, §5), asks the host to link or clear the
# bay (C3), and publishes its own view as the §8 status object. The host's
# ``spool_id``/``link_source`` is always authoritative; this class only mirrors
# what the host already holds, or drives a resolve when a tag is (re-)read.

import logging
import time

from . import ops
from . import tray
from .api import SpoolmanError, SpoolmanUnavailable
from .cache import SpoolCache

_LOGGER = logging.getLogger(__name__)

# A link command needs a moment to reach the host and come back in its status;
# do not read a still-empty host link as "cleared elsewhere" before that (C4 §3).
RECONCILE_SECONDS = 5.0
# The queued writes are replayed at most this often while Spoolman is away (C4 §6).
REPLAY_SECONDS = 5.0
OFFLINE_ERROR = "Spoolman offline"
OFFLINE_QUEUED_ERROR = "Spoolman offline; creation queued"
NO_SPOOL_ERROR = "no Spoolman spool for this tag"
AMBIGUOUS_ERROR = ("%d Spoolman spools match this spool; assign one"
                   " (C4 §4.1)")
# The most spools a bay's ``suggestions`` list names.
MAX_SUGGESTIONS = 10
BAMBU_VENDOR = "Bambu Lab"
# The keys the C4 §8 status object publishes per bay. The cached summary also
# carries the extruder temperature the host is pushed (C4 §7.4), which is a
# working value, not a §8 field.
SPOOL_STATUS_KEYS = ("material", "color_hex", "vendor", "remaining_g",
                     "remaining_pct", "initial_g", "flow_k")


class _Bay(object):
    """One bay's machine (C4 §3): the link as last applied, plus tag memory."""

    __slots__ = ("state", "spool_id", "source", "error", "last_tag",
                 "tag_uid", "link_sent_at", "mirror_queued", "filament_sent",
                 "suggestions")

    def __init__(self):
        self.state = "unlinked"
        self.spool_id = None
        self.source = None
        self.error = None
        self.last_tag = None       # the (uid, read_at) read seen last
        self.tag_uid = None        # the UID the current link came from
        self.link_sent_at = None   # clock() of the last link command
        self.mirror_queued = False  # the location mirror waits in the queue
        # (spool_id, material, temp_c, source) last pushed to the host; None
        # when nothing is pushed for this bay (C4 §7.4).
        self.filament_sent = None
        # The candidate spools of an ambiguous unit-decoded summary (C4 §4.1);
        # empty otherwise.
        self.suggestions = []


class Linker(object):
    def __init__(self, api, run_gcode, clock=time.monotonic, cache=None,
                 material_temps=None, wake=None):
        """``wake(seconds)``, when given, asks for another status pass that
        much later: a unit's filament setting waits and retries on a clock,
        not on status changes (C4 §7.5)."""
        self._api = api
        self._run_gcode = run_gcode
        self._clock = clock
        self._cache = cache if cache is not None else SpoolCache()
        self._material_temps = ops.material_temp_table(material_temps)
        self._bays = {}
        self._tray = tray.TraySync(
            run_gcode, clock, self._cache, self._reload_summary,
            self._remember_written, wake=wake,
            material_temps=self._material_temps)
        # bay key -> the spool id the host links it to, from the last status
        # (C4 §4.1: a spool linked to another bay is no candidate).
        self._host_links = {}
        self._last_replay = None
        self.spoolman_online = True

    async def _reachable(self):
        """A Spoolman call succeeded: pre-warm the cache once (C4 §6)."""
        self.spoolman_online = True
        await self._cache.prewarm(self._api)

    async def start(self):
        """Bootstrap Spoolman's extra fields when it is reachable (C4 §5.2)."""
        try:
            await ops.ensure_fields(self._api)
            await self._reachable()
        except SpoolmanUnavailable:
            self.spoolman_online = False

    def _bay(self, key):
        machine = self._bays.get(key)
        if machine is None:
            machine = self._bays[key] = _Bay()
        return machine

    @staticmethod
    def _in_runout(status, unit):
        """Whether ``unit``'s lane is in a runout, i.e. not an observed removal."""
        oams = ((status.get("topology") or {}).get("oams") or {}).get(unit) or {}
        lane = oams.get("lane")
        if not lane:
            return False
        entry = (status.get("lanes_by_fps") or {}).get(lane) or {}
        return entry.get("runout") not in (None, "idle")

    @staticmethod
    def _bay_in_use(status, unit, index):
        """Whether a lane has this bay loaded, loading or unloading (C2 §4.2).

        The filament is then threaded through the bay's hub, so a falling
        ``ready`` there is a runout starting, never a removal, even in a
        status sampled before the host recorded the runout.
        """
        oams = ((status.get("topology") or {}).get("oams") or {}).get(unit) or {}
        idx = oams.get("idx")
        for entry in (status.get("lanes_by_fps") or {}).values():
            if entry.get("op") in ("LOADED", "LOADING", "UNLOADING") \
                    and list(entry.get("unit") or []) == [idx, index]:
                return True
        return False

    def _cache_summary(self, spool):
        """Remember a spool the component just read (C4 §6, the read cache)."""
        self._cache.remember({}, spool)

    async def _reload_summary(self, spool_id):
        """Re-read a linked spool (C4 §7.5): Spoolman reports none of its
        own edits. The cache is written only when the summary changed."""
        try:
            spool = await self._api.request("GET", "/v1/spool/%s" % spool_id)
        except SpoolmanUnavailable:
            self.spoolman_online = False
            return
        except SpoolmanError:
            return
        await self._reachable()
        if isinstance(spool, dict) and \
                ops.spool_summary(spool) != self._cache.summary(spool_id):
            self._cache.remember({}, spool)

    def _remember_written(self, key, setting, spool_id):
        """A filament setting goes into a bay's unit (C4 §7.5): remember it,
        so the unit's report of it keeps the link (C4 §4.1)."""
        self._cache.remember_written(key, tray.fingerprint_summary(setting),
                                     spool_id)

    async def write_tray(self, unit, bay_index, spool_id, tag=None):
        """Write a spool's filament into a bay's unit now, on request
        (``OAMSM_SET_TRAY_INFO`` with ``SPOOL=``, C3 §4): read the spool,
        map it (C4 §7.5) against the unit's current report ``tag``, and send
        the explicit command. Returns the setting written. Raises
        SpoolmanUnavailable, SpoolmanError, ValueError (a spool with no
        material) or whatever the G-code raised."""
        spool = await self._api.request("GET", "/v1/spool/%s" % spool_id)
        await self._reachable()
        if not isinstance(spool, dict):
            raise SpoolmanError(404, "no spool %s" % spool_id)
        self._cache.remember({}, spool)
        setting = tray.setting_for(ops.spool_summary(spool),
                                   tray.setting_of_tag(tag),
                                   self._material_temps)
        if setting is None:
            raise ValueError("spool %s's filament has no material" % spool_id)
        key = "%s-%s" % (unit, bay_index)
        self._remember_written(key, setting, spool_id)
        await self._run_gcode(tray.gcode(unit, bay_index, setting))
        return setting

    async def _refresh_summary(self, spool_id, reload=False, tag=None):
        """Read and cache a spool's summary; silent while Spoolman is away."""
        if self._cache.summary(spool_id) is not None and not reload:
            return
        try:
            spool = await self._api.request("GET", "/v1/spool/%s" % spool_id)
        except SpoolmanUnavailable:
            self.spoolman_online = False
            return
        except SpoolmanError:
            return
        await self._reachable()
        if spool is not None:
            self._cache.remember(tag or {}, spool)

    async def _set_location(self, spool_id, location, machine=None):
        """Mirror the bay on the spool; ``""`` clears it (C4 §5.6).

        A write Spoolman cannot take is queued for a later replay (C4 §6), and
        the return says so.
        """
        try:
            await ops.set_location(self._api, spool_id, location)
            await self._reachable()
        except SpoolmanUnavailable:
            self.spoolman_online = False
            self._cache.enqueue({"op": "location", "spool_id": spool_id,
                                 "location": location or ""})
            return True
        except SpoolmanError as error:
            if machine is not None:
                machine.error = str(error)
        return False

    async def confirm(self, key, remaining_g):
        """The §5.8 answer: write the weight and leave ``pending_confirmation``."""
        machine = self._bays.get(key)
        if machine is None or machine.state != "pending_confirmation":
            return None
        spool_id = machine.spool_id
        try:
            spool = await self._api.request(
                "PATCH", "/v1/spool/%s" % spool_id,
                body={"remaining_weight": remaining_g})
        except SpoolmanUnavailable:
            self.spoolman_online = False
            machine.error = OFFLINE_ERROR
            return self._bay_status_object(key, machine)
        except SpoolmanError as error:
            machine.error = str(error)
            return self._bay_status_object(key, machine)
        self.spoolman_online = True
        machine.state = "linked"
        machine.error = None
        if spool is not None:
            self._cache_summary(spool)
        else:
            await self._refresh_summary(spool_id, reload=True)
        return self._bay_status_object(key, machine)

    def apply_edit(self, key, spool_id, spool, source=None, confirmed=False):
        """A UI edit (C4 §9) wrote ``spool``; show it in this bay now.

        ``source`` links a bay that was not linked yet (the host echoes the
        same link in its next status, which ``_adopt`` then finds current);
        ``confirmed`` ends ``pending_confirmation``, since the weight is known.
        Returns the bay's §8 entry.
        """
        machine = self._bay(key)
        if machine.spool_id != spool_id or machine.state not in (
                "linked", "pending_confirmation"):
            machine.state = "linked"
            machine.spool_id = spool_id
            machine.source = source or machine.source or "manual"
            machine.link_sent_at = self._clock()
            machine.mirror_queued = False
        elif confirmed and machine.state == "pending_confirmation":
            machine.state = "linked"
        machine.error = None
        self.spoolman_online = True
        if spool is not None:
            self._cache_summary(spool)
        return self._bay_status_object(key, machine)

    def status(self):
        """The §8 object and the status schema."""
        return {"schema_version": 1,
                "edit": True,
                "stop": True,
                "spoolman_online": bool(self.spoolman_online),
                "queued_writes": len(self._cache.queue),
                "bays": dict((key, self._bay_status_object(key, machine))
                             for key, machine in self._bays.items())}

    def _bay_status_object(self, key, machine):
        linked = machine.state in ("linked", "pending_confirmation")
        entry = {"state": machine.state,
                 "spool_id": machine.spool_id if linked else None,
                 "source": (machine.source or "manual") if linked else "none",
                 "pending_confirmation": machine.state == "pending_confirmation",
                 "error": machine.error,
                 "spool": self._published_summary(
                     machine.spool_id if linked else None)}
        if machine.suggestions and not linked:
            entry["suggestions"] = [dict(item) for item in machine.suggestions]
        tray_sync = self._tray.status(key)
        if tray_sync is not None:
            entry["tray_sync"] = tray_sync
        return entry

    def bay_summary(self, key):
        """The cached summary of the spool linked to a bay, with its
        ``spool_id``, or None (the input of the ``lane_data`` publication)."""
        machine = self._bays.get(key)
        if machine is None or machine.spool_id is None or \
                machine.state not in ("linked", "pending_confirmation"):
            return None
        summary = self._cache.summary(machine.spool_id)
        if summary is None:
            return None
        result = dict(summary)
        result["spool_id"] = machine.spool_id
        return result

    def _published_summary(self, spool_id):
        """The C4 §8 ``spool`` object: the cached summary, §8 keys only."""
        if spool_id is None:
            return None
        summary = self._cache.summary(spool_id)
        if summary is None:
            return None
        return dict((key, summary[key]) for key in SPOOL_STATUS_KEYS
                    if key in summary)

    async def on_status(self, status):
        """One pass over an ``oams_manager`` v2 status dict (C2, C4 §2)."""
        if not status.get("ready"):
            return
        await self._replay()
        settings = status.get("settings") or {}
        auto_create = bool(settings.get("auto_create_spoolman_spools"))
        sync_tray = bool(settings.get("sync_unit_tray_info", True))
        self._host_links = {}
        for unit, device in (status.get("devices") or {}).items():
            for bay in device.get("bays") or []:
                if bay.get("spool_id") is not None:
                    self._host_links["%s-%s" % (unit, bay.get("index"))] = \
                        bay["spool_id"]
        for unit, device in (status.get("devices") or {}).items():
            for bay in device.get("bays") or []:
                key = "%s-%s" % (unit, bay.get("index"))
                await self._bay_status(key, unit, bay, auto_create, status)
                # The host's link, not this bay's machine, decides what the
                # bay holds, so the push follows the link on every pass
                # (C4 §7.4).
                await self._push_filament(key, unit, bay)
                # The same link decides a unit's stored setting (C4 §7.5).
                await self._tray.on_bay(key, unit, device, bay, sync_tray)

    async def _bay_status(self, key, unit, bay, auto_create, status):
        machine = self._bay(key)
        tag = bay.get("tag")
        previous = machine.last_tag
        if tag is None:
            machine.last_tag = None
        else:
            machine.last_tag = (tag.get("uid"), tag.get("read_at"))

        # C4 §3: a tag-sourced link whose bay is ready but the tag has
        # disappeared (the previous status had a tag; a re-read found none)
        # means an untagged spool was inserted after the old one was removed.
        # Unlink immediately and clear the location mirror.
        if bay.get("ready", True) and tag is None and previous is not None \
                and machine.source == "tag" \
                and machine.state in ("linked", "pending_confirmation"):
            await self._unlink(unit, bay, machine)
            return

        if not bay.get("ready", True):
            # An empty bay: removal, unless the unit is in a runout, where the
            # spool is still in the bay and ``ready`` may fall (C4 §3).
            if self._in_runout(status, unit) or \
                    self._bay_in_use(status, unit, bay.get("index")):
                return
            if tag is None and machine.state in ("linked", "pending_confirmation"):
                await self._unlink(unit, bay, machine)
            return

        # The host's link is authoritative; on a fresh bay we adopt it even if a
        # tag is already in the bay (manual wins until a re-read, C4 §4 step 5).
        host_spool = bay.get("spool_id")
        fresh_adopt = host_spool is not None and \
            machine.state in ("unlinked", "resolving")
        read = None if tag is None else (tag.get("uid"), tag.get("read_at"))
        new_read = tag is not None and read != previous
        if new_read and not fresh_adopt and self._actionable(tag) \
                and self._should_resolve(machine, tag):
            await self._resolve(key, unit, bay, tag, machine, auto_create)
            return

        if not new_read and not fresh_adopt and machine.state == "resolving" \
                and tag is not None and self._actionable(tag) \
                and not self._has_queued_create(key):
            # Spoolman was away when the tag was read (C4 §6): the tag is still
            # in the bay, so retry now that Spoolman may answer; a queued create
            # is this bay's retry and replays on its own.
            await self._resolve(key, unit, bay, tag, machine, auto_create)
            return

        if host_spool is not None:
            await self._adopt(key, machine, host_spool, bay.get("link_source"),
                              bay.get("tag"))
            return

        if machine.state in ("linked", "pending_confirmation"):
            # The host holds no link while we do: it was cleared elsewhere, once
            # a link command we sent has had time to come back in the status.
            if machine.mirror_queued:
                return   # the location mirror of that link waits in the queue
            age = None if machine.link_sent_at is None else \
                self._clock() - machine.link_sent_at
            if age is None or age > RECONCILE_SECONDS:
                await self._reconcile(machine)

    async def push_bay(self, key, unit, bay, spool_id):
        """Push one bay's material and working temperature now (C4 7.4).

        A spool edit calls this before it answers, so a display that loads
        right after saving the spool never starts the load before the host
        knows the temperature it has to heat to.
        """
        await self._push_filament(key, unit, {"index": bay, "spool_id": spool_id})

    def forget_filament(self):
        """Klipper came back and forgot what it was told (C4 §7.4).

        The host keeps these values in memory only, so the next pass has to
        push every bay again.
        """
        for machine in self._bays.values():
            machine.filament_sent = None

    def _bay_filament(self, spool_id):
        """What a bay's spool says the host should heat to, or None (C4 §7.4).

        First match wins: the spool's own ``settings_extruder_temp``, then the
        material table. An unlinked bay, a spool Spoolman has not told us
        about, a spool with no material and a material outside the table are
        all unknown, and the host then uses its own default.
        """
        if spool_id is None:
            return None
        summary = self._cache.summary(spool_id)
        if summary is None:
            return None      # Spoolman offline and this spool was never read
        material = ops.material_name_of(summary.get("material"))
        temp = ops.extruder_temp_of(summary.get("extruder_temp"))
        source = "spool"
        if temp is None:
            temp = ops.material_extruder_temp(material, self._material_temps)
            source = "table"
        if temp is None:
            return None
        return (spool_id, material, temp, source)

    @staticmethod
    def _filament_command(unit, bay, values):
        """``OAMSM_SET_BAY_FILAMENT`` for ``values``, or the clearing form."""
        command = "OAMSM_SET_BAY_FILAMENT OAMS=%s BAY=%s" % (unit, bay)
        if values is None:
            return command     # no TEMP and no MATERIAL clears the bay
        parts = [command]
        if values[1]:
            # Quoted so a material with a space stays one argument: Klipper
            # splits extended parameters with shlex (klippy/gcode.py).
            parts.append('MATERIAL="%s"' % values[1].replace('"', ""))
        parts.append("TEMP=%d SOURCE=%s" % (values[2], values[3]))
        return " ".join(parts)

    async def _push_filament(self, key, unit, bay):
        """Push a bay's material and working temperature, on change only.

        The host keeps what it was last told, so an unchanged bay costs
        nothing; a bay that becomes unlinked is cleared once, and a Klipper
        restart (forget_filament) makes every bay push again.
        """
        machine = self._bay(key)
        wanted = self._bay_filament(bay.get("spool_id"))
        if wanted == machine.filament_sent:
            return
        try:
            await self._run_gcode(
                self._filament_command(unit, bay.get("index"), wanted))
        except Exception as error:
            # A Klipper that refuses this command must not break the pass,
            # and must not silence the retries (C4 §7.4).
            _LOGGER.info("openams_spoolman: bay filament not pushed for %s: "
                         "%s", key, error)
            return
        machine.filament_sent = wanted

    @staticmethod
    def _actionable(tag):
        """Whether a read has a link policy at all (C4 §4: tag first)."""
        fmt = tag.get("format")
        if fmt == "spoolman_link":
            return tag.get("spoolman_id") is not None
        if fmt == "unit_summary":
            # Nothing to match without a material (C4 §4.1).
            return bool((tag.get("summary") or {}).get("material"))
        return fmt == "bambu"

    @staticmethod
    def _should_resolve(machine, tag):
        """Manual wins until a re-read; only a different UID beats a tag link.

        A unit-decoded summary has no UID; every new read decides (C4 §4.1,
        whose Keep rule leaves a link that still matches alone)."""
        if tag.get("format") == "unit_summary":
            return True
        if machine.state in ("unlinked", "resolving"):
            return True
        if machine.source == "manual":
            return True
        return machine.tag_uid != tag.get("uid")

    async def _resolve(self, key, unit, bay, tag, machine, auto_create):
        if tag.get("format") == "unit_summary":
            await self._resolve_summary(key, unit, bay, tag, machine,
                                        auto_create)
            return
        machine.state = "resolving"
        machine.error = None
        if tag.get("format") == "spoolman_link":
            await self._resolve_link_tag(key, unit, bay, tag, machine)
        else:
            await self._resolve_bambu(key, unit, bay, tag, machine, auto_create)

    async def _resolve_link_tag(self, key, unit, bay, tag, machine):
        spool_id = tag.get("spoolman_id")
        spool = None
        try:
            spool = await self._api.request("GET", "/v1/spool/%s" % spool_id)
            await self._reachable()
        except SpoolmanUnavailable:
            self.spoolman_online = False   # offline: link the id anyway
        except SpoolmanError:
            machine.state = "unlinked"
            machine.source = None
            machine.spool_id = None
            machine.error = "Spoolman has no spool %s" % spool_id
            return
        await self._link(key, unit, bay, machine, spool_id, spool, tag=tag)

    async def _resolve_bambu(self, key, unit, bay, tag, machine, auto_create):
        try:
            spool = await ops.find_spool_by_tag(self._api, tag)
        except SpoolmanUnavailable:
            await self._resolve_offline(key, unit, bay, tag, machine, auto_create)
            return
        await self._reachable()
        if spool is not None:
            spool_id = spool["id"]
            uid = str(tag.get("uid") or "").strip().upper()
            known = str(ops.decode_extra(
                (spool.get("extra") or {}).get("card_uids")) or "")
            if uid and uid not in [part.strip().upper()
                                   for part in known.split(",")]:
                try:
                    spool = await ops.add_card_uid(self._api, spool, uid)
                except SpoolmanUnavailable:
                    self.spoolman_online = False
                    # Keep the link; the UID mirror is queued (C4 §6).
                    self._cache.enqueue({"op": "card_uid", "spool_id": spool_id,
                                         "uid": uid})
                except SpoolmanError:
                    pass   # keep the link; the UID mirror waits
            await self._link(key, unit, bay, machine, spool_id, spool, tag=tag)
            return
        if not auto_create:
            machine.state = "unlinked"
            machine.source = None
            machine.spool_id = None
            machine.error = NO_SPOOL_ERROR
            return
        try:
            vendor = await ops.get_or_create_vendor(self._api, BAMBU_VENDOR)
            filament = await ops.find_or_create_filament(
                self._api, vendor["id"], tag.get("bambu") or {})
            spool = await ops.create_spool(self._api, filament["id"], tag, key)
            await self._reachable()
        except SpoolmanUnavailable:
            await self._resolve_offline(key, unit, bay, tag, machine, auto_create)
            return
        await self._link(key, unit, bay, machine, spool["id"], spool,
                         pending=True, tag=tag)

    # ----------------------------------------- unit-decoded summaries (C4 §4.1)

    def _linked_elsewhere(self, key):
        """Spool ids linked to a bay other than ``key``: by the host's last
        status, or by another bay's machine."""
        ids = set(spool_id for other, spool_id in self._host_links.items()
                  if other != key)
        for other, machine in self._bays.items():
            if other != key and machine.spool_id is not None \
                    and machine.state in ("linked", "pending_confirmation"):
                ids.add(machine.spool_id)
        return ids

    async def _link_matches(self, machine, summary):
        """Whether the bay's linked spool still matches the summary: True,
        False, or None when its filament is not known (Spoolman offline and
        never read), which keeps the link."""
        spool_summary = self._cache.summary(machine.spool_id)
        if spool_summary is None:
            await self._refresh_summary(machine.spool_id)
            spool_summary = self._cache.summary(machine.spool_id)
        if spool_summary is None:
            return None
        return ops.summary_matches(spool_summary, summary)

    async def _summary_decide(self, key, summary, create):
        """Match, then create (C4 §4.1). Returns ``(verdict, payload)``:
        ``link`` (the one spool), ``ambiguous`` (the candidates), ``none``, or
        ``created`` (the new spool). Raises SpoolmanUnavailable."""
        spools = await self._api.request("GET", "/v1/spool") or []
        candidates = ops.matching_spools(
            spools, summary, excluded=self._linked_elsewhere(key))
        # A spool already located at this bay is preferred over the rest.
        pool = [spool for spool in candidates
                if spool.get("location") == key] or candidates
        if len(pool) == 1:
            return "link", pool[0]
        if len(pool) > 1:
            return "ambiguous", pool
        if not create:
            return "none", None
        return "created", await ops.create_summary_spool(
            self._api, summary, key)

    def _summary_for(self, key, tag):
        """The summary a read is matched by. A setting this component wrote
        into the unit (C4 §7.5) is matched without a manufacturer: its code
        came from Spoolman or the default table, and says nothing about who
        made the spool. Returns ``(summary, written spool id or None)``."""
        summary = ops.summary_of(tag)
        written = self._cache.written_spool(key, summary)
        if written is not None:
            summary = dict(summary, manufacturer=None)
        return summary, written

    async def _resolve_summary(self, key, unit, bay, tag, machine,
                               auto_create):
        """A unit-decoded summary has no UID, so no spool is identified
        uniquely; a link is made only when exactly one spool can be it."""
        summary, written = self._summary_for(key, tag)
        if written is not None and machine.spool_id == written:
            # The unit reports the setting written from this very spool: its
            # own echo of the write, not a new spool (C4 §4.1, §7.5).
            return
        was_linked = machine.spool_id is not None
        if was_linked and await self._link_matches(machine, summary) \
                is not False:
            # Keep: the link, of any source, still fits what the unit reads.
            self._cache.remember_summary(key, summary, machine.spool_id)
            return
        machine.error = None
        if not was_linked:
            machine.state = "resolving"
        try:
            verdict, payload = await self._summary_decide(
                key, summary, auto_create)
            await self._reachable()
        except SpoolmanUnavailable:
            await self._summary_offline(key, unit, bay, tag, summary, machine,
                                        auto_create)
            return
        except SpoolmanError as error:
            await self._summary_unresolved(unit, bay, machine, was_linked,
                                           str(error))
            return
        await self._apply_summary(key, unit, bay, tag, machine, verdict,
                                  payload, was_linked)

    async def _apply_summary(self, key, unit, bay, tag, machine, verdict,
                             payload, was_linked):
        summary = ops.summary_of(tag)
        if verdict in ("link", "created"):
            await self._link(key, unit, bay, machine, payload["id"], payload,
                             pending=verdict == "created", tag=tag)
            self._cache.remember_summary(key, summary, payload["id"])
        elif verdict == "ambiguous":
            await self._summary_unresolved(
                unit, bay, machine, was_linked,
                AMBIGUOUS_ERROR % len(payload), payload)
        else:
            await self._summary_unresolved(unit, bay, machine, was_linked,
                                           NO_SPOOL_ERROR)

    async def _summary_unresolved(self, unit, bay, machine, was_linked, error,
                                  candidates=()):
        """No single spool: never guess. A link that no longer fits the spool
        in the bay is cleared; the candidates go out as suggestions."""
        if was_linked:
            await self._unlink(unit, bay, machine)
        machine.state = "unlinked"
        machine.source = None
        machine.spool_id = None
        machine.error = error
        machine.suggestions = [
            ops.suggestion_of(spool)
            for spool in candidates[:MAX_SUGGESTIONS]]

    async def _summary_offline(self, key, unit, bay, tag, summary, machine,
                               auto_create):
        """Spoolman is away: the cache keyed by bay and summary, else queue
        the creation (C4 §6)."""
        self.spoolman_online = False
        spool_id = self._cache.lookup_summary(key, summary)
        if spool_id is not None:
            await self._link(key, unit, bay, machine, spool_id, tag=tag)
            return
        machine.state = "resolving"
        if auto_create:
            machine.error = OFFLINE_QUEUED_ERROR
            self._queue_create(key, unit, bay, tag)
        else:
            machine.error = OFFLINE_ERROR

    async def _resolve_offline(self, key, unit, bay, tag, machine, auto_create):
        """Spoolman is away: link the cached spool, else queue the create (C4 §6).

        Without ``auto_create_spoolman_spools`` there is nothing to queue (the
        online counterpart would go straight to ``unlinked``, C4 §3); the bay
        stays ``resolving`` with the plain offline error instead so the retry
        branch in ``_bay_status`` re-tries it once Spoolman answers again,
        exactly as if creation were merely delayed rather than off (contract
        silent on this combination; simplest reading).
        """
        self.spoolman_online = False
        spool_id = self._cache.lookup(tag)
        if spool_id is not None:
            await self._link(key, unit, bay, machine, spool_id, tag=tag)
            return
        machine.state = "resolving"
        if auto_create:
            machine.error = OFFLINE_QUEUED_ERROR
            self._queue_create(key, unit, bay, tag)
        else:
            machine.error = OFFLINE_ERROR

    def _has_queued_create(self, key):
        """Whether a creation for this bay key waits in the queue (C4 §6)."""
        return any(entry.get("op") == "create" and entry.get("key") == key
                   for entry in self._cache.queue)

    def _queue_create(self, key, unit, bay, tag):
        """Queue one creation per bay; a newer read replaces an older one."""
        self._cache.queue[:] = [
            entry for entry in self._cache.queue
            if not (entry.get("op") == "create" and entry.get("key") == key)]
        self._cache.persist()
        self._cache.enqueue({"op": "create", "key": key, "unit": unit,
                             "bay": bay.get("index"), "tag": tag})

    async def _link(self, key, unit, bay, machine, spool_id, spool=None,
                    pending=False, tag=None):
        """Tell the host about the link and mirror the location (C4 §5.6)."""
        old = machine.spool_id
        machine.suggestions = []
        machine.state = "pending_confirmation" if pending else "linked"
        machine.source = "tag"
        machine.spool_id = spool_id
        machine.tag_uid = machine.last_tag[0] if machine.last_tag else None
        machine.error = None
        machine.link_sent_at = self._clock()
        if spool is not None:
            self._cache.remember(tag or {}, spool)
        await self._run_gcode(
            "OAMSM_SET_BAY_SPOOL OAMS=%s BAY=%s SPOOL=%s SOURCE=tag"
            % (unit, bay.get("index"), spool_id))
        if old is not None and old != spool_id:
            # A re-read relinked this bay to a different spool (a new tag,
            # or the same tag found under a new id): the old spool no
            # longer sits here, matching what _adopt already does for a
            # manual relink (C4 §5.6).
            await self._set_location(old, "", machine)
        queued = await self._set_location(spool_id, key, machine)
        machine.mirror_queued = bool(queued)

    async def _adopt(self, key, machine, host_spool, host_source, tag=None):
        """Follow the host's authoritative link (C4 §3, first row)."""
        source = host_source if host_source in ("tag", "manual") else "manual"
        if machine.state == "linked" and machine.spool_id == host_spool \
                and machine.source == source:
            return
        if machine.state == "pending_confirmation" and \
                machine.spool_id == host_spool and source == "tag":
            return   # the host echoes our link; the display has yet to confirm
        old = machine.spool_id
        machine.suggestions = []
        machine.state = "linked"
        machine.source = source
        machine.spool_id = host_spool
        machine.error = None
        if source == "tag" and machine.last_tag is not None:
            machine.tag_uid = machine.last_tag[0]
        if old != host_spool:
            if old is not None:
                await self._set_location(old, "", machine)
            queued = await self._set_location(host_spool, key, machine)
            machine.mirror_queued = bool(queued)
        await self._refresh_summary(host_spool, reload=True, tag=tag)

    async def _unlink(self, unit, bay, machine):
        """Removal: clear the host's link and the location mirror (C4 §3)."""
        old = machine.spool_id
        machine.suggestions = []
        machine.state = "unlinked"
        machine.source = None
        machine.spool_id = None
        machine.tag_uid = None
        machine.error = None
        machine.mirror_queued = False
        await self._run_gcode("OAMSM_CLEAR_BAY_SPOOL OAMS=%s BAY=%s"
                              % (unit, bay.get("index")))
        if old is not None:
            await self._set_location(old, "", machine)

    async def _reconcile(self, machine):
        """The host's link vanished: follow it to ``unlinked`` (C4 §3)."""
        old = machine.spool_id
        machine.suggestions = []
        machine.state = "unlinked"
        machine.source = None
        machine.spool_id = None
        machine.tag_uid = None
        machine.error = None
        machine.mirror_queued = False
        if old is not None:
            await self._set_location(old, "", machine)

    async def _replay(self):
        """Replay the queued writes in order (C4 §6); stop at the first
        ``SpoolmanUnavailable`` and keep the rest for the next attempt."""
        if not self._cache.queue:
            return
        now = self._clock()
        if self._last_replay is not None and now - self._last_replay < REPLAY_SECONDS:
            return
        self._last_replay = now
        while self._cache.queue:
            entry = self._cache.queue[0]
            try:
                await self._replay_one(entry)
            except SpoolmanUnavailable:
                self.spoolman_online = False
                return
            except SpoolmanError:
                pass   # the target is gone (e.g. a deleted spool); drop it
            self._cache.discard(entry)
        await self._reachable()

    async def _replay_one(self, entry):
        op = entry.get("op")
        if op == "location":
            await ops.set_location(self._api, entry.get("spool_id"),
                                   entry.get("location") or "")
            self._mark_mirror_done(entry)
        elif op == "card_uid":
            spool = await self._api.request(
                "GET", "/v1/spool/%s" % entry.get("spool_id"))
            if spool is not None:
                await ops.add_card_uid(self._api, spool, entry.get("uid"))
        elif op == "create":
            await self._replay_create(entry)

    def _mark_mirror_done(self, entry):
        """A queued location mirror landed: give that bay a fresh grace period
        before ``_bay_status`` may reconcile it away (C4 §3's ``RECONCILE_SECONDS``,
        which otherwise counts from the link, not from the mirror actually
        reaching Spoolman)."""
        key = entry.get("location") or ""
        machine = self._bays.get(key)
        if machine is not None and machine.spool_id == entry.get("spool_id"):
            machine.link_sent_at = self._clock()
            machine.mirror_queued = False

    async def _replay_create(self, entry):
        """Replay one queued creation, de-duplicating by tag (C4 §4, §6)."""
        key, unit, bay_index, tag = (entry.get("key"), entry.get("unit"),
                                     entry.get("bay"), entry.get("tag") or {})
        machine = self._bay(key)
        bay = {"index": bay_index}
        if tag.get("format") == "unit_summary":
            await self._replay_create_summary(key, unit, bay, tag, machine)
            return
        spool = await ops.find_spool_by_tag(self._api, tag)
        pending = False
        if spool is None:
            vendor = await ops.get_or_create_vendor(self._api, BAMBU_VENDOR)
            filament = await ops.find_or_create_filament(
                self._api, vendor["id"], tag.get("bambu") or {})
            spool = await ops.create_spool(self._api, filament["id"], tag, key)
            pending = True
        still_resolving = machine.state == "resolving" and \
            machine.last_tag is not None and \
            machine.last_tag[0] == tag.get("uid")
        if not still_resolving:
            # The bay moved on or the spool was removed while this waited
            # (C4 §6); remember the spool for later lookups but do not link.
            self._cache.remember(tag, spool)
            return
        await self._link(key, unit, bay, machine, spool["id"], spool,
                         pending=pending, tag=tag)

    async def _replay_create_summary(self, key, unit, bay, tag, machine):
        """Replay a queued unit-decoded creation (C4 §4.1, §6): the whole
        policy again, so a spool made meanwhile is matched rather than
        duplicated. A read that is no longer the bay's current one is dropped
        without creating anything, since the summary says nothing durable."""
        if not (machine.state == "resolving"
                and machine.last_tag == (tag.get("uid"), tag.get("read_at"))):
            return
        verdict, payload = await self._summary_decide(
            key, self._summary_for(key, tag)[0], True)
        await self._apply_summary(key, unit, bay, tag, machine, verdict,
                                  payload, False)
