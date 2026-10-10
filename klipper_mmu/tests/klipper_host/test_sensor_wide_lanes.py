# SPDX-License-Identifier: GPL-3.0-or-later
"""Host assembly of an 8-lane and a 12-lane sensor capture (BAYS2/LOADS2)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from host.sensor_reader import read_sensors
from host.physical_state_reader import read_physical_state

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


class MCU:
    """Records every sent km_*_part request and dispatches a scripted reply."""
    def __init__(self, respond):
        self.respond = respond
        self.listeners = []
        self.sent = []

    def lookup_command(self, message):
        return _Command(self)

    def register_serial_response(self, callback, message):
        wrapper = _Wrapper(self, callback, message.split()[0])
        self.listeners.append(wrapper)
        return wrapper

    def emit(self, name, params):
        for wrapper in list(self.listeners):
            if wrapper.name == name:
                wrapper.callback(params)


class _Wrapper:
    def __init__(self, mcu, callback, name):
        self.mcu, self.callback, self.name = mcu, callback, name

    def unregister(self):
        self.mcu.listeners.remove(self)


class _Command:
    def __init__(self, mcu):
        self.mcu = mcu

    def send(self, args):
        self.mcu.sent.append(args)
        self.mcu.respond(self.mcu, args[-1])


def fields(**changes):
    params = dict(instance_hi=INSTANCE >> 32, instance_lo=INSTANCE & 0xffffffff, query=QUERY)
    params.update(changes)
    return params


# The firmware's km_sensor_part enum: FLAGS, BAYS0, BAYS1, LOADS0, LOADS1,
# BAYS2, LOADS2. BAYS2/LOADS2 (parts 5-6) carry lanes 8-11.
WIDE_PARTS = {
    0: ('km_sensor_flags', fields(lane_count=12, observed_ms=500,
                                  configured=0x1f01, present=0x1501,
                                  valid=0x0501, fresh=0x0101)),
    1: ('km_sensor_bays0', fields(sample0=1, sample1=0, sample2=0, sample3=0)),
    2: ('km_sensor_bays1', fields(sample4=0, sample5=0, sample6=0, sample7=0,
                                  hub_sample_ms=99, toolhead_sample_ms=0)),
    3: ('km_sensor_loads0', fields(configured=0, present=0, valid=0, fresh=0,
                                   sample0=0, sample1=0, sample2=0, sample3=0)),
    4: ('km_sensor_loads1', fields(sample4=0, sample5=0, sample6=0, sample7=0)),
    5: ('km_sensor_bays2', fields(sample8=0x11111111, sample9=0,
                                  sample10=0x22222222, sample11=0)),
    6: ('km_sensor_loads2', fields(configured=0x0b, present=0x09, valid=0x03,
                                   fresh=0x01, sample8=0x33333333, sample9=0,
                                   sample10=0, sample11=0x44444444)),
}


def wide_respond(mcu, part):
    name, params = WIDE_PARTS[part]
    mcu.emit(name, params)


class SensorWideLanesTest(unittest.TestCase):
    def test_read_sensors_assembles_a_12_lane_wide_capture(self):
        mcu = MCU(wide_respond)
        snapshot = read_sensors(mcu, Reactor(), INSTANCE, QUERY, lambda: True,
                                load_capable=True, wide_lanes=True)
        self.assertEqual({args[-1] for args in mcu.sent}, {0, 1, 2, 3, 4, 5, 6})
        self.assertEqual(snapshot.lane_count, 12)
        self.assertEqual(len(snapshot.bays), 12)
        self.assertEqual(len(snapshot.loads), 12)
        self.assertEqual((snapshot.bays[8].configured, snapshot.bays[8].present,
                          snapshot.bays[8].valid, snapshot.bays[8].fresh,
                          snapshot.bays[8].sample_ms), (True, True, True, False, 0x11111111))
        self.assertEqual((snapshot.bays[9].configured, snapshot.bays[9].present),
                         (True, False))
        self.assertEqual((snapshot.bays[10].present, snapshot.bays[10].valid,
                          snapshot.bays[10].sample_ms), (True, False, 0x22222222))
        self.assertEqual((snapshot.bays[11].configured, snapshot.bays[11].sample_ms),
                         (False, 0))
        self.assertEqual((snapshot.loads[8].configured, snapshot.loads[8].present,
                          snapshot.loads[8].valid, snapshot.loads[8].fresh,
                          snapshot.loads[8].sample_ms), (True, True, True, True, 0x33333333))
        self.assertEqual((snapshot.loads[9].present, snapshot.loads[9].valid),
                         (False, True))
        self.assertEqual(snapshot.loads[11].sample_ms, 0x44444444)

    def test_read_physical_state_assembles_status_and_a_12_lane_capture(self):
        status_parts = {
            0: ('km_status_identity', fields(state=0, stage=0, operation=0, client=0,
                                             session=0, request=0, lane=0, owner_lane=0,
                                             occupant_lane=0, flags=0, queued_events=0,
                                             head_event_seq=0)),
            1: ('km_status_motion', fields(velocity_bits=0, position_hi=0, position_lo=0,
                                           buffer_position_bits=0, system_fault=0,
                                           motion_fault=0, follower_state=0,
                                           follower_direction=0, follower_fault=0)),
            2: ('km_status_time', fields(commanded_current_ma=0, measured_current_ma=0,
                                         measured_current_sample_ms=0, control_ms=0,
                                         control_us=0, acquired_ms=500, acquired_us=0)),
            3: ('km_status_sessions', fields(client0=0, client1=0, client2=0, client3=0)),
            4: ('km_status_requests', fields(client0=0, client1=0, client2=0, client3=0)),
        }

        def respond(mcu, command, part):
            mcu.emit(*(status_parts[part] if command == 'km_snapshot_part' else WIDE_PARTS[part]))

        class TwoCommandMCU(MCU):
            def lookup_command(self, message):
                return _TwoArgCommand(self, message.split()[0])

        class _TwoArgCommand:
            def __init__(self, mcu, name):
                self.mcu, self.name = mcu, name

            def send(self, args):
                self.mcu.sent.append((self.name, args))
                self.mcu.respond(self.mcu, self.name, args[-1])

        mcu = TwoCommandMCU(respond)
        status, sensors = read_physical_state(mcu, Reactor(), INSTANCE, QUERY, lambda: True,
                                              load_capable=True, wide_lanes=True)
        self.assertEqual(status.values['acquired_ms'], sensors.observed_ms)
        self.assertEqual(sensors.lane_count, 12)
        self.assertEqual(sensors.loads[11].sample_ms, 0x44444444)
        self.assertEqual({args[-1] for name, args in mcu.sent if name == 'km_sensor_part'},
                         {0, 1, 2, 3, 4, 5, 6})


if __name__ == '__main__':
    unittest.main()
