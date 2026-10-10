# SPDX-License-Identifier: GPL-3.0-or-later
"""OpenAMS load-cancel callback for an exact, pending firmware request.

Call on the reactor thread while the command response channel is serialized.
Cancel admission never settles the OpenAMS waiter; a retained terminal does.
"""

from .command_transport import send_operation
from .protocol import Admission, CommandResult
from .requests import LOAD, Requests
from .wire import ADMITTED, Command, CommandCode, Fault


class OpenAMSLoadCancelBridge:
    """Per-path callback with OpenAMS's load_spool_cancel() shape."""

    def __init__(self, mcu, reactor, requests, fps, is_current=None,
                 timeout=5.0):
        if (not isinstance(requests, Requests) or not isinstance(fps, str)
                or not fps or not callable(is_current)
                or type(timeout) not in (int, float) or not 0 < timeout <= 60):
            raise ValueError('Invalid OpenAMS cancel bridge')
        self.mcu, self.reactor, self.requests = mcu, reactor, requests
        self.fps, self.is_current, self.timeout = fps, is_current, timeout
        self.instance, self.generation = requests.instance, requests.generation

    def load_spool_cancel(self):
        """Send CANCEL for this path's pending LOAD; retain it for its terminal.

        An uncertain exchange leaves the same match pending for reconciliation.
        Connection validity is required, but motion-start permission is not.
        """
        requests = self.requests
        if (not requests.connected or self.is_current() is not True
                or requests.instance != self.instance
                or requests.generation != self.generation):
            raise ValueError('MMU cancel connection is unavailable')
        identity = requests.cancel_target(self.fps)
        if identity.operation != LOAD or identity.session != requests.session:
            raise ValueError('No pending LOAD for this OpenAMS path')
        instance, generation = requests.instance, requests.generation

        def current():
            return (requests.connected and requests.generation == generation
                    and requests.instance == instance
                    and requests.pending.get(self.fps, (None,))[0] == identity
                    and self.is_current() is True)

        if not current():
            raise ValueError('MMU cancel connection changed')
        response = send_operation(self.mcu, self.reactor, instance, identity,
                                  current, cancel=True, timeout=self.timeout)
        if not current():
            raise ValueError('MMU cancel connection changed')
        if isinstance(response, Admission):
            if response.instance != instance or response.identity != identity:
                raise ValueError('Mismatched CANCEL admission; reconcile pending request')
            if response.admission not in ADMITTED or response.fault != Fault.NONE:
                raise ValueError('MMU CANCEL rejected; reconcile pending request')
        elif isinstance(response, CommandResult):
            if (response.instance != instance
                    or response.command != Command.CANCEL
                    or response.query != identity.request
                    or response.result == CommandCode.OK):
                raise ValueError('Mismatched CANCEL result; reconcile pending request')
            raise ValueError('MMU CANCEL rejected with command result %d'
                             % response.result)
        else:
            raise ValueError('Invalid CANCEL response; reconcile pending request')
        return response
