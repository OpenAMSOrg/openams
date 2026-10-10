# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from host import KlipperMMU


class CommandError(Exception):
    pass


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
    def completion(self):
        return Completion()

    def monotonic(self):
        return 1.0

    def register_async_callback(self, callback):
        callback(1.0)


class Printer:
    command_error = CommandError

    def __init__(self):
        self.reactor = Reactor()

    def get_reactor(self):
        return self.reactor


class Gcmd:
    def __init__(self, **params):
        self.params = dict(CLIENT=1, SESSION=3, REQUEST=4,
                           OPERATION=1, LANE=2)
        self.params.update(params)
        self.responses = []

    def get_int(self, name, minval=None, maxval=None):
        value = self.params[name]
        if type(value) is not int or not minval <= value <= maxval:
            raise CommandError('Invalid ' + name)
        return value

    def respond_info(self, text):
        self.responses.append(text)


class Wrapper:
    def __init__(self, mcu, callback, name):
        self.mcu, self.callback, self.name = mcu, callback, name

    def unregister(self):
        self.mcu.wrappers.remove(self)


class MCU:
    def __init__(self):
        self.wrappers = []
        self.sent = []
        self.respond = None

    def is_fileoutput(self):
        return False

    def lookup_command(self, fmt):
        self.format = fmt
        mcu = self
        class Cmd:
            def send(self, values):
                mcu.sent.append(values)
                mcu.respond(mcu)
        return Cmd()

    def register_serial_response(self, callback, fmt):
        wrapper = Wrapper(self, callback, fmt.split()[0])
        self.wrappers.append(wrapper)
        return wrapper

    def emit(self, kind, data):
        for wrapper in list(self.wrappers):
            if wrapper.name == kind:
                wrapper.callback(data)


def event(**fields):
    response = dict(instance_hi=1, instance_lo=2, seq=9, timestamp_ms=10,
                    client=1, session=3, request=4, operation=1, lane=2,
                    event_type=1, result_code=0, value=7, flags=0)
    response.update(fields)
    return response


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.extra = KlipperMMU.__new__(KlipperMMU)
        self.extra._printer = Printer()
        self.extra._mcu = MCU()
        self.extra._name = 'box'
        self.extra._mcu_lock = threading.Lock()
        self.extra._active = True
        self.extra._connected = True
        self.extra._state = 'attached'
        self.extra._instance = (1 << 32) | 2
        self.extra._generation = 1
        self.observed = []
        def observe():
            self.observed.append(True)
        self.extra._observe = observe

    def test_matching_event(self):
        self.extra._mcu.respond = lambda m: m.emit('km_event', event())
        gcmd = Gcmd()
        self.extra.cmd_KLIPPER_MMU_TERMINAL(gcmd)
        self.assertEqual(len(self.observed), 1)
        self.assertEqual(self.extra._mcu.sent, [[1, 2, 1, 3, 4, 1, 2]])
        self.assertIn('event_type=1', gcmd.responses[0])
        self.assertIn('value=7', gcmd.responses[0])
        self.assertNotIn('history_miss', gcmd.responses[0])
        self.assertFalse(self.extra._mcu_lock.locked())
        self.assertEqual(self.extra._mcu.wrappers, [])

    def test_evicted_history_stays_unknown(self):
        self.extra._mcu.respond = lambda m: m.emit('km_command_result', dict(
            instance_hi=1, instance_lo=2, command=7, query=4, result=4))
        gcmd = Gcmd()
        self.extra.cmd_KLIPPER_MMU_TERMINAL(gcmd)
        self.assertIn('history_miss=unknown', gcmd.responses[0])

    def test_disconnect_during_send(self):
        def disconnect(m):
            self.extra._generation += 1
            self.extra._connected = False
            m.emit('km_event', event())
        self.extra._mcu.respond = disconnect
        with self.assertRaisesRegex(CommandError, 'Stale terminal response'):
            self.extra.cmd_KLIPPER_MMU_TERMINAL(Gcmd())
        self.assertFalse(self.extra._mcu_lock.locked())
        self.assertEqual(self.extra._mcu.wrappers, [])

    def test_invalid_identity_does_not_observe_or_send(self):
        with self.assertRaises(CommandError):
            self.extra.cmd_KLIPPER_MMU_TERMINAL(Gcmd(LANE=8))
        self.assertFalse(self.observed)
        self.assertEqual(self.extra._mcu.sent, [])

    def test_unattached_does_not_send(self):
        self.extra._state = 'shutdown'
        with self.assertRaisesRegex(CommandError, 'state attached'):
            self.extra.cmd_KLIPPER_MMU_TERMINAL(Gcmd())
        self.assertEqual(self.extra._mcu.sent, [])

    def test_concurrent_commands_wait_for_the_mcu_mutex(self):
        in_send, release, order = threading.Event(), threading.Event(), []

        def respond(m):
            if len(m.sent[-1]) == 7:  # TERMINAL keeps the mutex until its reply.
                in_send.set()
                release.wait(5)
            order.append((len(m.sent[-1]), len(m.wrappers)))
            m.emit('km_event', event())

        self.extra._mcu.respond = respond
        terminal, peek = Gcmd(), Gcmd()
        first = threading.Thread(target=self.extra.cmd_KLIPPER_MMU_TERMINAL,
                                 args=(terminal,))
        second = threading.Thread(target=self.extra.cmd_KLIPPER_MMU_EVENT,
                                  args=(peek,))
        first.start()
        self.assertTrue(in_send.wait(5))
        second.start()
        second.join(0.05)
        self.assertEqual(self.extra._mcu.sent, [[1, 2, 1, 3, 4, 1, 2]])
        release.set()
        first.join(5)
        second.join(5)
        # EVENT sent only after TERMINAL unregistered its two responses.
        self.assertEqual(order, [(7, 2), (2, 2)])
        self.assertIn('event_type=1', terminal.responses[0])
        self.assertIn('seq=9', peek.responses[0])
        self.assertFalse(self.extra._mcu_lock.locked())
        self.assertEqual(self.extra._mcu.wrappers, [])


if __name__ == '__main__':
    unittest.main()
