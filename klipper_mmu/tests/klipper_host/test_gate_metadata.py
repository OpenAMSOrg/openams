# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import json
import os
import tempfile
import unittest
import sys
import types
from unittest.mock import Mock, patch

from host import KlipperMMU
from host.gate_metadata import GateMetadata


class GateMetadataTest(unittest.TestCase):
    def test_manual_edit_persists_and_status_is_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'gates.json')
            metadata = GateMetadata(path, 2)
            self.assertFalse(os.path.exists(path))
            metadata.edit(1, {'name': 'PLA White', 'available': True})
            stored = metadata.status()
            stored['1']['name'] = 'tampered'
            self.assertEqual(metadata.status()['1']['name'], 'PLA White')
            self.assertEqual(GateMetadata(path, 2).status(),
                             {'1': {'name': 'PLA White', 'available': True}})
            with open(path) as source:
                self.assertEqual(json.load(source)['gates']['1']['available'], True)

    def test_rejects_duplicate_keys_and_out_of_range_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'gates.json')
            for contents in (b'{"gates":{"0":{"name":"a","name":"b"}}}',
                             b'{"gates":{"2":{"name":"a"}}}',
                             b'{"gates":{"0":{"available":1}}}'):
                with open(path, 'wb') as output:
                    output.write(contents)
                with self.assertRaises(ValueError):
                    GateMetadata(path, 2)

    def test_invalid_edit_never_replaces_valid_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'gates.json')
            metadata = GateMetadata(path, 1)
            metadata.edit(0, {'material': 'PLA'})
            with open(path, 'rb') as source:
                before = source.read()
            for gate, update in ((1, {'name': 'x'}),
                                 (0, {'available': 'yes'}),
                                 (0, {'color': '\x00'}), (0, {})):
                with self.assertRaises(ValueError):
                    metadata.edit(gate, update)
                with open(path, 'rb') as source:
                    self.assertEqual(source.read(), before)

    def test_gate_status_reads_unknown_available_and_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'gates.json')
            metadata = GateMetadata(path, 3)
            self.assertIsNone(metadata.gate_status(0))  # never set: GATE_UNKNOWN
            metadata.edit(1, {'available': True})
            self.assertTrue(metadata.gate_status(1))  # GATE_AVAILABLE
            metadata.edit(2, {'available': False})
            self.assertFalse(metadata.gate_status(2))  # GATE_EMPTY
            metadata.edit(1, {'name': 'PLA'})  # unrelated field: available stays
            self.assertTrue(metadata.gate_status(1))
            with self.assertRaises(ValueError):
                metadata.gate_status(3)
            with self.assertRaises(ValueError):
                metadata.gate_status(-1)

    def test_size_and_path_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'gates.json')
            with open(path, 'wb') as output:
                output.write(b' ' * 8193)
            with self.assertRaisesRegex(ValueError, 'exceeds'):
                GateMetadata(path, 1)
            with self.assertRaisesRegex(ValueError, 'absolute'):
                GateMetadata('relative.json', 1)


class GateCommandTest(unittest.TestCase):
    def make_observer(self, path, gates='2', topology=False):
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
            def __init__(self, printer):
                self.printer = printer

            def get_printer(self):
                return self.printer

            def get_name(self):
                return 'klipper_mmu box'

            def get(self, key, default=None):
                values = {'gate_metadata_file': path}
                if gates is not None:
                    values['gate_metadata_gates'] = gates
                if topology:
                    values.update(openams_idx='1', openams_fps='fps',
                                  openams_family='boxturtle', openams_bays='2',
                                  openams_path_length_mm='800')
                return values.get(key, default)

            def getboolean(self, key, default=False):
                return default

        printer = Printer()
        with patch.dict(sys.modules, {'mcu': types.SimpleNamespace(
                get_printer_mcu=lambda p, n: p.mcu)}):
            observer = KlipperMMU(Config(printer))
        return observer, printer

    def command(self, callback, **parameters):
        command = types.SimpleNamespace(
            get=lambda key, default=None: parameters.get(key, default),
            get_int=lambda key, minval=None, maxval=None: int(parameters[key]),
            respond_info=Mock())
        callback(command)
        return command

    def test_config_requires_gate_count_without_topology(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'gates.json')
            with self.assertRaisesRegex(ValueError, 'gate_metadata_gates is required'):
                self.make_observer(path, gates=None)
            with self.assertRaisesRegex(ValueError, 'conflicts'):
                self.make_observer(path, gates='3', topology=True)

    def test_command_roundtrip_and_no_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'gates.json')
            observer, printer = self.make_observer(path)
            callback = printer.commands['KLIPPER_MMU_SET_GATE_METADATA']
            self.command(callback, GATE='1', NAME='White', MATERIAL='PLA',
                         COLOR='FFFFFF', AVAILABLE='1')
            self.assertEqual(observer.get_status(None)['gate_metadata'],
                             {'1': {'name': 'White', 'material': 'PLA',
                                    'color': 'FFFFFF', 'available': True}})
            self.assertFalse(hasattr(printer.mcu, 'send'))
            restarted, _ = self.make_observer(path)
            self.assertEqual(restarted.get_status(None)['gate_metadata'],
                             observer.get_status(None)['gate_metadata'])
            self.command(callback, GATE='1', AVAILABLE='0')
            self.assertIs(observer.get_status(None)['gate_metadata']['1']['available'],
                          False)

    def test_invalid_command_and_replace_failure_preserve_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'gates.json')
            observer, printer = self.make_observer(path)
            callback = printer.commands['KLIPPER_MMU_SET_GATE_METADATA']
            self.command(callback, GATE='0', NAME='Original')
            with open(path, 'rb') as source:
                before = source.read()
            for kwargs in ({'GATE': '2', 'NAME': 'Bad'},
                           {'GATE': '0', 'AVAILABLE': 'yes'}):
                with self.assertRaises(ValueError):
                    self.command(callback, **kwargs)
            with patch('host.gate_metadata.os.replace', side_effect=OSError('disk failure')):
                with self.assertRaisesRegex(ValueError, 'disk failure'):
                    self.command(callback, GATE='0', NAME='Changed')
            self.assertEqual(observer.get_status(None)['gate_metadata']['0']['name'],
                             'Original')
            with open(path, 'rb') as source:
                self.assertEqual(source.read(), before)


if __name__ == '__main__':
    unittest.main()
