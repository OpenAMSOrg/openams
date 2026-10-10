# OpenAMS RFID tag reader service (S4b).
#
# Copyright (C) 2026 JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# One TagReader per OAMS unit, owned by its driver (src/oams.py). It keeps a
# per-bay tag (docs/contracts/C2_status.md §4.6) that the driver publishes as
# bays[i].tag. On an OpenAMS 1 mainboard the separate [mfrc522] endpoint
# (src/mfrc522.py) hands its reads and removals in.
#
# A unit that reads and decodes its spools' tags itself (C1 section 4.7.1,
# AMS_CAP_TRAY_INFO) reports a decoded summary per bay instead; note_tray_info
# turns that into a "unit_summary" tag (C2 section 4.6), which carries no UID.
# With AMS_CAP_TRAY_READ the insertion/startup triggers and OAMS_RFID_SCAN ask
# the unit to read (ams_tray_read).
#
# Pure logic where possible: only the tray read reaches into the driver's
# Klipper command objects.

import logging

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


class TagReader(object):
    """The reader service for one OAMS unit's bays."""

    def __init__(self, oams):
        self.oams = oams
        # bay -> tag dict or None. Never partially published: every change
        # goes through _attribute/_clear, which republish the whole dict.
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

    # ------------------------------------------------------------ publishing

    def _publish(self):
        self.oams.tags = dict(self.tags)

    def tag_for(self, bay):
        return self.tags.get(bay)

    def _attribute(self, bay, tag):
        """A read's tag goes to ``bay``; the same UID elsewhere is cleared."""
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

    # -------------------------------------------------- external attribution
    # (An OpenAMS 1 mainboard's src/mfrc522.py hands its reads and removals
    # in here.)

    def note_external_read(self, bay, uid_hex, fmt, bambu=None):
        now = self.oams.reactor.monotonic()
        self._attribute(bay, _tag_dict(uid_hex, fmt, now, bambu=bambu))

    def note_external_removed(self, uid_hex):
        for bay, tag in list(self.tags.items()):
            if tag is not None and tag.get("uid") == uid_hex:
                self.tags[bay] = None
        self._publish()

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
        """A bay's F1S presence rose (C2 §4.6): schedule an insertion read.

        Called from the serial receive thread (``OAMS._note_f1s``); it must
        not block, so the wait-for-positioning and the read itself run in a
        deferred reactor callback instead.
        """
        if not self._reads_by_trigger():
            return  # nothing to read with here; [mfrc522] hands reads in.
        try:
            self.oams.reactor.register_async_callback(
                lambda eventtime: self._start_insertion_read(bay))
        except Exception:
            logging.exception(
                "OAMS[%s]: scheduling insertion RFID read failed",
                self.oams.oams_idx)

    def note_ready_fall(self, bay):
        """A bay's F1S presence fell: clear its tag unless a runout is live."""
        try:
            self.oams.reactor.register_async_callback(
                lambda eventtime: self._handle_ready_fall(bay))
        except Exception:
            logging.exception(
                "OAMS[%s]: scheduling RFID removal failed", self.oams.oams_idx)

    def note_hardware_ready(self):
        """The unit's first hardware snapshot arrived: maybe a startup read.

        Also called from the serial receive thread; deferred for the same
        reason as :meth:`note_ready_rise`.
        """
        if not self._reads_by_trigger():
            return
        try:
            self.oams.reactor.register_async_callback(
                lambda eventtime: self._start_startup_read())
        except Exception:
            logging.exception(
                "OAMS[%s]: scheduling startup RFID read failed",
                self.oams.oams_idx)

    def _setting(self, name, default):
        manager = self.oams.printer.lookup_object("oams_manager", None)
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
        if not self._setting("read_tags_at_startup", True):
            return
        if self._uses_tray_read():
            reactor = self.oams.reactor
            reactor.register_callback(
                lambda eventtime: self._start_tray_startup_read(),
                reactor.monotonic() + _TRAY_STARTUP_DELAY_S)
            return
        values = getattr(self.oams, "f1s_hes_value", [])
        ready = [bay for bay in range(self.oams.bay_count)
                if bay < len(values) and values[bay]]
        if len(ready) == 1:
            self.trigger(ready[0])

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
        self._clear_unless_in_use(bay)

    def _clear_unless_in_use(self, bay):
        manager = self.oams.printer.lookup_object("oams_manager", None)
        if manager is not None and (
                manager.bay_in_use(self.oams.oams_idx, bay)
                or manager.unit_runout_active(self.oams.oams_idx)):
            return
        self._clear(bay)

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
