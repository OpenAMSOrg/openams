# SPDX-License-Identifier: GPL-3.0-or-later
"""Physical state cache for one configured OpenAMS-facing MMU unit.

Only one retained MCU capture may populate this cache. The caller supplies a
connection-generation check from the transport and serializes observations on
Klipper's reactor. This class does not grant motion or implement a device.
"""

import math
from dataclasses import dataclass

from .openams_sensor_projection import project_sensor_presence
from .physical_ownership import reconcile_path_ownership
from .protocol import Identity, SensorSnapshot, Snapshot
from .requests import FOLLOW, HOST, LOAD
from .wire import Direction, Fault, FollowerState, OpState, Stage, known


@dataclass(frozen=True)
class FollowerObservation:
    enabled: bool
    direction: object  # 0/1 when armed; None when disarmed.
    bay: object  # Reconciled occupant, or None when the path is empty.
    instance: int
    generation: int
    query: int


class OpenAMSDeviceState:
    def __init__(self, oams_idx, fps_name, device_family, bay_count,
                 path_length_mm):
        if (type(oams_idx) is not int or oams_idx < 0
                or type(fps_name) is not str or not fps_name.strip()
                or type(device_family) is not str or not device_family.strip()
                or type(bay_count) is not int or not 1 <= bay_count <= 12
                or type(path_length_mm) not in (int, float)
                or not math.isfinite(path_length_mm) or path_length_mm <= 0):
            raise ValueError('Configured MMU identity, topology and path length required')
        self.oams_idx = oams_idx
        self.fps_name = fps_name
        self.device_family = device_family
        self.bay_count = bay_count
        self.path_length_mm = float(path_length_mm)
        self.disconnect()

    def _clear_readings(self):
        self.hardware_snapshot_ready = False
        self.f1s_hes_value = None
        self.load_present = None
        self.hub_hes_value = None
        self.owner_identity = None
        self.local_reserved_lane = None
        self.follower_observation = None

    def disconnect(self):
        self._instance = None
        self._generation = None
        self._last_query = 0
        self._clear_readings()

    def begin_connection(self, instance, generation):
        if (type(instance) is not int or not 0 < instance < 1 << 64
                or type(generation) is not int or generation < 0):
            raise ValueError('Valid MCU instance and connection generation required')
        self.disconnect()
        self._instance = instance
        self._generation = generation

    def observe(self, status, sensors, generation, current, virtual_hub=False):
        """Publish a same-capture view, or leave hardware readiness false.

        A callback from an older generation cannot clear a newer connection.
        `current` must be checked by the caller immediately before this call.
        virtual_hub: see project_sensor_presence.
        """
        if type(generation) is not int or type(current) is not bool:
            raise ValueError('Connection generation and currentness required')
        if generation != self._generation or self._instance is None:
            return False
        if not current:
            self.disconnect()
            return False
        self._clear_readings()
        if (not isinstance(status, Snapshot)
                or not isinstance(sensors, SensorSnapshot)
                or status.instance != self._instance
                or sensors.instance != self._instance
                or type(status.query) is not int
                or status.query <= self._last_query):
            return False
        self._last_query = status.query
        presence = project_sensor_presence(sensors, self._instance, self.bay_count,
                                           virtual_hub=virtual_hub)
        if presence is None:
            return False
        try:
            ownership = reconcile_path_ownership(status, presence, virtual_hub=virtual_hub)
        except ValueError:
            return False
        if ownership.kind == 'unknown':
            return False
        self.f1s_hes_value = presence.bay_present
        self.load_present = presence.load_present
        self.local_reserved_lane = (ownership.lane
                                    if ownership.kind in ('local', 'staging')
                                    else None)
        self.hub_hes_value = tuple(ownership.kind == 'known' and
                                   bay == ownership.lane
                                   for bay in range(self.bay_count))
        values = status.values
        session = values.get('session')
        request = values.get('request')
        sessions = values.get('client_sessions')
        requests = values.get('client_requests')
        if (ownership.kind in ('known', 'feeding') and values['lane_active']
                and values['owner_lane'] == ownership.lane
                and type(values.get('client')) is int
                and values['client'] == HOST
                and type(values.get('operation')) is int
                and values['operation'] in (LOAD, FOLLOW)
                and type(values.get('lane')) is int
                and values['lane'] == ownership.lane
                and type(session) is int and 0 < session < 1 << 32
                and type(request) is int and 0 < request < 1 << 32
                and type(sessions) in (tuple, list) and len(sessions) == 4
                and type(requests) in (tuple, list) and len(requests) == 4
                and type(sessions[HOST]) is int and sessions[HOST] == session
                and type(requests[HOST]) is int
                and request <= requests[HOST] < 1 << 32):
            self.owner_identity = Identity(HOST, session, request,
                                           values['operation'], ownership.lane)
        follower_state = values.get('follower_state')
        follower_direction = values.get('follower_direction')
        follower_fault = values.get('follower_fault')
        if (type(follower_state) is int
                and follower_state in (FollowerState.DISARMED,
                                       FollowerState.IDLE,
                                       FollowerState.DRIVING)
                and type(follower_direction) is int
                and follower_direction in known(Direction)
                and type(follower_fault) is int
                and follower_fault == Fault.NONE):
            bay = ownership.lane if ownership.kind == 'known' else None
            if follower_state == FollowerState.DISARMED:
                self.follower_observation = FollowerObservation(
                    False, None, bay, self._instance, generation, status.query)
            elif (self.owner_identity is not None
                  and type(values.get('stage')) is int
                  and values['stage'] == Stage.FOLLOW
                  and type(values.get('state')) is int
                  and values['state'] == OpState.FOLLOWING):
                # Armed idle is still active following; output pulses are not
                # required. An exact HOST LOAD owner covers automatic following
                # after load; stage and state distinguish it from LOAD admission.
                self.follower_observation = FollowerObservation(
                    True, follower_direction, bay, self._instance,
                    generation, status.query)
        self.hardware_snapshot_ready = True
        return True
