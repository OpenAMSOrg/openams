# SPDX-License-Identifier: GPL-3.0-or-later
"""LOAD attempt records: wire decoding, query, messages and the observer."""
import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from host import KlipperMMU  # noqa: E402
from host.gate_metadata import GateMetadata  # noqa: E402
from host.load_report import (Attempt, decode_load_attempts, describe,  # noqa: E402
                              gate_learned_from_load, query_load_attempts)
from host.operation_stats import OperationStats  # noqa: E402
from host.protocol import Event, Identity, SensorSample, SensorSnapshot  # noqa: E402
from host.wire import (LOAD_ATTEMPTS, LOAD_ATTEMPTS_QUERY, EventType, Fault,  # noqa: E402
                       OpType, Stage, payload_fields)

# The event reader's fake reactor and MCU drive the real exchange().
spec = importlib.util.spec_from_file_location(
    'km_event_reader_fixture', ROOT / 'tests/klipper_event_reader/test_reader.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
MCU, Reactor = fixture.MCU, fixture.Reactor

INSTANCE = 0x1020304050607080
LOAD = Identity(1, 9, 10, 1, 2)
EJECT = Identity(LOAD.client, LOAD.session, LOAD.request, OpType.EJECT, LOAD.lane)
HUB, BUFFER, RETRACT = (Stage.LOAD_FEED_TO_HUB, Stage.LOAD_FEED_TO_BUFFER,
                        Stage.LOAD_RETRY_RETRACT)
STEPS_PER_M = 688172  # BoxTurtle: 16 microsteps, 200 steps/rev, 4.65 mm.


def reply(*records, session=9, request=10, instance=INSTANCE):
    params = dict(instance_hi=instance >> 32, instance_lo=instance & 0xffffffff,
                  session=session, request=request, count=len(records))
    for index in range(3):
        stage, fault, travel = records[index] if index < len(records) else (0, 0, 0)
        params.update({'stage%d' % index: stage, 'fault%d' % index: fault,
                       'travel%d' % index: travel})
    return params


def mm(value):
    return round(value * STEPS_PER_M / 1000)


class DecodeTest(unittest.TestCase):
    def test_reply_fields_follow_the_firmware_format(self):
        self.assertEqual([name for name, _ in payload_fields(LOAD_ATTEMPTS)][:3],
                         ['session', 'request', 'count'])
        self.assertEqual(LOAD_ATTEMPTS_QUERY.split()[0], 'km_load_attempts_query')
        self.assertEqual(decode_load_attempts(reply((HUB, Fault.NO_PROGRESS, 5),
                                                    (BUFFER, Fault.NONE, 7))),
                         (INSTANCE, 9, 10, (Attempt(HUB, Fault.NO_PROGRESS, 5),
                                            Attempt(BUFFER, Fault.NONE, 7))))
        self.assertEqual(decode_load_attempts(reply())[3], ())

    def test_malformed_or_unknown_values_are_rejected(self):
        # Fault codes are an append-only wire enum: 12 is now SELECTOR (a
        # deliberate protocol addition), so the rejected fault is one past it.
        bad = [dict(reply(), count=4), reply((16, 0, 0)),
               reply((HUB, Fault.SELECTOR + 1, 0)),
               dict(reply(), travel2=1 << 32), dict(reply(), session=None)]
        for params in bad:
            with self.subTest(params=params), self.assertRaises(ValueError):
                decode_load_attempts(params)


class DescribeTest(unittest.TestCase):
    def test_each_cause_says_where_and_why(self):
        attempts = (Attempt(BUFFER, Fault.JAM, mm(312)),
                    Attempt(HUB, Fault.NO_PROGRESS, mm(523)),
                    Attempt(BUFFER, Fault.NO_PROGRESS, mm(1900)))
        summary, lines = describe(EventType.LOAD_FAILED, 2, attempts, STEPS_PER_M,
                                  mm(1612))
        self.assertEqual(summary, 'lane 2 LOAD failed after 3 attempts')
        self.assertEqual(lines, [
            'attempt 1, HUB to extruder: compressed at 312 mm past HUB (19% of '
            'the 1612 mm Bowden estimate), before the calibrated minimum: '
            'obstruction or kink in the Bowden, or a path shorter than calibrated '
            '(recalibrate); retract behind the HUB and retry',
            'attempt 2, lane to HUB: no HUB after 523 mm: tip catching at the hub '
            'entry, spool snag, tangle or filament not gripped; back off and '
            'retry at half speed',
            'attempt 3, HUB to extruder: no resistance within 1900 mm past HUB '
            '(118% of the 1612 mm Bowden estimate): lane drive slipping, PTFE '
            'disconnected between HUB and toolhead, FPS not responding (magnet, '
            'Hall or DiffA/DiffB wiring), or a path longer than the bound '
            '(recalibrate with KLIPPER_MMU_CALIBRATE)'])

    def test_final_causes_success_and_units(self):
        summary, lines = describe(EventType.LOAD_COMPLETE, 0, (
            Attempt(BUFFER, Fault.FILAMENT_LOST, 1000),
            Attempt(BUFFER, Fault.BUFFER_SIGNAL, 2000),
            Attempt(BUFFER, Fault.NONE, 3000)))
        self.assertEqual(summary, 'lane 0 LOAD succeeded after 3 attempts')
        self.assertEqual(lines, [
            'attempt 1, HUB to extruder: HUB lost at 1000 microsteps past HUB: '
            'runout or broken filament',
            'attempt 2, HUB to extruder: stopped by BUFFER_SIGNAL at 2000 '
            'microsteps past HUB',
            'attempt 3, HUB to extruder: arrived at 3000 microsteps past HUB'])
        summary, lines = describe(EventType.LOAD_FAILED, 1, (
            Attempt(HUB, Fault.NO_PROGRESS, 10), Attempt(RETRACT, Fault.DRIVER_FAULT, 0)))
        self.assertEqual(summary, 'lane 1 LOAD failed after 1 attempt')
        self.assertEqual(lines[1], 'retract before attempt 2 stopped by DRIVER_FAULT')
        # A retryable cause ends a failed LOAD early only by its time budget;
        # a cancelled one was retrying.
        for event_type, tail in ((EventType.LOAD_FAILED, 'the 240 s LOAD time budget ran out'),
                                 (EventType.LOAD_CANCELLED, 'back off and retry')):
            lines = describe(event_type, 1, (Attempt(HUB, Fault.NO_PROGRESS, 10),))[1]
            self.assertTrue(lines[0].endswith('; ' + tail), lines)
        self.assertEqual(describe(EventType.LOAD_CANCELLED, 1, ()),
                         ('lane 1 LOAD cancelled', []))


class QueryTest(unittest.TestCase):
    def test_only_the_matching_identity_reply_is_returned(self):
        replies = [reply(session=8), reply(instance=INSTANCE + 1),
                   reply((BUFFER, Fault.NONE, 42))]
        mcu = MCU(lambda mcu: [mcu.emit('km_load_attempts', params) for params in replies])
        attempts = query_load_attempts(mcu, Reactor(), INSTANCE, LOAD, lambda: True)
        self.assertEqual(attempts, (Attempt(BUFFER, Fault.NONE, 42),))
        self.assertEqual(mcu.command_format, LOAD_ATTEMPTS_QUERY)
        self.assertEqual(mcu.sent, [[INSTANCE >> 32, INSTANCE & 0xffffffff, 1, 9, 10, 1, 2]])
        self.assertEqual(mcu.listeners, [])
        silent = MCU(lambda mcu: None)
        with self.assertRaisesRegex(ValueError, 'timed out'):
            query_load_attempts(silent, Reactor(), INSTANCE, LOAD, lambda: True)


class GateLearnedFromLoadTest(unittest.TestCase):
    """Pure classification a no-BAY-switch gate's status is learned from
    (host/__init__.py's _learn_gate_status wires this to gate_metadata.py)."""

    def test_reaching_the_buffer_proves_available_even_if_load_failed_later(self):
        # Reached HUB (and past it) on an earlier attempt, then failed for an
        # unrelated reason on a later one: the gate had filament regardless.
        attempts = (Attempt(BUFFER, Fault.NONE, mm(1600)),
                    Attempt(HUB, Fault.NO_PROGRESS, mm(5)))
        self.assertTrue(gate_learned_from_load(EventType.LOAD_FAILED, attempts))

    def test_load_complete_proves_available(self):
        self.assertTrue(gate_learned_from_load(
            EventType.LOAD_COMPLETE, (Attempt(BUFFER, Fault.NONE, mm(1600)),)))

    def test_never_reaching_the_buffer_on_a_failed_load_proves_empty(self):
        attempts = (Attempt(HUB, Fault.NO_PROGRESS, mm(200)),
                    Attempt(RETRACT, Fault.NO_PROGRESS, 0),
                    Attempt(HUB, Fault.NO_PROGRESS, mm(150)))
        self.assertFalse(gate_learned_from_load(EventType.LOAD_FAILED, attempts))

    def test_cancelled_or_attemptless_proves_nothing(self):
        attempts = (Attempt(HUB, Fault.NO_PROGRESS, mm(200)),)
        self.assertIsNone(gate_learned_from_load(EventType.LOAD_CANCELLED, attempts))
        self.assertIsNone(gate_learned_from_load(EventType.LOAD_FAILED, ()))


def make_observer(records):
    mmu = object.__new__(KlipperMMU)
    mmu._name, mmu._active, mmu._connected = 'box', True, True
    mmu._state, mmu._instance, mmu._generation = 'attached', INSTANCE, 3
    mmu.operation_stats, mmu._bowden_estimate, mmu.last_load = OperationStats(), None, None
    mmu._gate_metadata = None
    mmu._mcu = MCU(lambda mcu: mcu.emit('km_load_attempts', reply(*records)))
    mmu._mcu.get_constants = lambda: {'KLIPPER_MMU_FEEDER_STEPS_PER_M': STEPS_PER_M}
    return mmu


class ObserverTest(unittest.TestCase):
    def observer(self, records):
        mmu = make_observer(records)
        self.console = []
        gcode = types.SimpleNamespace(respond_info=self.console.append)
        mmu._printer = types.SimpleNamespace(get_reactor=Reactor,
                                             lookup_object=lambda name: gcode)
        return mmu

    def acked(self, mmu, event_type):
        mmu.record_acked(Event(INSTANCE, LOAD, 1, 100, event_type,
                               0 if event_type == EventType.LOAD_COMPLETE else 7, 0, 0))

    def test_retries_are_shown_and_kept_for_status(self):
        mmu = self.observer([(HUB, Fault.NO_PROGRESS, mm(200)), (BUFFER, Fault.NONE, mm(1600))])
        with self.assertLogs(level='INFO') as logs:
            self.acked(mmu, EventType.LOAD_COMPLETE)
        expected = ('klipper_mmu box: lane 2 LOAD succeeded after 2 attempts\n'
                    'attempt 1, lane to HUB: no HUB after 200 mm: tip catching at '
                    'the hub entry, spool snag, tangle or filament not gripped; '
                    'back off and retry\n'
                    'attempt 2, HUB to extruder: arrived at 1600 mm past HUB')
        self.assertEqual(self.console, [expected])
        self.assertIn(expected, logs.output[0])
        self.assertEqual(mmu.last_load['summary'], 'lane 2 LOAD succeeded after 2 attempts')
        self.assertEqual((mmu.last_load['lane'], mmu.last_load['request']), (2, 10))

    def test_first_attempt_success_is_only_logged(self):
        mmu = self.observer([(BUFFER, Fault.NONE, mm(1600))])
        with self.assertLogs(level='INFO'):
            self.acked(mmu, EventType.LOAD_COMPLETE)
        self.assertEqual(self.console, [])
        self.assertEqual(mmu.last_load['attempts'],
                         ['attempt 1, HUB to extruder: arrived at 1600 mm past HUB'])

    def test_arrivals_are_kept_per_lane_for_lane_slip(self):
        mmu = self.observer([(BUFFER, Fault.NONE, mm(1600))])
        with self.assertLogs(level='INFO'):
            for _ in range(7):
                self.acked(mmu, EventType.LOAD_COMPLETE)
        self.assertEqual(mmu._arrival_history, {2: [mm(1600)] * 5})
        failed = self.observer([(HUB, Fault.NO_PROGRESS, mm(200))])
        with self.assertLogs(level='INFO'):
            self.acked(failed, EventType.LOAD_FAILED)
        self.assertFalse(getattr(failed, '_arrival_history', {}))

    def test_unavailable_report_never_fails_the_ack(self):
        mmu = self.observer([])
        mmu._mcu = MCU(lambda mcu: None)  # Firmware without the query stays silent.
        with self.assertLogs(level='WARNING'):
            self.acked(mmu, EventType.LOAD_FAILED)
        self.assertIsNone(mmu.last_load)
        self.assertEqual(mmu.operation_stats.snapshot(), {('load', 2, 'failure'): 1})


def bays_snapshot(configured):
    """A raw SensorSnapshot with (or without) a real per-lane BAY switch.

    _learn_gate_status only reads .bays[*].configured; the other fields are
    placeholders."""
    placeholder = SensorSample(False, False, False, False, 0)
    bay = SensorSample(configured, False, True, True, 100)
    return SensorSnapshot(INSTANCE, 1, 4, 100, (bay,) * 4, placeholder, placeholder)


class LearnGateStatusTest(unittest.TestCase):
    """host/__init__.py's _learn_gate_status: LOAD outcomes update a no-BAY-
    switch gate's persisted status (gate_metadata.py), never a real one's."""

    def gated_observer(self, records, snapshot=None):
        mmu = make_observer(records)
        gcode = types.SimpleNamespace(respond_info=lambda message: None)
        mmu._printer = types.SimpleNamespace(get_reactor=Reactor,
                                             lookup_object=lambda name: gcode)
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        mmu._gate_metadata = GateMetadata(os.path.join(directory, 'gates.json'), 4)
        mmu.capture_physical_state = Mock(
            return_value=(None, 0., None, snapshot or bays_snapshot(False)))
        return mmu

    def acked(self, mmu, event_type):
        mmu.record_acked(Event(INSTANCE, LOAD, 1, 100, event_type,
                               0 if event_type == EventType.LOAD_COMPLETE else 7, 0, 0))

    def test_unknown_becomes_available_after_a_successful_load(self):
        mmu = self.gated_observer([(BUFFER, Fault.NONE, mm(1600))])
        self.assertIsNone(mmu._gate_metadata.gate_status(LOAD.lane))
        with self.assertLogs(level='INFO'):
            self.acked(mmu, EventType.LOAD_COMPLETE)
        self.assertTrue(mmu._gate_metadata.gate_status(LOAD.lane))

    def test_unknown_becomes_empty_after_a_load_that_finds_no_filament(self):
        mmu = self.gated_observer([(HUB, Fault.NO_PROGRESS, mm(200))])
        with self.assertLogs(level='INFO'):
            self.acked(mmu, EventType.LOAD_FAILED)
        self.assertFalse(mmu._gate_metadata.gate_status(LOAD.lane))

    def test_a_real_bay_switch_is_never_overwritten_by_a_load_outcome(self):
        mmu = self.gated_observer([(BUFFER, Fault.NONE, mm(1600))],
                                  snapshot=bays_snapshot(True))
        with self.assertLogs(level='INFO'):
            self.acked(mmu, EventType.LOAD_COMPLETE)
        self.assertIsNone(mmu._gate_metadata.gate_status(LOAD.lane))

    def test_eject_marks_a_no_bay_gate_empty(self):
        mmu = self.gated_observer([])
        mmu._gate_metadata.edit(LOAD.lane, {'available': True})
        mmu.record_acked(Event(INSTANCE, EJECT, 1, 100, EventType.EJECT_COMPLETE,
                               0, 0, 0))
        self.assertFalse(mmu._gate_metadata.gate_status(LOAD.lane))

    def test_eject_leaves_a_real_bay_gate_alone(self):
        mmu = self.gated_observer([], snapshot=bays_snapshot(True))
        mmu.record_acked(Event(INSTANCE, EJECT, 1, 100, EventType.EJECT_COMPLETE,
                               0, 0, 0))
        self.assertIsNone(mmu._gate_metadata.gate_status(LOAD.lane))

    def test_user_override_persists_through_a_load_that_proves_nothing(self):
        mmu = self.gated_observer([(HUB, Fault.NO_PROGRESS, mm(200))])
        mmu._gate_metadata.edit(LOAD.lane, {'available': True})  # user's own note
        with self.assertLogs(level='INFO'):
            self.acked(mmu, EventType.LOAD_CANCELLED)
        self.assertTrue(mmu._gate_metadata.gate_status(LOAD.lane))

    def test_gate_status_untouched_without_gate_metadata_configured(self):
        mmu = make_observer([(BUFFER, Fault.NONE, mm(1600))])
        mmu._printer = types.SimpleNamespace(get_reactor=Reactor,
                                             lookup_object=lambda name: types.SimpleNamespace(
                                                 respond_info=lambda message: None))
        with self.assertLogs(level='INFO'):
            self.acked(mmu, EventType.LOAD_COMPLETE)  # must not raise


if __name__ == '__main__':
    unittest.main()
