# SPDX-License-Identifier: GPL-3.0-or-later
"""Read one retained queue head through Klipper's public MCU command API."""

from .command_transport import exchange
from .protocol import Event, decode_command_result, decode_event, event_supported
from .wire import COMMAND_RESULT, EVENT, EVENT_PEEK, Command, CommandCode


def peek_event(mcu, reactor, instance, is_current, timeout=5.0):
    """Return a retained Event or None when the queue is empty.

    Run on the reactor thread with the caller's shared MCU mutex held.
    is_current must capture connection generation and instance across the wait.
    A connection change, rejected command, malformed head, or timeout raises
    ValueError. The caller must reconcile an event before separately ACKing it.
    """
    if (type(instance) is not int or not 0 < instance < 1 << 64
            or not callable(is_current) or timeout <= 0):
        raise ValueError('Invalid event peek arguments')
    hi, lo = instance >> 32, instance & 0xffffffff

    def on_event(params):
        if (params.get('instance_hi'), params.get('instance_lo')) != (hi, lo):
            return None
        event = decode_event(params)
        if not event_supported(event):
            raise ValueError('Unsupported retained event')
        return event

    def on_result(params):
        if (params.get('command') != Command.EVENT_PEEK
                or params.get('query') != 0):
            return None
        reply = decode_command_result(params)
        if reply.result in (CommandCode.UNBOUND, CommandCode.INSTANCE_MISMATCH):
            # The firmware reports its *current* instance, which can differ
            # from the requested instance for UNBOUND or INSTANCE_MISMATCH.
            raise ValueError('Event peek stale controller instance')
        if reply.instance != instance:
            return None
        if reply.result != CommandCode.EMPTY:
            raise ValueError('Event peek rejected with result %d' % reply.result)
        return reply

    head = exchange(mcu, reactor, [(EVENT_PEEK, [hi, lo])],
                    {EVENT: on_event, COMMAND_RESULT: on_result}, is_current,
                    timeout, 'event peek')
    return head if isinstance(head, Event) else None
