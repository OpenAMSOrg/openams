# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only facts an OpenAMS device adapter may take from Klipper-MMU.

The present status wire has no per-bay sensor validity or calibrated route
length. Keep OpenAMS hardware readiness false until those facts are published
and reconciled; a path occupancy flag is not a per-bay sensor reading.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class OpenAMSObservation:
    instance: int
    query: int
    state: int
    operation: int
    lane_active: bool
    path_occupied: bool
    system_fault: int
    motion_fault: int


def observe_openams_device(status):
    """Return a coherent cached diagnostic, or None after loss of identity.

    This consumes KlipperMMU.get_status(), which is a cached read and carries
    no freshness or connection-generation proof. Callers must obtain a fresh
    transport and snapshot observation before making any external decision.
    It never grants motion or declares OpenAMS hardware_snapshot_ready.
    """
    if type(status) is not dict:
        raise ValueError('Invalid Klipper-MMU status')
    if status.get('connected') is not True or status.get('state') != 'attached':
        return None
    instance = status.get('instance')
    snap = status.get('snapshot')
    if type(instance) is not int or not 0 < instance < 1 << 64 or type(snap) is not dict:
        return None
    if type(snap.get('instance')) is not int or snap['instance'] != instance:
        return None
    query = snap.get('query')
    if (type(query) is not int or not 0 < query < 1 << 32
            or type(status.get('last_query')) is not int
            or status['last_query'] != query):
        return None
    fields = ('state', 'operation', 'system_fault', 'motion_fault')
    if any(type(snap.get(key)) is not int for key in fields):
        return None
    flags = ('lane_active', 'path_occupied')
    if any(type(snap.get(key)) is not bool for key in flags):
        return None
    return OpenAMSObservation(instance, query, snap['state'], snap['operation'],
                              snap['lane_active'], snap['path_occupied'],
                              snap['system_fault'], snap['motion_fault'])
