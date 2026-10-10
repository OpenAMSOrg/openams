# SPDX-License-Identifier: GPL-3.0-or-later
'''Pack one measured DiffA/DiffB calibration dict into the MCU wire record.

Conversion only: no I/O, no curve synthesis, and no work at import.
'''

import struct

_RECORD_KEYS = frozenset((
    'channel_map',
    'vref_mv',
    'min_valid_mv',
    'max_valid_mv',
    'path_tolerance_counts',
    'points',
))
_POINT_KEYS = frozenset(('a_raw', 'b_raw', 'position_permille'))
_CHANNEL_MAP = {
    'a_low_b_high': 0,
    'b_low_a_high': 1,
}


def _require_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError('%s must be an integer' % (name,))
    return value


def encode_calibration(record) -> bytes:
    '''Return the fixed little-endian calibration bytes, or raise ValueError.'''
    if not isinstance(record, dict):
        raise ValueError('record must be a dict')
    if set(record) != _RECORD_KEYS:
        raise ValueError('record must contain exactly the calibration keys')
    channel = record['channel_map']
    if not isinstance(channel, str) or channel not in _CHANNEL_MAP:
        raise ValueError('channel_map must be a_low_b_high or b_low_a_high')
    vref_mv = _require_int(record['vref_mv'], 'vref_mv')
    if vref_mv < 1 or vref_mv > 65535:
        raise ValueError('vref_mv must be 1..65535')
    min_valid_mv = _require_int(record['min_valid_mv'], 'min_valid_mv')
    if min_valid_mv < 0 or min_valid_mv > vref_mv - 1:
        raise ValueError('min_valid_mv must be 0..vref_mv-1')
    max_valid_mv = _require_int(record['max_valid_mv'], 'max_valid_mv')
    if max_valid_mv != 0 and (
            max_valid_mv < min_valid_mv + 1 or max_valid_mv > vref_mv):
        raise ValueError('max_valid_mv must be 0 or min_valid_mv+1..vref_mv')
    tolerance = _require_int(
        record['path_tolerance_counts'], 'path_tolerance_counts')
    if tolerance < 1 or tolerance > 4095:
        raise ValueError('path_tolerance_counts must be 1..4095')
    points = record['points']
    if not isinstance(points, list):
        raise ValueError('points must be a list')
    count = len(points)
    if count < 2 or count > 8:
        raise ValueError('points must contain 2..8 entries')
    parsed = []
    for index, point in enumerate(points):
        if not isinstance(point, dict) or set(point) != _POINT_KEYS:
            raise ValueError(
                'point %d must contain exactly a_raw, b_raw, position_permille'
                % (index,))
        prefix = 'points[%d]' % (index,)
        a_raw = _require_int(point['a_raw'], prefix + '.a_raw')
        b_raw = _require_int(point['b_raw'], prefix + '.b_raw')
        position = _require_int(
            point['position_permille'], prefix + '.position_permille')
        if a_raw < 0 or a_raw > 4095 or b_raw < 0 or b_raw > 4095:
            raise ValueError('%s raw counts must be 0..4095' % (prefix,))
        if index == 0:
            if position != 0:
                raise ValueError('first position_permille must be 0')
        elif index == count - 1:
            if position != 1000:
                raise ValueError('last position_permille must be 1000')
        elif position < 1 or position > 999:
            raise ValueError(
                '%s position_permille must be 1..999' % (prefix,))
        if parsed:
            prev_a, prev_b, prev_pos = parsed[-1]
            if position <= prev_pos:
                raise ValueError('position_permille must be strictly increasing')
            if a_raw < prev_a or b_raw < prev_b:
                raise ValueError('a_raw and b_raw must be nondecreasing')
            if a_raw == prev_a and b_raw == prev_b:
                raise ValueError('paired raw segment length must be non-zero')
        parsed.append((a_raw, b_raw, position))
    channel_code = _CHANNEL_MAP[channel]
    chunks = [struct.pack(
        '<BBHHHH', channel_code, count, vref_mv, min_valid_mv,
        max_valid_mv, tolerance)]
    last = count - 1
    for index, (a_raw, b_raw, position) in enumerate(parsed):
        # The JSON names are physical pins. The portable decoder swaps its
        # inputs for map 1, so its calibration table must use low/high order.
        if channel_code == 1:
            a_raw, b_raw = b_raw, a_raw
        if index == 0 or index == last:
            chunks.append(struct.pack('<HH', a_raw, b_raw))
        else:
            chunks.append(struct.pack('<HHH', a_raw, b_raw, position))
    encoded = b''.join(chunks)
    expected = 6 * count + 6
    if len(encoded) != expected or expected > 54:
        raise ValueError('encoded length must be 6*n+6 and at most 54')
    return encoded
