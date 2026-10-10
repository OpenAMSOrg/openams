# SPDX-License-Identifier: GPL-3.0-or-later
"""Apply a later physical follower observation to the OpenAMS runtime.

The caller obtains ``before`` from the device before issuing the command and
calls this on the reactor thread after a separately scheduled physical refresh.
This bridge does no transport work and never treats command admission as motor
state. A missing or ambiguous capture leaves the runtime unchanged.
"""

from .openams_device import OpenAMSDevice
from .openams_device_state import FollowerObservation
from .openams_follow_bridge import FollowCommand
from .protocol import Admission, Identity
from .requests import FOLLOW, HOST, LOAD
from .wire import AdmissionResult, Direction, Fault, known


class OpenAMSVerifiedFollowerBridge:
    def __init__(self, device, runtime):
        if (not isinstance(device, OpenAMSDevice)
                or not callable(getattr(runtime, 'accept_verified_follower', None))):
            raise ValueError('OpenAMS device and runtime follower receiver required')
        self.device = device
        self.runtime = runtime

    def accept(self, command, before, unit, enabled):
        """Return whether runtime accepted a proven post-command motor state."""
        device = self.device
        if (not isinstance(command, FollowCommand)
                or command.follower_confirmed is not False
                or not isinstance(before, FollowerObservation)
                or type(enabled) is not bool
                or type(unit) is not tuple or len(unit) != 2
                or type(unit[0]) is not int or unit[0] != device.oams_idx
                or type(unit[1]) is not int
                or not 0 <= unit[1] < device.bay_count
                or not isinstance(command.identity, Identity)
                or not isinstance(command.response, Admission)
                or command.identity != command.response.identity
                or command.response.admission not in (
                    AdmissionResult.ACCEPTED,
                    AdmissionResult.ALREADY_IN_PROGRESS)
                or command.response.fault != Fault.NONE
                or command.response.instance != before.instance
                or command.identity.client != HOST
                or command.identity.operation not in ((FOLLOW,) if enabled
                                                      else (LOAD, FOLLOW))
                or command.identity.lane != unit[1]
                or before.bay != unit[1]
                or type(before.query) is not int or before.query <= 0):
            return False
        after = device.follower_observation()
        if (not isinstance(after, FollowerObservation)
                or after.instance != before.instance
                or after.generation != before.generation
                or type(after.query) is not int or after.query <= before.query
                or after.enabled is not enabled):
            return False
        if enabled:
            # Reconciliation binds the armed motor to the exact request and bay.
            if (type(command.direction) is not int
                    or command.direction not in known(Direction)
                    or after.direction != command.direction
                    or after.bay != unit[1]
                    or device.owner_identity() != command.identity):
                return False
        elif after.bay is not None and after.bay != unit[1]:
            return False
        return self.runtime.accept_verified_follower(device.fps_name, unit,
                                                     enabled) is True
