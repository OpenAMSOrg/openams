# OpenAMS RFID tag reader service (S4b) and tag attribution.
#
# Copyright (C) 2026 JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# One TagReader per OAMS unit, owned by its driver (src/oams.py). It keeps a
# per-bay tag (docs/contracts/C2_status.md §4.6) that the driver publishes as
# bays[i].tag, and, on a unit whose reads the host attributes, a per-bay
# tag_state (C2 §4.5).
#
# A reader's field can cover several bays (an OpenAMS 1 mainboard has two
# readers for four bays; [mfrc522] in src/mfrc522.py hands every sample in
# through note_field). Which bay a tag belongs to is decided by the pure
# Attribution class below, from spool motion, never from a single read:
#
#   * a tag is identified by its spool key (tag_identity.spool_key): both tags
#     of a Bambu spool share one;
#   * a UID whose presence changes while exactly one covered bay turns belongs
#     to that bay; timing (the bay that was just inserted) is weak evidence
#     that gives a candidate, unless the other covered bays are accounted for;
#   * a tag leaving the field is never a removal: a bay's tag clears when its
#     ready falls outside a runout, or when another spool is attributed there.
#
# A unit that reads and decodes its spools' tags itself (C1 section 4.7.1,
# AMS_CAP_TRAY_INFO) reports a decoded summary per bay instead; note_tray_info
# turns that into a "unit_summary" tag (C2 section 4.6), which carries no UID.
# With AMS_CAP_TRAY_READ the insertion/startup triggers and OAMS_RFID_SCAN ask
# the unit to read (ams_tray_read). Such a unit attributes its own reads.

import json
import logging
import os
from collections import OrderedDict

try:                                  # deployed as a Klipper extras package
    from . import tag_identity
except ImportError:                   # imported standalone (src/ on sys.path)
    import tag_identity

# Busy retries (C1 §4.7), 1 s apart, up to 5 times.
_BUSY_RETRIES = 5
_BUSY_INTERVAL_S = 1.0

# C2 §4.6: an insertion trigger waits for positioning to end before reading.
_POSITIONING_POLL_S = 0.5
_POSITIONING_TIMEOUT_S = 60.0

# A unit-decoded read (ams_tray_read) may turn the spool past the reader, so
# the host waits this long for its outcome (ams_tray_info) before it lets the
# next read go.
_TRAY_READ_TIMEOUT_S = 120.0
# How long after the first hardware snapshot a startup read waits, so the
# unit's answers to the connect-time ams_get_tray_info queries are in.
_TRAY_STARTUP_DELAY_S = 2.0

# ams_tray_info state values (C1 section 4.7.1).
TRAY_NO_SPOOL, TRAY_PRESENT, TRAY_READING, TRAY_NO_TAG, TRAY_DECODED, \
    TRAY_FAILED = range(6)

# ---------------------------------------------------------------- attribution
# Starting values of the thresholds still to be measured on the hardware
# (C2 §4.6.1). Every one is an Attribution argument; [mfrc522] exposes them
# as options.
#: A bay is "handled" this long after its ready rises (the user's hands are on
#: that spool).
T_HAND_S = 30.0
#: A field change with no covered bay turning or handled is remembered this
#: long, and becomes insertion evidence if exactly one covered bay is then
#: inserted.
ORPHAN_S = 120.0
#: A UID present in every good sample across this many samples of one bay's
#: turn is not that bay's tag.
N_STATIC = 8
#: Consecutive good samples a UID must be present (absent) before it counts as
#: entering (leaving) the field, so a one-sample dropout is not a change.
FIELD_DEBOUNCE = 2
#: A ready bay that turned this long in all (loads, unloads), sampled, without
#: any tag showing is taken to hold a spool with no tag.
NO_TAG_TURN_S = 30.0
#: How often a kept tag (a runout's empty spool) is checked for its deferred
#: clear.
_KEPT_CHECK_S = 2.0

# Per-bay states (C2 §4.5 tag_state.state).
NONE, UNKNOWN, CANDIDATE, ATTRIBUTED, CONFLICT = (
    "none", "unknown", "candidate", "attributed", "conflict")
# Conflict reasons (C2 §4.5 tag_state.reason).
TWO_MOVING = "two_moving"
CLAIMED_TWICE = "claimed_twice"
CHANGED_IN_PLACE = "changed_in_place"
AMBIGUOUS_PAIR = "ambiguous_pair"
NO_TAG_AFTER_TURN = "no_tag_after_turn"
# Evidence (C2 §4.6 tag.evidence).
MOTION, ELIMINATION, SINGLE, INSERTION, ASSERTED, UNIT = (
    "motion", "elimination", "single", "insertion", "asserted", "unit")
#: Evidence that comes from the bay itself: it may stand next to another bay
#: holding the same key (the clone rule), and it overrides indirect evidence.
DIRECT = (MOTION, SINGLE, ASSERTED)

#: A MIFARE Classic tag whose Bambu decode failed this many times is taken
#: for a tag that is not Bambu's (``unknown``), and not decoded again.
DECODE_ATTEMPTS = 3

#: The tag cache keeps at most this many UIDs (the least recently seen go).
CACHE_LIMIT = 256
CACHE_FILE = "openams_tag_cache.json"
CACHE_VERSION = 1


def _tag_dict(uid_hex, fmt, read_at, spoolman_id=None, bambu=None,
             openspool=None, summary=None):
    """C2 §4.6's tag object, exactly: ``openspool`` and ``summary`` only when
    they are not None."""
    tag = {"uid": uid_hex, "format": fmt, "read_at": read_at,
           "spoolman_id": spoolman_id, "bambu": bambu}
    if openspool is not None:
        tag["openspool"] = openspool
    if summary is not None:
        tag["summary"] = summary
    return tag


def tray_summary(record):
    """C2 §4.6's ``summary`` object from an ``ams_tray_info`` record.

    ``color_rgba`` (0xRRGGBBAA on the wire) becomes ``color_argb``
    (0xAARRGGBB), the form the Bambu tag's ``color_argb`` already has; all
    zero means the unit reported no colour. A blank text or a zero
    temperature is None. ``manufacturer`` is None: a unit's summary does not
    say who made the spool. A ``GF`` code is a Bambu Studio *preset*, not a
    maker (``GFB60`` is Polymaker PolyLite ABS, ``GF?99`` the generic
    presets), and a stored per-bay setting carries no tag at all.
    """
    code = record.get("code") or None
    rgba = int(record.get("color_rgba") or 0) & 0xFFFFFFFF
    argb = ((rgba & 0xFF) << 24) | (rgba >> 8) if rgba else None
    return {"code": code,
            "material": record.get("material") or None,
            "color_argb": argb,
            "nozzle_min_c": record.get("nozzle_min_c") or None,
            "nozzle_max_c": record.get("nozzle_max_c") or None,
            "manufacturer": None}


def tag_record(fmt, bambu=None, spoolman_id=None, openspool=None):
    """The decoded content of one tag, as the tag cache keeps it."""
    return {"format": fmt, "bambu": bambu, "spoolman_id": spoolman_id,
            "openspool": openspool}


def _norm_uid(uid):
    if isinstance(uid, (bytes, bytearray)):
        return bytes(uid).hex()
    return str(uid).strip().lower()


def _material_and_color(record):
    """(material, color_argb) of a decoded record, for tag_state candidates."""
    if not record:
        return None, None
    bambu = record.get("bambu")
    if isinstance(bambu, dict):
        return (bambu.get("type") or None,
                bambu.get("color_argb"))
    openspool = record.get("openspool")
    if isinstance(openspool, dict):
        color = None
        text = str(openspool.get("color_hex") or "").strip().lstrip("#")
        if len(text) == 6:
            try:
                color = 0xFF000000 | int(text, 16)
            except ValueError:
                color = None
        return openspool.get("type") or None, color
    return None, None


class TagCache(object):
    """UID -> decoded record, and through it spool key -> UIDs (C2 §4.6.1).

    A UID decoded once is known: its siblings (the other tag of the same
    spool) resolve to the same key. Pure; the TagReader persists it."""

    def __init__(self, limit=CACHE_LIMIT):
        self.limit = limit
        self._records = OrderedDict()
        #: Counts the changes, so a caller can tell cheaply that nothing did.
        self.version = 0

    def record(self, uid):
        return self._records.get(_norm_uid(uid))

    def put(self, uid, record):
        """Remember ``record`` for ``uid``; True when anything changed."""
        uid = _norm_uid(uid)
        if record is None:
            return False
        record = tag_record(record.get("format") or "unknown",
                            bambu=record.get("bambu"),
                            spoolman_id=record.get("spoolman_id"),
                            openspool=record.get("openspool"))
        existing = self._records.get(uid)
        if (existing is not None and isinstance(existing.get("bambu"), dict)
                and not isinstance(record.get("bambu"), dict)
                and record.get("format") in ("bambu", "unknown")):
            return False      # a decoded Bambu tag stays decoded
        changed = existing != record
        self._records[uid] = record
        self._records.move_to_end(uid)
        while len(self._records) > self.limit:
            self._records.popitem(last=False)
        if changed:
            self.version += 1
        return changed

    def drop(self, uid):
        if self._records.pop(_norm_uid(uid), None) is None:
            return False
        self.version += 1
        return True

    def key_of(self, uid):
        """The spool key of ``uid``: from its record when decoded, else
        ``uid:<UID>``."""
        uid = _norm_uid(uid)
        record = self._records.get(uid) or {}
        return tag_identity.spool_key({"uid": uid,
                                       "format": record.get("format"),
                                       "bambu": record.get("bambu")})

    def uids_of(self, key):
        return [uid for uid in self._records if self.key_of(uid) == key]

    def bambu_record_for_key(self, key):
        """A decoded Bambu record of any UID with ``key``, or None."""
        if not key or not key.startswith("bambu:"):
            return None
        for uid, record in self._records.items():
            if (isinstance(record.get("bambu"), dict)
                    and self.key_of(uid) == key):
                return record
        return None

    def dump(self):
        return {uid: dict(record) for uid, record in self._records.items()}

    def load(self, records):
        self._records = OrderedDict()
        self.version += 1
        for uid, record in (records or {}).items():
            if isinstance(record, dict):
                self.put(uid, record)


class _Reader(object):
    """One reader field, as the host sees it."""

    def __init__(self, index, bays, enumerates, debounce):
        self.index = index
        self.bays = tuple(bays)
        #: False for a reader that answers one UID per scan (it cannot show a
        #: tag leaving, and an insertion read is taken as before, C1 §4.7).
        self.enumerates = enumerates
        self.debounce = max(1, int(debounce))
        self.inset = frozenset()       # the debounced field
        self.raw = None                # the last good sample, or None
        self.present_count = {}
        self.absent_count = {}
        self.times = []                # times of the last good samples
        self.errors = 0
        self.orphans = []              # (time, uid, since)


class _Bay(object):
    """One bay's attribution state."""

    def __init__(self, index):
        self.index = index
        self.ready = False
        self.rise_at = None
        self.fall_at = None
        self.turning = False
        self.turn_start = None
        self.turn_end = None
        #: The bay is loaded and follows the extruder: its spool turns as the
        #: printer pulls, so it is not still, yet nobody "turns" it.
        self.following = False
        self.follow_end = None
        #: Just inserted, its insertion read still to come (a reader that is
        #: read only when asked).
        self.awaiting_read = False
        self.turn_keys = {}            # key -> uid seen by motion this turn
        self.static = {}               # reader -> [common, count, moved, fired]
        self.reset()

    def reset(self):
        self.state = UNKNOWN if self.ready else NONE
        self.key = None
        self.uid = None
        self.evidence = None
        self.read_at = None
        self.confirmed_at = None
        self.ready_fell = False
        self.kept = False
        self.candidates = []           # [(key, uid)], at most two
        self.reason = None
        self.other_bay = None
        self.restored = None           # (key, uid, read_at) of a restart
        self.silent_s = 0.0            # turned, sampled, no tag seen

    def holds(self, key=None):
        held = self.key is not None and self.state in (ATTRIBUTED, CONFLICT)
        return held and (key is None or self.key == key)


class Attribution(object):
    """Which bay each tag belongs to (C2 §4.6.1). Pure: no I/O.

    ``coverage`` lists, per reader, the bays its field covers. Inputs are
    ``note_field`` (the UIDs of one sample, or None for an error), ready
    edges (``note_ready``) and ``note_turning`` (a bay's spool is turned by
    the unit). ``cache`` resolves a UID to its spool key."""

    def __init__(self, bay_count, coverage, cache=None, enumerates=True,
                 debounce=FIELD_DEBOUNCE, hand_s=T_HAND_S, orphan_s=ORPHAN_S,
                 static_samples=N_STATIC, insertion_read_trusted=None,
                 no_tag_turn_s=NO_TAG_TURN_S):
        self.cache = cache if cache is not None else TagCache()
        self.hand_s = float(hand_s)
        self.orphan_s = float(orphan_s)
        self.static_samples = int(static_samples)
        self.no_tag_turn_s = float(no_tag_turn_s)
        #: A reader that answers one UID per sample and is sampled only when
        #: the unit is idle (an extension's reader) cannot show motion: the
        #: read after a bay's insertion goes to that bay, under the spool-key
        #: and removal rules below.
        self.insertion_read_trusted = (not enumerates
                                       if insertion_read_trusted is None
                                       else bool(insertion_read_trusted))
        self.bays = [_Bay(index) for index in range(bay_count)]
        self.readers = []
        for index, bays in enumerate(coverage or ()):
            bays = tuple(b for b in bays if 0 <= b < bay_count)
            self.readers.append(_Reader(index, bays, enumerates,
                                        debounce if enumerates else 1))

    # ------------------------------------------------------------- queries

    def readers_of(self, bay):
        return [r for r in self.readers if bay in r.bays]

    def reader_of(self, bay):
        readers = self.readers_of(bay)
        return readers[0].index if readers else None

    def field(self, reader):
        return set(self.readers[reader].inset)

    def handled(self, bay, now):
        rise = self.bays[bay].rise_at
        return rise is not None and 0 <= now - rise <= self.hand_s

    def focus_readers(self, now):
        """Readers covering a bay that turns or was just inserted: the ones
        worth sampling fast (C2 §4.6.1)."""
        return [r.index for r in self.readers
                if any(self.bays[b].turning or self.handled(b, now)
                       for b in r.bays)]

    def key_of(self, uid):
        return self.cache.key_of(uid)

    def _keys_of(self, uids):
        keys = OrderedDict()
        for uid in uids or ():
            uid = _norm_uid(uid)
            keys.setdefault(self.key_of(uid), uid)
        return keys

    def _held_elsewhere(self, key, bay):
        return any(other.index != bay and other.holds(key)
                   for other in self.bays)

    def holder_of(self, key, exclude=None):
        for other in self.bays:
            if other.index != exclude and other.holds(key):
                return other.index
        return None

    def shared_with(self, bay):
        """The other bay holding this bay's key (the clone case), or None."""
        bs = self.bays[bay]
        if not bs.holds():
            return None
        return self.holder_of(bs.key, exclude=bay)

    # ------------------------------------------------------------ outputs

    def tag(self, bay, read_uid_first=True):
        """The bay's attributed tag (C2 §4.6), or None: never a candidate,
        and none while the bay is in conflict. A conflict that settles back
        on the same spool republishes the same tag, ``read_at`` and all."""
        bs = self.bays[bay]
        if bs.state != ATTRIBUTED or bs.key is None:
            return None
        record = self.cache.record(bs.uid) or tag_record("unknown")
        tag = _tag_dict(bs.uid, record.get("format") or "unknown",
                        bs.read_at, spoolman_id=record.get("spoolman_id"),
                        bambu=record.get("bambu"),
                        openspool=record.get("openspool"))
        uids = [bs.uid.upper()] + [uid.upper() for uid in
                                   self.cache.uids_of(bs.key)
                                   if uid != bs.uid]
        tag.update({"spool_key": bs.key, "uids": uids,
                    "evidence": bs.evidence,
                    "confirmed_at": bs.confirmed_at})
        if self.shared_with(bay) is not None:
            tag["shared_identity"] = True
        return tag

    def tag_state(self, bay):
        """C2 §4.5 ``tag_state``."""
        bs = self.bays[bay]
        candidates = []
        for key, uid in bs.candidates[:2]:
            material, color = _material_and_color(self.cache.record(uid))
            candidates.append({"spool_key": key, "uid": uid,
                               "material": material, "color_argb": color})
        reason = bs.reason if bs.state in (CONFLICT, UNKNOWN) else None
        if (bs.state == UNKNOWN and reason is None and not bs.awaiting_read
                and self._read_only_when_asked(bay)):
            # No read is left to wait for: this reader is not sampled while
            # the spool turns, and its insertion read found no tag (or the
            # spool was there before the host started).
            reason = NO_TAG_AFTER_TURN
        return {"state": bs.state,
                "reason": reason,
                "candidates": candidates,
                "other_bay": (bs.other_bay if bs.reason == CLAIMED_TWICE
                              else None),
                "reader": self.reader_of(bay)}

    def _read_only_when_asked(self, bay):
        readers = self.readers_of(bay)
        return bool(readers) and not any(r.enumerates for r in readers)

    def shared_pairs(self):
        """[(bay, other bay)] for every bay whose key another bay holds."""
        pairs = []
        for bs in self.bays:
            other = self.shared_with(bs.index)
            if other is not None:
                pairs.append((bs.index, other))
        return pairs

    def persisted(self):
        """bay -> {key, uid, read_at, ready} of every attributed bay."""
        out = {}
        for bs in self.bays:
            if bs.state == ATTRIBUTED and bs.key is not None:
                out[bs.index] = {"key": bs.key, "uid": bs.uid,
                                 "read_at": bs.read_at, "ready": bs.ready}
        return out

    # ------------------------------------------------------------- inputs

    def init_ready(self, bay, ready):
        """A bay's ready at startup (no edge seen)."""
        bs = self.bays[bay]
        bs.ready = bool(ready)
        if bs.state in (NONE, UNKNOWN):
            bs.state = UNKNOWN if bs.ready else NONE

    def restore(self, bay, key, uid, read_at):
        """A bay attributed before a restart whose ready is still true
        resumes as a candidate until evidence confirms it (C2 §4.6.1)."""
        bs = self.bays[bay]
        if not bs.ready or bs.state != UNKNOWN or not key or not uid:
            return False
        bs.state = CANDIDATE
        bs.candidates = [(key, _norm_uid(uid))]
        bs.restored = (key, _norm_uid(uid), read_at)
        return True

    def note_ready(self, bay, ready, now, keep=False):
        """A ready edge. ``keep``: the fall is a runout or the bay is in use,
        so the tag stays (C4 §3: a runout is not removal)."""
        bs = self.bays[bay]
        if ready:
            bs.ready = True
            bs.rise_at = now
            bs.reset()               # a kept runout tag is dropped first
            bs.awaiting_read = True
            self._promote_orphans(bay, now)
            for reader in self.readers_of(bay):
                self._eliminate(reader, now)
            return
        bs.ready = False
        bs.fall_at = now
        if keep and (bs.holds() or bs.candidates):
            # A candidate (a bay not confirmed since a restart) is kept
            # like a tag: the spool is still there.
            bs.kept = True
            bs.ready_fell = True
            return
        bs.reset()

    def resync_ready(self, bay, ready, now, keep=False):
        """A bay's ready found changed after a time nobody watched (the unit
        reconnected). A fall is a fall. A rise starts the bay afresh as
        unknown, with no handling window: nobody saw the insertion."""
        bs = self.bays[bay]
        if not ready:
            self.note_ready(bay, False, now, keep=keep)
            return
        bs.ready = True
        bs.rise_at = None
        bs.reset()

    def kept_bays(self):
        return [bs.index for bs in self.bays if bs.kept and not bs.ready]

    def clear_kept(self, bay):
        """The deferred clear of a kept tag (the runout ended, the bay is
        not in use and its ready is still false)."""
        bs = self.bays[bay]
        if bs.kept and not bs.ready:
            bs.reset()
            return True
        return False

    def note_turning(self, bay, on, now):
        bs = self.bays[bay]
        if on and not bs.turning:
            bs.turning = True
            bs.turn_start = now
            bs.turn_keys = {}
            bs.static = {}
        elif not on and bs.turning:
            bs.turning = False
            bs.turn_end = now
            self._note_silent_turn(bs, now)

    def _note_silent_turn(self, bs, now):
        """A turn that ended with nothing seen: once a ready bay has turned
        ``no_tag_turn_s`` in all, sampled throughout and undisturbed, with
        no tag showing, its spool carries no tag this reader can see."""
        sampled = any(track[1] >= self.static_samples and not track[2]
                      for track in bs.static.values())
        if (not bs.ready or bs.state != UNKNOWN or bs.turn_keys
                or not sampled or bs.turn_start is None):
            return
        bs.silent_s += max(0.0, now - bs.turn_start)
        if bs.silent_s >= self.no_tag_turn_s:
            bs.reason = NO_TAG_AFTER_TURN

    def note_following(self, bay, on, now):
        """``bay`` is loaded and follows the extruder (or stopped doing so):
        its spool is not still, though the unit does not turn it."""
        bs = self.bays[bay]
        if on:
            bs.following = True
        elif bs.following:
            bs.following = False
            bs.follow_end = now

    def note_field(self, reader, uids, now, trigger_bay=None):
        """One sample of reader ``reader``: its UIDs, or None for an error.

        An error is ignored, never taken for an empty field. With
        ``trigger_bay`` the sample is the read after that bay's insertion."""
        r = self.readers[reader]
        if uids is None:
            r.errors += 1
            return
        present = frozenset(_norm_uid(u) for u in uids)
        if trigger_bay is not None and 0 <= trigger_bay < len(self.bays):
            self.bays[trigger_bay].awaiting_read = False
        first = r.raw is None
        r.raw = present
        r.times = (r.times + [now])[-(r.debounce + 2):]
        self._track_static(r, present, now)
        if first:
            # The first sample is a baseline: nothing changed that the host
            # saw, so it is evidence of nothing.
            r.inset = present
            r.present_count = dict((uid, r.debounce) for uid in present)
            entered, left = set(), set()
        else:
            entered, left = self._debounce(r, present)
        if not r.enumerates:
            left = set()       # a one-UID scan cannot show a tag leaving
        since = (r.times[-(r.debounce + 1)]
                 if len(r.times) > r.debounce else None)
        if len(r.bays) == 1:
            # One bay under this reader: a tag in its field is that bay's
            # (``single``), seen at a change or not. Two keys at once are
            # left alone.
            keys = self._keys_of(present)
            bay = r.bays[0]
            if len(keys) == 1 and self.bays[bay].ready:
                key, uid = next(iter(keys.items()))
                if not self.bays[bay].holds(key):
                    self._strong(bay, uid, key, SINGLE, now)
            entered, left = set(), set()
        elif (trigger_bay is not None and self.insertion_read_trusted
                and trigger_bay in r.bays):
            for uid in sorted(present):
                self._insertion_read(trigger_bay, uid, now)
            entered = entered - present
        for uid in sorted(entered) + sorted(left):
            self._change(r, uid, now, since)
        self._static_check(r, now)
        self._eliminate(r, now)

    def note_record(self, uid, now):
        """``uid``'s record arrived (decoded): re-key what it changed."""
        uid = _norm_uid(uid)
        key = self.key_of(uid)
        for bs in self.bays:
            if bs.holds() and bs.uid == uid and bs.key != key:
                bs.key = key
                bs.read_at = now      # the bay's spool key changed
            bs.candidates = [(self.key_of(u) if u == uid else k, u)
                             for k, u in bs.candidates]
            if (bs.state == CONFLICT and bs.reason == CHANGED_IN_PLACE
                    and bs.candidates and bs.candidates[0][0] == bs.key):
                # The "different" tag was the spool's other tag after all.
                bs.state = ATTRIBUTED
                bs.uid = bs.candidates[0][1]
                bs.candidates = []
                bs.reason = None

    # ---------------------------------------------------- explicit reads

    def explicit_read(self, bay, uids, now):
        """``OAMS_RFID_SCAN BAY=``: what the fresh sample ``uids`` (already
        fed through note_field) says about ``bay``. Returns the outcome."""
        bs = self.bays[bay]
        keys = self._keys_of(uids)
        covers = [r for r in self.readers_of(bay)]
        single = bool(covers) and all(len(r.bays) == 1 for r in covers)
        if bs.state == ATTRIBUTED and bs.key in keys:
            self._attribute(bay, keys[bs.key], bs.key, bs.evidence, now,
                            reread=True)
        elif single and bs.ready and len(keys) == 1:
            key, uid = next(iter(keys.items()))
            self._strong(bay, uid, key, SINGLE, now, reread=True)
        elif bs.ready and not bs.holds():
            own = [(k, u) for k, u in keys.items()
                   if not self._held_elsewhere(k, bay)]
            for key, uid in own[:2]:
                self._add_candidate(bay, uid, key)
        return self.outcome(bay, keys)

    def assert_bay(self, bay, uids, now):
        """``OAMS_RFID_SCAN BAY= ASSERT=1``: the single spool key in the
        fresh sample is this bay's. Returns the outcome; its word is
        ``not_ready``, ``empty`` or ``two`` when refused."""
        bs = self.bays[bay]
        keys = self._keys_of(uids)
        if not bs.ready:
            return {"word": "not_ready"}
        if not keys:
            return {"word": "empty"}
        if len(keys) > 1:
            return {"word": "two", "keys": list(keys)}
        key, uid = next(iter(keys.items()))
        self._strong(bay, uid, key, ASSERTED, now, reread=True)
        return self.outcome(bay, keys)

    def assert_uid(self, bay, uid, now):
        """A tag this host just wrote on ``bay``'s reader is that bay's."""
        uid = _norm_uid(uid)
        self._strong(bay, uid, self.key_of(uid), ASSERTED, now, reread=True)

    def outcome(self, bay, keys=None):
        bs = self.bays[bay]
        result = {"word": None, "key": bs.key, "reason": bs.reason,
                  "other_bay": bs.other_bay, "in_field": False,
                  "elsewhere": None, "ready": bs.ready}
        keys = keys or {}
        if bs.state == ATTRIBUTED:
            result["word"] = "attributed"
            result["in_field"] = bs.key in keys
        elif bs.state == CONFLICT:
            result["word"] = "conflict"
        elif bs.state == CANDIDATE:
            result["word"] = "candidate"
            result["key"] = bs.candidates[0][0]
        else:
            result["word"] = "no_tag"
            for key in keys:
                holder = self.holder_of(key, exclude=bay)
                if holder is not None:
                    result["elsewhere"] = holder
                    break
        return result

    # ----------------------------------------------------------- evidence

    def _debounce(self, r, present):
        entered, left = set(), set()
        for uid in present | r.inset:
            if uid in present:
                r.present_count[uid] = r.present_count.get(uid, 0) + 1
                r.absent_count.pop(uid, None)
                if uid not in r.inset and r.present_count[uid] >= r.debounce:
                    entered.add(uid)
            else:
                r.absent_count[uid] = r.absent_count.get(uid, 0) + 1
                r.present_count.pop(uid, None)
                if r.absent_count[uid] >= r.debounce:
                    left.add(uid)
        for uid in list(r.present_count):
            if uid not in present:
                r.present_count.pop(uid, None)
        r.inset = frozenset((r.inset | entered) - left)
        for uid in left:
            r.absent_count.pop(uid, None)
        return entered, left

    def _change(self, r, uid, now, since):
        """A UID entering or leaving a reader of several bays."""
        key = self.key_of(uid)
        turning = [b for b in r.bays if self.bays[b].turning]
        handled = [b for b in r.bays
                   if not self.bays[b].turning and self.handled(b, now)]
        if len(turning) == 1:
            bay = turning[0]
            others_still = not any(
                self.bays[o].turning or self.handled(o, now)
                for o in r.bays if o != bay)
            if others_still:
                self._motion(r, bay, uid, key, now)
            else:
                self._weak(r, bay, uid, key, now, since)
            return
        if turning:
            return              # two covered bays turning: nothing to tell
        if len(handled) == 1:
            self._weak(r, handled[0], uid, key, now, since)
        elif handled:
            for bay in handled:
                if not self._held_elsewhere(key, bay):
                    self._add_candidate(bay, uid, key)
        else:
            r.orphans = [o for o in r.orphans
                         if now - o[0] <= self.orphan_s]
            r.orphans.append((now, uid, since))

    def _motion(self, r, bay, uid, key, now):
        bs = self.bays[bay]
        if not bs.ready:
            return
        # The other covered bays are still: a key one of them holds cannot
        # have moved (a dropout of that bay's tag, not this bay's spool).
        # Only what such a bay holds by direct evidence, and only that very
        # UID: a key it got by inference is corrected by this motion (the
        # move rule), and another UID with its key is a clone's.
        for other in r.bays:
            os_ = self.bays[other]
            if (other != bay and os_.holds(key) and os_.evidence in DIRECT
                    and os_.uid == _norm_uid(uid)):
                return
        bs.turn_keys.setdefault(key, uid)
        if len(bs.turn_keys) >= 2:
            if not (bs.state == CONFLICT and bs.reason == TWO_MOVING):
                bs.state = CONFLICT
                bs.reason = TWO_MOVING
                bs.other_bay = None
            bs.candidates = list(bs.turn_keys.items())[:2]
            return
        self._strong(bay, uid, key, MOTION, now)

    def _insertion_read(self, bay, uid, now):
        """The read after ``bay``'s insertion, on a reader that answers one
        UID per sample: an extension's reader (C2 §4.6.1)."""
        bs = self.bays[bay]
        if not bs.ready:
            return
        self._strong(bay, uid, self.key_of(uid), INSERTION, now)

    def _weak(self, r, bay, uid, key, now, since):
        bs = self.bays[bay]
        if not bs.ready or self._held_elsewhere(key, bay):
            return
        if bs.holds():
            return               # weak evidence never displaces a tag
        if self._others_accounted(r, bay, key, since):
            self._strong(bay, uid, key, INSERTION, now)
            return
        self._add_candidate(bay, uid, key)

    def _others_accounted(self, r, bay, key, since):
        """No other covered bay can have caused a change of ``key``: each
        holds a different key, or has been still since ``since`` (a still
        spool's tag can neither enter nor leave the field)."""
        for other in r.bays:
            if other == bay:
                continue
            os_ = self.bays[other]
            if os_.state == ATTRIBUTED and os_.key not in (None, key):
                continue
            if since is not None and self._still_since(os_, since):
                continue
            return False
        return True

    def _still_since(self, bs, since):
        if bs.turning or bs.following:
            return False
        if bs.follow_end is not None and bs.follow_end > since:
            return False
        if bs.turn_end is not None and bs.turn_end > since:
            return False
        if bs.rise_at is not None and bs.rise_at + self.hand_s > since:
            return False
        if bs.fall_at is not None and bs.fall_at > since:
            return False
        return True

    def _add_candidate(self, bay, uid, key):
        bs = self.bays[bay]
        if not bs.ready or bs.holds():
            return
        for index, (known, _uid) in enumerate(bs.candidates):
            if known == key:
                bs.candidates[index] = (key, uid)
                return
        if bs.state == CONFLICT:
            if len(bs.candidates) < 2:
                bs.candidates.append((key, uid))
            return
        if not bs.candidates:
            bs.state = CANDIDATE
            bs.reason = None
            bs.candidates = [(key, uid)]
            return
        bs.candidates = bs.candidates[:1] + [(key, uid)]
        bs.state = CONFLICT
        bs.reason = AMBIGUOUS_PAIR
        bs.other_bay = None
        bs.restored = None

    def _remove_candidate(self, bs, key):
        bs.candidates = [(k, u) for k, u in bs.candidates if k != key]
        if bs.holds():
            return
        if bs.restored is not None and bs.restored[0] == key:
            bs.restored = None
        if not bs.candidates:
            bs.state = UNKNOWN if bs.ready else NONE
            bs.reason = None
            bs.other_bay = None
        elif bs.state == CONFLICT and bs.reason == AMBIGUOUS_PAIR:
            bs.state = CANDIDATE
            bs.reason = None

    def _strong(self, bay, uid, key, evidence, now, reread=False):
        """Strong evidence that ``key`` is ``bay``'s (C2 §4.6.1)."""
        bs = self.bays[bay]
        if not bs.ready:
            return
        if bs.holds(key):
            self._attribute(bay, uid, key, evidence, now, reread=reread)
            return
        if (bs.state == ATTRIBUTED and bs.key != key
                and evidence != ASSERTED and bs.evidence == MOTION
                and not bs.ready_fell):
            bs.state = CONFLICT
            bs.reason = CHANGED_IN_PLACE
            bs.other_bay = None
            bs.candidates = [(key, uid)]
            return
        # The move rule by spool key, with the clone rule (C2 §4.6.1).
        holders = [other for other in self.bays
                   if other.index != bay and other.holds(key)]
        in_place = [other for other in holders
                    if other.ready and not other.ready_fell]
        if in_place and evidence not in DIRECT:
            # Inferred evidence never takes a key from a bay whose spool is
            # still in place: say so instead.
            if not bs.holds():
                bs.state = CONFLICT
                bs.reason = CLAIMED_TWICE
                bs.other_bay = in_place[0].index
                bs.candidates = [(key, uid)]
            return
        for other in holders:
            protected = (other in in_place
                         and other.evidence in (MOTION, ASSERTED))
            if not protected:
                other.reset()                # the spool moved here
            # else: two spools carry one identity (clones); both stand.
        self._attribute(bay, uid, key, evidence, now, reread=reread)

    def _attribute(self, bay, uid, key, evidence, now, reread=False):
        bs = self.bays[bay]
        uid = _norm_uid(uid)
        same = bs.holds(key)
        restored = (bs.restored if bs.state == CANDIDATE and bs.restored
                    and bs.restored[0] == key else None)
        # The UID read on this bay goes first in `uids`, even when it is the
        # spool's other tag (a sibling read): that changes `uid`, never
        # `read_at` (C2 §4.6.1).
        bs.uid = uid
        if not same:
            bs.evidence = evidence
        elif evidence in DIRECT and bs.evidence not in DIRECT:
            bs.evidence = evidence
        if restored is not None and not reread:
            bs.read_at = restored[2]
        elif not same or reread or bs.read_at is None:
            bs.read_at = now
        bs.state = ATTRIBUTED
        bs.key = key
        bs.confirmed_at = now
        bs.candidates = []
        bs.reason = None
        bs.other_bay = None
        bs.restored = None
        bs.ready_fell = False
        bs.kept = False
        for other in self.bays:
            if other.index != bay and any(k == key
                                          for k, _ in other.candidates):
                self._remove_candidate(other, key)
        for reader in self.readers_of(bay):
            self._eliminate(reader, now)

    def _eliminate(self, r, now):
        """A covered bay whose neighbours are all accounted for by direct
        evidence gets the one remaining key (C2 §4.6.1)."""
        if len(r.bays) < 2:
            return
        for bay in r.bays:
            bs = self.bays[bay]
            if not bs.ready or bs.holds():
                continue
            others = [self.bays[o] for o in r.bays if o != bay]
            if not all(o.ready and o.state == ATTRIBUTED
                       and o.evidence in DIRECT for o in others):
                continue
            taken = set(o.key for o in others)
            options = OrderedDict()
            for key, uid in bs.candidates:
                if key not in taken and not self._held_elsewhere(key, bay):
                    options[key] = uid
            for uid in sorted(r.inset):
                key = self.key_of(uid)
                if key not in taken and not self._held_elsewhere(key, bay):
                    options.setdefault(key, uid)
            if len(options) == 1:
                key, uid = next(iter(options.items()))
                self._strong(bay, uid, key, ELIMINATION, now)

    def _track_static(self, r, present, now):
        for bay in r.bays:
            bs = self.bays[bay]
            if not bs.turning:
                continue
            track = bs.static.get(r.index)
            if track is None:
                track = bs.static[r.index] = [set(present), 0, False, False]
            else:
                track[0] &= present
            track[1] += 1
            if any(self.bays[o].turning or self.handled(o, now)
                   for o in r.bays if o != bay):
                track[2] = True

    def _static_check(self, r, now):
        for bay in r.bays:
            bs = self.bays[bay]
            track = bs.static.get(r.index)
            if (not bs.turning or track is None or track[3] or track[2]
                    or track[1] < self.static_samples):
                continue
            track[3] = True
            others = [o for o in r.bays if o != bay]
            for uid in sorted(track[0]):
                key = self.key_of(uid)
                if any(k == key for k, _ in bs.candidates) and not bs.holds():
                    self._remove_candidate(bs, key)
                elif bs.holds(key) and bs.evidence not in DIRECT:
                    bs.reset()               # a promoted insertion, wrong
                if len(others) == 1 and not self._held_elsewhere(key,
                                                                 others[0]):
                    self._add_candidate(others[0], uid, key)

    def _promote_orphans(self, bay, now):
        for r in self.readers_of(bay):
            orphans, r.orphans = r.orphans, []
            for when, uid, since in orphans:
                if now - when > self.orphan_s:
                    continue
                key = self.key_of(uid)
                rivals = [o for o in r.bays if o != bay
                          and self.handled(o, now)]
                if rivals:
                    if not self._held_elsewhere(key, bay):
                        self._add_candidate(bay, uid, key)
                else:
                    self._weak(r, bay, uid, key, now, since)


# ------------------------------------------------------------- the service

class TagReader(object):
    """The reader service for one OAMS unit's bays."""

    def __init__(self, oams):
        self.oams = oams
        # bay -> tag dict or None. Never partially published: every change
        # goes through _attribute/_clear/_sync, which republish the dict.
        self.tags = {}
        # Background (queued) reads: one at a time, timer-driven so a busy
        # retry or a positioning wait never blocks the reactor or advances
        # its clock for every other unit's timers.
        self._queue = []
        self._active = None
        self._active_callback = None
        # Unit-decoded tags (C1 section 4.7.1): bay -> the last ams_tray_info
        # record, and the bay whose ams_tray_read outcome is awaited (it holds
        # the one-read-at-a-time slot, _active, until it arrives).
        self._tray = {}
        self._tray_wait = None
        self._tray_token = 0
        # Host attribution (a unit whose reads the host attributes): the
        # tag cache, the Attribution, and the [mfrc522] reader source.
        self.cache = TagCache()
        self.attr = None
        self._source = None
        self._turning = frozenset()
        self._restored = False
        self._saved = None
        self._saved_signature = None
        self._kept_timer = False
        self._decode_failures = {}
        self._turning_error_logged = False

    # ------------------------------------------------------------ publishing

    def _publish(self):
        self.oams.tags = dict(self.tags)

    def tag_for(self, bay):
        return self.tags.get(bay)

    def _attribute(self, bay, tag):
        """A unit's own report for ``bay`` (no host attribution): the same
        UID elsewhere is cleared."""
        if tag is not None and tag.get("uid") is not None:
            # A tag without a UID (a unit-decoded summary) is never "the same
            # tag" as another bay's: None == None must not clear a bay.
            for other, existing in list(self.tags.items()):
                if (other != bay and existing is not None
                        and existing.get("uid") == tag["uid"]):
                    self.tags[other] = None
        self.tags[bay] = tag
        self._publish()

    def _clear(self, bay):
        if self.tags.get(bay) is not None:
            self.tags[bay] = None
            self._publish()

    def _bay_count(self):
        return int(getattr(self.oams, "bay_count", 4) or 4)

    def _now(self):
        return self.oams.reactor.monotonic()

    def _sync(self):
        """Republish every bay's tag from the attribution, and persist."""
        if self.attr is None:
            return
        self.tags = {bay: self.attr.tag(bay)
                     for bay in range(len(self.attr.bays))}
        self._publish()
        self._save_state()

    # ------------------------------------------------------ host attribution

    def attributes(self):
        """Whether the host attributes this unit's reads (tag_state)."""
        return self.attr is not None

    def has_host_reader(self):
        """Whether a host-driven reader ([mfrc522]) is attached."""
        return self._source is not None

    def tag_state_for(self, bay):
        if self.attr is None or not 0 <= bay < len(self.attr.bays):
            return None
        return self.attr.tag_state(bay)

    def coverage(self):
        """The bays each reader covers, or None."""
        if self.attr is None:
            return None
        return [list(r.bays) for r in self.attr.readers]

    def faults(self):
        """Info faults (C2 §4.8): two bays holding one tag identity."""
        if self.attr is None:
            return []
        out = []
        for bay, other in self.attr.shared_pairs():
            out.append({
                "severity": "info", "code": "shared_tag_identity",
                "text": "%s: same tag identity as %s" % (
                    self.oams.spool_label(bay), self._short_label(other)),
                "bay": bay, "actions": [], "source": "tag"})
        return out

    def _short_label(self, bay):
        label = self.oams.spool_label(bay)
        return label.split(" ", 1)[1] if " " in label else label

    def attach_readers(self, source, coverage, **options):
        """A host-driven reader source ([mfrc522]) for this unit: it hands
        in every sample (``note_field``) and samples one reader on demand
        (``source.sample_reader(index)``)."""
        self._source = source
        self._ensure_attribution(coverage, enumerates=True, **options)

    def _ensure_attribution(self, coverage=None, **options):
        if self.attr is not None:
            return self.attr
        if coverage is None:
            coverage = self._default_coverage()
        self.attr = Attribution(self._bay_count(), coverage, cache=self.cache,
                                **options)
        values = getattr(self.oams, "f1s_hes_value", None) or []
        if getattr(self.oams, "hardware_snapshot_ready", False):
            for bay in range(len(self.attr.bays)):
                self.attr.init_ready(bay, bay < len(values) and values[bay])
            self._restore_state()
        return self.attr

    def _default_coverage(self):
        caps = getattr(self.oams, "capabilities", None) or {}
        coverage = caps.get("rfid_coverage")
        if coverage:
            return [list(bays) for bays in coverage]
        return [list(range(self._bay_count()))]

    def update_turning(self, now=None):
        """Feed the unit's turning bays (C2 §4.6.1) to the attribution: a
        bay positioning, or the bay of a load, unload, runout reload or
        calibration in flight."""
        if self.attr is None:
            return
        now = self._now() if now is None else now
        turning = set()
        try:
            turning |= set(self.oams.positioning_bays(now))
        except Exception:
            pass
        manager = self._manager()
        if manager is not None and hasattr(manager, "turning_bays"):
            try:
                turning |= set(manager.turning_bays(self.oams.oams_idx))
            except Exception:
                if not self._turning_error_logged:
                    self._turning_error_logged = True
                    logging.exception("OAMS[%s]: turning bays unavailable",
                                      self.oams.oams_idx)
        turning = frozenset(b for b in turning
                            if 0 <= b < len(self.attr.bays))
        for bay in range(len(self.attr.bays)):
            on = bay in turning
            if on != self.attr.bays[bay].turning:
                self.attr.note_turning(bay, on, now)
            # A loaded bay that only follows the extruder still turns as
            # the printer pulls: not "turning", but not still either.
            following = False
            if not on and manager is not None:
                try:
                    following = bool(manager.bay_in_use(self.oams.oams_idx,
                                                        bay))
                except Exception:
                    following = False
            if following != self.attr.bays[bay].following:
                self.attr.note_following(bay, following, now)
        self._turning = turning

    def note_turning(self, bay, on, now):
        if self.attr is not None:
            self.attr.note_turning(bay, on, now)

    def focus_readers(self, now=None):
        """The readers to sample fast right now (C2 §4.6.1)."""
        if self.attr is None:
            return []
        now = self._now() if now is None else now
        self.update_turning(now)
        return self.attr.focus_readers(now)

    def needs_record(self, uid):
        """Whether ``uid`` has no decoded Bambu record yet (a Bambu tag is
        decoded once, then known from the cache), and is still worth a
        decode."""
        record = self.cache.record(uid)
        if record and isinstance(record.get("bambu"), dict):
            return False
        return self._decode_failures.get(_norm_uid(uid), 0) < DECODE_ATTEMPTS

    def pending_record(self, uid):
        """The record of a MIFARE Classic tag not decoded yet: a Bambu tag
        whose content is pending (``bambu`` null), so a spool link waits for
        the decode; ``unknown`` once its decode failed DECODE_ATTEMPTS
        times."""
        if self._decode_failures.get(_norm_uid(uid), 0) >= DECODE_ATTEMPTS:
            return tag_record("unknown")
        return tag_record("bambu")

    def note_decode_failed(self, uid):
        uid = _norm_uid(uid)
        self._decode_failures[uid] = self._decode_failures.get(uid, 0) + 1

    def record_for_key(self, key):
        return self.cache.bambu_record_for_key(key)

    def reader_at_rest(self, reader):
        """True when no bay the reader covers is turning (a full decode
        needs the tag still, C2 §4.6.1)."""
        if self.attr is None or not 0 <= reader < len(self.attr.readers):
            return True
        return not any(self.attr.bays[b].turning
                       for b in self.attr.readers[reader].bays)

    def note_field(self, reader, uids_or_error, now=None, records=None,
                   trigger_bay=None):
        """One sample of ``reader``: a list of UIDs (hex), or None for an
        error, and any records decoded with it (uid -> record)."""
        attr = self._ensure_attribution()
        if not 0 <= reader < len(attr.readers):
            return
        now = self._now() if now is None else now
        self.update_turning(now)
        for uid, record in (records or {}).items():
            if self.cache.put(uid, record):
                attr.note_record(uid, now)    # re-key first, then the sample
        attr.note_field(reader, uids_or_error, now, trigger_bay=trigger_bay)
        self._sync()

    def note_record(self, uid, record, now=None):
        """A decoded record for ``uid`` (a tag written, or decoded later)."""
        now = self._now() if now is None else now
        if self.cache.put(uid, record) and self.attr is not None:
            self.attr.note_record(uid, now)
        self._sync()

    # -------------------------------------------------- external attribution
    # Thin callers of note_field, kept for older callers: a read is the
    # reader's last field plus that UID, a removal the field without it.

    def note_external_read(self, bay, uid_hex, fmt, bambu=None):
        attr = self._ensure_attribution()
        reader = attr.reader_of(bay)
        if reader is None:
            return
        uid = _norm_uid(uid_hex)
        records = {uid: tag_record(fmt, bambu=bambu)} if fmt != "unknown" \
            else None
        self.note_field(reader, sorted(attr.field(reader) | {uid}),
                        records=records)

    def note_external_removed(self, uid_hex):
        if self.attr is None:
            return       # nothing host-attributed; a summary has no UID
        uid = _norm_uid(uid_hex)
        for r in self.attr.readers:
            if uid in r.inset:
                self.note_field(r.index, sorted(set(r.inset) - {uid}))

    # ------------------------------------------------------ explicit reads

    def scan_bay(self, bay, assert_=False):
        """``OAMS_RFID_SCAN BAY=`` through the attached reader source: a
        fresh sample of the bay's reader. Returns the outcome dict."""
        attr = self._ensure_attribution()
        reader = attr.reader_of(bay)
        if self._source is None or reader is None:
            return {"word": "error", "error": "no reader covers this bay"}
        uids = self._source.sample_reader(reader)
        if uids is None:
            return {"word": "error", "error": "the reader did not answer"}
        return self.apply_explicit(bay, uids, assert_)

    def apply_explicit(self, bay, uids, assert_):
        """The explicit read's verdict on a fresh sample already fed through
        note_field."""
        now = self._now()
        if assert_:
            outcome = self.attr.assert_bay(bay, uids, now)
        else:
            outcome = self.attr.explicit_read(bay, uids, now)
        self._sync()
        return outcome

    def describe(self, bay, outcome):
        """``(ok, text)`` for an explicit read's outcome (C3)."""
        label = self.oams.spool_label(bay)
        word = outcome.get("word")
        if word == "error":
            return False, "%s: %s" % (label, outcome.get("error"))
        if word == "not_ready":
            return False, ("%s holds no spool (its sensor reads empty)"
                           % label)
        if word == "empty":
            return False, "No tag in the field of %s's reader" % label
        if word == "two":
            return False, "Two tags answer this reader; turn the other spool away"
        if word == "attributed":
            return True, "%s: attributed %s%s" % (
                label, outcome.get("key"),
                "" if outcome.get("in_field") else " (not in the field now)")
        if word == "candidate":
            return True, ("%s: candidate %s (not confirmed: load, or"
                          " OAMS_RFID_SCAN ... ASSERT=1)"
                          % (label, outcome.get("key")))
        if word == "conflict":
            reason = outcome.get("reason")
            extra = ""
            if reason == CLAIMED_TWICE and outcome.get("other_bay") is not None:
                extra = " (also on %s)" % self._short_label(
                    outcome["other_bay"])
            return True, "%s: conflict %s%s" % (label, reason, extra)
        elsewhere = outcome.get("elsewhere")
        if elsewhere is not None:
            detail = " (the tag in the field is %s's)" % self._short_label(
                elsewhere)
        elif outcome.get("ready") is False:
            detail = " (the bay holds no spool)"
        else:
            detail = ""
        return True, "%s: no tag in the field%s" % (label, detail)

    # -------------------------------------------------------- persistence

    def _manager(self):
        printer = getattr(self.oams, "printer", None)
        if printer is None:
            return None
        try:
            return printer.lookup_object("oams_manager", None)
        except Exception:
            return None

    def _cache_path(self):
        """The persisted cache: next to the OpenAMS config file, and only
        when that file exists (a test or a bare install writes nothing)."""
        manager = self._manager()
        config = getattr(manager, "openams_config_path", None)
        if not config or not os.path.isfile(config):
            return None
        return os.path.join(os.path.dirname(config), CACHE_FILE)

    def _unit_key(self):
        name = getattr(self.oams, "name", None)
        return (name.split()[-1] if name else
                "oams%s" % getattr(self.oams, "oams_idx", ""))

    def _load_file(self, path):
        try:
            with open(path) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return {}
        return data

    def _restore_state(self):
        """Restore the persisted cache; a bay still ready resumes as a
        candidate of what it held (C2 §4.6.1)."""
        if self._restored or self.attr is None:
            return
        self._restored = True
        path = self._cache_path()
        if path is None:
            return
        data = self._load_file(path)
        unit = (data.get("units") or {}).get(self._unit_key()) or {}
        self.cache.load(unit.get("records"))
        for bay, entry in (unit.get("bays") or {}).items():
            try:
                bay = int(bay)
            except ValueError:
                continue
            if (0 <= bay < len(self.attr.bays) and isinstance(entry, dict)
                    and entry.get("ready")):
                self.attr.restore(bay, entry.get("key"), entry.get("uid"),
                                  entry.get("read_at"))
        self._saved = self._snapshot()

    def _snapshot(self):
        bays = {}
        if self.attr is not None:
            bays = {str(bay): entry
                    for bay, entry in self.attr.persisted().items()}
        return {"records": self.cache.dump(), "bays": bays}

    def _save_state(self):
        if self.attr is None or not self._restored:
            return
        signature = (self.cache.version, self.attr.persisted())
        if signature == self._saved_signature:
            return
        self._saved_signature = signature
        snapshot = self._snapshot()
        if snapshot == self._saved:
            return
        path = self._cache_path()
        if path is None:
            return
        data = self._load_file(path) or {"version": CACHE_VERSION}
        data["version"] = CACHE_VERSION
        data.setdefault("units", {})[self._unit_key()] = snapshot
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as handle:
                json.dump(data, handle, sort_keys=True)
            os.replace(tmp, path)
            self._saved = snapshot
        except OSError:
            logging.info("OAMS[%s]: tag cache not saved to %s",
                         getattr(self.oams, "oams_idx", "?"), path)

    # -- background (queued, timer-driven): trigger() for insertion/startup --

    def trigger(self, bay):
        """Queue a read of ``bay``. Reads never overlap on one unit."""
        self._queue.append((bay, None))
        self._pump_queue()

    def _pump_queue(self):
        if self._active is not None or not self._queue:
            return
        bay, callback = self._queue.pop(0)
        self._active = bay
        self._active_callback = callback
        self._background_read(bay, _BUSY_RETRIES)

    def _background_read(self, bay, retries_left):
        """One queued read (an extension reader may take it instead)."""
        self._background_tray_read(bay, retries_left)

    # ------------------------------------------------------------ triggers

    def note_ready_rise(self, bay, now):
        """A bay's F1S presence rose (C2 §4.6).

        Called from the serial receive thread (``OAMS._note_f1s``); it must
        not block, so the attribution, the wait-for-positioning and the read
        itself run in a deferred reactor callback instead.
        """
        try:
            self.oams.reactor.register_async_callback(
                lambda eventtime: self._handle_ready_rise(bay, now))
        except Exception:
            logging.exception(
                "OAMS[%s]: scheduling insertion RFID read failed",
                self.oams.oams_idx)

    def _handle_ready_rise(self, bay, now):
        if self.attr is not None and 0 <= bay < len(self.attr.bays):
            self.attr.note_ready(bay, True, now)
            self._sync()
        if self._reads_by_trigger():
            self._start_insertion_read(bay)

    def note_ready_fall(self, bay):
        """A bay's F1S presence fell: clear its tag unless a runout is live."""
        try:
            self.oams.reactor.register_async_callback(
                lambda eventtime: self._handle_ready_fall(bay))
        except Exception:
            logging.exception(
                "OAMS[%s]: scheduling RFID removal failed", self.oams.oams_idx)

    def note_hardware_ready(self):
        """The unit's first hardware snapshot arrived.

        Also called from the serial receive thread; deferred for the same
        reason as :meth:`note_ready_rise`.
        """
        try:
            self.oams.reactor.register_async_callback(
                lambda eventtime: self._handle_hardware_ready())
        except Exception:
            logging.exception(
                "OAMS[%s]: scheduling startup RFID read failed",
                self.oams.oams_idx)

    def _handle_hardware_ready(self):
        if self._ext_reads() and not self._uses_tray_info():
            self._ensure_attribution(**self._ext_attribution_options())
        if self.attr is not None:
            values = getattr(self.oams, "f1s_hes_value", None) or []
            now = self._now()
            for bay in range(len(self.attr.bays)):
                ready = bool(bay < len(values) and values[bay])
                if self._restored and ready != self.attr.bays[bay].ready:
                    # The unit came back (a reconnect) with this bay's
                    # ready changed while nobody watched: an edge.
                    self.attr.resync_ready(
                        bay, ready, now,
                        keep=not ready and self._in_use(bay))
                else:
                    self.attr.init_ready(bay, ready)
            self._restore_state()
            self._sync()
            self._arm_kept_check()
        if self._reads_by_trigger():
            self._start_startup_read()

    def _ext_attribution_options(self):
        """Extension hook: the Attribution options of an extension reader."""
        return {}

    def _setting(self, name, default):
        manager = self._manager()
        if manager is None:
            return default
        return manager.settings.get(name, default)

    def _start_insertion_read(self, bay):
        if not self._setting("read_tag_on_insertion", True):
            return
        self._await_not_positioning(
            bay, self.oams.reactor.monotonic() + _POSITIONING_TIMEOUT_S)

    def _await_not_positioning(self, bay, deadline):
        """Poll ``positioning_bays()`` with a one-shot, never a blocking wait.

        A one-shot callback (not ``reactor.pause``) so this routine wait for
        an ordinary insertion never itself advances the shared clock; it
        fires only as the reactor's own clock reaches it.
        """
        reactor = self.oams.reactor
        if (bay not in self.oams.positioning_bays()
                or reactor.monotonic() >= deadline):
            self.trigger(bay)
            return
        reactor.register_callback(
            lambda eventtime, b=bay, d=deadline: self._await_not_positioning(
                b, d),
            reactor.monotonic() + _POSITIONING_POLL_S)

    def _start_startup_read(self):
        """Startup. A unit that decodes its own tags is asked to read the
        bays it has not read. A host-attributed reader is never read into a
        bay at startup (C2 §4.6.1): the bays start
        unknown, or as candidates of what they held before the restart."""
        if not self._setting("read_tags_at_startup", True):
            return
        if self._uses_tray_read():
            reactor = self.oams.reactor
            reactor.register_callback(
                lambda eventtime: self._start_tray_startup_read(),
                reactor.monotonic() + _TRAY_STARTUP_DELAY_S)
            return
        self._startup_baseline()

    def _startup_baseline(self):
        """Extension hook: sample the field once for a baseline (never
        attributed on its own)."""

    def _start_tray_startup_read(self):
        """Startup read of a unit that decodes its own tags: every ready bay
        the unit reports as holding a spool it has not read (state 1). A bay
        it has not reported, or already read, is left alone - a read may
        turn the spool, and the reads go one at a time through the queue."""
        if not self._setting("read_tags_at_startup", True):
            return
        values = getattr(self.oams, "f1s_hes_value", [])
        for bay in range(self.oams.bay_count):
            record = self._tray.get(bay)
            if (bay < len(values) and values[bay] and record is not None
                    and record["state"] == TRAY_PRESENT):
                self.trigger(bay)

    def _handle_ready_fall(self, bay):
        """A bay's F1S presence fell: clear its tag, with two exceptions.

        (a) this bay is the loaded/loading/unloading bay of some lane (the
        filament is threaded through its hub right now, so the spool cannot
        have been pulled); or (b) the unit's lane is mid-runout. (b) alone
        races a runout: the reducer has not always recorded ``runout`` yet
        on the very frame the driver sees the same F1S fall on, so
        ``unit_runout_active`` can still answer False at that instant. (a)
        is the fact that does not race.
        """
        if self.attr is not None and 0 <= bay < len(self.attr.bays):
            self.attr.note_ready(bay, False, self._now(),
                                 keep=self._in_use(bay))
            self._sync()
            self._arm_kept_check()
            return
        self._clear_unless_in_use(bay)

    def _in_use(self, bay):
        manager = self._manager()
        return manager is not None and (
            manager.bay_in_use(self.oams.oams_idx, bay)
            or manager.unit_runout_active(self.oams.oams_idx))

    def _clear_unless_in_use(self, bay):
        if self._in_use(bay):
            return
        self._clear(bay)

    def _arm_kept_check(self):
        """A kept tag (a runout's empty spool) clears once the runout ended,
        the bay is not in use and its ready is still false (C2 §4.6.1)."""
        if self._kept_timer or self.attr is None or not self.attr.kept_bays():
            return
        self._kept_timer = True
        reactor = self.oams.reactor
        reactor.register_callback(lambda eventtime: self._kept_check(),
                                  reactor.monotonic() + _KEPT_CHECK_S)

    def _kept_check(self):
        self._kept_timer = False
        if self.attr is None:
            return
        changed = False
        for bay in self.attr.kept_bays():
            if not self._in_use(bay) and self.attr.clear_kept(bay):
                changed = True
        if changed:
            self._sync()
        self._arm_kept_check()

    # ------------------------------------------- unit-decoded tags (C1 4.7.1)

    def _uses_tray_info(self):
        """Whether the unit decodes its own tags (AMS_CAP_TRAY_INFO)."""
        return getattr(self.oams, "ams_get_tray_info_cmd", None) is not None

    def _reads_by_trigger(self):
        """Whether the insertion and startup triggers read at all:
        ``ams_tray_read`` on a unit that decodes its own tags and can be
        told to read."""
        if self._uses_tray_read():
            return True
        return self._ext_reads()

    def _ext_reads(self):
        """Extension hook (oams_ext.py): whether an extension reader reads
        on these triggers."""
        return False

    def _uses_tray_read(self):
        """Whether reads are ``ams_tray_read`` (AMS_CAP_TRAY_READ)."""
        return getattr(self.oams, "ams_tray_read_cmd", None) is not None

    def tray_record(self, bay):
        """The last ``ams_tray_info`` record of ``bay``, or None."""
        return self._tray.get(bay)

    def note_tray_info(self, bay, record):
        """One ``ams_tray_info`` (reactor thread): publish the bay's tag.

        States 4 (decoded) publishes a ``unit_summary`` tag; 3 (read, no tag)
        and 5 (read failed) publish no tag (5 also leaves a telemetry note);
        0 and 1 (no spool / not read) clear it unless the filament is in use,
        as a falling F1S does; 2 (reading) leaves what is published. A repeat
        of an already published summary keeps its ``read_at``, so only a read
        counts as a re-read (C2 §4.6).
        """
        previous = self._tray.get(bay)
        self._tray[bay] = record
        state = record["state"]
        if state == TRAY_DECODED:
            summary = tray_summary(record)
            existing = self.tags.get(bay)
            repeat = (existing is not None
                      and existing.get("format") == "unit_summary"
                      and existing.get("summary") == summary
                      and previous is not None
                      and previous["state"] == TRAY_DECODED)
            if not repeat:
                self.tags[bay] = _tag_dict(
                    None, "unit_summary", self.oams.reactor.monotonic(),
                    summary=summary)
                self._publish()
        elif state in (TRAY_NO_TAG, TRAY_FAILED):
            self._clear(bay)
            if state == TRAY_FAILED:
                self.oams._publish_telemetry("rfid", {
                    "result": TRAY_FAILED, "result_name": "read_failed",
                    "source": "tray_info", "bay": bay,
                    "timestamp": self.oams.reactor.monotonic()})
        elif state in (TRAY_NO_SPOOL, TRAY_PRESENT):
            self._clear_unless_in_use(bay)
        if (self._tray_wait == bay and state in (
                TRAY_NO_SPOOL, TRAY_NO_TAG, TRAY_DECODED, TRAY_FAILED)):
            self._release_tray_wait()

    def _arm_tray_wait(self, bay):
        """An ``ams_tray_read`` was accepted: hold the read slot until its
        outcome arrives, or the timeout (the unit may have lost the read)."""
        self._tray_wait = bay
        self._tray_token += 1
        token = self._tray_token
        reactor = self.oams.reactor
        reactor.register_callback(
            lambda eventtime, t=token: self._tray_wait_timeout(t),
            reactor.monotonic() + _TRAY_READ_TIMEOUT_S)

    def _tray_wait_timeout(self, token):
        if token == self._tray_token and self._tray_wait is not None:
            logging.info("OAMS[%s]: tray read of bay %s: no outcome reported",
                         self.oams.oams_idx, self._tray_wait)
            self._release_tray_wait()

    def _release_tray_wait(self):
        bay = self._tray_wait
        self._tray_wait = None
        self._tray_token += 1
        if self._active == bay:
            self._active = None
            callback = self._active_callback
            self._active_callback = None
            if callback is not None:
                callback(("ok", self.tags.get(bay)))
            self._pump_queue()

    def _background_tray_read(self, bay, retries_left):
        try:
            result = self.oams.tray_read_once(bay)
        except Exception:
            logging.exception("OAMS[%s]: tray read of bay %s failed",
                              self.oams.oams_idx, bay)
            result = "unavailable"
        if result == "busy" and retries_left > 0:
            self.oams.reactor.register_callback(
                lambda eventtime, b=bay, r=retries_left - 1:
                    self._background_tray_read(b, r),
                self.oams.reactor.monotonic() + _BUSY_INTERVAL_S)
            return
        if result == "accepted":
            self._arm_tray_wait(bay)       # the outcome is ams_tray_info
            return
        self.oams._publish_telemetry("rfid", {
            "result_name": result, "source": "tray_read", "bay": bay,
            "timestamp": self.oams.reactor.monotonic()})
        callback = self._active_callback
        self._active = None
        self._active_callback = None
        if callback is not None:
            callback((result, self.tags.get(bay)))
        self._pump_queue()

    def tray_read_now(self, bay, timeout=30.0):
        """``OAMS_RFID_SCAN BAY=`` on a unit with AMS_CAP_TRAY_READ: waits its
        turn, retries ``busy``, and returns the immediate
        result's name. On ``accepted`` the read slot stays held until the
        outcome (``ams_tray_info``) arrives, so reads never overlap."""
        reactor = self.oams.reactor
        deadline = reactor.monotonic() + timeout
        while self._active is not None and reactor.monotonic() < deadline:
            reactor.pause(reactor.monotonic() + 0.05)
        self._active = bay
        self._active_callback = None
        try:
            result = self.oams.tray_read_once(bay)
            for _ in range(_BUSY_RETRIES):
                if result != "busy":
                    break
                reactor.pause(reactor.monotonic() + _BUSY_INTERVAL_S)
                result = self.oams.tray_read_once(bay)
        except Exception:
            self._active = None
            self._pump_queue()
            raise
        if result == "accepted":
            self._arm_tray_wait(bay)
        else:
            self._active = None
            self._pump_queue()
        return result
