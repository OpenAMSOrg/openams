#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise the MMU extra through pinned Klipper's public MCU config path."""
import threading
import hashlib
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
KLIPPY = pathlib.Path(os.environ.get(
    'KLIPPER_MMU_TEST_SOURCE', str(ROOT / 'build/klipper-menuconfig'))) / 'klippy'
DICT = pathlib.Path(os.environ.get(
    'KLIPPER_MMU_TEST_DICTIONARY',
    str(ROOT / 'build/calibration-build-matrix/diagnostic_diff/out/klipper.dict')))
PINNED_HASHES = {
    'mcu.py': '36c47e1f910abfdd1670b323af752fa4a44afdf8ea21a936664f067b1e35c23b',
    'msgproto.py': 'c205bb7d70d968b5fbe8bbb86247a125994eb5d7acde16660afb2471672d6278',
}
for filename, expected in PINNED_HASHES.items():
    source = KLIPPY / filename
    actual = hashlib.sha256(source.read_bytes()).hexdigest()
    if actual != expected:
        raise SystemExit('pinned Klipper source mismatch: %s' % source)
sys.path.insert(0, str(KLIPPY))
sys.path.insert(0, str(ROOT / 'klipper-mmu'))

# These modules are only used by unrelated MCU runtime paths. The real pinned
# MCUConfigHelper and msgproto classes below remain intact.
for dependency in ('serialhdl', 'pins', 'chelper', 'clocksync'):
    sys.modules.setdefault(dependency, types.ModuleType(dependency))
import mcu
import msgproto
from host import KlipperMMU


class Printer:
    config_error = ValueError
    command_error = ValueError

    def __init__(self):
        self.objects = {}
        self.handlers = {}

    def get_reactor(self):
        return self

    def mutex(self):
        return threading.Lock()

    def lookup_object(self, name):
        return self.objects[name]

    def lookup_objects(self, name):
        return []

    def register_event_handler(self, event, callback):
        self.handlers.setdefault(event, []).append(callback)

    def get_start_args(self):
        return {}

    def set_rollover_info(self, *args, **kwargs):
        pass


class Pins:
    def register_chip(self, *args):
        pass

    def get_pin_resolver(self, name):
        return self

    def update_command(self, command):
        return command


class GCode:
    def register_mux_command(self, *args, **kwargs):
        pass


class Config:
    def __init__(self, printer, options):
        self.printer, self.options = printer, options

    def get_printer(self):
        return self.printer

    def get_name(self):
        return 'klipper_mmu test'

    def get(self, name, default=None):
        return self.options.get(name, default)

    def getboolean(self, name, default=False):
        return self.options.get(name, default)

    def getfloat(self, name, default=None, **limits):
        return self.options.get(name, default)


class Serial:
    def __init__(self, parser):
        self.parser = parser
        self.sent = []

    def send(self, command):
        self.parser.create_command(command)
        self.sent.append(command)

    def get_serialqueue(self):
        return object()


class Restart:
    def check_restart_on_send_config(self):
        pass

    def check_restart_on_crc_mismatch(self):
        pass


class Connection:
    def __init__(self, mcu_obj, serial):
        self.mcu_obj, self.serial = mcu_obj, serial

    def get_mcu(self):
        return self.mcu_obj

    def get_serial(self):
        return self.serial

    def get_clocksync(self):
        return object()

    def get_restart_helper(self):
        return Restart()

    def is_shutdown(self):
        return False

    def log_info(self):
        return ''


class MCU:
    def __init__(self, printer, serial, constants, fileoutput=False):
        self.printer, self.serial = printer, serial
        self.constants, self.fileoutput = constants, fileoutput
        self.helper = mcu.MCUConfigHelper(Config(printer, {}), Connection(self, serial))

    def get_name(self):
        return 'mcu'

    def get_constants(self):
        return self.constants

    def is_fileoutput(self):
        return self.fileoutput

    def register_config_callback(self, callback):
        self.helper.register_config_callback(callback)

    def add_config_cmd(self, command, is_init=False):
        self.helper.add_config_cmd(command, is_init=is_init)

    def create_oid(self):
        return self.helper.create_oid()

    def lookup_query_command(self, *args):
        return self

    def send(self):
        if self.fileoutput:
            return {'is_config': 0, 'crc': 0, 'move_count': 500}
        return {'is_config': int(bool(self.serial.sent)),
                'crc': self.helper._config_crc, 'move_count': 500}


class CalibrationIntegration(unittest.TestCase):
    def setUp(self):
        self.assertTrue(DICT.is_file(), 'generated diagnostic dictionary is required')
        self.dictionary = json.loads(DICT.read_text())
        self.parser = msgproto.MessageParser()
        self.parser.process_identify(DICT.read_bytes(), decompress=False)
        self.constants = self.dictionary['config']
        self.assertIn('config_km_diff_calibration oid=%c data=%*s',
                      self.dictionary['commands'])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def make_record(self, mid=500):
        # Synthetic ADC counts within the stated 100..3200 mV valid range.
        record = {'channel_map': 'a_low_b_high', 'vref_mv': 3300,
                  'min_valid_mv': 100, 'max_valid_mv': 3200,
                  'path_tolerance_counts': 20,
                  'points': [{'a_raw': 250, 'b_raw': 2200, 'position_permille': 0},
                             {'a_raw': mid, 'b_raw': 2600, 'position_permille': 500},
                             {'a_raw': 1900, 'b_raw': 3900,
                              'position_permille': 1000}]}
        path = pathlib.Path(self.temp.name) / ('record-%d.json' % mid)
        path.write_text(json.dumps(record))
        return str(path)

    def setup_host(self, calibration=None, startup=False, fileoutput=False,
                   diff_probe=False, bowden=None):
        printer = Printer()
        printer.objects.update(pins=Pins(), gcode=GCode(),
                               motion_queuing=types.SimpleNamespace(
                                   setup_mcu_movequeue=lambda *args: None))
        serial = Serial(self.parser)
        chip = MCU(printer, serial, self.constants, fileoutput)
        printer.objects['mcu'] = chip
        old = mcu.get_printer_mcu
        mcu.get_printer_mcu = lambda p, name: chip
        try:
            options = {'diagnostic_startup': startup, 'diff_probe': diff_probe}
            if calibration is not None:
                options['diff_calibration_file'] = calibration
            if bowden is not None:
                options['bowden_estimate_file'] = bowden
            host = KlipperMMU(Config(printer, options))
        finally:
            mcu.get_printer_mcu = old
        return chip, host

    def test_receive_only_probe_init_and_reconnect(self):
        if os.environ.get('KLIPPER_MMU_TEST_EXPECT_PROBE') != '1':
            self.skipTest('requires a receive-only image dictionary')
        self.assertEqual(self.constants.get('KLIPPER_MMU_ACTUATION'), 0)
        self.assertEqual(self.constants.get('KLIPPER_MMU_DIFF_PROBE'), 1)
        chip, _ = self.setup_host(diff_probe=True)
        chip.helper._finalize_config()
        self.assertEqual(chip.helper._config_cmds,
                         ['allocate_oids count=0',
                          'finalize_config crc=%d' % chip.helper._config_crc])
        self.assertEqual(chip.helper._init_cmds, ['km_diff_probe_start'])
        for command in chip.helper._config_cmds + chip.helper._init_cmds:
            self.parser.create_command(command)

        reconnect, _ = self.setup_host(diff_probe=True)
        reconnect.helper._send_get_config = lambda: {
            'is_config': 1, 'crc': chip.helper._config_crc, 'move_count': 500}
        reconnect.helper._connect()
        self.assertEqual(reconnect.serial.sent, ['km_diff_probe_start'])

    def test_config_order_crc_and_reconnect(self):
        path = self.make_record()
        chip, host = self.setup_host(path, startup=True)
        self.assertEqual(len(chip.helper._config_callbacks), 1)
        chip.helper._finalize_config()
        commands = chip.helper._config_cmds
        self.assertEqual(len([c for c in commands if c.startswith(
            'config_km_diff_calibration ')]), 1)
        self.assertEqual(commands[0], 'allocate_oids count=1')
        self.assertTrue(commands[1].startswith('config_km_diff_calibration oid=0 data='))
        self.assertEqual(commands[-1], 'finalize_config crc=%d' % chip.helper._config_crc)
        self.assertEqual(chip.helper._init_cmds, ['km_diagnostic_start'])
        for command in commands + chip.helper._init_cmds:
            self.parser.create_command(command)

        changed, _ = self.setup_host(self.make_record(501), startup=True)
        changed.helper._finalize_config()
        self.assertNotEqual(chip.helper._config_crc, changed.helper._config_crc)

        # Pinned reconnect path: matching CRC emits init only, never calibration.
        reconnect, _ = self.setup_host(path, startup=True)
        reconnect.helper._send_get_config = lambda: {
            'is_config': 1, 'crc': chip.helper._config_crc, 'move_count': 500}
        reconnect.helper._connect()
        self.assertEqual(reconnect.serial.sent, ['km_diagnostic_start'])

        stale, _ = self.setup_host(self.make_record(501), startup=True)
        stale.helper._send_get_config = lambda: {
            'is_config': 1, 'crc': chip.helper._config_crc, 'move_count': 500}
        with self.assertRaisesRegex(mcu.error, 'CRC does not match'):
            stale.helper._connect()
        self.assertEqual(stale.serial.sent, [])

    def test_step_estimate_restores_fps_steps_through_exact_dictionary(self):
        fmt = 'config_km_fps_steps oid=%c steps=%u'
        low = self.constants.get('KLIPPER_MMU_FPS_MIN_STEPS')
        high = self.constants.get('KLIPPER_MMU_FPS_MAX_STEPS')
        self.assertEqual(fmt in self.dictionary['commands'], low is not None)
        if low is None:
            self.skipTest('image has no FPS arrival')
        path = self.make_record()
        estimate = {'kind': 'step_estimate', 'unit': 'feeder_microsteps',
                    'span': 'hub_to_fps_arrival', 'steps': (low + high) // 2,
                    'lane': 0, 'mmu': 'test',
                    'diff_calibration_sha256': hashlib.sha256(
                        pathlib.Path(path).read_bytes()).hexdigest(),
                    'firmware': {k: v for k, v in self.constants.items()
                                 if k.startswith('KLIPPER_MMU_') and type(v) is int}}
        bowden = pathlib.Path(self.temp.name) / 'bowden.json'
        bowden.write_text(json.dumps({'estimate': estimate}))
        chip, _ = self.setup_host(path, startup=True, bowden=str(bowden))
        chip.helper._finalize_config()
        commands = chip.helper._config_cmds
        steps = estimate['steps']
        self.assertEqual(commands[0], 'allocate_oids count=2')
        self.assertTrue(commands[1].startswith('config_km_diff_calibration oid=0 '))
        self.assertEqual(commands[2], 'config_km_fps_steps oid=1 steps=%d' % steps)
        for command in commands:
            self.parser.create_command(command)
        # Another calibration file is another identity: LOAD stays refused
        # until this session's own CALIBRATE measures a distance.
        other, _ = self.setup_host(self.make_record(501), startup=True, bowden=str(bowden))
        other.helper._finalize_config()
        self.assertFalse(any(c.startswith('config_km_fps_steps')
                             for c in other.helper._config_cmds))

    def test_file_output_and_opt_out(self):
        file_chip, _ = self.setup_host(self.make_record(), startup=True,
                                       fileoutput=True)
        file_chip.helper._finalize_config()
        self.assertEqual(file_chip.helper._config_cmds,
                         ['allocate_oids count=0',
                          'finalize_config crc=%d' % file_chip.helper._config_crc])
        self.assertEqual(file_chip.helper._init_cmds, [])
        plain, _ = self.setup_host()
        plain.helper._finalize_config()
        self.assertEqual(plain.helper._config_callbacks, [])
        self.assertFalse(any('calibration' in cmd
                             for cmd in plain.helper._config_cmds))


if __name__ == '__main__':
    unittest.main()
