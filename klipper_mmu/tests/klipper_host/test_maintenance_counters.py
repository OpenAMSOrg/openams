# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from host import KlipperMMU
from host.maintenance_counters import MaintenanceCounters


class MaintenanceCountersTest(unittest.TestCase):
    def test_persistence_copy_bounds_and_failed_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'maintenance.json')
            counts = MaintenanceCounters(path)
            counts.edit('blade', increment=2, limit=2, warning='Replace cutter blade')
            view = counts.status()
            view['blade']['value'] = 90
            self.assertEqual(counts.status()['blade']['value'], 2)
            self.assertEqual(MaintenanceCounters(path).status(), counts.status())
            with open(path, 'rb') as source:
                before = source.read()
            for args in ({'increment': 1000000000}, {'limit': -1},
                         {'warning': 'bad\ntext'}, {'warning': 'x' * 161},
                         {'reset': True, 'increment': 1}):
                with self.assertRaises(ValueError):
                    counts.edit('blade', **args)
            with patch('host.maintenance_counters.os.replace', side_effect=OSError('disk')):
                with self.assertRaises(OSError):
                    counts.edit('blade', reset=True)
            self.assertEqual(counts.status()['blade']['value'], 2)
            with open(path, 'rb') as source:
                self.assertEqual(source.read(), before)

    def test_bad_file_and_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'maintenance.json')
            for payload in (b'{"counters":{"blade":{"value":1,"value":2,"limit":0,"warning":false}}}',
                            b'{"counters":{"blade":{"value":true,"limit":0,"warning":""}}}',
                            b'{"counters":{"blade":{"value":0,"limit":0,"warning":false}}}',
                            b' ' * 8193):
                with open(path, 'wb') as output:
                    output.write(payload)
                with self.assertRaises(ValueError):
                    MaintenanceCounters(path)
            with self.assertRaises(ValueError):
                MaintenanceCounters('relative.json')


class MaintenanceCommandTest(unittest.TestCase):
    def test_opt_in_registration_edits_and_no_mcu_send(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'maintenance.json')

            class Printer:
                config_error = ValueError
                command_error = ValueError

                def __init__(self):
                    self.mcu = types.SimpleNamespace(register_config_callback=Mock())
                    self.commands = {}

                def lookup_objects(self, name):
                    return []

                def lookup_object(self, name):
                    return types.SimpleNamespace(register_mux_command=lambda name, mux, value, callback, **kw: self.commands.__setitem__(name, callback))

                get_reactor = lambda self: types.SimpleNamespace(mutex=threading.Lock)
                def register_event_handler(self, *args):
                    pass

            class Config:
                def __init__(self, printer, enabled):
                    self.printer, self.enabled = printer, enabled

                def get_printer(self):
                    return self.printer

                def get_name(self):
                    return 'klipper_mmu box'

                def get(self, key, default=None):
                    return path if self.enabled and key == 'maintenance_counters_file' else default

                def getboolean(self, key, default=False):
                    return default

            def make(enabled):
                printer = Printer()
                with patch.dict(sys.modules, {'mcu': types.SimpleNamespace(get_printer_mcu=lambda p, n: p.mcu)}):
                    observer = KlipperMMU(Config(printer, enabled))
                return observer, printer

            _, disabled = make(False)
            self.assertNotIn('KLIPPER_MMU_MAINTENANCE', disabled.commands)
            observer, printer = make(True)
            command = printer.commands['KLIPPER_MMU_MAINTENANCE']

            def call(**params):
                gcmd = types.SimpleNamespace(get=lambda key, default=None: params.get(key, default), respond_info=Mock())
                command(gcmd)
                return gcmd.respond_info.call_args.args[0]

            self.assertIn('due=0', call(COUNTER='blade', INCR='1', LIMIT='2',
                                         WARNING='Replace cutter blade'))
            self.assertIn('due=0', call(COUNTER='blade', INCR='1'))
            self.assertIn('due=1', call(COUNTER='blade', INCR='1'))
            self.assertIn('warning="Replace cutter blade"', call(COUNTER='blade'))
            self.assertIn('value=0', call(COUNTER='blade', RESET='1'))
            with self.assertRaises(ValueError):
                call(COUNTER='blade', INCR='-1')
            with open(path, 'rb') as source:
                before = source.read()
            with self.assertRaises(ValueError):
                call(COUNTER='blade', WARNING='bad\ntext')
            with patch('host.maintenance_counters.os.replace', side_effect=OSError('disk')):
                with self.assertRaisesRegex(ValueError, 'disk'):
                    call(COUNTER='blade', INCR='1')
            with open(path, 'rb') as source:
                self.assertEqual(source.read(), before)
            self.assertIn('value=0', call(COUNTER='blade'))
            self.assertEqual(observer.get_status(None)['maintenance_counters']['blade']['value'], 0)
            self.assertEqual(make(True)[0].get_status(None)['maintenance_counters']['blade']['limit'], 2)
            self.assertFalse(hasattr(printer.mcu, 'send'))


if __name__ == '__main__':
    unittest.main()
