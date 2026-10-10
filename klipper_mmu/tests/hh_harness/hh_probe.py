# SPDX-License-Identifier: GPL-3.0-or-later
"""HH-1 probe (Milestone 1): the real klipper-mmu core behind Happy Hare's
operation-level seam for PRELOAD and UNLOAD, with HH's own filament model
(test/hh/filament.py) as the plant. LOAD and EJECT stay teleport stubs (this
milestone only proves PRELOAD/UNLOAD).

This file extends an earlier read-only review probe (141 lines, 123/138
baseline): the
teleport stubs are unchanged and kept behind --teleport for comparison; the
new part is real_preload_gate/real_unload_sequence, which call into
hh_shim.so (the real core, tests/mock_driver.c and tests/stepper_reference.c,
built from tests/hh_harness/Makefile) tick by tick and apply the resulting
motion to HH's FilamentPath with FilamentPath.advance().

Usage (cwd must be the extracted Happy Hare root):
  python hh_probe.py [--teleport] <test.module|test.module.Class> [...]

Env:
  KM_HH_LIB      path to libhh_shim.so (default: ./build/libhh_shim.so next to
                 this file)
"""
import ctypes
import os
import sys
import unittest
import traceback

sys.path.insert(0, os.getcwd())
from test.hh import bootstrap

TELEPORT = '--teleport' in sys.argv
if TELEPORT:
    sys.argv.remove('--teleport')

_LIB_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'build', 'libhh_shim.so')
_LIB_PATH = os.environ.get('KM_HH_LIB', _LIB_DEFAULT)

KM_OK = 0
KM_REQ_ACCEPTED = 0
KM_REQ_NO_SPOOL = 4
KM_EVENT_NONE = 0
KM_EVENT_UNLOAD_COMPLETE = 4
KM_EVENT_PRELOAD_COMPLETE = 13
OP_PRELOAD = 0
OP_UNLOAD = 1


def steps_per_mm():
    """HH's own rendered [mmu_stepper unit0_gear] for the boxturtle profile:
    rotation_distance, gear_ratio and microsteps (installer/Kconfig.gear_stepper
    defaults; box_turtle does not override them, but this reads the live
    render rather than hard-coding the numbers a second time). Klipper's
    stepper steps/mm is (full_steps_per_rotation * microsteps * gear_ratio) /
    rotation_distance; the rendered SLB gear_ratio here is 50:10 (5:1) -
    without it steps/mm is wrong by that factor."""
    from test.hh import cfg, profiles
    text = cfg.render(profiles.get('boxturtle'))['config/base/mmu_hardware.cfg']
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines) if l.strip().startswith('[mmu_stepper unit0_gear]'))
    values = {}
    for line in lines[start + 1:]:
        if line.strip().startswith('['):
            break
        if ':' not in line:
            continue
        key, _, rest = line.partition(':')
        values[key.strip()] = rest.split('#', 1)[0].strip()
    rotation_distance = float(values['rotation_distance'])
    microsteps = float(values['microsteps'])
    full_steps = float(values.get('full_steps_per_rotation', 200))
    num, _, den = values.get('gear_ratio', '1:1').partition(':')
    gear_ratio = float(num) / float(den)
    return (full_steps * microsteps * gear_ratio) / rotation_distance


def load_lib():
    lib = ctypes.CDLL(_LIB_PATH)
    lib.km_hh_init.argtypes = [ctypes.c_double]
    lib.km_hh_init.restype = ctypes.c_int
    lib.km_hh_sensors.argtypes = [ctypes.c_int] * 7
    lib.km_hh_request.argtypes = [ctypes.c_int, ctypes.c_int]
    lib.km_hh_request.restype = ctypes.c_int
    lib.km_hh_adopt.argtypes = [ctypes.c_int]
    lib.km_hh_adopt.restype = ctypes.c_int
    lib.km_hh_run.argtypes = [ctypes.c_int, ctypes.c_int32,
                              ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_int)]
    lib.km_hh_run.restype = ctypes.c_int
    return lib


_lib = None if TELEPORT else load_lib()
_steps_per_mm = None if TELEPORT else steps_per_mm()


def install_stub(session):
    """The teleport stub (unchanged from seam_probe.py): all four operations
    move HH's model straight to the end state an operation would reach. Used
    under --teleport to reproduce the 123/138 baseline for comparison, and
    (load_sequence/_eject_from_gate only) in the milestone itself."""
    mmu = session.mmu
    from extras.mmu.mmu_constants import (
        GATE_EMPTY, GATE_AVAILABLE, GATE_AVAILABLE_FROM_BUFFER,
        FILAMENT_POS_UNLOADED, FILAMENT_POS_LOADED, FILAMENT_POS_EXTRUDER_ENTRY,
        SENSOR_EXIT_PREFIX, SENSOR_ENTRY_PREFIX)
    from extras.mmu.mmu_utils import MmuError

    def fil():
        return session.printer.harness_filament

    def datum(profile):
        name = mmu.sensor_manager.get_qualified_endstop_name(profile.endstop)
        return fil().position(name)

    def tip_to(g, target, reason):
        fil().advance(g, target - fil().tip[g], reason)

    def gripped(g):
        return fil().tip[g] >= fil().layout['mmu_entry']

    def load_sequence(bowden_move=None, skip_extruder=False, purge=None, extruder_only=False):
        g = mmu.gate_selected
        if g < 0:
            raise MmuError('km: no gate selected')
        if mmu.filament_pos >= FILAMENT_POS_EXTRUDER_ENTRY:
            raise MmuError('Cannot load extruder because filament already in extruder')
        mmu.log_info('Loading gate %d...' % g)
        if not gripped(g):
            mmu.gate_maps.set_gate_status(g, GATE_EMPTY)
            mmu.set_filament_pos_state(FILAMENT_POS_UNLOADED)
            raise MmuError("Couldn't pick up filament at gate")
        tip_to(g, fil().layout['toolhead'] + 28.0, 'km LOAD')
        mmu.gate_maps.set_gate_status(g, max(mmu.gate_status[g], GATE_AVAILABLE))
        mmu.set_filament_pos_state(FILAMENT_POS_LOADED)
        mmu._spoolman_activate_spool(mmu.gate_spool_id[g])

    def unload_sequence_teleport(bowden_move=None, check_state=False, form_tip=None, extruder_only=False):
        g = mmu.gate_selected
        if mmu.filament_pos == FILAMENT_POS_UNLOADED:
            mmu.log_debug('Filament already ejected')
            return
        mmu.log_info('Unloading gate %d...' % g)
        mmu._spoolman_activate_spool(0)
        prof = mmu._gate_profile()
        tip_to(g, datum(prof) + prof.parking_distance, 'km UNLOAD')
        if mmu.gate_status[g] != GATE_EMPTY:
            mmu.gate_maps.set_gate_status(g, GATE_AVAILABLE_FROM_BUFFER)
        mmu.set_filament_pos_state(FILAMENT_POS_UNLOADED)
        if mmu.is_handling_runout:
            _eject_from_gate()

    def _preload_gate_teleport(pending=None):
        g = mmu.gate_selected
        if mmu.sensor_manager.check_gate_sensor(SENSOR_EXIT_PREFIX, g):
            mmu.log_always('Filament already preloaded in gate %d' % g)
            mmu.gate_maps.set_gate_status(g, GATE_AVAILABLE)
            mmu.last_preloaded_gate = g
            mmu._check_pending_filament(g, pending=pending)
            return
        prof = mmu._preload_profile()
        mmu.log_always('Preloading gate %d...' % g)
        if not gripped(g):
            mmu.gate_maps.set_gate_status(g, GATE_EMPTY)
            mmu.set_filament_pos_state(FILAMENT_POS_UNLOADED)
            if mmu.sensor_manager.check_gate_sensor(SENSOR_ENTRY_PREFIX, g):
                mmu.gate_maps.set_gate_status(g, -1)
                return
            raise MmuError("Couldn't pick up filament at gate")
        with mmu.wrap_suspend_filament_monitoring(), mmu.wrap_suspend_insert_events():
            tip_to(g, datum(prof) + prof.parking_distance, 'km PRELOAD')
            mmu.set_filament_pos_state(FILAMENT_POS_UNLOADED)
        mmu.gate_maps.set_gate_status(g, GATE_AVAILABLE)
        mmu.last_preloaded_gate = g
        mmu.log_always('Filament detected and loaded in gate %d' % g)
        mmu._check_pending_filament(g, pending=pending)

    def _eject_from_gate():
        g = mmu.gate_selected
        mmu.log_always('Ejecting gate %d...' % g)
        tip_to(g, fil().layout['mmu_entry'] - 0.01, 'km EJECT')
        mmu.gate_maps.reset_gate(g)
        mmu.log_always('The filament in gate %d can be removed' % g)

    # ---- real-core PRELOAD/UNLOAD (Milestone 1) ------------------------
    def sensor_state(g):
        bay = bool(fil().triggered('mmu_entry_%d' % g))
        load = bool(fil().triggered('mmu_exit_%d' % g))
        hub = bool(fil().triggered('unit0:mmu_shared_exit'))
        return bay, load, hub

    def publish(g):
        bay, load, hub = sensor_state(g)
        _lib.km_hh_sensors(g, int(bay), int(load), int(hub), 0, 0, 0)

    # A first pure per-tick loop (BATCH_TICKS=1) measured under 3s for the
    # whole TestPreload/TestQuietPlacement files, but over 6s for one UNLOAD
    # once steps/mm was corrected to include HH's rendered gear_ratio (the
    # BoxTurtle bowden is long and the Baseline profile's default velocities
    # are slow: see hh_shim.c km_hh_init). Batching ticks between Python
    # round trips (review section 2, "add km_hh_run(ticks, max_delta_steps)")
    # cuts ctypes/Python overhead by BATCH_TICKS; sensors are still published
    # every batch, well inside max_sensor_age_ms (200 ms) and the resulting
    # extra reaction lag is a few hundredths of a mm at these feed speeds -
    # noise next to the mm-scale rest-position differences this milestone
    # already documents, not a new correctness path.
    BATCH_TICKS = 50

    def run_until_event(g, max_ticks=4000000):
        """Drive km_hh_run in batches of BATCH_TICKS control ticks: apply the
        completed steps to HH's model, publish its sensors back, and stop on
        the first terminal/fault event. See hh_shim.c km_hh_run."""
        delta = ctypes.c_int32(0)
        event = ctypes.c_int(0)
        ticks = 0
        while ticks < max_ticks:
            n = _lib.km_hh_run(BATCH_TICKS, 1 << 30, ctypes.byref(delta), ctypes.byref(event))
            ticks += n
            if delta.value:
                fil().advance(g, delta.value / _steps_per_mm, 'km run')
            publish(g)
            if event.value != KM_EVENT_NONE:
                return event.value
            if n == 0:
                raise AssertionError('km_hh_run made no progress for gate %d' % g)
        raise AssertionError('km_hh_run: gate %d produced no terminal event in %d ticks' % (g, max_ticks))

    def real_preload_gate(pending=None):
        g = mmu.gate_selected
        if mmu.sensor_manager.check_gate_sensor(SENSOR_EXIT_PREFIX, g):
            mmu.log_always('Filament already preloaded in gate %d' % g)
            mmu.gate_maps.set_gate_status(g, GATE_AVAILABLE)
            mmu.last_preloaded_gate = g
            mmu._check_pending_filament(g, pending=pending)
            return
        assert _lib.km_hh_init(_steps_per_mm) == 0
        publish(g)
        result = _lib.km_hh_request(OP_PRELOAD, g)
        if result == KM_REQ_NO_SPOOL:
            mmu.gate_maps.set_gate_status(g, GATE_EMPTY)
            mmu.set_filament_pos_state(FILAMENT_POS_UNLOADED)
            if mmu.sensor_manager.check_gate_sensor(SENSOR_ENTRY_PREFIX, g):
                mmu.gate_maps.set_gate_status(g, -1)
                return
            raise MmuError("Couldn't pick up filament at gate")
        if result != KM_REQ_ACCEPTED:
            raise MmuError('km: PRELOAD rejected, admission result %d' % result)
        mmu.log_always('Preloading gate %d...' % g)
        with mmu.wrap_suspend_filament_monitoring(), mmu.wrap_suspend_insert_events():
            event = run_until_event(g)
        mmu.set_filament_pos_state(FILAMENT_POS_UNLOADED)
        if event != KM_EVENT_PRELOAD_COMPLETE:
            mmu.gate_maps.set_gate_status(g, GATE_EMPTY)
            raise MmuError('km: PRELOAD ended in event %d, not PRELOAD_COMPLETE' % event)
        mmu.gate_maps.set_gate_status(g, GATE_AVAILABLE)
        mmu.last_preloaded_gate = g
        mmu.log_always('Filament detected and loaded in gate %d' % g)
        mmu._check_pending_filament(g, pending=pending)

    def real_unload_sequence(bowden_move=None, check_state=False, form_tip=None, extruder_only=False):
        g = mmu.gate_selected
        if mmu.filament_pos == FILAMENT_POS_UNLOADED:
            mmu.log_debug('Filament already ejected')
            return
        assert _lib.km_hh_init(_steps_per_mm) == 0
        publish(g)
        # UNLOAD needs a loaded core. The prior load_sequence was a teleport
        # (LOAD stays a stub this milestone), so make the core agree by path
        # reconciliation: ADOPT the loaded lane with consistent sensors
        # (design rule, review_hh_harness_review.md s.2 "UNLOAD needs a loaded
        # core"; km_core_reconcile_path, src/operation_core.c:945).
        adopted = _lib.km_hh_adopt(g)
        if adopted != KM_OK:
            raise MmuError('km: ADOPT refused for gate %d, result %d' % (g, adopted))
        mmu.log_info('Unloading gate %d...' % g)
        mmu._spoolman_activate_spool(0)
        result = _lib.km_hh_request(OP_UNLOAD, g)
        if result != KM_REQ_ACCEPTED:
            raise MmuError('km: UNLOAD rejected, admission result %d' % result)
        event = run_until_event(g)
        if event != KM_EVENT_UNLOAD_COMPLETE:
            raise MmuError('km: UNLOAD ended in event %d, not UNLOAD_COMPLETE' % event)
        if mmu.gate_status[g] != GATE_EMPTY:
            mmu.gate_maps.set_gate_status(g, GATE_AVAILABLE_FROM_BUFFER)
        mmu.set_filament_pos_state(FILAMENT_POS_UNLOADED)
        if mmu.is_handling_runout:
            _eject_from_gate()

    mmu.load_sequence = load_sequence
    mmu._eject_from_gate = _eject_from_gate
    if TELEPORT:
        mmu.unload_sequence = unload_sequence_teleport
        mmu._preload_gate = _preload_gate_teleport
    else:
        mmu.unload_sequence = real_unload_sequence
        mmu._preload_gate = real_preload_gate


_boot = bootstrap.Session.boot
def boot(self, *a, **k):
    r = _boot(self, *a, **k)
    self.filament()
    install_stub(self)
    return r
bootstrap.Session.boot = boot


class Result(unittest.TextTestResult):
    def addFailure(self, test, err):
        super().addFailure(test, err)
        note(test, err, 'FAIL')
    def addError(self, test, err):
        super().addError(test, err)
        note(test, err, 'ERROR')

notes = []
def note(test, err, kind):
    msg = traceback.format_exception_only(err[0], err[1])[-1].strip()
    notes.append((kind, test.id().split('.', 2)[-1], msg[:160]))


if __name__ == '__main__':
    suite = unittest.TestSuite()
    for name in sys.argv[1:]:
        suite.addTests(unittest.defaultTestLoader.loadTestsFromName(name))
    r = unittest.TextTestRunner(resultclass=Result, verbosity=0).run(suite)
    mode = 'teleport' if TELEPORT else 'real-core'
    print('SUMMARY[%s] %s ran=%d pass=%d fail=%d error=%d' % (
        mode, ' '.join(sys.argv[1:]), r.testsRun,
        r.testsRun - len(r.failures) - len(r.errors), len(r.failures), len(r.errors)))
    for kind, name, msg in notes:
        print('  %s %s :: %s' % (kind, name, msg))
