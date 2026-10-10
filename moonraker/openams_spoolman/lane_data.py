# The per-tool filament state OrcaSlicer's "sync filaments from printer" reads
# from Moonraker's database namespace ``lane_data`` (the layout of AFC's
# ``lane_data``): one ``laneN`` object per tool, N = tool number + 1.
#
# Pure functions over the ``oams_manager`` status and the cached Spoolman
# spool summaries, so the content is tested without a Moonraker.

import re

from . import ops

NAMESPACE = "lane_data"


def _tool_number(name, position):
    """The tool number a group name ends in (``T3`` -> 3), else its position."""
    found = re.search(r"(\d+)\s*$", str(name or ""))
    return int(found.group(1)) if found else position


def _unit_names(oams):
    """``oams_idx -> unit name`` from the status topology."""
    names = {}
    for unit, entry in ((oams.get("topology") or {}).get("oams") or {}).items():
        if isinstance(entry, dict) and entry.get("idx") is not None:
            names[entry["idx"]] = unit
    return names


def _represents(oams, group):
    """The ``[oams_idx, bay]`` that stands for a group: the bay it has loaded
    on its lane, else its next bay, else None."""
    lane = (oams.get("lanes_by_fps") or {}).get(group.get("lane")) or {}
    if lane.get("op") == "LOADED" and lane.get("group") == group.get("name") \
            and isinstance(lane.get("unit"), (list, tuple)) \
            and len(lane["unit"]) == 2:
        return list(lane["unit"])
    nxt = group.get("next_bay")
    if isinstance(nxt, (list, tuple)) and len(nxt) == 2:
        return list(nxt)
    return None


def _color(color_hex):
    text = str(color_hex or "").strip().lstrip("#")
    if len(text) in (6, 8) and re.fullmatch(r"[0-9A-Fa-f]+", text):
        return "#" + text[:6].upper()
    return "#000000"


def build(oams, bay_summary, material_temps=None):
    """The ``lane_data`` content for an ``oams_manager`` status.

    ``bay_summary(key)`` returns the cached summary of the spool linked to the
    bay ``"<unit>-<bay>"`` or None. Returns ``{"lane1": {...}, ...}``, one lane
    per group in tool order; a group with no bay or no linked spool is a lane
    with ``material`` ``""``.
    """
    groups = [g for g in (oams.get("groups") or []) if isinstance(g, dict)]
    units = _unit_names(oams)
    lanes = {}
    for position, group in enumerate(groups):
        tool = _tool_number(group.get("name"), position)
        summary = None
        bay = _represents(oams, group)
        if bay is not None and bay[0] in units:
            summary = bay_summary("%s-%s" % (units[bay[0]], bay[1]))
        lane = {"lane": str(tool), "color": "#000000", "material": "",
                "bed_temp": 0, "nozzle_temp": 0, "name": "", "vendor": "",
                "spool_id": None}
        material = ops.material_name_of((summary or {}).get("material"))
        if material:
            nozzle = ops.extruder_temp_of(summary.get("extruder_temp"))
            if nozzle is None:
                nozzle = ops.material_extruder_temp(material, material_temps)
            if nozzle is None:
                nozzle = ops.extruder_temp_of(
                    group.get("next_filament_temp_c"))
            try:
                bed = int(float(summary.get("bed_temp")))
            except (TypeError, ValueError):
                bed = None
            if not bed or bed < 0:
                bed = ops.material_extruder_temp(material,
                                                 ops.MATERIAL_BED_TEMP)
            lane.update({
                "color": _color(summary.get("color_hex")),
                "material": material,
                "bed_temp": int(bed or 0),
                "nozzle_temp": int(nozzle or 0),
                "name": str(summary.get("name") or material),
                "vendor": str(summary.get("vendor") or ""),
                "spool_id": summary.get("spool_id")})
        lanes["lane%d" % (tool + 1)] = lane
    return lanes
