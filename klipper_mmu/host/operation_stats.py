# SPDX-License-Identifier: GPL-3.0-or-later
"""Session-local counts of retained operation terminals with confirmed ACKs."""

from collections import OrderedDict

from .protocol import Event
from .wire import TERMINAL_EVENTS, Client, OpType

_NAMES = {OpType.LOAD: 'load', OpType.UNLOAD: 'unload',
          OpType.CALIBRATE: 'calibrate', OpType.PRELOAD: 'preload',
          OpType.EJECT: 'eject', OpType.HOME: 'home'}
# (operation, event type) -> (operation name, outcome)
_OUTCOMES = {(operation, event_type): (_NAMES[operation], outcome)
             for operation, types in TERMINAL_EVENTS.items()
             for event_type, outcome in zip(types,
                                            ('success', 'cancel', 'failure'))}


class OperationStats:
    """One host process' observations; no persistence or firmware inference."""

    def __init__(self):
        self._counts = {}
        self._last_sequence = OrderedDict()

    def record_acked(self, event):
        """Record one exact terminal after a successful MCU ACK and live proof.

        The caller must provide both guarantees. A serial high-water mark per
        instance prevents duplicate retries without retaining every event.
        """
        if not isinstance(event, Event):
            return False
        outcome = _OUTCOMES.get((event.identity.operation, event.event_type))
        if (outcome is None
                or event.identity.client != Client.HOST
                or not 0 <= event.identity.lane < 8
                or not 0 < event.seq < 1 << 32):
            return False
        last = self._last_sequence.get(event.instance, 0)
        delta = (event.seq - last) & 0xffffffff
        if last and (delta == 0 or delta >= 0x80000000):
            return False
        self._last_sequence[event.instance] = event.seq
        self._last_sequence.move_to_end(event.instance)
        if len(self._last_sequence) > 8:
            self._last_sequence.popitem(last=False)
        key = (outcome[0], event.identity.lane, outcome[1])
        self._counts[key] = self._counts.get(key, 0) + 1
        return True

    def snapshot(self):
        """Return an independent read-only view of the observed totals."""
        return dict(self._counts)
