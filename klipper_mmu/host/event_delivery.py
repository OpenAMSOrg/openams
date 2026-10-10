# SPDX-License-Identifier: GPL-3.0-or-later
"""Deliver a retained MCU event before acknowledging its exact queue head.

The caller owns when to peek, the connection generation, and reconciliation.
All methods run on Klipper's reactor thread. A consumer must return true only
after it has accepted the event; this helper never infers operation completion.
"""

from .command_transport import exchange
from .protocol import (Event, Identity, decode_command_result, decode_event,
                       event_supported)
from .wire import COMMAND_RESULT, EVENT_ACK, EVENT_PEEK, Command, CommandCode


class EventDelivery:
    def __init__(self, instance, generation):
        if type(instance) is not int or not 0 < instance < 1 << 64:
            raise ValueError("Invalid controller instance")
        if type(generation) is not int or generation < 1:
            raise ValueError("Invalid connection generation")
        self.instance = instance
        self.generation = generation
        self.connected = True
        self.pending = None
        self.ack_eligible = False
        self.last_acked = 0

    def disconnect(self):
        self.connected = False
        self.generation += 1
        # An accepted event must be reconciled before delivery resumes.
        self.pending = None
        self.ack_eligible = False

    def peek_command(self):
        if not self.connected:
            raise ConnectionError("MMU connection is disconnected")
        return EVENT_PEEK, [self.instance >> 32, self.instance & 0xffffffff]

    def _ack_command(self, event):
        identity = event.identity
        return EVENT_ACK, [self.instance >> 32, self.instance & 0xffffffff,
                           identity.client, identity.session, identity.request,
                           identity.operation, identity.lane, event.seq]

    def observe(self, params, generation, consumer, ack_eligible=True):
        """Return an ACK command only after consumer(event) returns true.

        A repeated eligible head resends its ACK without repeating the side
        effect. A head accepted inside a consumer boundary remains ineligible
        until that boundary completes. A new head requires reconciliation.
        """
        if not self.connected or generation != self.generation:
            return None
        if isinstance(params, Event):
            event = params
            if event.instance != self.instance:
                return None
            identity = event.identity
            fields = ((event.instance, 64, True), (event.seq, 32, True),
                      (event.timestamp_ms, 32, False),
                      (event.event_type, 8, False),
                      (event.result_code, 8, False), (event.value, 32, False),
                      (event.flags, 8, False))
            if not isinstance(identity, Identity):
                raise ValueError("Invalid retained event identity")
            fields += ((identity.client, 8, False), (identity.session, 32, False),
                       (identity.request, 32, False),
                       (identity.operation, 8, False), (identity.lane, 8, False))
            if any(type(value) is not int or not int(nonzero) <= value < 1 << bits
                   for value, bits, nonzero in fields):
                raise ValueError("Invalid retained event field")
        else:
            if (params.get("instance_hi"), params.get("instance_lo")) != (
                    self.instance >> 32, self.instance & 0xffffffff):
                return None
            event = decode_event(params)
        if not event_supported(event):
            raise ValueError("Unsupported retained event")
        # Serial arithmetic preserves ordering when the firmware wraps from
        # UINT32_MAX to 1 (zero is skipped). One queue holds at most 16 events.
        delta = (event.seq - self.last_acked) & 0xffffffff
        if self.last_acked and (delta == 0 or delta >= 0x80000000):
            return None
        if self.pending is not None:
            if event != self.pending:
                raise RuntimeError("Queue head changed before ACK confirmation")
            return self._ack_command(event) if self.ack_eligible else None
        if not callable(consumer):
            raise TypeError("Event consumer must be callable")
        accepted = consumer(event)
        if not accepted:
            return None
        if not self.connected or generation != self.generation:
            return None
        self.pending = event
        self.ack_eligible = ack_eligible
        return self._ack_command(event) if ack_eligible else None

    def complete_consumer_boundary(self, event, generation):
        """Permit ACK only for the exact head accepted by a completed boundary."""
        if (not self.connected or generation != self.generation
                or self.pending != event):
            raise ValueError("Consumer boundary pending event changed")
        self.ack_eligible = True

    def acknowledge(self, params, generation):
        """Accept only the matching successful km_event_ack administrative reply."""
        if not self.connected or generation != self.generation:
            return False
        if (params.get("instance_hi"), params.get("instance_lo")) != (
                self.instance >> 32, self.instance & 0xffffffff):
            return False
        reply = decode_command_result(params)
        if (reply.command != Command.EVENT_ACK or self.pending is None
                or not self.ack_eligible or reply.query != self.pending.seq):
            return False
        if reply.result != CommandCode.OK:
            # STALE or other failure needs a fresh peek/reconciliation.
            return False
        self.last_acked = self.pending.seq
        self.pending = None
        self.ack_eligible = False
        return True


def send_pending_ack(mcu, reactor, delivery, generation, is_current, timeout=5.0):
    """Send the ACK for an already accepted head and confirm its MCU reply.

    Run on the reactor thread with the caller's shared MCU mutex held.
    The pending event must have come from observe() after consumer acceptance.
    A rejected ACK leaves it pending for caller reconciliation.
    """
    if (not isinstance(delivery, EventDelivery) or not callable(is_current)
            or timeout <= 0 or generation != delivery.generation
            or not delivery.connected or delivery.pending is None
            or not delivery.ack_eligible):
        raise ValueError("No current accepted event to acknowledge")
    command_format, args = delivery._ack_command(delivery.pending)
    instance, seq = delivery.instance, delivery.pending.seq

    def current():
        return (delivery.connected and generation == delivery.generation
                and is_current())

    def on_result(params):
        if (params.get("instance_hi"), params.get("instance_lo"),
                params.get("command"), params.get("query")) != (
                instance >> 32, instance & 0xffffffff, Command.EVENT_ACK, seq):
            return None
        return decode_command_result(params)

    reply = exchange(mcu, reactor, [(command_format, args)],
                     {COMMAND_RESULT: on_result}, current, timeout, "ACK")
    if reply.result != CommandCode.OK:
        raise ValueError("ACK rejected with result %d" % reply.result)
    # acknowledge checks the exact pending sequence and generation again.
    if not delivery.acknowledge({"instance_hi": instance >> 32,
                                 "instance_lo": instance & 0xffffffff,
                                 "command": Command.EVENT_ACK, "query": seq,
                                 "result": reply.result}, generation):
        raise ValueError("ACK pending event changed")
    return True
