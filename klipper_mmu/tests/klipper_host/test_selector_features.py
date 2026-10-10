# SPDX-License-Identifier: GPL-3.0-or-later
import unittest

from host import KlipperMMU
from host.protocol import Event, SnapshotAssembler
from host.requests import Requests
from host.wire import CONFIG_SELECTOR_GATE, EventType, OpCode, OpType

class FakeMCU:
    def __init__(self, lane_count=2, has_selector=True, fileoutput=False):
        self.constants = {'KLIPPER_MMU_LANE_COUNT': lane_count}
        self.has_selector = has_selector
        self.fileoutput = fileoutput
        self.cmds = []
        self.oids = 0

    def get_constants(self): return self.constants
    def is_fileoutput(self): return self.fileoutput
    def try_lookup_command(self, cmd):
        # Klipper's real msgparser.lookup_command() requires an exact match
        # against the full registered wire format (name AND params), not
        # just the bare command name (regression: host/__init__.py used to
        # pass the bare name here, which real Klipper always rejects as a
        # "Command format mismatch" -- try_lookup_command always returned
        # None, so _config_selector_gates() never sent a host-calibrated
        # gate table on any real MCU. This fake now matches that real
        # contract instead of the bug.)
        return object() if cmd == CONFIG_SELECTOR_GATE and self.has_selector else None
    def create_oid(self):
        self.oids += 1
        return self.oids
    def add_config_cmd(self, cmd):
        self.cmds.append(cmd)


class FakePrinter:
    def config_error(self, msg): return ValueError(msg)


def device(mcu, steps, calibration=None):
    host = KlipperMMU.__new__(KlipperMMU)
    host.__dict__.update(_mcu=mcu, _printer=FakePrinter(), _selector_gate_steps=steps,
                         _name='box', _selector_calibration=calibration)
    return host


class SelectorHostTest(unittest.TestCase):
    def test_gate_table_config_sent(self):
        mcu = FakeMCU()
        device(mcu, [120, 240])._config_selector_gates()
        self.assertEqual(mcu.cmds, ['config_km_selector_gate oid=1 gate=0 steps=120',
                                    'config_km_selector_gate oid=2 gate=1 steps=240'])

    def test_gate_table_config_mismatch_refused(self):
        with self.assertRaisesRegex(ValueError, 'length must match lane count'):
            device(FakeMCU(), [120, 240, 360])._config_selector_gates()

    def test_gate_table_config_skipped_without_command_or_in_file_output(self):
        for mcu in (FakeMCU(has_selector=False), FakeMCU(fileoutput=True)):
            device(mcu, [120, 240])._config_selector_gates()
            self.assertEqual(mcu.cmds, [])

    def test_status_parse_selector(self):
        assembler = SnapshotAssembler(1, 1, selector_capable=True)
        assembler.add('km_status_identity', {'instance_hi': 0, 'instance_lo': 1, 'query': 1, 'state': 0, 'stage': 0, 'operation': 0, 'client': 0, 'session': 0, 'request': 0, 'lane': 0, 'owner_lane': 0, 'occupant_lane': 0, 'flags': 0, 'queued_events': 0, 'head_event_seq': 0})
        assembler.add('km_status_motion', {'instance_hi': 0, 'instance_lo': 1, 'query': 1, 'velocity_bits': 0, 'position_hi': 0, 'position_lo': 0, 'buffer_position_bits': 0, 'system_fault': 0, 'motion_fault': 0, 'follower_state': 1, 'follower_direction': 0, 'follower_fault': 0})
        assembler.add('km_status_time', {'instance_hi': 0, 'instance_lo': 1, 'query': 1, 'commanded_current_ma': 0, 'measured_current_ma': 0, 'measured_current_sample_ms': 0, 'control_ms': 0, 'control_us': 0, 'acquired_ms': 0, 'acquired_us': 0})
        assembler.add('km_status_sessions', {'instance_hi': 0, 'instance_lo': 1, 'query': 1, 'client0': 0, 'client1': 0, 'client2': 0, 'client3': 0})
        assembler.add('km_status_requests', {'instance_hi': 0, 'instance_lo': 1, 'query': 1, 'client0': 0, 'client1': 0, 'client2': 0, 'client3': 0})
        
        snap = assembler.add('km_status_selector', {'instance_hi': 0, 'instance_lo': 1, 'query': 1, 'homed': 1, 'gate': 2})
        self.assertIsNotNone(snap)
        
        self.assertTrue(assembler.delivered)
        vals = snap.values
        self.assertEqual(vals['homed'], 1)
        self.assertEqual(vals['gate'], 2)


class HomeRequestTest(unittest.TestCase):
    def test_home_has_no_lane_and_settles_on_its_terminal(self):
        requests = Requests(0x1234, 13)
        identity = requests.issue_home()
        self.assertEqual((identity.operation, identity.lane), (OpType.HOME, 0xff))
        with self.assertRaisesRegex(ValueError, 'local request'):
            requests.issue_eject(1)
        terminal = Event(0x1234, identity, 1, 100, EventType.HOME_COMPLETE, OpCode.SUCCESS, 0, 0)
        self.assertTrue(requests.accept_home(terminal, requests.generation))
        self.assertIs(requests.home_terminal, terminal)
        self.assertIsNone(requests.pending_home)
        # A finished HOME never blocks the next one.
        self.assertEqual(requests.issue_home().request, identity.request + 1)

    def test_lanes_reach_eleven_and_no_further(self):
        requests = Requests(0x1234, 13)
        self.assertEqual(requests.issue_eject(11).lane, 11)
        requests.pending_eject = None
        with self.assertRaisesRegex(ValueError, 'Invalid MMU lane'):
            requests.issue_eject(12)
        with self.assertRaisesRegex(ValueError, 'Invalid MMU lane'):
            requests.issue_eject(0xff)


class DeviceIdTableTest(unittest.TestCase):
    def test_every_implemented_device_id_has_a_host_family(self):
        # A device the firmware implements but the host cannot name gets no
        # motion session (_motion_composition_matches), so the table must
        # cover every KLIPPER_MMU_DEVICE_ID of KLIPPER_MMU_PROFILE_IMPLEMENTED.
        import re
        from pathlib import Path
        from host import _OPENAMS_DEVICE_IDS
        text = (Path(__file__).resolve().parents[2] / 'mcu/Kconfig.devices').read_text()
        implemented = re.search(r'config KLIPPER_MMU_PROFILE_IMPLEMENTED\n\s+bool\n\s+default y if ([^\n]+)',
                                text).group(1)
        ids = dict(re.findall(r'default (\d+) if KLIPPER_MMU_DEVICE_(\w+)', text))
        ids = {name: int(value) for value, name in ids.items()}
        known = set(_OPENAMS_DEVICE_IDS.values())
        for name in re.findall(r'KLIPPER_MMU_DEVICE_(\w+)', implemented):
            self.assertIn(ids[name], known, name)
