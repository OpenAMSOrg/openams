# A unit's stored per-bay filament setting, kept in step with Spoolman (C4 §7.5).
#
# A unit with AMS_CAP_TRAY_WRITE (C1 §4.7.2) stores, per bay, the filament it
# reports for a spool whose tag it does not read (a cardboard spool, say), and
# accepts a new one from the host. Spoolman is the source of truth:
# ``setting_for`` maps a bay's linked spool to that setting, and ``TraySync``
# decides, bay by bay on every status pass, whether to write it with
# ``OAMSM_SET_TRAY_INFO`` (C3 §4). Pure Python over the host's status and the
# cached spool summaries; the linker owns the instance.

import logging

from . import ops

_LOGGER = logging.getLogger(__name__)

# What the unit's command takes (C1 §4.7.2).
CODE_MAX = 6
MATERIAL_MAX = 8
# A spool's own temperature becomes a nozzle range this wide on each side,
# kept inside the range the host accepts (C3 §4).
TEMP_MARGIN_C = 10
MIN_TEMP_C = ops.MIN_EXTRUDER_TEMP
MAX_TEMP_C = ops.MAX_EXTRUDER_TEMP

# Best-effort defaults by material, for whatever the spool's filament does not
# name itself: Bambu's generic filament code and a nozzle range in °C. Looked up
# like the extruder table (C4 §7.4): the exact name, then the part before the
# first "-", "+" or space. This is the one table; a material it does not know
# takes the generic PLA code, and a range from the extruder table (±10 °C) or
# else PLA's.
MATERIAL_DEFAULTS = {
    "PLA": ("GFL99", 190, 230),
    "PLA-CF": ("GFL98", 190, 230),
    "PETG": ("GFG99", 220, 260),
    "ABS": ("GFB99", 240, 280),
    "ASA": ("GFB98", 240, 280),
    "TPU": ("GFU99", 200, 240),
    "PA": ("GFN99", 260, 300),
    "PC": ("GFC99", 260, 300),
    "PVA": ("GFS99", 190, 230),
}
FALLBACK_MATERIAL = "PLA"

# The sync's pacing (C4 §7.5): a changed spool waits this long before it is
# written; a write is retried this long later while the unit still reports
# something else, at most this many times per change; a linked spool is
# re-read from Spoolman this often, so an edit made there arrives.
DEBOUNCE_SECONDS = 2.0
RETRY_SECONDS = 30.0
MAX_ATTEMPTS = 3
REFRESH_SECONDS = 60.0
# The unit's refusals no retry can change (C1 §4.7.2): it cannot store the
# code or material, so the same setting would be refused again. The host's
# command names the result in its error.
PERMANENT_RESULTS = ("invalid_field",)


# ------------------------------------------------------------------ mapping


def _ascii(text):
    """Printable ASCII only (the unit's fields are ASCII), without quotes."""
    return "".join(char for char in str(text or "")
                   if 0x20 <= ord(char) <= 0x7E and char != '"')


def material_of(value):
    """A material as the unit stores it: ASCII, upper-cased, at most 8
    characters; None when nothing is left."""
    text = _ascii(value).strip().upper()[:MATERIAL_MAX].strip()
    return text or None


def code_of(value):
    """A filament code the unit can store, upper-cased, or None: 1 to 6
    printable ASCII characters without spaces. Anything else is no code."""
    if not isinstance(value, str):
        return None
    text = value.strip().upper()
    if not 1 <= len(text) <= CODE_MAX or _ascii(text) != text \
            or " " in text:
        return None
    return text


def defaults_for(material):
    """``(code, nozzle_min_c, nozzle_max_c)`` of the table row for a
    material, or None."""
    name = str(material or "").strip().upper()
    if not name:
        return None
    if name in MATERIAL_DEFAULTS:
        return MATERIAL_DEFAULTS[name]
    for index, char in enumerate(name):
        if char in "-+ ":
            return MATERIAL_DEFAULTS.get(name[0:index])
    return None


def nozzle_range(temp):
    """A spool temperature as ``(min, max)``: ±10 °C, kept in 150-400."""
    return (max(MIN_TEMP_C, int(temp) - TEMP_MARGIN_C),
            min(MAX_TEMP_C, int(temp) + TEMP_MARGIN_C))


def color_rgba_of(color_hex):
    """Spoolman's ``RRGGBB`` (opaque) or ``RRGGBBAA`` as ``0xRRGGBBAA``, or
    None when there is no valid colour."""
    text = str(color_hex or "").strip().lstrip("#")
    if len(text) not in (6, 8):
        return None
    try:
        value = int(text, 16)
    except ValueError:
        return None
    return (value << 8) | 0xFF if len(text) == 6 else value


def setting_of_tag(tag):
    """The setting a bay's unit reports, from its ``unit_summary`` tag (C2
    §4.6), in ``setting_for``'s form; None for no tag or another format."""
    if not isinstance(tag, dict) or tag.get("format") != "unit_summary":
        return None
    summary = tag.get("summary") or {}
    argb = summary.get("color_argb")
    rgba = 0 if argb is None else \
        ((int(argb) & 0xFFFFFF) << 8) | ((int(argb) >> 24) & 0xFF)
    return {"code": str(summary.get("code") or "").strip().upper(),
            "material": material_of(summary.get("material")) or "",
            "color_rgba": rgba,
            "nozzle_min_c": int(summary.get("nozzle_min_c") or 0),
            "nozzle_max_c": int(summary.get("nozzle_max_c") or 0)}


def setting_for(spool_summary, current=None, material_temps=None):
    """The setting a unit should store for a spool, or None when the spool's
    filament names no material (C4 §7.5).

    ``spool_summary`` is the cached summary (``ops.spool_summary``) and
    ``current`` what the unit reports now (``setting_of_tag``). What the
    filament names itself wins: its code (the ``filament_code`` extra), its
    colour, and its ``settings_extruder_temp`` ±10 °C. What it does not name
    keeps the unit's own value while the material is unchanged, so a write
    never replaces a more specific code or range with a default; otherwise it
    takes the table above (no colour when there is none).
    """
    summary = spool_summary or {}
    material = material_of(summary.get("material"))
    if material is None:
        return None
    same = current is not None and current.get("material") == material
    row = defaults_for(summary.get("material"))
    code = code_of(summary.get("filament_code"))
    if code is None:
        if same and code_of(current.get("code")):
            code = code_of(current["code"])
        else:
            code = (row or MATERIAL_DEFAULTS[FALLBACK_MATERIAL])[0]
    rgba = color_rgba_of(summary.get("color_hex"))
    if rgba is None:
        rgba = current["color_rgba"] if same else 0
    temp = ops.extruder_temp_of(summary.get("extruder_temp"))
    if temp is not None:
        low, high = nozzle_range(temp)
    elif same and current.get("nozzle_min_c") and current.get("nozzle_max_c"):
        low, high = current["nozzle_min_c"], current["nozzle_max_c"]
    elif row is not None:
        low, high = row[1], row[2]
    else:
        table = ops.material_extruder_temp(summary.get("material"),
                                           material_temps)
        if table is not None:
            low, high = nozzle_range(table)
        else:
            low, high = MATERIAL_DEFAULTS[FALLBACK_MATERIAL][1:]
    return {"code": code, "material": material, "color_rgba": rgba,
            "nozzle_min_c": low, "nozzle_max_c": high}


def fingerprint_summary(setting):
    """A setting in ``ops.summary_of``'s form, so a unit's report of it has
    the same ``ops.summary_fingerprint`` (C4 §6)."""
    rgba = setting["color_rgba"]
    return {"code": setting["code"] or None,
            "material": setting["material"],
            "color_hex": "%06X" % (rgba >> 8) if rgba else None}


def gcode(unit, bay, setting):
    """``OAMSM_SET_TRAY_INFO`` for a setting (C3 §4)."""
    material = setting["material"]
    if " " in material:
        # Klipper splits extended parameters with shlex (klippy/gcode.py).
        material = '"%s"' % material
    parts = ["OAMSM_SET_TRAY_INFO OAMS=%s BAY=%s" % (unit, bay)]
    if setting["code"]:
        parts.append("CODE=%s" % setting["code"])
    parts.append("MATERIAL=%s COLOR=%08X MIN=%d MAX=%d" % (
        material, setting["color_rgba"], setting["nozzle_min_c"],
        setting["nozzle_max_c"]))
    return " ".join(parts)


def facts_of(spool_id, summary):
    """What of a spool the unit's setting follows: a change in it is a
    Spoolman-side change (C4 §7.5)."""
    return (spool_id, summary.get("material"), summary.get("color_hex"),
            summary.get("filament_code"), summary.get("extruder_temp"))


def tagged_tag(tag):
    """Whether a bay's tag is one the host read itself (it has a UID, or a
    format other than the unit's own summary)."""
    return isinstance(tag, dict) and (
        tag.get("uid") is not None or tag.get("format") != "unit_summary")


# ------------------------------------------------------------------- policy


class _TrayBay(object):
    """One bay's sync memory (C4 §7.5)."""

    __slots__ = ("spool_id", "facts", "settled", "attempts", "next_at",
                 "state", "error", "refreshed_at")

    def __init__(self, spool_id, facts, now):
        self.spool_id = spool_id
        self.facts = facts           # the spool's facts as last seen
        self.settled = None          # facts that need no write
        self.attempts = 0            # writes for the current facts
        self.next_at = now + DEBOUNCE_SECONDS
        self.state = "pending"
        self.error = None
        self.refreshed_at = now


class TraySync(object):
    """Write each eligible bay's spool into its unit when they differ.

    A bay is eligible when its unit can store a setting
    (``capabilities.tray_write``, with ``tray_info`` to see the result),
    ``settings.sync_unit_tray_info`` is on, a spool is present and linked,
    and neither the bay's tag nor the spool is one the unit reads by tag.
    A write follows a Spoolman-side change only: a new link (unless the
    unit's own report made it), a re-link, or a change of the spool's
    filament. It waits ``DEBOUNCE_SECONDS``, happens only while the unit
    reports something else, and is tried at most ``MAX_ATTEMPTS`` times per
    change, so the unit's re-delivered report can never start another.
    """

    def __init__(self, run_gcode, clock, cache, reload_summary,
                 remember_written, wake=None, material_temps=None):
        self._run_gcode = run_gcode
        self._clock = clock
        self._cache = cache
        self._reload_summary = reload_summary
        self._remember_written = remember_written
        self._wake = wake
        self._material_temps = material_temps
        self._bays = {}

    def status(self, key):
        """The bay's ``tray_sync`` entry (C4 §8), or None when the bay is
        not synced."""
        entry = self._bays.get(key)
        if entry is None:
            return None
        return {"state": entry.state, "error": entry.error}

    def _later(self, seconds):
        if self._wake is not None:
            self._wake(max(0.0, seconds))

    @staticmethod
    def eligible(device, bay, enabled):
        capabilities = (device or {}).get("capabilities") or {}
        return bool(enabled and capabilities.get("tray_write")
                    and capabilities.get("tray_info") and bay.get("ready")
                    and bay.get("spool_id") is not None
                    and not tagged_tag(bay.get("tag")))

    async def on_bay(self, key, unit, device, bay, enabled):
        """One pass over one bay (the host's C2 bay entry)."""
        if not self.eligible(device, bay, enabled):
            self._bays.pop(key, None)
            return
        spool_id = bay["spool_id"]
        now = self._clock()
        entry = self._bays.get(key)
        if entry is not None and entry.spool_id == spool_id \
                and now - entry.refreshed_at >= REFRESH_SECONDS:
            # Spoolman tells nobody about its own edits: look again.
            entry.refreshed_at = now
            await self._reload_summary(spool_id)
        summary = self._cache.summary(spool_id)
        if summary is None or summary.get("tagged"):
            # Not known yet (Spoolman offline, never read), or a spool the
            # unit identifies by its tag: nothing to write.
            self._bays.pop(key, None)
            return
        facts = facts_of(spool_id, summary)
        if entry is None or entry.spool_id != spool_id:
            entry = self._bays[key] = _TrayBay(spool_id, facts, now)
            if bay.get("link_source") == "tag":
                # The unit's own report linked this spool: it already holds
                # the filament that decided the link.
                entry.settled = facts
        elif entry.facts != facts:
            entry.facts = facts
            entry.attempts = 0
            entry.error = None
            entry.next_at = now + DEBOUNCE_SECONDS
        self._later(REFRESH_SECONDS - (now - entry.refreshed_at))
        current = setting_of_tag(bay.get("tag"))
        wanted = setting_for(summary, current, self._material_temps)
        if wanted is None:
            self._bays.pop(key, None)
            return
        if wanted == current:
            entry.settled = facts
            entry.state = "in_sync"
            entry.error = None
            return
        if entry.settled == facts:
            if entry.state != "failed":
                entry.state = "kept"
            return
        if entry.attempts >= MAX_ATTEMPTS:
            entry.settled = facts
            entry.state = "failed"
            entry.error = entry.error or (
                "the unit still reports another filament after %d writes"
                % MAX_ATTEMPTS)
            return
        entry.state = "pending"
        if bay.get("positioning") or now < entry.next_at:
            self._later(max(entry.next_at - now, DEBOUNCE_SECONDS))
            return
        await self._write(key, unit, bay, entry, spool_id, wanted, now)

    async def _write(self, key, unit, bay, entry, spool_id, wanted, now):
        entry.attempts += 1
        entry.next_at = now + RETRY_SECONDS
        # Remembered first: the unit's report of this setting may arrive
        # before the command returns (C4 §4.1, the Keep rule).
        self._remember_written(key, wanted, spool_id)
        try:
            await self._run_gcode(gcode(unit, bay.get("index"), wanted))
        except Exception as error:
            entry.error = str(error)
            _LOGGER.info("openams_spoolman: filament setting of %s not "
                         "written: %s", key, error)
            if any(name in entry.error for name in PERMANENT_RESULTS):
                # No retry: this change of the spool is done with; the next
                # one is tried afresh.
                entry.settled = entry.facts
                entry.state = "failed"
                return
        else:
            entry.error = None
        self._later(RETRY_SECONDS)
