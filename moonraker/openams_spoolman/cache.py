# The offline cache and write queue of the openams_spoolman component (C4 §6).
#
# One JSON-safe state holds the tag keys (a tag UID or a Bambu tray UID -> spool
# id), the spool summaries the component's status publishes, and the write queue
# that is replayed when Spoolman answers again. The persistence hooks are
# injected, so the Moonraker glue can pass a database and the tests a dict.

from . import ops
from .api import SpoolmanUnavailable


class SpoolCache(object):
    """The read cache and the write queue (C4 §6)."""

    def __init__(self, load=None, save=None):
        self._load = load
        self._save = save
        self._keys = {}
        self._summaries = {}
        # bay key -> {"fingerprint", "spool_id"}: the filament setting the
        # component last wrote into that bay's unit, and from which spool
        # (C4 §7.5).
        self._written = {}
        self.queue = []
        # bay key -> spool id: a tag link that still waits for the user's
        # confirmation (C4 §5.8). Kept on disk, so a Moonraker restart asks
        # again instead of taking the host's link as confirmed.
        self._pending = {}
        # Pairs of spools that hold the two tags of one spool, found when a
        # read could not backfill the tray UUID (C4 §9.2): reported, never
        # merged automatically. ``{"spools": [x, y], "reason", "tray_uid",
        # "uid"}`` entries.
        self.duplicates = []
        self._prewarmed = False
        state = load() if load is not None else None
        if isinstance(state, dict):
            self._keys = dict(state.get("keys") or {})
            self._summaries = dict(state.get("summaries") or {})
            self._written = dict((key, dict(value)) for key, value in
                                 (state.get("written") or {}).items()
                                 if isinstance(value, dict))
            self.queue = [entry for entry in (state.get("queue") or [])
                          if isinstance(entry, dict)]
            self._pending = dict(
                (key, value) for key, value in
                (state.get("pending") or {}).items()
                if isinstance(value, int) and not isinstance(value, bool))
            self.duplicates = [
                dict(entry) for entry in (state.get("duplicates") or [])
                if isinstance(entry, dict)
                and isinstance(entry.get("spools"), list)
                and len(entry["spools"]) == 2]

    def state(self):
        """The JSON-safe state to persist: the keys, summaries, written
        settings, queue, pending confirmations and reported duplicates."""
        return {"keys": dict(self._keys),
                "summaries": dict(self._summaries),
                "written": dict((key, dict(value))
                                for key, value in self._written.items()),
                "queue": [dict(entry) for entry in self.queue],
                "pending": dict(self._pending),
                "duplicates": [dict(entry, spools=list(entry["spools"]))
                               for entry in self.duplicates]}

    def add_duplicate(self, first, second, reason, tray_uid=None, uid=None):
        """Report two spools as one spool's two records (C4 §9.2); each
        unordered pair once. Returns whether the pair is new."""
        pair = set((first, second))
        if first == second or any(set(entry["spools"]) == pair
                                  for entry in self.duplicates):
            return False
        self.duplicates.append({"spools": [first, second], "reason": reason,
                                "tray_uid": tray_uid, "uid": uid})
        self.persist()
        return True

    def drop_duplicates(self, keep):
        """Keep only the reported pairs ``keep(entry)`` accepts."""
        kept = [entry for entry in self.duplicates if keep(entry)]
        if len(kept) != len(self.duplicates):
            self.duplicates = kept
            self.persist()

    def pending_spool(self, bay_key):
        """The spool a bay's tag link waits on the user to confirm, or None."""
        return self._pending.get(bay_key)

    def set_pending(self, bay_key, spool_id):
        if self._pending.get(bay_key) != spool_id:
            self._pending[bay_key] = spool_id
            self.persist()

    def clear_pending(self, bay_key):
        if self._pending.pop(bay_key, None) is not None:
            self.persist()

    def remember_written(self, bay_key, summary, spool_id):
        """The component wrote a filament setting (in ``ops.summary_of``'s
        form) into a bay's unit from ``spool_id`` (C4 §7.5). One per bay:
        the newest replaces the last."""
        self._written[bay_key] = {
            "fingerprint": ops.summary_fingerprint(summary),
            "spool_id": spool_id}
        self.persist()

    def written_spool(self, bay_key, summary):
        """The spool a bay's unit-decoded summary was written from, when
        it is the setting the component last wrote there; else None."""
        entry = self._written.get(bay_key)
        if entry is None or \
                entry.get("fingerprint") != ops.summary_fingerprint(summary):
            return None
        return entry.get("spool_id")

    def persist(self):
        if self._save is not None:
            self._save(self.state())

    def lookup(self, tag):
        """The spool id a tag is cached under, or None, in the order of C4 §4
        (``ops.match_tag``): the UID read on the bay, then the valid tray
        UUID, then a sibling UID the host publishes for the spool key."""
        uids = ops.tag_uids(tag)
        if uids and ("uid:" + uids[0]) in self._keys:
            return self._keys["uid:" + uids[0]]
        tray = ops.tag_tray(tag)
        if tray and ("tray:" + tray) in self._keys:
            return self._keys["tray:" + tray]
        if not (tag or {}).get("shared_identity"):
            for uid in uids[1:]:
                if ("uid:" + uid) in self._keys:
                    return self._keys["uid:" + uid]
        return None

    def lookup_uid(self, uid):
        """The spool id a tag UID is cached under, or None."""
        value = str(uid or "").strip().upper()
        return self._keys.get("uid:" + value) if value else None

    def remember_uid(self, uid, spool_id):
        """Cache one tag UID for a spool whose summary is not at hand: a
        sibling UID added to the linked spool (C4 §4.3)."""
        value = str(uid or "").strip().upper()
        if value and spool_id is not None and \
                self._keys.get("uid:" + value) != spool_id:
            self._keys["uid:" + value] = spool_id
            self.persist()

    def remember(self, tag, spool):
        """Cache a resolved spool under the tag's UID, and under its Bambu
        tray key when the spool carries that tray UID: online, a tray UID
        finds only a spool that has it (C4 §4), and a UID found a spool
        first."""
        uids = ops.tag_uids(tag)
        tray = ops.tag_tray(tag)
        if tray != ops.spool_tray(spool):
            tray = None
        # Only the UID read on the bay: it is the one the spool now lists.
        self._remember(spool, uids[:1], tray)

    def remember_spool(self, spool):
        """Cache a spool under every tag it carries now (its ``card_uids``
        and valid ``tray_uid``), e.g. after a merge (C4 §9.2)."""
        self._remember(spool, ops.spool_uids(spool), ops.spool_tray(spool))

    def lookup_summary(self, bay_key, summary):
        """The spool id a bay's unit-decoded summary is cached under (C4 §6)."""
        return self._keys.get(self._summary_key(bay_key, summary))

    def remember_summary(self, bay_key, summary, spool_id):
        """Cache the spool a bay's summary resolved to; the key is the bay and
        the summary, since a summary carries no UID (C4 §4.1)."""
        if spool_id is None:
            return
        self._keys[self._summary_key(bay_key, summary)] = spool_id
        self.persist()

    @staticmethod
    def _summary_key(bay_key, summary):
        return "sum:%s:%s" % (bay_key, ops.summary_fingerprint(summary))

    def _remember(self, spool, uids, tray_uid, persist=True):
        spool_id = spool.get("id") if isinstance(spool, dict) else None
        if spool_id is None:
            return
        for uid in uids:
            value = str(uid or "").strip().upper()
            if value:
                self._keys["uid:" + value] = spool_id
        if tray_uid:
            self._keys["tray:" + str(tray_uid).strip().lower()] = spool_id
        self._summaries[str(spool_id)] = ops.spool_summary(spool)
        if persist:
            self.persist()

    def summary(self, spool_id):
        """The cached C4 §8 summary of a spool, or None."""
        return self._summaries.get(str(spool_id))

    def enqueue(self, entry):
        """Append a write to the replay queue (C4 §6)."""
        self.queue.append(entry)
        self.persist()

    def discard(self, entry):
        """Drop a replayed write; ``entry`` must be the queued object."""
        for index, item in enumerate(self.queue):
            if item is entry:
                del self.queue[index]
                self.persist()
                return

    def scan(self, spools, only=None):
        """Learn from a full ``GET /v1/spool`` (C4 §6): the tag keys of every
        spool, and the summaries of all of them or, with ``only``, of those
        ids. Returns the ids whose summary changed.

        A tag key follows what Spoolman holds, in the order §4 resolves: a
        UID listed in ``card_uids`` and a ``tray_uid`` each belong to the
        first spool that carries them, and a key whose spool no longer
        carries the tag is dropped, unless a queued UID write still has to
        put it there."""
        uids, trays, ids = {}, {}, set()
        for spool in spools:
            if not isinstance(spool, dict) or spool.get("id") is None:
                continue
            ids.add(spool["id"])
            for uid in ops.spool_uids(spool):
                uids.setdefault("uid:" + uid, spool["id"])
            tray = ops.spool_tray(spool)
            if tray:
                trays.setdefault("tray:" + tray, spool["id"])
        queued = set("uid:" + str(entry.get("uid") or "").strip().upper()
                     for entry in self.queue if entry.get("op") == "card_uid")
        keys = dict(self._keys)
        for key, spool_id in self._keys.items():
            if key.startswith(("uid:", "tray:")) and spool_id in ids \
                    and key not in queued:
                del keys[key]
        keys.update(trays)
        keys.update(uids)
        changed = set()
        summaries = dict(self._summaries)
        for spool in spools:
            if not isinstance(spool, dict) or spool.get("id") is None:
                continue
            if only is not None and spool["id"] not in only:
                continue
            summary = ops.spool_summary(spool)
            if summaries.get(str(spool["id"])) != summary:
                summaries[str(spool["id"])] = summary
                changed.add(spool["id"])
        if keys != self._keys or summaries != self._summaries:
            self._keys = keys
            self._summaries = summaries
            self.persist()   # once for the whole scan, not once per spool
        return changed

    async def prewarm(self, api):
        """Remember every spool Spoolman holds, once per component start."""
        if self._prewarmed:
            return
        self._prewarmed = True
        try:
            spools = await api.request("GET", "/v1/spool") or []
        except SpoolmanUnavailable:
            self._prewarmed = False
            return
        self.scan(spools)
