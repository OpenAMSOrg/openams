# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in OpenAMS unit backed by one firmware-owned HOST session."""

from dataclasses import replace
import logging
import math

from .openams_cancel_bridge import OpenAMSLoadCancelBridge
from .openams_command_device import OpenAMSCommandDevice
from .openams_follow_bridge import OpenAMSFollowCommandBridge
from .openams_operation_bridge import (OpenAMSLoadCommandBridge,
                                       OpenAMSPTFECalibrationCommandBridge,
                                       OpenAMSUnloadCommandBridge)
from .openams_verified_follower import OpenAMSVerifiedFollowerBridge
from .command_transport import send_operation
from .protocol import (Admission, CommandResult, Event, event_flags_supported,
                       openams_terminal_value)
from .event_reader import peek_event
from .requests import Completion
from .wire import (ADMITTED, TERMINAL_EVENTS, AdmissionResult, Client, Command,
                   CommandCode, Direction, Fault, FollowerState, OpCode,
                   OpState, OpType, Stage, known)


# Standalone requests: an UNPRELOAD is a lane-local UNLOAD.
_LOCAL_OPERATIONS = {'PRELOAD': OpType.PRELOAD, 'UNPRELOAD': OpType.UNLOAD,
                     'EJECT': OpType.EJECT, 'HOME': OpType.HOME}


class _NotSettled(Exception):
    pass


class OpenAMSManagedUnit(OpenAMSCommandDevice):
    """The manager's single unit, initially physical-only until admitted."""

    def __init__(self, *args, host_watchdog_ms, lease_max_age_ms):
        super().__init__(*args)
        if (type(host_watchdog_ms) is not int or host_watchdog_ms <= 0
                or type(lease_max_age_ms) is not int
                or not 0 < lease_max_age_ms < host_watchdog_ms):
            raise ValueError('Invalid OpenAMS HOST lease bounds')
        self._host_watchdog_ms = host_watchdog_ms
        self._lease_max_age_ms = lease_max_age_ms
        self._runtime = None
        self._lease = None
        self._service_timer = None
        self._follower_verifier = None
        self._pending_follower = None
        self._pending_terminal_stop = None

    @property
    def firmware_owns_liveness(self):
        return True

    def get_webhook_status(self):
        # Why and where each attempt of the latest LOAD ended, for UIs.
        return dict(super().get_webhook_status(),
                    last_load=getattr(self._observer, 'last_load', None))

    @property
    def motion_available(self):
        observer = self._observer
        lease = self._lease
        if (lease is None or observer._host_lease is not lease
                or observer._host_session_ambiguous
                or not self._event_current()):
            return False
        permitted = lease.motion_permitted(self._board_ready)
        if not permitted and not lease.available:
            observer._retain_host_recovery()
        return permitted

    def _board_ready(self):
        observer = self._observer
        status = self._captured_status
        if (not self._ready() or status is None
                or observer._firmware_actuation != 1
                or not observer._diagnostic_startup
                or observer._startup_state != 'ready'
                or observer._startup_result != 0
                or observer._host_session_ambiguous
                or observer._prepared_host_session is not self._lease.prepared):
            return False
        values = status.values
        sessions = values.get('client_sessions')
        return (values.get('motor_ready') is True
                and values.get('host_connected') is True
                and type(sessions) in (tuple, list) and len(sessions) == 4
                and sessions[Client.HOST] in (
                    self._lease.prepared.requests.session - 1,
                    self._lease.prepared.requests.session)
                and values.get('system_fault') == Fault.NONE
                and values.get('motion_fault') == Fault.NONE
                and values.get('follower_fault') == Fault.NONE
                and values.get('position_uncertain') is False
                and values.get('pending_fault_event') is False)

    def bind_runtime(self, runtime, fps_name, prepared=None, mcu=None,
                     reactor=None):
        # OAMSManager binds during construction, before MCU connect. A second
        # binding with a prepared session is internal to _activate().
        if prepared is not None:
            return super().bind_runtime(runtime, fps_name, prepared, mcu, reactor)
        if fps_name != self.fps_name:
            raise ValueError('OpenAMS FPS does not match this unit')
        missing = [name for name in (
            'dispatch', 'accept_terminal', 'accept_reload_terminal',
            'accept_verified_follower', 'accept_retired_terminal',
            'accept_firmware_fault') if not callable(getattr(runtime, name, None))]
        if missing:
            raise ValueError('OpenAMS runtime lacks %s; Klipper-MMU needs the '
                             'runtime hooks in this repository\'s src/oams_runtime.py'
                             % ', '.join(missing))
        if self._runtime is not None and self._runtime is not runtime:
            raise ValueError('OpenAMS unit is already bound to another runtime')
        self._runtime = runtime
        self._follower_verifier = OpenAMSVerifiedFollowerBridge(self, runtime)

    def refresh(self):
        ready = super().refresh()
        observer = self._observer
        if (self._event_binding is not None
                and observer._prepared_host_session is None):
            # A normal physical refresh clears only its cache. Retire a
            # disconnected command binding separately, retaining its exact
            # waiters and ACK head until explicit recovery proves them safe.
            self.retire_event_binding()
            self._lease = None
            self._pending_follower = None
            self._pending_terminal_stop = None
        if (ready and self._runtime is not None and self._event_binding is None
                and (self._retired_requests is None
                     or (not self._retired_requests.pending
                         and self._retired_requests.pending_preload is None
                         and self._retired_requests.pending_unpreload is None
                         and self._retired_requests.pending_eject is None))
                and self._retired_pending_ack is None
                and observer._prepared_host_session is None
                and not observer._host_session_ambiguous
                and observer._firmware_actuation == 1
                and observer._diagnostic_startup
                and observer._startup_state == 'ready'
                and observer._startup_result == 0):
            self._activate()
            ready = super().refresh()
        return ready

    def retry_retired_ack(self, timeout=5.0):
        """Explicitly retire this unit's exact ACK after observer confirmation."""
        record = self._observer.host_recovery_record()
        head = self._retired_pending_ack
        if (self._event_binding is not None or not isinstance(head, Event)
                or not self._retired_pending_ack_eligible
                or record is None or record.pending_ack != head
                or not record.pending_ack_eligible
                or head.instance != record.instance
                or head.identity.client != Client.HOST
                or head.identity.session != record.session):
            raise ValueError('Retired OpenAMS ACK recovery is unavailable')
        self._observer.retry_retained_host_ack(timeout=timeout)
        if self._observer.host_recovery_record() != replace(
                record, pending_ack=None, pending_ack_eligible=False):
            raise ValueError('Retired OpenAMS ACK recovery changed')
        self._retired_pending_ack = None
        self._retired_pending_ack_eligible = False
        return True

    def recover_retired_local_terminal(self, timeout=5.0):
        """Accept one old standalone terminal, then retry its exact ACK."""
        observer = self._observer
        with observer._mcu_lock:
            record = observer.host_recovery_record()
            requests = self._retired_requests
            if (getattr(observer, '_event_cycle_in_progress', False)
                    or self._event_binding is not None or requests is None
                    or record is None or not observer._host_session_ambiguous
                    or observer._host_lease is not None
                    or record.pending_ack is not None
                    or record.pending_ack_eligible
                    or self._retired_pending_ack is not None
                    or self._retired_pending_ack_eligible
                    or requests.connected or requests.pending
                    or requests.instance != record.instance
                    or requests.session != record.session
                    or requests.generation != record.request_generation + 1
                    or len(record.pending_requests) != 1
                    or type(timeout) not in (int, float)
                    or not math.isfinite(timeout) or not 0 < timeout <= 60):
                raise ValueError('Retired local terminal recovery is unavailable')
            name, identity, token = record.pending_requests[0]
            pending = getattr(requests, 'pending_' + name.lower(), None)
            operation = _LOCAL_OPERATIONS.get(name)
            if token is not None or pending is None or identity != pending:
                raise ValueError('Retired local request changed')
            if (identity.client != Client.HOST or identity.session != record.session
                    or identity.operation != operation
                    or type(identity.lane) is not int
                    or not (0 <= identity.lane < self.bay_count
                            or name == 'HOME' and identity.lane == 0xff)
                    or any(getattr(requests, 'pending_' + other) is not None
                           for other in ('preload', 'unpreload', 'eject', 'home')
                           if other != name.lower())):
                raise ValueError('Retired local request changed')
            capture = observer.capture_physical_state()
            if capture is None:
                raise ValueError('Retired local terminal requires a live capture')
            connection, _, status, _ = capture
            fields = status.values
            sessions = fields.get('client_sessions')
            if (connection != observer.physical_connection_identity()
                    or status.query <= record.query_highwater
                    or connection[1] != record.instance
                    or status.instance != record.instance
                    or type(sessions) not in (tuple, list) or len(sessions) != 4
                    or sessions[Client.HOST] != record.session
                    or type(fields.get('queued_events')) is not int
                    or fields['queued_events'] < 1
                    or type(fields.get('head_event_seq')) is not int
                    or observer.host_recovery_record() is not record):
                raise ValueError('Retired local capture changed')

            def current():
                return (observer.physical_connection_identity() == connection
                        and observer.host_recovery_record() is record
                        and observer._host_session_ambiguous
                        and observer._host_lease is None
                        and self._event_binding is None
                        and self._retired_requests is requests
                        and not requests.connected
                        and requests.generation == record.request_generation + 1
                        and not getattr(observer, '_event_cycle_in_progress', False)
                        and getattr(requests, 'pending_' + name.lower()) == identity)

            head = peek_event(observer._mcu, observer._printer.get_reactor(),
                              record.instance, current, timeout)
            if (not current() or not isinstance(head, Event)
                    or head.instance != record.instance
                    or head.identity != identity
                    or head.seq != fields['head_event_seq']):
                raise ValueError('Retired local queue head changed')
            if not requests.accept_retired_local(head):
                raise ValueError('Retired local terminal was not accepted')
            observer._host_recovery_record = replace(
                record, pending_ack=head, pending_ack_eligible=True,
                pending_requests=())
            self._retired_pending_ack = head
            self._retired_pending_ack_eligible = True
        # The committed result is the consumer boundary. An uncertain ACK
        # leaves its exact obligation available to retry_retired_ack().
        return self.retry_retired_ack(timeout=timeout)

    def recover_retired_waiter_terminal(self, timeout=5.0):
        """Settle one exact old OpenAMS waiter with a fresh physical STOP."""
        observer = self._observer
        with observer._mcu_lock:
            record = observer.host_recovery_record()
            requests = self._retired_requests
            runtime = self._runtime
            if (getattr(observer, '_event_cycle_in_progress', False)
                    or self._event_binding is not None or requests is None
                    or runtime is None
                    or not callable(getattr(runtime, 'accept_retired_terminal', None))
                    or record is None or not observer._host_session_ambiguous
                    or observer._host_lease is not None
                    or record.pending_ack is not None or record.pending_ack_eligible
                    or self._retired_pending_ack is not None
                    or self._retired_pending_ack_eligible
                    or requests.connected or len(requests.pending) != 1
                    or requests.pending_preload is not None
                    or requests.pending_unpreload is not None
                    or requests.pending_eject is not None
                    or requests.instance != record.instance
                    or requests.session != record.session
                    or requests.generation != record.request_generation + 1
                    or len(record.pending_requests) != 1
                    or type(timeout) not in (int, float)
                    or not math.isfinite(timeout) or not 0 < timeout <= 60):
                raise ValueError('Retired OpenAMS waiter recovery is unavailable')
            waiter, identity, token = record.pending_requests[0]
            if (waiter != self.fps_name
                    or requests.pending.get(waiter) != (identity, token)
                    or type(token) is not int or not 0 <= token <= 255
                    or identity.client != Client.HOST
                    or identity.session != record.session
                    or identity.operation not in (OpType.LOAD, OpType.UNLOAD,
                                                  OpType.CALIBRATE)
                    or type(identity.lane) is not int
                    or not 0 <= identity.lane < self.bay_count):
                raise ValueError('Retired OpenAMS request changed')
            capture = observer.capture_physical_state()
            if capture is None:
                raise ValueError('Retired OpenAMS terminal requires a live capture')
            connection, _, status, _ = capture
            fields = status.values
            sessions = fields.get('client_sessions')
            if (connection != observer.physical_connection_identity()
                    or status.query <= record.query_highwater
                    or connection[1] != record.instance
                    or status.instance != record.instance
                    or type(sessions) not in (tuple, list) or len(sessions) != 4
                    or sessions[Client.HOST] != record.session
                    or type(fields.get('queued_events')) is not int
                    or fields['queued_events'] < 1
                    or type(fields.get('head_event_seq')) is not int
                    or fields['head_event_seq'] <= 0
                    or fields.get('follower_state') != FollowerState.DISARMED
                    or fields.get('follower_fault') != Fault.NONE
                    or fields.get('velocity') != 0
                    or fields.get('motion_fault') != Fault.NONE
                    or fields.get('system_fault') != Fault.NONE
                    or fields.get('position_uncertain') is not False
                    or type(fields.get('client_requests')) not in (tuple, list)
                    or len(fields['client_requests']) != 4
                    or type(fields['client_requests'][Client.HOST]) is not int
                    or fields['client_requests'][Client.HOST] != identity.request
                    or observer.host_recovery_record() is not record):
                raise ValueError('Retired OpenAMS physical STOP is unproven')
            occupied = fields.get('path_occupied') is True
            following_load = (identity.operation == OpType.LOAD
                              and fields.get('state') == OpState.FOLLOWING
                              and fields.get('stage') == Stage.FOLLOW
                              and fields.get('lane_active') is True)
            # UNLOADING is not settled.
            settled = (fields.get('state') in (OpState.IDLE, OpState.LOADED)
                       and fields.get('stage') == Stage.STOPPED
                       and fields.get('lane_active') is False
                       and fields.get('operation') == OpType.NONE
                       and fields.get('client') == Client.NONE
                       and fields.get('session') == identity.session
                       and fields.get('request') == identity.request
                       and fields.get('lane') == identity.lane)
            if (fields.get('path_occupied') not in (True, False)
                    or not (following_load or settled)
                    or (occupied and fields.get('occupant_lane') != identity.lane)
                    or (following_load and (not occupied
                                           or fields.get('owner_lane') != identity.lane
                                           or fields.get('client') != identity.client
                                           or fields.get('session') != identity.session
                                           or fields.get('request') != identity.request
                                           or fields.get('operation') != identity.operation
                                           or fields.get('lane') != identity.lane))
                    or (settled and fields.get('state') == OpState.LOADED
                        and not occupied)
                    or (settled and fields.get('state') == OpState.IDLE
                        and occupied)):
                raise ValueError('Retired OpenAMS bay or owner changed')

            def current():
                return (observer.physical_connection_identity() == connection
                        and observer.host_recovery_record() is record
                        and observer._host_session_ambiguous
                        and observer._host_lease is None
                        and self._event_binding is None
                        and self._retired_requests is requests
                        and not requests.connected
                        and requests.generation == record.request_generation + 1
                        and requests.pending == {waiter: (identity, token)}
                        and not getattr(observer, '_event_cycle_in_progress', False))

            head = peek_event(observer._mcu, observer._printer.get_reactor(),
                              record.instance, current, timeout)
            if (not current() or not isinstance(head, Event)
                    or head.instance != record.instance
                    or head.identity != identity
                    or head.seq != fields['head_event_seq']):
                raise ValueError('Retired OpenAMS queue head changed')
            # Validate the terminal before entering the runtime. Requests.event()
            # intentionally rejects disconnected sessions, so retain its exact
            # terminal mapping here for the explicit recovery path.
            terminal = TERMINAL_EVENTS[identity.operation]
            if (head.event_type not in terminal
                    or head.result_code not in known(OpCode)
                    or not event_flags_supported(head)
                    or head.event_type == terminal[0]
                    and head.result_code != OpCode.SUCCESS):
                raise ValueError('Unsupported retired OpenAMS terminal')
            successful = head.event_type == terminal[0]
            if identity.operation == OpType.LOAD:
                expected_occupied = successful
            elif identity.operation == OpType.UNLOAD:
                expected_occupied = not successful
            else:
                # PTFE calibration starts with an empty shared path and
                # retracts the probe before completion. An occupied path
                # cannot prove a safe terminal of any outcome.
                expected_occupied = False
            if occupied != expected_occupied:
                raise ValueError('Retired OpenAMS terminal contradicts physical path')
            code = (OpCode.SUCCESS if head.event_type == terminal[0]
                    else OpCode.CANCEL if head.event_type == terminal[1]
                    else head.result_code or OpCode.ERROR_UNSPECIFIED)
            # Peeking can take time while firmware continues to run. Read a
            # second physical snapshot after the peek so the STOP proof cannot
            # be an observation that predates the retained terminal check.
            # The mutex stays held, so no other exchange runs in between.
            confirmed = observer.capture_physical_state()
            if confirmed is None:
                raise ValueError('Retired OpenAMS physical STOP was lost')
            second_connection, _, second_status, _ = confirmed
            proof_fields = ('client_sessions', 'client_requests', 'queued_events',
                            'head_event_seq', 'follower_state', 'follower_fault',
                            'velocity', 'state', 'stage', 'motion_fault',
                            'system_fault', 'position_uncertain', 'path_occupied',
                            'lane_active', 'owner_lane', 'occupant_lane',
                            'client', 'session', 'request', 'operation', 'lane')
            if (not current() or second_connection != connection
                    or second_status.instance != record.instance
                    or second_status.query <= status.query
                    or any(second_status.values.get(key) != fields.get(key)
                           for key in proof_fields)):
                raise ValueError('Retired OpenAMS physical STOP changed')
            unit = (self.oams_idx, identity.lane)
            if runtime.accept_retired_terminal(
                    waiter, unit, token, code, openams_terminal_value(head),
                    (unit, token, True)) is not True:
                raise ValueError('Retired OpenAMS waiter was not accepted')
            # Runtime settlement is the irreversible boundary. Its callback
            # may synchronously update runtime state; do not leave the old
            # Requests match live after the waiter has been settled.
            del requests.pending[waiter]
            observer._host_recovery_record = replace(
                record, pending_ack=head, pending_ack_eligible=True,
                pending_requests=())
            self._retired_pending_ack = head
            self._retired_pending_ack_eligible = True
        return self.retry_retired_ack(timeout=timeout)

    def retry_retained_fault_ack(self, timeout=5.0):
        """Explicitly accept and ACK the diagnosed fault; never resume motion."""
        return self._observer.retry_retained_fault_ack(self, timeout=timeout)

    def acknowledge_fault(self):
        """OAMS_CLEAR_FAULT OAMS=<idx>, the C2 `clear_fault` token.

        C5 §7.6's recovery for this unit is its fault acknowledgement, the
        same one KLIPPER_MMU_ACK_FAULT runs. It settles the retained event and
        the OpenAMS latch's waiter; the firmware fault and the path stay, and
        KLIPPER_MMU_RECONCILE_PATH is what clears them."""
        return self.retry_retained_fault_ack()

    def preload(self, bay):
        """OAMS_PRELOAD SPOOL=<bay> (C3 §2): this unit's PRELOAD, the same
        path KLIPPER_MMU_PRELOAD BAY= runs."""
        return self.start_preload(bay)

    def unpreload(self, bay):
        """OAMS_UNPRELOAD SPOOL=<bay> (C3 §2): KLIPPER_MMU_UNPRELOAD's."""
        return self.start_unpreload(bay)

    def eject(self, bay):
        """OAMS_EJECT SPOOL=<bay> (C3 §2): KLIPPER_MMU_EJECT's."""
        return self.start_eject(bay)

    def home(self):
        """OAMS_HOME (C3 §2): KLIPPER_MMU_HOME's, for a selector build."""
        return self.start_home()

    def _activate(self):
        observer = self._observer
        self._pending_follower = None
        self._pending_terminal_stop = None
        reserved = self._state.local_reserved_lane
        occupant = self.current_spool
        if reserved is not None:
            if occupant is not None:
                observer._retain_host_recovery()
                raise ValueError('Preloaded HOST path has a conflicting occupant')
            preloaded = observer.prepare_preloaded_host_session(self.bay_count)
            if preloaded.lane != reserved:
                observer._retain_host_recovery()
                raise ValueError('Preloaded HOST lane changed during preparation')
            prepared = preloaded.prepared
        elif occupant is None:
            prepared = observer.prepare_cold_host_session(self.bay_count)
        else:
            occupied = observer.prepare_occupied_host_session(self.bay_count)
            if occupied.lane != occupant:
                observer._retain_host_recovery()
                raise ValueError('Occupied HOST lane changed during preparation')
            prepared = occupied.prepared
        lease = observer.start_host_lease(
            prepared, self._host_watchdog_ms, self._lease_max_age_ms)
        mcu, reactor = observer.get_mcu(), observer._printer.get_reactor()
        current = lambda: (self._event_current()
                           and observer._prepared_host_session is prepared
                           and not observer._host_session_ambiguous)
        motion = lambda: self.motion_available
        accept = self._runtime.accept_terminal
        args = (mcu, reactor, prepared.requests, self.fps_name, self.bay_count)
        self._load = OpenAMSLoadCommandBridge(
            *args, is_current=current, motion_permitted=motion,
            accept_terminal=accept)
        self._unload = OpenAMSUnloadCommandBridge(
            *args, current_occupant=lambda: self._command_occupant(),
            is_current=current, motion_permitted=motion,
            accept_terminal=accept)
        self._calibrate = OpenAMSPTFECalibrationCommandBridge(
            *args, is_current=current, motion_permitted=motion,
            accept_terminal=accept)
        self._cancel = OpenAMSLoadCancelBridge(
            mcu, reactor, prepared.requests, self.fps_name, is_current=current)
        self._follow = OpenAMSFollowCommandBridge(
            mcu, reactor, prepared.requests,
            owner_identity=self.owner_identity,
            current_occupant=lambda: self.current_spool,
            is_current=current, motion_permitted=motion,
            bay_count=self.bay_count)
        self._lease = lease
        try:
            super().bind_runtime(self._runtime, self.fps_name, prepared, mcu, reactor)
            self._service_timer = reactor.register_timer(
                self._service, reactor.monotonic() + self._service_period())
        except Exception:
            observer._retain_host_recovery()
            raise

    def _service_period(self):
        return min(0.25, self._lease.max_age_s / 3.)

    def _service(self, eventtime):
        observer = self._observer
        lease = self._lease
        reactor = observer._printer.get_reactor()
        if (lease is None or observer._host_lease is not lease
                or observer._host_session_ambiguous or not self._event_current()):
            self._pending_follower = None
            self._pending_terminal_stop = None
            return reactor.NEVER
        now = reactor.monotonic()
        # Each step below waits for another MCU exchange to finish. An event
        # transaction may have released the mutex to its consumer; defer to it
        # only while the last confirmed lease is live.
        if getattr(observer, '_event_cycle_in_progress', False):
            if (lease.confirmed_at is None or now < lease.confirmed_at
                    or now >= lease.confirmed_at + lease.max_age_s):
                observer._retain_host_recovery()
                return reactor.NEVER
            remaining = lease.confirmed_at + lease.max_age_s - now
            return now + min(self._service_period(), remaining)

        def peek_fault():
            # A retained fault may block renewal or the normal terminal
            # consumer. This is one bounded read, never an event ACK.
            probe = getattr(observer, 'peek_retained_fault_once', None)
            if callable(probe):
                try:
                    probe(self, timeout=min(.2, lease.max_age_s / 4.))
                except Exception:
                    pass

        probe_fault_on_failure = True
        try:
            # A heartbeat's board capture does not update this unit's cache.
            # Refresh it ahead of TTL so command permission never relies on an
            # expired physical observation between otherwise healthy cycles.
            if (self._pending_follower is not None
                    or self._captured_at is None or now < self._captured_at
                    or now - self._captured_at >= self._cache_ttl / 2.):
                self.refresh()
                if self._bound is None or self._identity() != self._bound:
                    # refresh() itself gave up on this connection (no capture
                    # at all, or a changed identity): a real disconnect, not
                    # a one-tick reading.
                    raise ConnectionError('OpenAMS physical capture is unavailable')
                if self._ready():
                    self._verify_pending_follower(reactor.monotonic())
                # Otherwise this tick's capture was inconclusive on a live
                # connection: the firmware keeps its terminal retained, so
                # renew and service events anyway and capture again next
                # tick. A real disconnect fails renew_host_lease below.
            # Renew before the event query can consume the remaining lease.
            if (lease.confirmed_at is None or
                    reactor.monotonic() - lease.confirmed_at
                    >= lease.max_age_s / 2.):
                observer.renew_host_lease(timeout=min(.2, lease.max_age_s / 4.))
            probe_fault_on_failure = False
            pending_before_event = self._pending_follower
            outcome = observer.service_openams_event_once(
                self, timeout=min(.2, lease.max_age_s / 4.))
            if outcome == 'blocked':
                pending = self._pending_follower
                now = reactor.monotonic()
                if (pending is not None and pending is not pending_before_event
                        and pending[3] is False):
                    self._pending_terminal_stop = pending
                if (pending is not None
                        and pending is self._pending_terminal_stop
                        and pending[3] is False
                        and pending[4] is self._event_binding
                        and self._event_current()
                        and observer._host_lease is lease
                        and not observer._host_session_ambiguous
                        and now < pending[5]
                        and lease.confirmed_at is not None
                        and lease.confirmed_at <= now
                        and now < lease.confirmed_at + lease.max_age_s):
                    # The terminal consumer just admitted a follower STOP.
                    # Keep its retained head for a later physical capture.
                    return min(now + self._service_period(), pending[5],
                               lease.confirmed_at + lease.max_age_s)
                self._pending_terminal_stop = None
                peek_fault()
                # The timer stops here too: a live connection that cannot
                # deliver its retained event leaves any outstanding OpenAMS
                # waiter (an UNLOAD, say) blocked forever with no reply
                # instead of failing. Settle it the same way the except
                # branch below does, before recovery retires the binding.
                self._settle_abandoned_waiters()
                logging.warning(
                    'klipper_mmu %s: HOST service could not deliver a retained '
                    'event and will not retry; run KLIPPER_MMU_RECOVER_SESSION '
                    '(or KLIPPER_MMU_RECOVER_WAITER for an outstanding '
                    'LOAD/UNLOAD/CALIBRATE) to recover', self.name)
                observer._retain_host_recovery()
                return reactor.NEVER
            self._pending_terminal_stop = None
        except Exception:
            if probe_fault_on_failure:
                # A board fault can prevent lease renewal before the normal
                # event cycle sees its retained head. One read-only peek may
                # still report it; failure here never restores motion.
                peek_fault()
            self._pending_terminal_stop = None
            # The timer stops here: fail every blocked waiter now instead of
            # leaving its G-code command to hang. Recovery stays retained.
            self._settle_abandoned_waiters()
            logging.exception(
                'klipper_mmu %s: HOST service failed and will not retry; '
                'run KLIPPER_MMU_RECOVER_SESSION (or KLIPPER_MMU_RECOVER_WAITER '
                'for an outstanding LOAD/UNLOAD/CALIBRATE) to recover', self.name)
            observer._retain_host_recovery()
            return reactor.NEVER
        return reactor.monotonic() + self._service_period()

    def _settle_abandoned_waiters(self):
        """Fail every OpenAMS waiter still open on this HOST session.

        Uses the same Completion/Requests.deliver/accept_terminal path as a
        firmware-rejected START (OpenAMSLoadCommandBridge._defer_rejection):
        no second completion channel. A waiter OpenAMS no longer recognizes
        (stale token or generation) is left for RECOVER_WAITER instead.
        """
        binding = self._event_binding
        runtime = self._runtime
        if binding is None or runtime is None:
            return
        requests = binding[1].requests
        if not requests.connected:
            return
        for waiter, (identity, token) in list(requests.pending.items()):
            completion = Completion(requests.instance, requests.generation,
                                    waiter, identity, token,
                                    OpCode.ERROR_KLIPPER_CALL, 0)

            def settle(fps, code, value, gen, lane=identity.lane):
                if (not runtime.accept_terminal(fps, code, value, gen)
                        and not runtime.accept_reload_terminal(
                            fps, code, value, gen, lane)):
                    raise _NotSettled()

            try:
                requests.deliver(completion, settle)
            except Exception:
                pass

    def _verify_pending_follower(self, now):
        pending = self._pending_follower
        if pending is None:
            return
        command, before, unit, enabled, binding, deadline = pending
        if (now > deadline or self._event_binding is not binding
                or not self._event_current()):
            self._pending_follower = None
            self._pending_terminal_stop = None
            return
        if self._follower_verifier.accept(command, before, unit, enabled):
            self._pending_follower = None
            self._pending_terminal_stop = None

    def _command(self, callback, *args):
        with self._observer._mcu_lock:
            return callback(*args)

    def _command_occupant(self):
        if not self._ready():
            return None
        return (self._state.local_reserved_lane
                if self._state.local_reserved_lane is not None
                else self.current_spool)

    def start_load_spool(self, spool_idx, gen=None):
        reserved = self._state.local_reserved_lane if self._ready() else None
        if reserved is not None and spool_idx != reserved:
            raise ValueError('Local PRELOAD reserves another MMU bay')
        return self._command(super().start_load_spool, spool_idx, gen)

    def start_preload(self, bay):
        """Start firmware PRELOAD without creating an OpenAMS operation."""
        if type(bay) is not int or not 0 <= bay < self.bay_count:
            raise ValueError('Invalid MMU bay')
        if not self._event_current() or not self.motion_available:
            raise ValueError('MMU motion permission or connection is unavailable')

        def start():
            requests = self._event_binding[1].requests
            generation, instance = requests.generation, requests.instance

            def current():
                return (self._event_current() and requests.connected
                        and requests.generation == generation
                        and requests.instance == instance
                        and self.motion_available)

            if not current():
                raise ValueError('MMU motion permission or connection changed')
            identity = requests.issue_preload(bay)
            response = send_operation(self._event_binding[2], self._event_binding[3],
                                      instance, identity, current,
                                      direction=Direction.FORWARD)
            if not current():
                raise ValueError('MMU motion permission or connection changed')
            if isinstance(response, Admission):
                if response.instance != instance or response.identity != identity:
                    raise ValueError('Mismatched PRELOAD admission; reconcile')
                if response.admission in ADMITTED:
                    return identity, response
                # Definite rejection has no retained terminal. Preserve the
                # identity/result for inspection without calling OpenAMS.
                requests.preload_terminal = response
                requests.pending_preload = None
                return identity, response
            if (not isinstance(response, CommandResult)
                    or response.instance != instance
                    or response.command != Command.START
                    or response.query != identity.request
                    or response.result == CommandCode.OK):
                raise ValueError('Mismatched PRELOAD result; reconcile')
            requests.preload_terminal = response
            requests.pending_preload = None
            return identity, response

        return self._command(start)

    def start_unload_spool(self, gen=None):
        return self._command(super().start_unload_spool, gen)

    def start_unpreload(self, bay):
        """Unload a proven local reservation without an OpenAMS waiter."""
        if type(bay) is not int or not 0 <= bay < self.bay_count:
            raise ValueError('Invalid MMU bay')
        # A cache entry can still be within its TTL after the local path has
        # changed. Prove the reservation again immediately before allocating
        # a request; the command guard pins this exact capture through ACK.
        if not self.refresh():
            raise ValueError('UNPRELOAD requires a current local reservation in this bay')
        if not self._event_current() or not self.motion_available:
            raise ValueError('MMU motion permission or connection is unavailable')

        def start():
            requests = self._event_binding[1].requests
            generation, instance = requests.generation, requests.instance
            capture = self._captured_status

            def current():
                return (self._event_current() and requests.connected
                        and requests.generation == generation
                        and requests.instance == instance
                        and self._captured_status is capture
                        and self._ready() and self.motion_available
                        and self._state.local_reserved_lane == bay
                        and self.current_spool is None)

            if not current():
                raise ValueError('UNPRELOAD requires a current local reservation in this bay')
            identity = requests.issue_unpreload(bay)
            response = send_operation(self._event_binding[2], self._event_binding[3],
                                      instance, identity, current,
                                      direction=Direction.REVERSE)
            if not current():
                raise ValueError('UNPRELOAD connection or capture changed')
            if isinstance(response, Admission):
                if response.instance != instance or response.identity != identity:
                    raise ValueError('Mismatched UNPRELOAD admission; reconcile')
                if response.admission not in ADMITTED:
                    requests.unpreload_terminal = response
                    requests.pending_unpreload = None
                return identity, response
            if (not isinstance(response, CommandResult)
                    or response.instance != instance
                    or response.command != Command.START
                    or response.query != identity.request
                    or response.result == CommandCode.OK):
                raise ValueError('Mismatched UNPRELOAD result; reconcile')
            requests.unpreload_terminal = response
            requests.pending_unpreload = None
            return identity, response

        return self._command(start)

    def start_eject(self, bay):
        """Start a full firmware EJECT without an OpenAMS waiter."""
        if type(bay) is not int or not 0 <= bay < self.bay_count:
            raise ValueError('Invalid MMU bay')
        if not self.refresh():
            # Say what to do when the reason is simply a preloaded lane.
            with self._observer._mcu_lock:
                capture = self._observer.capture_physical_state()
            state = capture[2].values.get('state') if capture is not None else None
            if state == OpState.PRELOADED:
                raise ValueError('EJECT needs a clear shared path: a lane is '
                                 'preloaded; run KLIPPER_MMU_UNPRELOAD first')
            raise ValueError('EJECT requires a current physical snapshot')
        if (self._state.hub_hes_value is None
                or self.current_spool is not None):
            raise ValueError('EJECT requires a clear shared path after UNLOAD')
        if not self._event_current() or not self.motion_available:
            raise ValueError('MMU motion permission or connection is unavailable')

        def start():
            requests = self._event_binding[1].requests
            generation, instance = requests.generation, requests.instance
            capture = self._captured_status

            def current():
                return (self._event_current() and requests.connected
                        and requests.generation == generation
                        and requests.instance == instance
                        and self._captured_status is capture
                        and self._ready() and self.motion_available
                        and self._state.hub_hes_value is not None
                        and self.current_spool is None)

            if not current():
                raise ValueError('EJECT connection or capture changed')
            identity = requests.issue_eject(bay)
            response = send_operation(self._event_binding[2], self._event_binding[3],
                                      instance, identity, current,
                                      direction=Direction.REVERSE)
            if not current():
                raise ValueError('EJECT connection or capture changed')
            if isinstance(response, Admission):
                if response.instance != instance or response.identity != identity:
                    raise ValueError('Mismatched EJECT admission; reconcile')
                if response.admission not in ADMITTED:
                    requests.eject_terminal = response
                    requests.pending_eject = None
                return identity, response
            if (not isinstance(response, CommandResult)
                    or response.instance != instance
                    or response.command != Command.START
                    or response.query != identity.request
                    or response.result == CommandCode.OK):
                raise ValueError('Mismatched EJECT result; reconcile')
            requests.eject_terminal = response
            requests.pending_eject = None
            return identity, response

        return self._command(start)

    def start_home(self):
        """Start firmware HOME without creating an OpenAMS operation."""
        if not self._event_current() or not self.motion_available:
            raise ValueError('MMU motion permission or connection is unavailable')

        def start():
            requests = self._event_binding[1].requests
            generation, instance = requests.generation, requests.instance

            def current():
                return (self._event_current() and requests.connected
                        and requests.generation == generation
                        and requests.instance == instance
                        and self.motion_available)

            if not current():
                raise ValueError('MMU motion permission or connection changed')
            identity = requests.issue_home()
            response = send_operation(self._event_binding[2], self._event_binding[3],
                                      instance, identity, current,
                                      direction=Direction.FORWARD)
            if not isinstance(response, Admission):
                requests.pending_home = None
            return identity, response

        return self._command(start)

    def cancel_local_request(self, operation):
        """Cancel the exact standalone request; its retained event settles it."""
        if operation not in ('PRELOAD', 'UNPRELOAD', 'EJECT', 'HOME'):
            raise ValueError('Invalid local operation')

        def cancel():
            binding = self._event_binding
            if binding is None or not self._event_current():
                raise ValueError('MMU cancel connection is unavailable')
            requests = binding[1].requests
            instance, generation, session = (requests.instance,
                                             requests.generation,
                                             requests.session)
            field = 'pending_' + operation.lower()
            identity = getattr(requests, field)
            if (identity is None or identity.session != session
                    or identity.client != Client.HOST
                    or identity.operation != _LOCAL_OPERATIONS[operation]):
                raise ValueError('No pending %s for this session' % operation)

            def current():
                return (self._event_binding is binding and self._event_current()
                        and requests.connected and requests.instance == instance
                        and requests.generation == generation
                        and requests.session == session
                        and getattr(requests, field) == identity)

            if not current():
                raise ValueError('MMU cancel connection changed')
            response = send_operation(binding[2], binding[3], instance, identity,
                                      current, cancel=True)
            if not current():
                raise ValueError('MMU cancel connection changed')
            if isinstance(response, Admission):
                if response.instance != instance or response.identity != identity:
                    raise ValueError('Mismatched CANCEL admission; reconcile')
                if (response.admission not in (AdmissionResult.ACCEPTED,
                                               AdmissionResult.ALREADY_IN_PROGRESS)
                        or response.fault != Fault.NONE):
                    raise ValueError('MMU CANCEL rejected; reconcile')
            elif isinstance(response, CommandResult):
                if (response.instance != instance
                        or response.command != Command.CANCEL
                        or response.query != identity.request
                        or response.result == CommandCode.OK):
                    raise ValueError('Mismatched CANCEL result; reconcile')
                raise ValueError('MMU CANCEL rejected with command result %d'
                                 % response.result)
            else:
                raise ValueError('Invalid CANCEL response; reconcile')
            return identity, response

        return self._command(cancel)

    def start_calibrate(self, kind, bay, gen=None):
        return self._command(super().start_calibrate, kind, bay, gen)

    def load_spool_cancel(self):
        return self._command(super().load_spool_cancel)

    def set_oams_follower(self, enable, direction):
        def issue():
            before = self.follower_observation()
            binding = self._event_binding
            # One physical path has one follower motor. A later command makes
            # an older admission unsuitable for confirming the current state.
            self._pending_follower = None
            self._pending_terminal_stop = None
            command = super(OpenAMSManagedUnit, self).set_oams_follower(
                enable, direction)
            if before is not None and binding is self._event_binding:
                now = self._observer.physical_monotonic()
                self._pending_follower = (
                    command, before, (self.oams_idx, before.bay), bool(enable),
                    binding, now + self._cache_ttl)
            return command

        return self._command(issue)
