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


def material_name_of(value):
    """A filament's material as the host stores it, or None (C4 §7.4)."""
    name = str(value or "").strip()
    return name[0:16] or None


def _split_uids(value):
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    return [str(item).strip().upper() for item in items if str(item).strip()]


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
    if bambu.get("tray_uid"):
        extra["tray_uid"] = encode_extra(bambu["tray_uid"])
    uid = str(tag.get("uid") or "").strip().upper()
    if uid:
        extra["card_uids"] = encode_extra(uid)
    body = {"location": location or ""}
    lot_nr = normalize_lot(bambu.get("production"))
    if lot_nr:
        body["lot_nr"] = lot_nr
    if extra:
        body["extra"] = extra
    return await api.request("PATCH", "/v1/spool/%d" % spool["id"], body=body)


async def find_spool_by_tag(api, tag, spools=None):
    """The spool a Bambu tag belongs to (C4 §4): ``tray_uid`` first, then UID."""
    if spools is None:
        spools = await api.request("GET", "/v1/spool") or []
    tray_uid = (tag.get("bambu") or {}).get("tray_uid")
    if tray_uid:
        for spool in spools:
            if decode_extra((spool.get("extra") or {}).get("tray_uid")) == tray_uid:
                return spool
    uid = str(tag.get("uid") or "").strip().upper()
    if uid:
        for spool in spools:
            extra = spool.get("extra") or {}
            if uid in _split_uids(decode_extra(extra.get("card_uids"))):
                return spool
    return None


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


def _vendor_matches(manufacturer, vendor):
    """A known manufacturer must be the spool's vendor; an unknown one (None)
    constrains nothing. Only Bambu is known from a filament code."""
    if not manufacturer:
        return True
    wanted = str(manufacturer).strip().lower()
    have = str(vendor or "").strip().lower()
    return have in (wanted, BAMBU_VENDOR.lower()) if wanted == "bambu" \
        else have == wanted


def summary_matches(spool_summary, summary):
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
                           spool_summary.get("vendor"))


def matching_spools(spools, summary, excluded=()):
    """The spools a summary may belong to: not archived, not in ``excluded``
    (linked to another bay), and whose filament matches (C4 §4.1)."""
    found = []
    for spool in spools:
        if not isinstance(spool, dict) or spool.get("archived"):
            continue
        if spool.get("id") in excluded:
            continue
        if summary_matches(spool_summary(spool), summary):
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


async def create_summary_spool(api, summary, location):
    """Vendor, filament and spool for a summary no spool matches (C4 §4.1).

    The vendor is "Bambu Lab" for a Bambu filament code, else "Generic". The
    filament is found or created by vendor, material and colour; the spool is
    full by its filament's weight (1000 g when it has none) and carries no
    tag UID or tray UID, since the summary has neither.
    """
    vendor_name = BAMBU_VENDOR if summary.get("manufacturer") == "Bambu" \
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
