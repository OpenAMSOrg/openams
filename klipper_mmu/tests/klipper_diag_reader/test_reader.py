# SPDX-License-Identifier: GPL-3.0-or-later
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from host.diag_reader import read_diag

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
    ('km_diag_bay_bounce', fields(count0=1, count1=2, count2=3, count3=4,
                                  count4=5, count5=6, count6=7, count7=8)),
    ('km_diag_bay_idle', fields(edges0=10, edges1=11, edges2=12, edges3=13,
                                edges4=14, edges5=15, edges6=16, edges7=17,
                                hub_edges=20, hub_bounce=21, toolhead_bounce=22,
                                selector_bounce=23)),
    ('km_diag_load_bounce', fields(count0=30, count1=31, count2=32, count3=33,
                                   count4=34, count5=35, count6=36, count7=37)),
    ('km_diag_load_idle', fields(edges0=40, edges1=41, edges2=42, edges3=43,
                                 edges4=44, edges5=45, edges6=46, edges7=47)),
    ('km_diag_tmc0', fields(gstat0=1, gstat1=2, gstat2=3, gstat3=4,
                            drv_status0=100, drv_status1=101,
                            drv_status2=102, drv_status3=103)),
    ('km_diag_tmc1', fields(gstat4=5, gstat5=6, gstat6=7, gstat7=8,
                            drv_status4=104, drv_status5=105,
                            drv_status6=106, drv_status7=107)),
)


class DiagReaderTest(unittest.TestCase):
    def read(self, respond, current=lambda: True):
        mcu = MCU(respond)
        try:
            return read_diag(mcu, Reactor(), INSTANCE, QUERY, current), mcu
        finally:
            self.assertEqual(mcu.listeners, [])

    def test_complete_capture_and_exact_query(self):
        def respond(mcu, part):
            name, params = PARTS[part]
            mcu.emit(name, params)
        snapshot, mcu = self.read(respond)
        self.assertEqual((snapshot.instance, snapshot.query), (INSTANCE, QUERY))
        self.assertEqual(snapshot.bay_bounce, (1, 2, 3, 4, 5, 6, 7, 8))
        self.assertEqual(snapshot.bay_idle_edges, (10, 11, 12, 13, 14, 15, 16, 17))
        self.assertEqual((snapshot.hub_idle_edges, snapshot.hub_bounce,
                          snapshot.toolhead_bounce), (20, 21, 22))
        self.assertEqual(snapshot.load_bounce, (30, 31, 32, 33, 34, 35, 36, 37))
        self.assertEqual(snapshot.load_idle_edges, (40, 41, 42, 43, 44, 45, 46, 47))
        self.assertEqual(snapshot.gstat, (1, 2, 3, 4, 5, 6, 7, 8))
        self.assertEqual(snapshot.drv_status, (100, 101, 102, 103, 104, 105, 106, 107))
        self.assertEqual(mcu.sent, [[INSTANCE >> 32, INSTANCE & 0xffffffff, QUERY, part]
                                    for part in range(6)])
        self.assertEqual(mcu.command_format,
                         'km_diag_part instance_hi=%u instance_lo=%u query=%u part=%c')

    def test_missing_part_or_unrelated_identity_times_out(self):
        def respond(mcu, part):
            name, params = PARTS[part]
            if part == 5:
                params = dict(params, instance_lo=1)
            mcu.emit(name, params)
        with self.assertRaisesRegex(ValueError, 'timed out'):
            self.read(respond)

    def test_rejected_query_is_error(self):
        mcu = MCU(lambda mcu, part: mcu.emit(
            'km_command_result', fields(command=8, result=6)))
        with self.assertRaisesRegex(ValueError, 'result 6'):
            read_diag(mcu, Reactor(), INSTANCE, QUERY, lambda: True)
        self.assertEqual(len(mcu.sent), 1)
        self.assertEqual(mcu.listeners, [])

    def test_stale_controller_result_is_error(self):
        def respond(mcu, part):
            if part == 0:
                mcu.emit('km_command_result', fields(
                    instance_lo=1, command=8, result=2))
        with self.assertRaisesRegex(ValueError, 'stale controller'):
            self.read(respond)

    def test_conflicting_fragment_is_error(self):
        def respond(mcu, part):
            name, params = PARTS[part]
            if part == 0:
                mcu.emit(name, params)
                mcu.emit(name, dict(params, count0=99))
            else:
                mcu.emit(name, params)
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
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
