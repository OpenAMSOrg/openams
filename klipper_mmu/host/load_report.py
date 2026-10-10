# SPDX-License-Identifier: GPL-3.0-or-later
"""Why and where each attempt of one LOAD ended (docs/fps-arrival.md).

Firmware feeds a LOAD up to three times and keeps, until the next LOAD, one
record per attempt: the stage and fault that ended it and the completed feeder
travel. The host reads them once after the LOAD terminal. The report is
diagnostic only; it never settles or retries an operation.
"""
from dataclasses import dataclass

from .command_transport import exchange
from .wire import (LOAD_ATTEMPTS, LOAD_ATTEMPTS_QUERY, EventType, Fault, Stage,
                   known, members)

ATTEMPTS = 3  # KM_LOAD_ATTEMPTS
BUDGET_S = 240  # KM_LOAD_BUDGET_MS
RETRIED = (Fault.NO_PROGRESS, Fault.JAM)
SEGMENTS = {Stage.LOAD_FEED_TO_HUB: 'lane to HUB',
            Stage.LOAD_FEED_TO_BUFFER: 'HUB to extruder'}
CAUSES = {
    (Stage.LOAD_FEED_TO_HUB, Fault.NO_PROGRESS):
        'no HUB after %s: tip catching at the hub entry, spool snag, tangle '
        'or filament not gripped',
    # The FPS reads compression only; any resistance at the extruder is arrival.
    (Stage.LOAD_FEED_TO_BUFFER, Fault.NO_PROGRESS):
        'no resistance within %s: lane drive slipping, PTFE disconnected '
        'between HUB and toolhead, FPS not responding (magnet, Hall or '
        'DiffA/DiffB wiring), or a path longer than the bound '
        '(recalibrate with KLIPPER_MMU_CALIBRATE)',
    (Stage.LOAD_FEED_TO_BUFFER, Fault.JAM):
        'compressed at %s, before the calibrated minimum: obstruction or '
        'kink in the Bowden, or a path shorter than calibrated (recalibrate)',
    (Stage.LOAD_FEED_TO_BUFFER, Fault.FILAMENT_LOST):
        'HUB lost at %s: runout or broken filament',
    (Stage.LOAD_FEED_TO_BUFFER, Fault.NONE): 'arrived at %s',
}
RESULTS = {EventType.LOAD_COMPLETE: 'succeeded', EventType.LOAD_CANCELLED: 'cancelled',
           EventType.LOAD_FAILED: 'failed'}
_STAGE_NAMES = {value: name for name, value in members(Stage).items()}
_FAULT_NAMES = {value: name for name, value in members(Fault).items()}


@dataclass(frozen=True)
class Attempt:
    stage: int
    fault: int
    travel: int  # Completed feeder microsteps since the feed began, or since HUB.


def decode_load_attempts(params):
    """Return (instance, session, request, attempts) of one reply."""
    names = ['instance_hi', 'instance_lo', 'session', 'request', 'count']
    for index in range(ATTEMPTS):
        names += ['stage%d' % index, 'fault%d' % index, 'travel%d' % index]
    values = [params.get(name) for name in names]
    if any(type(value) is not int or not 0 <= value < 1 << 32 for value in values):
        raise ValueError('Malformed load attempts reply')
    hi, lo, session, request, count = values[:5]
    records = tuple(Attempt(*values[5 + 3 * i:8 + 3 * i])
                    for i in range(min(count, ATTEMPTS)))
    if count > ATTEMPTS or any(a.stage not in known(Stage) or a.fault not in known(Fault)
                               for a in records):
        raise ValueError('Unsupported load attempts reply')
    return hi << 32 | lo, session, request, records


def query_load_attempts(mcu, reactor, instance, identity, is_current, timeout=0.2):
    """Read the attempts of this LOAD identity; () when firmware has none.

    Call on the reactor thread holding the observer's MCU mutex.
    """
    def on_reply(params):
        reply_instance, session, request, attempts = decode_load_attempts(params)
        if (reply_instance, session, request) != (instance, identity.session,
                                                  identity.request):
            return None
        return attempts

    args = [instance >> 32, instance & 0xffffffff, identity.client, identity.session,
            identity.request, identity.operation, identity.lane]
    return exchange(mcu, reactor, [(LOAD_ATTEMPTS_QUERY, args)],
                    {LOAD_ATTEMPTS: on_reply}, is_current, timeout,
                    'load attempts query')


def gate_learned_from_load(event_type, attempts):
    """Whether this ACKed LOAD terminal proves a no-BAY-switch gate had
    filament (True), proves it did not (False), or proves nothing either
    way (None -- gate status is left unchanged, never fabricated).

    Reaching the HUB (any attempt at Stage.LOAD_FEED_TO_BUFFER) is downstream
    evidence filament was in the gate, the same promotion Happy Hare grants a
    gate with no local sensor once filament is confirmed further along the
    path (mmu_filament_movement.py's FILAMENT_POS_START_BOWDEN check, right
    after _home_to_gate()). A LOAD that failed without any attempt ever
    reaching the HUB is the same "couldn't pick up filament at gate" case
    that marks a gate empty there (_home_to_gate's exhausted-attempts
    branch). A cancelled LOAD, or one with no attempt records, proves
    nothing.
    """
    if any(a.stage == Stage.LOAD_FEED_TO_BUFFER for a in attempts):
        return True
    if event_type == EventType.LOAD_COMPLETE:
        return True
    if event_type == EventType.LOAD_FAILED and attempts:
        return False
    return None


def describe(event_type, lane, attempts, steps_per_m=0, bowden_steps=None):
    """Return (summary, one line per attempt), e.g. 'attempt 1, HUB to
    extruder: compressed at 312 mm past HUB (19% of the 1612 mm Bowden
    estimate), before the calibrated minimum: ...; retract behind the HUB and
    retry'. Distances need the firmware's KLIPPER_MMU_FEEDER_STEPS_PER_M;
    without it they are microsteps. bowden_steps is a stored HUB-to-arrival
    step estimate."""
    def distance(steps):
        return ('%.0f mm' % (steps * 1000. / steps_per_m) if steps_per_m
                else '%d microsteps' % steps)

    lines = []
    for number, a in enumerate(attempts, 1):
        if a.stage == Stage.LOAD_RETRY_RETRACT:
            lines.append('retract before attempt %d stopped by %s'
                         % (number, _FAULT_NAMES[a.fault]))
            continue
        after_hub = a.stage == Stage.LOAD_FEED_TO_BUFFER
        where = distance(a.travel) + (' past HUB' if after_hub else '')
        if after_hub and bowden_steps:
            where += ' (%.0f%% of the %s Bowden estimate)' % (
                100. * a.travel / bowden_steps, distance(bowden_steps))
        cause = CAUSES.get((a.stage, a.fault), 'stopped by %s at %%s' % (
            _FAULT_NAMES[a.fault]))
        line = 'attempt %d, %s: %s' % (number, SEGMENTS.get(
            a.stage, _STAGE_NAMES[a.stage]), cause % where)
        if a.fault in RETRIED and number < ATTEMPTS:
            # A failed LOAD ends early on a retryable cause only by its budget.
            line += ('; the %d s LOAD time budget ran out' % BUDGET_S
                     if number == len(attempts) and event_type == EventType.LOAD_FAILED
                     else '; %s and retry%s' % (
                         'retract behind the HUB' if after_hub else 'back off',
                         ' at half speed' if number + 1 == ATTEMPTS else ''))
        lines.append(line)
    tries = sum(a.stage != Stage.LOAD_RETRY_RETRACT for a in attempts)
    summary = 'lane %d LOAD %s' % (lane, RESULTS.get(event_type, 'ended'))
    if tries:
        summary += ' after %d attempt%s' % (tries, '' if tries == 1 else 's')
    return summary, lines
