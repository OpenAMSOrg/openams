# SPDX-License-Identifier: GPL-3.0-or-later
"""Match full firmware identities before delivering OpenAMS compatibility tokens.

The device adapter owns transport, verified instance/session establishment, event
acknowledgement and physical-path reconciliation. This object never sends a
command or infers operation completion from a status or admission acknowledgement.
Call all methods on the reactor thread. Capture generation BEFORE queuing a
response callback, and pass that captured value when matching it.
"""
from dataclasses import dataclass

from .protocol import (Admission, Event, Identity, Snapshot, event_flags_supported,
                       openams_terminal_value)
from .wire import (ADMITTED, TERMINAL_EVENTS, AdmissionResult,
                   Client, EventFlag, Fault, OpCode, OpType, known)

HOST = Client.HOST
LOAD, UNLOAD, FOLLOW = OpType.LOAD, OpType.UNLOAD, OpType.FOLLOW
CALIBRATE, PRELOAD, EJECT, HOME = OpType.CALIBRATE, OpType.PRELOAD, OpType.EJECT, OpType.HOME


def _uint(value, bits, nonzero=False):
    if type(value) is not int or not int(nonzero) <= value < 1 << bits:
        raise ValueError("Invalid %d-bit identity value" % bits)
    return value


def _rejection_code(admission, fault):
    # Same OpenAMS result codes as the portable km_protocol_result_code helper.
    if fault in (Fault.NO_PROGRESS, Fault.STALLED):
        return OpCode.TIMEOUT
    if fault in (Fault.HOST_COMM_LOST, Fault.EMERGENCY_STOP):
        return OpCode.ERROR_KLIPPER_CALL
    if fault != Fault.NONE:
        return OpCode.ERROR_UNSPECIFIED
    return {AdmissionResult.BUSY: OpCode.ERROR_BUSY,
            AdmissionResult.REJECTED_CONFLICT: OpCode.ERROR_BUSY,
            AdmissionResult.PATH_OCCUPIED: OpCode.SPOOL_ALREADY_IN_BAY,
            AdmissionResult.NO_SPOOL: OpCode.NO_SPOOL_IN_BAY,
            AdmissionResult.INVALID_LANE: OpCode.NO_SPOOL_IN_BAY,
            AdmissionResult.FAULTED: OpCode.ERROR_UNSPECIFIED,
            AdmissionResult.UNAVAILABLE: OpCode.ERROR_UNSPECIFIED,
            AdmissionResult.REJECTED_STALE: OpCode.CANCEL}[admission]


@dataclass(frozen=True)
class Completion:
    instance: int
    generation: int
    waiter: str
    identity: Identity
    token: int
    code: int
    value: int


class Requests:
    """One verified MCU instance/host session, at most eight current waiters.

    waiter is the OpenAMS FPS path name, NOT the physical MMU bay. Starting a new
    operation for that waiter supersedes its old match, including across bays.
    The caller must settle any superseded runtime waiter before issuing new work.
    last_request must include the reconciled firmware high-water mark. A new
    object/session is never a substitute for negotiating ownership on reconnect.
    """

    def __init__(self, instance, session, last_request=0):
        self.instance = _uint(instance, 64, True)
        self.session = _uint(session, 32, True)
        self.last_request = _uint(last_request, 32)
        self.generation = 1
        self.connected = True
        self.pending = {}
        self.pending_preload = None
        self.preload_terminal = None
        self.pending_unpreload = None
        self.unpreload_terminal = None
        self.pending_eject = None
        self.eject_terminal = None
        self.pending_home = None
        self.home_terminal = None

    def issue_eject(self, lane):
        return self._issue_local('eject', EJECT, lane)

    def accept_eject(self, event, generation):
        return self._accept_local('eject', EJECT, event, generation)

    def _issue_local(self, name, operation, lane):
        if name == 'preload':
            for prior in ('preload', 'unpreload'):
                if getattr(self, 'pending_' + prior) is not None:
                    raise ValueError('Existing %s requires terminal reconciliation'
                                     % prior.upper())
        if any(getattr(self, 'pending_' + local) is not None
               for local in ('preload', 'unpreload', 'eject', 'home')):
            raise ValueError('Existing local request requires terminal reconciliation')
        if not self.connected:
            raise ConnectionError('MMU connection has not been reconciled')
        _uint(lane, 8)
        if lane >= 12 and not (operation == HOME and lane == 0xff):
            raise ValueError('Invalid MMU lane')
        if self.last_request == 0xffffffff:
            raise OverflowError('Request IDs exhausted')
        self.last_request += 1
        identity = Identity(HOST, self.session, self.last_request, operation, lane)
        setattr(self, 'pending_' + name, identity)
        setattr(self, name + '_terminal', None)
        return identity

    def _accept_local(self, name, operation, event, generation):
        if (not isinstance(event, Event) or not self._current(event.instance, generation)
                or event.identity != getattr(self, 'pending_' + name)
                or event.event_type not in TERMINAL_EVENTS[operation]):
            return False
        if (event.result_code not in known(OpCode)
                or event.flags & ~EventFlag.OVERFLOW):
            raise ValueError('Unsupported local terminal result')
        if (event.event_type == TERMINAL_EVENTS[operation][0]
                and event.result_code != OpCode.SUCCESS):
            raise ValueError('Contradictory local success event')
        setattr(self, name + '_terminal', event)
        setattr(self, 'pending_' + name, None)
        return True

    def issue_unpreload(self, lane):
        return self._issue_local('unpreload', UNLOAD, lane)

    def accept_unpreload(self, event, generation):
        return self._accept_local('unpreload', UNLOAD, event, generation)

    def issue_preload(self, lane):
        """Allocate a separate firmware request with no OpenAMS waiter."""
        return self._issue_local('preload', PRELOAD, lane)

    def accept_preload(self, event, generation):
        """Retire only an exact retained PRELOAD terminal; keep its result visible."""
        return self._accept_local('preload', PRELOAD, event, generation)

    def issue_home(self):
        return self._issue_local('home', HOME, 0xff)

    def accept_home(self, event, generation):
        return self._accept_local('home', HOME, event, generation)

    def accept_retired_local(self, event):
        """Settle one disconnected local request after external recovery proof."""
        if not isinstance(event, Event) or self.connected or event.instance != self.instance:
            return False
        identity = event.identity
        if identity == self.pending_preload:
            terminal, pending, result = (TERMINAL_EVENTS[PRELOAD], 'pending_preload',
                                         'preload_terminal')
        elif identity == self.pending_unpreload:
            terminal, pending, result = (TERMINAL_EVENTS[UNLOAD], 'pending_unpreload',
                                         'unpreload_terminal')
        elif identity == self.pending_eject:
            terminal, pending, result = (TERMINAL_EVENTS[EJECT], 'pending_eject',
                                         'eject_terminal')
        elif identity == self.pending_home:
            terminal, pending, result = (TERMINAL_EVENTS[HOME], 'pending_home',
                                         'home_terminal')
        else:
            return False
        if (identity.session != self.session or event.event_type not in terminal
                or event.result_code not in known(OpCode)
                or event.flags & ~EventFlag.OVERFLOW
                or event.event_type == terminal[0]
                and event.result_code != OpCode.SUCCESS):
            raise ValueError('Unsupported retired local terminal')
        setattr(self, result, event)
        setattr(self, pending, None)
        return True

    def issue(self, operation, lane, waiter=None, token=None):
        if not self.connected:
            raise ConnectionError("MMU connection has not been reconciled")
        if type(operation) is not int or operation not in (LOAD, UNLOAD, FOLLOW, CALIBRATE):
            raise ValueError("Unsupported operation")
        _uint(lane, 8)
        if lane >= 12:
            raise ValueError("Invalid MMU lane")
        if operation == FOLLOW:
            if waiter is not None or token is not None:
                raise ValueError("Following has no load/unload/calibration waiter")
        else:
            if not isinstance(waiter, str) or not waiter:
                raise ValueError("An OpenAMS waiter name is required")
            _uint(token, 8)  # Compatibility generation zero is valid.
            if waiter not in self.pending and len(self.pending) >= 8:
                raise ValueError("Too many pending MMU waiters")
        if self.last_request == 0xffffffff:
            raise OverflowError("Request IDs exhausted; reconcile and negotiate a newer session")
        self.last_request += 1
        identity = Identity(HOST, self.session, self.last_request, operation, lane)
        if waiter is not None:
            self.pending[waiter] = (identity, token)
        return identity

    def cancel_target(self, waiter):
        """Exact in-flight target; not an inference about post-load ownership.

        Disable of automatic following after a completed load must instead use
        the reconciled owner identity from firmware status. It is not a new ID.
        """
        if not self.connected:
            raise ConnectionError("MMU connection has not been reconciled")
        return self.pending[waiter][0]

    def _current(self, instance, generation):
        return self.connected and generation == self.generation and instance == self.instance

    def _match(self, instance, identity, generation, code, value):
        if not self._current(instance, generation):
            return None
        for waiter, (expected, token) in self.pending.items():
            if expected == identity:
                return Completion(instance, generation, waiter, identity, token, code, value)
        return None

    def admission(self, response, generation):
        if not isinstance(response, Admission):
            raise TypeError("Expected an admission response")
        if not self._current(response.instance, generation):
            return None
        if response.admission in ADMITTED:
            # Accepted / already running / already completed all need a terminal.
            return None
        if (response.admission not in known(AdmissionResult)
                or response.fault not in known(Fault)):
            raise ValueError("Unsupported admission result")
        return self._match(response.instance, response.identity, generation,
                           _rejection_code(response.admission, response.fault), 0)

    def event(self, response, generation):
        if not isinstance(response, Event):
            raise TypeError("Expected a retained event")
        if not self._current(response.instance, generation):
            return None
        terminals = TERMINAL_EVENTS.get(response.identity.operation)
        if not terminals or response.event_type not in terminals:
            # Follow stop/fault and unsolicited faults belong to the device's
            # fault/ownership handler, never a load/unload completion alias.
            return None
        success, cancelled, _ = terminals
        if (response.result_code not in known(OpCode)
                or not event_flags_supported(response)):
            raise ValueError("Unsupported terminal result")
        if response.event_type == success:
            if response.result_code != OpCode.SUCCESS:
                raise ValueError("Contradictory success event")
            code = OpCode.SUCCESS
        elif response.event_type == cancelled:
            code = OpCode.CANCEL
        else:
            # A failed event can never succeed.
            code = response.result_code or OpCode.ERROR_UNSPECIFIED
        # A step-estimated calibration is retained by the host, not given to
        # OpenAMS as an encoder-length PTFE value.
        return self._match(response.instance, response.identity, generation, code,
                           openams_terminal_value(response))

    def deliver(self, completion, callback):
        """Call callback(waiter, code, value, token), then retire only that match.

        A failed callback keeps the match available for retry. Recheck immediately
        before delivery so a matched-but-delayed callback cannot cross reconnect
        or replacement. No firmware acknowledgement is sent by this method.
        """
        if completion is None:
            return False
        if not isinstance(completion, Completion):
            raise TypeError("Expected a matched completion")
        expected = (completion.identity, completion.token)
        if (not self._current(completion.instance, completion.generation)
                or self.pending.get(completion.waiter) != expected):
            return False
        callback(completion.waiter, completion.code, completion.value, completion.token)
        # The callback may synchronously start another operation for this waiter.
        if self.pending.get(completion.waiter) == expected:
            del self.pending[completion.waiter]
        return True

    def disconnect(self):
        self.generation += 1
        self.connected = False

    def resume(self, snapshot):
        """Refresh counters from a verified same-controller coherent snapshot.

        Preserve pending matches; status cannot settle them. Faults, physical path
        ownership and eligibility to send motion remain the adapter/core's concern.
        A different MCU or a newer host session requires explicit reconciliation,
        including settling abandoned waiters as unknown, before a new tracker.
        """
        if self.connected:
            raise RuntimeError("Disconnect before reconciling a new connection")
        if not isinstance(snapshot, Snapshot) or snapshot.instance != self.instance:
            raise ConnectionError("Different MMU controller; pending results remain unknown")
        sessions = snapshot.values["client_sessions"]
        requests = snapshot.values["client_requests"]
        if len(sessions) != 4 or len(requests) != 4:
            raise ValueError("Malformed client counters")
        session = _uint(sessions[HOST], 32)
        request = _uint(requests[HOST], 32)
        if session > self.session:
            raise ConnectionError("A newer host session exists on the controller")
        if session == self.session:
            self.last_request = max(self.last_request, request)
        self.connected = True
