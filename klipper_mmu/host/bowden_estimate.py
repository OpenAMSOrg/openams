# SPDX-License-Identifier: GPL-3.0-or-later
"""Retained step-estimated HUB-to-FPS path length; never an encoder measurement.

Without an encoder and toolhead switch, firmware PTFE calibration reports the
completed feeder microsteps from HUB to qualified FPS arrival and flags them as
an estimate (docs/fps-arrival.md). This opt-in per-printer file keeps the last
ACK-confirmed result, tagged with the MCU dictionary's integer KLIPPER_MMU_*
constants and the DiffA/DiffB calibration file hash it was measured against.
Its only use is to restore the calibrated HUB-to-arrival distance at the next
configuration of the same firmware and calibration (fps_steps), so LOAD does
not need a fresh CALIBRATE every session. The firmware's own arrival window
bounds acceptance around that distance; this file carries no margin.
"""
import json
import os
import string
import tempfile
from dataclasses import dataclass

from .command_transport import exchange
from .protocol import Event, event_flags_supported
from .wire import (CALIBRATED_ARRIVAL_SET, CALIBRATED_ARRIVAL_SET_RESULT,
                   CommandCode, EventFlag, EventType, OpCode, OpType, known)

MAX_BYTES = 8192
LABELS = {'kind': 'step_estimate', 'unit': 'feeder_microsteps',
          'span': 'hub_to_fps_arrival'}
FIELDS = set(LABELS) | {'steps', 'lane', 'mmu', 'firmware', 'diff_calibration_sha256'}


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key %s' % key)
        result[key] = value
    return result


def _validate(record):
    if type(record) is not dict or set(record) != {'estimate'}:
        raise ValueError('expected an estimate object')
    estimate = record['estimate']
    if type(estimate) is not dict or set(estimate) != FIELDS:
        raise ValueError('invalid estimate fields')
    if any(estimate[key] != value for key, value in LABELS.items()):
        raise ValueError('estimate is not labeled as step-estimated microsteps')
    firmware, digest = estimate['firmware'], estimate['diff_calibration_sha256']
    if (type(estimate['steps']) is not int or not 0 < estimate['steps'] < 1 << 32
            or type(estimate['lane']) is not int or not 0 <= estimate['lane'] < 8
            or type(estimate['mmu']) is not str or not estimate['mmu']
            or type(firmware) is not dict or not firmware
            or any(type(key) is not str or not key.startswith('KLIPPER_MMU_')
                   or type(value) is not int for key, value in firmware.items())
            or digest is not None and (type(digest) is not str or len(digest) != 64
                                       or set(digest) - set(string.hexdigits.lower()))):
        raise ValueError('invalid estimate values')
    return estimate


def firmware_identity(constants):
    return {key: value for key, value in constants.items()
            if type(key) is str and key.startswith('KLIPPER_MMU_') and type(value) is int}


def is_step_estimate(event):
    """True only for a successful CALIBRATE terminal flagged as an estimate."""
    return (isinstance(event, Event)
            and event.event_type == EventType.PTFE_CAL_COMPLETE
            and event.identity.operation == OpType.CALIBRATE
            and event.result_code == OpCode.SUCCESS
            and bool(event.flags & EventFlag.STEP_ESTIMATE)
            and event_flags_supported(event))


class BowdenEstimate:
    def __init__(self, path):
        path = os.path.expanduser(path)
        if not os.path.isabs(path):
            raise ValueError('bowden_estimate_file must be an absolute path')
        self.path = path
        self._estimate = None
        try:
            with open(path, 'rb') as source:
                contents = source.read(MAX_BYTES + 1)
        except FileNotFoundError:
            return
        if len(contents) > MAX_BYTES:
            raise ValueError('file exceeds %d bytes' % MAX_BYTES)
        self._estimate = _validate(json.loads(contents.decode('utf-8'),
                                              object_pairs_hook=_unique_pairs))

    def status(self):
        if self._estimate is None:
            return None
        return dict(self._estimate, firmware=dict(self._estimate['firmware']))

    def _persist(self, estimate):
        """Atomically make `estimate` (or None) the retained current one;
        the write half shared by record() and restore()."""
        if estimate is None:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass
            self._estimate = None
            return
        contents = (json.dumps({'estimate': estimate}, sort_keys=True,
                               separators=(',', ':')) + '\n').encode('utf-8')
        if len(contents) > MAX_BYTES:
            raise ValueError('estimate exceeds %d bytes' % MAX_BYTES)
        fd, temporary = tempfile.mkstemp(prefix='.bowden-estimate-',
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
        self._estimate = estimate

    def fps_steps(self, mmu, constants, diff_calibration_sha256):
        """The calibrated HUB-to-arrival distance in microsteps, or None
        without a matching estimate for this MMU, firmware identity and
        calibration, or one outside the firmware's advertised outer limits
        (the firmware's own config command would otherwise refuse it and
        shut the MCU down). None leaves LOAD refused until CALIBRATE runs."""
        low = constants.get('KLIPPER_MMU_FPS_MIN_STEPS')
        high = constants.get('KLIPPER_MMU_FPS_MAX_STEPS')
        estimate = self._estimate
        if (estimate is None or type(low) is not int or type(high) is not int
                or estimate['mmu'] != mmu
                or estimate['diff_calibration_sha256'] != diff_calibration_sha256
                or estimate['firmware'] != firmware_identity(constants)):
            return None
        steps = estimate['steps']
        return steps if low <= steps <= high else None

    def record(self, event, mmu, constants, diff_calibration_sha256):
        """Replace the retained result with one ACK-confirmed estimate."""
        if not is_step_estimate(event):
            return False
        estimate = dict(LABELS, steps=event.value, lane=event.identity.lane, mmu=mmu,
                        firmware=firmware_identity(constants),
                        diff_calibration_sha256=diff_calibration_sha256)
        _validate({'estimate': estimate})
        self._persist(estimate)
        return True

    def restore(self, previous):
        """Put a previously retained estimate (or None) back as current,
        undoing what record() just wrote for this same CALIBRATE. Used only
        by KLIPPER_MMU_CALIBRATE SAVE=0: the measurement still happened and
        record() already persisted it (is_step_estimate does not know
        SAVE), so this is the one place that puts back what was there
        before it, on both the file and the live firmware (see
        send_calibrated_arrival_set) -- unlike a per-gate selector
        calibration, calibrated_arrival_steps is one value shared by every
        lane, so measuring lane 1 without a real recalibration must not
        leave every other lane aiming at it."""
        self._persist(previous)


@dataclass
class CalibratedArrivalResult:
    result: int  # km_command_result (CommandCode).
    status: int  # Negated km_result_t; meaningful only when result is OK.


def _decode_calibrated_arrival(params, instance):
    fields = []
    for name, bits in (('instance_hi', 32), ('instance_lo', 32),
                       ('result', 8), ('status', 8)):
        value = params.get(name)
        if type(value) is not int or not 0 <= value < 1 << bits:
            raise ValueError('Malformed calibrated arrival reply')
        fields.append(value)
    hi, lo, result, status = fields
    if (hi << 32 | lo) != instance:
        raise ValueError('MMU controller instance changed')
    if result not in known(CommandCode) or (result != CommandCode.OK and status):
        raise ValueError('Unsupported calibrated arrival reply')
    return CalibratedArrivalResult(result, status)


def send_calibrated_arrival_set(mcu, reactor, instance, is_current, steps,
                                timeout=5.0):
    """Runtime restore of the calibrated HUB-to-arrival distance (reference
    feeder microsteps; 0 clears it). Idle-only in firmware
    (operation_core.h km_core_set_calibrated_arrival_steps)."""
    if type(steps) is not int or not 0 <= steps < 1 << 32:
        raise ValueError('Invalid calibrated arrival request')
    if type(instance) is not int or not 0 < instance < 1 << 64:
        raise ValueError('Invalid calibrated arrival request')

    def on_result(params):
        return _decode_calibrated_arrival(params, instance)

    return exchange(mcu, reactor, [(CALIBRATED_ARRIVAL_SET, [
        instance >> 32, instance & 0xffffffff, steps])],
        {CALIBRATED_ARRIVAL_SET_RESULT: on_result}, is_current, timeout,
        'calibrated arrival command')
