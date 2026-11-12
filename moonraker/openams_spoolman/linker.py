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
# A decoded tag no spool claims, but a spool made from the spool's other tag
# may be it (C4 §4.2 step 6), or an archived spool is (step 5): offered,
# never linked silently. The user's answer is an ordinary manual link.
LEGACY_ERROR = "Same spool as #%d? This may be its other tag"
LEGACY_MANY_ERROR = ("%d Spoolman spools may hold this spool's other tag;"
                     " assign one")
ARCHIVED_ERROR = "Archived spool #%d matches this tag; assign it to restore it"
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
                 "tag_key", "read_uid", "awaiting_decode", "link_sent_at",
                 "mirror_queued", "filament_sent", "suggestions",
                 "suggestion_key", "suggestion_kind", "suggested_tag",
                 "seen", "startup", "limbo_key", "startup_manual")

    def __init__(self):
        self.state = "unlinked"
        self.spool_id = None
        self.source = None
        self.error = None
        # The (spool key, read_at) read seen last: a read is new when either
        # changes, so a sibling UID of the same spool is not (C4 §4.3).
        self.last_tag = None
        self.tag_key = None        # the spool key the current link came from
        self.seen = False          # no status pass has reached the bay yet
        # The startup window (C4 §3): from this machine's start, or the
        # host's restart, until the bay first shows a tag or an empty bay.
        # Meanwhile a tag that is not there yet is no removal, and the key
        # the bay waits for (``limbo_key``: the read remembered from before
        # a host restart, or the bay's one restored candidate) confirmed is
        # no new read, so a manual link the host kept stands.
        self.startup = True
        # The manual link the host already held on the first pass of that
        # window (a spool id; False: none; None: no pass yet). The first tag
        # the bay then shows was there all along, read again only because
        # of the restart: no new read, so that manual link stands.
        self.startup_manual = None
        self.limbo_key = None
        self.read_uid = None       # the UID read on the bay, last status
        # A tag attributed by its UID but not decoded yet that no spool lists
        # waits for its decode; nothing is created from a bare UID (C4 §4.2
        # step 8).
        self.awaiting_decode = False
        self.link_sent_at = None   # clock() of the last link command
        self.mirror_queued = False  # the location mirror waits in the queue
        # (spool_id, material, temp_c, source) last pushed to the host; None
        # when nothing is pushed for this bay (C4 §7.4).
        self.filament_sent = None
        # The candidate spools of an ambiguous unit-decoded summary (C4 §4.1),
        # or the spool a tag may be the other tag of, or an archived spool
        # (C4 §4.2); empty otherwise. ``suggestion_kind`` says which
        # (``summary``, ``legacy``, ``archived``); for the two tag kinds the
        # spool key and tag they were made for, so a manual link to one of
        # them completes the tag's step 4 and a new tag drops them.
        self.suggestions = []
        self.suggestion_key = None
        self.suggestion_kind = None
        self.suggested_tag = None


class Linker(object):
    def __init__(self, api, run_gcode, clock=time.monotonic, cache=None,
                 material_temps=None, wake=None, tag_vendor=BAMBU_VENDOR):
        """``wake(seconds)``, when given, asks for another status pass that
        much later: a unit's filament setting waits and retries on a clock,
        not on status changes (C4 §7.5). ``tag_vendor`` is the vendor a Bambu
        tag's new spools are created under (C4 §5.3)."""
        self._api = api
        self._run_gcode = run_gcode
        self._clock = clock
        self._cache = cache if cache is not None else SpoolCache()
        self._material_temps = ops.material_temp_table(material_temps)
        self._tag_vendor = tag_vendor or BAMBU_VENDOR
        self._bays = {}
        self._tray = tray.TraySync(
            run_gcode, clock, self._cache, self._reload_summary,
            self._remember_written, wake=wake,
            material_temps=self._material_temps)
        # bay key -> the spool id the host links it to, and the link's
        # source, from the last status (C4 §4.1: a spool linked to another
        # bay is no candidate).
        self._host_links = {}
        self._host_sources = {}
        self._last_replay = None
        self._pending_pruned = False
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

    async def refresh_linked(self):
        """Re-read the linked spools from Spoolman (C4 §6): its web UI tells
        nobody about an edit of a spool, its filament or its vendor. One
        ``GET /v1/spool`` serves every bay. Returns whether anything a UI or
        the host shows may have changed: a linked spool's summary, or
        Spoolman's reachability. Silent while nothing is linked."""
        ids = set(machine.spool_id for machine in self._bays.values()
                  if machine.spool_id is not None and machine.state in
                  ("linked", "pending_confirmation"))
        ids.update(self._host_links.values())
        if not ids:
            return False
        was_online = self.spoolman_online
        try:
            spools = await self._api.request("GET", "/v1/spool") or []
        except SpoolmanUnavailable:
            self.spoolman_online = False
            return was_online
        except SpoolmanError:
            return False
        self.spoolman_online = True
        changed = self._cache.scan(spools, only=ids)
        return bool(changed) or not was_online

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
        self._cache.clear_pending(key)
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
        if machine.state != "pending_confirmation":
            self._cache.clear_pending(key)
        machine.error = None
        self.spoolman_online = True
        if spool is not None:
            self._cache_summary(spool)
        return self._bay_status_object(key, machine)

    async def duplicates_report(self):
        """The spools that look like one spool's records (C4 §9.2), from one
        ``GET /v1/spool``: the pairs a read found (a spool known by a UID
        while another holds the tag's tray UUID, ``other_tag``), still true,
        and the spools that hold one tray UUID (``shared_tray``) or list one
        UID (``shared_uid``); with them the spools whose tag identity is
        incomplete (``without_tray``, ``invalid_tray``). Read only. Raises SpoolmanUnavailable or
        SpoolmanError."""
        spools = await self._api.request("GET", "/v1/spool") or []
        self.spoolman_online = True
        live = dict((spool["id"], spool) for spool in spools
                    if isinstance(spool, dict) and spool.get("id") is not None)

        def still(entry):
            first, second = entry["spools"]
            tray = entry.get("tray_uid")
            return first in live and second in live \
                and ops.spool_tray(live[second]) == tray \
                and ops.spool_tray(live[first]) in (None, tray)

        self._cache.drop_duplicates(still)
        pairs = [dict(entry, spools=list(entry["spools"]))
                 for entry in self._cache.duplicates]
        seen = set(frozenset(entry["spools"]) for entry in pairs)
        for entry in ops.duplicate_pairs(spools):
            if frozenset(entry["spools"]) not in seen:
                seen.add(frozenset(entry["spools"]))
                pairs.append(entry)
        report = {"duplicates": pairs}
        report.update(ops.tag_gaps(spools))
        return report

    async def merge(self, keep_id, merge_id, confirm=False):
        """Merge spool ``merge_id`` into ``keep_id`` (C4 §9.2), only when
        ``confirm``; without it, say what would be written and change
        nothing. The kept spool gets both spools' UIDs and the tray UUID; the
        other is archived with its tags and location cleared; a bay the host
        links to the archived spool is relinked to the kept one with the same
        source. Weights and usage are not touched. Raises ValueError (the
        same spool, an archived kept spool, two tray UUIDs),
        SpoolmanUnavailable or SpoolmanError."""
        if keep_id == merge_id:
            raise ValueError("keep and merge name the same spool %s" % keep_id)
        keep = await self._api.request("GET", "/v1/spool/%s" % keep_id)
        other = await self._api.request("GET", "/v1/spool/%s" % merge_id)
        self.spoolman_online = True
        if not isinstance(keep, dict) or not isinstance(other, dict):
            raise SpoolmanError(404, "no spool %s or %s" % (keep_id, merge_id))
        if keep.get("archived"):
            raise ValueError("spool %s is archived; restore it in Spoolman "
                             "first" % keep_id)
        plan = ops.merge_plan(keep, other)
        bays = sorted(key for key, spool_id in self._host_links.items()
                      if spool_id == merge_id)
        for key, machine in sorted(self._bays.items()):
            if machine.spool_id == merge_id and key not in bays and \
                    machine.state in ("linked", "pending_confirmation"):
                bays.append(key)
        result = {"keep": keep_id, "merge": merge_id,
                  "card_uids": plan["card_uids"],
                  "tray_uid": plan["tray_uid"], "relink": bays,
                  "confirmed": False}
        if not confirm:
            return result
        kept, _archived = await ops.merge_spools(self._api, keep, other, plan)
        if isinstance(kept, dict):
            self._cache.remember_spool(kept)
        self._cache.drop_duplicates(
            lambda entry: merge_id not in entry["spools"])
        for key in bays:
            machine = self._bay(key)
            unit, _, index = key.rpartition("-")
            source = self._host_sources.get(key) or machine.source
            source = source if source in ("tag", "manual") else "manual"
            await self._run_gcode(
                "OAMSM_SET_BAY_SPOOL OAMS=%s BAY=%s SPOOL=%s SOURCE=%s"
                % (unit, index, keep_id, source))
            self._host_links[key] = keep_id
            self._host_sources[key] = source
            self._cache.clear_pending(key)
            self._drop_suggestions(machine)
            machine.state = "linked"
            machine.spool_id = keep_id
            machine.source = source
            machine.error = None
            machine.link_sent_at = self._clock()
            queued = await self._set_location(keep_id, key, machine)
            machine.mirror_queued = bool(queued)
        result["confirmed"] = True
        return result

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
        if not self._pending_pruned:
            self._pending_pruned = True
            self._prune_pending_bays(status)
        self._host_links = {}
        self._host_sources = {}
        for unit, device in (status.get("devices") or {}).items():
            for bay in device.get("bays") or []:
                if bay.get("spool_id") is not None:
                    bay_key = "%s-%s" % (unit, bay.get("index"))
                    self._host_links[bay_key] = bay["spool_id"]
                    self._host_sources[bay_key] = bay.get("link_source")
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

    def _prune_pending_bays(self, status):
        """The first pass after this component started: a confirmation kept
        for a bay the host no longer reports is dropped (C4 §5.8)."""
        keys = set("%s-%s" % (unit, bay.get("index"))
                   for unit, device in (status.get("devices") or {}).items()
                   for bay in device.get("bays") or [])
        for key in self._cache.pending_bays():
            if key not in keys:
                self._cache.clear_pending(key)

    async def _bay_status(self, key, unit, bay, auto_create, status):
        machine = self._bay(key)
        tag = bay.get("tag")
        previous = machine.last_tag
        previous_uid = machine.read_uid
        tag_state = bay.get("tag_state") or {}
        ready = bay.get("ready", True)
        # A two-tag host publishes only an attributed tag (C2). A bay whose
        # tag is a candidate (restored after a host restart) or in conflict
        # still holds its spool: that is no removal, and when the tag comes
        # back it is no new read either (C4 §3, §4.3).
        limbo = tag is None and tag_state.get("state") in ("candidate",
                                                           "conflict")
        if not machine.seen:
            machine.seen = True
            pending = self._cache.pending_spool(key)
            if pending is not None and not (
                    bay.get("spool_id") == pending
                    and bay.get("link_source") == "tag"):
                # The bay changed while this component was away: the
                # confirmation it kept is for a link the host no longer
                # holds (C4 §5.8).
                self._cache.clear_pending(key)
        startup = machine.startup
        if startup and machine.startup_manual is None:
            machine.startup_manual = bay.get("spool_id") \
                if bay.get("link_source") == "manual" \
                and bay.get("spool_id") is not None else False
        if tag is not None or not ready:
            machine.startup = False
        if limbo:
            candidates = tag_state.get("candidates") or []
            if startup and previous is None and machine.limbo_key is None \
                    and bay.get("spool_id") is not None \
                    and len(candidates) == 1 \
                    and isinstance(candidates[0], dict):
                # No read remembered (a power cycle): the bay's one
                # restored candidate is the tag it waits for, on whichever
                # pass of the startup window it first shows.
                machine.limbo_key = candidates[0].get("spool_key")
        elif tag is None:
            machine.last_tag = None
            machine.read_uid = None
            if not machine.startup:
                machine.limbo_key = None
        else:
            # Keyed by the spool, not the UID: the host adds a sibling UID to
            # ``uids`` without a new ``read_at`` (C4 §4.3), so it is no new
            # tag and never overrides a manual link.
            machine.last_tag = (ops.tag_key(tag), tag.get("read_at"))
            uids = ops.tag_uids(tag)
            machine.read_uid = uids[0] if uids else None
            if machine.limbo_key is not None:
                if previous is None and \
                        machine.limbo_key == machine.last_tag[0]:
                    # The tag the bay held before the restart, confirmed:
                    # the link the host kept stands, a manual one too.
                    previous = machine.last_tag
                    if machine.source == "tag" and machine.state in (
                            "linked", "pending_confirmation"):
                        machine.tag_key = machine.last_tag[0]
                machine.limbo_key = None
            elif startup and previous is None and machine.last_tag[0] \
                    and machine.startup_manual \
                    and bay.get("link_source") == "manual" \
                    and bay.get("spool_id") == machine.startup_manual:
                # No read remembered and no candidate restored (a power
                # cycle with no tag cache): the first tag of the window on
                # a bay the host kept manually linked is no new read. (A
                # unit-decoded summary has no spool key and decides anew.)
                previous = machine.last_tag
        if machine.suggestion_key is not None and not limbo and (
                machine.last_tag is None
                or machine.last_tag[0] != machine.suggestion_key):
            self._drop_suggestions(machine)   # the tag they were for is gone

        # C4 §3: a tag-sourced link whose bay is ready but the tag has
        # disappeared (the previous status had a tag; a re-read found none)
        # means an untagged spool was inserted after the old one was removed.
        # Unlink immediately and clear the location mirror.
        if bay.get("ready", True) and tag is None and previous is not None \
                and not limbo and machine.source == "tag" \
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
        new_read = tag is not None and machine.last_tag != previous
        if new_read and not fresh_adopt and self._actionable(tag) \
                and self._should_resolve(machine, tag):
            await self._resolve(key, unit, bay, tag, machine, auto_create)
            return

        if tag is not None and previous_uid is not None \
                and machine.read_uid != previous_uid:
            # The host read the spool's other tag on this bay: the same
            # spool, so no link changes; its UID joins the spool (C4 §4.3).
            await self._note_sibling(machine)

        if not new_read and not fresh_adopt and machine.state == "resolving" \
                and tag is not None and self._actionable(tag) \
                and not machine.awaiting_decode \
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

    def host_restarted(self):
        """Klipper came back (C4 §3): each bay starts its startup window
        again. A tag read remembered from before is what the bay waits for,
        not a read the host still shows, so the bay is not unlinked while
        the host restores its attribution, and that tag confirmed is no new
        read. A unit-decoded summary has no spool key: unchanged."""
        for machine in self._bays.values():
            machine.startup = True
            machine.startup_manual = None
            if machine.last_tag is not None and machine.last_tag[0]:
                machine.limbo_key = machine.limbo_key or machine.last_tag[0]
                machine.last_tag = None

    def forget_filament(self):
        """Klipper came back and forgot what it was told (C4 §7.4).

        The host keeps these values in memory only, so the next pass has to
        push every bay again.
        """
        for machine in self._bays.values():
            machine.filament_sent = None

    def _bay_filament(self, spool_id):
        """What a bay's spool tells the host, or None (C4 §7.4): the
        material, the temperature to heat to, where that came from, and the
        spool's remaining percent.

        The temperature: first match wins, the spool's own
        ``settings_extruder_temp``, then the material table; else unknown,
        and the host then uses its own default. The remaining percent is the
        spool's remaining weight over its initial weight, 0-100, or unknown;
        only the LCD screen shows it (C2 §6a). A spool with none of the three
        known (and an unlinked bay, or a spool Spoolman has not told us
        about) is None, which clears the bay.
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
            source = "table" if temp is not None else "spool"
        remaining = ops.remaining_pct_of(summary.get("remaining_pct"))
        if temp is None and material is None and remaining is None:
            return None
        return (spool_id, material, temp, source, remaining)

    @staticmethod
    def _filament_command(unit, bay, values):
        """``OAMSM_SET_BAY_FILAMENT`` for ``values``, or the clearing form.

        TEMP and REMAINING go only when known: a host keeps an absent one as
        unknown, and one that predates REMAINING ignores it (C3 §4)."""
        command = "OAMSM_SET_BAY_FILAMENT OAMS=%s BAY=%s" % (unit, bay)
        if values is None:
            return command     # no TEMP and no MATERIAL clears the bay
        parts = [command]
        if values[1]:
            # Quoted so a material with a space stays one argument: Klipper
            # splits extended parameters with shlex (klippy/gcode.py).
            parts.append('MATERIAL="%s"' % values[1].replace('"', ""))
        if values[2] is not None:
            parts.append("TEMP=%d" % values[2])
        parts.append("SOURCE=%s" % values[3])
        if values[4] is not None:
            parts.append("REMAINING=%d" % values[4])
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
        """Whether a read has a link policy at all (C4 §4: tag first).

        Any tag with a spool key is: a decoded Bambu tag through the whole
        resolution order, a bare UID (a Bambu tag attributed before it was
        decoded, or a tag this host cannot decode) through the UIDs in
        ``card_uids`` only (C4 §4.2 step 8)."""
        fmt = tag.get("format")
        if fmt == "spoolman_link":
            return tag.get("spoolman_id") is not None
        if fmt == "unit_summary":
            # Nothing to match without a material (C4 §4.1).
            return bool((tag.get("summary") or {}).get("material"))
        return ops.tag_key(tag) is not None

    @staticmethod
    def _should_resolve(machine, tag):
        """Manual wins until a re-read; only a different spool key beats a
        tag link, so the spool's other tag never does (C4 §4.3).

        A unit-decoded summary has no UID; every new read decides (C4 §4.1,
        whose Keep rule leaves a link that still matches alone)."""
        if tag.get("format") == "unit_summary":
            return True
        if machine.state in ("unlinked", "resolving"):
            return True
        if machine.source == "manual":
            return True
        return machine.tag_key != ops.tag_key(tag)

    async def _resolve(self, key, unit, bay, tag, machine, auto_create):
        machine.awaiting_decode = False
        if tag.get("format") == "unit_summary":
            await self._resolve_summary(key, unit, bay, tag, machine,
                                        auto_create)
            return
        if tag.get("format") == "spoolman_link":
            machine.state = "resolving"
            machine.error = None
            await self._resolve_link_tag(key, unit, bay, tag, machine)
        else:
            await self._resolve_tag(key, unit, bay, tag, machine, auto_create)

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

    @staticmethod
    def _snapshot(machine):
        return (machine.state, machine.spool_id, machine.source, machine.error)

    @staticmethod
    def _restore(machine, before):
        machine.state, machine.spool_id, machine.source, machine.error = before

    async def _resolve_tag(self, key, unit, bay, tag, machine, auto_create):
        """A tag with a spool key (C4 §4.2): the resolution order of
        ``_tag_decide`` against what Spoolman holds now."""
        before = self._snapshot(machine)
        machine.state = "resolving"
        machine.error = None
        try:
            spools = await self._api.request("GET", "/v1/spool") or []
            await self._reachable()
            verdict, payload = await self._tag_decide(key, tag, spools,
                                                      auto_create)
        except SpoolmanUnavailable:
            await self._resolve_offline(key, unit, bay, tag, machine,
                                        auto_create, before)
            return
        await self._apply_tag(key, unit, bay, tag, machine, verdict, payload,
                              spools, before)

    async def _tag_decide(self, key, tag, spools, create):
        """C4 §4.2 for a tag with a spool key. Returns ``(verdict,
        payload)``: ``found`` (the spool), ``uid_only`` (a bare UID no spool
        lists: nothing more is tried), ``archived`` or ``legacy`` (the spools
        to suggest), ``created`` (the new spool) or ``none``. Raises
        SpoolmanUnavailable."""
        spool = ops.match_tag(tag, spools)[0]
        if spool is not None:
            return "found", spool
        if not ops.tag_decoded(tag):
            return "uid_only", None
        try:
            archived = await ops.find_archived(self._api, tag)
        except SpoolmanError:
            archived = None   # a Spoolman that refuses the query: skip it
        if archived is not None:
            return "archived", [archived]
        legacy = ops.legacy_spools(spools, tag, key,
                                   self._linked_elsewhere(key),
                                   self._tag_vendor)
        if legacy:
            return "legacy", legacy
        if not create:
            return "none", None
        vendor = await ops.get_or_create_vendor(self._api, self._tag_vendor)
        filament = await ops.find_or_create_filament(
            self._api, vendor["id"], tag.get("bambu") or {})
        return "created", await ops.create_spool(self._api, filament["id"],
                                                 tag, key)

    async def _apply_tag(self, key, unit, bay, tag, machine, verdict, payload,
                         spools, before):
        if verdict == "found":
            # A known spool: its UID and tray UUID are completed, and it is
            # linked without a confirmation; its weight is Spoolman's.
            spool = await self._known_spool(tag, payload, spools)
            await self._link(key, unit, bay, machine, spool["id"], spool,
                             tag=tag)
        elif verdict == "created":
            await self._link(key, unit, bay, machine, payload["id"], payload,
                             pending=True, tag=tag)
        elif verdict in ("archived", "legacy"):
            self._suggest(bay, machine, tag, verdict, payload, before)
        elif verdict == "uid_only":
            self._wait_for_decode(machine, tag, before)
        else:
            machine.state = "unlinked"
            machine.source = None
            machine.spool_id = None
            machine.error = NO_SPOOL_ERROR

    @staticmethod
    def _was_linked(before):
        return before[0] in ("linked", "pending_confirmation")

    def _wait_for_decode(self, machine, tag, before, error=None):
        """A bare UID no spool lists (C4 §4.2 step 8). A link the bay holds
        stays. Otherwise a Bambu tag waits ``resolving`` for its decode,
        which is a new read; a tag this host cannot decode leaves the bay
        unlinked (UIs offer Assign spool)."""
        if self._was_linked(before):
            self._restore(machine, before)
            return
        machine.source = None
        machine.spool_id = None
        machine.error = error
        if tag.get("format") == "bambu":
            machine.state = "resolving"
            machine.awaiting_decode = error is None
        else:
            machine.state = "unlinked"

    def _suggest(self, bay, machine, tag, kind, spools, before):
        """Offer the spool(s) a tag may be (C4 §4.2 steps 5, 6): the bay
        stays unlinked with them as ``suggestions``; the user's manual link
        to one completes the tag's step 4 (``_adopt``). A link the host
        already holds (a manual one) wins and stays."""
        if bay.get("spool_id") is not None and self._was_linked(before):
            self._restore(machine, before)
            return
        machine.state = "unlinked"
        machine.source = None
        machine.spool_id = None
        if kind == "archived":
            machine.error = ARCHIVED_ERROR % spools[0]["id"]
        elif len(spools) == 1:
            machine.error = LEGACY_ERROR % spools[0]["id"]
        else:
            machine.error = LEGACY_MANY_ERROR % len(spools)
        machine.suggestions = [ops.suggestion_of(spool)
                               for spool in spools[:MAX_SUGGESTIONS]]
        machine.suggestion_kind = kind
        machine.suggestion_key = ops.tag_key(tag)
        machine.suggested_tag = dict(tag)

    @staticmethod
    def _drop_suggestions(machine):
        machine.suggestions = []
        machine.suggestion_key = None
        machine.suggestion_kind = None
        machine.suggested_tag = None

    def _report_duplicate(self, first, second, tray, uid):
        """A spool found by its UID has no tray UUID while another holds the
        tag's (C4 §9.2): two records of one spool. Reported, never merged."""
        if self._cache.add_duplicate(first, second, "other_tag",
                                     tray_uid=tray, uid=uid):
            _LOGGER.warning(
                "openams_spoolman: Spoolman spools %s and %s look like one "
                "spool's two tags; see /server/openams_spoolman/duplicates",
                first, second)

    async def _known_spool(self, tag, spool, spools, unarchive=False):
        """C4 §4.2 step 4 for a spool a tag belongs to: add the UID read on
        the bay if it is new, and backfill the tray UUID if the spool has none
        and no other spool holds it (else report the pair). One PATCH, only
        when something is new; ``unarchive`` restores an archived spool in
        it. Returns the spool as last known."""
        uids = ops.tag_uids(tag)
        uid = uids[0] if uids else None
        tray = ops.tag_tray(tag)
        backfill = None
        if tray and ops.spool_tray(spool) is None:
            holder = ops.tray_holder(spools, tray, other_than=spool["id"])
            if holder is None:
                backfill = tray
            else:
                self._report_duplicate(spool["id"], holder["id"], tray, uid)
        body = {}
        extra = ops.tag_extra(spool, uid, backfill)
        if extra is not None:
            body["extra"] = extra
        if unarchive and spool.get("archived"):
            body["archived"] = False
        if not body:
            return spool
        try:
            written = await self._api.request(
                "PATCH", "/v1/spool/%d" % spool["id"], body=body)
        except SpoolmanUnavailable:
            self.spoolman_online = False
            if uid and uid not in ops.spool_uids(spool):
                # Keep the link; the UID mirror is queued (C4 §6).
                self._cache.enqueue({"op": "card_uid",
                                     "spool_id": spool["id"], "uid": uid})
            return spool
        except SpoolmanError:
            return spool   # keep the link; the UID mirror waits
        return written if isinstance(written, dict) else spool

    async def _note_sibling(self, machine):
        """The UID read on a tag-linked bay changed to another UID of the
        same spool key (C4 §4.3): add it to the linked spool. No link
        command, no location write, no active-spool change."""
        if machine.state not in ("linked", "pending_confirmation") \
                or machine.source != "tag" or machine.spool_id is None \
                or machine.last_tag is None \
                or machine.last_tag[0] != machine.tag_key:
            return
        uid = machine.read_uid
        spool_id = machine.spool_id
        if not uid or self._cache.lookup_uid(uid) == spool_id:
            return
        try:
            spool = await self._api.request("GET", "/v1/spool/%s" % spool_id)
            extra = ops.tag_extra(spool, uid) if isinstance(spool, dict) \
                else None
            if extra is not None:
                await self._api.request("PATCH", "/v1/spool/%s" % spool_id,
                                        body={"extra": extra})
            await self._reachable()
        except SpoolmanUnavailable:
            self.spoolman_online = False
            self._cache.enqueue({"op": "card_uid", "spool_id": spool_id,
                                 "uid": uid})
        except SpoolmanError:
            return
        self._cache.remember_uid(uid, spool_id)

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
        # The vendor is not compared: a spool the user filed under another
        # brand is still the spool in the bay (C4 §4.1, Keep).
        return ops.summary_matches(spool_summary,
                                   dict(summary, manufacturer=None))

    async def _summary_decide(self, key, summary, create):
        """Match, then create (C4 §4.1). Returns ``(verdict, payload)``:
        ``link`` (the one spool), ``ambiguous`` (the candidates), ``none``, or
        ``created`` (the new spool). Raises SpoolmanUnavailable."""
        spools = await self._api.request("GET", "/v1/spool") or []
        excluded = self._linked_elsewhere(key)
        candidates = ops.matching_spools(spools, summary, excluded=excluded,
                                         bambu_vendor=self._tag_vendor)
        # Spoolman's own data wins over what the unit's code says of the
        # maker: a spool already located at this bay, under any vendor, is
        # preferred, then the one this bay and summary resolved to before.
        anyone = ops.matching_spools(
            spools, dict(summary, manufacturer=None), excluded=excluded)
        remembered = self._cache.lookup_summary(key, summary)
        pool = [spool for spool in anyone if spool.get("location") == key] \
            or [spool for spool in anyone if spool.get("id") == remembered] \
            or candidates
        if len(pool) == 1:
            return "link", pool[0]
        if len(pool) > 1:
            return "ambiguous", pool
        if not create:
            return "none", None
        return "created", await ops.create_summary_spool(
            self._api, summary, key, self._tag_vendor)

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
        self._drop_suggestions(machine)
        machine.suggestions = [
            ops.suggestion_of(spool)
            for spool in candidates[:MAX_SUGGESTIONS]]
        if machine.suggestions:
            machine.suggestion_kind = "summary"

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

    async def _resolve_offline(self, key, unit, bay, tag, machine, auto_create,
                               before=None):
        """Spoolman is away: link the cached spool, else queue the create (C4 §6).

        The cache answers in the order of C4 §4 (``SpoolCache.lookup``), and
        a queued creation replays through the whole order, so the two tags of
        one spool end as one spool. Without ``auto_create_spoolman_spools``
        there is nothing to queue (the
        online counterpart would go straight to ``unlinked``, C4 §3); the bay
        stays ``resolving`` with the plain offline error instead so the retry
        branch in ``_bay_status`` re-tries it once Spoolman answers again,
        exactly as if creation were merely delayed rather than off (contract
        silent on this combination; simplest reading). A bare UID queues
        nothing (C4 §4.2 step 8): a Bambu one waits, retried once Spoolman
        answers; any other leaves the bay as it was.
        """
        self.spoolman_online = False
        spool_id = self._cache.lookup(tag)
        if spool_id is not None:
            uids = ops.tag_uids(tag)
            if uids and self._cache.lookup_uid(uids[0]) != spool_id:
                # Found by the tray UUID or a sibling: the read UID joins the
                # spool once Spoolman answers (C4 §4.2 step 4).
                self._cache.enqueue({"op": "card_uid", "spool_id": spool_id,
                                     "uid": uids[0]})
                self._cache.remember_uid(uids[0], spool_id)
            await self._link(key, unit, bay, machine, spool_id, tag=tag)
            return
        if not ops.tag_decoded(tag):
            self._wait_for_decode(machine, tag, before or
                                  ("unlinked", None, None, None),
                                  error=OFFLINE_ERROR)
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
        self._drop_suggestions(machine)
        machine.awaiting_decode = False
        machine.state = "pending_confirmation" if pending else "linked"
        machine.source = "tag"
        machine.spool_id = spool_id
        if pending:
            self._cache.set_pending(key, spool_id)
        else:
            self._cache.clear_pending(key)
        machine.tag_key = machine.last_tag[0] if machine.last_tag else None
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
            await self._clear_location(key, old, machine)
        queued = await self._set_location(spool_id, key, machine)
        machine.mirror_queued = bool(queued)

    async def _clear_location(self, key, spool_id, machine=None):
        """Clear a spool's ``location`` as bay ``key`` lets go of it (C4
        §5.6), unless another bay links it now: on a move the new bay may have
        linked it first, and its location then stays (C4 §4.4)."""
        if spool_id in self._linked_elsewhere(key):
            return False
        return await self._set_location(spool_id, "", machine)

    async def _adopt(self, key, machine, host_spool, host_source, tag=None):
        """Follow the host's authoritative link (C4 §3, first row)."""
        source = host_source if host_source in ("tag", "manual") else "manual"
        # A tag link the user had not confirmed before Moonraker restarted
        # still waits for that confirmation.
        pending = source == "tag" and \
            self._cache.pending_spool(key) == host_spool
        if machine.state == "linked" and machine.spool_id == host_spool \
                and machine.source == source and not pending:
            return
        if machine.state == "pending_confirmation" and \
                machine.spool_id == host_spool and source == "tag":
            return   # the host echoes our link; the display has yet to confirm
        if not pending:
            self._cache.clear_pending(key)
        old = machine.spool_id
        # A manual link to a spool this bay's tag suggested is the user's
        # answer (C4 §4.2): the tag's step 4 runs for it.
        accepted = None
        if machine.suggestion_kind in ("legacy", "archived") and host_spool \
                in [entry.get("spool_id") for entry in machine.suggestions]:
            accepted = (machine.suggestion_kind, machine.suggested_tag)
        self._drop_suggestions(machine)
        machine.awaiting_decode = False
        machine.state = "pending_confirmation" if pending else "linked"
        machine.source = source
        machine.spool_id = host_spool
        machine.error = None
        if source == "tag" and machine.last_tag is not None:
            machine.tag_key = machine.last_tag[0]
        if old != host_spool:
            if old is not None:
                await self._clear_location(key, old, machine)
            queued = await self._set_location(host_spool, key, machine)
            machine.mirror_queued = bool(queued)
        if accepted is not None:
            await self._accept_suggestion(host_spool, *accepted)
        await self._refresh_summary(host_spool, reload=True,
                                    tag=tag if source == "tag" else None)

    async def _accept_suggestion(self, spool_id, kind, tag):
        """The user linked a spool the bay's tag suggested (C4 §4.2): add the
        tag's UID and backfill its tray UUID (step 4), restoring an archived
        spool. Spoolman away: the UID is queued; nothing else is lost, since
        the next read of the tag completes the rest."""
        uids = ops.tag_uids(tag)
        try:
            spools = await self._api.request("GET", "/v1/spool") or []
            spool = await self._api.request("GET", "/v1/spool/%s" % spool_id)
            await self._reachable()
        except SpoolmanUnavailable:
            self.spoolman_online = False
            if uids:
                self._cache.enqueue({"op": "card_uid", "spool_id": spool_id,
                                     "uid": uids[0]})
            return
        except SpoolmanError:
            return
        if not isinstance(spool, dict):
            return
        spool = await self._known_spool(tag, spool, spools,
                                        unarchive=kind == "archived")
        self._cache.remember(tag, spool)

    async def _unlink(self, unit, bay, machine):
        """Removal: clear the host's link and the location mirror (C4 §3)."""
        key = "%s-%s" % (unit, bay.get("index"))
        self._cache.clear_pending(key)
        old = machine.spool_id
        self._drop_suggestions(machine)
        machine.awaiting_decode = False
        machine.state = "unlinked"
        machine.source = None
        machine.spool_id = None
        machine.tag_key = None
        machine.error = None
        machine.mirror_queued = False
        await self._run_gcode("OAMSM_CLEAR_BAY_SPOOL OAMS=%s BAY=%s"
                              % (unit, bay.get("index")))
        if old is not None:
            await self._clear_location(key, old, machine)

    async def _reconcile(self, machine):
        """The host's link vanished: follow it to ``unlinked`` (C4 §3)."""
        mine = None
        for key, other in self._bays.items():
            if other is machine:
                mine = key
                self._cache.clear_pending(key)
        old = machine.spool_id
        self._drop_suggestions(machine)
        machine.awaiting_decode = False
        machine.state = "unlinked"
        machine.source = None
        machine.spool_id = None
        machine.tag_key = None
        machine.error = None
        machine.mirror_queued = False
        if old is not None:
            # The host moves a link: the bay that took the spool keeps its
            # location (C4 §4.4).
            await self._clear_location(mine, old, machine)

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
        """Replay one queued creation through the whole resolution order of
        C4 §4 (``_tag_decide``), so a spool made meanwhile, by another tool
        or by the replay of the spool's other tag, is linked rather than
        duplicated (C4 §6)."""
        key, unit, bay_index, tag = (entry.get("key"), entry.get("unit"),
                                     entry.get("bay"), entry.get("tag") or {})
        machine = self._bay(key)
        bay = {"index": bay_index}
        if tag.get("format") == "unit_summary":
            await self._replay_create_summary(key, unit, bay, tag, machine)
            return
        spools = await self._api.request("GET", "/v1/spool") or []
        # Only a bay that still resolves this key gets a new spool: one made
        # for a bay that moved on would name, as its location, a bay it is not
        # in.
        verdict, payload = await self._tag_decide(
            key, tag, spools, self._resolving(machine, tag))
        if not self._resolving(machine, tag):
            # The bay moved on or the spool was removed while this waited
            # (C4 §6); remember the spool for later lookups but do not link.
            if verdict in ("found", "created"):
                self._cache.remember(tag, payload)
            if verdict == "created":
                # ... even while the spool was being made
                await ops.set_location(self._api, payload["id"], "")
            return
        await self._apply_tag(key, unit, bay, tag, machine, verdict, payload,
                              spools, self._snapshot(machine))

    @staticmethod
    def _resolving(machine, tag):
        """The bay still waits for this tag's spool."""
        return machine.state == "resolving" and \
            machine.last_tag is not None and \
            machine.last_tag[0] == ops.tag_key(tag)

    async def _replay_create_summary(self, key, unit, bay, tag, machine):
        """Replay a queued unit-decoded creation (C4 §4.1, §6): the whole
        policy again, so a spool made meanwhile is matched rather than
        duplicated. A read that is no longer the bay's current one is dropped
        without creating anything, since the summary says nothing durable."""
        if not (machine.state == "resolving"
                and machine.last_tag == (ops.tag_key(tag),
                                         tag.get("read_at"))):
            return
        verdict, payload = await self._summary_decide(
            key, self._summary_for(key, tag)[0], True)
        await self._apply_summary(key, unit, bay, tag, machine, verdict,
                                  payload, False)
