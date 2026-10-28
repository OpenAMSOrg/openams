# The offline cache and write queue of the openams_spoolman component (C4 §6).
#
# One JSON-safe state holds the tag keys (a tag UID or a Bambu tray UID -> spool
# id), the spool summaries the component's status publishes, and the write queue
# that is replayed when Spoolman answers again. The persistence hooks are
# injected, so the Moonraker glue can pass a database and the tests a dict.

from . import ops
from .api import SpoolmanUnavailable


def _uids(value):
    """A ``card_uids`` extra value as a list of upper-case UIDs."""
    if value is None:
        return []
    return [part.strip().upper() for part in str(value).split(",")
            if part.strip()]


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

    def state(self):
        """The JSON-safe state to persist: the keys, summaries, written
        settings and queue."""
        return {"keys": dict(self._keys),
                "summaries": dict(self._summaries),
                "written": dict((key, dict(value))
                                for key, value in self._written.items()),
                "queue": [dict(entry) for entry in self.queue]}

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
        """The spool id a tag is cached under, or None: UID first, then tray."""
        tag = tag or {}
        uid = str(tag.get("uid") or "").strip().upper()
        if uid and ("uid:" + uid) in self._keys:
            return self._keys["uid:" + uid]
        tray = (tag.get("bambu") or {}).get("tray_uid")
        if tray:
            key = "tray:" + str(tray).strip().lower()
            if key in self._keys:
                return self._keys[key]
        return None

    def remember(self, tag, spool):
        """Cache a resolved spool under the tag's UID, and under its Bambu
        tray key when the spool carries that tray UID: online, a tray UID
        finds only a spool that has it (C4 §4), and a UID found a spool
        first."""
        tag = tag or {}
        uid = str(tag.get("uid") or "").strip().upper()
        tray = (tag.get("bambu") or {}).get("tray_uid")
        own = ops.decode_extra(
            ((spool or {}).get("extra") or {}).get("tray_uid"))
        if str(own or "").strip().lower() != str(tray or "").strip().lower():
            tray = None
        self._remember(spool, [uid] if uid else [], tray)

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
            extra = spool.get("extra") or {}
            for uid in _uids(ops.decode_extra(extra.get("card_uids"))):
                uids.setdefault("uid:" + uid, spool["id"])
            tray = ops.decode_extra(extra.get("tray_uid"))
            if tray:
                trays.setdefault("tray:" + str(tray).strip().lower(),
                                 spool["id"])
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
