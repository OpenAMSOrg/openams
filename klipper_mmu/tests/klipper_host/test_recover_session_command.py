# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from host import KlipperMMU
from host.openams_managed_unit import OpenAMSManagedUnit


class RecoverSessionCommandTest(unittest.TestCase):
    def test_registration_requires_motion_configuration(self):
        class Config:
            def __init__(self, printer, motion):
                self.printer, self.motion = printer, motion

            def get_printer(self):
                return self.printer

            def get_name(self):
                return 'klipper_mmu box'

            def get(self, key, default=None):
                return dict(openams_idx='1', openams_fps='fps',
                            openams_family='boxturtle', openams_bays='4',
                            openams_path_length_mm='800',
                            openams_host_watchdog_ms='1000',
                            openams_lease_max_age_ms='400').get(key, default)

            def getboolean(self, key, default=False):
                return self.motion if key in ('openams_motion',
                                              'diagnostic_startup') else default

        class Printer:
            config_error = ValueError

            def __init__(self):
                self.mcu = types.SimpleNamespace(register_config_callback=lambda cb: None)
                self.commands = []

            def lookup_objects(self, name):
                return []

            def lookup_object(self, name):
                return types.SimpleNamespace(register_mux_command=lambda *a, **k:
                                             self.commands.append(a[0]))

            get_reactor = lambda self: types.SimpleNamespace(mutex=threading.Lock)
            def register_event_handler(self, *args):
                pass

        with patch.dict(sys.modules, {'mcu': types.SimpleNamespace(
                get_printer_mcu=lambda p, n: p.mcu)}):
            for motion in (False, True):
                printer = Printer()
                KlipperMMU(Config(printer, motion))
                self.assertEqual('KLIPPER_MMU_RECOVER_SESSION' in printer.commands,
                                 motion)

    def test_command_only_releases_record(self):
        observer = object.__new__(KlipperMMU)
        observer._printer = types.SimpleNamespace(command_error=ValueError)
        unit = object.__new__(OpenAMSManagedUnit)
        unit._state = types.SimpleNamespace(bay_count=2)
        observer._openams_unit = unit
        observer.reconcile_clean_host_recovery = Mock()
        command = types.SimpleNamespace(respond_info=Mock())

        with patch('host.openams_session_bootstrap.send_heartbeat') as beat:
            observer.cmd_KLIPPER_MMU_RECOVER_SESSION(command)
            beat.assert_not_called()
        observer.reconcile_clean_host_recovery.assert_called_once_with(2)
        self.assertIn('HOST motion remains revoked',
                      command.respond_info.call_args.args[0])

        observer._openams_unit = None
        with self.assertRaisesRegex(ValueError, 'requires openams_motion'):
            observer.cmd_KLIPPER_MMU_RECOVER_SESSION(command)
        observer._openams_unit = unit
        observer.reconcile_clean_host_recovery.side_effect = ValueError('unsafe head')
        with self.assertRaisesRegex(ValueError, 'RECOVER_SESSION: unsafe head'):
            observer.cmd_KLIPPER_MMU_RECOVER_SESSION(command)


if __name__ == '__main__':
    unittest.main()
