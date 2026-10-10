# SPDX-License-Identifier: GPL-3.0-or-later
"""KLIPPER_MMU_DIAGNOSE, KLIPPER_MMU_COMMISSION and the post-terminal hook."""
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from host import KlipperMMU  # noqa: E402
from host.openams_managed_unit import OpenAMSManagedUnit  # noqa: E402
from host.operation_stats import OperationStats  # noqa: E402
from host.protocol import (Admission, DiagSnapshot, Event, Identity,  # noqa: E402
                           SensorSample, SensorSnapshot, Snapshot)
from host.diagnostics import explain_startup_failure  # noqa: E402
from host.wire import (AdmissionResult, EventType, Fault, NO_LANE, NO_PIN,  # noqa: E402
                       OpState, OpType, PinRole, ResourceDiag, Result, StartupStep)

INSTANCE = 0x1020304050607080


def sample(present=False, configured=True, valid=True, fresh=True):
    return SensorSample(configured, present, valid, fresh, 100)


def build_sensors(lane_count=4, bays=None, loads=None, hub=None):
    unconfigured = sample(configured=False)
    bays = bays or tuple(sample() for _ in range(lane_count))
    loads = (loads or tuple(sample() for _ in range(lane_count))) + (unconfigured,) * 4
    hub = hub if hub is not None else sample()
    return SensorSnapshot(INSTANCE, 1, lane_count, 100, bays, hub, unconfigured, loads)


def build_status(**values):
    base = dict(path_occupied=False, lane_active=False, terminal_pending=False,
               occupant_lane=255, owner_lane=255, state=OpState.IDLE,
               buffer_valid=True, buffer_position=0)
    base.update(values)
    return Snapshot(INSTANCE, 1, base)


EMPTY = build_sensors()
REST = build_status()
HUB_STUCK = build_sensors(hub=sample(present=True))


def diag_snapshot(**overrides):
    base = dict(instance=INSTANCE, query=1,
               bay_bounce=(0,) * 8, load_bounce=(0,) * 8,
               bay_idle_edges=(0,) * 8, load_idle_edges=(0,) * 8,
               hub_bounce=0, hub_idle_edges=0, toolhead_bounce=0,
               gstat=(0,) * 8, drv_status=(0,) * 8)
    base.update(overrides)
    return DiagSnapshot(**base)


class Reactor:
    def __init__(self):
        self.now = 0.0
        self.pauses = 0

    def monotonic(self):
        return self.now

    def pause(self, waketime):
        self.pauses += 1
        self.now = waketime


class GCode:
    def __init__(self, **params):
        self.params = params
        self.messages = []
        self.respond_info = Mock(side_effect=self.messages.append)

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_int(self, name, minval=None, maxval=None):
        value = int(self.params[name])
        if (minval is not None and value < minval) or (maxval is not None and value > maxval):
            raise ValueError('%s out of range' % name)
        return value


def build_mmu():
    mmu = object.__new__(KlipperMMU)
    reactor = Reactor()
    gcode = GCode()
    mmu._name = 'box'
    mmu._printer = SimpleNamespace(command_error=RuntimeError, get_reactor=lambda: reactor,
                                   lookup_object=lambda name: gcode)
    mmu._mcu_lock = threading.Lock()
    mmu.retained_fault_diagnostic = None
    mmu.last_load = None
    mmu._bowden_estimate = None
    mmu._startup_state = None
    mmu._startup_result = None
    mmu._startup_detail = None
    mmu._diagnostic_seen = frozenset()
    mmu._diag_baseline = None
    mmu._diag_capable = False
    mmu.operation_stats = OperationStats()
    mmu._openams_unit = None
    return mmu, reactor, gcode


class DiagnoseCommandTest(unittest.TestCase):
    def test_clean_capture_reports_one_summary_line(self):
        mmu, _, _ = build_mmu()
        mmu.capture_physical_state = Mock(return_value=(None, 0., REST, EMPTY))
        gcmd = GCode()
        mmu.cmd_KLIPPER_MMU_DIAGNOSE(gcmd)
        self.assertEqual(len(gcmd.messages), 1)
        self.assertIn('no problems found', gcmd.messages[0])
        self.assertIn('state=idle', gcmd.messages[0])

    def test_findings_are_each_one_line(self):
        mmu, _, _ = build_mmu()
        mmu.capture_physical_state = Mock(return_value=(None, 0., REST, HUB_STUCK))
        gcmd = GCode()
        mmu.cmd_KLIPPER_MMU_DIAGNOSE(gcmd)
        self.assertEqual(len(gcmd.messages), 1)
        self.assertIn('klipper_mmu box error: HUB switch', gcmd.messages[0])

    def test_unavailable_connection_is_an_error(self):
        mmu, _, _ = build_mmu()
        mmu.capture_physical_state = Mock(return_value=None)
        with self.assertRaisesRegex(RuntimeError, 'available connection'):
            mmu.cmd_KLIPPER_MMU_DIAGNOSE(GCode())

    def test_faulted_startup_is_explained_with_pin_names(self):
        mmu, _, _ = build_mmu()
        mmu.capture_physical_state = Mock(return_value=None)
        mmu._startup_state, mmu._startup_result = 'faulted', Result.ERR_INVALID_PARAM
        mmu._startup_detail = dict(
            step=StartupStep.RESOURCES, lane=NO_LANE, cause=Result.ERR_INVALID_PARAM,
            reason=ResourceDiag.BAD_CLAIM, pin0=80, role0=PinRole.REWIND_FORWARD,
            lane0=0, pin1=26, role1=PinRole.REWIND_REVERSE, lane1=0)
        mmu._mcu = SimpleNamespace(get_enumerations=lambda: {
            'pin': {'PF0': 80, 'PB10': 26}})
        gcmd = GCode()
        mmu.cmd_KLIPPER_MMU_DIAGNOSE(gcmd)
        text, = gcmd.messages
        self.assertIn('checking that each pin can do its job', text)
        self.assertIn('respooler forward lane 0 (PF0) and respooler reverse '
                      'lane 0 (PB10)', text)
        self.assertIn('two channels of one timer', text)

    def test_undriven_lane_names_the_lane_and_the_real_cause(self):
        finding = explain_startup_failure(Result.ERR_FAULTED, dict(
            step=StartupStep.DRIVER_CONFIRM, lane=2, cause=Result.ERR_TIMEOUT,
            reason=ResourceDiag.NONE, pin0=NO_PIN, role0=0, lane0=0,
            pin1=NO_PIN, role1=0, lane1=0), {})
        self.assertEqual(finding.meaning, 'startup stopped while waiting for the '
                         'TMC2209 driver to answer on lane 2: no answer in time')
        self.assertIn('fitted and has motor power', finding.action)
        self.assertTrue(finding.action.endswith('then FIRMWARE_RESTART'))
        silent = explain_startup_failure(Result.ERR_FAULTED, dict(
            step=StartupStep.DRIVER_CONFIRM, lane=0, cause=Result.ERR_FAULTED,
            reason=ResourceDiag.NONE, pin0=NO_PIN, pin1=NO_PIN), {})
        self.assertTrue(silent.meaning.endswith(
            'on lane 0: it did not answer, or answered with a fault'))

    def test_detail_query_is_skipped_on_firmware_without_it(self):
        mmu, _, _ = build_mmu()
        mmu._detail_query = None
        mmu._mcu = SimpleNamespace(try_lookup_command=lambda fmt: None)
        self.assertIsNone(mmu._query_startup_detail(7))

    def test_firmware_without_detail_still_gets_a_sentence(self):
        finding = explain_startup_failure(Result.ERR_FAULTED, None, {})
        self.assertEqual(finding.meaning, 'startup stopped while starting: it faulted')

    def test_capture_failure_is_reported_not_raised_as_something_else(self):
        mmu, _, _ = build_mmu()
        mmu.capture_physical_state = Mock(side_effect=ValueError('boom'))
        with self.assertRaisesRegex(RuntimeError, 'boom'):
            mmu.cmd_KLIPPER_MMU_DIAGNOSE(GCode())


class PostTerminalHookTest(unittest.TestCase):
    def acked(self, mmu, lane=1):
        # UNLOAD_COMPLETE: exercises the generic hook without the LOAD report.
        event = Event(INSTANCE, Identity(1, 9, 10, OpType.UNLOAD, lane), 1, 100,
                     EventType.UNLOAD_COMPLETE, 0, 0, 0)
        mmu.record_acked(event)

    def test_a_finding_is_reported_once_then_not_repeated_until_it_clears(self):
        mmu, _, _ = build_mmu()
        mmu.capture_physical_state = Mock(return_value=(None, 0., REST, HUB_STUCK))
        self.acked(mmu)
        self.assertEqual(len(mmu._printer.lookup_object('gcode').messages), 1)
        # Same problem again: no repeat.
        self.acked(mmu)
        self.assertEqual(len(mmu._printer.lookup_object('gcode').messages), 1)
        # It clears: no message (nothing new to say), and the seen set resets.
        mmu.capture_physical_state = Mock(return_value=(None, 0., REST, EMPTY))
        self.acked(mmu)
        self.assertEqual(len(mmu._printer.lookup_object('gcode').messages), 1)
        # It comes back: reported again.
        mmu.capture_physical_state = Mock(return_value=(None, 0., REST, HUB_STUCK))
        self.acked(mmu)
        self.assertEqual(len(mmu._printer.lookup_object('gcode').messages), 2)

    def test_diagnostic_failure_never_raises_or_blocks_the_ack(self):
        mmu, _, _ = build_mmu()
        mmu.capture_physical_state = Mock(side_effect=RuntimeError('mcu gone'))
        self.acked(mmu)  # Must not raise.
        self.assertEqual(mmu.operation_stats.snapshot(),
                         {('unload', 1, 'success'): 1})

    def test_no_findings_send_nothing(self):
        mmu, _, _ = build_mmu()
        mmu.capture_physical_state = Mock(return_value=(None, 0., REST, EMPTY))
        self.acked(mmu)
        self.assertEqual(mmu._printer.lookup_object('gcode').messages, [])


class CommissionSwitchesFPSTest(unittest.TestCase):
    def test_switches_step_needs_no_openams_motion(self):
        mmu, _, _ = build_mmu()  # _openams_unit stays None: a -safe image.
        mmu.capture_physical_state = Mock(return_value=(None, 0., REST, HUB_STUCK))
        gcmd = GCode(STEP='switches')
        mmu.cmd_KLIPPER_MMU_COMMISSION(gcmd)
        self.assertIn('remove all filament', gcmd.messages[0])
        self.assertTrue(any('HUB switch=made' in m for m in gcmd.messages))

    def test_fps_step_needs_no_openams_motion(self):
        mmu, reactor, _ = build_mmu()  # _openams_unit stays None.
        never_moved = build_status(buffer_position=0, buffer_valid=True)
        mmu.capture_physical_state = Mock(return_value=(None, 0., never_moved, EMPTY))
        gcmd = GCode(STEP='fps')
        with patch('host._FPS_SAMPLE_S', 0.25), patch('host._FPS_SAMPLE_PERIOD', 0.1):
            mmu.cmd_KLIPPER_MMU_COMMISSION(gcmd)
        self.assertIn('push the FPS buffer', gcmd.messages[0])
        self.assertTrue(any('never moved' in m for m in gcmd.messages))

    def test_switches_step_shows_bounce_and_reports_wiggle_deltas(self):
        mmu, reactor, _ = build_mmu()
        mmu._diag_capable = True
        mmu.capture_physical_state = Mock(return_value=(None, 0., REST, EMPTY))
        before = diag_snapshot(hub_bounce=1)
        after = diag_snapshot(hub_bounce=4, bay_bounce=(0, 2, 0, 0, 0, 0, 0, 0))
        mmu.capture_diag = Mock(side_effect=[before, after])
        gcmd = GCode(STEP='switches')
        mmu.cmd_KLIPPER_MMU_COMMISSION(gcmd)
        self.assertTrue(any('bounce' in m and 'HUB switch' in m for m in gcmd.messages))
        self.assertIn('now gently wiggle', ' '.join(gcmd.messages))
        self.assertTrue(any('lane 1 BAY switch' in m and 'wiggling' in m
                            for m in gcmd.messages))
        self.assertTrue(any('HUB switch' in m and 'wiggling' in m for m in gcmd.messages))
        self.assertEqual(mmu._diag_baseline, None)  # DIAGNOSE's baseline, untouched.

    def test_lane_step_requires_openams_motion(self):
        mmu, _, _ = build_mmu()
        with self.assertRaisesRegex(RuntimeError, 'openams_motion'):
            mmu.cmd_KLIPPER_MMU_COMMISSION(GCode(STEP='lane', LANE='0'))


def fake_lane_unit(preload_response, eject_response, bay_count=4):
    unit = object.__new__(OpenAMSManagedUnit)
    unit._state = SimpleNamespace(bay_count=bay_count)
    requests = SimpleNamespace(preload_terminal=None, eject_terminal=None)
    unit._event_binding = (None, SimpleNamespace(requests=requests))
    unit._event_current = lambda: True
    calls = []
    unit.start_preload = lambda lane: (
        calls.append(('preload', lane)) or (None, preload_response))
    unit.start_eject = lambda lane: (
        calls.append(('eject', lane)) or (None, eject_response))
    unit.calls = calls
    return unit, requests


class CommissionLaneTest(unittest.TestCase):
    def test_wrong_lane_reports_a_swap_and_never_starts_motion(self):
        mmu, _, _ = build_mmu()
        unit, _ = fake_lane_unit(None, None)
        mmu._openams_unit = unit
        baseline = build_sensors()
        swapped = build_sensors(bays=(sample(), sample(present=True), sample(), sample()))
        mmu.capture_physical_state = Mock(side_effect=[
            (None, 0., REST, baseline), (None, 0., REST, swapped)])
        gcmd = GCode(STEP='lane', LANE='2')
        mmu.cmd_KLIPPER_MMU_COMMISSION(gcmd)
        self.assertTrue(any('wired swapped' in m for m in gcmd.messages))
        self.assertEqual(unit.calls, [])

    def test_insert_wait_times_out(self):
        mmu, reactor, _ = build_mmu()
        unit, _ = fake_lane_unit(None, None)
        mmu._openams_unit = unit
        baseline = build_sensors()
        mmu.capture_physical_state = Mock(return_value=(None, 0., REST, baseline))

        def pause(waketime):
            reactor.now = waketime + 61  # Jump straight past the 60 s deadline.
        reactor.pause = pause
        with self.assertRaisesRegex(RuntimeError, 'timed out'):
            mmu.cmd_KLIPPER_MMU_COMMISSION(GCode(STEP='lane', LANE='2'))
        self.assertEqual(unit.calls, [])

    def test_full_flow_reports_preload_eject_and_asks_for_confirmation(self):
        mmu, reactor, _ = build_mmu()
        preload_ok = Event(INSTANCE, Identity(1, 1, 1, OpType.PRELOAD, 2), 1, 0,
                           EventType.PRELOAD_COMPLETE, 0, 0, 0)
        eject_ok = Event(INSTANCE, Identity(1, 1, 1, OpType.EJECT, 2), 1, 0,
                         EventType.EJECT_COMPLETE, 0, 0, 0)
        unit, _ = fake_lane_unit(preload_ok, eject_ok)
        mmu._openams_unit = unit
        baseline = build_sensors()
        inserted = build_sensors(bays=(sample(), sample(), sample(present=True), sample()))
        mmu.capture_physical_state = Mock(side_effect=[
            (None, 0., REST, baseline), (None, 0., REST, inserted)])
        gcmd = GCode(STEP='lane', LANE='2')
        mmu.cmd_KLIPPER_MMU_COMMISSION(gcmd)
        self.assertEqual(unit.calls, [('preload', 2), ('eject', 2)])
        self.assertTrue(any('PRELOAD succeeded' in m for m in gcmd.messages))
        self.assertTrue(any('backed the filament out of the lane' in m for m in gcmd.messages))
        self.assertTrue(any('ANSWER=yes or ANSWER=no' in m for m in gcmd.messages))

    def test_answer_reports_the_respooler_verdict_without_motion(self):
        mmu, _, _ = build_mmu()
        unit, _ = fake_lane_unit(None, None)
        mmu._openams_unit = unit
        mmu.capture_physical_state = Mock(side_effect=AssertionError('must not capture'))
        gcmd = GCode(STEP='lane', LANE='2', ANSWER='no')
        mmu.cmd_KLIPPER_MMU_COMMISSION(gcmd)
        self.assertTrue(any('did not wind' in m for m in gcmd.messages))
        self.assertEqual(unit.calls, [])

    def test_await_local_terminal_polls_then_returns_or_times_out(self):
        mmu, reactor, _ = build_mmu()
        requests = SimpleNamespace(preload_terminal=None)
        unit = SimpleNamespace(_event_binding=(None, SimpleNamespace(requests=requests)),
                               _event_current=lambda: True)

        def pause(waketime):
            reactor.now = waketime
            requests.preload_terminal = 'done'
        reactor.pause = pause
        result = mmu._await_local_terminal(unit, 'preload_terminal', reactor.now + 5)
        self.assertEqual(result, 'done')

        requests.preload_terminal = None
        with self.assertRaisesRegex(RuntimeError, 'timed out'):
            mmu._await_local_terminal(unit, 'preload_terminal', reactor.now)


if __name__ == '__main__':
    unittest.main()
