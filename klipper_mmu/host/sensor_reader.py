# SPDX-License-Identifier: GPL-3.0-or-later
"""Read one coherent MMU sensor capture through Klipper's public MCU API."""

from .command_transport import exchange
from .protocol import (SENSOR_MESSAGES, SENSOR_WIDE_MESSAGES, SENSOR_LOAD_MESSAGES,
                       SENSOR_WIDE_LOAD_MESSAGES, SENSOR_FORMATS, SENSOR_PART_INDEX,
                       SensorSnapshotAssembler, decode_command_result)
from .wire import COMMAND_RESULT, SENSOR_PART, Command, CommandCode


def read_sensors(mcu, reactor, instance, query, is_current, timeout=5.0,
                 load_capable=False, wide_lanes=False):
    """Return one complete, typed sensor observation or raise ValueError.

    Run on the reactor thread with the caller's shared MCU mutex held. The
    caller first observes the firmware query high-water mark and consumes a
    unique query ID greater than both it and locally used IDs, including timed
    out IDs. is_current must capture connection generation and MCU instance.
    Unknown or stale sensor values stay explicitly invalid in the result.
    wide_lanes requests lanes 8-11 (BAYS2/LOADS2), sent only when the profile's
    lane_count exceeds 8; omitting it leaves an 8-lane image's wire unchanged.
    """
    if (type(instance) is not int or not 0 < instance < 1 << 64
            or type(query) is not int or not 0 < query < 1 << 32
            or not callable(is_current) or type(timeout) not in (int, float)
            or not 0 < timeout <= 60):
        raise ValueError('Invalid sensor query arguments')
    names = (SENSOR_MESSAGES + (SENSOR_WIDE_MESSAGES if wide_lanes else ())
            + (SENSOR_LOAD_MESSAGES if load_capable else ())
            + (SENSOR_WIDE_LOAD_MESSAGES if load_capable and wide_lanes else ()))
    assembler = SensorSnapshotAssembler(instance, query, load_capable, wide_lanes)
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
            raise ValueError('Sensor query stale controller instance')
        if reply.instance == instance:
            raise ValueError('Sensor query rejected with result %d' % reply.result)
        return None

    replies = {SENSOR_FORMATS[name]: (lambda params, name=name: on_part(name, params))
               for name in names}
    replies[COMMAND_RESULT] = on_result
    return exchange(mcu, reactor,
                    [(SENSOR_PART, [hi, lo, query, SENSOR_PART_INDEX[name]])
                     for name in names],
                    replies, is_current, timeout, 'sensor query')
