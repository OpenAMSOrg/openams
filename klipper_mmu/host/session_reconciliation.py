# SPDX-License-Identifier: GPL-3.0-or-later
"""Select fresh host counters from one coherent, observed MMU snapshot.

This is a conservative eligibility check, not a session handshake or motion
permission. The caller must obtain a fresh snapshot for the same observed MCU
instance and connection generation, then recheck firmware admission. Snapshot
age, hardware readiness and ownership can change immediately after capture.
"""

from dataclasses import dataclass

from .openams_sensor_projection import project_sensor_presence
from .physical_ownership import reconcile_path_ownership
from .protocol import Snapshot
from .wire import Client, Fault, OpState, OpType, Stage


@dataclass(frozen=True)
class SessionStart:
    instance: int
    snapshot_query: int
    session: int
    last_request: int


@dataclass(frozen=True)
class OccupiedSessionStart:
    session_start: SessionStart
    lane: int


def choose_host_session(snapshot, instance):
    """Return a new HOST session and zero request high-water, or reject.

    This helper cannot settle abandoned waiters or inspect retained history.
    The caller must do both before it permits any operation to use the result.
    """
    if (not isinstance(snapshot, Snapshot) or type(instance) is not int
            or not 0 < instance < 1 << 64 or snapshot.instance != instance
            or type(snapshot.query) is not int or not 0 < snapshot.query < 1 << 32
            or type(snapshot.values) is not dict):
        raise ValueError('Invalid same-instance snapshot')
    values = snapshot.values
    for name in ('state', 'stage', 'operation', 'client', 'queued_events',
                 'head_event_seq', 'system_fault', 'motion_fault', 'velocity'):
        if type(values.get(name)) is not int:
            raise ValueError('Malformed snapshot field %s' % name)
    for name in ('lane_active', 'terminal_pending', 'path_occupied',
                 'position_uncertain', 'pending_fault_event'):
        if type(values.get(name)) is not bool:
            raise ValueError('Malformed snapshot flag %s' % name)
    sessions = values.get('client_sessions')
    requests = values.get('client_requests')
    if (type(sessions) not in (tuple, list) or len(sessions) != 4
            or type(requests) not in (tuple, list) or len(requests) != 4
            or any(type(v) is not int or not 0 <= v < 1 << 32
                   for v in (*sessions, *requests))):
        raise ValueError('Malformed snapshot client counters')
    if (values['state'] != OpState.IDLE or values['stage'] != Stage.STOPPED
            or values['operation'] != OpType.NONE
            or values['client'] != Client.NONE
            or values['queued_events'] != 0 or values['head_event_seq'] != 0
            or values['system_fault'] != Fault.NONE
            or values['motion_fault'] != Fault.NONE
            or values['velocity'] != 0
            or any(values[name] for name in (
                'lane_active', 'terminal_pending', 'path_occupied',
                'position_uncertain', 'pending_fault_event'))):
        raise ValueError('MMU snapshot requires physical or event reconciliation')
    previous = sessions[Client.HOST]
    if previous == 0xffffffff:
        raise OverflowError('Host session IDs exhausted')
    return SessionStart(instance, snapshot.query, previous + 1, 0)


def choose_host_session_with_physical(status, sensors, instance, expected_bays,
                                      virtual_hub=False):
    """Choose a HOST session only when one current query proves an empty path.

    The caller must guard the transport connection generation, obtain a fresh
    same-instance status and sensor query, settle abandoned waiters and inspect
    retained history, then recheck firmware admission before using the result.
    This check is neither a session handshake nor motion permission.

    virtual_hub: see project_sensor_presence.
    """
    selected = choose_host_session(status, instance)
    presence = project_sensor_presence(sensors, instance, expected_bays,
                                       virtual_hub=virtual_hub)
    if presence is None:
        raise ValueError('Required physical sensors are unknown')
    if reconcile_path_ownership(status, presence, virtual_hub=virtual_hub).kind != 'empty':
        raise ValueError('Physical path is not proven empty')
    return selected


def choose_occupied_host_session(status, sensors, instance, expected_bays,
                                 virtual_hub=False):
    """Select a new HOST session for a proven, quiescent LOADED path.

    This is an eligibility check only. The caller still owns freshness and
    connection-generation guards, waiter/history reconciliation, and a final
    firmware admission check before using the selected session.
    """
    return _choose_reserved_host_session(status, sensors, instance,
                                         expected_bays, OpState.LOADED, 'known',
                                         virtual_hub)


def choose_preloaded_host_session(status, sensors, instance, expected_bays,
                                  virtual_hub=False):
    """Select a new HOST session for one stopped, physically proven PRELOAD.

    The caller must first settle old waiters/ACKs and exclude an active lease.
    This only selects counters; the caller must confirm a heartbeat and recheck
    admission before motion. The returned OccupiedSessionStart carries a lane
    reservation; it does not imply filament occupies the shared hub.
    """
    return _choose_reserved_host_session(status, sensors, instance,
                                         expected_bays, OpState.PRELOADED,
                                         'local', virtual_hub)


def _choose_reserved_host_session(status, sensors, instance, expected_bays,
                                  required_state, required_kind,
                                  virtual_hub=False):
    if (not isinstance(status, Snapshot) or type(instance) is not int
            or not 0 < instance < 1 << 64 or status.instance != instance
            or type(status.query) is not int or not 0 < status.query < 1 << 32
            or type(status.values) is not dict):
        raise ValueError('Invalid same-instance snapshot')
    values = status.values
    for name in ('state', 'stage', 'operation', 'client', 'queued_events',
                 'head_event_seq', 'system_fault', 'motion_fault', 'velocity'):
        if type(values.get(name)) is not int:
            raise ValueError('Malformed snapshot field %s' % name)
    for name in ('lane_active', 'terminal_pending', 'path_occupied',
                 'position_uncertain', 'pending_fault_event'):
        if type(values.get(name)) is not bool:
            raise ValueError('Malformed snapshot flag %s' % name)
    for name, limit in (('session', 1 << 32), ('request', 1 << 32),
                        ('lane', 1 << 8), ('owner_lane', 1 << 8),
                        ('occupant_lane', 1 << 8)):
        if type(values.get(name)) is not int or not 0 <= values[name] < limit:
            raise ValueError('Malformed snapshot field %s' % name)
    sessions = values.get('client_sessions')
    requests = values.get('client_requests')
    if (type(sessions) not in (tuple, list) or len(sessions) != 4
            or type(requests) not in (tuple, list) or len(requests) != 4
            or any(type(v) is not int or not 0 <= v < 1 << 32
                   for v in (*sessions, *requests))):
        raise ValueError('Malformed snapshot client counters')
    if (values['state'] != required_state or values['stage'] != Stage.STOPPED
            or values['operation'] != OpType.NONE
            or values['client'] != Client.NONE
            or values['queued_events'] != 0 or values['head_event_seq'] != 0
            or values['system_fault'] != Fault.NONE
            or values['motion_fault'] != Fault.NONE
            or values['velocity'] != 0 or not values['path_occupied']
            or any(values[name] for name in (
                'lane_active', 'terminal_pending', 'position_uncertain',
                'pending_fault_event'))):
        raise ValueError('MMU snapshot is not quiescent and occupied')
    presence = project_sensor_presence(sensors, instance, expected_bays,
                                       virtual_hub=virtual_hub)
    if presence is None:
        raise ValueError('Required physical sensors are unknown')
    ownership = reconcile_path_ownership(status, presence, virtual_hub=virtual_hub)
    if ownership.kind != required_kind:
        raise ValueError('Reserved lane is not physically proven')
    previous = sessions[Client.HOST]
    if previous == 0xffffffff:
        raise OverflowError('Host session IDs exhausted')
    return OccupiedSessionStart(
        SessionStart(instance, status.query, previous + 1, 0), ownership.lane)
