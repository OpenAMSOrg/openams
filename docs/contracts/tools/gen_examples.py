#!/usr/bin/env python3
"""Generate the C2/C4 example fixtures under docs/contracts/examples/.

Run it after changing a schema or a scenario to regenerate every fixture
consistently, then run check_examples.py. oams_manager v2 status is built per scenario;
openams_ui is a pure projection of it (project_ui) so the compact object
provably carries nothing the full status does not. Capability dicts come
from the real, pure src/ams_family.py.

Usage: python3 docs/contracts/tools/gen_examples.py   (from the repo root)
"""
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO, "src"))
import ams_family  # noqa: E402
import oams_ext  # noqa: E402

OUT_C2 = os.path.join(REPO, "docs/contracts/examples/c2")
OUT_C4 = os.path.join(REPO, "docs/contracts/examples/c4")
PATH_FACTOR = 1.14  # oams.py:40 FILAMENT_PATH_LENGTH_FACTOR

# Example dictionaries, with example version strings. The AMS 1 units are
# OpenAMS 1 mainboards (C1 §1.4): their firmware publishes its MCU and no
# AMS_* constants. The AMS 2 Pro and AMS HT are stock units behind a Cortex
# Bridge, which publishes the identity (C1 §2) and capability (C1 §7)
# constants below for each of them.
_BRIDGED = {
    "OAMS_PROTOCOL_VERSION": 3, "AMS_CAPABILITY_SCHEMA_VERSION": 5,
    "AMS_CAP_F1S_NORMAL_MOTION": 1, "AMS_CAP_LINK_STATUS": 1,
    "AMS_CAP_RUNTIME_PID_TUNING": 1, "AMS_CAP_HEATER_CONTROL": 1,
    "AMS_CAP_HOST_SENSOR_CONFIGURATION": 0, "AMS_CAP_LOGGER_CONTROL": 0,
    "AMS_CAP_ERROR_LED_CONTROL": 0, "AMS_CAP_RFID_TRANSPORT": 0,
    "AMS_RFID_READERS": 0, "AMS_VENT_ACTUATORS": 0,
    "AMS_HT_CAP_PTFE_CALIBRATION": 0, "AMS_HT_CAP_HUB_HES_CALIBRATION": 0,
    "AMS_CAP_TRAY_INFO": 1, "AMS_CAP_TRAY_READ": 1,
    "AMS_CAP_DRYER_SPOOL_ROTATION": 0,
    "MCU_VERSION": "example-1.0", "BUILD_VERSION": "example-1.0"}
FW_CONSTS = {
    "ams1": {"MCU": ams_family.STM32F072_MCU,
             "MCU_VERSION": "example-1.0", "BUILD_VERSION": "example-1.0"},
    "ams2": dict(_BRIDGED, BOARD_NAME="ams2", AMS_FAMILY="ams2", AMS_BAYS=4,
                 AMS_DRYER=1, AMS_CAP_BOARD_TELEMETRY=0, AMS_CAP_TRAY_WRITE=1),
    "ams_ht": dict(_BRIDGED, BOARD_NAME="ams-ht", AMS_FAMILY="ams_ht",
                   AMS_BAYS=1, AMS_DRYER=1, AMS_HT_DRYER=1, AMS_DRYER_ZONES=1,
                   AMS_CAP_BOARD_TELEMETRY=1),
    # A klipper-mmu device publishes no AMS_* constants at all (C5), so the
    # dictionary here carries only the identity fields C2 publishes.
    "boxturtle": {"BOARD_NAME": "klipper_mmu_boxturtle",
                  "AMS_CAPABILITY_SCHEMA_VERSION": 5,
                  "MCU_VERSION": "klipper-mmu 0.1.0",
                  "BUILD_VERSION": "gcc: 12.2"},
    "claymore": {"BOARD_NAME": "klipper_mmu_claymore",
                 "AMS_CAPABILITY_SCHEMA_VERSION": 5,
                 "MCU_VERSION": "klipper-mmu 0.1.0",
                 "BUILD_VERSION": "gcc: 12.2"},
}
# Example serials (C1 §2: 24 hex digits).
SERIALS = {"oams1": "000000000000000000000001",
           "oams2": "000000000000000000000002",
           "ams2": "000000000000000000000003",
           "amsht": "000000000000000000000004",
           "box": "000000000000000000000005",
           "clay": "000000000000000000000006"}
# The telemetry channels each AMS family's unit answers (an OpenAMS 1
# mainboard has none).
TELEMETRY_CHANNELS = {"ams1": [], "ams2": ["domain", "dryer"],
                      "ams_ht": ["board", "domain", "dryer"]}

# The three AMS families. A klipper-mmu unit (C5) has its own wire and
# publishes no OAMS telemetry tree.
AMS_UNITS = ("ams1", "ams2", "ams_ht")

# Unit supported_actions exactly as oams.py:_supported_actions builds them for
# a connected gen-protocol unit of each family (oams.py:358-406).
UNIT_ACTIONS = {
    "ams1": ["clear_errors", "load", "unload", "follower", "configure_pid",
             "calibrate_ptfe", "calibrate_hub_hes"],
    "ams2": ["clear_errors", "load", "unload", "follower", "dryer_start",
             "dryer_stop", "clear_fault", "rfid_scan"],
    "ams_ht": ["clear_errors", "load", "unload", "follower", "dryer_start",
               "dryer_stop", "clear_fault", "rfid_scan"],
}
# A klipper-mmu device: load, unload and the follower it owns, plus the four
# operations C5 §7.1 maps onto C2 now that C3 §2 publishes them. Its
# geometry calibration still has no C2 action token.
MMU_UNIT_ACTIONS = ["clear_errors", "load", "unload", "follower"]


def mmu_actions(family):
    """The klipper-mmu op tokens the manager publishes for this family
    (oams_manager._mmu_op_actions): preload, unpreload and eject always, home
    only where the family's selector kind has one to move."""
    profile = ams_family.profile(family)
    return [op for op in ("preload", "unpreload", "eject", "home")
            if profile.get(op)]


for _mmu in ams_family.KLIPPER_MMU_FAMILIES:
    UNIT_ACTIONS[_mmu] = MMU_UNIT_ACTIONS + mmu_actions(_mmu)
del _mmu
# The UI-facing subset openams_ui carries (engineering actions dropped).
# Unit-level actions openams_ui carries; load/unload/calibrate_ptfe travel per
# bay instead (bays[].a), engineering actions are dropped.
UI_UNIT_ACTIONS = (("dryer_start", "dryer_stop", "clear_fault", "rfid_scan",
                    "home")
                   + tuple(oams_ext.FAMILY.ui_unit_actions()))

SPOOLS = {  # Spoolman facts the openams_spoolman component holds (C4); id -> summary.
    # RESOLVED (review, C2 4/7): these facts live ONLY in the C4
    # openams_spoolman status now, never in oams_manager/openams_ui.
    # initial_g/flow_k are the edit form's prefills (C4 §8, §9); flow_k is
    # null where the spool carries no pressure advance.
    12: dict(material="PLA", color_hex="0A2989", vendor="Bambu Lab", remaining_g=812, remaining_pct=81, initial_g=1000, flow_k=0.02),
    15: dict(material="PETG", color_hex="F5F5F5", vendor="Polymaker", remaining_g=430, remaining_pct=43, initial_g=1000, flow_k=0.04),
    21: dict(material="PETG", color_hex="000000", vendor="Bambu Lab", remaining_g=1000, remaining_pct=100, initial_g=1000, flow_k=None),
    30: dict(material="ABS", color_hex="D32F2F", vendor="Bambu Lab", remaining_g=250, remaining_pct=25, initial_g=1000, flow_k=0.03),
    31: dict(material="ABS", color_hex="D32F2F", vendor="Bambu Lab", remaining_g=990, remaining_pct=99, initial_g=1000, flow_k=0.03),
    40: dict(material="PLA", color_hex="FFFFFF", vendor="Bambu Lab", remaining_g=640, remaining_pct=64, initial_g=1000, flow_k=0.01),
    41: dict(material="PLA", color_hex="FFD700", vendor="eSun", remaining_g=720, remaining_pct=72, initial_g=1000, flow_k=None),
    44: dict(material="ASA", color_hex="808080", vendor="Bambu Lab", remaining_g=880, remaining_pct=88, initial_g=1000, flow_k=0.05),
    50: dict(material="PA-CF", color_hex="202020", vendor="Bambu Lab", remaining_g=600, remaining_pct=60, initial_g=1000, flow_k=0.02),
}


def bambu_tag(spool_id, material, detailed, argb, uid, tray, weight=1000,
              dry_c=55, dry_h=8, bed=35, tmin=190, tmax=230):
    """A decoded tag as bambu_lab_tag_processor.decode returns it (verbatim keys)."""
    return {
        "uid": uid, "format": "bambu", "read_at": 1523.4, "spoolman_id": None,
        "bambu": {
            "manufacturer": "Bambu", "type": material, "detailed": detailed,
            "color_argb": argb, "weight_g": weight, "diameter_mm": 1.75,
            "drying_temp_c": dry_c, "drying_time_h": dry_h, "bed_temp_c": bed,
            "hotend_max_c": tmax, "hotend_min_c": tmin, "nozzle_diameter": 0.4,
            "spool_width_mm": 66.25, "length_m": 330, "tray_uid": tray,
            "production": "2024_05_12_10_30"},
    }


def unit_summary_tag(code, material, argb, tmin, tmax):
    """A tag the unit read and decoded itself (C1 §4.7.1, C2 §4.6): no UID,
    a summary (oams_tags.tray_summary)."""
    return {"uid": None, "format": "unit_summary", "read_at": 1523.4,
            "spoolman_id": None, "bambu": None,
            "summary": {"code": code, "material": material, "color_argb": argb,
                        "nozzle_min_c": tmin, "nozzle_max_c": tmax,
                        "manufacturer": None}}


def spoolman_link_tag(spool_id, uid):
    return {"uid": uid, "format": "spoolman_link", "read_at": 1523.4,
            "spoolman_id": spool_id, "bambu": None}


class Unit:
    def __init__(self, name, idx, family, lane="fps", ptfe_clicks=1824,
                 bay_ptfe=None, bays=None):
        self.name, self.idx, self.family, self.lane = name, idx, family, lane
        caps, warnings = ams_family.resolve_capabilities(family, FW_CONSTS[family])
        if family in AMS_UNITS:
            caps["telemetry_channels"] = list(TELEMETRY_CHANNELS[family])
        else:
            caps["telemetry"] = False
            caps["telemetry_channels"] = ["board", "domain", "dryer"]
        self.caps = caps
        self.protocol = (None if ams_family.is_stm32f072(FW_CONSTS[family])
                         else 3)
        # A klipper-mmu device is built for one lane count (BoxTurtle 4 or 8,
        # NightOwl 2 or 4), so the installed variant moves both bay_count and
        # the capability's lane range.
        self.bays = caps["bays"] if bays is None else bays
        if bays is not None:
            caps["bays"] = bays
            caps["bays_min"] = min(caps.get("bays_min", bays), bays)
            caps["bays_max"] = max(caps.get("bays_max", bays), bays)
            if "selector_kind" in caps:
                caps["spool_drive_count"] = bays
        self.ptfe_clicks = ptfe_clicks            # shared [oams] ptfe_length
        self.bay_ptfe = bay_ptfe or [None] * self.bays   # ptfe_length_<n>
        self.ready = [False] * self.bays
        self.loaded = [False] * self.bays
        self.spool = [None] * self.bays           # (spool_id, source)
        self.tag = [None] * self.bays
        self.calibrating_bay = None
        self.positioning = [False] * self.bays   # C2 §4.5, H16
        self.env = None
        self.dryer = None
        self.faults = []
        self.connected = True


def bay_path_mm(u, b):
    clicks = u.bay_ptfe[b] if u.bay_ptfe[b] is not None else u.ptfe_clicks
    return round(clicks / PATH_FACTOR, 1)


def bay_cal_state(u, b):
    if u.calibrating_bay == b:
        return "calibrating"
    return "configured" if bay_path_mm(u, b) > 0 else "uncalibrated"


def lane_busy(lane):
    return lane["op"] in ("LOADING", "UNLOADING", "CALIBRATING") or \
        lane["runout"] == "loading"


def bay_actions(u, b, lane):
    """Proposed per-bay derivation (C2 §5.4). rfid_scan is unit-level only:
    neither OAMS_RFID_SCAN nor OAMSM_RFID_READ takes a bay."""
    unit_acts = UNIT_ACTIONS[u.family]
    stop = any(f["severity"] == "stop" for f in u.faults)
    # No motion on any bay while the unit positions one (C2 §5.6, H16).
    stop = stop or any(u.positioning)
    busy = lane_busy(lane)
    idle = lane["op"] == "UNLOADED" and not busy
    here = lane["unit"] == [u.idx, b]
    cal = bay_cal_state(u, b)
    acts = []
    if ("load" in unit_acts and not stop and idle and u.ready[b]
            and not u.loaded[b] and cal == "configured"):
        acts.append("load")
    if ("unload" in unit_acts and not stop and here and lane["op"] == "LOADED"
            and lane["runout"] == "idle"):
        acts.append("unload")
    if ("calibrate_ptfe" in unit_acts and not stop and idle and u.ready[b]
            and cal != "calibrating"):
        acts.append("calibrate_ptfe")
    if u.ready[b]:
        acts.append("set_spool")
    if u.spool[b] is not None:
        acts.append("clear_spool")
    # A klipper-mmu unit's own bay operations (C5 §7.1): the unit answers
    # whether this bay may do one now, so they are offered whenever the unit
    # can perform them. `home` belongs to the unit, not to a bay.
    acts.extend(op for op in mmu_actions(u.family) if op != "home")
    return acts


def unit_cal_state(u):
    if u.calibrating_bay is not None:
        return "calibrating"
    return "configured" if u.ptfe_clicks > 0 else "uncalibrated"


def build_device(u, lane):
    f1s = [int(x) for x in u.ready] + [0] * (4 - u.bays)
    hub = [int(x) for x in u.loaded] + [0] * (4 - u.bays)
    current_spool = next((b for b in range(u.bays) if u.loaded[b]), None)
    telemetry = {}
    if "domain" in u.caps["telemetry_channels"]:
        telemetry["domain"] = {
            "motion_state": 2 if current_spool is not None else 0,
            "motion_stage": 4 if current_spool is not None else 0,
            "motion_fault": 0, "dryer_state": 0, "dryer_fault": 0,
            "system_fault": 0, "timestamp": 1523.1,
            "motion_state_name": "follow" if current_spool is not None else "idle",
            "motion_stage_name": "follow" if current_spool is not None else "stopped",
            "dryer_state_name": "off", "motion_fault_name": "none",
            "dryer_fault_name": "none", "system_fault_name": "none"}
        if u.dryer is not None:
            d = u.dryer
            telemetry["dryer"] = d["_raw"]
            telemetry["domain"]["dryer_state"] = d["_raw"]["state"]
            telemetry["domain"]["dryer_state_name"] = d["_raw"]["state_name"]
            telemetry["domain"]["dryer_fault"] = d["_raw"]["fault"]
            telemetry["domain"]["dryer_fault_name"] = d["_raw"]["fault_name"]
    bays = []
    for b in range(u.bays):
        sp = u.spool[b]
        bays.append({
            # EXISTS-v1 (oams.py:438-441)
            "index": b, "ready": u.ready[b], "loaded": u.loaded[b],
            # NEW-v2
            "calibrated": bay_cal_state(u, b) == "configured",
            "calibration_state": bay_cal_state(u, b),
            "path_length_mm": bay_path_mm(u, b),
            "positioning": u.positioning[b],                 # H16
            "spool_id": None if sp is None else sp[0],
            "link_source": "none" if sp is None else sp[1],
            # RESOLVED (review, C2 4/7): no "spool" summary here;
            # material/color/vendor/remaining live in the C4 openams_spoolman
            # status only, keyed "<unit>-<bay>".
            "tag": u.tag[b],
            "spool_hint": None,
            "supported_actions": bay_actions(u, b, lane),
        })
    dryer = None
    if u.dryer is not None:
        dryer = {k: v for k, v in u.dryer.items() if not k.startswith("_")}
    return {
        # ---- EXISTS-v1: oams.py get_webhook_status (408-445), verbatim keys
        "schema_version": 1, "current_spool": current_spool,
        "oams_idx": u.idx, "connected": u.connected,
        "fps_value": 0.48 if current_spool is not None else 0.0,
        "fps_upper_threshold": 0.7, "fps_lower_threshold": 0.3,
        "f1s_hes_value": f1s[:u.bays], "hub_hes_value": hub[:u.bays],
        "fps_target": 0.5, "current_kp": 0.375, "current_ki": 0.0,
        "current_kd": 0.0, "current_target": 0.3,
        "f1s_hes_value_0": f1s[0], "f1s_hes_value_1": f1s[1],
        "f1s_hes_value_2": f1s[2], "f1s_hes_value_3": f1s[3],
        "hub_hes_value_0": hub[0], "hub_hes_value_1": hub[1],
        "hub_hes_value_2": hub[2], "hub_hes_value_3": hub[3],
        "kp": 6.0, "ki": 0.0, "kd": 0.0,
        "encoder_clicks": 48211, "i_value": 0.0,
        "protocol_version": u.protocol,
        "gen_matched_protocol": u.protocol is not None,
        "family": u.family, "bay_count": u.bays,
        "capabilities": copy.deepcopy(u.caps), "capability_warnings": [],
        "bays": bays,
        "telemetry": telemetry,
        "supported_actions": list(UNIT_ACTIONS[u.family]),
        # ---- NEW-v2 (H1 unless noted)
        "identity": {"serial": SERIALS.get(u.name),
                     "firmware": u.caps.get("firmware_version"),
                     "board": u.caps.get("board_name")},
        "calibration": {"state": unit_cal_state(u),
                        "path_length_mm": round(u.ptfe_clicks / PATH_FACTOR, 1)},
        "environment": u.env,
        "dryer": dryer,
        "faults": u.faults,
        "follower_uncertain": getattr(u, "follower_uncertain", False),
    }


def make_dryer(state_name, state, target, remaining, heater_pct, fan_pct,
               fault_name="none", fault=0, adapter=None, chamber=None):
    raw = {"state": state, "fault": fault, "target_centic": int(target * 100),
           "heater_centic": int((target + 12) * 100) if heater_pct else 2600,
           "chamber_centic": int((chamber or 25) * 100), "humidity_mgm3": 6100,
           "remaining_s": remaining, "heater_permille": heater_pct * 10,
           "fan_permille": fan_pct * 10, "fan_rpm": 2100 if fan_pct else 0,
           "fan_pwm_permille": fan_pct * 10, "vent_position": 2 if state else 1,
           "purging": 0, "door_closed": 1, "timestamp": 1523.3,
           "target_c": float(target), "heater_c": (target + 12) if heater_pct else 26.0,
           "chamber_c": float(chamber or 25), "state_name": state_name,
           "fault_name": fault_name, "humidity_gm3": 6.1,
           "vent_position_name": "open" if state else "closed"}
    return {"state": state_name, "target_c": float(target), "remaining_s": remaining,
            "heater_pct": heater_pct, "fan_pct": fan_pct, "fault": fault_name,
            "adapter": adapter, "_raw": raw}


def lane(op="UNLOADED", group=None, unit=None, runout="idle", following=False,
         direction=1, since=1500.0, message=None, stage=None, reload_target=None,
         runout_from=None, pressure=0.0, extruder="extruder", motion_stage=None):
    # motion_settled is computed in build_status after devices are known;
    # use a sentinel here so callers do not need to pass it.
    if stage is not None:
        # C2 §5.3: a stage always carries the current step's text and the
        # display labels (src/oams_manager.py _lane_status), null / empty
        # when none is set.
        stage = dict(stage)
        stage.setdefault("labels", {})
        stage.setdefault("detail", None)
    return {
        # EXISTS-v1 (oams_manager.py:374-377)
        "op": op, "group": group, "unit": unit, "runout": runout,
        "following": following, "since": since, "message": message,
        # NEW-v2
        "direction": direction, "stage": stage, "motion_stage": motion_stage,
        "reload_target": reload_target,
        "runout_from": runout_from, "pressure": pressure,
        "set_point": 0.5, "extruder": extruder,
        # NEW-v2 (H1): filled by build_status
        "motion_settled": None,
    }


def _compute_motion_settled(units_on_lane, lane_dict, printing=False):
    """Compute the motion_settled predicate (C2 §5.6; corrected 2026-09-28).

    True iff nothing is moving and no motion action is in progress: op is
    UNLOADED or LOADED with runout idle; the printer is not printing;
    following is known (not None) -- True is fine on an idle LOADED lane;
    no stop/pause fault on any unit of the lane; no follower_uncertain; no
    bay positioning; all units connected.
    """
    op = lane_dict["op"]
    if op not in ("UNLOADED", "LOADED"):
        return False
    if lane_dict["runout"] != "idle":
        return False
    if printing:
        return False
    if lane_dict["following"] is None:
        return False
    for u in units_on_lane:
        if any(f["severity"] in ("stop", "pause") for f in u.faults):
            return False
        if not u.connected:
            return False
        if getattr(u, "follower_uncertain", False):
            return False
        if any(getattr(u, "positioning", [])):
            return False
    return True


def build_status(units, lanes, groups, ready=True, settings=None):
    units_sorted = sorted(units, key=lambda u: (u.idx, u.name))
    by_idx = {u.idx: u for u in units}
    by_name = {u.name: u for u in units}
    slot_id = {}
    n = 0
    for u in units_sorted:                       # legacy oams_manager.py:106-115
        for b in range(u.bays):
            slot_id[(u.name, b)] = n
            n += 1
    current_group = next((l["group"] for l in lanes.values()
                          if l["op"] == "LOADED" and l["group"]), None)
    fps_names = list(lanes)
    topo = {
        "schema_version": 1, "fps": fps_names,
        "oams": {u.name: {"idx": u.idx, "lane": u.lane, "kind": "oams",
                          "family": u.family, "bays": u.bays,
                          "capabilities": copy.deepcopy(u.caps)}
                 for u in units},
        "groups": {g: {"lane": (by_name[m[0][0]].lane if m else None),
                       "bays": ["%s-%d" % e for e in m]}
                   for g, m in groups.items()},
    }
    devices = {u.name: build_device(u, lanes[u.lane]) for u in units}
    # Compute motion_settled for each lane now that device/fault data is ready.
    for lane_name, ln in lanes.items():
        units_on_lane = [u for u in units if u.lane == lane_name]
        ln["motion_settled"] = _compute_motion_settled(units_on_lane, ln)
    api_units = []
    for u in units_sorted:
        api_units.append({
            "id": str(u.idx), "name": u.name, "kind": "oams", "topology": "hub",
            "lane": u.lane, "connected": u.connected,
            "slots": [{"id": slot_id[(u.name, b)], "bay": b,
                       "ready": u.ready[b], "loaded": u.loaded[b]}
                      for b in range(u.bays)]})
    api_groups = [{"name": g, "lane": topo["groups"][g]["lane"],
                   "slots": [slot_id[e] for e in m]}
                  for g, m in sorted(groups.items())]
    # RESOLVED (review, global): `lanes` is the UI-API-v1 array;
    # the per-FPS dict this line publishes today (unchanged shape) moves to
    # `lanes_by_fps`.
    lanes_v1 = []
    for fps_name, ln in lanes.items():
        current_slot = None
        if ln["unit"] is not None:
            uname = next(n for n, info in topo["oams"].items()
                        if info["idx"] == ln["unit"][0])
            current_slot = slot_id[(uname, ln["unit"][1])]
        # EXISTS-v1: _lane_status publishes both on lanes_by_fps as well
        # (the UI-API-v1 carryover, C2 §4.2).
        ln["state"] = ln["op"].lower()
        ln["current_slot"] = current_slot
        lanes_v1.append({
            "id": fps_name, "state": ln["op"].lower(),
            "current_group": ln["group"], "current_slot": current_slot,
            "following": bool(ln["following"]), "direction": ln["direction"],
            "message": ln["message"], "pressure": ln["pressure"],
            "set_point": ln["set_point"],
        })
    return {
        "schema_version": 2,
        "api_version": 1, "schema": "openams.manager",
        "ready": ready, "current_group": current_group,
        "lanes": lanes_v1, "lanes_by_fps": lanes, "topology": topo, "devices": devices,
        "settings": settings or {
            "read_tag_on_insertion": True, "read_tags_at_startup": True,
            "auto_reload_on_runout": True, "auto_create_spoolman_spools": True,
            "apply_pa_on_load": False, "sync_unit_tray_info": True},
        "commands": {"load": "OPENAMS_LOAD", "unload": "OPENAMS_UNLOAD",
                     "cancel": "OAMSM_LOAD_FILAMENT_CANCEL",
                     "reset": "OAMSM_CLEAR_ERRORS"},
        "units": api_units, "groups": api_groups,
    }


# ------------------------------------------------------------------ projection

def ref(status, unit_pair):
    """[oams_idx, bay] -> '<unit>-<bay>' using topology."""
    if unit_pair is None:
        return None
    for name, info in status["topology"]["oams"].items():
        if info["idx"] == unit_pair[0]:
            return "%s-%d" % (name, unit_pair[1])
    return None


def project_ui(status, unit_name):
    """Compact per-unit projection (C2 §6). Pure function of oams_manager v2."""
    dev = status["devices"][unit_name]
    info = status["topology"]["oams"][unit_name]
    lane_id = info["lane"]
    ln = status["lanes_by_fps"][lane_id]
    gmap = {}
    groups_out = []
    for g, gi in sorted(status["topology"]["groups"].items()):
        if gi["lane"] != lane_id:
            continue
        states = ""
        for m in gi["bays"]:
            u, b = m.rsplit("-", 1)
            bd = status["devices"][u]["bays"][int(b)]
            states += ("l" if bd["loaded"] else "p" if bd.get("positioning")
                       else "r" if bd["ready"] else "e")
            gmap[m] = g
        # GROUPS.md: members (in order, with state) only for groups holding one
        # of this unit's bays; other groups on the lane by name only (the
        # "Add to group" sheet lists them).
        if any(m.rsplit("-", 1)[0] == unit_name for m in gi["bays"]):
            groups_out.append({"n": g, "b": gi["bays"], "s": states})
        else:
            groups_out.append({"n": g})
    bays = []
    for bd in dev["bays"]:
        b = bd["index"]
        s = ("l" if bd["loaded"] else "p" if bd.get("positioning")
             else "r" if bd["ready"] else "e")
        if any(f.get("bay") == b and f["severity"] != "info" for f in dev["faults"]):
            s = "x"
        # RESOLVED (review, C2 4/7): the link only (spool_id +
        # link_source); the material/color/vendor/remaining summary lives in
        # the C4 openams_spoolman status, which the display merges in.
        sp = None
        if bd["link_source"] != "none":
            sp = {"id": bd["spool_id"], "src": bd["link_source"][0]}
        acts = "".join(code for name, code in (
            ("load", "l"), ("unload", "u"), ("calibrate_ptfe", "c"),
            ("set_spool", "a"), ("clear_spool", "d"),
            ("preload", "p"), ("unpreload", "q"), ("eject", "e"))
            if name in bd["supported_actions"])
        cal = {"configured": "c", "uncalibrated": "u",
               "calibrating": "p"}[bd["calibration_state"]]
        sid = next(sl["id"] for au in status["units"] if au["name"] == unit_name
                   for sl in au["slots"] if sl["bay"] == b)
        bays.append({"id": sid, "s": s, "cal": cal, "a": acts, "sp": sp})
    stage = ln["stage"]
    faults = []
    for uname, uinfo in status["topology"]["oams"].items():
        if uinfo["lane"] != lane_id:
            continue
        for f in status["devices"][uname]["faults"]:
            text = f["text"]
            if len(text) > 48:
                text = text[:47] + "…"
            faults.append({"sev": f["severity"], "code": f["code"],
                           "text": text, "unit": uname, "bay": f["bay"],
                           "act": f["actions"]})
    rank = {"stop": 0, "pause": 1, "info": 2}
    nf = len(faults)
    faults = sorted(faults, key=lambda f: rank[f["sev"]])[:1]
    env = dev["environment"]
    dryer = dev["dryer"]
    ucal = {"configured": "c", "uncalibrated": "u",
            "calibrating": "p"}[dev["calibration"]["state"]]
    return {
        "v": 1,
        "unit": unit_name, "idx": status["topology"]["oams"][unit_name]["idx"],
        "family": dev["family"],
        "serial": dev["identity"]["serial"],
        "online": dev["connected"],
        # C2 §6.2a: ready needs a reachable unit that may move filament and
        # carries no stop fault.
        "ready": bool(status["ready"] and dev["connected"]
                      and dev["capabilities"].get("normal_motion") is not False
                      and not any(f["severity"] == "stop"
                                  for f in dev["faults"])),
        "cal": ucal,
        "lane": {"id": lane_id, "ext": ln["extruder"], "op": ln["op"],
                 "group": ln["group"], "bay": ref(status, ln["unit"]),
                 "stage": (None if stage is None else
                           [stage["plan"], stage["index"], stage["failed"],
                            stage.get("detail"), stage.get("labels", {})]),
                 "runout": ln["runout"],
                 "from": ref(status, ln["runout_from"]),
                 "to": ref(status, ln["reload_target"]),
                 "ms": bool(ln["motion_settled"])},
        "bays": bays,
        "groups": groups_out,
        "env": (None if env is None else
                {"t": None if env["temp_c"] is None else round(env["temp_c"]),
                 "rh": None if env["rh_pct"] is None else round(env["rh_pct"])}),
        "dryer": (None if dryer is None else
                  {"state": dryer["state"],
                   "target": (None if dryer["target_c"] is None
                              else round(dryer["target_c"])),
                   "min": (dryer["remaining_s"] + 59) // 60,
                   "heat": dryer["heater_pct"], "fan": dryer["fan_pct"],
                   "fault": dryer["fault"], "adapter": dryer["adapter"]}),
        "fault": faults[0] if faults else None, "nf": nf,
        # A unit with neither raw RFID (rfid false) nor its own tray reads
        # offers no rfid_scan (openams_ui._unit_actions).
        "act": [a for a in dev["supported_actions"] if a in UI_UNIT_ACTIONS
                and not (a == "rfid_scan"
                         and dev["capabilities"].get("rfid") is False
                         and not dev["capabilities"].get("tray_read"))],
    }


# ------------------------------------------------------------------ scenarios

def ams1_base(name="oams1", idx=0, lane_name="fps"):
    u = Unit(name, idx, "ams1", lane=lane_name)
    u.ready = [True, True, True, False]
    u.spool = [(12, "tag"), (15, "manual"), None, None]
    u.tag = [bambu_tag(12, "PLA", "PLA Basic", 0xFF0A2989, "8a3f12c4",
                       "5c2e8d0a9f1b4e7c8a2d6f0e3b9c1a47"), None, None, None]
    u.env = {"temp_c": 24.3, "rh_pct": 31.0, "source": "hdc1080"}
    return u


def scenarios():
    out = {}
    groups1 = {"T0": [("oams1", 0), ("oams1", 1)], "T1": [("oams1", 2)]}

    # idle ---------------------------------------------------------------
    u = ams1_base()
    out["idle"] = (build_status([u], {"fps": lane()}, groups1), "oams1")

    # positioning: a spool just inserted in bay 2 is being positioned by the
    # unit on its own (H16); no bay of the unit offers motion meanwhile ----
    u = ams1_base()
    u.ready[2] = True
    u.positioning[2] = True
    out["positioning"] = (build_status([u], {"fps": lane()}, groups1),
                          "oams1")

    # loading ------------------------------------------------------------
    u = ams1_base()
    ln = lane(op="LOADING", group="T0", unit=[0, 0], following=False,
              since=1510.2, pressure=0.21,
              stage={"plan": ["cut", "retract", "feed"], "index": 2,
                     "failed": None},
              motion_stage="load_to_buffer")
    out["loading"] = (build_status([u], {"fps": ln}, groups1), "oams1")

    # loaded -------------------------------------------------------------
    u = ams1_base()
    u.loaded[0] = True
    ln = lane(op="LOADED", group="T0", unit=[0, 0], following=True,
              since=1520.8, message="loaded", pressure=0.48)
    out["loaded"] = (build_status([u], {"fps": ln}, groups1), "oams1")

    # runout_reload: T0 bay 0 ran out, reloading bay 1 -------------------
    u = ams1_base()
    u.ready[0] = False         # F1S cleared: filament tail left the bay
    u.spool[0] = (12, "tag")   # empty spool still in the bay, tag readable
    ln = lane(op="LOADED", group="T0", unit=[0, 0], runout="loading",
              following=False, since=1811.0, reload_target=[0, 1],
              runout_from=[0, 0], pressure=0.05,
              stage={"plan": ["feed"], "index": 0, "failed": None},
              motion_stage="load_to_buffer")
    out["runout_reload"] = (build_status([u], {"fps": ln}, groups1), "oams1")

    # error: AMS HT feed failure (PAUSE) + latched heater fault (STOP) ----
    u = Unit("amsht", 0, "ams_ht")
    u.ready = [True]
    u.spool = [(44, "manual")]
    u.tag = [unit_summary_tag("GFB98", "ASA", 0xFF808080, 240, 270)]
    u.env = {"temp_c": 61.0, "rh_pct": 12.0, "source": "firmware"}
    u.dryer = make_dryer("fault", 5, 80, 0, 0, 100, fault_name="heater_overtemperature",
                         fault=3, chamber=61)
    u.faults = [
        {"severity": "stop", "code": "heater_overtemperature",
         "text": "Dryer heater over temperature", "bay": None,
         "actions": ["clear_fault"], "source": "firmware"},
        {"severity": "pause", "code": "load failed",
         "text": "Spool loading failed (no filament progress (jam, dead motor, or missing sensor))",
         "bay": 0, "actions": ["retry_load", "clear_errors"], "source": "lane"},
    ]
    ln = lane(op="UNLOADED", group=None, unit=None, since=1604.5,
              message="load failed",
              stage={"plan": ["cut", "retract", "feed"], "index": 2,
                     "failed": "feed"})
    out["error"] = (build_status([u], {"fps": ln}, {"T0": [("amsht", 0)]}),
                    "amsht")

    # drying: AMS 2 Pro timed dry ----------------------------------------
    u = Unit("ams2", 0, "ams2")
    u.ready = [True, True, True, True]
    u.spool = [(50, "manual"), (40, "manual"), (41, "manual"), None]
    u.tag = [unit_summary_tag("GFN99", "PA-CF", 0xFF202020, 260, 290),
             unit_summary_tag("GFL99", "PLA", 0xFFFFFFFF, 190, 230),
             None, None]
    u.env = {"temp_c": 55.2, "rh_pct": 9.0, "source": "firmware"}
    u.dryer = make_dryer("timed_dry", 3, 55, 10740, 60, 40, chamber=55)
    groups = {"T0": [("ams2", 0)], "T1": [("ams2", 1), ("ams2", 2)]}
    out["drying"] = (build_status([u], {"fps": lane()}, groups), "ams2")

    # uncalibrated: two lanes. Left: a fresh AMS 1 (shared ptfe_length 0;
    # bay 0 has a ptfe_length_0 override) idle, so load is refused on bays 1-2
    # (D7) with calibrate offered. Right: an AMS 1 re-calibrating its bay 0.
    a = Unit("oams1", 0, "ams1", lane="left", ptfe_clicks=0,
             bay_ptfe=[1824, None, None, None])
    a.ready = [True, True, True, False]
    a.env = {"temp_c": 23.9, "rh_pct": 38.0, "source": "hdc1080"}
    b = Unit("oams2", 1, "ams1", lane="right")
    b.ready = [True, True, False, False]
    b.calibrating_bay = 0
    b.env = {"temp_c": 24.1, "rh_pct": 37.0, "source": "hdc1080"}
    lanes = {"left": lane(since=1400.0, extruder="extruder"),
             "right": lane(op="CALIBRATING", since=1402.5, extruder="extruder1")}
    groups = {"T0": [("oams1", 0), ("oams1", 1)], "T1": [("oams2", 1)]}
    out["uncalibrated"] = (build_status([a, b], lanes, groups), "oams1")

    # multi_unit: AMS 1 + AMS 2 Pro + AMS HT on one lane ------------------
    a = ams1_base("oams1", 0)
    b = Unit("ams2", 1, "ams2")
    b.ready = [True, True, True, False]
    b.loaded[1] = True
    b.spool = [(30, "manual"), (31, "manual"), (21, "manual"), None]
    b.tag = [unit_summary_tag("GFB99", "ABS", 0xFFD32F2F, 240, 270),
             unit_summary_tag("GFB99", "ABS", 0xFFD32F2F, 240, 270),
             unit_summary_tag("GFG99", "PETG", 0xFF000000, 230, 260),
             None]
    b.env = {"temp_c": 27.5, "rh_pct": 22.0, "source": "firmware"}
    b.dryer = make_dryer("off", 0, 0, 0, 0, 0, chamber=27.5)
    b.dryer["target_c"] = None
    c = Unit("amsht", 2, "ams_ht")
    c.ready = [True]
    c.spool = [(44, "manual")]
    c.tag = [unit_summary_tag("GFB98", "ASA", 0xFF808080, 240, 270)]
    c.env = {"temp_c": 26.0, "rh_pct": 18.0, "source": "firmware"}
    c.dryer = make_dryer("off", 0, 0, 0, 0, 0, chamber=26)
    c.dryer["target_c"] = None
    groups = {"T0": [("ams2", 1), ("ams2", 0), ("amsht", 0)],
              "T1": [("oams1", 0), ("oams1", 1)], "T2": [("ams2", 2)],
              "T3": [("oams1", 2)]}
    ln = lane(op="LOADED", group="T0", unit=[1, 1], following=True,
              since=1733.0, message="loaded", pressure=0.5)
    out["multi_unit"] = (build_status([a, b, c], {"fps": ln}, groups), "ams2")

    # remote_unit: display bound to oams1; oams2 on the same lane loads --
    a = ams1_base("oams1", 0)
    b = Unit("oams2", 1, "ams1")
    b.ready = [True, False, True, True]
    b.spool = [None, None, (21, "tag"), None]
    b.tag = [None, None,
             bambu_tag(21, "PETG", "PETG HF", 0xFF000000, "55ee66ff",
                       "00ff11ee22dd33cc44bb55aa66997788", dry_c=65, dry_h=8,
                       bed=70, tmin=230, tmax=260), None]
    b.env = {"temp_c": 24.0, "rh_pct": 33.0, "source": "hdc1080"}
    groups = {"T0": [("oams1", 0), ("oams1", 1)], "T1": [("oams2", 2), ("oams1", 2)]}
    ln = lane(op="LOADING", group="T1", unit=[1, 2], since=1900.4,
              stage={"plan": ["cut", "retract", "feed"], "index": 2,
                     "failed": None},
              motion_stage="load_to_buffer", pressure=0.18)
    out["remote_unit"] = (build_status([a, b], {"fps": ln}, groups), "oams1")

    # boxturtle_8: the 8-lane BoxTurtle install, the widest single unit C2
    # has to carry. A klipper-mmu device is a topology `kind: oams` unit with
    # its own family (C5 §7), no dryer, no RFID and no AMS telemetry tree.
    box = Unit("box", 0, "boxturtle", bays=8, ptfe_clicks=0)
    box.ready = [True, True, True, True, True, True, True, False]
    box.spool = [(12, "tag"), (15, "manual"), (41, "manual"),
                 (40, "manual"), (44, "manual"), (30, "manual"), None, None]
    box.tag = [bambu_tag(12, "PLA", "PLA Basic", 0xFF0A2989, "8a3f12c4",
                         "5c2e8d0a9f1b4e7c8a2d6f0e3b9c1a47"), None, None,
               None, None, None, None, None]
    box.calibrating_bay = 2
    ln = lane(op="UNLOADED", group=None, unit=None, since=1902.1,
              message=None, motion_stage="select_home")
    groups = {"T0": [("box", 0), ("box", 1)], "T1": [("box", 2)],
              "T2": [("box", b) for b in range(3, 8)]}
    out["boxturtle_8"] = (build_status([box], {"fps": ln}, groups), "box")

    # claymore_4: a four-lane indexed selector, the same klipper-mmu unit
    # shape with a selector to home. Its `act` carries `home` (C3 §2
    # OAMS_HOME), which boxturtle_8's feeder-per-lane lanes have not.
    clay = Unit("clay", 0, "claymore", bays=4, ptfe_clicks=1824)
    clay.ready = [True, True, True, False]
    clay.spool = [(12, "tag"), None, (30, "manual"), None]
    clay.tag = [bambu_tag(12, "PLA", "PLA Basic", 0xFF0A2989, "8a3f12c4",
                          "5c2e8d0a9f1b4e7c8a2d6f0e3b9c1a47"),
                None, None, None]
    ln = lane(op="UNLOADED", group=None, unit=None, since=1904.7,
              message=None, motion_stage="select_home")
    groups = {"T0": [("clay", 0), ("clay", 1)], "T1": [("clay", 2)]}
    out["claymore_4"] = (build_status([clay], {"fps": ln}, groups), "clay")
    return out


# ------------------------------------------------------------------ C4 examples

def spool_summary(spool_id):
    return None if spool_id is None else dict(SPOOLS[spool_id])


def c4_examples():
    base_bays = {
        "oams1-0": {"state": "linked", "spool_id": 12, "source": "tag",
                    "pending_confirmation": False, "error": None,
                    "spool": spool_summary(12)},
        "oams1-1": {"state": "linked", "spool_id": 15, "source": "manual",
                    "pending_confirmation": False, "error": None,
                    "spool": spool_summary(15)},
        "oams1-2": {"state": "unlinked", "spool_id": None, "source": "none",
                    "pending_confirmation": False, "error": None,
                    "spool": None},
        "oams1-3": {"state": "unlinked", "spool_id": None, "source": "none",
                    "pending_confirmation": False, "error": None,
                    "spool": None},
    }
    ex = {}
    ex["linked"] = {"schema_version": 1, "spoolman_online": True,
                    "queued_writes": 0, "bays": copy.deepcopy(base_bays)}
    r = copy.deepcopy(base_bays)
    r["oams1-2"] = {"state": "resolving", "spool_id": None, "source": "none",
                    "pending_confirmation": False, "error": None, "spool": None}
    ex["resolving"] = {"schema_version": 1, "spoolman_online": True,
                       "queued_writes": 0, "bays": r}
    p = copy.deepcopy(base_bays)
    # A newly created Bambu spool: full until the display's "New spool, full?"
    # answer, so the summary already exists but remaining is provisional (100%).
    p["oams1-2"] = {"state": "pending_confirmation", "spool_id": 21,
                    "source": "tag", "pending_confirmation": True, "error": None,
                    "spool": spool_summary(21)}
    ex["pending_confirmation"] = {"schema_version": 1, "spoolman_online": True,
                                  "queued_writes": 0, "bays": p}
    o = copy.deepcopy(base_bays)
    o["oams1-2"] = {"state": "resolving", "spool_id": None, "source": "none",
                    "pending_confirmation": False,
                    "error": "Spoolman unreachable; spool creation queued",
                    "spool": None}
    ex["offline"] = {"schema_version": 1, "spoolman_online": False,
                     "queued_writes": 1, "bays": o}
    for name, obj in ex.items():   # the edit capability flag (C4 §9)
        ex[name] = dict([("schema_version", 1), ("spoolman_online",
                         obj["spoolman_online"]), ("edit", True), ("stop", True)]
                        + [(k, v) for k, v in obj.items()
                           if k not in ("schema_version", "spoolman_online")])
    return ex


def minified(obj):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def render():
    """Every example file as {absolute path: text}, without writing any."""
    out = {}

    def dump(path, obj):
        out[path] = json.dumps(obj, indent=2) + "\n"

    for name, (status, bound) in scenarios().items():
        dump(os.path.join(OUT_C2, "oams_manager_%s.json" % name), status)
        dump(os.path.join(OUT_C2, "openams_ui_%s.json" % name),
             project_ui(status, bound))
    for name, obj in c4_examples().items():
        dump(os.path.join(OUT_C4, "openams_spoolman_%s.json" % name), obj)
    return out


def check():
    """Compare the checked-in examples with a fresh generation; return the
    list of files that differ or are missing (empty when in step)."""
    stale = []
    for path, text in sorted(render().items()):
        try:
            with open(path) as f:
                current = f.read()
        except OSError:
            current = None
        if current != text:
            stale.append(path)
    return stale


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--check" in argv:
        stale = check()
        for path in stale:
            print("STALE " + os.path.relpath(path, REPO))
        if stale:
            print("%d example(s) differ from a fresh generation; run"
                  " docs/contracts/tools/gen_examples.py" % len(stale))
            return 1
        print("OK: examples match a fresh generation")
        return 0
    os.makedirs(OUT_C2, exist_ok=True)
    os.makedirs(OUT_C4, exist_ok=True)
    for path, text in render().items():
        with open(path, "w") as f:
            f.write(text)
    for name, (status, bound) in scenarios().items():
        ui = project_ui(status, bound)
        print("%-14s oams_manager=%6d B  openams_ui(%s)=%4d B" % (
            name, len(minified(status)), bound, len(minified(ui).encode())))
    for name, obj in c4_examples().items():
        print("c4 %-20s %4d B" % (name, len(minified(obj))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
