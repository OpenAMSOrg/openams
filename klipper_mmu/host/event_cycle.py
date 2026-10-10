# SPDX-License-Identifier: GPL-3.0-or-later
"""One explicit retained-event delivery attempt on Klipper's reactor thread."""

from contextlib import nullcontext

from .event_delivery import EventDelivery, send_pending_ack
from .event_reader import peek_event
from .openams_terminal_consumer import consume_terminal
from .requests import Requests
from .wire import OpType


def run_event_cycle(mcu, reactor, delivery, requests, runtime, is_current,
                    timeout=5.0, consumer_boundary=None, stats=None):
    """Return 'empty', 'blocked', or 'acked'; never schedule another cycle.

    The caller must hold the observer's MCU mutex, establish the controller
    instance/session first, and reconcile a retained head when blocked. The
    consumer accepts only a matching settled OpenAMS terminal. Other events
    remain queued for an owner/fault handler; they are never discarded here.
    """
    if (not isinstance(delivery, EventDelivery) or not isinstance(requests, Requests)
            or not callable(is_current) or delivery.instance != requests.instance):
        raise ValueError('Mismatched event-cycle state')
    generation = delivery.generation
    request_generation = requests.generation

    def current():
        return (is_current() and delivery.connected and requests.connected
                and delivery.generation == generation
                and requests.generation == request_generation)

    if not current():
        raise ValueError('Event-cycle connection changed')
    event = peek_event(mcu, reactor, delivery.instance, current, timeout)
    if not current():
        raise ValueError('Stale event-cycle response')
    if event is None:
        if delivery.pending is not None:
            raise ValueError('Retained event disappeared before ACK confirmation; reconcile')
        return 'empty'
    # EventDelivery ignores already ACKed sequences before comparing a pending
    # head. A replayed older head must still be reconciled when an ACK is
    # outstanding; it cannot silently turn the cycle into a blocked result.
    if delivery.pending is not None and event != delivery.pending:
        raise RuntimeError('Queue head changed before ACK confirmation')
    pending_at_start = delivery.pending
    with (consumer_boundary() if consumer_boundary is not None
          else nullcontext()):
        if not current():
            raise ValueError('Stale event-cycle consumer')
        delivery.observe(event, generation, lambda item: (
            requests.accept_preload(item, request_generation)
            if item.identity.operation == OpType.PRELOAD else
            requests.accept_eject(item, request_generation)
            if item.identity.operation == OpType.EJECT else
            requests.accept_home(item, request_generation)
            if item.identity.operation == OpType.HOME else
            requests.accept_unpreload(item, request_generation)
            if item.identity == requests.pending_unpreload else
            consume_terminal(item, requests, request_generation, runtime)),
            ack_eligible=False)
    if not current():
        raise ValueError('Stale event-cycle consumer')
    if pending_at_start is None and delivery.pending == event:
        delivery.complete_consumer_boundary(event, generation)
    if delivery.pending != event or not delivery.ack_eligible:
        return 'blocked'
    send_pending_ack(mcu, reactor, delivery, generation, current, timeout)
    if not current():
        raise ValueError('Stale event-cycle ACK')
    if stats is not None:
        # Accounting is diagnostic. It cannot reverse a confirmed MCU ACK.
        try:
            stats.record_acked(event)
        except Exception:
            pass
    return 'acked'
