# SPDX-License-Identifier: GPL-3.0-or-later
"""Wire-format regressions for the pure host calibration encoder."""
import copy
import pathlib
import struct
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from host.calibration import encode_calibration


RECORD = {
    'channel_map': 'a_low_b_high',
    'vref_mv': 3300,
    'min_valid_mv': 100,
    'max_valid_mv': 3200,
    'path_tolerance_counts': 25,
    'points': [
        {'a_raw': 200, 'b_raw': 2200, 'position_permille': 0},
        {'a_raw': 1000, 'b_raw': 3000, 'position_permille': 500},
        {'a_raw': 2000, 'b_raw': 3900, 'position_permille': 1000},
    ],
}


class EncoderTests(unittest.TestCase):
    def test_wire_fields_and_implicit_endpoint_positions(self):
        encoded = encode_calibration(RECORD)
        self.assertEqual(len(encoded), 24)
        self.assertEqual(encoded, struct.pack(
            '<BBHHHHHHHHHHH', 0, 3, 3300, 100, 3200, 25,
            200, 2200, 1000, 3000, 500, 2000, 3900))

    def test_b_low_map_canonicalizes_physical_pairs(self):
        record = copy.deepcopy(RECORD)
        record['channel_map'] = 'b_low_a_high'
        for point in record['points']:
            point['a_raw'], point['b_raw'] = point['b_raw'], point['a_raw']
        encoded = encode_calibration(record)
        self.assertEqual(encoded[:2], bytes((1, 3)))
        # The MCU stores these low/high pairs; its live decoder swaps B/A too.
        self.assertEqual(encoded[2:], encode_calibration(RECORD)[2:])

    def test_rejects_values_the_mcu_cannot_accept(self):
        cases = (
            ('vref_mv', 0),
            ('min_valid_mv', 3300),
            ('max_valid_mv', 100),
            ('path_tolerance_counts', True),
            ('path_tolerance_counts', 4096),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                record = copy.deepcopy(RECORD)
                record[field] = value
                with self.assertRaises(ValueError):
                    encode_calibration(record)
        for change in ({'a_raw': 4096}, {'position_permille': 0},
                       {'a_raw': 100}, {'b_raw': True}):
            with self.subTest(change=change):
                record = copy.deepcopy(RECORD)
                record['points'][1].update(change)
                with self.assertRaises(ValueError):
                    encode_calibration(record)


if __name__ == '__main__':
    unittest.main()
