# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit reactor-thread HOST lease renewal for one prepared MMU session.

The owner serializes this exchange with all other MCU commands and calls renew()
soon enough for the firmware watchdog. No timer, retry, or motion authority is
created here. A failed or ambiguous renewal requires fresh reconciliation.
"""

import math

from .command_transport import send_heartbeat
from .openams_session_bootstrap import PreparedHostSession
from .protocol import CommandResult


class HostLease:
    def __init__(self, prepared, mcu, reactor, generation, is_current,
                 host_watchdog_ms, max_age_ms):
        if not isinstance(prepared, PreparedHostSession):
            raise TypeError('Expected a prepared HOST session')
        if (type(generation) is not int or generation < 1
                or prepared.delivery.generation != generation
                or prepared.delivery.instance != prepared.requests.instance
                or not callable(is_current)):
            raise ValueError('Invalid HOST connection identity')
        if (type(host_watchdog_ms) is not int or host_watchdog_ms <= 0
                or type(max_age_ms) is not int
                or not 0 < max_age_ms < host_watchdog_ms):
            raise ValueError('HOST lease bound must be below firmware watchdog')
        self.prepared = prepared
        self.mcu = mcu
        self.reactor = reactor
        self.generation = generation
        self.is_current = is_current
        self.max_age_s = max_age_ms / 1000.
        self.available = True
        self.confirmed_at = None  # Cold-session admission has no local age proof.
        self.pending_ack_on_disconnect = None
        self.ack_eligible_on_disconnect = False
        self.pending_requests_on_disconnect = ()
        self.request_generation_on_disconnect = None
        self.delivery_generation_on_disconnect = None

    def _current(self):
        requests, delivery = self.prepared.requests, self.prepared.delivery
        return (self.available and requests.connected and delivery.connected
                and delivery.generation == self.generation
                and requests.instance == delivery.instance
                and requests.session > 0
                and self.is_current(requests.instance, self.generation) is True)

    def disconnect(self):
        if self.available:
            requests, delivery = self.prepared.requests, self.prepared.delivery
            self.pending_ack_on_disconnect = delivery.pending
            self.ack_eligible_on_disconnect = delivery.ack_eligible
            self.pending_requests_on_disconnect = tuple(sorted(
                (waiter, identity, token)
                for waiter, (identity, token) in requests.pending.items()))
            if requests.pending_preload is not None:
                self.pending_requests_on_disconnect += (
                    ('PRELOAD', requests.pending_preload, None),)
            if requests.pending_unpreload is not None:
                self.pending_requests_on_disconnect += (
                    ('UNPRELOAD', requests.pending_unpreload, None),)
            if requests.pending_eject is not None:
                self.pending_requests_on_disconnect += (
                    ('EJECT', requests.pending_eject, None),)
            if requests.pending_home is not None:
                self.pending_requests_on_disconnect += (
                    ('HOME', requests.pending_home, None),)
            self.request_generation_on_disconnect = requests.generation
            self.delivery_generation_on_disconnect = delivery.generation
            self.available = False
            self.confirmed_at = None
            requests.disconnect()
            delivery.disconnect()

    def renew(self, timeout=5.0):
        """Confirm one exact heartbeat; any failure permanently closes this lease."""
        if not self._current():
            self.disconnect()
            raise ConnectionError('HOST lease connection is unavailable')
        requests = self.prepared.requests
        try:
            sent_at = self.reactor.monotonic()
            if (not math.isfinite(sent_at) or self.confirmed_at is not None
                    and not self.confirmed_at <= sent_at
                    < self.confirmed_at + self.max_age_s):
                raise ConnectionError('HOST lease expired before renewal')
            reply = send_heartbeat(self.mcu, self.reactor, requests.instance,
                                   requests.session, self._current,
                                   timeout=timeout)
            if not self._current():
                raise ConnectionError('HOST lease connection changed')
            if (not isinstance(reply, CommandResult)
                    or (reply.instance, reply.command, reply.query, reply.result)
                    != (requests.instance, 3, requests.session, 0)):
                raise ValueError('HOST heartbeat was not confirmed OK')
            received_at = self.reactor.monotonic()
            if (not math.isfinite(received_at)
                    or not sent_at <= received_at
                    < sent_at + self.max_age_s):
                raise ConnectionError('HOST heartbeat reply arrived after lease deadline')
            # Firmware refreshed its watchdog before the response reached us.
            # Pre-send time is a conservative lower bound for that refresh.
            self.confirmed_at = sent_at
            return True
        except Exception:
            self.disconnect()
            raise

    def motion_permitted(self, board_ready):
        """Check fresh confirmed lease and live board/actuation readiness."""
        if not callable(board_ready):
            raise TypeError('Live board readiness predicate required')
        if not self._current():
            self.disconnect()
            return False
        now = self.reactor.monotonic()
        if self.confirmed_at is None:
            return False
        if (not math.isfinite(now)
                or not self.confirmed_at <= now
                < self.confirmed_at + self.max_age_s):
            self.disconnect()
            return False
        try:
            ready = board_ready() is True
        except Exception:
            self.disconnect()
            raise
        return ready and self._current()
