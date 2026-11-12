# The tag -> Spoolman policy for the openams_spoolman component (C4 §4 and §5).
#
# Every function takes an ``api`` (``api.SpoolmanAPI``) and speaks only the
# requests C4 §5 lists, in the order C4 §5 lists them: extra field bootstrap,
# vendor get-or-create, filament find-or-create, spool create-then-PATCH, and
# the ``location``/``extra`` mirrors. Extra values are JSON-encoded strings and
# a PATCH of ``extra`` always sends the whole merged object (C4 §5).

import json
import logging
import re

# Material densities (g/cm3) and default bed temperatures (°C). Public facts
# about common filaments; the tag's own bed temperature wins when it is set.
MATERIAL_DENSITY = {
    "PLA": 1.24, "PLA-CF": 1.29, "PETG": 1.27, "PETG-CF": 1.30,
    "ABS": 1.04, "ASA": 1.07, "HIPS": 1.04, "PC": 1.20,
    "PA": 1.14, "PA-CF": 1.19, "PA6": 1.13, "TPU": 1.21,
    "TPE": 1.20, "PVA": 1.23, "PPS": 1.35, "PP": 0.90,
}
MATERIAL_BED_TEMP = {
    "PLA": 60, "PLA-CF": 60, "PETG": 80, "PETG-CF": 80,
    "ABS": 100, "ASA": 100, "HIPS": 100, "PC": 110,
    "PA": 45, "PA-CF": 45, "PA6": 45, "TPU": 35,
    "TPE": 35, "PVA": 60, "PPS": 120, "PP": 90,
}
# Default extruder temperatures (°C) per material: what the host heats to
# before it moves the filament, when Spoolman holds no temperature for the
# spool (C4 §7.4). Public facts about common filaments.
MATERIAL_EXTRUDER_TEMP = {
    "PLA": 210, "PLA-CF": 220, "PETG": 240, "PETG-CF": 250,
    "ABS": 250, "ASA": 255, "HIPS": 240, "PC": 270,
    "PA": 270, "PA-CF": 280, "PA6": 275, "TPU": 225,
    "TPE": 225, "PVA": 200, "PP": 235, "PPS": 320,
}
# The range the host accepts (C3 §4): a spool's own value outside it is not a
# temperature the host could hold, so the table is used instead.
MIN_EXTRUDER_TEMP = 150
MAX_EXTRUDER_TEMP = 400
DEFAULT_DENSITY = 1.24
DEFAULT_BED_TEMP = 60
# A unit-decoded summary has no weight (C4 §4.1): a created spool starts at
# its filament's own weight, else this, and waits for the user to confirm.
DEFAULT_SPOOL_WEIGHT = 1000
BAMBU_VENDOR = "Bambu Lab"
MAX_VENDOR_NAME = 64
GENERIC_VENDOR = "Generic"

# The extra fields the component requires, C4 §5.2: (key, display name, type).
SPOOL_FIELDS = (("card_uids", "Card UIDs", "text"),
                ("tray_uid", "Tray UID", "text"),
                ("flow_k", "Flow K", "float"))
FILAMENT_FIELDS = (("drying_temp", "Drying temperature", "integer"),
                   ("drying_time", "Drying time", "integer"),
                   ("variant", "Variant", "text"),
                   ("filament_code", "Filament code", "text"))
# The filament extra naming the manufacturer's filament code (for example a
# Bambu filament code such as "GFB60"), which a unit that stores a per-bay
# filament setting is given (C4 §7.5). Set by the user; never written by the
# component.
CODE_FIELD = "filament_code"


def encode_extra(value):
    """A Spoolman extra value: the JSON text of ``value`` (C4 §5)."""
    return json.dumps(value)


def decode_extra(text):
    """The value behind a Spoolman extra string; a UI may have written it bare."""
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def normalize_lot(text):
    """The tag's manufacturing date as ``YYYY-MM-DD``, else unchanged (C4 §5.1).

    Bambu tags carry six-digit ``YYMMDD``; the century is padded on before the
    ``YYYYMMDD``/ISO normalizing, which also accepts an already normalized date.
    """
    if text is None:
        return None
    value = str(text).strip()
    digits = value
    if len(digits) == 6 and digits.isdigit():
        digits = "20" + digits
    if len(digits) == 8 and digits.isdigit():
        return "%s-%s-%s" % (digits[0:4], digits[4:6], digits[6:8])
    if len(value) >= 10 and value[0:4].isdigit() and value[4] == "-" \
            and value[5:7].isdigit() and value[7] == "-" and value[8:10].isdigit():
        return value[0:10]
    return value


def _material_key(material):
    return str(material or "").strip().upper().replace(" ", "")


def same_material(first, second):
    """Whether two material names are the same filament type (case and
    spaces ignored)."""
    return _material_key(first) == _material_key(second)


def _mean_int(first, second):
    values = [int(v) for v in (first, second) if v is not None]
    if not values:
        return None
    return int(round(sum(values) / float(len(values))))


def material_temp_table(overrides=None):
    """The extruder temperature table with ``overrides`` merged over it.

    ``material_temps`` in ``[openams_spoolman]`` replaces or adds individual
    entries; the built-in values stand for everything it does not name
    (C4 §7.4).
    """
    table = dict(MATERIAL_EXTRUDER_TEMP)
    table.update(overrides or {})
    return table


def parse_material_temps(text):
    """The ``material_temps`` option as ``{NAME: TEMP}`` (C4 §7.4).

    ``NAME=TEMP`` entries separated by commas or new lines. An entry that does
    not parse, or a temperature outside the range the host accepts, is logged
    and skipped: one typo must not stop the component.
    """
    overrides = {}
    for entry in re.split(r"[,\n]", text or ""):
        name, sep, value = entry.partition("=")
        name = name.strip().upper()
        if not sep or not name:
            if entry.strip():
                logging.warning(
                    "openams_spoolman: material_temps entry '%s' is not "
                    "NAME=TEMP; ignored", entry.strip())
            continue
        try:
            temp = int(value.strip())
        except ValueError:
            logging.warning(
                "openams_spoolman: material_temps %s='%s' is not a number; "
                "ignored", name, value.strip())
            continue
        if not MIN_EXTRUDER_TEMP <= temp <= MAX_EXTRUDER_TEMP:
            logging.warning(
                "openams_spoolman: material_temps %s=%d is outside %d-%d; "
                "ignored", name, temp, MIN_EXTRUDER_TEMP, MAX_EXTRUDER_TEMP)
            continue
        overrides[name] = temp
    return overrides


def parse_tag_vendor(text):
    """The ``tag_vendor`` option as a vendor name (C4 §5.3).

    The vendor a Bambu tag's new vendor, filament and spool are created
    under. Empty or unset is "Bambu Lab"; a name over 64 characters (what the
    edit endpoint takes, C4 §9) is logged and replaced by it: one typo must
    not stop the component.
    """
    name = str(text or "").strip()
    if not name:
        return BAMBU_VENDOR
    if len(name) > MAX_VENDOR_NAME:
        logging.warning(
            "openams_spoolman: tag_vendor is longer than %d characters; "
            "using '%s'", MAX_VENDOR_NAME, BAMBU_VENDOR)
        return BAMBU_VENDOR
    return name


def material_extruder_temp(material, table=None):
    """The table temperature for a material, or None (C4 §7.4).

    The material, upper-cased and trimmed, is matched exactly first; a
    variant then falls back to the part before its first ``-``, ``+`` or
    space, so ``PLA+`` and ``ABS-GF`` take ``PLA`` and ``ABS``.
    """
    table = MATERIAL_EXTRUDER_TEMP if table is None else table
    name = str(material or "").strip().upper()
    if not name:
        return None
    if name in table:
        return table[name]
    for index, char in enumerate(name):
        if char in "-+ ":
            return table.get(name[0:index])
    return None


def extruder_temp_of(value):
    """A temperature the host can hold as an int, or None (C4 §7.4).

    A spool's own ``settings_extruder_temp`` wins the material table, but only
    when it is one the host would accept; anything else (a float string, 0, a
    thousand) is not a temperature this machine can heat to, so the caller
    falls back to the table.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        temp = int(float(value))
    except (TypeError, ValueError):
        return None
    if not MIN_EXTRUDER_TEMP <= temp <= MAX_EXTRUDER_TEMP:
        return None
    return temp


def remaining_pct_of(value):
    """A spool's remaining percent as the host keeps it: an int 0-100, or
    None for anything that is not a number (C4 §7.4). A spool used past its
    initial weight reads 0, one refilled past it 100."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value:            # NaN
        return None
    return max(0, min(100, int(round(value))))


def material_name_of(value):
    """A filament's material as the host stores it, or None (C4 §7.4)."""
    name = str(value or "").strip()
    return name[0:16] or None


def _split_uids(value):
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    return [str(item).strip().upper() for item in items if str(item).strip()]


# ------------------------------------------------ two-tag identity (C4 §4.3)
#
# ``valid_tray`` and ``spool_key`` are a copy of ``src/tag_codec.py``'s, with
# the same behaviour (the two-tag interfaces, §1): the host, the display and
# this component derive the same spool identity from one tag, with no registry.

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def valid_tray(value):
    """Whether ``value`` is a usable Bambu tray UUID: a string of exactly 32
    hex digits once trimmed, not all ``0`` and not all ``f``/``F``. A blank
    block 9 (all zero or all F) would otherwise make every such tag the same
    spool."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if len(text) != 32 or any(char not in _HEX_DIGITS for char in text):
        return False
    text = text.lower()
    return text != "0" * 32 and text != "f" * 32


def spool_key(tag):
    """The spool a tag belongs to: ``bambu:<tray UUID>`` (lower case) for a
    Bambu tag with a valid tray UUID, shared by both tags of one spool; else
    ``uid:<UID>`` (upper case); else None (a ``unit_summary`` has no UID)."""
    if not isinstance(tag, dict):
        return None
    if tag.get("format") == "bambu":
        bambu = tag.get("bambu")
        tray = bambu.get("tray_uid") if isinstance(bambu, dict) else None
        if valid_tray(tray):
            return "bambu:" + tray.strip().lower()
    uid = tag.get("uid")
    if uid is not None and str(uid).strip():
        return "uid:" + str(uid).strip().upper()
    return None


def tag_key(tag):
    """The tag's spool key: the host's ``spool_key`` when it publishes one
    (C2, additive), else computed by ``spool_key``."""
    key = tag.get("spool_key") if isinstance(tag, dict) else None
    if isinstance(key, str) and key.strip():
        return key.strip()
    return spool_key(tag)


def tag_uids(tag):
    """The tag's UIDs in upper case, the one read on this bay first: the
    host's ``uids`` when it publishes them (every UID it knows for the spool
    key, the read one first), else the read ``uid`` alone. A ``uid`` the
    host list lacks comes after it."""
    if not isinstance(tag, dict):
        return []
    uids = []
    host = tag.get("uids")
    for uid in (host if isinstance(host, (list, tuple)) else []):
        value = str(uid or "").strip().upper()
        if value and value not in uids:
            uids.append(value)
    read = str(tag.get("uid") or "").strip().upper()
    if read and read not in uids:
        uids.append(read)
    return uids


def tag_tray(tag):
    """The tag's tray UUID, trimmed and lower case, or None when the tag is
    not a decoded Bambu tag or the tray UUID is not valid."""
    if not isinstance(tag, dict) or tag.get("format") != "bambu":
        return None
    bambu = tag.get("bambu")
    tray = bambu.get("tray_uid") if isinstance(bambu, dict) else None
    return tray.strip().lower() if valid_tray(tray) else None


def tag_decoded(tag):
    """Whether a tag carries decoded Bambu content to create a spool from. A
    tag seen while passing the reader is attributed by its UID before it is
    decoded (``bambu`` null or empty): nothing is ever created from it."""
    if not isinstance(tag, dict) or tag.get("format") != "bambu":
        return False
    bambu = tag.get("bambu")
    return isinstance(bambu, dict) and \
        any(value not in (None, "") for value in bambu.values())


def spool_uids(spool):
    """A spool's ``card_uids`` as a list of upper-case UIDs."""
    extra = (spool or {}).get("extra") or {}
    return _split_uids(decode_extra(extra.get("card_uids")))


def spool_tray(spool):
    """A spool's ``tray_uid``, trimmed and lower case, or None when it has
    none or the value is not a valid tray UUID (blank or malformed)."""
    extra = (spool or {}).get("extra") or {}
    tray = decode_extra(extra.get("tray_uid"))
    return tray.strip().lower() if valid_tray(tray) else None


def match_tag(tag, spools):
    """The spool a tag belongs to, by the resolution order of C4 §4.2:
    ``(spool, how)``, or ``(None, None)``.

    1. ``uid``: a spool whose ``card_uids`` lists the UID read on the bay. A
       UID a person put on a spool wins, which keeps two clones apart.
    2. ``tray``: a spool whose ``tray_uid`` is the tag's tray UUID, compared
       trimmed and in any case, and only for a valid tray UUID.
    3. ``sibling``: a spool whose ``card_uids`` lists another UID the host
       publishes for this spool key (``tag.uids``), when that spool has no
       other tray UUID and the host does not report two bays sharing the key.
    """
    live = [spool for spool in spools or []
            if isinstance(spool, dict) and spool.get("id") is not None]
    uids = tag_uids(tag)
    tray = tag_tray(tag)
    if uids:
        for spool in live:
            if uids[0] in spool_uids(spool):
                return spool, "uid"
    if tray:
        for spool in live:
            if spool_tray(spool) == tray:
                return spool, "tray"
    if not (tag or {}).get("shared_identity"):
        for uid in uids[1:]:
            for spool in live:
                if uid in spool_uids(spool) and \
                        spool_tray(spool) in (None, tray):
                    return spool, "sibling"
    return None, None


def tray_holder(spools, tray, other_than=None):
    """The first spool (not ``other_than``) whose tray UUID is ``tray``."""
    if not tray:
        return None
    for spool in spools or []:
        if isinstance(spool, dict) and spool.get("id") is not None \
                and spool.get("id") != other_than \
                and spool_tray(spool) == tray:
            return spool
    return None


def tag_extra(spool, uid=None, tray=None):
    """The spool's whole ``extra`` with ``uid`` added to ``card_uids`` and
    ``tray`` written as ``tray_uid`` when the spool has none (C4 §4.2 step 4),
    or None when neither is new. A write normalises both values: UIDs upper
    case, sorted, comma separated; the tray UUID lower case."""
    extra = dict((spool or {}).get("extra") or {})
    uids = spool_uids(spool)
    own = spool_tray(spool)
    value = str(uid or "").strip().upper()
    add_uid = bool(value) and value not in uids
    add_tray = bool(tray) and own is None
    if not add_uid and not add_tray:
        return None
    if add_uid:
        uids.append(value)
    extra["card_uids"] = encode_extra(",".join(sorted(set(uids))))
    tray_value = tray if add_tray else own
    if tray_value:
        extra["tray_uid"] = encode_extra(tray_value.strip().lower())
    return extra


def _same_text(first, second):
    return str(first or "").strip().lower() == \
        str(second or "").strip().lower()


def filament_matches_tag(spool, bambu, vendor_name=BAMBU_VENDOR):
    """Whether a spool's filament is the one a Bambu tag decodes to, as §5.4
    finds it: vendor (the ``tag_vendor`` option), material, name
    (``detailed``) and colour. Text is compared trimmed and in any case."""
    filament = (spool or {}).get("filament") or {}
    vendor = (filament.get("vendor") or {}).get("name")
    if not _same_text(vendor, vendor_name):
        return False
    if not same_material(filament.get("material"), bambu.get("type")):
        return False
    if not _same_text(filament.get("name"), bambu.get("detailed")):
        return False
    color_hex = "%06X" % (int(bambu.get("color_argb") or 0) & 0xFFFFFF)
    return str(filament.get("color_hex") or "").strip().upper() == color_hex


def legacy_spools(spools, tag, bay_key, excluded=(),
                  vendor_name=BAMBU_VENDOR):
    """The spools a decoded tag may be the *other* tag of (C4 §4.2 step 6):
    not archived, not linked to another bay, with the tag's filament, at least
    one ``card_uids`` entry, no tray UUID, and no location or this bay's.
    Such a spool was made from its other tag by a tool that keeps no tray
    UUID (AFC, by hand): it is suggested, never linked or merged silently."""
    bambu = (tag or {}).get("bambu")
    if not isinstance(bambu, dict):
        return []
    found = []
    for spool in spools or []:
        if not isinstance(spool, dict) or spool.get("id") is None \
                or spool.get("archived") or spool.get("id") in excluded:
            continue
        if not spool_uids(spool) or spool_tray(spool) is not None:
            continue
        location = str(spool.get("location") or "").strip()
        if location and location != bay_key:
            continue
        if filament_matches_tag(spool, bambu, vendor_name):
            found.append(spool)
    return found


async def find_archived(api, tag):
    """An archived spool the tag belongs to, by ``match_tag``'s order, or
    None (C4 §4.2 step 5): ``GET /v1/spool`` leaves archived spools out, so a
    finished spool put back in a bay would otherwise be created again."""
    spools = await api.request("GET", "/v1/spool",
                               query={"allow_archived": "true"}) or []
    archived = [spool for spool in spools
                if isinstance(spool, dict) and spool.get("archived")]
    return match_tag(tag, archived)[0]


def duplicate_pairs(spools):
    """Spools that claim one identity (C4 §9.2): two that hold one tray UUID
    (``shared_tray``), or that list one tag UID (``shared_uid``). Archived
    spools are left out. Each unordered pair once, the first reason wins."""
    live = [spool for spool in spools or []
            if isinstance(spool, dict) and spool.get("id") is not None
            and not spool.get("archived")]
    pairs, seen = [], set()

    def add(first, second, reason, tray=None, uid=None):
        pair = frozenset((first, second))
        if first == second or pair in seen:
            return
        seen.add(pair)
        pairs.append({"spools": [first, second], "reason": reason,
                      "tray_uid": tray, "uid": uid})

    trays, uids = {}, {}
    for spool in live:
        tray = spool_tray(spool)
        if tray:
            trays.setdefault(tray, []).append(spool["id"])
        for uid in spool_uids(spool):
            if spool["id"] not in uids.setdefault(uid, []):
                uids[uid].append(spool["id"])
    for tray, ids in trays.items():
        for other in ids[1:]:
            add(ids[0], other, "shared_tray", tray=tray)
    for uid, ids in uids.items():
        for other in ids[1:]:
            add(ids[0], other, "shared_uid", uid=uid)
    return pairs


def tag_gaps(spools):
    """The spools whose tag identity is incomplete (C4 §9.2), archived ones
    left out: ``without_tray`` lists a tag UID but holds no tray UUID (made
    from one tag by a tool that keeps none: the next read of that tag
    completes it, and until then its other tag is only suggested);
    ``invalid_tray`` holds a ``tray_uid`` that is not a tray UUID (blank or
    malformed: it never matches a tag). Spool ids, in Spoolman's order."""
    without, invalid = [], []
    for spool in spools or []:
        if not isinstance(spool, dict) or spool.get("id") is None \
                or spool.get("archived"):
            continue
        raw = decode_extra(((spool.get("extra") or {}).get("tray_uid")))
        if spool_tray(spool) is None:
            if raw not in (None, "") and str(raw).strip():
                invalid.append(spool["id"])
            elif spool_uids(spool):
                without.append(spool["id"])
    return {"without_tray": without, "invalid_tray": invalid}


def merge_plan(keep, other):
    """What merging spool ``other`` into ``keep`` writes (C4 §9.2): the union
    of both ``card_uids`` and the one tray UUID. Raises ValueError when they
    hold two different tray UUIDs: they are then two spools, not one."""
    keep_tray, other_tray = spool_tray(keep), spool_tray(other)
    if keep_tray and other_tray and keep_tray != other_tray:
        raise ValueError("spools %s and %s hold different tray UIDs; they "
                         "are two spools" % (keep.get("id"), other.get("id")))
    return {"card_uids": sorted(set(spool_uids(keep)) |
                                set(spool_uids(other))),
            "tray_uid": keep_tray or other_tray}


async def merge_spools(api, keep, other, plan):
    """Write a merge (C4 §9.2): the kept spool first gets every UID and the
    tray UUID, then the other is archived with its ``card_uids``,
    ``tray_uid`` and ``location`` cleared, so an archived spool never claims
    a tag. Kept first: a failure half way leaves both listing the UIDs, which
    the duplicates report shows and a second merge finishes. Returns both
    spools as Spoolman now holds them."""
    extra = dict(keep.get("extra") or {})
    extra["card_uids"] = encode_extra(",".join(plan["card_uids"]))
    if plan.get("tray_uid"):
        extra["tray_uid"] = encode_extra(plan["tray_uid"])
    kept = await api.request("PATCH", "/v1/spool/%d" % keep["id"],
                             body={"extra": extra})
    cleared = dict(other.get("extra") or {})
    cleared["card_uids"] = encode_extra("")
    cleared["tray_uid"] = encode_extra("")
    archived = await api.request(
        "PATCH", "/v1/spool/%d" % other["id"],
        body={"archived": True, "location": "", "extra": cleared})
    return kept, archived


async def ensure_fields(api):
    """POST the extra fields Spoolman does not have yet (C4 §5.2).

    Returns the keys created, so a caller can log them; a second call creates
    nothing.
    """
    created = []
    for entity, specs in (("spool", SPOOL_FIELDS), ("filament", FILAMENT_FIELDS)):
        existing = set()
        for field in await api.request("GET", "/v1/field/%s" % entity) or []:
            existing.add(field["key"])
        for key, name, field_type in specs:
            if key in existing:
                continue
            await api.request(
                "POST", "/v1/field/%s/%s" % (entity, key),
                body={"name": name, "field_type": field_type})
            created.append(key)
    return created


async def get_or_create_vendor(api, name):
    """The vendor named ``name``; created when Spoolman has none (C4 §5.3)."""
    found = await api.request("GET", "/v1/vendor", query={"name": name}) or []
    for vendor in found:
        if vendor.get("name") == name:
            return vendor
    return await api.request("POST", "/v1/vendor", body={"name": name})


async def _vendor_name(api, vendor_id):
    """The name of ``vendor_id``: §5.4 filters filaments by ``vendor.name``."""
    for vendor in await api.request("GET", "/v1/vendor") or []:
        if vendor.get("id") == vendor_id:
            return vendor.get("name")
    return None


async def _filament_candidates(api, vendor_id, material=None):
    """The filaments of a vendor (and material when given), as §5.4 queries them."""
    query = {}
    if material:
        query["material"] = material
    vendor_name = await _vendor_name(api, vendor_id)
    if vendor_name:
        query["vendor.name"] = vendor_name
    return await api.request("GET", "/v1/filament", query=query) or []


async def _create_filament(api, body, extra=None):
    """POST a filament; its extras go in a PATCH, since the create body stays
    on Spoolman's own fields (§5.4). Returns the filament as last written."""
    filament = await api.request("POST", "/v1/filament", body=body)
    if extra:
        filament = await api.request(
            "PATCH", "/v1/filament/%d" % filament["id"], body={"extra": extra})
    return filament


async def find_or_create_filament(api, vendor_id, bambu):
    """The filament matching a Bambu tag, created from it when absent (C4 §5.4)."""
    name = bambu.get("detailed")
    material = bambu.get("type")
    color_hex = "%06X" % (int(bambu.get("color_argb") or 0) & 0xFFFFFF)
    for filament in await _filament_candidates(api, vendor_id, material):
        if filament.get("name") == name \
                and str(filament.get("color_hex") or "").upper() == color_hex:
            return filament
    key = _material_key(material)
    extra = {"variant": encode_extra(name)}
    if bambu.get("drying_temp_c") is not None:
        extra["drying_temp"] = encode_extra(int(bambu["drying_temp_c"]))
    if bambu.get("drying_time_h") is not None:
        extra["drying_time"] = encode_extra(int(bambu["drying_time_h"]))
    return await _create_filament(api, {
        "name": name,
        "vendor_id": vendor_id,
        "material": material,
        "density": MATERIAL_DENSITY.get(key, DEFAULT_DENSITY),
        "diameter": bambu.get("diameter_mm", 1.75),
        "color_hex": color_hex,
        "weight": bambu.get("weight_g"),
        "settings_extruder_temp": _mean_int(bambu.get("hotend_min_c"),
                                            bambu.get("hotend_max_c")),
        "settings_bed_temp": bambu.get("bed_temp_c")
        or MATERIAL_BED_TEMP.get(key, DEFAULT_BED_TEMP)}, extra)


async def find_or_create_filament_by(api, vendor_id, material, color_hex=None,
                                     current=None, weight=None):
    """The filament of a vendor with this material and color, created when
    absent: the UI edit path (C4 §9).

    An existing filament is never modified, since other spools may use it.
    Matching is on vendor, material (any case), color (any case; both absent
    match) and, when ``current`` (the spool's present filament) has one, the
    diameter; a filament named like ``current`` wins a tie. A new filament
    carries ``current``'s diameter and weight, and, when the material is
    unchanged, its name, density and temperatures too; a changed material
    takes the defaults above, and a filament with no ``current`` takes them all.
    """
    current = current or {}
    wanted = str(color_hex or "").lower()
    diameter = current.get("diameter")
    key = _material_key(material)
    matches = []
    # No material in the query: Spoolman's match may be case-sensitive.
    for filament in await _filament_candidates(api, vendor_id):
        if _material_key(filament.get("material")) != key:
            continue
        if str(filament.get("color_hex") or "").lower() != wanted:
            continue
        if diameter is not None and filament.get("diameter") is not None \
                and float(filament["diameter"]) != float(diameter):
            continue
        matches.append(filament)
    for filament in matches:
        if current.get("name") and filament.get("name") == current["name"]:
            return filament
    if matches:
        return matches[0]
    same = _material_key(current.get("material")) == key
    body = {
        "name": current.get("name") if same and current.get("name")
        else material,
        "vendor_id": vendor_id,
        "material": material,
        "density": (current.get("density") if same else None)
        or MATERIAL_DENSITY.get(key, DEFAULT_DENSITY),
        "diameter": diameter if diameter is not None else 1.75,
        "weight": current.get("weight") if current.get("weight") is not None
        else weight,
        "settings_bed_temp": (current.get("settings_bed_temp") if same
                              else None)
        or MATERIAL_BED_TEMP.get(key, DEFAULT_BED_TEMP)}
    if wanted:
        body["color_hex"] = wanted
    if same and current.get("settings_extruder_temp") is not None:
        body["settings_extruder_temp"] = current["settings_extruder_temp"]
    body = dict((k, v) for k, v in body.items() if v is not None)
    return await _create_filament(api, body)


async def create_spool(api, filament_id, tag, location):
    """A new, full spool for a tag, located and carrying its UIDs (C4 §5.5)."""
    bambu = tag.get("bambu") or {}
    weight = bambu.get("weight_g")
    spool = await api.request("POST", "/v1/spool", body={
        "filament_id": filament_id,
        "initial_weight": weight,
        "remaining_weight": weight})
    extra = {}
    tray = tag_tray(tag)
    if tray:
        # Only a valid tray UUID, normalised: a blank one would make the
        # spool claim every tag with a blank block 9 (C4 §4.2).
        extra["tray_uid"] = encode_extra(tray)
    uids = tag_uids(tag)
    if uids:
        # The UID read on the bay only: another UID the host knows for the
        # key may be a clone's (C4 §4.2).
        extra["card_uids"] = encode_extra(uids[0])
    body = {"location": location or ""}
    lot_nr = normalize_lot(bambu.get("production"))
    if lot_nr:
        body["lot_nr"] = lot_nr
    if extra:
        body["extra"] = extra
    return await api.request("PATCH", "/v1/spool/%d" % spool["id"], body=body)


async def find_spool_by_tag(api, tag, spools=None):
    """The spool a tag belongs to (C4 §4.2), by ``match_tag``'s order: a spool
    that lists the tag's UID in ``card_uids`` first, then the one with its
    valid tray UUID (any case, trimmed), then one that lists a sibling UID.

    A UID somebody put on a spool is a deliberate choice; a tray UID is shared
    by every clone of one tag dump and by both tags of one genuine spool.
    """
    if spools is None:
        spools = await api.request("GET", "/v1/spool") or []
    return match_tag(tag, spools)[0]


async def add_card_uid(api, spool, uid):
    """Add a tag UID to a spool's ``card_uids``, sorted, no duplicates (§5.7)."""
    extra = dict(spool.get("extra") or {})
    uids = _split_uids(decode_extra(extra.get("card_uids")))
    value = str(uid or "").strip().upper()
    if value and value not in uids:
        uids.append(value)
    extra["card_uids"] = encode_extra(",".join(sorted(uids)))
    return await api.request("PATCH", "/v1/spool/%d" % spool["id"],
                             body={"extra": extra})


async def set_location(api, spool_id, location):
    """Mirror the bay on the spool; ``""`` clears it (C4 §5.6)."""
    return await api.request("PATCH", "/v1/spool/%d" % spool_id,
                             body={"location": location or ""})


def flow_k_of(spool):
    """The spool's ``extra.flow_k`` pressure advance, or None (C4 §7.2).

    Read exactly as ``active.py`` applies it: a value that is not a number,
    or a bool, is no pressure advance and the PA step is skipped.
    """
    value = decode_extra((spool.get("extra") or {}).get("flow_k"))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def spool_summary(spool):
    """The C4 §8 ``spool`` object for a spool as Spoolman returns it.

    ``extruder_temp`` is what the host heats to when the spool carries no
    temperature of its own (C4 §7.4). It is a working value, not a §8 field,
    so it appears only when there is one to say: §8 publishes this object
    under a schema that names its keys, and a null one would add a key that
    schema does not. A caller reads an absent value as None either way.
    """
    filament = spool.get("filament") or {}
    vendor = filament.get("vendor") or {}
    remaining = spool.get("remaining_weight")
    initial = spool.get("initial_weight")
    percent = None
    if remaining is not None and initial:
        percent = int(round(remaining / float(initial) * 100))
    color_hex = str(filament.get("color_hex") or "").upper()
    summary = {"material": filament.get("material"),
               "color_hex": color_hex or None,
               "vendor": vendor.get("name"),
               "remaining_g": remaining,
               "remaining_pct": percent,
               "initial_g": initial if initial is not None
               else filament.get("weight"),
               "flow_k": flow_k_of(spool)}
    temp = filament.get("settings_extruder_temp")
    if temp is not None:
        summary["extruder_temp"] = temp
    # Working values for the slicer's filament list (lane_data), likewise
    # present only when there is one to say.
    bed = filament.get("settings_bed_temp")
    if bed is not None:
        summary["bed_temp"] = bed
    if filament.get("name"):
        summary["name"] = filament["name"]
    # Two more working values, for a unit's stored filament setting (C4
    # §7.5), each present only when there is one to say: the filament's own
    # code, and whether Spoolman knows the spool by a tag (a tray UID or
    # card UIDs), whose reading the unit then trusts over any setting.
    code = decode_extra((filament.get("extra") or {}).get(CODE_FIELD))
    if isinstance(code, str) and code.strip():
        summary["filament_code"] = code.strip()
    extra = spool.get("extra") or {}
    if decode_extra(extra.get("tray_uid")) or \
            _split_uids(decode_extra(extra.get("card_uids"))):
        summary["tagged"] = True
    return summary


# --------------------------------------------- unit-decoded summaries (C4 §4.1)


def summary_of(tag):
    """The facts a ``unit_summary`` tag carries, normalized for matching.

    ``color_hex`` is the six uppercase hex digits of ``color_argb`` (alpha
    dropped, as for a Bambu tag), or None when the unit reported no colour.
    """
    summary = tag.get("summary") or {}
    argb = summary.get("color_argb")
    color_hex = None if argb is None else "%06X" % (int(argb) & 0xFFFFFF)
    return {"code": summary.get("code") or None,
            "material": str(summary.get("material") or "").strip() or None,
            "color_hex": color_hex,
            "manufacturer": summary.get("manufacturer"),
            "nozzle_min_c": summary.get("nozzle_min_c"),
            "nozzle_max_c": summary.get("nozzle_max_c")}


def summary_fingerprint(summary):
    """The offline cache's identity of a summary: code, material, colour."""
    return "%s|%s|%s" % (str(summary.get("code") or "").lower(),
                         _material_key(summary.get("material")).lower(),
                         str(summary.get("color_hex") or "").upper())


def _vendor_matches(manufacturer, vendor, bambu_vendor=BAMBU_VENDOR):
    """A known manufacturer must be the spool's vendor; an unknown one (None)
    constrains nothing. Only Bambu is known from a filament code, and its
    spools are filed under ``bambu_vendor`` (the ``tag_vendor`` option) or
    "Bambu Lab"."""
    if not manufacturer:
        return True
    wanted = str(manufacturer).strip().lower()
    have = str(vendor or "").strip().lower()
    if wanted == "bambu":
        return have in (wanted, BAMBU_VENDOR.lower(), bambu_vendor.lower())
    return have == wanted


def summary_matches(spool_summary, summary, bambu_vendor=BAMBU_VENDOR):
    """Whether a spool's filament (a C4 §8 ``spool`` summary) is the one a
    unit-decoded summary describes: material, colour when the unit reported
    one, and vendor when the manufacturer is known. The filament code is not
    stored in Spoolman, so it is not compared."""
    if not same_material(spool_summary.get("material"),
                         summary.get("material")):
        return False
    wanted = summary.get("color_hex")
    if wanted and str(spool_summary.get("color_hex") or "").upper() != wanted:
        return False
    return _vendor_matches(summary.get("manufacturer"),
                           spool_summary.get("vendor"), bambu_vendor)


def matching_spools(spools, summary, excluded=(), bambu_vendor=BAMBU_VENDOR):
    """The spools a summary may belong to: not archived, not in ``excluded``
    (linked to another bay), and whose filament matches (C4 §4.1)."""
    found = []
    for spool in spools:
        if not isinstance(spool, dict) or spool.get("archived"):
            continue
        if spool.get("id") in excluded:
            continue
        if summary_matches(spool_summary(spool), summary, bambu_vendor):
            found.append(spool)
    return found


def suggestion_of(spool):
    """One entry of a bay's ``suggestions`` list (C4 §8)."""
    summary = spool_summary(spool)
    entry = {"spool_id": spool["id"]}
    for key in ("material", "color_hex", "vendor", "remaining_g"):
        entry[key] = summary.get(key)
    entry["location"] = spool.get("location") or ""
    return entry


async def create_summary_spool(api, summary, location,
                               bambu_vendor=BAMBU_VENDOR):
    """Vendor, filament and spool for a summary no spool matches (C4 §4.1).

    The vendor is ``bambu_vendor`` (the ``tag_vendor`` option, "Bambu Lab" by
    default) for a Bambu filament code, else "Generic". The
    filament is found or created by vendor, material and colour; the spool is
    full by its filament's weight (1000 g when it has none) and carries no
    tag UID or tray UID, since the summary has neither.
    """
    vendor_name = bambu_vendor if summary.get("manufacturer") == "Bambu" \
        else GENERIC_VENDOR
    vendor = await get_or_create_vendor(api, vendor_name)
    current = {"material": summary["material"], "name": summary["material"]}
    temp = _mean_int(summary.get("nozzle_min_c"), summary.get("nozzle_max_c"))
    if temp:
        current["settings_extruder_temp"] = temp
    filament = await find_or_create_filament_by(
        api, vendor["id"], summary["material"], summary.get("color_hex"),
        current=current, weight=DEFAULT_SPOOL_WEIGHT)
    weight = filament.get("weight") or DEFAULT_SPOOL_WEIGHT
    spool = await api.request("POST", "/v1/spool", body={
        "filament_id": filament["id"], "initial_weight": weight,
        "remaining_weight": weight})
    return await api.request("PATCH", "/v1/spool/%d" % spool["id"],
                             body={"location": location or ""})
