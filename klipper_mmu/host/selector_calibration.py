# SPDX-License-Identifier: GPL-3.0-or-later
"""Selector gate/angle calibration: measured values that override the Kconfig
default and selector_gate_steps, one gate at a time (docs/selectors.md), and
the runtime firmware exchanges that carry them (gate write, servo test move,
and releasing the selector's motors so it can be moved by hand).

The stored value is exactly the wire's own km_selector_gate_set value=%i:
selector microsteps for the LINEAR/ROTARY kinds, degrees for SERVO, -1
clearing either kind's gate back to uncalibrated (klipper_mmu/selector.h).
Same load/validate/atomic-save file shape as bowden_estimate.py, and the same
firmware_identity tag, so a table measured against one firmware build is
never silently applied to a different one.
"""
import json
import os
import tempfile
from dataclasses import dataclass

from .bowden_estimate import firmware_identity
from .command_transport import exchange
from .wire import (CommandCode, Result, SELECTOR_GATE_SET,
                   SELECTOR_GATE_SET_RESULT, SELECTOR_MOTORS_OFF,
                   SELECTOR_MOTORS_OFF_RESULT, SELECTOR_SERVO_TEST,
                   SELECTOR_SERVO_TEST_RESULT, known)

MAX_BYTES = 8192
LABELS = {'kind': 'selector_gate'}
FIELDS = set(LABELS) | {'mmu', 'firmware', 'gates'}
CLEAR = -1  # Uncalibrated, either kind (klipper_mmu/selector.h).
# The negated km_result_t a command's reply carries; meaningful only with
# result CommandCode.OK. Mirrors path_reconciliation.py's own STATUS_NAMES.
STATUS_NAMES = {-Result.OK: 'ok', -Result.ERR_INVALID_PARAM: 'invalid',
               -Result.ERR_BUSY: 'busy', -Result.ERR_UNAVAILABLE: 'unavailable'}


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key %s' % key)
        result[key] = value
    return result


def _validate(record):
    if type(record) is not dict or set(record) != {'calibration'}:
        raise ValueError('expected a calibration object')
    calibration = record['calibration']
    if type(calibration) is not dict or set(calibration) != FIELDS:
        raise ValueError('invalid calibration fields')
    if any(calibration[key] != value for key, value in LABELS.items()):
        raise ValueError('calibration is not labeled as a selector gate table')
    firmware, gates, mmu = (calibration['firmware'], calibration['gates'],
                            calibration['mmu'])
    if (type(mmu) is not str or not mmu
            or type(firmware) is not dict or not firmware
            or any(type(key) is not str or not key.startswith('KLIPPER_MMU_')
                   or type(value) is not int for key, value in firmware.items())
            or type(gates) is not dict
            or any(type(g) is not str or not g.isdigit() or not 0 <= int(g) < 256
                   or type(v) is not int or not -1 <= v <= 0xffff
                   for g, v in gates.items())):
        raise ValueError('invalid calibration values')
    return calibration


class SelectorCalibration:
    """One printer's measured selector_calibration_file (opt-in)."""

    def __init__(self, path):
        path = os.path.expanduser(path)
        if not os.path.isabs(path):
            raise ValueError('selector_calibration_file must be an absolute path')
        self.path = path
        self._calibration = None
        try:
            with open(path, 'rb') as source:
                contents = source.read(MAX_BYTES + 1)
        except FileNotFoundError:
            return
        if len(contents) > MAX_BYTES:
            raise ValueError('file exceeds %d bytes' % MAX_BYTES)
        self._calibration = _validate(json.loads(
            contents.decode('utf-8'), object_pairs_hook=_unique_pairs))

    def status(self):
        if self._calibration is None:
            return None
        return dict(self._calibration, firmware=dict(self._calibration['firmware']),
                   gates=dict(self._calibration['gates']))

    def gates_for(self, mmu, constants):
        """{gate: value} measured for this exact mmu name and firmware
        identity, or {} without a matching stored table."""
        calibration = self._calibration
        if (calibration is None or calibration['mmu'] != mmu
                or calibration['firmware'] != firmware_identity(constants)):
            return {}
        return {int(g): v for g, v in calibration['gates'].items()}

    def save(self, mmu, constants, values):
        """Merge {gate: value} into the stored table for this mmu/firmware --
        a different mmu name or firmware identity replaces the whole table,
        the same one it was measured against -- and persist atomically."""
        gates = dict(self.gates_for(mmu, constants))
        gates.update(values)
        calibration = dict(LABELS, mmu=mmu, firmware=firmware_identity(constants),
                          gates={str(g): v for g, v in sorted(gates.items())})
        _validate({'calibration': calibration})
        contents = (json.dumps({'calibration': calibration}, sort_keys=True,
                              separators=(',', ':')) + '\n').encode('utf-8')
        if len(contents) > MAX_BYTES:
            raise ValueError('calibration exceeds %d bytes' % MAX_BYTES)
        fd, temporary = tempfile.mkstemp(prefix='.selector-calibration-',
                                        dir=os.path.dirname(self.path))
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(contents)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self._calibration = calibration


def extrapolate_linear(gates, lane_count, gate, value):
    """Happy Hare's own linear/rotary-selector rule
    (mmu_linear_selector.py:392-400): once both gate 0 and the last gate are
    known, distribute every gate evenly between them; otherwise only the one
    just-measured gate changes. Returns the full replacement {gate: value}
    map to save (a subset of one entry when extrapolation does not apply)."""
    gates = dict(gates)
    gates[gate] = value
    last = lane_count - 1
    other = last if gate == 0 else 0 if gate == last else None
    if other is None or lane_count < 2 or gates.get(other, CLEAR) == CLEAR:
        return {gate: value}
    low, high = gates[0], gates[last]
    spacing = (high - low) / last
    return {g: round(low + g * spacing) for g in range(lane_count)}


def spaced_angles(gate, angle, spacing, lane_count):
    """Happy Hare's own MMU_CALIBRATE_SERVO_SELECTOR SPACING rule
    (mmu_servo_selector.py _generate_gate_angles): every gate's angle is
    `spacing` degrees from its neighbour's, anchored at `gate`'s `angle`."""
    start = angle - gate * spacing
    return {g: round(start + g * spacing) for g in range(lane_count)}


@dataclass(frozen=True)
class SelectorCommandResult:
    result: int  # km_command_result (CommandCode).
    status: int  # Negated km_result_t; meaningful only when result is OK.


def _decode(params, instance):
    fields = []
    for name, bits in (('instance_hi', 32), ('instance_lo', 32),
                       ('result', 8), ('status', 8)):
        value = params.get(name)
        if type(value) is not int or not 0 <= value < 1 << bits:
            raise ValueError('Malformed selector command reply')
        fields.append(value)
    hi, lo, result, status = fields
    if (hi << 32 | lo) != instance:
        raise ValueError('MMU controller instance changed')
    if (result not in known(CommandCode)
            or (status not in STATUS_NAMES and status)
            or (result != CommandCode.OK and status)):
        raise ValueError('Unsupported selector command reply')
    return SelectorCommandResult(result, status)


def _exchange(mcu, reactor, instance, is_current, command_format, result_format,
             tail, timeout):
    if type(instance) is not int or not 0 < instance < 1 << 64:
        raise ValueError('Invalid selector command request')

    def on_result(params):
        reply = _decode(params, instance)
        return reply

    return exchange(mcu, reactor, [(command_format, [
        instance >> 32, instance & 0xffffffff] + tail)],
        {result_format: on_result}, is_current, timeout, 'selector command')


def send_selector_gate_set(mcu, reactor, instance, is_current, gate, value,
                           timeout=5.0):
    """value is steps (LINEAR/ROTARY) or degrees (SERVO); -1 clears either
    kind's gate back to uncalibrated. Returns a SelectorCommandResult."""
    if type(gate) is not int or not 0 <= gate < 256 or type(value) is not int:
        raise ValueError('Invalid selector gate request')
    return _exchange(mcu, reactor, instance, is_current, SELECTOR_GATE_SET,
                     SELECTOR_GATE_SET_RESULT, [gate, value], timeout)


def send_selector_servo_test(mcu, reactor, instance, is_current, degrees,
                             timeout=5.0):
    """Idle-only jog of the SERVO kind's servo to `degrees`, not saved
    anywhere; save a good angle afterwards with send_selector_gate_set."""
    if type(degrees) is not int:
        raise ValueError('Invalid selector servo test request')
    return _exchange(mcu, reactor, instance, is_current, SELECTOR_SERVO_TEST,
                     SELECTOR_SERVO_TEST_RESULT, [degrees], timeout)


def send_selector_motors_off(mcu, reactor, instance, is_current, timeout=5.0):
    """Happy Hare's MMU_MOTORS_OFF equivalent: idle-only, releases the
    selector (and its shared gear) so it can be moved by hand to align it
    at a gate before send_selector_gate_set/KLIPPER_MMU_CALIBRATE_SELECTOR."""
    return _exchange(mcu, reactor, instance, is_current, SELECTOR_MOTORS_OFF,
                     SELECTOR_MOTORS_OFF_RESULT, [], timeout)
