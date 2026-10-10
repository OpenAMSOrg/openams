# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded preparation of a cold HOST session on the reactor thread.

The caller owns fresh snapshot acquisition, abandoned waiter reconciliation,
and exclusive use of the command response channel. A heartbeat establishes
host liveness only; firmware still arbitrates ownership at the first START.
"""

from dataclasses import dataclass

from .command_transport import send_heartbeat
from .event_delivery import EventDelivery
from .protocol import CommandResult
from .requests import Requests
from .wire import Command, CommandCode
from .session_reconciliation import (choose_host_session_with_physical,
                                     choose_occupied_host_session,
                                     choose_preloaded_host_session)


@dataclass(frozen=True)
class PreparedHostSession:
    requests: Requests
    delivery: EventDelivery
    snapshot_query: int


@dataclass(frozen=True)
class PreparedOccupiedHostSession:
    prepared: PreparedHostSession
    lane: int


def prepare_cold_host_session(mcu, reactor, status, sensors, instance,
                              expected_bays, generation, is_current,
                              timeout=5.0, virtual_hub=False):
    """Return fresh host helpers after one confirmed heartbeat exchange.

    is_current(instance, generation) must verify the captured connection
    identity and generation. A timeout leaves heartbeat outcome ambiguous and
    must be reconciled by the caller before another attempt.
    """
    _check_guard(instance, generation, is_current)
    selected = choose_host_session_with_physical(
        status, sensors, instance, expected_bays, virtual_hub=virtual_hub)
    return _confirm_host_session(mcu, reactor, instance, generation,
                                 is_current, selected, timeout)


def prepare_occupied_host_session(mcu, reactor, status, sensors, instance,
                                  expected_bays, generation, is_current,
                                  timeout=5.0, virtual_hub=False):
    """Confirm a new HOST session for one physically proven LOADED lane."""
    _check_guard(instance, generation, is_current)
    selected = choose_occupied_host_session(
        status, sensors, instance, expected_bays, virtual_hub=virtual_hub)
    prepared = _confirm_host_session(
        mcu, reactor, instance, generation, is_current,
        selected.session_start, timeout)
    return PreparedOccupiedHostSession(prepared, selected.lane)


def prepare_preloaded_host_session(mcu, reactor, status, sensors, instance,
                                   expected_bays, generation, is_current,
                                   timeout=5.0, virtual_hub=False):
    """Confirm a new HOST session for one quiescent, lane-local PRELOAD.

    The caller must exclude an active lease and settle old waiters/ACKs before
    calling. A timeout has an ambiguous outcome and requires reconciliation.
    The returned PreparedOccupiedHostSession holds a local reservation lane,
    not proof that filament reached the shared hub.
    """
    _check_guard(instance, generation, is_current)
    selected = choose_preloaded_host_session(
        status, sensors, instance, expected_bays, virtual_hub=virtual_hub)
    prepared = _confirm_host_session(
        mcu, reactor, instance, generation, is_current,
        selected.session_start, timeout)
    return PreparedOccupiedHostSession(prepared, selected.lane)


def _check_guard(instance, generation, is_current):
    if type(generation) is not int or generation < 1 or not callable(is_current):
        raise ValueError('Invalid connection generation or guard')
    if is_current(instance, generation) is not True:
        raise ValueError('MMU connection changed before reconciliation')


def _confirm_host_session(mcu, reactor, instance, generation, is_current,
                          selected, timeout):

    def guarded():
        return is_current(instance, generation) is True

    if not guarded():
        raise ValueError('MMU connection changed before heartbeat')
    reply = send_heartbeat(mcu, reactor, instance, selected.session,
                           guarded, timeout=timeout)
    if not guarded():
        raise ValueError('MMU connection changed after heartbeat')
    if (not isinstance(reply, CommandResult) or reply.instance != instance
            or reply.command != Command.HEARTBEAT
            or reply.query != selected.session
            or reply.result != CommandCode.OK):
        raise ValueError('HOST heartbeat was not confirmed OK')
    return PreparedHostSession(
        Requests(instance, selected.session, selected.last_request),
        EventDelivery(instance, generation), selected.snapshot_query)
