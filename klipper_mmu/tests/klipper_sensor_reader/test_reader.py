# SPDX-License-Identifier: GPL-3.0-or-later
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from host.sensor_reader import read_sensors

INSTANCE = 0x1020304050607080
QUERY = 9


class Completion:
    def __init__(self):
        self.value = None

    def test(self):
        return self.value is not None

    def complete(self, value):
        self.value = value

    def wait(self, deadline):
        return self.value


class Reactor:
    time = 1.0

    def completion(self):
        return Completion()

    def monotonic(self):
        return self.time

    def register_async_callback(self, callback):
        callback(self.time)


class Wrapper:
    def __init__(self, mcu, callback, name):
        self.mcu, self.callback, self.name = mcu, callback, name

    def unregister(self):
        self.mcu.listeners.remove(self)


class Command:
    def __init__(self, mcu):
        self.mcu = mcu

    def send(self, args):
        self.mcu.sent.append(args)
        self.mcu.respond(self.mcu, args[-1])


class MCU:
    def __init__(self, respond):
        self.respond = respond
        self.listeners = []
        self.sent = []

    def lookup_command(self, message):
        self.command_format = message
        return Command(self)

    def register_serial_response(self, callback, message):
        wrapper = Wrapper(self, callback, message.split()[0])
        self.listeners.append(wrapper)
        return wrapper

    def emit(self, name, params):
        for wrapper in list(self.listeners):
            if wrapper.name == name:
                wrapper.callback(params)


def fields(**changes):
    params = dict(instance_hi=INSTANCE >> 32, instance_lo=INSTANCE & 0xffffffff,
                  query=QUERY)
    params.update(changes)
    return params


PARTS = (
    ('km_sensor_flags', fields(lane_count=2, observed_ms=400,
                               configured=0x103, present=0x101,
                               valid=0x103, fresh=0x103)),
    ('km_sensor_bays0', fields(sample0=390, sample1=391,
                               sample2=0, sample3=0)),
    ('km_sensor_bays1', fields(sample4=0, sample5=0, sample6=0,
                               sample7=0, hub_sample_ms=392,
                               toolhead_sample_ms=0)),
)
LOADS = (
    ('km_sensor_loads0', fields(configured=1, present=1, valid=1, fresh=1,
                                sample0=393, sample1=0, sample2=0, sample3=0)),
    ('km_sensor_loads1', fields(sample4=0, sample5=0, sample6=0, sample7=0)),
)


class SensorReaderTest(unittest.TestCase):
    def read(self, respond, current=lambda: True):
        mcu = MCU(respond)
        try:
            return read_sensors(mcu, Reactor(), INSTANCE, QUERY, current), mcu
        finally:
            self.assertEqual(mcu.listeners, [])

    def test_complete_capture_and_exact_query(self):
        def respond(mcu, part):
            name, params = PARTS[part]
            mcu.emit(name, params)
        snapshot, mcu = self.read(respond)
        self.assertEqual((snapshot.instance, snapshot.query, snapshot.lane_count),
                         (INSTANCE, QUERY, 2))
        self.assertTrue(snapshot.bays[0].present)
        self.assertFalse(snapshot.bays[1].present)
        self.assertTrue(snapshot.bays[1].valid)
        self.assertEqual(snapshot.hub.sample_ms, 392)
        self.assertFalse(snapshot.toolhead.configured)
        self.assertEqual(mcu.sent, [[INSTANCE >> 32, INSTANCE & 0xffffffff,
                                      QUERY, part] for part in range(3)])
        self.assertEqual(mcu.command_format,
                         'km_sensor_part instance_hi=%u instance_lo=%u '
                         'query=%u part=%c')
        self.assertEqual(len(snapshot.loads), 8)
        self.assertTrue(all(not load.configured and not load.valid
                            for load in snapshot.loads))

    def test_capable_capture_reads_five_parts(self):
        parts = PARTS + LOADS
        mcu = MCU(lambda mcu, part: mcu.emit(*parts[part]))
        snapshot = read_sensors(mcu, Reactor(), INSTANCE, QUERY,
                                lambda: True, load_capable=True)
        self.assertEqual(len(mcu.sent), 5)
        self.assertTrue(snapshot.loads[0].present)
        self.assertEqual(snapshot.loads[0].sample_ms, 393)
        self.assertFalse(snapshot.loads[1].configured)
        self.assertEqual(mcu.listeners, [])

    def test_capable_load_validation(self):
        for change, error in (({'configured': 4}, 'Unused load bits'),
                              ({'sample1': 1}, 'Unconfigured load timestamp'),
                              ({'fresh': 2}, 'Inconsistent load flags')):
            parts = PARTS + ((LOADS[0][0], dict(LOADS[0][1], **change)), LOADS[1])
            mcu = MCU(lambda mcu, part: mcu.emit(*parts[part]))
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, error):
                read_sensors(mcu, Reactor(), INSTANCE, QUERY,
                             lambda: True, load_capable=True)
            self.assertEqual(mcu.listeners, [])

    def test_missing_part_or_unrelated_identity_times_out(self):
        def respond(mcu, part):
            name, params = PARTS[part]
            if part == 2:
                params = dict(params, instance_lo=1)
            mcu.emit(name, params)
        with self.assertRaisesRegex(ValueError, 'timed out'):
            self.read(respond)

    def test_rejected_query_is_error(self):
        mcu = MCU(lambda mcu, part: mcu.emit(
            'km_command_result', fields(command=8, result=6)))
        with self.assertRaisesRegex(ValueError, 'result 6'):
            read_sensors(mcu, Reactor(), INSTANCE, QUERY, lambda: True)
        self.assertEqual(len(mcu.sent), 1)
        self.assertEqual(mcu.listeners, [])

    def test_stale_controller_result_is_error(self):
        def respond(mcu, part):
            if part == 0:
                mcu.emit('km_command_result', fields(
                    instance_lo=1, command=8, result=2))
        with self.assertRaisesRegex(ValueError, 'stale controller'):
            self.read(respond)

    def test_malformed_matching_part_is_error(self):
        def respond(mcu, part):
            name, params = PARTS[part]
            if part == 0:
                params = dict(params, lane_count=9)
            mcu.emit(name, params)
        with self.assertRaisesRegex(ValueError, 'lane count'):
            self.read(respond)

    def test_disconnect_during_exchange_is_error(self):
        current = [True]
        def respond(mcu, part):
            if part == 1:
                current[0] = False
            name, params = PARTS[part]
            mcu.emit(name, params)
        with self.assertRaisesRegex(ValueError, 'connection changed before send'):
            self.read(respond, lambda: current[0])


if __name__ == '__main__':
    unittest.main()
