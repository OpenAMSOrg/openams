# SPDX-License-Identifier: GPL-3.0-or-later
"""Conservative shared-path attribution from status and physical presence.

Firmware retains status and sensor views together for one query ID. The
caller must guard connection generation and obtain a current observation;
this decision is not motion permission and cannot resolve an unknown source.
A lane's BAY reading may be None (no per-lane switch at all); it is then
tolerated exactly like an unconfigured LOAD reading -- never treated as a
real switch reporting the lane empty.
"""

from dataclasses import dataclass
from typing import Optional

from .openams_sensor_projection import SensorPresence
from .protocol import Snapshot
from .wire import Client, Fault, OpState, OpType, Stage


@dataclass(frozen=True)
class PathOwnership:
    kind: str  # 'known', 'local', 'staging', 'feeding', 'retracting', 'empty' or 'unknown'
    lane: Optional[int] = None


def reconcile_path_ownership(status, sensors, virtual_hub=False):
    """Return a known occupant only when logical and physical facts agree.

    A retained path_occupied flag can outlive an absent switch signal. A shared
    hub signal alone cannot identify which bay supplied the filament. Unknown
    results must keep OpenAMS hardware readiness false until reconciled.

    virtual_hub: see project_sensor_presence. It also flips what a settled
    PRELOAD's own LOAD switch must read (below): a real separate-hub device
    parks just behind that switch, so it stays made; a virtual-hub device's
    LOAD switch is the shared boundary PRELOAD must clear behind, so it reads
    empty instead once settled.
    """
    if (not isinstance(status, Snapshot)
            or not isinstance(sensors, SensorPresence)
            or status.instance != sensors.instance
            or type(status.instance) is not int
            or type(status.query) is not int
            or type(sensors.query) is not int
            or not 0 < status.query < 1 << 32
            or not 0 < sensors.query < 1 << 32
            or type(sensors.observed_ms) is not int
            or not 0 <= sensors.observed_ms < 1 << 32
            or type(status.values) is not dict
            or type(sensors.bay_present) is not tuple
            or not 0 < len(sensors.bay_present) <= 12
            or any(type(present) not in (bool, type(None))
                   for present in sensors.bay_present)
            or type(sensors.load_present) is not tuple
            or len(sensors.load_present) < len(sensors.bay_present)
            or any(type(present) not in (bool, type(None)) for present in
                   sensors.load_present[:len(sensors.bay_present)])
            or type(sensors.shared_hub_present) is not bool
            or type(sensors.toolhead_present) not in (bool, type(None))):
        raise ValueError('Invalid same-instance path observations')
    values = status.values
    for name in ('path_occupied', 'lane_active', 'terminal_pending',
                 'position_uncertain', 'pending_fault_event'):
        if type(values.get(name)) is not bool:
            raise ValueError('Malformed status flag %s' % name)
    for name in ('owner_lane', 'occupant_lane', 'system_fault', 'motion_fault'):
        if type(values.get(name)) is not int:
            raise ValueError('Malformed status field %s' % name)
    if (type(values.get('acquired_ms')) is not int
            or not 0 <= values['acquired_ms'] < 1 << 32):
        raise ValueError('Malformed status acquisition time')

    unknown = PathOwnership('unknown')
    if (status.query != sensors.query
            or values['acquired_ms'] != sensors.observed_ms):
        return unknown
    if (values['position_uncertain'] or values['pending_fault_event']
            or values['system_fault'] != Fault.NONE
            or values['motion_fault'] != Fault.NONE):
        return unknown
    if values['terminal_pending']:
        sessions = values.get('client_sessions')
        requests = values.get('client_requests')
        lane = values.get('lane')
        session = values.get('session')
        request = values.get('request')
        if (not values['lane_active'] or type(values.get('client')) is not int
                or values['client'] != Client.HOST
                or type(lane) is not int or lane != values['owner_lane']
                or not 0 <= lane < len(sensors.bay_present)
                or type(session) is not int or not 0 < session < 1 << 32
                or type(request) is not int or not 0 < request < 1 << 32
                or type(sessions) not in (tuple, list) or len(sessions) != 4
                or type(requests) not in (tuple, list) or len(requests) != 4
                or type(sessions[Client.HOST]) is not int
                or type(requests[Client.HOST]) is not int
                or sessions[Client.HOST] != session
                or requests[Client.HOST] != request):
            return unknown
    # Without a toolhead switch the firmware also relies on HUB alone.
    shared_present = (sensors.shared_hub_present
                      or sensors.toolhead_present is True)
    # A step-estimated PTFE calibration feeds and retracts like LOAD/UNLOAD.
    operation = (values.get('state'), values.get('operation'))
    calibrating = operation == (OpState.CALIBRATING, OpType.CALIBRATE)
    lane = values['owner_lane']
    # A shared-drive selector positions its gate and grips before any lane
    # can reach the shared path at all (selector.h's SELECT_HOME/SELECT_MOVE/
    # SERVO_DWELL precede every operation's own feed/retract stages). Like
    # PRELOAD, the operation owns the lane while the shared path stays
    # untouched; which operation it will go on to run does not matter yet.
    if (values['terminal_pending'] and not shared_present
            and values.get('stage') in (Stage.SELECT_HOME, Stage.SELECT_MOVE,
                                        Stage.SERVO_DWELL)
            and (not values['path_occupied'] or values['occupant_lane'] == lane)
            and 0 <= lane < len(sensors.bay_present)):
        return PathOwnership('staging', lane)
    # Feeding toward HUB, including after a LOAD retry backed out of it. A
    # LOAD switch is an optional extra assurance (some shared-drive selector
    # families have only a per-gate BAY switch and a shared HUB/gear sensor,
    # per their own dossier); an unconfigured reading never blocks this, only
    # a real switch reporting the lane empty does. A BAY switch is optional
    # the same way (a device with none at all has nothing to contradict this).
    # A virtual hub's own LOAD switch is the boundary being fed toward, so it
    # reads empty all the way there (and `not shared_present` already implies
    # it: a virtual hub's shared reading is any() of the LOAD switches).
    if (values['terminal_pending'] and not shared_present
            and (operation == (OpState.LOADING, OpType.LOAD) or calibrating)
            and values.get('stage') in (Stage.LOAD_FEED_TO_HUB,
                                        Stage.LOAD_RETRY_RETRACT)
            and (not values['path_occupied'] or values['occupant_lane'] == lane)
            and sensors.bay_present[lane] is not False
            and (sensors.load_present[lane] is not True if virtual_hub
                 else sensors.load_present[lane] is not False)
            and all(present is not True or sensors.bay_present[index] is not False
                    for index, present in enumerate(
                        sensors.load_present[:len(sensors.bay_present)]))):
        return PathOwnership('feeding', lane)
    # PRELOAD, UNPRELOAD and EJECT move filament inside the owning lane only:
    # while the HOST operation runs the shared path stays clear and the lane
    # is taken, before and after its LOAD switch marks the lane's own path
    # occupied. UNPRELOAD reports UNLOADING/UNLOAD like a full unload, so it
    # is told apart by its stage: the retract to LOAD, then braking with LOAD
    # already clear (a full unload still brakes with LOAD occupied and stays
    # 'retracting'). Without this, every refresh during their travel read
    # 'unknown' and OpenAMS dropped the session mid-operation.
    stage = values.get('stage')
    unpreload = (operation == (OpState.UNLOADING, OpType.UNLOAD)
                 and 0 <= lane < len(sensors.bay_present)
                 and (stage == Stage.UNLOAD_RETRACT_TO_LOAD
                      or (stage == Stage.BRAKING
                          and sensors.load_present[lane] is False)))
    if (values['terminal_pending'] and not shared_present
            and (not values['path_occupied'] or values['occupant_lane'] == lane)
            and (operation in ((OpState.PRELOADING, OpType.PRELOAD),
                               (OpState.EJECTING, OpType.EJECT)) or unpreload)
            and 0 <= lane < len(sensors.bay_present)):
        return PathOwnership('staging', lane)
    if not values['path_occupied']:
        # A lane's LOAD switch can remain occupied by staged filament while
        # the shared hub and toolhead path is empty. Require its BAY switch to
        # agree, but do not treat staged filament as shared-path ownership.
        if (not values['lane_active'] and not shared_present
                and not any(present is True and sensors.bay_present[lane] is False
                            for lane, present in enumerate(
                                sensors.load_present[:len(sensors.bay_present)]))):
            return PathOwnership('empty')
        return unknown

    lane = values['occupant_lane']
    # A settled PRELOAD rests either on a LOAD switch (a real per-lane switch
    # reads True there, every other reads False) or, for a device with no
    # per-lane LOAD switch at all (every reading None -- no LOAD-switch
    # evidence whatsoever, per firmware's preload_rests_behind_hub()), behind
    # the shared HUB instead. A None mixed among real readings proves
    # nothing and must not grant this on its own.
    if (not shared_present and type(values.get('state')) is int
            and values['state'] == OpState.PRELOADED
            and type(values.get('stage')) is int
            and values['stage'] == Stage.STOPPED
            and type(values.get('operation')) is int
            and values['operation'] == OpType.NONE
            and not values['lane_active']
            and 0 <= lane < len(sensors.bay_present)
            and sensors.bay_present[lane] is not False
            and type(sensors.load_present) is tuple
            and len(sensors.load_present) >= len(sensors.bay_present)
            and (all(present is None for present in
                     sensors.load_present[:len(sensors.bay_present)])
                 or (sensors.load_present[lane] is (False if virtual_hub else True)
                     and all(present is False for index, present in
                             enumerate(sensors.load_present[:len(sensors.bay_present)])
                             if index != lane)))):
        return PathOwnership('local', lane)
    # A device with no per-lane LOAD switch at all (every reading None, same
    # QIDI-shaped case as the 'local' rest above) has no intermediate switch
    # between BAY and the shared HUB/toolhead: one continuous UNLOAD retract
    # clears BAY on its way past HUB and toolhead, before firmware's own
    # terminal settles. A None mixed among real readings still proves
    # nothing on its own, so this only relaxes BAY when every reading is
    # unconfigured, never when a real switch elsewhere reports empty.
    no_load_switches = all(present is None for present in
                           sensors.load_present[:len(sensors.bay_present)])
    # Retracting to and past HUB. A real separate-hub device's own LOAD
    # switch stays made or unconfigured through this travel; a virtual-hub
    # device's LOAD switch is the HUB, so with the shared path clear it reads
    # empty in every one of these stages.
    past_hub_stages = (Stage.UNLOAD_REWIND_TO_SPOOL, Stage.UNLOAD_STOP_SETTLE,
                       Stage.BRAKING)
    if (not shared_present and values['terminal_pending']
            and (operation == (OpState.UNLOADING, OpType.UNLOAD) or calibrating)
            and stage in (Stage.UNLOAD_RETRACT_TO_HUB,) + past_hub_stages
                + (Stage.CALIBRATE_RETRACT,)
            and 0 <= lane < len(sensors.bay_present)
            and values['owner_lane'] == lane
            and (sensors.bay_present[lane] is not False or no_load_switches)
            and (sensors.load_present[lane] is False if virtual_hub
                 else sensors.load_present[lane] is not False)):
        # HUB may clear before the configured extra retract finishes. Firmware
        # retains the reservation; do not publish a loaded hub or empty path.
        # An unconfigured LOAD switch (no reading at all) never blocks this,
        # same as the feeding branch above.
        return PathOwnership('retracting', lane)
    if (not 0 <= lane < len(sensors.bay_present)
            or sensors.bay_present[lane] is False or not shared_present):
        return unknown
    if (values.get('state') == OpState.PRELOADED  # lane-local
            and any(present is not None for present in
                    sensors.load_present[:len(sensors.bay_present)])):
        return unknown
    if values['lane_active'] and values['owner_lane'] != lane:
        return unknown
    return PathOwnership('known', lane)
