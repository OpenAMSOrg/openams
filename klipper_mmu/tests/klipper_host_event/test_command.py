# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from host import KlipperMMU
spec = importlib.util.spec_from_file_location(
    'terminal_test_fixture', ROOT / 'tests/klipper_host_terminal/test_command.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
CommandError, Gcmd, MCU, Printer, event = (
    fixture.CommandError, fixture.Gcmd, fixture.MCU, fixture.Printer, fixture.event)


class EventCommandTest(unittest.TestCase):
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
        self.extra._observe = lambda: self.observed.append(True)

    def test_head_and_empty_are_read_only(self):
        self.extra._mcu.respond = lambda m: m.emit('km_event', event())
        gcmd = Gcmd()
        self.extra.cmd_KLIPPER_MMU_EVENT(gcmd)
        self.assertEqual(self.observed, [True])
        self.assertEqual(self.extra._mcu.sent, [[1, 2]])
        self.assertIn('seq=9', gcmd.responses[0])
        self.assertIn('value=7', gcmd.responses[0])
        self.assertFalse(self.extra._mcu_lock.locked())
        self.assertEqual(self.extra._mcu.wrappers, [])

        self.extra._mcu.respond = lambda m: m.emit('km_command_result', dict(
            instance_hi=1, instance_lo=2, command=5, query=0, result=5))
        empty = Gcmd()
        self.extra.cmd_KLIPPER_MMU_EVENT(empty)
        self.assertIn('queue_empty=true', empty.responses[0])
        self.assertEqual(self.extra._mcu.wrappers, [])

    def test_disconnect_during_send_drops_reply_and_cleans_up(self):
        def disconnect(m):
            self.extra._generation += 1
            self.extra._connected = False
            m.emit('km_event', event())
        self.extra._mcu.respond = disconnect
        with self.assertRaisesRegex(CommandError, 'Stale event peek response'):
            self.extra.cmd_KLIPPER_MMU_EVENT(Gcmd())
        self.assertFalse(self.extra._mcu_lock.locked())
        self.assertEqual(self.extra._mcu.wrappers, [])

    def test_guards_prevent_send(self):
        self.extra._active = False
        with self.assertRaisesRegex(CommandError, 'active MCU connection'):
            self.extra.cmd_KLIPPER_MMU_EVENT(Gcmd())
        self.extra._active = True
        self.extra._mcu.is_fileoutput = lambda: True
        with self.assertRaisesRegex(CommandError, 'active MCU connection'):
            self.extra.cmd_KLIPPER_MMU_EVENT(Gcmd())
        self.assertEqual(self.extra._mcu.sent, [])

    def test_unattached_or_malformed_head_is_never_published(self):
        self.extra._state = 'unbound'
        with self.assertRaisesRegex(CommandError, 'state attached'):
            self.extra.cmd_KLIPPER_MMU_EVENT(Gcmd())
        self.assertEqual(self.extra._mcu.sent, [])
        self.extra._state = 'attached'
        self.extra._mcu.respond = lambda m: m.emit('km_event', event(seq=0))
        gcmd = Gcmd()
        with self.assertRaisesRegex(CommandError, 'Event sequence'):
            self.extra.cmd_KLIPPER_MMU_EVENT(gcmd)
        self.assertEqual(gcmd.responses, [])
        self.assertEqual(self.extra._mcu.wrappers, [])


if __name__ == '__main__':
    unittest.main()
