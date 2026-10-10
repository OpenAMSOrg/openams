# SPDX-License-Identifier: GPL-3.0-or-later
"""Focused host calibration delivery checks. Run with unittest discovery."""
import threading
import json
import importlib.util
import pathlib
import struct
import sys
import tempfile
import types
import unittest
import zlib

PACKAGE = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PACKAGE))


class ConfigError(Exception):
    pass


class Printer:
    config_error = ConfigError

    def __init__(self):
        self.events = {}
        self.gcode = types.SimpleNamespace(register_mux_command=lambda *a, **k: None)
        self.mcu = MCU()

    def lookup_objects(self, kind):
        return []

    def lookup_object(self, kind):
        return self.gcode

    get_reactor = lambda self: types.SimpleNamespace(mutex=threading.Lock)
    def register_event_handler(self, name, handler):
        self.events[name] = handler


class MCU:
    def __init__(self):
        self.callbacks = []
        self.commands = []
        self.oids = 0
        self.fileoutput = False
        self.constants = {
            'KLIPPER_MMU_PROTOCOL_VERSION': 1,
            'KLIPPER_MMU_INTEGRATION_VERSION': 1,
            'KLIPPER_MMU_DIAGNOSTIC_STARTUP': 1,
            'KLIPPER_MMU_ACTUATION': 0,
            'KLIPPER_MMU_CONFIG_DIFF_BUFFER': 1,
            'KLIPPER_MMU_DIFF_CALIBRATION': 1,
        }

    def register_config_callback(self, callback):
        self.callbacks.append(callback)

    def is_fileoutput(self):
        return self.fileoutput

    def get_constants(self):
        return self.constants

    def create_oid(self):
        oid = self.oids
        self.oids += 1
        return oid

    def add_config_cmd(self, cmd, is_init=False):
        self.commands.append((cmd, is_init))


class Config:
    def __init__(self, printer, file=None, startup=False):
        self.printer = printer
        self.file = file
        self.startup = startup

    def get_printer(self):
        return self.printer

    def get_name(self):
        return 'klipper_mmu unit'

    def get(self, key, default=None):
        return {'mcu': 'mcu', 'diff_calibration_file': self.file}.get(key, default)

    def getboolean(self, key, default=False):
        if key == 'diagnostic_startup':
            return self.startup
        return default


MEASURED_SHAPE = {
    'channel_map': 'a_low_b_high',
    'vref_mv': 3300,
    'min_valid_mv': 100,
    'max_valid_mv': 3200,
    'path_tolerance_counts': 100,
    'points': [
        {'a_raw': 10, 'b_raw': 2050, 'position_permille': 0},
        {'a_raw': 1900, 'b_raw': 4000, 'position_permille': 1000},
    ],
}


class DeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_mcu = sys.modules.get('mcu')
        sys.modules['mcu'] = types.SimpleNamespace(
            get_printer_mcu=lambda printer, name: printer.mcu)
        import host
        cls.host = host

    @classmethod
    def tearDownClass(cls):
        if cls.old_mcu is None:
            del sys.modules['mcu']
        else:
            sys.modules['mcu'] = cls.old_mcu

    def make_file(self, directory, data):
        path = pathlib.Path(directory) / 'measured-diff.json'
        path.write_text(json.dumps(data), encoding='utf-8')
        return str(path)

    def test_opt_out_and_file_output_have_no_calibration_commands(self):
        printer = Printer()
        extra = self.host.load_config_prefix(Config(printer))
        self.assertEqual(printer.mcu.callbacks, [])
        self.assertIsNone(extra.get_status(None)['diff_calibration_sha256'])
        with tempfile.TemporaryDirectory() as directory:
            path = self.make_file(directory, MEASURED_SHAPE)
            printer = Printer()
            printer.mcu.fileoutput = True
            self.host.load_config_prefix(Config(printer, path, True))
            printer.mcu.callbacks[0]()
            self.assertEqual(printer.mcu.oids, 0)
            self.assertEqual(printer.mcu.commands, [])

    def test_one_ordinary_table_before_init_and_crc_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.make_file(directory, MEASURED_SHAPE)
            printer = Printer()
            extra = self.host.load_config_prefix(Config(printer, path, True))
            printer.mcu.callbacks[0]()
            commands = printer.mcu.commands
            self.assertEqual(len(commands), 2)
            self.assertFalse(commands[0][1])
            self.assertTrue(commands[0][0].startswith(
                'config_km_diff_calibration oid=0 data='))
            self.assertEqual(commands[1], ('km_diagnostic_start', True))
            self.assertEqual(printer.mcu.oids, 1)
            self.assertEqual(extra.get_status(None)['diff_calibration_file'], path)
            first_crc = zlib.crc32(commands[0][0].encode())
            changed = json.loads(json.dumps(MEASURED_SHAPE))
            changed['points'][1]['a_raw'] += 1
            self.make_file(directory, changed)
            second = Printer()
            self.host.load_config_prefix(Config(second, path, True))
            second.mcu.callbacks[0]()
            self.assertNotEqual(first_crc,
                                zlib.crc32(second.mcu.commands[0][0].encode()))

    def test_bad_file_and_firmware_gate_fail_before_object_or_command(self):
        with tempfile.TemporaryDirectory() as directory:
            invalid = json.loads(json.dumps(MEASURED_SHAPE))
            invalid['points'][1]['a_raw'] = 5000
            path = self.make_file(directory, invalid)
            printer = Printer()
            with self.assertRaises(ConfigError):
                self.host.load_config_prefix(Config(printer, path, True))
            self.assertEqual(printer.mcu.callbacks, [])
            self.assertEqual(printer.mcu.oids, 0)
            self.assertEqual(printer.mcu.commands, [])
            self.make_file(directory, MEASURED_SHAPE)
            extra = self.host.load_config_prefix(Config(printer, path, False))
            printer.mcu.constants['KLIPPER_MMU_ACTUATION'] = 1
            with self.assertRaises(ConfigError):
                printer.mcu.callbacks[0]()
            self.assertEqual(printer.mcu.oids, 0)
            self.assertEqual(printer.mcu.commands, [])
            self.assertFalse(extra.get_status(None)['motion_available'])

    def test_actuation_image_receives_calibration_only_with_startup_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.make_file(directory, MEASURED_SHAPE)
            printer = Printer()
            extra = self.host.load_config_prefix(Config(printer, path, True))
            printer.mcu.constants['KLIPPER_MMU_ACTUATION'] = 1
            printer.mcu.callbacks[0]()
            self.assertEqual(printer.mcu.oids, 1)
            self.assertTrue(printer.mcu.commands[0][0].startswith(
                'config_km_diff_calibration oid=0 data='))
            self.assertEqual(printer.mcu.commands[1], ('km_diagnostic_start', True))
            self.assertFalse(extra.get_status(None)['motion_available'])

    def test_b_low_a_high_preserves_physical_labels_and_encodes_canonical_pairs(self):
        with tempfile.TemporaryDirectory() as directory:
            record = json.loads(json.dumps(MEASURED_SHAPE))
            record['channel_map'] = 'b_low_a_high'
            record['points'] = [
                {'a_raw': 2000, 'b_raw': 10, 'position_permille': 0},
                {'a_raw': 4000, 'b_raw': 1900, 'position_permille': 1000},
            ]
            path = self.make_file(directory, record)
            printer = Printer()
            self.host.load_config_prefix(Config(printer, path))
            printer.mcu.callbacks[0]()
            payload = bytes.fromhex(
                printer.mcu.commands[0][0].split(' data=', 1)[1])
            self.assertEqual(payload[0], 1)
            self.assertEqual(struct.unpack_from('<HHHH', payload, 10),
                             (10, 2000, 1900, 4000))

    def test_duplicate_json_keys_rejected_before_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'duplicate.json'
            path.write_text('{"channel_map":"a_low_b_high",'
                            '"channel_map":"b_low_a_high"}', encoding='utf-8')
            printer = Printer()
            with self.assertRaisesRegex(ConfigError, 'duplicate JSON key'):
                self.host.load_config_prefix(Config(printer, str(path)))
            self.assertEqual(printer.mcu.callbacks, [])

    def test_missing_or_invalid_receiver_gate_has_no_side_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.make_file(directory, MEASURED_SHAPE)
            for gate in ('KLIPPER_MMU_CONFIG_DIFF_BUFFER',
                         'KLIPPER_MMU_DIFF_CALIBRATION'):
                for value in (None, 0, True, '1'):
                    with self.subTest(gate=gate, value=value):
                        printer = Printer()
                        self.host.load_config_prefix(Config(printer, path, True))
                        if value is None:
                            printer.mcu.constants.pop(gate)
                        else:
                            printer.mcu.constants[gate] = value
                        with self.assertRaisesRegex(ConfigError, gate):
                            printer.mcu.callbacks[0]()
                        self.assertEqual(printer.mcu.oids, 0)
                        self.assertEqual(printer.mcu.commands, [])

    def test_command_round_trips_pinned_klipper_codec_at_maximum_table(self):
        upstream = PACKAGE.parent / 'build/klipper-menuconfig/klippy/msgproto.py'
        spec = importlib.util.spec_from_file_location('pinned_msgproto', upstream)
        codec = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(codec)
        parser = codec.MessageParser()
        fmt = 'config_km_diff_calibration oid=%c data=%*s'
        parser.process_identify(json.dumps({
            'commands': {fmt: 100}, 'responses': {}, 'output': {},
        }).encode(), decompress=False)
        record = json.loads(json.dumps(MEASURED_SHAPE))
        record['points'] = [
            {'a_raw': i * 500, 'b_raw': i * 500 + 50,
             'position_permille': round(i * 1000 / 7)}
            for i in range(8)
        ]
        record['points'][-1]['position_permille'] = 1000
        with tempfile.TemporaryDirectory() as directory:
            path = self.make_file(directory, record)
            printer = Printer()
            self.host.load_config_prefix(Config(printer, path))
            printer.mcu.callbacks[0]()
            encoded = parser.create_command(printer.mcu.commands[0][0])
            decoded, end = parser.messages_by_name[fmt.split()[0]].parse(
                encoded, 0)
            self.assertEqual(end, len(encoded))
            self.assertEqual(len(decoded['data']), 54)
            self.assertEqual(decoded['oid'], 0)
            frame = parser.encode_msgblock(0, encoded)
            self.assertLessEqual(len(frame), 64)


if __name__ == '__main__':
    unittest.main()
