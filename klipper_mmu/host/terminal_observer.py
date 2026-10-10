# SPDX-License-Identifier: GPL-3.0-or-later
"""Read a retained MCU terminal by full identity, without changing MCU state."""

from .command_transport import exchange
from .protocol import Event, Identity, decode_command_result, decode_event
from .wire import (COMMAND_RESULT, EVENT, TERMINAL_EVENTS, TERMINAL_QUERY,
                   Command, CommandCode, EventType, OpType)

_TERMINAL_TYPES = dict(TERMINAL_EVENTS)
_TERMINAL_TYPES[OpType.FOLLOW] = (EventType.FOLLOW_FAULT, EventType.STOPPED)


def query_terminal(mcu, reactor, instance, identity, is_current, timeout=5.0):
    """Return an Event, or None if the MCU has no retained terminal.

    Call on Klipper's reactor thread with the host's shared MCU mutex held.
    is_current must capture the connection generation and return false if the
    connection or observed instance changes while send/wait yields. A timeout,
    malformed matching reply, or lost connection raises ValueError. The caller
    applies the operation matcher before delivering any OpenAMS completion.
    """
    if (type(instance) is not int or not 0 < instance < 1 << 64
            or not isinstance(identity, Identity)
            or type(identity.client) is not int or not 0 <= identity.client < 256
            or type(identity.session) is not int or not 0 < identity.session < 1 << 32
            or type(identity.request) is not int or not 0 < identity.request < 1 << 32
            or type(identity.operation) is not int
            or identity.operation not in _TERMINAL_TYPES
            or type(identity.lane) is not int or not 0 <= identity.lane < 256
            or not callable(is_current) or timeout <= 0):
        raise ValueError('Invalid terminal query identity')
    hi, lo = instance >> 32, instance & 0xffffffff
    fields = (hi, lo, identity.client, identity.session, identity.request,
              identity.operation, identity.lane)

    def on_event(params):
        # Ignore events from an older instance or an unrelated request. Check
        # their identity before decoding the remaining fields as ours.
        if tuple(params.get(key) for key in (
                'instance_hi', 'instance_lo', 'client', 'session', 'request',
                'operation', 'lane')) != fields:
            return None
        event = decode_event(params)
        if event.event_type not in _TERMINAL_TYPES[identity.operation]:
            raise ValueError('Unexpected terminal event type')
        return event

    def on_result(params):
        if (params.get('command') != Command.TERMINAL
                or params.get('query') != identity.request
                or (params.get('instance_hi'), params.get('instance_lo')) != (hi, lo)):
            return None
        result = decode_command_result(params)
        if result.result != CommandCode.STALE:
            raise ValueError('Terminal query rejected with result %d' % result.result)
        return result

    found = exchange(mcu, reactor, [(TERMINAL_QUERY, list(fields))],
                     {EVENT: on_event, COMMAND_RESULT: on_result}, is_current,
                     timeout, 'terminal')
    return found if isinstance(found, Event) else None
