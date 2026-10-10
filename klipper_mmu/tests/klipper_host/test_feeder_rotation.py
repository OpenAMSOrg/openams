# SPDX-License-Identifier: GPL-3.0-or-later
import unittest

from host import KlipperMMU
from host.wire import CONFIG_FEEDER_ROTATION


class FakeMCU:
    def __init__(self, lane_count=2, has_feeder_rotation=True, fileoutput=False):
        self.constants = {'KLIPPER_MMU_LANE_COUNT': lane_count}
        self.has_feeder_rotation = has_feeder_rotation
        self.fileoutput = fileoutput
        self.cmds = []
        self.oids = 0

    def get_constants(self): return self.constants

    def is_fileoutput(self): return self.fileoutput

    def try_lookup_command(self, cmd):
        return object() if cmd == CONFIG_FEEDER_ROTATION and self.has_feeder_rotation else None

    def create_oid(self):
        self.oids += 1
        return self.oids

    def add_config_cmd(self, cmd):
        self.cmds.append(cmd)


class FakePrinter:
    def config_error(self, msg): return ValueError(msg)


def device(mcu, distances):
    host = KlipperMMU.__new__(KlipperMMU)
    host.__dict__.update(_mcu=mcu, _printer=FakePrinter(),
                         _feeder_rotation_distances_mm=distances, _name='box')
    return host


class TestFeederRotationDistances(unittest.TestCase):
    def test_four_lanes_emit_scaled_commands(self):
        mcu = FakeMCU(lane_count=4)
        host = device(mcu, [22.0, 22.88, 22.0, 21.12])
        host._config_feeder_rotation_distances()
        self.assertEqual(mcu.cmds, [
            'config_km_feeder_rotation oid=1 lane=0 um=22000',
            'config_km_feeder_rotation oid=2 lane=1 um=22880',
            'config_km_feeder_rotation oid=3 lane=2 um=22000',
            'config_km_feeder_rotation oid=4 lane=3 um=21120',
        ])

    def test_length_mismatch_is_refused_without_commands(self):
        mcu = FakeMCU(lane_count=4)
        host = device(mcu, [22.0, 22.88, 22.0])
        with self.assertRaisesRegex(ValueError, 'length must match lane count'):
            host._config_feeder_rotation_distances()
        self.assertEqual(mcu.cmds, [])

    def test_firmware_without_command_is_refused(self):
        mcu = FakeMCU(lane_count=2, has_feeder_rotation=False)
        host = device(mcu, [22.0, 22.88])
        with self.assertRaisesRegex(ValueError, 'no per-lane rotation distance'):
            host._config_feeder_rotation_distances()
        self.assertEqual(mcu.cmds, [])

    def test_fileoutput_is_a_noop(self):
        mcu = FakeMCU(lane_count=2, fileoutput=True)
        host = device(mcu, [22.0, 22.88])
        host._config_feeder_rotation_distances()
        self.assertEqual(mcu.cmds, [])

    def test_rounding_to_whole_micrometres(self):
        mcu = FakeMCU(lane_count=2)
        host = device(mcu, [22.0004, 22.0006])
        host._config_feeder_rotation_distances()
        self.assertEqual(mcu.cmds, [
            'config_km_feeder_rotation oid=1 lane=0 um=22000',
            'config_km_feeder_rotation oid=2 lane=1 um=22001',
        ])


if __name__ == '__main__':
    unittest.main()
