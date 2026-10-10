#
# Copyright (C) 2025-2026 JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# openams_ui: the compact per-unit projection of the oams_manager status
# (docs/contracts/C2_status.md section 6, work package H5). One Klipper printer
# object per unit, named "openams_ui <unit>", registered by oams_manager. Every
# value is a pure function of the oams_manager v2 status, so the compact object
# provably carries nothing the full status does not: a small client (the
# 480x320 display) subscribes to this instead of the whole snapshot.
#
# The derivation below follows the reference implementation in
# docs/contracts/tools/gen_examples.py::project_ui. It is repeated here (rather
# than imported) because src/ must not import from docs/.

import json

try:                                  # deployed as a Klipper extras package
    from . import ams_family
    from . import oams_ext
except ImportError:                   # imported standalone (src/ on sys.path)
    import ams_family
    import oams_ext

# Unit-level actions openams_ui carries. load/unload/calibrate_ptfe travel per
# bay instead (bays[].a); engineering actions (configure_pid, ...) are
# dropped. `home` is a klipper-mmu unit's selector, so it is the one
# unit-level action such a unit adds. An extension may carry more: each with
# the capability that must not be false for it to be offered.
_EXT_UI_ACTIONS = dict(oams_ext.FAMILY.ui_unit_actions())
UI_UNIT_ACTIONS = (("dryer_start", "dryer_stop", "clear_fault", "rfid_scan",
                    "home") + tuple(_EXT_UI_ACTIONS))

# bays[].a letters, in the contract's order. The last three are a klipper-mmu
# unit's own bay operations (C5 §7.1); an AMS unit never publishes them.
_BAY_ACTION_CODES = (("load", "l"), ("unload", "u"), ("calibrate_ptfe", "c"),
                     ("set_spool", "a"), ("clear_spool", "d"),
                     ("preload", "p"), ("unpreload", "q"), ("eject", "e"))
_CAL_CODES = {"configured": "c", "uncalibrated": "u", "calibrating": "p"}
_FAULT_RANK = {"stop": 0, "pause": 1, "info": 2}

# Every family a device may publish, from the registry (src/ams_family.py): the
# three AMS families and the klipper-mmu devices.
_DEVICE_FAMILIES = ams_family.FAMILIES


def minified(obj):
    """The contract's byte-counting form (C2 section 6): no whitespace, and
    non-ASCII fault text counted as the UTF-8 bytes it is."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def ref(status, unit_pair):
    """[oams_idx, bay] (the reducer's tuple) -> '<unit>-<bay>', or None."""
    if not unit_pair:
        return None
    for name, info in status["topology"]["oams"].items():
        if info["idx"] == unit_pair[0]:
            return "%s-%d" % (name, unit_pair[1])
    return None


def _slot_id(status, unit_name, bay):
    """The global slot id (UI API v1 `units[].slots[].id`) of one bay."""
    for unit in status.get("units") or ():
        if unit.get("name") == unit_name:
            for slot in unit.get("slots") or ():
                if slot.get("bay") == bay:
                    return slot.get("id")
    return None


def _bay_of(status, bay_ref):
    """The device bay dict for a '<unit>-<bay>' reference (topology format)."""
    unit, _, bay = bay_ref.rpartition("-")
    device = status["devices"].get(unit)
    if device is None:
        return {}
    for bd in device.get("bays") or ():
        if bd.get("index") == int(bay):
            return bd
    return {}


def _family(status, unit_name, device):
    """The projection's family. A follower's driver has no OAMS family, and
    the v2 device schema (which excludes inline_follower) carries none, so the
    topology's unit kind supplies it."""
    family = device.get("family")
    if family in _DEVICE_FAMILIES:
        return family
    info = status["topology"]["oams"].get(unit_name) or {}
    if info.get("kind") == "follower":
        return "inline_follower"
    return info.get("family") or "ams1"


def _dryer(device):
    """The unit's dryer object, or None when it has none. A unit whose
    firmware reports no dryer is never given one, and a dryer that is present
    is always complete: a client indexes `state` and `fault` unconditionally,
    so a missing key takes its "nothing known" value instead of vanishing."""
    dryer = device.get("dryer")
    if dryer is None or (device.get("capabilities") or {}).get(
            "dryer") is False:
        return None
    return {"state": dryer.get("state", "unknown"),
            "target_c": dryer.get("target_c"),
            "remaining_s": dryer.get("remaining_s"),
            "heater_pct": dryer.get("heater_pct"),
            "fan_pct": dryer.get("fan_pct"),
            "fault": dryer.get("fault", "none"),
            "adapter": dryer.get("adapter")}


def _unit_actions(device, dryer):
    """The unit-level actions to publish: the unit's own, minus any its
    reported capabilities rule out (a UI must not offer what the firmware
    does not have, whatever a driver lists)."""
    caps = device.get("capabilities") or {}
    actions = []
    if not device.get("connected"):
        return actions          # nothing can be asked of an offline unit
    for action in device.get("supported_actions") or ():
        if action not in UI_UNIT_ACTIONS:
            continue
        if action in ("dryer_start", "dryer_stop") and dryer is None:
            continue
        # A unit reads tags either through raw RFID or by decoding them
        # itself (tray reads, as a Cortex Bridge's AMS does): hide the scan
        # only when it has neither.
        if (action == "rfid_scan" and caps.get("rfid") is False
                and not caps.get("tray_read")):
            continue
        needs = _EXT_UI_ACTIONS.get(action)
        if needs and not caps.get(needs, True):
            continue
        actions.append(action)
    return actions


def _unit_ready(status, device, faults):
    """Whether this unit can be operated now: the manager is ready, the unit
    is reachable and may move filament, and no `stop` fault is on it. A UI
    greys out a unit that is not ready; a capability-limited unit that cannot
    move filament is never ready."""
    if not status["ready"] or not device.get("connected"):
        return False
    if ams_family.motion_block_reason(device.get("capabilities") or {}):
        return False
    return not any(f.get("severity") == "stop" for f in faults)


def _bay_state(bd):
    # C2 §6.2: a bay the firmware is positioning on its own (H16) reads `p`,
    # not `r`: it cannot be loaded until the positioning ends.
    if bd.get("loaded"):
        return "l"
    if bd.get("positioning"):
        return "p"
    return "r" if bd.get("ready") else "e"


def _read_from_a_tag(tag):
    """False only when the bay's tag record is a unit's stored setting
    (format `unit_summary`, an AMS 2 Pro's tray summary: no tag was read).
    A unit that publishes no tag record keeps its link source."""
    return not (tag and tag.get("format") == "unit_summary")


def project(status, unit_name):
    """The openams_ui object for one unit, from an oams_manager v2 status."""
    device = status["devices"][unit_name]
    lane_id = status["topology"]["oams"][unit_name]["lane"]
    lane = status["lanes_by_fps"][lane_id]
    faults = device.get("faults") or ()
    bays = []
    for bd in device.get("bays") or ():
        state = _bay_state(bd)
        if any(f.get("bay") == bd.get("index") and f.get("severity") != "info"
               for f in faults):
            state = "x"
        # The link only (spool_id + link_source); the material/color/vendor/
        # remaining summary lives in the C4 openams_spoolman status.
        link = bd.get("link_source") or "none"
        spool = None
        if link != "none":
            src = link[0]
            if link == "tag" and not _read_from_a_tag(bd.get("tag")):
                # Linked from the unit's own stored setting (an AMS 2 Pro's
                # tray summary: no tag UID), not from a tag: a display must
                # not show it as an RFID read. The setting is the unit's
                # manual one, so it goes out as manual.
                src = "m"
            spool = {"id": bd.get("spool_id"), "src": src}
        acts = "".join(code for name, code in _BAY_ACTION_CODES
                       if name in (bd.get("supported_actions") or ()))
        bays.append({"id": _slot_id(status, unit_name, bd.get("index")),
                     "s": state,
                     "cal": _CAL_CODES.get(bd.get("calibration_state"), "u"),
                     "a": acts, "sp": spool})
    groups = []
    for name, info in sorted(status["topology"]["groups"].items()):
        # A group with no bay is on no lane yet (lane None): any lane may fill
        # it, so every unit lists it, by name. Left out, a UI would neither
        # offer it nor know the name is taken, and its "New group" would ask
        # for one that already exists.
        if info["lane"] is None and not info["bays"]:
            groups.append({"n": name})
            continue
        if info["lane"] != lane_id:
            continue
        # Members and their states only for groups holding one of this unit's
        # bays; other groups on the lane are listed by name only.
        if any(bay_ref.rsplit("-", 1)[0] == unit_name
               for bay_ref in info["bays"]):
            states = "".join(_bay_state(_bay_of(status, bay_ref))
                             for bay_ref in info["bays"])
            groups.append({"n": name, "b": list(info["bays"]), "s": states})
        else:
            groups.append({"n": name})
    lane_faults = []
    for name, info in status["topology"]["oams"].items():
        if info["lane"] != lane_id:
            continue
        for fault in (status["devices"].get(name) or {}).get("faults") or ():
            text = fault["text"]
            if len(text) > 48:
                text = text[:47] + "\u2026"
            lane_faults.append({"sev": fault["severity"],
                                "code": fault["code"], "text": text,
                                "unit": name, "bay": fault["bay"],
                                "act": list(fault["actions"])})
    # nf counts every fault on the lane; only the most severe one is carried.
    nf = len(lane_faults)
    lane_faults.sort(key=lambda f: _FAULT_RANK.get(f["sev"], 3))
    env = device.get("environment")
    dryer = _dryer(device)
    remaining = None if dryer is None else dryer.get("remaining_s")
    stage = lane.get("stage")
    return {
        "v": 1,
        "unit": unit_name,
        # The unit's index (OAMS_* commands take OAMS=<idx>) and, per bay,
        # the global slot id (OPENAMS_LOAD ... SLOT=<id>), C2 §4.10 / §6.2.
        "idx": status["topology"]["oams"][unit_name]["idx"],
        "family": _family(status, unit_name, device),
        "serial": (device.get("identity") or {}).get("serial"),
        "online": bool(device.get("connected")),
        "ready": _unit_ready(status, device, faults),
        "cal": _CAL_CODES.get((device.get("calibration") or {}).get("state"),
                              "u"),
        "lane": {"id": lane_id, "ext": lane.get("extruder"),
                 "op": lane["op"], "group": lane["group"],
                 "bay": ref(status, lane.get("unit")),
                 "stage": (None if stage is None else
                           [stage["plan"], stage["index"], stage["failed"],
                            stage.get("detail"), stage.get("labels", {})]),
                 "runout": lane["runout"],
                 "from": ref(status, lane.get("runout_from")),
                 "to": ref(status, lane.get("reload_target")),
                 # C2 §4.2's motion_settled, so a UI need not re-derive it.
                 "ms": bool(lane.get("motion_settled"))},
        "bays": bays,
        "groups": groups,
        "env": (None if env is None else
                {"t": (None if env.get("temp_c") is None
                       else int(round(env["temp_c"]))),
                 "rh": (None if env.get("rh_pct") is None
                        else int(round(env["rh_pct"])))}),
        "dryer": (None if dryer is None else
                  {"state": dryer["state"],
                   "target": (None if dryer.get("target_c") is None
                              else int(round(dryer["target_c"]))),
                   "min": (None if remaining is None
                           else (remaining + 59) // 60),
                   "heat": dryer.get("heater_pct"), "fan": dryer.get("fan_pct"),
                   "fault": dryer["fault"], "adapter": dryer.get("adapter")}),
        "fault": lane_faults[0] if lane_faults else None,
        "nf": nf,
        "act": _unit_actions(device, dryer),
    }


class OpenAMSUIProjection:
    """The "openams_ui <unit>" printer object. get_status() is rate-limited so
    the projection is rebuilt at most once per `interval` seconds (0.5 s: the
    contract's <= 2 Hz). Inside the window, and whenever a rebuild produces the
    same projection, the previous object is returned unchanged, so Klipper's
    status poller has nothing to resend."""

    def __init__(self, manager, unit_name, interval=0.5):
        self.manager = manager
        self.unit_name = unit_name
        self.interval = interval
        self._status = None
        self._minified = None
        self._built_at = None

    def get_status(self, eventtime=None):
        now = (self.manager.reactor.monotonic()
               if eventtime is None else eventtime)
        if (self._built_at is not None
                and now - self._built_at < self.interval):
            return self._status
        try:
            status = project(self.manager.get_status(now), self.unit_name)
        except KeyError:
            # The unit is gone from the current topology (a rebuild is in
            # flight); keep publishing the last projection until it returns.
            return self._status
        digest = minified(status)
        self._built_at = now
        if digest != self._minified:
            self._status = status
            self._minified = digest
        return self._status
