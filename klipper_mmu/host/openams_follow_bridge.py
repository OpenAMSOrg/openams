# SPDX-License-Identifier: GPL-3.0-or-later
"""Guarded OpenAMS set_oams_follower callback for one MMU path.

Call on the reactor thread with serialized command responses. The caller must
derive owner_identity and current_occupant from a fresh, reconciled physical
capture. The bridge cannot turn a stale status into permission. An admission
only acknowledges the request; a later status/event must prove the transition.
Automatic post-load following is owned by the completed LOAD identity, so a
disable targets that exact identity. There is no OpenAMS follower waiter.
"""

from dataclasses import dataclass

from .command_transport import send_operation
from .protocol import Admission, CommandResult, Identity
from .requests import FOLLOW, HOST, LOAD, Requests
from .wire import AdmissionResult, Command, Fault


@dataclass(frozen=True)
class FollowCommand:
    identity: Identity
    response: Admission
    # Admission confirms only that the request was accepted, not motor state.
    follower_confirmed: bool = False
    # The requested direction must agree with later controller truth.
    direction: object = None


class OpenAMSFollowCommandBridge:
    """Per-unit callback with OpenAMS's set_oams_follower(enable, direction)."""

    def __init__(self, mcu, reactor, requests, owner_identity=None,
                 current_occupant=None, is_current=None, motion_permitted=None,
                 bay_count=1, timeout=5.0):
        if (not isinstance(requests, Requests)
                or not callable(owner_identity)
                or not callable(current_occupant)
                or not callable(is_current)
                or type(bay_count) is not int or not 1 <= bay_count <= 12
                or type(timeout) not in (int, float) or not 0 < timeout <= 60):
            raise ValueError('Invalid OpenAMS follower bridge')
        self.mcu, self.reactor, self.requests = mcu, reactor, requests
        self.owner_identity = owner_identity
        self.current_occupant = current_occupant
        self.is_current = is_current
        self.motion_permitted = motion_permitted
        self.bay_count, self.timeout = bay_count, timeout
        self.instance, self.generation = requests.instance, requests.generation

    def _owner(self):
        lane = self.current_occupant()
        owner = self.owner_identity()
        if (type(lane) is not int or not 0 <= lane < self.bay_count
                or not isinstance(owner, Identity)
                or owner.client != HOST or owner.session != self.requests.session
                or type(owner.request) is not int
                or not 0 < owner.request <= self.requests.last_request
                or owner.operation not in (LOAD, FOLLOW) or owner.lane != lane):
            raise ValueError('Reconciled MMU follower ownership is unavailable')
        return owner

    def set_oams_follower(self, enable, direction):
        """Issue FOLLOW START or exact-owner CANCEL; return admission only.

        Disable can stop automatic post-load following without issuing a new
        request. It cannot confirm that the motor stopped; callers must observe
        the resulting firmware status/event and retain fault handling.
        """
        if type(enable) is not int or enable not in (0, 1):
            raise ValueError('Invalid follower enable')
        if type(direction) is not int or direction not in (0, 1):
            raise ValueError('Invalid follower direction')
        requests = self.requests
        if (not requests.connected or requests.instance != self.instance
                or requests.generation != self.generation
                or self.is_current() is not True):
            raise ValueError('MMU follower connection is unavailable')
        if enable and (not callable(self.motion_permitted)
                       or self.motion_permitted() is not True):
            raise ValueError('MMU follower motion permission is unavailable')
        owner = self._owner()
        instance, generation = requests.instance, requests.generation

        if (self.owner_identity() != owner
                or self.current_occupant() != owner.lane):
            raise ValueError('MMU follower ownership changed before send')
        identity = requests.issue(FOLLOW, owner.lane) if enable else owner

        def current():
            if (not requests.connected or requests.instance != instance
                    or requests.generation != generation
                    or self.is_current() is not True
                    or (enable and self.motion_permitted() is not True)):
                return False
            observed_owner = self.owner_identity()
            observed_lane = self.current_occupant()
            if (observed_owner == owner and type(observed_lane) is int
                    and observed_lane == owner.lane):
                return True
            # The command itself can replace the owner on START or clear it
            # on CANCEL before its admission reaches the host. Any other
            # transition is ambiguous and requires a fresh reconciliation.
            if enable:
                return (observed_owner == identity and type(observed_lane) is int
                        and observed_lane == owner.lane)
            return (observed_owner is None and (observed_lane is None
                    or type(observed_lane) is int and observed_lane == owner.lane))

        if not current():
            raise ValueError('MMU follower connection changed')
        response = send_operation(self.mcu, self.reactor, instance, identity,
                                  current, direction=direction if enable else None,
                                  cancel=not enable, timeout=self.timeout)
        if not current():
            raise ValueError('MMU follower connection changed')
        if isinstance(response, Admission):
            if response.instance != instance or response.identity != identity:
                raise ValueError('Mismatched FOLLOW admission; reconcile ownership')
            # ALREADY_COMPLETED is not proof of an active follower or a stop.
            if (response.admission not in (AdmissionResult.ACCEPTED,
                                           AdmissionResult.ALREADY_IN_PROGRESS)
                    or response.fault != Fault.NONE):
                raise ValueError('FOLLOW rejected; reconcile ownership')
            return FollowCommand(identity, response,
                                 direction=direction if enable else None)
        if isinstance(response, CommandResult):
            if (response.instance != instance
                    or response.command != (Command.START if enable
                                            else Command.CANCEL)
                    or response.query != identity.request):
                raise ValueError('Mismatched FOLLOW command result')
        raise ValueError('FOLLOW command failed; reconcile ownership')
