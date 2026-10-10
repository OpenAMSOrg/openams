# SPDX-License-Identifier: GPL-3.0-or-later
"""Guarded OpenAMS motion callbacks mapped to full-identity MMU START requests.

This is not an installed device or a host motion controller. A caller must
first reconcile MCU identity, host session, path ownership and hardware
readiness, then provide live connection and motion-permission predicates.
Accepted admission leaves the OpenAMS waiter pending until a retained terminal
settles it through the separate event cycle. A definite rejection is deferred
until OpenAMS finishes its current dispatch, then settles only that waiter.
"""

from dataclasses import dataclass

from .command_transport import send_operation
from .protocol import Admission, CommandResult, Identity
from .requests import CALIBRATE, LOAD, UNLOAD, Completion, Requests
from .wire import ADMITTED, Command, CommandCode, Direction


class _NotSettled(Exception):
    pass


@dataclass(frozen=True)
class LoadStart:
    identity: Identity
    response: object


class OpenAMSLoadCommandBridge:
    """Per-unit callback with OpenAMS's start_load_spool(bay, gen) shape."""

    hardware_snapshot_ready = False

    def __init__(self, mcu, reactor, requests, fps, bay_count,
                 is_current=None, motion_permitted=None, accept_terminal=None):
        if (not isinstance(requests, Requests) or not isinstance(fps, str)
                or not fps or type(bay_count) is not int
                or not 1 <= bay_count <= 12):
            raise ValueError('Invalid OpenAMS load bridge')
        self.mcu, self.reactor, self.requests = mcu, reactor, requests
        self.fps, self.bay_count = fps, bay_count
        self.is_current, self.motion_permitted = is_current, motion_permitted
        self.accept_terminal = accept_terminal

    def _defer_rejection(self, completion):
        """Settle after OpenAMS finishes dispatching this StartLoad effect."""
        def settle(eventtime):
            if self.is_current() is not True:
                return

            def accept(fps, code, value, token):
                if not self.accept_terminal(fps, code, value, token):
                    raise _NotSettled()

            try:
                self.requests.deliver(completion, accept)
            except _NotSettled:
                # Keep the exact match for explicit waiter reconciliation.
                pass

        self.reactor.register_async_callback(settle)

    def _start(self, spool_idx, gen, operation, direction, lane_current):
        if type(gen) is not int or not 0 <= gen <= 255:
            raise ValueError('Invalid OpenAMS waiter generation')
        if self.fps in self.requests.pending:
            raise ValueError('Existing OpenAMS waiter must settle before START')
        if (not callable(self.is_current) or not callable(self.motion_permitted)
                or not callable(self.accept_terminal)
                or self.is_current() is not True
                or self.motion_permitted() is not True
                or lane_current() is not True
                or not self.requests.connected):
            raise ValueError('MMU motion permission or connection is unavailable')
        instance = self.requests.instance
        generation = self.requests.generation

        def current():
            return (self.requests.connected
                    and self.requests.generation == generation
                    and self.requests.instance == instance
                    and self.is_current() is True
                    and self.motion_permitted() is True
                    and lane_current() is True)

        if not current():
            raise ValueError('MMU motion permission or connection changed')
        identity = self.requests.issue(operation, spool_idx, self.fps, gen)
        response = send_operation(self.mcu, self.reactor, instance, identity,
                                  current, direction=direction)
        if not current():
            raise ValueError('MMU motion permission or connection changed')
        if not isinstance(response, (Admission, CommandResult)):
            raise ValueError('Invalid MMU START response; reconcile pending request')
        if isinstance(response, Admission):
            if response.instance != instance or response.identity != identity:
                raise ValueError('Mismatched START admission; reconcile pending request')
            completion = self.requests.admission(response, generation)
            if response.admission not in ADMITTED:
                if completion is None:
                    raise ValueError('Unmatched START rejection; reconcile pending request')
                self._defer_rejection(completion)
        else:
            if (response.instance != instance
                    or response.command != Command.START
                    or response.query != identity.request
                    or response.result == CommandCode.OK):
                raise ValueError('Mismatched START result; reconcile pending request')
            # Runtime._effect_failed also queues a code-5 completion after this
            # callback raises. Our exact acceptance callback runs first and
            # retires Requests only once the public runtime hook settles it.
            self._defer_rejection(Completion(instance, generation, self.fps,
                                             identity, gen, 5, 0))
            raise ValueError('MMU START rejected with command result %d'
                             % response.result)
        return LoadStart(identity, response)

    def start_load_spool(self, spool_idx, gen=None):
        """Return START admission; retained terminal settles accepted motion.

        A send failure or lost guard leaves the request for reconciliation.
        """
        if type(spool_idx) is not int or not 0 <= spool_idx < self.bay_count:
            raise ValueError('Invalid OpenAMS bay')
        return self._start(spool_idx, gen, LOAD, Direction.FORWARD,
                           lambda: True)


class OpenAMSUnloadCommandBridge(OpenAMSLoadCommandBridge):
    """OpenAMS unload callback using the current reconciled path occupant."""

    def __init__(self, mcu, reactor, requests, fps, bay_count,
                 current_occupant=None, is_current=None, motion_permitted=None,
                 accept_terminal=None):
        super().__init__(mcu, reactor, requests, fps, bay_count,
                         is_current, motion_permitted, accept_terminal)
        self.current_occupant = current_occupant

    def start_unload_spool(self, gen=None):
        if not callable(self.current_occupant):
            raise ValueError('Current reconciled path occupant is unavailable')
        lane = self.current_occupant()
        if type(lane) is not int or not 0 <= lane < self.bay_count:
            raise ValueError('Current reconciled path occupant is unknown')
        def lane_current():
            occupant = self.current_occupant()
            return type(occupant) is int and occupant == lane

        return self._start(lane, gen, UNLOAD, Direction.REVERSE, lane_current)


class OpenAMSPTFECalibrationCommandBridge(OpenAMSLoadCommandBridge):
    """OpenAMS start_calibrate callback for firmware's PTFE probe only.

    The START direction field is required by the wire format; firmware ignores
    it for calibration. The same live motion guard applies to the PTFE probe.
    """

    def start_calibrate(self, kind, bay, gen=None):
        if kind != 'ptfe':
            raise ValueError('Unsupported OpenAMS calibration kind: %r' % kind)
        if type(bay) is not int or not 0 <= bay < self.bay_count:
            raise ValueError('Invalid OpenAMS bay')
        return self._start(bay, gen, CALIBRATE, Direction.FORWARD,
                           lambda: True)
