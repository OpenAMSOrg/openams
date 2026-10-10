# SPDX-License-Identifier: GPL-3.0-or-later
"""Plain-language diagnostics and guided commissioning (docs/diagnostics.md).

Every rule is a small function of already-captured, typed data (a `Snapshot`
status and a `SensorSnapshot`, as `capture_physical_state` returns): no
registries, no plugin framework, no class hierarchy. A `Finding` says what was
seen, what it most likely means physically, and what to check or change.
KLIPPER_MMU_DIAGNOSE and the post-terminal hook in host/__init__.py apply
`diagnose()`; KLIPPER_MMU_COMMISSION drives the physical steps below through
the existing PRELOAD/EJECT command paths and applies the `commission_*`
verdict functions. Phase 2 adds the firmware switch bounce/idle-edge and TMC
GSTAT/DRV_STATUS counters (mcu/switch_gpio.c, src/operation_core.c,
mcu/tmc_uart_gpio.c, one new read-only km_diag_part query) as more small
functions of the same shape; it needed no different shape.
"""
import statistics
from collections import namedtuple

from .wire import (EventType, Fault, NO_LANE, NO_PIN, OpState, PinRole,
                   ResourceDiag, Result, StartupStep, members)

# docs/fps-arrival.md#defaults-and-per-printer-bounds
FPS_ARRIVAL_PERMILLE = 700
# A path length recalibration is suggested once arrival drifts this far from
# the stored HUB-to-FPS step estimate.
BOWDEN_DRIFT_FRACTION = 0.10
# A lane is reported as slipping once the median of its last few arrival
# travels exceeds the other lanes' (or the calibration) by this fraction.
# Provisional: FPS arrival repeatability on fitted hardware is not measured.
LANE_SLIP_FRACTION = 0.03
LANE_SLIP_SAMPLES = 3
LANE_SLIP_HISTORY = 5  # arrivals kept per lane by host/__init__.py
# KLIPPER_MMU_COMMISSION STEP=fps.
FPS_COMMISSION_TARGET = 900
_FPS_NEAR_ZERO = 50
_FPS_MOVED_MARGIN = 50

# Each step's user instruction lives here once, printed by host/__init__.py.
INSTRUCTIONS = {
    'switches': 'remove all filament so the MMU is empty; every switch should read open',
    'switches_wiggle': 'now gently wiggle each switch cable and connector for a '
                       'few seconds each',
    'lane': 'insert filament into lane %d until the BAY switch clicks',
    'fps': 'push the FPS buffer fully, then release it',
}
# KLIPPER_MMU_COMMISSION STEP=switches: how long to sample bounce/idle-edge
# counters while the user wiggles cables.
SWITCH_WIGGLE_S = 10.0

Finding = namedtuple('Finding', 'severity subject meaning action')


def format_finding(mmu_name, finding):
    return ('klipper_mmu %s %s: %s: %s -- %s'
           % (mmu_name, finding.severity, finding.subject, finding.meaning,
              finding.action))


_STATE_NAMES = {value: name.lower() for name, value in members(OpState).items()}


def summarize(status, sensors):
    """One line of plain state, for KLIPPER_MMU_DIAGNOSE when nothing is wrong."""
    values = status.values
    occupant = values.get('occupant_lane') if values.get('path_occupied') else None
    hub = ('made' if _present(sensors.hub) else
          'open' if _absent(sensors.hub) else 'unknown')
    return ('state=%s path_occupied=%s%s hub=%s'
           % (_STATE_NAMES.get(values.get('state'), 'unknown'),
              values.get('path_occupied'),
              '' if occupant is None else ' lane=%d' % occupant, hub))


def _known(sample):
    return sample.configured and sample.valid and sample.fresh


def _present(sample):
    return _known(sample) and sample.present


def _absent(sample):
    return _known(sample) and not sample.present


def _at_rest(values):
    return values.get('lane_active') is False and values.get('terminal_pending') is False


def _iter_switches(sensors):
    for i in range(sensors.lane_count):
        if sensors.bays[i].configured:
            yield 'lane %d BAY switch' % i, sensors.bays[i]
    for i in range(sensors.lane_count):
        if sensors.loads[i].configured:
            yield 'lane %d LOAD switch' % i, sensors.loads[i]
    if sensors.hub.configured:
        yield 'HUB switch', sensors.hub
    if sensors.toolhead.configured:
        yield 'toolhead switch', sensors.toolhead


# --- KLIPPER_MMU_DIAGNOSE rules -------------------------------------------

def _rule_switch_validity(sensors):
    """1. A configured switch that is not valid/fresh: wiring, noise, disconnected."""
    return [Finding('error', name, 'the reading is not valid or not fresh',
                    'check the wiring and connector; a floating input needs a '
                    "pull-up ('^') in the pin config")
           for name, sample in _iter_switches(sensors) if not _known(sample)]


def _rule_bay_load_mismatch(status, sensors):
    """2. At rest, LOAD made but BAY open on the same lane."""
    if not _at_rest(status.values):
        return []
    findings = []
    for lane in range(sensors.lane_count):
        if _present(sensors.loads[lane]) and _absent(sensors.bays[lane]):
            findings.append(Finding(
                'warning', 'lane %d BAY switch' % lane,
                'the LOAD switch is made but BAY reads open on the same lane',
                "check the BAY switch lever, wiring and polarity ('!' on the pin) "
                'on lane %d, or filament was fed in without a spool at the bay'
                % lane))
    return findings


def _rule_hub_without_owner(status, sensors):
    """3. At rest, HUB made while firmware says the shared path is empty."""
    if not _at_rest(status.values):
        return []
    if _present(sensors.hub) and status.values.get('path_occupied') is False:
        return [Finding(
            'error', 'HUB switch',
            'HUB reads made but firmware reports the shared path empty',
            'check the hub for a trapped tip or stuck string, a stuck switch '
            "lever, or inverted polarity ('!' on the pin); do not eject every "
            'lane -- check the hub itself first')]
    return []


def _rule_loaded_lane_without_switches(status, sensors):
    """4. At rest, firmware says lane N is loaded but its BAY/LOAD reads empty."""
    values = status.values
    if not _at_rest(values) or values.get('path_occupied') is not True:
        return []
    lane = values.get('occupant_lane')
    if type(lane) is not int or not 0 <= lane < sensors.lane_count:
        return []
    findings = []
    if _absent(sensors.bays[lane]):
        findings.append(Finding(
            'error', 'lane %d BAY switch' % lane,
            'firmware reports lane %d loaded but its BAY switch reads empty' % lane,
            'check for runout, broken filament, or a BAY switch fault on lane %d'
            % lane))
    if _absent(sensors.loads[lane]):
        findings.append(Finding(
            'error', 'lane %d LOAD switch' % lane,
            'firmware reports lane %d loaded but its LOAD switch reads empty' % lane,
            'check for runout, broken filament, or a LOAD switch fault on lane %d'
            % lane))
    return findings



def _rule_occupied_unknown_lane(status):
    """5. At rest, firmware says path is occupied but no lane is known to be in it."""
    values = status.values
    if not _at_rest(values) or values.get('path_occupied') is not True:
        return []
    if values.get('occupant_lane') == 255:
        return [Finding(
            'error', 'shared path',
            'the shared path reads occupied (HUB made) but no lane is known to be in it',
            'a filament fragment at the HUB, or a stuck or miswired HUB switch; '
            'check the HUB switch and path, then clear with KLIPPER_MMU_RECONCILE_PATH')]
    return []

def _rule_ambiguous_path(status, sensors):
    """5. More than one lane's LOAD switch made while HUB is made."""
    if not _at_rest(status.values) or not _present(sensors.hub):
        return []
    loaded = [i for i in range(sensors.lane_count) if _present(sensors.loads[i])]
    if len(loaded) > 1:
        return [Finding(
            'error', 'shared path',
            "more than one lane's LOAD switch is made while HUB is made (lanes %s)"
            % ', '.join(str(i) for i in loaded),
            'which lane is actually in the hub cannot be known from switches '
            'alone; clear filament from every lane but the intended one, or '
            'reconcile explicitly with KLIPPER_MMU_RECONCILE_PATH')]
    return []


def _rule_fps(status):
    """6. FPS: at/above arrival threshold with nothing loaded, or invalid/stale.

    The FPS reads compression only; 0 is the normal rest (slack or tension),
    never a fault on its own.
    """
    values = status.values
    if values.get('buffer_valid') is False:
        return [Finding(
            'warning', 'FPS buffer signal', 'the buffer reading is invalid or stale',
            'check the DiffA/DiffB wiring and the FPS cable')]
    position = values.get('buffer_position')
    if (values.get('buffer_valid') is True and type(position) is int
            and position >= FPS_ARRIVAL_PERMILLE
            and values.get('path_occupied') is False
            and values.get('lane_active') is False):
        return [Finding(
            'warning', 'FPS buffer',
            'buffer position is %d, at or above the %d arrival threshold, with '
            'nothing loaded' % (position, FPS_ARRIVAL_PERMILLE),
            'the FPS magnet may be displaced, or DiffA/DiffB calibration is '
            'inverted or wrong; recalibrate DiffA/DiffB')]
    return []


# 7. Plain meaning and action for every retained fault code.
_FAULT_TEXT = {
    Fault.HOST_COMM_LOST: (
        'Klipper stopped heartbeating the MCU',
        'check for an overloaded host (LED effects, high CPU) or a flaky '
        'USB/CAN link, then retry'),
    Fault.DRIVER_OVERCURRENT: (
        'a lane TMC driver reported overcurrent',
        'check for a jammed or grinding lane motor, or a short in its wiring; '
        'power cycle motor power and run KLIPPER_MMU_RESTART_DRIVER'),
    Fault.DRIVER_FAULT: (
        'the driver stopped responding or reported a fault',
        'check that motor power (24 V) is on and the motor/UART cable is '
        'seated, or ground the motor screws after a static reset; then run '
        'KLIPPER_MMU_RESTART_DRIVER'),
    Fault.NO_PROGRESS: (
        'the lane made no progress',
        'see the LOAD attempt report for the segment and likely cause'),
    Fault.JAM: (
        'the lane jammed',
        'see the LOAD attempt report for the segment and likely cause'),
    Fault.BUFFER_SIGNAL: (
        'the FPS buffer signal failed',
        'check the DiffA/DiffB wiring and the FPS cable'),
    Fault.FILAMENT_LOST: (
        'HUB was lost while feeding',
        'see the LOAD attempt report; likely runout or broken filament'),
    Fault.STARVED: (
        'the operation could not get the motion it needed',
        'check for a mechanical obstruction on the lane'),
    Fault.STALLED: (
        'a stall was detected',
        'check for a mechanical obstruction on the lane'),
    Fault.EMERGENCY_STOP: (
        'an emergency stop was triggered',
        "check the printer's emergency-stop chain and clear it before continuing"),
    Fault.SENSOR_FAILURE: (
        'a sensor reading failed',
        'check the switch named above, if one was identified, for wiring, '
        'noise or disconnection'),
    Fault.SELECTOR: (
        'the selector could not reach a gate (homing found no switch within its '
        'travel, a switch reading went stale, or the servo did not take the pulse)',
        'check that the selector moves freely and its endstop or gate index '
        'switches are wired, clear the fault, and the next selection homes again'),
    Fault.CONTROL_TIMING: (
        'the MMU controller missed its timing (the step queue ran dry, the '
        'control task stalled, or the MCU clock failed)',
        'this is MCU load or a firmware problem, not wiring: note what the '
        'printer was doing and report it with the KLIPPER_MMU_DIAGNOSE output'),
}


def explain_fault(code, lane=None):
    """Plain meaning and action for one firmware fault code, or None."""
    text = _FAULT_TEXT.get(code)
    if text is None:
        return None
    subject = ('lane %d fault' % lane if type(lane) is int and lane != 255
              else 'retained fault')
    return Finding('error', subject, *text)


_STEP_TEXT = {
    StartupStep.CLOCK: 'waiting for the MCU clock',
    StartupStep.IDENTITY: "establishing this boot's identity",
    StartupStep.CALIBRATION: 'loading the DiffA/DiffB calibration',
    StartupStep.DEADLINE: 'starting, after the startup deadline had passed',
    StartupStep.ALREADY_STARTED: 'starting a board already started since its reset',
    StartupStep.PROFILE: 'checking the device profile built into the firmware',
    StartupStep.PINS: 'checking the pin map built into the firmware',
    StartupStep.RESOURCES: 'checking that each pin can do its job (timers, ADC)',
    StartupStep.SAVED_CONFIG: 'checking the saved driver, switch and respooler settings',
    StartupStep.RESERVE: 'reserving the MMU pins',
    StartupStep.SWITCHES: 'setting up the switch inputs',
    StartupStep.ADC: 'starting the DiffA/DiffB ADC',
    StartupStep.DRIVER_SETUP: 'setting up the stepper driver',
    StartupStep.REWIND_SETUP: 'setting up the respooler output',
    StartupStep.DRIVER_CONFIRM: 'waiting for the TMC2209 driver to answer',
    StartupStep.BIND: 'attaching the drivers',
    StartupStep.ATTACH: 'starting the control loop',
    StartupStep.SELECTOR_SETUP: 'setting up the selector and its servo',
}
_RESULT_TEXT = {
    Result.ERR_INVALID_PARAM: 'a setting is invalid',
    Result.ERR_BUSY: 'something else already owns it',
    Result.ERR_FAULTED: 'it faulted',
    Result.ERR_CONFLICT: 'two things claim the same resource',
    Result.ERR_TIMEOUT: 'no answer in time',
    Result.ERR_NOT_SUPPORTED: 'this firmware cannot do that',
}
_ROLE_TEXT = {
    PinRole.STEP: 'STEP', PinRole.DIR: 'DIR', PinRole.ENABLE: 'ENABLE',
    PinRole.UART: 'TMC UART', PinRole.BAY: 'BAY switch', PinRole.HUB: 'HUB switch',
    PinRole.TOOLHEAD: 'toolhead switch', PinRole.BUFFER_A: 'DiffA',
    PinRole.BUFFER_B: 'DiffB', PinRole.REWIND_FORWARD: 'respooler forward',
    PinRole.REWIND_REVERSE: 'respooler reverse',
    PinRole.REWIND_ENABLE: 'respooler enable', PinRole.ENCODER_A: 'encoder A',
    PinRole.ENCODER_B: 'encoder B', PinRole.CURRENT: 'current sense',
    PinRole.LOAD: 'LOAD switch',
    PinRole.SELECTOR_STEP: 'selector STEP', PinRole.SELECTOR_DIR: 'selector DIR',
    PinRole.SELECTOR_ENABLE: 'selector ENABLE', PinRole.SELECTOR_UART: 'selector TMC UART',
    PinRole.SELECTOR_ENDSTOP: 'selector endstop', PinRole.SERVO: 'servo',
}
_SHARED_ROLES = {PinRole.HUB, PinRole.TOOLHEAD, PinRole.BUFFER_A,
                 PinRole.BUFFER_B, PinRole.ENCODER_A, PinRole.ENCODER_B}


def explain_startup_failure(result, detail, pin_names):
    """Plain meaning and action for a faulted diagnostic startup.

    detail is the decoded km_diagnostic_detail, or None when the firmware
    predates it; pin_names maps MCU pin numbers to names such as 'PA1'."""
    detail = detail or {'step': StartupStep.NONE, 'lane': NO_LANE,
                        'cause': result, 'reason': ResourceDiag.NONE,
                        'pin0': NO_PIN, 'pin1': NO_PIN}
    step, lane = detail['step'], detail['lane']
    where = _STEP_TEXT.get(step, 'starting')
    if lane != NO_LANE:
        where += ' on lane %d' % lane
    pins = []
    for k in '01':
        pin, role = detail['pin' + k], detail.get('role' + k)
        if pin != NO_PIN:
            label = _ROLE_TEXT.get(role, 'pin')
            if role not in _SHARED_ROLES:
                label += ' lane %d' % detail['lane' + k]
            pins.append('%s (%s)' % (label, pin_names.get(pin, pin)))
    cause = _RESULT_TEXT.get(detail['cause'], 'result %s' % detail['cause'])
    if (step in (StartupStep.DRIVER_SETUP, StartupStep.DRIVER_CONFIRM)
            and detail['cause'] == Result.ERR_FAULTED):
        cause = 'it did not answer, or answered with a fault'
    meaning = 'startup stopped while %s: %s' % (where, cause)
    if pins:
        meaning += '; involves ' + ' and '.join(pins)
    roles = {detail.get('role0'), detail.get('role1')}
    if step in (StartupStep.DRIVER_SETUP, StartupStep.DRIVER_CONFIRM):
        action = ("check that this lane's TMC2209 is fitted and has motor "
                  'power, and that its UART pin is right (expected on a bare '
                  'board with no drivers)')
    elif roles & {PinRole.REWIND_FORWARD, PinRole.REWIND_REVERSE}:
        action = ('respooler forward and reverse must be two channels of one '
                  'timer (TIM2, TIM3, TIM4, TIM5 or TIM12), a different timer '
                  'for each lane')
    elif roles & {PinRole.BUFFER_A, PinRole.BUFFER_B}:
        action = 'DiffA and DiffB must be two different channels of one ADC'
    elif step == StartupStep.PINS:
        action = ('each pin list needs one pin per lane, and no pin may be '
                  'used twice')
    elif step == StartupStep.CALIBRATION:
        action = 'set diff_calibration_file in [klipper_mmu]'
    else:
        action = 'fix the cause shown'
    return Finding('error', 'startup', meaning,
                   action + ', rebuild if a setting changed, then FIRMWARE_RESTART')


def _rule_retained_fault(status, retained_fault):
    """Only while firmware is still faulted: a fault RECONCILE_PATH already
    cleared is history, not a finding. The retained record names the lane."""
    code = status.values.get('system_fault')
    if status.values.get('state') != OpState.FAULT:
        return []
    lane = (retained_fault.get('lane')
            if retained_fault and retained_fault.get('value') == code else None)
    finding = explain_fault(code, lane)
    return [finding] if finding else []


def _rule_bowden_drift(last_load, bowden_steps):
    """8. Last LOAD arrival travel vs the stored Bowden estimate, +/- 10%."""
    if not last_load or not bowden_steps:
        return []
    arrival = last_load.get('arrival_steps')
    if not arrival:
        return []
    if abs(arrival - bowden_steps) / float(bowden_steps) > BOWDEN_DRIFT_FRACTION:
        return [Finding(
            'warning', 'lane %s Bowden length' % last_load.get('lane'),
            'the last LOAD arrival travel was %d%% of the calibrated '
            '%d-microstep estimate'
            % (round(100. * arrival / bowden_steps), bowden_steps),
            'the path length probably changed (PTFE moved, cut, or a coupler '
            'slipped); recalibrate with KLIPPER_MMU_CALIBRATE')]
    return []


def _rule_lane_slip(arrival_history, bowden_steps):
    """13. A lane that needs more feed than the others to reach the extruder.

    The HUB-to-extruder path is shared, so a longer arrival on every lane is a
    path change (rule 8), while one lane alone is slipping at its drive. Slip
    only adds steps, so the basis is never below the calibration.
    """
    if not arrival_history or not bowden_steps:
        return []
    ratios = {lane: statistics.median(history[-LANE_SLIP_SAMPLES:]) / float(bowden_steps)
              for lane, history in arrival_history.items()
              if len(history) >= LANE_SLIP_SAMPLES}
    findings = []
    for lane in sorted(ratios):
        others = [ratio for other, ratio in ratios.items() if other != lane]
        if others and statistics.median(others) > 1.0:
            reference, basis = statistics.median(others), 'the other lanes'
        else:
            reference, basis = 1.0, 'the calibration'
        excess = ratios[lane] - reference
        if excess >= LANE_SLIP_FRACTION:
            findings.append(Finding(
                'warning', 'lane %d drive' % lane,
                'its last %d loads needed %d%% more feed to reach the extruder than %s'
                % (LANE_SLIP_SAMPLES, round(100 * excess), basis),
                'the lane drive gear is probably slipping: check the idler tension and '
                'clean the gear teeth; a heavy or binding spool does the same; lowering '
                'the feed speed confirms it'))
    return findings


_RESULT_NAMES = {value: name for name, value in members(Result).items()}


def _rule_startup_fault(startup_state, startup_result):
    """9. A board-startup driver fault needs a firmware restart."""
    if startup_state != 'faulted':
        return []
    name = _RESULT_NAMES.get(startup_result, str(startup_result))
    return [Finding(
        'error', 'board startup',
        'diagnostic startup did not complete (%s); like a Klipper TMC startup '
        'error, it needs a firmware restart' % name,
        'most often motor power was off when the MCU started (MCU on USB '
        'power alone), or a UART/motor cable is loose; power the motors on, '
        'then run FIRMWARE_RESTART')]


# --- Phase 2: firmware switch bounce/idle-edge and TMC gstat/drv_status ----
# (mcu/switch_gpio.c, src/operation_core.c, mcu/tmc_uart_gpio.c). diag is one
# DiagSnapshot (host/diag_reader.py); baseline is the previous one kept by the
# caller (KlipperMMU._diag_baseline, like _diagnostic_seen). Bounce/idle-edge
# rules are deltas and need a baseline; the first-ever capture only seeds it,
# with no finding. TMC flags are this capture's absolute register bits.
BOUNCE_DELTA_THRESHOLD = 3
IDLE_EDGE_LOAD_HUB_THRESHOLD = 2
IDLE_EDGE_BAY_THRESHOLD = 4


def _delta16(current, previous):
    """None on a saturating counter that went backwards: a rebind/restart,
    not evidence; the caller reseeds the baseline either way."""
    return current - previous if current >= previous else None


def _rule_bounce(diag, baseline, lane_count):
    """Phase 2 #1: rejected debounce candidates since the last capture."""
    if diag is None or baseline is None:
        return []
    findings = []
    def check(name, current, previous):
        delta = _delta16(current, previous)
        if delta is not None and delta >= BOUNCE_DELTA_THRESHOLD:
            findings.append(Finding(
                'warning', name,
                '%d rejected debounce candidates since the last capture' % delta,
                "a noisy or floating input; check the connector/crimp, or add "
                "a pull-up ('^') in the pin config"))
    for i in range(lane_count):
        check('lane %d BAY switch' % i, diag.bay_bounce[i], baseline.bay_bounce[i])
        check('lane %d LOAD switch' % i, diag.load_bounce[i], baseline.load_bounce[i])
    check('HUB switch', diag.hub_bounce, baseline.hub_bounce)
    check('toolhead switch', diag.toolhead_bounce, baseline.toolhead_bounce)
    return findings


def _rule_idle_edges(diag, baseline, lane_count):
    """Phase 2 #2: accepted switch edges while nothing was moving it."""
    if diag is None or baseline is None:
        return []
    findings = []
    def check(name, current, previous, threshold):
        delta = _delta16(current, previous)
        if delta is not None and delta >= threshold:
            findings.append(Finding(
                'warning', name,
                '%d edges since the last capture while nothing was moving it' % delta,
                'a loose connector, a worn lever, or filament being pushed by hand'))
    for i in range(lane_count):
        check('lane %d BAY switch' % i, diag.bay_idle_edges[i],
              baseline.bay_idle_edges[i], IDLE_EDGE_BAY_THRESHOLD)
        check('lane %d LOAD switch' % i, diag.load_idle_edges[i],
              baseline.load_idle_edges[i], IDLE_EDGE_LOAD_HUB_THRESHOLD)
    check('HUB switch', diag.hub_idle_edges, baseline.hub_idle_edges,
          IDLE_EDGE_LOAD_HUB_THRESHOLD)
    return findings


# GSTAT (mcu/tmc_uart_gpio.c t->gstat): bit positions as read by src/tmc2209.c.
_GSTAT_RESET = 0x1
_GSTAT_DRV_ERR = 0x2
_GSTAT_UV_CP = 0x4
# DRV_STATUS: TMC2209 datasheet bit positions, as read by src/tmc2209.c.
_DRV_OTPW = 0x1
_DRV_OT = 0x2
_DRV_S2GA = 0x4
_DRV_S2GB = 0x8
_DRV_S2VSA = 0x10
_DRV_S2VSB = 0x20
_DRV_OLA = 0x40
_DRV_OLB = 0x80


def _rule_tmc(diag, lane_count):
    """Phase 2 #3: this capture's absolute TMC2209 GSTAT/DRV_STATUS bits."""
    if diag is None:
        return []
    findings = []
    for lane in range(lane_count):
        gstat, drv = diag.gstat[lane], diag.drv_status[lane]
        subject = 'lane %d driver' % lane
        if gstat & _GSTAT_RESET:
            findings.append(Finding(
                'warning', subject, 'the driver reset (GSTAT reset)',
                'usually static discharge from PTFE in dry air, or a supply '
                'dip; ground the motor screws/frame'))
        if gstat & _GSTAT_UV_CP:
            findings.append(Finding(
                'error', subject,
                'the charge pump reported undervoltage (GSTAT uv_cp)',
                'motor supply (24 V) is off or sagging'))
        if gstat & _GSTAT_DRV_ERR:
            if drv & (_DRV_S2GA | _DRV_S2GB):
                findings.append(Finding(
                    'error', subject,
                    'a motor phase is shorted to ground (DRV_STATUS s2ga/s2gb)',
                    'check the motor cable on lane %d' % lane))
            elif drv & (_DRV_S2VSA | _DRV_S2VSB):
                findings.append(Finding(
                    'error', subject,
                    'a motor phase is shorted to the supply (DRV_STATUS s2vsa/s2vsb)',
                    'check the motor cable on lane %d' % lane))
            else:
                findings.append(Finding(
                    'error', subject, 'the driver reported an error (GSTAT drv_err)',
                    'check the motor cable and connector on lane %d' % lane))
        if drv & _DRV_OT:
            findings.append(Finding(
                'error', subject,
                'the driver overheated and shut down (DRV_STATUS ot)',
                'improve cooling or lower the run current on lane %d' % lane))
        elif drv & _DRV_OTPW:
            findings.append(Finding(
                'warning', subject, 'the driver is hot (DRV_STATUS otpw)',
                'improve cooling or lower the run current on lane %d' % lane))
        if drv & (_DRV_OLA | _DRV_OLB):
            findings.append(Finding(
                'warning', subject,
                'possible open load (DRV_STATUS ola/olb); the TMC reports this '
                'unreliably at standstill or high speed',
                'check the motor connector on lane %d if it also misbehaves' % lane))
    return findings


def diagnose(status, sensors, retained_fault=None, last_load=None, bowden_steps=None,
            startup_state=None, startup_result=None, diag=None, diag_baseline=None,
            arrival_history=None):
    """Apply every rule to one coherent capture; () when nothing is wrong."""
    findings = []
    findings += _rule_switch_validity(sensors)
    findings += _rule_bay_load_mismatch(status, sensors)
    findings += _rule_hub_without_owner(status, sensors)
    findings += _rule_loaded_lane_without_switches(status, sensors)
    findings += _rule_occupied_unknown_lane(status)
    findings += _rule_ambiguous_path(status, sensors)
    findings += _rule_fps(status)
    findings += _rule_retained_fault(status, retained_fault)
    slip = _rule_lane_slip(arrival_history, bowden_steps)
    # A slipping lane explains its own long arrival; recalibrating would hide it.
    if not any(f.subject == 'lane %s drive' % (last_load or {}).get('lane') for f in slip):
        findings += _rule_bowden_drift(last_load, bowden_steps)
    findings += slip
    findings += _rule_startup_fault(startup_state, startup_result)
    findings += _rule_bounce(diag, diag_baseline, sensors.lane_count)
    findings += _rule_idle_edges(diag, diag_baseline, sensors.lane_count)
    findings += _rule_tmc(diag, sensors.lane_count)
    return tuple(findings)


# --- KLIPPER_MMU_COMMISSION verdicts ---------------------------------------

def _bounce_by_name(diag, lane_count):
    if diag is None:
        return {}
    by_name = {'lane %d BAY switch' % i: diag.bay_bounce[i] for i in range(lane_count)}
    by_name.update(('lane %d LOAD switch' % i, diag.load_bounce[i]) for i in range(lane_count))
    by_name['HUB switch'] = diag.hub_bounce
    by_name['toolhead switch'] = diag.toolhead_bounce
    return by_name


def commission_switches(sensors, diag=None):
    """STEP=switches: one (name, state, note) row per configured switch.

    diag (a DiagSnapshot) adds each switch's bounce count since setup to its
    note, when the firmware has KLIPPER_MMU_DIAG_COUNTERS.
    """
    bounce = _bounce_by_name(diag, sensors.lane_count)
    rows = []
    for name, sample in _iter_switches(sensors):
        suffix = '' if name not in bounce else ' (%d bounces since setup)' % bounce[name]
        if not _known(sample):
            rows.append((name, 'unknown', 'invalid or not fresh: a floating '
                        "input (add a pull-up '^') or disconnected" + suffix))
        elif sample.present:
            rows.append((name, 'made', 'reads made with the MMU empty: likely '
                        "inverted polarity (toggle '!' on the pin), or "
                        'leftover filament' + suffix))
        else:
            rows.append((name, 'open', 'OK' + suffix))
    return tuple(rows)


def commission_switch_wiggle(before, after, lane_count):
    """STEP=switches wiggle window: a Finding for every switch whose bounce or
    idle-edge count moved while the user wiggled cables (before/after are
    DiagSnapshot); () when nothing moved."""
    findings = []
    def check(name, bounce_delta, idle_delta):
        if bounce_delta or idle_delta:
            findings.append(Finding(
                'info', name,
                '%d bounce and %d edge count(s) while wiggling cables'
                % (bounce_delta, idle_delta),
                'reseat or replace this switch\'s connector/crimp if it keeps happening'))
    for i in range(lane_count):
        check('lane %d BAY switch' % i,
              max(0, after.bay_bounce[i] - before.bay_bounce[i]),
              max(0, after.bay_idle_edges[i] - before.bay_idle_edges[i]))
        check('lane %d LOAD switch' % i,
              max(0, after.load_bounce[i] - before.load_bounce[i]),
              max(0, after.load_idle_edges[i] - before.load_idle_edges[i]))
    check('HUB switch', max(0, after.hub_bounce - before.hub_bounce),
          max(0, after.hub_idle_edges - before.hub_idle_edges))
    check('toolhead switch', max(0, after.toolhead_bounce - before.toolhead_bounce), 0)
    return tuple(findings)


def commission_lane_insert(baseline, sensors, lane):
    """STEP=lane wait: ('ok', None), ('wrong_lane', other) or ('waiting', None)."""
    if _present(sensors.bays[lane]) and not _present(baseline.bays[lane]):
        return 'ok', None
    for i in range(sensors.lane_count):
        if i == lane:
            continue
        if ((_present(sensors.bays[i]) and not _present(baseline.bays[i]))
                or (_present(sensors.loads[i]) and not _present(baseline.loads[i]))):
            return 'wrong_lane', i
    return 'waiting', None


def _local_operation_finding(lane, label, result, success_event, ok_action, fail_action):
    if hasattr(result, 'event_type'):
        if result.event_type == success_event:
            return Finding('info', 'lane %d %s' % (lane, label),
                           '%s succeeded' % label, ok_action)
        return Finding('error', 'lane %d %s' % (lane, label),
                       '%s failed (event %d)' % (label, result.event_type),
                       fail_action)
    if hasattr(result, 'admission'):
        return Finding('error', 'lane %d %s' % (lane, label),
                       '%s was rejected (admission %d)' % (label, result.admission),
                       fail_action)
    return Finding('error', 'lane %d %s' % (lane, label),
                   '%s did not complete normally' % label,
                   'run KLIPPER_MMU_QUERY and try again')


def commission_preload_result(lane, result):
    return _local_operation_finding(
        lane, 'PRELOAD', result, EventType.PRELOAD_COMPLETE,
        'feeder direction and the LOAD switch are OK',
        'check the feeder motor direction and grip (tension/gears), or the '
        'LOAD switch on lane %d' % lane)


def commission_eject_result(lane, result):
    return _local_operation_finding(
        lane, 'EJECT', result, EventType.EJECT_COMPLETE,
        'the feeder backed the filament out of the lane past the BAY switch',
        'check the feeder motor direction, grip, or a jam on lane %d' % lane)


def commission_respooler_answer(lane, wound):
    """STEP=lane ANSWER=yes|no: did the spool wind the filament back?"""
    if wound:
        return Finding('info', 'lane %d respooler' % lane,
                       'the spool wound the filament back during EJECT',
                       'the N20 respooler is OK')
    return Finding('warning', 'lane %d respooler' % lane,
                   'the spool did not wind the filament back during EJECT',
                   'check the N20 respooler wiring polarity (it may be wired '
                   'reversed) or a slipping gear/one-way bearing')


def commission_fps(samples):
    """STEP=fps: samples is ((buffer_position, buffer_valid), ...) over the window."""
    valid = [position for position, ok in samples if ok and type(position) is int]
    if not valid:
        return (Finding('error', 'FPS buffer', 'no valid buffer reading during the test',
                        'check the DiffA/DiffB wiring and the FPS cable'),)
    start, peak, end = valid[0], max(valid), valid[-1]
    findings = []
    if start > _FPS_NEAR_ZERO:
        findings.append(Finding(
            'warning', 'FPS buffer at rest',
            'buffer reads %d at rest before any push; 0 is the normal rest '
            '(slack or tension)' % start,
            'DiffA/DiffB calibration is likely inverted or wrong; recalibrate'))
    if peak - start < _FPS_MOVED_MARGIN:
        findings.append(Finding(
            'error', 'FPS buffer',
            'buffer position never moved while pushed (peak %d)' % peak,
            'check the FPS cable, DiffA/DiffB wiring, or a displaced magnet'))
    elif peak < FPS_COMMISSION_TARGET:
        findings.append(Finding(
            'warning', 'FPS buffer',
            'buffer only reached %d while pushed (target %d)'
            % (peak, FPS_COMMISSION_TARGET),
            'check the DiffA/DiffB calibration, or that the buffer has its '
            'full mechanical travel'))
    if peak - start >= _FPS_MOVED_MARGIN and end - start > _FPS_NEAR_ZERO:
        findings.append(Finding(
            'warning', 'FPS buffer release',
            'buffer did not return near its start after release (%d, started '
            'at %d)' % (end, start),
            'check for a sticking buffer slide'))
    return tuple(findings)
