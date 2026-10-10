# SPDX-License-Identifier: GPL-3.0-or-later
"""Read one coherent firmware status and sensor capture on Klipper's reactor."""

from .command_transport import exchange
from .protocol import (STATUS_MESSAGES, SENSOR_MESSAGES, SENSOR_WIDE_MESSAGES,
                       SENSOR_LOAD_MESSAGES, SENSOR_WIDE_LOAD_MESSAGES, SENSOR_FORMATS,
                       SENSOR_PART_INDEX, SnapshotAssembler, SensorSnapshotAssembler,
                       decode_command_result)
from .wire import (COMMAND_RESULT, SENSOR_PART, SNAPSHOT_PART, STATUS_REPLIES,
                   Command, CommandCode)


def read_physical_state(mcu, reactor, instance, query, is_current, timeout=5.0,
                        load_capable=False, selector_capable=False, wide_lanes=False):
    """Return (status, sensors) from one MCU capture or raise ValueError.

    Caller owns the shared MCU mutex and supplies a unique query greater than
    the firmware's observed high-water, including timed-out IDs. is_current
    captures connection generation and observed controller instance. This does
    not poll, retry, authorize motion, or infer absence from invalid sensors.
    wide_lanes requests lanes 8-11 (BAYS2/LOADS2), sent only when the profile's
    lane_count exceeds 8; omitting it leaves an 8-lane image's wire unchanged.
    """
    if (type(instance) is not int or not 0 < instance < 1 << 64
            or type(query) is not int or not 0 < query < 1 << 32
            or not callable(is_current) or type(timeout) not in (int, float)
            or not 0 < timeout <= 60):
        raise ValueError('Invalid physical state query arguments')
    status = SnapshotAssembler(instance, query, selector_capable)
    status_names = STATUS_MESSAGES if selector_capable else STATUS_MESSAGES[:-1]
    sensor_names = (SENSOR_MESSAGES + (SENSOR_WIDE_MESSAGES if wide_lanes else ())
                    + (SENSOR_LOAD_MESSAGES if load_capable else ())
                    + (SENSOR_WIDE_LOAD_MESSAGES if load_capable and wide_lanes else ()))
    sensors = SensorSnapshotAssembler(instance, query, load_capable, wide_lanes)
    built = [None, None]
    hi, lo = instance >> 32, instance & 0xffffffff

    def on_part(index, name, params):
        if (params.get('instance_hi'), params.get('instance_lo'),
                params.get('query')) != (hi, lo, query):
            return None
        value = (status if index == 0 else sensors).add(name, params)
        if value is not None:
            built[index] = value
        if built[0] is None or built[1] is None:
            return None
        if built[0].values['acquired_ms'] != built[1].observed_ms:
            raise ValueError('Physical state acquisition times differ')
        return tuple(built)

    def on_result(params):
        if (params.get('command') != Command.SNAPSHOT
                or params.get('query') != query):
            return None
        reply = decode_command_result(params)
        if reply.result in (CommandCode.UNBOUND, CommandCode.INSTANCE_MISMATCH):
            raise ValueError('Physical state stale controller instance')
        if reply.instance == instance:
            raise ValueError('Physical state query rejected with result %d'
                             % reply.result)
        return None

    replies = {}
    for name, fmt in zip(status_names, STATUS_REPLIES[:len(status_names)]):
        replies[fmt] = lambda params, name=name: on_part(0, name, params)
    for name in sensor_names:
        replies[SENSOR_FORMATS[name]] = lambda params, name=name: on_part(1, name, params)
    replies[COMMAND_RESULT] = on_result
    sends = [(SNAPSHOT_PART, [hi, lo, query, part])
             for part in range(len(status_names))]
    sends += [(SENSOR_PART, [hi, lo, query, SENSOR_PART_INDEX[name]])
              for name in sensor_names]
    return exchange(mcu, reactor, sends, replies, is_current, timeout,
                    'physical state')
