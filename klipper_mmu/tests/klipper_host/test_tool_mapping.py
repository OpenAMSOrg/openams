# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import json
import os
import sys
import tempfile
import unittest
import types
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from host.tool_mapping import ToolMapping
from host import KlipperMMU


class ToolMappingTest(unittest.TestCase):
    def test_persist_alias_and_resolve(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'tools.json')
            mapping = ToolMapping(path, 3)
            self.assertIsNone(mapping.resolve(0))
            mapping.set(0, 2)
            mapping.set(1, 2)
            self.assertEqual(ToolMapping(path, 3).status(), {'0': 2, '1': 2})
            snapshot = mapping.status()
            snapshot['0'] = 0
            self.assertEqual(mapping.resolve(0), 2)

    def test_rejection_leaves_file_and_memory_intact(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'tools.json')
            mapping = ToolMapping(path, 2)
            mapping.set(0, 1)
            with open(path, 'rb') as source:
                before = source.read()
            for tool, gate in ((2, 0), (0, 2), (True, 0), (0, True)):
                with self.assertRaises(ValueError):
                    mapping.set(tool, gate)
                with open(path, 'rb') as source:
                    self.assertEqual(source.read(), before)
                self.assertEqual(mapping.resolve(0), 1)

    def test_corrupt_rejected_on_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'tools.json')
            for payload in (b'{', b'{"tools":{"0":0,"0":1}}',
                            b'{"tools":{"0":true}}', b'{"tools":{"2":0}}',
                            b'{"tools":{"0":0},"tools":{}}'):
                with open(path, 'wb') as output:
                    output.write(payload)
                with self.assertRaises((ValueError, json.JSONDecodeError)):
                    ToolMapping(path, 2)
            with open(path, 'wb') as output:
                output.write(b' ' * 1025)
            with self.assertRaisesRegex(ValueError, 'too large'):
                ToolMapping(path, 2)

    def test_path_and_count_bounds(self):
        with self.assertRaisesRegex(ValueError, 'absolute'):
            ToolMapping('tools.json', 2)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'tools.json')
            for count in (0, 13, True):
                with self.assertRaises(ValueError):
                    ToolMapping(path, count)

    def test_replace_failure_preserves_file_and_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'tools.json')
            mapping = ToolMapping(path, 2)
            mapping.set(0, 1)
            with open(path, 'rb') as source:
                before = source.read()
            with patch('host.tool_mapping.os.replace', side_effect=OSError('disk failure')):
                with self.assertRaisesRegex(OSError, 'disk failure'):
                    mapping.set(0, 0)
            self.assertEqual(mapping.resolve(0), 1)
            with open(path, 'rb') as source:
                self.assertEqual(source.read(), before)


class ToolMappingCommandTest(unittest.TestCase):
    def test_opt_in_command_query_set_and_status(self):
        class Printer:
            config_error = ValueError
            command_error = ValueError

            def __init__(self):
                self.mcu = types.SimpleNamespace(register_config_callback=Mock())
                self.commands = {}

            def lookup_objects(self, name):
                return []

            def lookup_object(self, name):
                return types.SimpleNamespace(register_mux_command=lambda name, mux,
                                             value, callback, **kwargs:
                                             self.commands.__setitem__(name, callback))

            get_reactor = lambda self: types.SimpleNamespace(mutex=threading.Lock)
            def register_event_handler(self, *args):
                pass

        class Config:
            def __init__(self, printer, path=None):
                self.printer = printer
                self.path = path

            def get_printer(self):
                return self.printer

            def get_name(self):
                return 'klipper_mmu box'

            def get(self, key, default=None):
                if self.path is None:
                    return default
                return {'tool_mapping_file': self.path,
                        'tool_mapping_gates': '2'}.get(key, default)

            def getboolean(self, key, default=False):
                return default

        def make_observer(printer, path=None):
            with patch.dict(sys.modules, {'mcu': types.SimpleNamespace(
                    get_printer_mcu=lambda p, n: p.mcu)}):
                return KlipperMMU(Config(printer, path))

        def invoke(callback, **parameters):
            command = types.SimpleNamespace(
                get=lambda key, default=None: parameters.get(key, default),
                get_int=lambda key, minval=None, maxval=None: int(parameters[key]),
                respond_info=Mock())
            callback(command)
            return command.respond_info.call_args.args[0]

        plain_printer = Printer()
        plain = make_observer(plain_printer)
        self.assertNotIn('KLIPPER_MMU_TOOL_MAP', plain_printer.commands)
        self.assertIsNone(plain.get_status(None)['tool_mapping'])
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'tools.json')
            printer = Printer()
            observer = make_observer(printer, path)
            callback = printer.commands['KLIPPER_MMU_TOOL_MAP']
            self.assertIn('gate=unmapped', invoke(callback, TOOL='0'))
            self.assertIn('gate=1', invoke(callback, TOOL='0', GATE='1'))
            self.assertIn('gate=1', invoke(callback, TOOL='1', GATE='1'))
            self.assertEqual(observer.get_status(None)['tool_mapping'],
                             {'0': 1, '1': 1})
            self.assertFalse(hasattr(printer.mcu, 'send'))
            with self.assertRaises(ValueError):
                invoke(callback, TOOL='0', GATE='2')
            self.assertEqual(observer.get_status(None)['tool_mapping']['0'], 1)
            self.assertIn('gate=unmapped', invoke(callback, TOOL='0', CLEAR='1'))
            self.assertEqual(observer.get_status(None)['tool_mapping'], {'1': 1})
            with open(path, 'rb') as source:
                preserved = source.read()
            self.assertIn('gate=unmapped', invoke(callback, TOOL='0', CLEAR='1'))
            with open(path, 'rb') as source:
                self.assertEqual(source.read(), preserved)
            with self.assertRaises(ValueError):
                invoke(callback, TOOL='1', GATE='0', CLEAR='1')
            self.assertEqual(observer.get_status(None)['tool_mapping'], {'1': 1})
            self.assertIn('gate=unmapped', invoke(callback, TOOL='1', CLEAR='1'))
            self.assertEqual(observer.get_status(None)['tool_mapping'], {})
            self.assertEqual(make_observer(Printer(), path).get_status(None)['tool_mapping'],
                             {})

    def test_config_requires_count_and_absolute_file(self):
        class Printer:
            config_error = ValueError

            def __init__(self):
                self.mcu = types.SimpleNamespace(register_config_callback=Mock())

            def lookup_objects(self, name):
                return []

            def lookup_object(self, name):
                return types.SimpleNamespace(register_mux_command=Mock())

            get_reactor = lambda self: types.SimpleNamespace(mutex=threading.Lock)
            def register_event_handler(self, *args):
                pass

        class Config:
            def __init__(self, values):
                self.printer = Printer()
                self.values = values

            def get_printer(self):
                return self.printer

            def get_name(self):
                return 'klipper_mmu box'

            def get(self, key, default=None):
                return self.values.get(key, default)

            def getboolean(self, key, default=False):
                return default

        def build(values):
            with patch.dict(sys.modules, {'mcu': types.SimpleNamespace(
                    get_printer_mcu=lambda p, n: p.mcu)}):
                return KlipperMMU(Config(values))

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'tools.json')
            with self.assertRaisesRegex(ValueError, 'tool_mapping_gates is required'):
                build({'tool_mapping_file': path})
            with self.assertRaisesRegex(ValueError, 'requires tool_mapping_file'):
                build({'tool_mapping_gates': '2'})
            with self.assertRaisesRegex(ValueError, 'absolute'):
                build({'tool_mapping_file': 'tools.json', 'tool_mapping_gates': '2'})
            topology = {
                'tool_mapping_file': path, 'tool_mapping_gates': '3',
                'openams_idx': '1', 'openams_fps': 'fps',
                'openams_family': 'boxturtle', 'openams_bays': '2',
                'openams_path_length_mm': '800'}
            with self.assertRaisesRegex(ValueError, 'conflicts'):
                build(topology)
            observer = build({**topology, 'tool_mapping_gates': '2'})
            self.assertEqual(observer.get_status(None)['tool_mapping'], {})


if __name__ == '__main__':
    unittest.main()
