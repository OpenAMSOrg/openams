# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit stopped-state exchanges: path reconciliation and lane driver restart.

Call on the reactor thread holding the observer's MCU mutex. The
caller supplies the operator's intent; firmware rechecks its own guards and
never moves filament or enables outputs. See docs/path-reconciliation.md.
"""

from dataclasses import dataclass

from .command_transport import exchange
from .wire import (DRIVER_RESTART, DRIVER_RESTART_RESULT, PATH_RECONCILE,
                   PATH_RECONCILE_RESULT, CommandCode, Result, known)

LANE_UNKNOWN = 255
# km_driver_restart's own reserved lane value, naming the selector's own
# TMC2209 instead of a numbered lane (docs/selectors.md). Distinct from
# LANE_UNKNOWN above: that one means "no lane" for CLEAR; this one names a
# real target. Matches mcu/commands.c's KM_DRIVER_RESTART_SELECTOR exactly.
DRIVER_RESTART_SELECTOR = 254
# Negated firmware km_result_t, reported only with administrative result 0.
STATUS_NAMES = {-Result.OK: 'ok', -Result.ERR_INVALID_PARAM: 'invalid',
                -Result.ERR_BUSY: 'busy',
                -Result.ERR_UNAVAILABLE: 'sensor_unavailable',
                -Result.ERR_FAULTED: 'faulted',
                -Result.ERR_CONFLICT: 'sensor_conflict',
                -Result.ERR_STALE: 'nothing_to_reconcile',
                -Result.ERR_NOT_SUPPORTED: 'sensor_not_configured'}
# A restarting lane is asked again, each time with a new fresh capture.
RESTARTING = -Result.ERR_UNAVAILABLE
RESTART_STATUS_NAMES = {-Result.OK: 'ok', -Result.ERR_INVALID_PARAM: 'invalid_lane',
                        -Result.ERR_BUSY: 'busy', RESTARTING: 'restarting',
                        -Result.ERR_FAULTED: 'faulted',
                        -Result.ERR_NOT_SUPPORTED: 'not_a_tmc_feeder'}
RESTART_WAIT = 2.0
RESTART_POLL = 0.1


@dataclass(frozen=True)
class PathReconcileResult:
    instance: int
    query: int
    result: int  # Administrative km_command_result code.
    status: int  # Key of the command's status names; zero unless result is zero.


def _decode(params, names, what):
    fields = []
    for name, bits in (('instance_hi', 32), ('instance_lo', 32), ('query', 32),
                       ('result', 8), ('status', 8)):
        value = params.get(name)
        if type(value) is not int or not 0 <= value < 1 << bits:
            raise ValueError('Malformed %s reply' % what)
        fields.append(value)
    hi, lo, query, result, status = fields
    if (result not in known(CommandCode) or status not in names
            or (result != CommandCode.OK and status)):
        raise ValueError('Unsupported %s reply' % what)
    return PathReconcileResult(hi << 32 | lo, query, result, status)


def decode_path_reconcile_result(params):
    return _decode(params, STATUS_NAMES, 'path reconciliation')


def decode_driver_restart_result(params):
    return _decode(params, RESTART_STATUS_NAMES, 'driver restart')


def _exchange(mcu, reactor, instance, query, is_current, timeout, what,
              command_format, result_format, decode, tail):
    if (type(instance) is not int or not 0 < instance < 1 << 64
            or type(query) is not int or not 0 < query < 1 << 32
            or not callable(is_current) or type(timeout) not in (int, float)
            or not 0 < timeout <= 60):
        raise ValueError('Invalid %s request' % what)

    def on_result(params):
        if params.get('query') != query:
            return None
        reply = decode(params)
        if reply.instance != instance:
            raise ValueError('MMU controller instance changed')
        return reply

    return exchange(mcu, reactor, [(command_format, [
        instance >> 32, instance & 0xffffffff, query] + tail)],
        {result_format: on_result}, is_current, timeout, what)


def _lane(lane, what):
    if type(lane) is not int or not 0 <= lane < 8:
        raise ValueError('Invalid %s request' % what)
    return lane


def send_path_reconcile(mcu, reactor, instance, query, lane, is_current,
                        timeout=5.0):
    """lane=None requests CLEAR; an integer lane requests ADOPT.

    query must identify the latest fresh physical capture, used at most once
    for reconciliation. A timeout leaves the outcome
    unknown: read a fresh snapshot before deciding whether to retry.
    """
    what = 'path reconciliation'
    tail = [0, LANE_UNKNOWN] if lane is None else [1, _lane(lane, what)]
    return _exchange(mcu, reactor, instance, query, is_current, timeout, what,
                     PATH_RECONCILE, PATH_RECONCILE_RESULT,
                     decode_path_reconcile_result, tail)


def send_driver_restart(mcu, reactor, instance, query, lane, is_current,
                        timeout=5.0):
    """One firmware restart step for a stopped lane's TMC feeder driver, or
    for the selector's own TMC2209 (lane=DRIVER_RESTART_SELECTOR).

    The first step restarts the chip; RESTARTING asks for another step with a
    newer capture. Status 0 means the chip is ready and the lane's (or the
    selector's) STEP/DIR latch is cleared. A timeout leaves the outcome
    unknown.
    """
    what = 'driver restart'
    value = lane if lane == DRIVER_RESTART_SELECTOR else _lane(lane, what)
    return _exchange(mcu, reactor, instance, query, is_current, timeout, what,
                     DRIVER_RESTART, DRIVER_RESTART_RESULT, decode_driver_restart_result,
                     [value])
