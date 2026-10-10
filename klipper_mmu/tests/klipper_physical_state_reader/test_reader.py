# SPDX-License-Identifier: GPL-3.0-or-later
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from host.physical_state_reader import read_physical_state

INSTANCE = 0x1020304050607080
QUERY = 9


def fields(**changes):
    value = dict(instance_hi=INSTANCE >> 32, instance_lo=INSTANCE & 0xffffffff,
                 query=QUERY)
    value.update(changes)
    return value


STATUS = (
    ('km_status_identity', fields(state=0, stage=0, operation=0, client=0,
                                  session=0, request=0, lane=255, owner_lane=255,
                                  occupant_lane=255, flags=0, queued_events=0,
                                  head_event_seq=0)),
    ('km_status_motion', fields(velocity_bits=0, position_hi=0, position_lo=0,
                                buffer_position_bits=0, system_fault=0,
                                motion_fault=0, follower_state=1,
                                follower_direction=1, follower_fault=0)),
    ('km_status_time', fields(commanded_current_ma=0, measured_current_ma=0,
                              measured_current_sample_ms=0, control_ms=400,
                              control_us=400000, acquired_ms=400,
                              acquired_us=400000)),
    ('km_status_sessions', fields(client0=0, client1=2, client2=0, client3=0)),
    ('km_status_requests', fields(client0=0, client1=3, client2=0, client3=0)),
)
SENSORS = (
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
    ('km_sensor_loads0', fields(configured=1, present=0, valid=1, fresh=1,
                                sample0=399, sample1=0, sample2=0, sample3=0)),
    ('km_sensor_loads1', fields(sample4=0, sample5=0, sample6=0, sample7=0)),
)


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
    def __init__(self, mcu, name):
        self.mcu, self.name = mcu, name

    def send(self, args):
        self.mcu.sent.append((self.name, args))
        self.mcu.respond(self.mcu, self.name, args[-1])


class MCU:
    def __init__(self, respond):
        self.respond = respond
        self.listeners = []
        self.sent = []

    def lookup_command(self, message):
        return Command(self, message.split()[0])

    def register_serial_response(self, callback, message):
        wrapper = Wrapper(self, callback, message.split()[0])
        self.listeners.append(wrapper)
        return wrapper

    def emit(self, name, params):
        for wrapper in list(self.listeners):
            if wrapper.name == name:
                wrapper.callback(params)


def normal(mcu, command, part):
    name, params = (STATUS if command == 'km_snapshot_part' else SENSORS)[part]
    mcu.emit(name, params)


class PhysicalStateReaderTest(unittest.TestCase):
    def read(self, respond=normal, current=lambda: True):
        mcu = MCU(respond)
        try:
            return read_physical_state(mcu, Reactor(), INSTANCE, QUERY, current), mcu
        finally:
            self.assertEqual(mcu.listeners, [])

    def test_one_query_yields_status_and_sensors(self):
        (status, sensors), mcu = self.read()
        self.assertEqual((status.instance, sensors.instance), (INSTANCE, INSTANCE))
        self.assertEqual((status.query, sensors.query), (QUERY, QUERY))
        self.assertEqual(status.values['acquired_ms'], sensors.observed_ms)
        self.assertEqual((status.values['follower_state'],
                          status.values['follower_direction'],
                          status.values['follower_fault']), (1, 1, 0))
        self.assertTrue(sensors.bays[0].present)
        self.assertFalse(sensors.bays[1].present)
        self.assertFalse(sensors.toolhead.configured)
        self.assertEqual(mcu.sent, [
            (command, [INSTANCE >> 32, INSTANCE & 0xffffffff, QUERY, part])
            for command, count in (('km_snapshot_part', 5), ('km_sensor_part', 3))
            for part in range(count)])

    def test_capable_physical_capture_includes_loads(self):
        def respond(mcu, command, part):
            mcu.emit(*(STATUS if command == 'km_snapshot_part'
                       else SENSORS + LOADS)[part])
        mcu = MCU(respond)
        status, sensors = read_physical_state(mcu, Reactor(), INSTANCE, QUERY,
                                               lambda: True, load_capable=True)
        self.assertEqual(status.values['acquired_ms'], sensors.observed_ms)
        self.assertTrue(sensors.loads[0].configured)
        self.assertFalse(sensors.loads[0].present)
        self.assertEqual([args[-1] for name, args in mcu.sent
                          if name == 'km_sensor_part'], list(range(5)))
        self.assertEqual(mcu.listeners, [])

    def test_missing_fragment_and_unrelated_identity_fail(self):
        def missing(mcu, command, part):
            if command == 'km_sensor_part' and part == 2:
                return
            normal(mcu, command, part)
        with self.assertRaisesRegex(ValueError, 'timed out'):
            self.read(missing)

        def unrelated(mcu, command, part):
            name, params = (STATUS if command == 'km_snapshot_part' else SENSORS)[part]
            mcu.emit(name, dict(params, query=QUERY - 1))
        with self.assertRaisesRegex(ValueError, 'timed out'):
            self.read(unrelated)

    def test_rejection_and_malformed_matching_fragment_fail(self):
        def rejected(mcu, command, part):
            mcu.emit('km_command_result', fields(command=8, result=6))
        with self.assertRaisesRegex(ValueError, 'result 6'):
            self.read(rejected)

        def malformed(mcu, command, part):
            name, params = (STATUS if command == 'km_snapshot_part' else SENSORS)[part]
            if command == 'km_sensor_part' and part == 0:
                params = dict(params, lane_count=9)
            mcu.emit(name, params)
        with self.assertRaisesRegex(ValueError, 'lane count'):
            self.read(malformed)

        def inconsistent_time(mcu, command, part):
            name, params = (STATUS if command == 'km_snapshot_part' else SENSORS)[part]
            if command == 'km_sensor_part' and part == 0:
                params = dict(params, observed_ms=401)
            mcu.emit(name, params)
        with self.assertRaisesRegex(ValueError, 'acquisition times differ'):
            self.read(inconsistent_time)

    def test_connection_change_aborts_and_unregisters(self):
        current = [True]
        def disconnect(mcu, command, part):
            if command == 'km_snapshot_part' and part == 1:
                current[0] = False
            normal(mcu, command, part)
        with self.assertRaisesRegex(ValueError, 'connection changed before send'):
            self.read(disconnect, lambda: current[0])


if __name__ == '__main__':
    unittest.main()
