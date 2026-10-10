# SPDX-License-Identifier: GPL-3.0-or-later
"""One latched km_fault as the OpenAMS UIs' fault entry (C5 §7.6, C2 §4.8).

The UIs render one fault per unit and offer only the existing action tokens
`clear_errors`, `clear_fault`, `resume` and `retry_load`, so a klipper-mmu
fault is published here with the C2 severity C5 §7.6 assigns, a stable code,
a short user-facing text and the tokens that really clear or resume the unit.
"""
from .wire import Fault

# km_fault -> (C2 code, severity, text, action tokens).
#
# C5 §7.6's severities, with two corrections the wire itself forces:
#
#  * `clear_errors` is dropped everywhere, because OpenAMSCommandDevice's
#    clear_errors raises for this unit (integration/openams/README.md), so it
#    would offer a token that clears nothing.
#  * A fault whose physical state a person must settle first offers no
#    `clear_fault`, and its text says what to do instead. A lane latch
#    survives every recovery except reconciliation
#    (docs/path-reconciliation.md, "From a stopped fault"), and the two
#    driver faults keep a latch of their own: reconciliation refuses them,
#    and the lane is repaired with KLIPPER_MMU_RESTART_DRIVER once the cause
#    is removed. A selector fault leaves the shared path unproven, so the
#    operator states which lane occupies it with RECONCILE_PATH
#    ACTION=ADOPT.
_ENTRIES = {
    Fault.HOST_COMM_LOST: (
        'mmu_host_comm_lost', 'stop',
        'Host link lost; restart the firmware', ()),
    Fault.DRIVER_OVERCURRENT: (
        'mmu_driver_overcurrent', 'stop',
        'Overcurrent; remove the cause, then clear', ()),
    Fault.DRIVER_FAULT: (
        'mmu_driver_fault', 'stop',
        'Driver fault; remove the cause, then clear', ()),
    Fault.NO_PROGRESS: (
        'mmu_no_progress', 'pause', 'No progress; retry the load',
        ('retry_load',)),
    Fault.JAM: (
        'mmu_jam', 'pause', 'Jammed; clear the path, then retry',
        ('retry_load',)),
    Fault.BUFFER_SIGNAL: (
        'mmu_buffer_signal', 'pause', 'Follower signal lost; clear the fault',
        ('clear_fault',)),
    Fault.FILAMENT_LOST: (
        'mmu_filament_lost', 'pause', 'Filament lost; retry the load',
        ('retry_load',)),
    Fault.STARVED: (
        'mmu_starved', 'pause', 'Follower starved; retry the load',
        ('retry_load',)),
    Fault.STALLED: (
        'mmu_stalled', 'pause', 'Follower stalled; retry the load',
        ('retry_load',)),
    Fault.EMERGENCY_STOP: (
        'mmu_emergency_stop', 'stop', 'Emergency stop; clear to resume',
        ('clear_fault',)),
    Fault.SENSOR_FAILURE: (
        'mmu_sensor_failure', 'stop', 'Sensor failure; check it, then clear',
        ('clear_fault',)),
    Fault.CONTROL_TIMING: (
        'mmu_control_timing', 'stop', 'Control timing lost; clear to resume',
        ('clear_fault',)),
    Fault.SELECTOR: (
        'mmu_selector', 'stop', 'Check the path, then clear', ()),
}

# openams_ui cuts a longer text to this many characters (C2 §6.2); every
# published text fits, so no fault reaches a display ellipsized.
MAX_TEXT = 48

_UNKNOWN = ('mmu_unknown', 'stop', 'Unknown MMU fault; check the unit', ())


def project_fault(value, physical_lane=None, bay_count=None):
    """One C2 §4.8 fault entry for a latched km_fault.

    `physical_lane` is OpenAMS's latch key: a lane of this unit, or 255 for a
    unit-wide fault, which is never a bay. A value outside the enumeration
    still publishes, as the `stop` default a UI must never treat as
    informational (C2 §5.7)."""
    code, severity, text, actions = _ENTRIES.get(value, _UNKNOWN)
    bay = None
    if (type(physical_lane) is int and type(bay_count) is int
            and 0 <= physical_lane < bay_count):
        bay = physical_lane
    return {'severity': severity, 'code': code, 'text': text, 'bay': bay,
            'actions': list(actions), 'source': 'firmware'}
