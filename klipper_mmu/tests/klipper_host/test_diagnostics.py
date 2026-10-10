# SPDX-License-Identifier: GPL-3.0-or-later
"""Table-driven checks for the plain-language diagnostics and commissioning
verdict functions (docs/diagnostics.md); no MCU or reactor involved."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from host.diagnostics import (  # noqa: E402
    Finding, commission_eject_result, commission_fps, commission_lane_insert,
    commission_preload_result, commission_respooler_answer, commission_switches,
    commission_switch_wiggle, diagnose, format_finding, summarize)
from host.protocol import (DiagSnapshot, Event, Identity, SensorSample,  # noqa: E402
                           SensorSnapshot, Snapshot)
from host.wire import EventType, Fault, OpState  # noqa: E402

INSTANCE = 0x1020304050607080


def sample(present=False, configured=True, valid=True, fresh=True):
    return SensorSample(configured, present, valid, fresh, 100)


def build_sensors(lane_count=4, bays=None, loads=None, hub=None, toolhead=None):
    unconfigured = sample(configured=False)
    bays = bays or tuple(sample() for _ in range(lane_count))
    loads = loads or tuple(sample() for _ in range(lane_count))
    loads = loads + (unconfigured,) * (8 - len(loads))
    hub = hub if hub is not None else sample()
    toolhead = toolhead if toolhead is not None else unconfigured
    return SensorSnapshot(INSTANCE, 1, lane_count, 100, bays, hub, toolhead, loads)


def build_status(**values):
    base = dict(path_occupied=False, lane_active=False, terminal_pending=False,
               occupant_lane=255, owner_lane=255, state=OpState.IDLE,
               buffer_valid=True, buffer_position=0)
    base.update(values)
    return Snapshot(INSTANCE, 1, base)


def subjects(findings):
    return [(f.severity, f.subject) for f in findings]


REST = build_status()
EMPTY_SENSORS = build_sensors()


def diag(**overrides):
    base = dict(instance=INSTANCE, query=1,
               bay_bounce=(0,) * 8, load_bounce=(0,) * 8,
               bay_idle_edges=(0,) * 8, load_idle_edges=(0,) * 8,
               hub_bounce=0, hub_idle_edges=0, toolhead_bounce=0,
               gstat=(0,) * 8, drv_status=(0,) * 8)
    base.update(overrides)
    return DiagSnapshot(**base)


class DiagnoseRuleTest(unittest.TestCase):
    """One row per docs/diagnostics.md rule; each names the exact findings."""

    def test_rules(self):
        made = sample(present=True)
        stale = sample(valid=False)
        loaded_lane2 = tuple(sample(present=(i == 2)) for i in range(4))
        cases = [
            ('clean rest finds nothing', REST, EMPTY_SENSORS, {}, []),
            ('1: a stale HUB is named',
             REST, build_sensors(hub=stale), {},
             [('error', 'HUB switch')]),
            ('1: a stale load switch is named',
             REST, build_sensors(loads=(sample(), sample(), stale, sample())), {},
             [('error', 'lane 2 LOAD switch')]),
            ('1: an unconfigured switch is silent',
             REST, build_sensors(toolhead=sample(configured=False)), {}, []),
            ('2: LOAD made, BAY open on the same lane',
             REST, build_sensors(bays=(sample(), sample(), sample(present=False), sample()),
                                 loads=(sample(), sample(), made, sample())),
             {}, [('warning', 'lane 2 BAY switch')]),
            ('2: not at rest, no finding',
             build_status(lane_active=True),
             build_sensors(bays=(sample(), sample(), sample(present=False), sample()),
                          loads=(sample(), sample(), made, sample())),
             {}, []),
            ('3: HUB made while path reported empty',
             build_status(path_occupied=False), build_sensors(hub=made), {},
             [('error', 'HUB switch')]),
            ('3: HUB made while path occupied is not a finding',
             build_status(path_occupied=True, occupant_lane=1),
             build_sensors(hub=made,
                           bays=(sample(), made, sample(), sample()),
                           loads=(sample(), made, sample(), sample())),
             {}, []),
            ('4: firmware says lane 1 loaded but BAY reads empty',
             build_status(path_occupied=True, occupant_lane=1),
             build_sensors(hub=made,
                           bays=(sample(), sample(present=False), sample(), sample()),
                           loads=(sample(), made, sample(), sample())),
             {}, [('warning', 'lane 1 BAY switch'), ('error', 'lane 1 BAY switch')]),
            ('4: firmware says lane 1 loaded but LOAD reads empty',
             build_status(path_occupied=True, occupant_lane=1),
             build_sensors(hub=made,
                           bays=(sample(), made, sample(), sample()),
                           loads=(sample(), sample(present=False), sample(), sample())),
             {}, [('error', 'lane 1 LOAD switch')]),
            ('X: path occupied but unknown lane',
             build_status(path_occupied=True, occupant_lane=255),
             build_sensors(hub=made),
             {}, [('error', 'shared path')]),
            ('5: two lanes LOAD made with HUB made is ambiguous',
             build_status(path_occupied=True, occupant_lane=0),
             build_sensors(hub=made, bays=(made, sample(), made, sample()),
                          loads=(made, sample(), made, sample())),
             {}, [('error', 'shared path')]),
            ('5: one lane LOAD made with HUB made is fine',
             build_status(path_occupied=True, occupant_lane=0),
             build_sensors(hub=made, bays=(made, sample(), sample(), sample()),
                          loads=(made, sample(), sample(), sample())),
             {}, []),
            ('6: buffer at/above threshold with nothing loaded',
             build_status(buffer_position=700), EMPTY_SENSORS, {},
             [('warning', 'FPS buffer')]),
            ('6: buffer below threshold at rest is normal',
             build_status(buffer_position=699), EMPTY_SENSORS, {}, []),
            ('6: buffer invalid or stale',
             build_status(buffer_valid=False), EMPTY_SENSORS, {},
             [('warning', 'FPS buffer signal')]),
            ('6: high buffer while loading is not a finding',
             build_status(buffer_position=900, lane_active=True), EMPTY_SENSORS, {}, []),
        ]
        for name, status, sensors, extra, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(subjects(diagnose(status, sensors, **extra)), expected)

    def test_retained_fault_names_every_code_it_knows(self):
        # Every code km_fault_t defines, so a new fault cannot ship unexplained.
        codes = [v for k, v in vars(Fault).items() if k.isupper() and k != 'NONE']
        self.assertIn(Fault.SELECTOR, codes)
        for code in codes:
            retained = {'value': code, 'lane': 2}
            faulted = build_status(state=OpState.FAULT, system_fault=code)
            findings = diagnose(faulted, EMPTY_SENSORS, retained_fault=retained)
            self.assertEqual(len(findings), 1, code)
            self.assertEqual(findings[0].severity, 'error')
            self.assertEqual(findings[0].subject, 'lane 2 fault')
            self.assertTrue(findings[0].meaning)
            self.assertTrue(findings[0].action)
        # A fault RECONCILE_PATH already cleared is history, not a finding.
        self.assertEqual(diagnose(REST, EMPTY_SENSORS,
                                  retained_fault={'value': Fault.JAM, 'lane': 2}), ())
        # A global fault (no lane owner), or one without a matching retained
        # record, is named generically.
        faulted = build_status(state=OpState.FAULT, system_fault=Fault.DRIVER_FAULT)
        for retained in ({'value': Fault.DRIVER_FAULT, 'lane': 255}, None,
                         {'value': Fault.JAM, 'lane': 2}):
            findings = diagnose(faulted, EMPTY_SENSORS, retained_fault=retained)
            self.assertEqual(findings[0].subject, 'retained fault')

    def test_bowden_drift_needs_more_than_ten_percent(self):
        for arrival, drift in ((1000, False), (1099, False), (1101, True), (899, True)):
            last_load = {'lane': 3, 'arrival_steps': arrival}
            findings = diagnose(REST, EMPTY_SENSORS, last_load=last_load, bowden_steps=1000)
            self.assertEqual(bool(findings), drift, arrival)
            if drift:
                self.assertEqual(findings[0].severity, 'warning')
                self.assertEqual(findings[0].subject, 'lane 3 Bowden length')
        # No estimate, no last LOAD, or no successful arrival: nothing to compare.
        self.assertEqual(diagnose(REST, EMPTY_SENSORS,
                                  last_load={'lane': 0, 'arrival_steps': 2000},
                                  bowden_steps=None), ())
        self.assertEqual(diagnose(REST, EMPTY_SENSORS, last_load=None, bowden_steps=1000), ())
        self.assertEqual(diagnose(REST, EMPTY_SENSORS,
                                  last_load={'lane': 0, 'arrival_steps': None},
                                  bowden_steps=1000), ())

    def test_lane_slip(self):
        def slip(history, bowden=1000):
            return [(f.subject, f.meaning) for f in diagnose(
                REST, EMPTY_SENSORS, bowden_steps=bowden, arrival_history=history)]

        def meaning(pct, basis):
            return ('its last 3 loads needed %d%% more feed to reach the extruder than %s'
                    % (pct, basis))
        for history, bowden in (({}, 1000), (None, 1000), ({0: [1100] * 3}, None),
                                ({0: [1000] * 3}, 1000), ({0: [1100] * 2}, 1000),
                                ({0: [1000, 1100, 1000]}, 1000),  # median, not mean
                                ({0: [1200, 1200, 1000, 1000, 1000]}, 1000),  # last 3 only
                                ({0: [1050] * 3, 1: [1050] * 3}, 1000),  # path change
                                ({0: [1000] * 3, 1: [1029] * 3}, 1000),
                                ({0: [1000] * 3, 1: [900] * 3}, 1000)):  # never below 1.0
            self.assertEqual(slip(history, bowden), [], history)
        self.assertEqual(slip({2: [1040] * 3}),
                         [('lane 2 drive', meaning(4, 'the calibration'))])
        self.assertEqual(len(slip({0: [1000] * 3, 1: [1031] * 3})), 1)
        self.assertEqual(slip({0: [1020] * 3, 1: [1020] * 3, 2: [1020] * 3, 3: [1060] * 3}),
                         [('lane 3 drive', meaning(4, 'the other lanes'))])
        # Others 1.00 and 1.02: even count, median 1.01.
        self.assertEqual(slip({0: [1000] * 3, 1: [1020] * 3, 2: [1050] * 3}),
                         [('lane 2 drive', meaning(4, 'the other lanes'))])
        self.assertEqual([s for s, _ in slip({3: [1100] * 3, 0: [1100] * 3, 1: [1000] * 3,
                                               2: [1000] * 3})],
                         ['lane 0 drive', 'lane 3 drive'])
        finding = diagnose(REST, EMPTY_SENSORS, bowden_steps=1000,
                           arrival_history={1: [1040] * 3})[0]
        self.assertEqual(finding.severity, 'warning')
        self.assertIn('idler tension', finding.action)

    def test_slipping_lane_replaces_its_bowden_drift(self):
        history = {0: [1000] * 3, 1: [1000] * 3, 2: [1150] * 3}
        subjects = [f.subject for f in diagnose(
            REST, EMPTY_SENSORS, bowden_steps=1000, arrival_history=history,
            last_load={'lane': 2, 'arrival_steps': 1150})]
        self.assertEqual(subjects, ['lane 2 drive'])  # not "recalibrate"
        subjects = [f.subject for f in diagnose(
            REST, EMPTY_SENSORS, bowden_steps=1000, arrival_history=history,
            last_load={'lane': 0, 'arrival_steps': 1150})]
        self.assertEqual(subjects, ['lane 0 Bowden length', 'lane 2 drive'])

    def test_startup_fault_only_fires_when_faulted(self):
        findings = diagnose(REST, EMPTY_SENSORS, startup_state='faulted', startup_result=-4)
        self.assertEqual(findings[0].severity, 'error')
        self.assertEqual(findings[0].subject, 'board startup')
        self.assertIn('ERR_FAULTED', findings[0].meaning)
        for state in ('idle', 'waiting', 'ready', None):
            self.assertEqual(diagnose(REST, EMPTY_SENSORS, startup_state=state), ())

    def test_format_finding_and_summarize(self):
        finding = Finding('error', 'HUB switch', 'made while empty', 'check the hub')
        self.assertEqual(format_finding('box', finding),
                         'klipper_mmu box error: HUB switch: made while empty -- check the hub')
        self.assertEqual(summarize(REST, EMPTY_SENSORS),
                         'state=idle path_occupied=False hub=open')
        loaded = build_status(path_occupied=True, occupant_lane=1, state=OpState.LOADED)
        self.assertEqual(summarize(loaded, build_sensors(hub=sample(present=True))),
                         'state=loaded path_occupied=True lane=1 hub=made')


class CommissionSwitchesTest(unittest.TestCase):
    def test_every_configured_switch_gets_one_row(self):
        rows = commission_switches(build_sensors(
            bays=(sample(present=True), sample(), sample(valid=False), sample()),
            hub=sample()))
        by_name = {name: (state, note) for name, state, note in rows}
        self.assertEqual(by_name['lane 0 BAY switch'][0], 'made')
        self.assertIn('inverted polarity', by_name['lane 0 BAY switch'][1])
        self.assertEqual(by_name['lane 2 BAY switch'][0], 'unknown')
        self.assertIn('pull-up', by_name['lane 2 BAY switch'][1])
        self.assertEqual(by_name['lane 1 BAY switch'], ('open', 'OK'))
        self.assertEqual(by_name['HUB switch'], ('open', 'OK'))
        self.assertNotIn('toolhead switch', by_name)
        self.assertEqual(len(rows), 4 + 4 + 1)  # bays + loads + hub, no toolhead


class CommissionLaneTest(unittest.TestCase):
    def test_insert_detects_the_target_lane_a_swap_or_nothing_yet(self):
        baseline = build_sensors()
        target = build_sensors(bays=(sample(), sample(), sample(present=True), sample()))
        self.assertEqual(commission_lane_insert(baseline, target, 2), ('ok', None))
        swapped = build_sensors(bays=(sample(), sample(present=True), sample(), sample()))
        self.assertEqual(commission_lane_insert(baseline, swapped, 2), ('wrong_lane', 1))
        swapped_load = build_sensors(loads=(sample(), sample(present=True), sample(), sample()))
        self.assertEqual(commission_lane_insert(baseline, swapped_load, 2), ('wrong_lane', 1))
        self.assertEqual(commission_lane_insert(baseline, baseline, 2), ('waiting', None))

    def test_preload_and_eject_results(self):
        success = Event(INSTANCE, Identity(1, 1, 1, 6, 2), 1, 0,
                        EventType.PRELOAD_COMPLETE, 0, 0, 0)
        failed = Event(INSTANCE, Identity(1, 1, 1, 6, 2), 1, 0,
                       EventType.PRELOAD_FAILED, 1, 0, 0)
        self.assertEqual(commission_preload_result(2, success).severity, 'info')
        self.assertEqual(commission_preload_result(2, failed).severity, 'error')

        class Rejected:
            admission = 5
        self.assertEqual(commission_preload_result(2, Rejected()).severity, 'error')
        self.assertIn('rejected', commission_preload_result(2, Rejected()).meaning)

        eject_ok = Event(INSTANCE, Identity(1, 1, 1, 7, 2), 1, 0,
                         EventType.EJECT_COMPLETE, 0, 0, 0)
        self.assertEqual(commission_eject_result(2, eject_ok).severity, 'info')

    def test_respooler_answer(self):
        self.assertEqual(commission_respooler_answer(1, True).severity, 'info')
        no = commission_respooler_answer(1, False)
        self.assertEqual(no.severity, 'warning')
        self.assertIn('reversed', no.action)


class CommissionFPSTest(unittest.TestCase):
    def test_ok_never_moved_short_of_target_and_sticking(self):
        self.assertEqual(commission_fps([(0, True), (950, True), (0, True)]), ())
        never_moved = commission_fps([(0, True)] * 5)
        self.assertEqual(len(never_moved), 1)
        self.assertIn('never moved', never_moved[0].meaning)
        short = commission_fps([(0, True), (800, True), (0, True)])
        self.assertEqual(len(short), 1)
        self.assertIn('only reached', short[0].meaning)
        sticking = commission_fps([(0, True), (950, True), (400, True)])
        self.assertEqual(len(sticking), 1)
        self.assertIn('did not return', sticking[0].meaning)
        inverted = commission_fps([(200, True), (950, True), (0, True)])
        self.assertEqual(len(inverted), 1)
        self.assertIn('inverted', inverted[0].action)
        no_valid = commission_fps([(None, False), (0, False)])
        self.assertEqual(len(no_valid), 1)
        self.assertEqual(no_valid[0].severity, 'error')


class Phase2DeltaRuleTest(unittest.TestCase):
    """Firmware switch bounce/idle-edge and TMC counters (docs/diagnostics.md)."""

    def test_no_baseline_seeds_silently(self):
        self.assertEqual(diagnose(REST, EMPTY_SENSORS, diag=diag(), diag_baseline=None), ())

    def test_bounce_rules(self):
        cases = [
            ('below threshold: no finding', diag(hub_bounce=2), diag(hub_bounce=0), []),
            ('at threshold(3): HUB named', diag(hub_bounce=3), diag(hub_bounce=0),
             [('warning', 'HUB switch')]),
            ('bay switch delta', diag(bay_bounce=(0, 5, 0, 0, 0, 0, 0, 0)), diag(),
             [('warning', 'lane 1 BAY switch')]),
            ('load switch delta', diag(load_bounce=(0, 0, 4, 0, 0, 0, 0, 0)), diag(),
             [('warning', 'lane 2 LOAD switch')]),
            ('toolhead delta', diag(toolhead_bounce=3), diag(),
             [('warning', 'toolhead switch')]),
            ('counter went backwards: a rebind, not a finding',
             diag(hub_bounce=1), diag(hub_bounce=50), []),
        ]
        for name, current, previous, expected in cases:
            with self.subTest(name=name):
                findings = diagnose(REST, EMPTY_SENSORS, diag=current, diag_baseline=previous)
                self.assertEqual(subjects(findings), expected)

    def test_idle_edge_rules(self):
        cases = [
            ('bay below threshold(4): no finding',
             diag(bay_idle_edges=(3, 0, 0, 0, 0, 0, 0, 0)), diag(), []),
            ('bay at threshold(4)',
             diag(bay_idle_edges=(4, 0, 0, 0, 0, 0, 0, 0)), diag(),
             [('warning', 'lane 0 BAY switch')]),
            ('load at threshold(2)',
             diag(load_idle_edges=(0, 2, 0, 0, 0, 0, 0, 0)), diag(),
             [('warning', 'lane 1 LOAD switch')]),
            ('hub at threshold(2)', diag(hub_idle_edges=2), diag(),
             [('warning', 'HUB switch')]),
        ]
        for name, current, previous, expected in cases:
            with self.subTest(name=name):
                findings = diagnose(REST, EMPTY_SENSORS, diag=current, diag_baseline=previous)
                self.assertEqual(subjects(findings), expected)

    def test_tmc_rules_are_absolute_not_delta(self):
        cases = [
            ('clean', diag(), []),
            ('reset', diag(gstat=(1, 0, 0, 0, 0, 0, 0, 0)), [('warning', 'lane 0 driver')]),
            ('uv_cp', diag(gstat=(4, 0, 0, 0, 0, 0, 0, 0)), [('error', 'lane 0 driver')]),
            ('drv_err + s2ga', diag(gstat=(2, 0, 0, 0, 0, 0, 0, 0),
                                    drv_status=(4, 0, 0, 0, 0, 0, 0, 0)),
             [('error', 'lane 0 driver')]),
            ('drv_err + s2vsa', diag(gstat=(2, 0, 0, 0, 0, 0, 0, 0),
                                     drv_status=(0x10, 0, 0, 0, 0, 0, 0, 0)),
             [('error', 'lane 0 driver')]),
            ('drv_err alone', diag(gstat=(2, 0, 0, 0, 0, 0, 0, 0)), [('error', 'lane 0 driver')]),
            ('ot', diag(drv_status=(2, 0, 0, 0, 0, 0, 0, 0)), [('error', 'lane 0 driver')]),
            ('otpw', diag(drv_status=(1, 0, 0, 0, 0, 0, 0, 0)), [('warning', 'lane 0 driver')]),
            ('ola/olb', diag(drv_status=(0x40, 0, 0, 0, 0, 0, 0, 0)),
             [('warning', 'lane 0 driver')]),
        ]
        for name, current, expected in cases:
            with self.subTest(name=name):
                # No baseline at all: TMC findings still fire (absolute, unlike bounce/idle-edge).
                findings = diagnose(REST, EMPTY_SENSORS, diag=current, diag_baseline=None)
                self.assertEqual(subjects(findings), expected)

    def test_tmc_causes_are_named_in_the_meaning(self):
        findings = diagnose(REST, EMPTY_SENSORS,
                            diag=diag(gstat=(2, 0, 0, 0, 0, 0, 0, 0),
                                      drv_status=(4, 0, 0, 0, 0, 0, 0, 0)))
        self.assertIn('shorted to ground', findings[0].meaning)
        findings = diagnose(REST, EMPTY_SENSORS,
                            diag=diag(drv_status=(2, 0, 0, 0, 0, 0, 0, 0)))
        self.assertIn('overheated', findings[0].meaning)


class CommissionSwitchesDiagTest(unittest.TestCase):
    def test_bounce_count_shown_only_when_diag_present(self):
        with_diag = {name: note for name, state, note
                    in commission_switches(EMPTY_SENSORS, diag(hub_bounce=5))}
        self.assertIn('5 bounces since setup', with_diag['HUB switch'])
        without_diag = {name: note for name, state, note in commission_switches(EMPTY_SENSORS)}
        self.assertNotIn('bounces', without_diag['HUB switch'])

    def test_wiggle_reports_only_switches_that_moved(self):
        before = diag(hub_bounce=1, bay_idle_edges=(0,) * 8)
        after = diag(hub_bounce=1, bay_idle_edges=(0, 3, 0, 0, 0, 0, 0, 0))
        findings = commission_switch_wiggle(before, after, 4)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].subject, 'lane 1 BAY switch')
        self.assertEqual(findings[0].severity, 'info')
        self.assertEqual(commission_switch_wiggle(before, before, 4), ())


if __name__ == '__main__':
    unittest.main()
