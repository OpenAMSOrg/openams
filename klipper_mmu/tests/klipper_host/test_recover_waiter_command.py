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
from host.protocol import Event, Identity
from host.requests import Requests


class RecoverWaiterCommandTest(unittest.TestCase):
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
                self.assertEqual('KLIPPER_MMU_RECOVER_WAITER' in printer.commands,
                                 motion)

    def test_explicit_command_delegates_only_to_managed_unit(self):
        observer = object.__new__(KlipperMMU)
        observer._printer = types.SimpleNamespace(command_error=ValueError)
        unit = object.__new__(OpenAMSManagedUnit)
        unit.recover_retired_waiter_terminal = Mock()
        unit._retired_pending_ack = None
        observer._host_recovery_record = None
        observer._openams_unit = unit
        gcmd = types.SimpleNamespace(respond_info=Mock())
        observer.cmd_KLIPPER_MMU_RECOVER_WAITER(gcmd)
        unit.recover_retired_waiter_terminal.assert_called_once_with(timeout=5.0)
        self.assertIn('HOST motion remains revoked',
                      gcmd.respond_info.call_args.args[0])

        observer._openams_unit = None
        with self.assertRaisesRegex(ValueError, 'requires openams_motion'):
            observer.cmd_KLIPPER_MMU_RECOVER_WAITER(gcmd)
        unit.recover_retired_waiter_terminal.side_effect = ValueError('unsafe head')
        observer._openams_unit = unit
        with self.assertRaisesRegex(ValueError, 'RECOVER_WAITER: unsafe head'):
            observer.cmd_KLIPPER_MMU_RECOVER_WAITER(gcmd)
        self.assertEqual(gcmd.respond_info.call_count, 1)

    def test_uncertain_ack_retries_only_exact_settled_waiter(self):
        observer = object.__new__(KlipperMMU)
        observer._printer = types.SimpleNamespace(command_error=ValueError)
        observer._host_recovery_record = None
        unit = object.__new__(OpenAMSManagedUnit)
        unit._event_binding = None
        unit._retired_pending_ack = None
        unit._retired_pending_ack_eligible = False
        unit.recover_retired_waiter_terminal = Mock()
        unit.retry_retired_ack = Mock()
        observer._openams_unit = unit
        gcmd = types.SimpleNamespace(respond_info=Mock())
        requests = Requests(12, 34)
        requests.connected = False
        requests.generation = 2
        unit._retired_requests = requests
        identity = Identity(1, 34, 5, 1, 0)
        event = Event(12, identity, 9, 100, 1, 0, 42, 0)
        record = types.SimpleNamespace(instance=12, session=34,
                                       request_generation=1,
                                       pending_ack=event,
                                       pending_ack_eligible=True,
                                       pending_requests=())

        # The first recovery accepted the terminal, removed its waiter, and
        # left its ACK uncertain. The command must retry only that ACK.
        unit.recover_retired_waiter_terminal.side_effect = ValueError('ACK uncertain')
        with self.assertRaisesRegex(ValueError, 'ACK uncertain'):
            observer.cmd_KLIPPER_MMU_RECOVER_WAITER(gcmd)
        observer._host_recovery_record = record
        unit._retired_pending_ack = event
        unit._retired_pending_ack_eligible = True
        observer.cmd_KLIPPER_MMU_RECOVER_WAITER(gcmd)
        unit.retry_retired_ack.assert_called_once_with(timeout=5.0)
        self.assertEqual(unit.recover_retired_waiter_terminal.call_count, 1)
        self.assertIn('HOST motion remains revoked',
                      gcmd.respond_info.call_args.args[0])

        for invalid in ('wrong head', 'live pending', 'local terminal'):
            with self.subTest(invalid=invalid):
                if invalid == 'wrong head':
                    unit._retired_pending_ack = Event(12, identity, 10, 100,
                                                      1, 0, 42, 0)
                elif invalid == 'live pending':
                    requests.pending['fps'] = (identity, 4)
                else:
                    requests.preload_terminal = event
                with self.assertRaisesRegex(ValueError,
                                            'No exact retired waiter ACK'):
                    observer.cmd_KLIPPER_MMU_RECOVER_WAITER(gcmd)
                unit._retired_pending_ack = event
                requests.pending.clear()
                requests.preload_terminal = None
        unit.retry_retired_ack.assert_called_once()


if __name__ == '__main__':
    unittest.main()
