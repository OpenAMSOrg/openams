# The UI spool edit (C4 §9): set a bay's material, color, vendor, weights and
# pressure advance in Spoolman through one call.
#
# ``parse`` validates the request; ``apply`` does the Spoolman writes (and, for
# an unlinked bay, the host link). It raises ``EditError`` with a message a
# person can read; the component turns that into a JSON-RPC error.

import re

from . import ops
from .api import SpoolmanError, SpoolmanUnavailable

DEFAULT_VENDOR = "Generic"
DEFAULT_INITIAL_WEIGHT = 1000.0
OFFLINE_MESSAGE = "Spoolman is offline"
_COLOR = re.compile(r"^[0-9a-fA-F]{6}$")
FIELDS = ("material", "color_hex", "vendor", "remaining_weight",
          "initial_weight", "flow_k")


class EditError(Exception):
    def __init__(self, message, status=400):
        Exception.__init__(self, message)
        self.status = status


def _number(args, name):
    value = args[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or value != value or value in (float("inf"), float("-inf")):
        raise EditError("%s must be a number" % name)
    return float(value)


def _text(args, name, limit):
    value = args[name]
    if not isinstance(value, str):
        raise EditError("%s must be a string" % name)
    value = value.strip()
    if not 1 <= len(value) <= limit:
        raise EditError("%s must be 1 to %d characters" % (name, limit))
    return value


def parse(args):
    """Validate the request's ``unit``, ``bay`` and edit fields.

    Returns ``(unit, bay, fields)``; ``fields`` holds only the fields given,
    in their stored form (``color_hex`` lowercase, numbers as floats).
    """
    unit = args.get("unit")
    if not isinstance(unit, str) or not unit.strip():
        raise EditError("unit is required and must be a string")
    bay = args.get("bay")
    if isinstance(bay, bool) or not isinstance(bay, int) or bay < 0:
        raise EditError("bay is required and must be an integer, 0 or more")
    fields = {}
    if args.get("material") is not None:
        fields["material"] = _text(args, "material", 32)
    if args.get("vendor") is not None:
        fields["vendor"] = _text(args, "vendor", 64)
    if args.get("color_hex") is not None:
        value = args["color_hex"]
        if not isinstance(value, str) or not _COLOR.match(value):
            raise EditError("color_hex must be exactly 6 hex digits, "
                            "without '#'")
        fields["color_hex"] = value.lower()
    if args.get("remaining_weight") is not None:
        fields["remaining_weight"] = _number(args, "remaining_weight")
        if fields["remaining_weight"] < 0:
            raise EditError("remaining_weight must be 0 g or more")
    if args.get("initial_weight") is not None:
        fields["initial_weight"] = _number(args, "initial_weight")
        if fields["initial_weight"] <= 0:
            raise EditError("initial_weight must be more than 0 g")
    if args.get("flow_k") is not None:
        fields["flow_k"] = _number(args, "flow_k")
        if not 0 <= fields["flow_k"] <= 2:
            raise EditError("flow_k must be between 0 and 2")
    if not fields:
        raise EditError("nothing to change: give at least one of %s"
                        % ", ".join(FIELDS))
    return unit.strip(), bay, fields


def _weights(fields, spool, filament):
    """The spool PATCH fields for the weights given, or ``{}``.

    Both weights are written as ``initial_weight`` and ``used_weight``, so the
    result does not depend on how Spoolman orders them inside one PATCH. A
    changed initial weight alone keeps the remaining weight.
    """
    if "initial_weight" not in fields and "remaining_weight" not in fields:
        return {}
    patch = {}
    initial = fields.get("initial_weight")
    if initial is None:
        initial = spool.get("initial_weight")
        if initial is None:
            initial = filament.get("weight")
    else:
        patch["initial_weight"] = initial
    remaining = fields.get("remaining_weight", spool.get("remaining_weight"))
    if initial is None or remaining is None:
        if "remaining_weight" in fields:
            patch["remaining_weight"] = fields["remaining_weight"]
        return patch
    if remaining > initial:
        name = "remaining_weight" if "remaining_weight" in fields \
            else "initial_weight"
        raise EditError("%s: the remaining weight (%g g) cannot exceed the "
                        "initial weight (%g g)" % (name, remaining, initial))
    patch["used_weight"] = round(initial - remaining, 3)
    return patch


async def _retarget(api, filament, fields):
    """The id of the filament a spool should use after a material, color or
    vendor change, or None when its present filament already fits."""
    vendor_name = fields.get("vendor") or \
        (filament.get("vendor") or {}).get("name") or DEFAULT_VENDOR
    material = fields.get("material") or filament.get("material")
    if not material:
        raise EditError("material: the spool's filament has none, "
                        "so one is needed")
    color = fields.get("color_hex",
                       str(filament.get("color_hex") or "").lower())
    if vendor_name == (filament.get("vendor") or {}).get("name") \
            and ops.same_material(material, filament.get("material")) \
            and color == str(filament.get("color_hex") or "").lower():
        return None
    vendor = await ops.get_or_create_vendor(api, vendor_name)
    target = await ops.find_or_create_filament_by(
        api, vendor["id"], material, color or None, current=filament,
        weight=fields.get("initial_weight"))
    return None if target["id"] == filament.get("id") else target["id"]


async def _edit_linked(api, spool_id, fields):
    spool = await api.request("GET", "/v1/spool/%d" % spool_id)
    filament = spool.get("filament") or {}
    patch = _weights(fields, spool, filament)
    if any(name in fields for name in ("material", "color_hex", "vendor")):
        filament_id = await _retarget(api, filament, fields)
        if filament_id is not None:
            patch["filament_id"] = filament_id
    if "flow_k" in fields:
        extra = dict(spool.get("extra") or {})
        extra["flow_k"] = ops.encode_extra(fields["flow_k"])
        patch["extra"] = extra
    if patch:
        spool = await api.request("PATCH", "/v1/spool/%d" % spool_id,
                                  body=patch)
        if not isinstance(spool, dict) or "filament" not in spool:
            spool = await api.request("GET", "/v1/spool/%d" % spool_id)
    return spool


async def _create(api, run_gcode, unit, bay, fields):
    material = fields.get("material")
    if not material:
        raise EditError("material is required to add a spool to %s Spool %d"
                        % (unit, bay + 1))
    initial = fields.get("initial_weight", DEFAULT_INITIAL_WEIGHT)
    remaining = fields.get("remaining_weight", initial)
    if remaining > initial:
        raise EditError("remaining_weight: the remaining weight (%g g) cannot "
                        "exceed the initial weight (%g g)" % (remaining, initial))
    vendor = await ops.get_or_create_vendor(
        api, fields.get("vendor") or DEFAULT_VENDOR)
    filament = await ops.find_or_create_filament_by(
        api, vendor["id"], material, fields.get("color_hex"), weight=initial)
    body = {"filament_id": filament["id"], "initial_weight": initial,
            "remaining_weight": remaining, "location": "%s-%d" % (unit, bay)}
    if "flow_k" in fields:
        body["extra"] = {"flow_k": ops.encode_extra(fields["flow_k"])}
    spool = await api.request("POST", "/v1/spool", body=body)
    try:
        await run_gcode("OAMSM_SET_BAY_SPOOL OAMS=%s BAY=%d SPOOL=%d "
                        "SOURCE=manual" % (unit, bay, spool["id"]))
    except Exception as error:
        # The host did not take the link: take the new spool back out, so
        # Spoolman holds no spool that belongs to no bay.
        try:
            await api.request("DELETE", "/v1/spool/%d" % spool["id"])
        except (SpoolmanUnavailable, SpoolmanError):
            pass
        raise EditError("the printer did not accept the spool link: %s"
                        % error, 502)
    return spool


async def apply(api, run_gcode, unit, bay, fields, spool_id):
    """Run the edit; ``spool_id`` is the bay's linked spool, or None.

    Returns ``(spool, created)`` with the spool as Spoolman now holds it.
    Every Spoolman write that can fail comes before the one that changes the
    spool, so a failure leaves at most an unused vendor or filament behind.
    """
    try:
        if spool_id is not None:
            return await _edit_linked(api, spool_id, fields), False
        return await _create(api, run_gcode, unit, bay, fields), True
    except SpoolmanUnavailable:
        raise EditError(OFFLINE_MESSAGE, 503)
    except SpoolmanError as error:
        if error.status == 404 and spool_id is not None:
            raise EditError("Spoolman has no spool %d" % spool_id, 404)
        raise EditError("Spoolman refused the change: %s"
                        % (error.message or error), 502)
