# SPDX-License-Identifier: GPL-3.0-or-later
"""Read one switch bounce/idle-edge and TMC gstat/drv_status capture
(docs/diagnostics.md) through Klipper's public MCU API. Shares the firmware's
snapshot/sensor query high-water; a caller may reuse a query already used for
a fresh KLIPPER_MMU_PHYSICAL capture in the same exchange window."""

from .command_transport import exchange
from .protocol import DIAG_MESSAGES, DiagSnapshotAssembler, decode_command_result
from .wire import COMMAND_RESULT, DIAG_PART, DIAG_REPLIES, Command, CommandCode


def read_diag(mcu, reactor, instance, query, is_current, timeout=5.0):
    """Return one complete DiagSnapshot or raise ValueError.

    Run on the reactor thread with the caller's shared MCU mutex held, the
    same way read_sensors/read_physical_state are. Requires firmware's
    KLIPPER_MMU_DIAG_COUNTERS; the caller checks that constant first.
    """
    if (type(instance) is not int or not 0 < instance < 1 << 64
            or type(query) is not int or not 0 < query < 1 << 32
            or not callable(is_current) or type(timeout) not in (int, float)
            or not 0 < timeout <= 60):
        raise ValueError('Invalid diag query arguments')
    assembler = DiagSnapshotAssembler(instance, query)
    hi, lo = instance >> 32, instance & 0xffffffff

    def on_part(name, params):
        if ((params.get('instance_hi'), params.get('instance_lo'),
             params.get('query')) != (hi, lo, query)):
            return None
        return assembler.add(name, params)

    def on_result(params):
        if (params.get('command') != Command.SNAPSHOT
                or params.get('query') != query):
            return None
        reply = decode_command_result(params)
        if reply.result in (CommandCode.UNBOUND, CommandCode.INSTANCE_MISMATCH):
            raise ValueError('Diag query stale controller instance')
        if reply.instance == instance:
            raise ValueError('Diag query rejected with result %d' % reply.result)
        return None

    replies = {fmt: lambda params, name=name: on_part(name, params)
               for name, fmt in zip(DIAG_MESSAGES, DIAG_REPLIES)}
    replies[COMMAND_RESULT] = on_result
    return exchange(mcu, reactor,
                    [(DIAG_PART, [hi, lo, query, part])
                     for part in range(len(DIAG_MESSAGES))],
                    replies, is_current, timeout, 'diag query')
