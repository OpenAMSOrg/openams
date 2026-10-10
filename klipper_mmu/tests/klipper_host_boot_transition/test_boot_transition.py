# SPDX-License-Identifier: GPL-3.0-or-later
"""Connection-local boot observation with a modeled Klipper MCU query."""
import threading
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from host import KlipperMMU
from host.wire import ENUMERATIONS, members


class CommandError(Exception):
    pass


class Printer:
    command_error = CommandError
    config_error = CommandError

    def __init__(self, mcu):
        self.mcu = mcu
        self.events = {}
        self.gcode = types.SimpleNamespace(register_mux_command=lambda *a, **kw: None)

    def lookup_objects(self, name):
        return []

    def lookup_object(self, name):
        assert name == 'gcode'
        return self.gcode

    get_reactor = lambda self: types.SimpleNamespace(mutex=threading.Lock)
    def register_event_handler(self, event, callback):
        self.events[event] = callback


class MCU:
    def __init__(self):
        self.instance = 0
        self.state = 0
        self.query_hook = None
        self.fail = False
        self.sent = []
        self.enumerations = {}  # A legacy image declares none.

    def is_fileoutput(self):
        return False

    def get_constants(self):
        return {'KLIPPER_MMU_PROTOCOL_VERSION': 1,
                'KLIPPER_MMU_INTEGRATION_VERSION': 1,
                'KLIPPER_MMU_ACTUATION': 0}

    def get_enumerations(self):
        return self.enumerations

    def try_lookup_command(self, template):
        return None  # Nothing optional (selector, detail query) in this image.

    def lookup_query_command(self, command, response):
        assert command == 'km_transport_query challenge=%u'
        return self

    def send(self, args):
        self.sent.append(args[0])
        instance, state = self.instance, self.state
        if self.query_hook:
            hook, self.query_hook = self.query_hook, None
            hook()
        if self.fail:
            raise CommandError('synthetic query failure')
        return {'challenge': args[0], 'instance_hi': instance >> 32,
                'instance_lo': instance & 0xffffffff, 'last_query': 0,
                'state': state}


class Config:
    def __init__(self, printer):
        self.printer = printer

    def get_printer(self):
        return self.printer

    def get_name(self):
        return 'klipper_mmu test'

    def get(self, name, default=None):
        return default

    def getboolean(self, name, default=False):
        return default


def fixture():
    mcu = MCU()
    printer = Printer(mcu)
    fake_mcu = types.SimpleNamespace(get_printer_mcu=lambda p, name: p.mcu)
    with patch.dict(sys.modules, {'mcu': fake_mcu}):
        observer = KlipperMMU(Config(printer))
    return mcu, printer, observer


class BootTransitionTests(unittest.TestCase):
    def test_unbound_does_not_erase_prior_attached_instance(self):
        mcu, printer, observer = fixture()
        mcu.instance, mcu.state = 0x100000001, 1
        printer.events['klippy:connect']()
        self.assertEqual((observer.get_status(None)['last_attached_instance'],
                          observer.get_status(None)['boot_transition']),
                         (mcu.instance, None))
        printer.events['klippy:disconnect']()
        mcu.instance, mcu.state = 0, 0
        printer.events['klippy:connect']()
        status = observer.get_status(None)
        self.assertEqual(status['instance'], 0)
        self.assertEqual(status['last_attached_instance'], 0x100000001)
        self.assertIsNone(status['boot_transition'])
        self.assertFalse(status['boot_instance_changed'])
        self.assertFalse(status['motion_available'])

    def test_same_and_changed_attached_instance(self):
        mcu, printer, observer = fixture()
        mcu.instance, mcu.state = 11, 1
        printer.events['klippy:connect']()
        printer.events['klippy:disconnect']()
        printer.events['klippy:connect']()
        self.assertEqual(observer.get_status(None)['boot_transition'], 'same')
        printer.events['klippy:disconnect']()
        mcu.instance = 12
        printer.events['klippy:connect']()
        status = observer.get_status(None)
        self.assertEqual(status['boot_transition'], 'changed')
        self.assertTrue(status['boot_instance_changed'])
        self.assertEqual(status['last_attached_instance'], 12)
        self.assertFalse(status['motion_available'])
        observer._observe()
        status = observer.get_status(None)
        self.assertEqual(status['boot_transition'], 'same')
        self.assertTrue(status['boot_instance_changed'])
        printer.events['klippy:disconnect']()
        self.assertTrue(observer.get_status(None)['boot_instance_changed'])

    def test_failed_query_and_stale_response_do_not_claim_new_boot(self):
        mcu, printer, observer = fixture()
        mcu.instance, mcu.state = 11, 1
        printer.events['klippy:connect']()
        printer.events['klippy:disconnect']()
        mcu.instance, mcu.fail = 12, True
        printer.events['klippy:connect']()
        self.assertEqual(observer.get_status(None)['last_attached_instance'], 11)
        self.assertIsNone(observer.get_status(None)['boot_transition'])
        self.assertFalse(observer.get_status(None)['boot_instance_changed'])
        self.assertEqual(observer.get_status(None)['state'], 'unavailable')
        self.assertIsNotNone(observer.get_status(None)['last_error'])
        mcu.fail = False
        mcu.query_hook = printer.events['klippy:disconnect']
        printer.events['klippy:connect']()
        self.assertEqual(observer.get_status(None)['last_attached_instance'], 11)
        self.assertIsNone(observer.get_status(None)['boot_transition'])
        self.assertEqual(observer.get_status(None)['state'], 'disconnected')
        self.assertIsNone(observer.get_status(None)['last_error'])
        printer.events['klippy:connect']()
        self.assertEqual(observer.get_status(None)['boot_transition'], 'changed')

    def test_firmware_enumerations_gate_connect(self):
        exported = {name: members(namespace)
                    for name, namespace in ENUMERATIONS.items()}
        for label, enumerations in (('verified', exported), ('legacy', {})):
            with self.subTest(label):
                mcu, printer, observer = fixture()
                mcu.instance, mcu.state = 11, 1
                mcu.enumerations = enumerations
                printer.events['klippy:connect']()
                status = observer.get_status(None)
                self.assertEqual((status['state'], status['instance']),
                                 ('attached', 11))
                self.assertEqual(len(mcu.sent), 1)
        missing = {name: dict(values) for name, values in exported.items()}
        del missing['km_op_state']['LOADED']
        renumbered = {name: dict(values) for name, values in exported.items()}
        renumbered['km_op_state']['LOADED'] = 3
        for label, enumerations, reason in (
                ('missing', missing, 'km_op_state LOADED missing'),
                ('renumbered', renumbered, 'km_op_state LOADED is 3')):
            with self.subTest(label):
                mcu, printer, observer = fixture()
                mcu.instance, mcu.state = 11, 1
                mcu.enumerations = enumerations
                # Optional device: connect returns and the printer continues.
                printer.events['klippy:connect']()
                status = observer.get_status(None)
                self.assertEqual((status['state'], status['connected'],
                                  status['instance']),
                                 ('unavailable', False, None))
                self.assertIn(reason, status['last_error'])
                replies = []
                observer.cmd_KLIPPER_MMU_QUERY(types.SimpleNamespace(
                    respond_info=replies.append))
                self.assertEqual(mcu.sent, [])
                self.assertIn('state=unavailable', replies[0])

    def test_new_host_object_has_no_prior_instance(self):
        mcu, printer, observer = fixture()
        mcu.instance, mcu.state = 11, 1
        printer.events['klippy:connect']()
        other_mcu, other_printer, other = fixture()
        other_mcu.instance, other_mcu.state = 12, 1
        other_printer.events['klippy:connect']()
        self.assertIsNone(other.get_status(None)['boot_transition'])
        self.assertFalse(other.get_status(None)['boot_instance_changed'])
        self.assertEqual(other.get_status(None)['last_attached_instance'], 12)


if __name__ == '__main__':
    unittest.main()
