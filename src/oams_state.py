# OpenAMS — pure state machine (store core)
#
# Copyright (C) 2025-2026 JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# This module is INTENTIONALLY PURE: it imports nothing from Klipper (no mcu, no
# reactor, no gcode) and performs no I/O. It defines the immutable state, the
# events (actions), the side effects (as data), and a pure reducer
#   reduce(system, action, world, now) -> (new_system, [effects])
# so the orchestration logic can be unit-tested with no hardware. The runtime
# (oams_runtime.py) is the only place that executes effects and touches Klipper.

import re
from dataclasses import dataclass, field, replace
from typing import FrozenSet, Mapping, Optional, Tuple

# --- firmware result codes (SUCCESS is 0 -> always compare, never test truthy) -
# The full firmware enum is kept here even where the host never branches on a
# code, so "(code N)" in user-facing messages can be looked up.
OAMS_OP_CODE_SUCCESS = 0
OAMS_OP_CODE_ERROR_UNSPECIFIED = 1
OAMS_OP_CODE_ERROR_BUSY = 2
OAMS_OP_CODE_SPOOL_ALREADY_IN_BAY = 3
OAMS_OP_CODE_NO_SPOOL_IN_BAY = 4
OAMS_OP_CODE_ERROR_KLIPPER_CALL = 5
OAMS_OP_CODE_CANCEL = 6
OAMS_OP_CODE_TIMEOUT = 7
# F9: the firmware refused a load because the bay's PTFE length is unset (the
# request result "uncalibrated", 6 in ams_request_result).
OAMS_OP_CODE_UNCALIBRATED = 8
# F8: an operation cut short because the unit lost its host session (its
# watchdog fired, or a config_reset); recovery is FIRMWARE_RESTART.
OAMS_OP_CODE_SESSION_LOST = 9
SESSION_LOST_TEXT = ("host communication lost: the unit stopped and needs "
                     "FIRMWARE_RESTART")


def describe_code(code, fault=None):
    """Human-readable form of a firmware result code for user-facing
    messages. Verified against firmware 2.0.25: BUSY is op_begin's rejection
    of a concurrent op, ALREADY_IN_BAY is the load coroutine's occupied-hub
    rejection, NO_SPOOL_IN_BAY is unload-with-nothing-loaded, KLIPPER_CALL is
    a calibration aborted via emergency stop. TIMEOUT (protocol v3+) is the
    firmware's own no-progress watchdog: it has already stopped the motors.
    `fault`, when given, is the unit's latched fault text and is appended."""
    text = _describe_code(code)
    return "%s; %s" % (text, fault) if fault else text


def _describe_code(code):
    if code == OAMS_OP_CODE_ERROR_BUSY:
        return "OAMS is busy with another operation"
    if code == OAMS_OP_CODE_SPOOL_ALREADY_IN_BAY:
        return "filament already detected in the hub"
    if code == OAMS_OP_CODE_NO_SPOOL_IN_BAY:
        return "no spool present in the bay"
    if code == OAMS_OP_CODE_ERROR_KLIPPER_CALL:
        return "stopped by klipper monitor"
    if code == OAMS_OP_CODE_TIMEOUT:
        return "no filament progress (jam, dead motor, or missing sensor)"
    if code == OAMS_OP_CODE_SESSION_LOST:
        return SESSION_LOST_TEXT
    if code == OAMS_OP_CODE_UNCALIBRATED:
        return "the bay has no PTFE length; run OAMS_CALIBRATE_PTFE_LENGTH"
    if code == OAMS_OP_CODE_CANCEL:
        return "cancelled"
    if code == OAMS_OP_CODE_ERROR_UNSPECIFIED:
        # H18: the firmware's generic refusal (op code 1). The unit's own reply
        # carries no reason, so the host maps it to the one thing code 1
        # always means: it declined the op. Anything the host can predict
        # (uncalibrated bay, latched fault) is refused before sending instead.
        return "the unit refused the operation (not ready or not permitted)"
    return "code %s" % (code,)

# The lane message (and the G-code error) of a sequence the user stopped.
STOPPED_TEXT = "Stopped by the user"

# --- follower directions (firmware convention) ---
FOLLOWER_REVERSE = 0
FOLLOWER_FORWARD = 1

# --- top-level operating states ---
OP_UNLOADED = "UNLOADED"
OP_LOADING = "LOADING"
OP_LOADED = "LOADED"
OP_UNLOADING = "UNLOADING"
OP_CALIBRATING = "CALIBRATING"

# --- runout sub-state (only meaningful while OP_LOADED) ---
RUNOUT_IDLE = "idle"
RUNOUT_PAUSING = "pausing"      # feeding PAUSE_DISTANCE before coasting
RUNOUT_COASTING = "coasting"    # follower coasting, consuming the old tail
RUNOUT_LOADING = "loading"      # next-spool load in flight (non-blocking)

# --- tunables (decision constants; shared with the runtime) ---
PAUSE_DISTANCE = 60.0
# Authoritative per-op deadline used ONLY against firmware that makes no
# liveness promise (protocol < 3 / legacy). Protocol >= 3 firmware runs its own
# no-progress watchdog and always completes an op, so the host downgrades this
# to a coarse disconnect backstop (OAMS_DISCONNECT_BACKSTOP) that exists only to
# release a blocked GCode wait() if the MCU dies entirely.
OAMS_ACTION_TIMEOUT = 120.0
OAMS_DISCONNECT_BACKSTOP = 300.0
POLL_INTERVAL = 0.1
MONITOR_INTERVAL = 1.0

# --- stage vocabulary (C2 §5.3) ---
# The standard step names every interface translates, how long a plan with no
# recorded failure may sit unchanged on a settled lane before the reducer drops
# it (a guard against a macro that aborted without announcing DONE), and the
# limits a plan and its display text are held to.
STAGE_NAMES = ("cut", "retract", "feed", "purge", "calibrate", "heat", "home",
               "clean")
STAGE_STALE_S = 600.0
# A macro plan whose step changed this recently is a flow in progress on the
# lane (no runout starts, a flickering hub is held loaded); older, an unfinished
# plan is not assumed to be running anything.
STAGE_FLOW_FRESH_S = 180.0
# A custom step name (any standard name is a valid custom name too).
STAGE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,15}$")
STAGE_MAX_STEPS = 8
STAGE_LABEL_MAX_BYTES = 47
STAGE_DETAIL_MAX_BYTES = 24


def stage_name_ok(name):
    """Whether `name` is a legal step name: a standard name, or a custom one
    matching the name rule (C2 §5.3)."""
    return bool(STAGE_NAME_RE.match(name))


def stage_text(text, limit):
    """A trimmed display string, cut to `limit` UTF-8 bytes on a code point
    boundary (never mid-character). An empty result is `None`."""
    text = (text or "").strip()
    if not text:
        return None
    raw = text.encode("utf-8")[:limit]
    while raw:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raw = raw[:-1]
    return None


# ====================================================================== state

@dataclass(frozen=True)
class LaneState:
    """Immutable per-FPS-lane state. Transition with dataclasses.replace()."""
    op: str = OP_UNLOADED
    group: Optional[str] = None
    unit: Optional[Tuple[int, int]] = None      # (oams_idx, bay)
    # None means a follower command failed; its physical state is unknown.
    following: Optional[bool] = False
    direction: int = FOLLOWER_FORWARD
    # Failed commands can target a unit after a completed op clears lane.unit.
    uncertain_follower_units: Tuple[Tuple[int, int], ...] = ()
    # runout sub-machine
    runout: str = RUNOUT_IDLE
    pause_origin: Optional[float] = None
    coast_origin: Optional[float] = None
    reload_target: Optional[Tuple[int, int]] = None
    # async-op bookkeeping (one firmware op in flight per lane)
    op_deadline: Optional[float] = None
    # Monotonic nonzero 32-bit per-lane op generation. Incremented every time
    # a firmware op is started; Start* effects carry it and the driver echoes
    # it back in OpCompleted, so a stale reply (late after a timeout, or from
    # a different OAMS unit on the same lane) cannot complete the wrong op.
    # Zero is reserved: it is the firmware sentinel for "no active operation".
    # Protocol v4 echoes all 32 bits; the runtime narrows the value to one
    # byte for every driver that cannot (see Runtime._driver_gen).
    op_gen: int = 0
    prior_op: Optional[str] = None              # op to return to after CALIBRATING
    since: float = 0.0
    message: Optional[str] = None
    # C2 §4.8 lane fault: the reducer's reason and the bay it concerns. Read
    # only while `message` names a failure; every failure transition sets both.
    fault_text: Optional[str] = None
    fault_unit: Optional[Tuple[int, int]] = None
    # C2 §5.3 stage plan: the announced step list, the 0-based current step
    # (None = announced but not started), the first step that failed (None =
    # none), whether the manager created it itself, and when it last changed.
    # The display label of each labeled step and the current step's detail
    # ride with the plan; the automatic plan carries neither.
    stage_plan: Tuple[str, ...] = ()
    stage_index: Optional[int] = None
    stage_failed: Optional[str] = None
    stage_auto: bool = False
    stage_since: float = 0.0
    stage_labels: Tuple[Tuple[str, str], ...] = ()
    stage_detail: Optional[str] = None
    # C2 §5.2/§5.3 rule 5: the loaded bay a runout started from, kept for the UI
    # after the reload succeeds or fails; cleared only by the next user-initiated
    # load/unload on this lane or by CLEAR_ERRORS.
    runout_from: Optional[Tuple[int, int]] = None


@dataclass(frozen=True)
class SystemState:
    lanes: Mapping[str, LaneState] = field(default_factory=dict)  # fps_name -> LaneState
    # True once every bound OAMS reports protocol >= 3, i.e. the firmware owns
    # per-op liveness (its own no-progress watchdog). The host then uses only a
    # coarse disconnect backstop instead of an authoritative per-op deadline.
    fw_owns_liveness: bool = False
    # Retained firmware faults, keyed by physical lane (255 is global).
    firmware_faults: Mapping[int, tuple] = field(default_factory=dict)


def set_liveness(system, owns):
    """Return system with the firmware-owns-liveness flag set (called once at
    ready, after every unit's protocol version is known)."""
    return replace(system, fw_owns_liveness=bool(owns))


# ====================================================================== world

@dataclass(frozen=True)
class LaneWorld:
    """Read-only hardware snapshot the reducer is allowed to see, per lane."""
    extruder_pos: float = 0.0
    printing: bool = False  # a print job runs (print_stats), not just G-code
    loaded: Mapping[Tuple[int, int], bool] = field(default_factory=dict)  # hub HES
    ready: Mapping[Tuple[int, int], bool] = field(default_factory=dict)   # bay_loadable() (f1s HES, or a no-BAY unit's own gate status)
    calibrated: Mapping[Tuple[int, int], bool] = field(default_factory=dict)  # H10: per-bay PTFE length is set (False = unusable)
    # H15: unit index -> refusal reason for a unit that may not move
    # filament. A unit absent from the map can move.
    motion_blocked: Mapping[int, str] = field(default_factory=dict)
    # H16: a unit present here answers BUSY to any motion (C1 §4.1)
    positioning: Mapping[int, FrozenSet[int]] = field(default_factory=dict)
    reserved: Mapping[int, int] = field(default_factory=dict)  # local PRELOAD, unit -> bay
    group_bays: Mapping[str, Tuple[Tuple[int, int], ...]] = field(default_factory=dict)
    path_len: Mapping[int, float] = field(default_factory=dict)           # unit idx -> mm
    # path_len is the filament path from the unit's 'loaded' sensor to the
    # extruder, in millimetres. Unit conversion (e.g. OAMS encoder clicks)
    # happens in the drivers, so the reducer has exactly one unit to reason in.
    reload_before: float = 0.0
    # C2 §4.9 settings.auto_reload_on_runout (H7): false turns the coasting
    # automatic reload into a pause.
    auto_reload: bool = True
    # unit index -> the unit's config name ("oams1"), so a reason can name a
    # spool the way the UI does ("oams1 Spool 3"). Absent: "OAMS <index>".
    unit_names: Mapping[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class World:
    lanes: Mapping[str, LaneWorld] = field(default_factory=dict)

    def lane(self, fps):
        return self.lanes.get(fps, LaneWorld())


# ===================================================================== actions

@dataclass(frozen=True)
class Tick:
    pass

@dataclass(frozen=True)
class Load:
    fps: str = ""
    group: str = ""

@dataclass(frozen=True)
class LoadBay:
    # Low-level: load a specific (oams_idx, bay) directly (OAMS_LOAD_SPOOL),
    # bypassing group selection. The group, if the bay belongs to one, is
    # resolved so runout protection still applies.
    fps: str = ""
    unit: Tuple[int, int] = (0, 0)

@dataclass(frozen=True)
class Unload:
    fps: str = ""

@dataclass(frozen=True)
class Cancel:
    fps: str = ""

@dataclass(frozen=True)
class Calibrate:
    fps: str = ""
    oams_idx: int = 0
    bay: int = 0
    kind: str = "ptfe"          # "ptfe" | "hub_hes"

@dataclass(frozen=True)
class OpCompleted:
    fps: str = ""
    code: int = OAMS_OP_CODE_SUCCESS
    value: Optional[int] = None
    # Echo of the op_gen the driver was started with. The reducer only accepts
    # a completion whose gen matches the lane's op_gen exactly: every op the
    # store starts carries an int gen, so gen=None (an unsolicited firmware
    # status from a unit with no op in flight) never completes anything.
    gen: Optional[int] = None
    # The unit's latched fault text at completion (name plus the recovery
    # hint), when its telemetry carries one; shown beside a failure code.
    fault: Optional[str] = None

@dataclass(frozen=True)
class Timeout:
    fps: str = ""

@dataclass(frozen=True)
class Follow:
    # Enable/disable the follower on the lane's loaded unit, through the store
    # so LaneState.following/direction stay truthful.
    fps: str = ""
    enable: int = 0
    direction: int = FOLLOWER_FORWARD

@dataclass(frozen=True)
class FollowerFailed:
    fps: str
    unit: Tuple[int, int]

@dataclass(frozen=True)
class FollowerApplied:
    fps: str
    unit: Tuple[int, int]
    following: bool

@dataclass(frozen=True)
class ClearErrors:
    # The monitor's automatic resync (after a unit's snapshot or motion
    # permission was briefly unavailable) keeps a lane whose firmware op is
    # still in flight: that op's own completion or deadline settles it. An
    # explicit OAMSM_CLEAR_ERRORS resyncs every lane.
    keep_in_flight: bool = False

@dataclass(frozen=True)
class StagePlan:
    # OAMSM_STAGE PLAN=... announces a macro's step list (C2 §5.3). Pure: it
    # only touches LaneState and returns no effects. An invalid request leaves
    # the lane unchanged; the manager warns, the reducer does not.
    # `labels` carries the (step, display label) pairs of the plan, already
    # trimmed and length-limited; the manager drops a label whose step is not
    # in the plan and the reducer drops it again.
    fps: str = ""
    plan: Tuple[str, ...] = ()
    labels: Tuple[Tuple[str, str], ...] = ()

@dataclass(frozen=True)
class StageEnter:
    # OAMSM_STAGE NAME=...: the macro enters a step of its plan, with the
    # DETAIL text it wants shown for that step (None clears it).
    fps: str = ""
    name: str = ""
    detail: Optional[str] = None

@dataclass(frozen=True)
class StageFail:
    # OAMSM_STAGE FAILED=1: the macro marks the current step failed.
    fps: str = ""

@dataclass(frozen=True)
class Stopped:
    # openams/stop took effect on this lane: the current step failed and the
    # lane's message says the user stopped it.
    fps: str = ""

@dataclass(frozen=True)
class StageDone:
    # OAMSM_STAGE DONE=1: the macro ends its plan.
    fps: str = ""

@dataclass(frozen=True)
class FirmwareFault:
    fps: Optional[str]
    physical_lane: int
    code: int
    identity: tuple

@dataclass(frozen=True)
class FirmwareFaultCleared:
    # A verified clear of a latched firmware fault: identity must match the
    # stored latch's identity exactly, or nothing changes.
    physical_lane: int
    identity: tuple


@dataclass(frozen=True)
class UnitSessionLost:
    # A unit's host-communication watchdog fired: the firmware stopped all
    # motion and dropped the pending op without a reply. The lane that was
    # waiting on that unit settles now, with the reason, sending no command
    # (the unit no longer listens).
    fps: str = ""
    oams_idx: int = 0
    reason: str = ""
    printing: bool = False


# Actions whose reduction reads the hardware snapshot. The runtime only builds
# a World for these; everything else (completions, timeouts, follower toggles)
# is decided from LaneState alone. Keep this in sync with the reducer: a branch
# that starts reading `lw` for a new action type must be added here.
WORLD_ACTIONS = (Load, LoadBay, Tick, ClearErrors, FirmwareFaultCleared)


# ===================================================================== effects

@dataclass(frozen=True)
class OpResult:
    ok: bool
    code: Optional[int]
    message: str
    value: Optional[int] = None

@dataclass(frozen=True)
class StartLoad:
    unit: Tuple[int, int]
    fps: str = ""
    gen: int = 0

@dataclass(frozen=True)
class StartUnload:
    unit: Tuple[int, int]
    fps: str = ""
    gen: int = 0

@dataclass(frozen=True)
class StartCalibrate:
    unit: Tuple[int, int]
    kind: str
    fps: str = ""
    gen: int = 0

@dataclass(frozen=True)
class CancelLoad:
    unit: Tuple[int, int]
    gen: int = 0

@dataclass(frozen=True)
class CancelUnload:
    unit: Tuple[int, int]
    gen: int = 0

@dataclass(frozen=True)
class SetFollower:
    unit: Tuple[int, int]
    enable: int
    direction: int
    fps: str = ""

@dataclass(frozen=True)
class Pause:
    fps: str
    reason: str

@dataclass(frozen=True)
class ArmDeadline:
    fps: str
    seconds: float

@dataclass(frozen=True)
class CancelDeadline:
    fps: str

@dataclass(frozen=True)
class Settle:
    fps: str
    result: OpResult


# ===================================================================== reducer

def initial_system(fps_names):
    return SystemState(lanes={name: LaneState() for name in fps_names})


def reduce(system, action, world, now):
    """Pure top-level reducer. Returns (new_system, [effects]).

    Contract: every request action (Load, LoadBay, Unload, Calibrate) either
    starts a firmware op or settles synchronously with a refusal. It never
    returns an empty effects list while a waiter is pending: a caller blocked
    in request(...).wait() will always receive a result, either the op
    completion or an immediate refusal from this function."""
    # Per-op deadline: authoritative for legacy firmware, a coarse disconnect
    # backstop once the firmware owns liveness (protocol >= 3).
    deadline = (OAMS_DISCONNECT_BACKSTOP if system.fw_owns_liveness
                else OAMS_ACTION_TIMEOUT)

    if isinstance(action, FirmwareFault):
        # The caller validates the retained event against the MCU/session.
        # A global fault is only admissible with no operation in flight.
        if (type(action.physical_lane) is not int
                or not 0 <= action.physical_lane <= 255
                or type(action.code) is not int or action.code <= 0
                or not isinstance(action.identity, tuple)
                or not action.identity):
            return system, []
        if action.physical_lane == 255:
            if action.fps is not None or any(
                    lane.op in (OP_LOADING, OP_UNLOADING, OP_CALIBRATING)
                    or lane.runout == RUNOUT_LOADING
                    or lane.following is not False
                    or lane.uncertain_follower_units
                    for lane in system.lanes.values()):
                return system, []
        else:
            lane = system.lanes.get(action.fps)
            if (lane is None or lane.unit is None
                    or lane.unit[1] != action.physical_lane
                    or lane.following is not False
                    or lane.uncertain_follower_units):
                return system, []
        previous = system.firmware_faults.get(action.physical_lane)
        if previous is not None:
            return system, []
        faults = dict(system.firmware_faults)
        faults[action.physical_lane] = (action.fps, action.code, action.identity)
        effects = []
        if action.fps is not None:
            lane = system.lanes[action.fps]
            if lane.op_deadline is not None:
                effects.append(CancelDeadline(action.fps))
            if lane.op in (OP_LOADING, OP_UNLOADING, OP_CALIBRATING):
                effects.append(Settle(action.fps, OpResult(
                    False, action.code, "firmware fault (%s)" % action.code)))
        effects.append(Pause(action.fps or "global", "firmware fault (%s)" % action.code))
        return replace(system, firmware_faults=faults), effects

    if isinstance(action, FirmwareFaultCleared):
        if (type(action.physical_lane) is not int
                or not 0 <= action.physical_lane <= 255
                or not isinstance(action.identity, tuple) or not action.identity):
            return system, []
        record = system.firmware_faults.get(action.physical_lane)
        if record is None or record[2] != action.identity:
            return system, []
        faults = dict(system.firmware_faults)
        del faults[action.physical_lane]
        targets = (system.lanes if action.physical_lane == 255
                   else {record[0]: system.lanes[record[0]]}
                        if record[0] in system.lanes else {})
        lanes = dict(system.lanes)
        for fps, old in targets.items():
            lane = _resync_lane(world.lane(fps), now)
            if old.uncertain_follower_units:
                lane = replace(lane, following=None,
                               uncertain_follower_units=old.uncertain_follower_units)
            lanes[fps] = lane
        return replace(system, firmware_faults=faults, lanes=lanes), []

    if isinstance(action, ClearErrors):
        lanes = {}
        for fps, old in system.lanes.items():
            if action.keep_in_flight and _in_flight(old):
                lanes[fps] = old
                continue
            lane = _resync_lane(world.lane(fps), now)
            if old.uncertain_follower_units:
                lane = replace(lane, following=None,
                               uncertain_follower_units=
                               old.uncertain_follower_units)
            lanes[fps] = lane
        # Generic OpenAMS resync is not proof that firmware cleared a latched
        # MMU fault. Keep the physical latch until a separate verified clear.
        return replace(system, lanes=lanes), []

    if isinstance(action, Tick):
        lanes = dict(system.lanes)
        effects = []
        for fps, lane in system.lanes.items():
            nl, fx = _reduce_lane(lane, action, world.lane(fps), now, fps,
                                  deadline)
            lanes[fps] = nl
            effects.extend(fx)
        return replace(system, lanes=lanes), effects

    # lane-scoped action
    fps = getattr(action, "fps", None)
    if fps is None or fps not in system.lanes:
        # An fps that is not None but is not registered: settle request-type
        # actions with a refusal so their waiters are never stranded.
        if fps is not None and isinstance(action, (Load, LoadBay, Unload, Calibrate)):
            return system, [Settle(fps, OpResult(False, None,
                                                 "unknown lane %s" % fps))]
        return system, []
    if any(record[0] == fps or physical_lane == 255
           for physical_lane, record in system.firmware_faults.items()):
        # A firmware fault is latched on this lane or system-wide. Settle
        # request-type actions immediately so their waiters are never stranded.
        if isinstance(action, (Load, LoadBay, Unload, Calibrate)):
            msg = _latched_fault_msg(fps, system.firmware_faults)
            return system, [Settle(fps, OpResult(False, None, msg))]
        return system, []
    nl, fx = _reduce_lane(system.lanes[fps], action, world.lane(fps), now, fps,
                          deadline)
    lanes = dict(system.lanes)
    lanes[fps] = nl
    return replace(system, lanes=lanes), fx


def _latched_fault_msg(fps, firmware_faults):
    """Return a user-facing refusal message for the first latched fault that
    blocks fps (either a lane-specific fault or a global physical_lane 255 one).
    Falls back to a generic string if the fault map is unexpectedly empty."""
    for physical_lane, record in firmware_faults.items():
        if record[0] == fps or physical_lane == 255:
            return ("firmware fault (%s) latched; OAMS_CLEAR_FAULT first"
                    % record[1])
    return "firmware fault latched; OAMS_CLEAR_FAULT first"


def _in_flight(lane):
    """A firmware op (or a runout reload) is running on this lane."""
    return (lane.op in (OP_LOADING, OP_UNLOADING, OP_CALIBRATING)
            or (lane.op == OP_LOADED and lane.runout == RUNOUT_LOADING))


def _resync_lane(lw, now):
    """Recompute a lane purely from the hardware snapshot (ClearErrors/startup)."""
    for group, bays in lw.group_bays.items():
        for unit in bays:
            if lw.loaded.get(unit):
                # H14: the hub sensor proves a spool is loaded, but says
                # nothing about the follower. Publishing following=False would
                # assert a stopped follower that may still be running, so
                # publish the honest unknown (None) instead. None is the
                # codebase's existing "follower state unverified" value, and
                # the manager already treats it conservatively (it stops such
                # followers rather than assuming they are idle). The
                # alternative — issuing SetFollower(0) on a resync — would put
                # a side effect on this shared, deliberately effect-free path
                # (the fault-clear resync asserts it returns no effects).
                return LaneState(op=OP_LOADED, group=group, unit=unit,
                                 following=None, since=now)
    return LaneState(op=OP_UNLOADED, since=now)


def _group_of(lw, unit):
    """The group on this lane that contains `unit`, or None."""
    for group, bays in lw.group_bays.items():
        if unit in bays:
            return group
    return None


def _hub_occupied(lw):
    """True when any hub HES on the lane still sees filament."""
    return any(lw.loaded.values())


def _selectable(lw, unit):
    """A local PRELOAD holds its unit for that bay alone."""
    return lw.reserved.get(unit[0], unit[1]) == unit[1]


def _group_candidates(lw, group):
    bays = lw.group_bays.get(group, ())
    # H10: a bay with no PTFE length is not loadable — skip it entirely so
    # group selection can fall through to a calibrated spare. H15: a bay on a
    # unit that may not move filament is skipped the same way. H16:
    # a bay on a unit that is positioning a fresh spool would only get BUSY.
    bays = tuple(unit for unit in bays
                 if _calibrated(lw, unit)
                 and unit[0] not in lw.motion_blocked
                 and unit[0] not in lw.positioning)
    reserved = tuple(unit for unit in bays
                     if lw.reserved.get(unit[0]) == unit[1])
    return reserved + tuple(unit for unit in bays
                            if unit not in reserved and _selectable(lw, unit))


def _calibrated(lw, unit):
    """True unless the snapshot explicitly says this bay is uncalibrated. A
    unit with no per-bay length (e.g. a follower) is absent from the map and
    stays usable, so this must test `is False`, not falsiness."""
    return lw.calibrated.get(unit) is not False


def spool_name(unit_name, bay):
    """A spool position in a sentence: its unit's config name and the 1-based
    position, "oams2 Spool 3" (bay index 2). Two units can share a family name,
    so a bare "Spool 3" would be ambiguous."""
    return "%s Spool %d" % (unit_name, bay + 1)


def _unit_label(lw, idx):
    return lw.unit_names.get(idx, "OAMS %d" % idx)


def _uncalibrated_msg(lw, unit):
    """H10 refusal: the bay has no PTFE length, so a load would be refused by
    the unit anyway. Naming the exact bay lets the user fix it directly."""
    return ("%s is not calibrated; run OAMS_CALIBRATE_PTFE_LENGTH"
            " SPOOL=%d" % (spool_name(_unit_label(lw, unit[0]), unit[1]),
                           unit[1]))


def _positioning_msg(lw, unit):
    """H16 refusal: the unit is positioning a newly inserted spool, and while
    it does, it answers BUSY to every motion command."""
    return ("%s is positioning a newly inserted spool; retry when it"
            " finishes" % _unit_label(lw, unit[0]))


def _load_guard(lane, lw, fps, unit=None):
    """Shared pre-load safety checks for Load and LoadBay. Returns the Settle
    effects rejecting the request, or None when loading may start. `unit` is
    the bay being loaded when the caller already fixed one (LoadBay)."""
    if lane.op != OP_UNLOADED:
        return [Settle(fps, OpResult(False, None, "lane busy (%s)" % lane.op))]
    if lane.uncertain_follower_units:
        return [Settle(fps, OpResult(False, None,
                "follower state unknown on unit(s) %s; stop and verify them"
                " before loading" % (lane.uncertain_follower_units,)))]
    if _hub_occupied(lw):
        return [Settle(fps, OpResult(False, None,
                "filament still detected in a hub on this lane;"
                " unload it first"))]
    if unit is not None and unit[0] in lw.motion_blocked:
        return [Settle(fps, OpResult(False, None, lw.motion_blocked[unit[0]]))]
    if unit is not None and unit[0] in lw.positioning:
        return [Settle(fps, OpResult(False, None, _positioning_msg(lw, unit)))]
    if unit is not None and lw.calibrated.get(unit) is False:
        return [Settle(fps, OpResult(False, None, _uncalibrated_msg(lw, unit)))]
    return None


def _begin_op(lane, now, fps, deadline, effect_for_gen, stage=None, **fields):
    """Single chokepoint for starting a firmware op: bumps the lane's op_gen,
    stamps the deadline, and arms the runtime timer, so the gen/deadline
    invariant cannot be forgotten at an individual call site. `deadline` is the
    authoritative per-op timeout for legacy firmware, or a coarse disconnect
    backstop once the firmware owns liveness.

    `stage` is the C2 §5.3 step this op performs (feed/retract/calibrate): the
    op enters it in an active macro plan, or creates the automatic one-step
    plan, so every path that starts an op goes through the same rule.

    op_gen is a nonzero unsigned 32-bit value: protocol-v4 firmware echoes it
    exactly, and zero stays reserved for the firmware's "no active operation"
    sentinel, so UINT32_MAX wraps to 1. Drivers whose wire carries one byte
    (oams_cmd_*2 gen=%c, the follower) receive the low byte; the runtime maps
    their echo back to this logical value."""
    gen = (lane.op_gen + 1) & 0xFFFFFFFF
    if not gen:
        gen = 1
    if stage is not None:
        lane = _stage_step(lane, stage, now)
    nl = replace(lane, op_gen=gen, op_deadline=now + deadline,
                 since=now, **fields)
    return nl, [effect_for_gen(gen), ArmDeadline(fps, deadline)]


def _stage_clear(lane):
    """Reset every stage field to its default (C2 §5.3 rules 3-4)."""
    return replace(lane, stage_plan=(), stage_index=None, stage_failed=None,
                   stage_auto=False, stage_since=0.0, stage_labels=(),
                   stage_detail=None)


def _stage_plan_ok(plan):
    """A legal plan (C2 §5.3): 1 to STAGE_MAX_STEPS steps, every name a legal
    step name (standard or custom), and no duplicates."""
    return (bool(plan) and len(plan) <= STAGE_MAX_STEPS
            and all(stage_name_ok(name) for name in plan)
            and len(set(plan)) == len(plan))


def _stage_macro_active(lane):
    """An active macro plan: announced and not yet ended, not an automatic
    one-step plan the manager created, and with no recorded failure."""
    return (bool(lane.stage_plan) and not lane.stage_auto
            and lane.stage_failed is None)


def _stage_step(lane, name, now):
    """Rule 2: an op that starts enters its step in an active macro plan when
    the plan contains it (leaving the stage alone when it does not), or creates
    the automatic one-step plan when there is no active macro plan.

    A macro plan whose index is still None (announced, never entered) or that
    this step would move backwards through has gone stale: it never got the
    STAGE FAILED=1 that a strict macro raise should have sent, and a later
    manager op must not be attributed to it. Such a plan has ended; fall
    through and create the automatic plan instead (design review, finding
    2)."""
    if _stage_macro_active(lane):
        if name in lane.stage_plan:
            pos = lane.stage_plan.index(name)
            if lane.stage_index is not None and pos >= lane.stage_index:
                # Rule 6: a detail belongs to the step it was given with, so
                # entering a step the manager did not describe clears it.
                return replace(lane, stage_index=pos, stage_since=now,
                               stage_detail=None)
        else:
            return lane
    return replace(lane, stage_plan=(name,), stage_index=0, stage_failed=None,
                   stage_auto=True, stage_since=now, stage_labels=(),
                   stage_detail=None)


def _stage_fail_current(lane, now):
    """Rules 2-3: record the current step as failed. The first failure wins
    until a new plan is announced; an index is required so an announced but
    unstarted plan is never attributed."""
    if not lane.stage_plan or lane.stage_index is None:
        return lane
    if lane.stage_failed is not None:
        return lane
    return replace(lane, stage_failed=lane.stage_plan[lane.stage_index],
                   stage_since=now)


def _stage_refused(lane, now):
    """Rule 2: a synchronous refusal never creates a plan; an active macro plan
    marks the current step failed."""
    if not _stage_macro_active(lane) or lane.stage_index is None:
        return lane
    return replace(lane, stage_failed=lane.stage_plan[lane.stage_index],
                   stage_since=now)


def _stage_op_end(lane, now, ok):
    """Rule 3: a manager op completed. On success an automatic plan ends and a
    macro plan is left for the macro to continue; on any failure the current
    step is attributed (an automatic plan is kept, not cleared)."""
    if not lane.stage_plan:
        return lane
    if ok:
        return _stage_clear(lane) if lane.stage_auto else lane
    return _stage_fail_current(lane, now)


def _reduce_lane(lane, action, lw, now, fps, deadline):
    op = lane.op

    # C2 §5.3 stage-announcement actions (OAMSM_STAGE). Pure, no effects; a
    # request the contract does not allow leaves the lane unchanged.
    if isinstance(action, StagePlan):
        if not _stage_plan_ok(action.plan):
            return lane, []
        labels = []
        for step, label in action.labels:
            text = stage_text(label, STAGE_LABEL_MAX_BYTES)
            if text is not None and step in action.plan:
                labels.append((step, text))
        return replace(lane, stage_plan=tuple(action.plan), stage_index=None,
                       stage_failed=None, stage_auto=False,
                       stage_since=now, stage_labels=tuple(labels),
                       stage_detail=None), []

    if isinstance(action, StageEnter):
        if not lane.stage_plan or action.name not in lane.stage_plan:
            return lane, []
        pos = lane.stage_plan.index(action.name)
        if lane.stage_index is not None and pos < lane.stage_index:
            return lane, []
        return replace(lane, stage_index=pos, stage_since=now,
                       stage_detail=stage_text(action.detail,
                                               STAGE_DETAIL_MAX_BYTES)), []

    if isinstance(action, StageFail):
        return _stage_fail_current(lane, now), []

    if isinstance(action, Stopped):
        return replace(_stage_fail_current(lane, now),
                       message=STOPPED_TEXT), []

    if isinstance(action, StageDone):
        if not lane.stage_plan:
            return lane, []
        return _stage_clear(lane), []

    if isinstance(action, Load):
        rejected = _load_guard(lane, lw, fps)
        if rejected is not None:
            return _stage_refused(lane, now), rejected
        for unit in _group_candidates(lw, action.group):
            if lw.ready.get(unit):
                return _begin_op(
                    lane, now, fps, deadline,
                    lambda gen, unit=unit: StartLoad(unit, fps, gen),
                    op=OP_LOADING, group=action.group, unit=unit,
                    runout=RUNOUT_IDLE, pause_origin=None, coast_origin=None,
                    reload_target=None, runout_from=None, message=None,
                    stage="feed")
        # H15: name a ready bay whose unit cannot move, so the user learns
        # why instead of seeing a generic empty-group error.
        for unit in lw.group_bays.get(action.group, ()):
            if lw.ready.get(unit) and unit[0] in lw.motion_blocked:
                return _stage_refused(lane, now), [
                    Settle(fps, OpResult(False, None,
                                         lw.motion_blocked[unit[0]]))]
        # H16: a ready bay skipped because its unit is positioning is named
        # too, so the user waits for the insertion instead of chasing a
        # calibration or an empty bay.
        for unit in lw.group_bays.get(action.group, ()):
            if lw.ready.get(unit) and unit[0] in lw.positioning:
                return _stage_refused(lane, now), [
                    Settle(fps, OpResult(False, None, _positioning_msg(lw, unit)))]
        # H10: a bay that is ready but has no PTFE length is skipped above.
        # Name it instead of the generic "no ready spool" so the user knows the
        # load was refused for calibration, not for an empty bay.
        for unit in lw.group_bays.get(action.group, ()):
            if lw.ready.get(unit) and lw.calibrated.get(unit) is False:
                return _stage_refused(lane, now), [
                    Settle(fps, OpResult(False, None, _uncalibrated_msg(lw, unit)))]
        return _stage_refused(lane, now), [
            Settle(fps, OpResult(False, None,
                                 "no ready spool in group %s" % action.group))]

    if isinstance(action, LoadBay):
        rejected = _load_guard(lane, lw, fps, unit=action.unit)
        if rejected is not None:
            return _stage_refused(lane, now), rejected
        unit = action.unit
        if not _selectable(lw, unit):
            return _stage_refused(lane, now), [
                Settle(fps, OpResult(False, None,
                        "unit %s has a different locally reserved bay"
                        % (unit[0],)))]
        return _begin_op(
            lane, now, fps, deadline, lambda gen: StartLoad(unit, fps, gen),
            op=OP_LOADING, group=_group_of(lw, unit), unit=unit,
            runout=RUNOUT_IDLE, pause_origin=None, coast_origin=None,
            reload_target=None, runout_from=None, message=None, stage="feed")

    if isinstance(action, Unload):
        if op != OP_LOADED or lane.unit is None:
            return _stage_refused(lane, now), [
                Settle(fps, OpResult(False, None, "nothing loaded"))]
        if lane.runout == RUNOUT_LOADING:
            # A runout auto-reload firmware op is in flight on this lane.
            # Starting another op would bump op_gen (orphaning the reload's
            # completion) and leave that unit feeding unmonitored.
            return _stage_refused(lane, now), [
                Settle(fps, OpResult(False, None,
                        "lane is auto-loading the next spool after a runout;"
                        " wait for it to finish"))]
        unit = lane.unit
        return _begin_op(
            lane, now, fps, deadline, lambda gen: StartUnload(unit, fps, gen),
            op=OP_UNLOADING, runout=RUNOUT_IDLE, pause_origin=None,
            coast_origin=None, reload_target=None, runout_from=None,
            stage="retract")

    if isinstance(action, Calibrate):
        if (op in (OP_LOADING, OP_UNLOADING, OP_CALIBRATING)
                or (op == OP_LOADED and lane.runout == RUNOUT_LOADING)):
            # RUNOUT_LOADING included: calibrating over an in-flight reload
            # would restore op=LOADED with a bumped gen and no deadline,
            # wedging the lane until CLEAR_ERRORS.
            return _stage_refused(lane, now), [
                Settle(fps, OpResult(False, None,
                        "busy, cannot calibrate now"))]
        unit = (action.oams_idx, action.bay)
        return _begin_op(
            lane, now, fps, deadline,
            lambda gen: StartCalibrate(unit, action.kind, fps, gen),
            op=OP_CALIBRATING, prior_op=op, stage="calibrate")

    if isinstance(action, Cancel):
        if op == OP_LOADING and lane.unit is not None:
            # await OpCompleted(CANCEL)
            return lane, [CancelLoad(lane.unit, lane.op_gen)]
        if op == OP_UNLOADING and lane.unit is not None:
            return lane, [CancelUnload(lane.unit, lane.op_gen)]
        return lane, []

    if isinstance(action, UnitSessionLost):
        # The unit's host-communication watchdog fired: the firmware already
        # stopped all motion and dropped the op without a reply. Settle only a
        # lane that was waiting on this unit, and send nothing — the unit no
        # longer listens, so a Start*/CancelLoad/SetFollower would be lost.
        unit = lane.unit
        if op == OP_LOADING and unit is not None and unit[0] == action.oams_idx:
            lane = _stage_op_end(lane, now, False)
            nl = replace(lane, op=OP_UNLOADED, group=None, unit=None,
                         following=False, op_deadline=None,
                         message="load failed", fault_unit=unit,
                         fault_text=action.reason)
            return nl, [CancelDeadline(fps),
                        Settle(fps, OpResult(False, None, action.reason))]
        if (op == OP_UNLOADING and unit is not None
                and unit[0] == action.oams_idx):
            lane = _stage_op_end(lane, now, False)
            nl = replace(lane, op=OP_LOADED, following=False,
                         op_deadline=None, message="unload failed",
                         fault_unit=unit, fault_text=action.reason)
            return nl, [CancelDeadline(fps),
                        Settle(fps, OpResult(False, None, action.reason))]
        if (op == OP_CALIBRATING and unit is not None
                and unit[0] == action.oams_idx):
            lane = _stage_op_end(lane, now, False)
            nl = replace(lane, op=lane.prior_op or OP_UNLOADED, prior_op=None,
                         op_deadline=None, message="calibration failed",
                         fault_unit=unit, fault_text=action.reason)
            return nl, [CancelDeadline(fps),
                        Settle(fps, OpResult(False, None, action.reason))]
        target = lane.reload_target
        if (op == OP_LOADED and lane.runout == RUNOUT_LOADING
                and target is not None and target[0] == action.oams_idx):
            # C2 §5.3 rule 5: keep reload_target and runout_from through the
            # failed reload and the UNLOADED that follows it.
            lane = _stage_op_end(lane, now, False)
            nl = replace(lane, op=OP_UNLOADED, group=None, unit=None,
                         runout=RUNOUT_IDLE,
                         pause_origin=None, coast_origin=None,
                         op_deadline=None, message="reload failed",
                         fault_unit=target, fault_text=action.reason)
            return nl, [CancelDeadline(fps), Pause(fps, action.reason)]
        if (op == OP_LOADED and lane.runout == RUNOUT_IDLE
                and unit is not None and unit[0] == action.oams_idx):
            # The firmware stopped the follower; record that, but send nothing.
            effects = [Pause(fps, action.reason)] if action.printing else []
            return replace(lane, following=False), effects
        return lane, []

    if isinstance(action, Follow):
        if lane.unit is None:
            return lane, []
        nl = replace(lane, following=bool(action.enable),
                     direction=action.direction)
        return nl, [SetFollower(lane.unit, action.enable, action.direction, fps)]

    if isinstance(action, FollowerFailed):
        uncertain = lane.uncertain_follower_units
        if action.unit not in uncertain:
            uncertain += (action.unit,)
        following = (None if lane.unit in (None, action.unit)
                     else lane.following)
        effects = ([Pause(fps, "follower stop unconfirmed during runout")]
                   if lane.runout == RUNOUT_COASTING else [])
        return replace(lane, following=following,
                       uncertain_follower_units=uncertain), effects

    if isinstance(action, FollowerApplied):
        if lane.unit is None and action.following:
            # An unassigned lane cannot claim that an orphan follower belongs
            # to its current path. Keep the unresolved unit visible.
            return lane, []
        uncertain = tuple(unit for unit in lane.uncertain_follower_units
                          if unit != action.unit)
        following = (action.following if lane.unit == action.unit
                     or lane.unit is None and not uncertain
                     else lane.following)
        return replace(lane, following=following,
                       uncertain_follower_units=uncertain), []

    if isinstance(action, OpCompleted):
        if action.gen != lane.op_gen:
            # Stale reply (late after a timeout), a reply from another OAMS
            # unit on this lane, or an unsolicited status (gen=None) — it does
            # not belong to the op in flight.
            return lane, []
        return _complete(lane, action.code, action.value, now, fps,
                         fault=action.fault)

    if isinstance(action, Timeout):
        return _complete(lane, OAMS_OP_CODE_ERROR_UNSPECIFIED, None, now, fps,
                         timed_out=True)

    if isinstance(action, Tick):
        # C2 §5.3 rule 4: a plan with no recorded failure that has not changed
        # for STAGE_STALE_S while the lane sits settled is dropped (a guard
        # against a macro that aborted without announcing DONE). A failed plan
        # is never dropped by time.
        if (lane.stage_plan and lane.stage_failed is None
                and lane.op in (OP_UNLOADED, OP_LOADED)
                and lane.runout == RUNOUT_IDLE
                and now - lane.stage_since > STAGE_STALE_S):
            lane = _stage_clear(lane)
        # Belt-and-braces deadline backing up the runtime's reactor timer in
        # case it was ever lost. For legacy firmware this enforces the 120 s
        # op timeout; once the firmware owns liveness op_deadline is the long
        # disconnect backstop, so this only fires if the MCU went silent.
        if (lane.op_deadline is not None and now > lane.op_deadline
                and _in_flight(lane)):
            return _complete(lane, OAMS_OP_CODE_ERROR_UNSPECIFIED, None, now,
                             fps, timed_out=True)
        if op == OP_LOADED:
            return _runout_tick(lane, lw, now, fps, deadline)
        return lane, []

    return lane, []


def _complete(lane, code, value, now, fps, timed_out=False, fault=None):
    op = lane.op
    ok = (code == OAMS_OP_CODE_SUCCESS)

    # Runout auto-reload completion (op stays LOADED while reloading).
    if op == OP_LOADED and lane.runout == RUNOUT_LOADING:
        lane = _stage_op_end(lane, now, ok)
        if ok:
            # C2 §5.3 rule 5: reload_target and runout_from are kept so the UI
            # can still show "A1 ran out → B2" after the reload.
            nl = replace(lane, unit=lane.reload_target, runout=RUNOUT_IDLE,
                         following=True, direction=FOLLOWER_FORWARD,
                         pause_origin=None, coast_origin=None,
                         op_deadline=None, message="next spool loaded")
            return nl, [CancelDeadline(fps)]
        reason = ("timed out loading next spool" if timed_out
                  else "failed to load next spool (%s)" % describe_code(code, fault))
        nl = replace(lane, op=OP_UNLOADED, group=None, unit=None,
                     runout=RUNOUT_IDLE, pause_origin=None, coast_origin=None,
                     op_deadline=None, message="reload failed",
                     fault_unit=lane.reload_target, fault_text=reason)
        effects = [CancelDeadline(fps)]
        if timed_out and lane.reload_target is not None:
            # The firmware op is still running; tell it to stop feeding.
            effects.append(CancelLoad(lane.reload_target, lane.op_gen))
        effects.append(Pause(fps, reason))
        return nl, effects

    if op == OP_LOADING:
        lane = _stage_op_end(lane, now, ok)
        if ok:
            nl = replace(lane, op=OP_LOADED, op_deadline=None,
                         following=True, direction=FOLLOWER_FORWARD,
                         message="loaded")
            return nl, [CancelDeadline(fps),
                        Settle(fps, OpResult(True, code, "Spool loaded successfully"))]
        if code == OAMS_OP_CODE_CANCEL:
            nl = replace(lane, op=OP_UNLOADED, group=None, unit=None,
                         following=False, op_deadline=None, message="cancelled")
            effects = [CancelDeadline(fps)]
            if lane.following is not False and lane.unit is not None:
                # A follower enabled mid-load would otherwise outlive the lane's
                # knowledge of its unit and become unstoppable via the store.
                effects.append(SetFollower(lane.unit, 0, lane.direction, fps))
            effects.append(Settle(fps, OpResult(False, code,
                                                "Spool loading cancelled")))
            return nl, effects
        if code == OAMS_OP_CODE_ERROR_BUSY:
            # H16: the unit was positioning a newly inserted spool and refused
            # the load at admission; nothing moved, so this is a refusal, not a
            # lane failure. No CancelLoad: no firmware op ever started.
            nl = replace(lane, op=OP_UNLOADED, group=None, unit=None,
                         following=False, op_deadline=None, message=None)
            effects = [CancelDeadline(fps)]
            if lane.following is not False and lane.unit is not None:
                effects.append(SetFollower(lane.unit, 0, lane.direction, fps))
            effects.append(Settle(fps, OpResult(False, code,
                "OAMS is busy (positioning a newly inserted spool?); retry")))
            return nl, effects
        msg = ("timed out loading spool" if timed_out
               else "Spool loading failed (%s)" % describe_code(code, fault))
        nl = replace(lane, op=OP_UNLOADED, group=None, unit=None,
                     following=False, op_deadline=None, message="load failed",
                     fault_unit=lane.unit, fault_text=msg)
        effects = [CancelDeadline(fps)]
        if timed_out and lane.unit is not None:
            # The firmware op is still running; tell it to stop feeding rather
            # than abandoning it mid-load.
            effects.append(CancelLoad(lane.unit, lane.op_gen))
        if lane.following is not False and lane.unit is not None:
            effects.append(SetFollower(lane.unit, 0, lane.direction, fps))
        effects.append(Settle(fps, OpResult(False, code, msg)))
        return nl, effects

    if op == OP_UNLOADING:
        lane = _stage_op_end(lane, now, ok)
        if ok:
            nl = replace(lane, op=OP_UNLOADED, group=None, unit=None,
                         following=False, op_deadline=None, message="unloaded")
            return nl, [CancelDeadline(fps),
                        Settle(fps, OpResult(True, code, "Spool unloaded successfully"))]
        if code == OAMS_OP_CODE_CANCEL:
            # A cancelled unload (the user's Stop) is not a fault: the spool is
            # still loaded, the follower stops, and nothing latches.
            nl = replace(lane, op=OP_LOADED, following=False, op_deadline=None,
                         message="cancelled")
            effects = [CancelDeadline(fps)]
            if lane.unit is not None:
                effects.append(SetFollower(lane.unit, 0, FOLLOWER_REVERSE, fps))
            effects.append(Settle(fps, OpResult(False, code,
                                                "Spool unloading cancelled")))
            return nl, effects
        # Unload failed: still loaded; stop the follower so it can't keep rewinding.
        msg = ("timed out unloading spool" if timed_out
               else "Spool unloading failed (%s)" % describe_code(code, fault))
        nl = replace(lane, op=OP_LOADED, following=False, op_deadline=None,
                     message="unload failed",
                     fault_unit=lane.unit, fault_text=msg)
        effects = [CancelDeadline(fps)]
        if timed_out and lane.unit is not None:
            effects.append(CancelUnload(lane.unit, lane.op_gen))
        if lane.unit is not None:
            effects.append(SetFollower(lane.unit, 0, FOLLOWER_REVERSE, fps))
        effects.append(Settle(fps, OpResult(False, code, msg)))
        return nl, effects

    if op == OP_CALIBRATING:
        lane = _stage_op_end(lane, now, ok)
        target = lane.prior_op or OP_UNLOADED
        msg = ("Calibration complete" if ok
               else ("timed out calibrating" if timed_out
                     else "Calibration failed (%s)" % describe_code(code, fault)))
        fields = {} if ok else {"fault_unit": lane.unit, "fault_text": msg}
        nl = replace(lane, op=target, prior_op=None, op_deadline=None,
                     message=("calibrated" if ok else "calibration failed"),
                     **fields)
        return nl, [CancelDeadline(fps),
                    Settle(fps, OpResult(ok, code, msg, value))]

    # Stray completion with nothing in flight — ignore.
    return lane, []


def _runout_tick(lane, lw, now, fps, deadline):
    r = lane.runout

    if r == RUNOUT_COASTING and lane.following is None:
        # The coasting model assumes the follower stopped. A failed stop leaves
        # that assumption unproven, so do not schedule an automatic reload.
        return lane, []

    if r == RUNOUT_IDLE:
        # `lw.printing` is a real print job. A lane with a flow in progress
        # (an active macro plan: an unload's cut or retract, a toolchange)
        # reads an empty or moving path by design, never a runout.
        if (lw.printing and lane.unit is not None
                and not (_stage_macro_active(lane)
                         and now - lane.stage_since <= STAGE_FLOW_FRESH_S)
                and not lw.loaded.get(lane.unit, False)):
            # C2 §5.3 rule 5: remember the bay the runout started from so a UI
            # can show "A1 ran out → B2" across the reload.
            return replace(lane, runout=RUNOUT_PAUSING,
                           pause_origin=lw.extruder_pos,
                           runout_from=lane.unit), []
        return lane, []

    # Later phases need a loaded unit; bail safely if it vanished.
    if lane.unit is None:
        return replace(lane, runout=RUNOUT_IDLE, pause_origin=None,
                       coast_origin=None, reload_target=None), []

    if r == RUNOUT_PAUSING:
        if lw.extruder_pos - lane.pause_origin >= PAUSE_DISTANCE:
            return (replace(lane, runout=RUNOUT_COASTING,
                            coast_origin=lw.extruder_pos, following=False),
                    [SetFollower(lane.unit, 0, FOLLOWER_FORWARD, fps)])
        return lane, []

    if r == RUNOUT_COASTING:
        path_len = lw.path_len.get(lane.unit[0], 0.0)
        if path_len <= 0:
            reason = ("ptfe_length is not calibrated (0); cannot"
                      " auto-load the next spool. Run"
                      " OAMS_CALIBRATE_PTFE_LENGTH.")
            nl = replace(lane, op=OP_UNLOADED, group=None, unit=None,
                         runout=RUNOUT_IDLE, pause_origin=None,
                         coast_origin=None, message="ptfe_length uncalibrated",
                         fault_unit=lane.unit, fault_text=reason)
            return nl, [Pause(fps, reason)]
        consumed = lw.extruder_pos - lane.coast_origin
        if consumed + PAUSE_DISTANCE + lw.reload_before > path_len:
            # C2 §5.2: with settings.auto_reload_on_runout off, the point where
            # a reload would otherwise start (the spare-spool selection) is a
            # pause instead.
            if not lw.auto_reload:
                reason = ("filament runout on group %s"
                          " (auto reload is off)" % lane.group)
                nl = replace(lane, op=OP_UNLOADED, group=None, unit=None,
                             runout=RUNOUT_IDLE, pause_origin=None,
                             coast_origin=None, message="auto reload off",
                             fault_unit=lane.unit, fault_text=reason)
                return nl, [Pause(fps, reason)]
            # H10: _group_candidates already drops uncalibrated bays, so the
            # reload target chosen here is always calibrated.
            for unit in _group_candidates(lw, lane.group):
                if unit == lane.unit:
                    continue  # never reload the bay that just ran out
                if lw.ready.get(unit):
                    # C2 §5.3 rule 2: a runout reload always gets the
                    # automatic plan, replacing any (possibly stale) macro
                    # plan left on the lane .
                    return _begin_op(
                        _stage_clear(lane), now, fps, deadline,
                        lambda gen, unit=unit: StartLoad(unit, fps, gen),
                        runout=RUNOUT_LOADING, reload_target=unit,
                        stage="feed")
            # H16: a ready spare whose unit is positioning is not a dead end;
            # keep coasting and re-evaluate on the next tick.
            for unit in lw.group_bays.get(lane.group, ()):
                if (unit != lane.unit and lw.ready.get(unit)
                        and unit[0] in lw.positioning):
                    return lane, []
            # A ready spare whose unit must not be loaded (firmware without
            # filament motion, or a unit that needs host arrival
            # confirmation on a lane with no arrival signal) is named with
            # its own reason, not reported as an empty group.
            for unit in lw.group_bays.get(lane.group, ()):
                if (unit != lane.unit and lw.ready.get(unit)
                        and unit[0] in lw.motion_blocked):
                    reason = ("filament runout on group %s and the spare"
                              " spool cannot be loaded: %s"
                              % (lane.group, lw.motion_blocked[unit[0]]))
                    nl = replace(lane, op=OP_UNLOADED, group=None, unit=None,
                                 runout=RUNOUT_IDLE, pause_origin=None,
                                 coast_origin=None, message="no spare spool",
                                 fault_unit=unit, fault_text=reason)
                    return nl, [Pause(fps, reason)]
            # A ready spare that cannot be loaded because it has no PTFE
            # length is a different problem from an empty group: say so.
            for unit in lw.group_bays.get(lane.group, ()):
                if (unit != lane.unit and lw.ready.get(unit)
                        and lw.calibrated.get(unit) is False):
                    reason = ("filament runout on group %s and the"
                              " spare spool is not calibrated (%s); run"
                              " OAMS_CALIBRATE_PTFE_LENGTH SPOOL=%d."
                              % (lane.group,
                                 spool_name(_unit_label(lw, unit[0]), unit[1]),
                                 unit[1]))
                    nl = replace(lane, op=OP_UNLOADED, group=None, unit=None,
                                 runout=RUNOUT_IDLE, pause_origin=None,
                                 coast_origin=None,
                                 message="spare spool is not calibrated",
                                 fault_unit=unit, fault_text=reason)
                    return nl, [Pause(fps, reason)]
            reason = ("filament runout on group %s and no spare"
                      " spool available" % lane.group)
            nl = replace(lane, op=OP_UNLOADED, group=None, unit=None,
                         runout=RUNOUT_IDLE, pause_origin=None,
                         coast_origin=None, message="no spare spool",
                         fault_unit=lane.unit, fault_text=reason)
            return nl, [Pause(fps, reason)]
        return lane, []

    # RUNOUT_LOADING: waiting on OpCompleted/Timeout (handled in _complete).
    return lane, []
