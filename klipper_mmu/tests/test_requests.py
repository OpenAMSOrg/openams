# SPDX-License-Identifier: GPL-3.0-or-later
"""OPENAMS_SOURCE points to the sibling repository for real runtime checks."""
import dataclasses
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
from host.protocol import Admission, Event, Identity, Snapshot, decode_event
from host.requests import Requests, LOAD, UNLOAD, FOLLOW, CALIBRATE, PRELOAD

INSTANCE = 0x12345678abcdef01
SESSION = 0x89abcdef


def event(identity, kind=1, code=0, seq=123, instance=INSTANCE, value=0):
    return decode_event(dict(instance_hi=instance >> 32, instance_lo=instance & 0xffffffff,
        client=identity.client, session=identity.session, request=identity.request,
        operation=identity.operation, lane=identity.lane, seq=seq, timestamp_ms=0xfedcba98,
        event_type=kind, result_code=code, value=value, flags=0))


def snapshot(instance=INSTANCE, session=SESSION, request=0, **extra):
    return Snapshot(instance, 1, dict(client_sessions=(0, session, 0, 0),
                                     client_requests=(0, request, 0, 0), **extra))


class RequestsTest(unittest.TestCase):
    def setUp(self):
        self.requests = Requests(INSTANCE, SESSION, 0x10000)
        self.calls = []

    def deliver(self, completion):
        return self.requests.deliver(completion, lambda *args: self.calls.append(args))

    def test_preload_terminal_is_distinct_from_openams_waiter(self):
        identity = self.requests.issue_preload(2)
        self.assertEqual(identity.operation, PRELOAD)
        self.assertEqual(self.requests.pending, {})
        with self.assertRaisesRegex(ValueError, 'Existing PRELOAD'):
            self.requests.issue_preload(3)
        self.assertFalse(self.requests.accept_preload(
            event(dataclasses.replace(identity, request=identity.request + 1), 13),
            self.requests.generation))
        self.assertFalse(self.requests.accept_preload(
            event(identity, 13), self.requests.generation + 1))
        terminal = event(identity, 13)
        self.assertTrue(self.requests.accept_preload(terminal, self.requests.generation))
        self.assertIs(self.requests.preload_terminal, terminal)
        self.assertIsNone(self.requests.pending_preload)
        self.assertIsNone(self.requests.event(terminal, self.requests.generation))
        self.assertEqual(self.calls, [])

    def test_preload_failure_and_disconnect_keep_exact_pending_identity(self):
        identity = self.requests.issue_preload(1)
        self.requests.disconnect()
        self.assertFalse(self.requests.accept_preload(
            event(identity, 15, 5), self.requests.generation - 1))
        self.assertEqual(self.requests.pending_preload, identity)
        self.requests.resume(snapshot(request=identity.request))
        self.assertTrue(self.requests.accept_preload(
            event(identity, 15, 5), self.requests.generation))
        self.assertEqual(self.requests.preload_terminal.event_type, 15)

    def test_unpreload_retains_only_exact_unload_terminal_without_waiter(self):
        identity = self.requests.issue_unpreload(2)
        self.assertEqual(identity.operation, UNLOAD)
        self.assertEqual(self.requests.pending, {})
        with self.assertRaisesRegex(ValueError, 'Existing UNPRELOAD'):
            self.requests.issue_preload(2)
        self.assertFalse(self.requests.accept_unpreload(
            event(dataclasses.replace(identity, request=identity.request + 1), 4),
            self.requests.generation))
        self.assertFalse(self.requests.accept_unpreload(
            event(identity, 4), self.requests.generation + 1))
        terminal = event(identity, 4)
        self.assertTrue(self.requests.accept_unpreload(terminal, self.requests.generation))
        self.assertIs(self.requests.unpreload_terminal, terminal)
        self.assertIsNone(self.requests.pending_unpreload)
        self.assertIsNone(self.requests.event(terminal, self.requests.generation))

    def test_full_width_counter_zero_token_and_follow_no_waiter(self):
        r = Requests(INSTANCE, SESSION, 0xfffffffd)
        identity = r.issue(LOAD, 7, 'fps', 0)
        self.assertEqual(identity, Identity(1, SESSION, 0xfffffffe, LOAD, 7))
        follow = r.issue(FOLLOW, 7)
        self.assertEqual(follow.request, 0xffffffff)
        self.assertEqual(r.cancel_target('fps'), identity)
        before = dict(r.pending)
        with self.assertRaises(OverflowError):
            r.issue(UNLOAD, 7, 'fps', 1)
        self.assertEqual(r.pending, before)
        self.assertEqual(r.last_request, 0xffffffff)

    def test_invalid_input_does_not_consume_ids_or_replace_waiters(self):
        for kwargs in (
                dict(operation=0, lane=0, waiter='fps', token=0),
                dict(operation=True, lane=0, waiter='fps', token=0),
                dict(operation=LOAD, lane=12, waiter='fps', token=0),
                dict(operation=LOAD, lane=True, waiter='fps', token=0),
                dict(operation=LOAD, lane=0, waiter='', token=0),
                dict(operation=LOAD, lane=0, waiter='fps', token=256),
                dict(operation=LOAD, lane=0, waiter='fps', token=True),
                dict(operation=FOLLOW, lane=0, waiter='fps', token=1)):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.requests.issue(**kwargs)
        self.assertEqual(self.requests.last_request, 0x10000)
        self.assertEqual(self.requests.pending, {})
        for args in ((0, 1), (True, 1), (1 << 64, 1), (1, 0), (1, True), (1, 1, -1)):
            with self.assertRaises(ValueError):
                Requests(*args)
        for lane in range(8):
            self.requests.issue(LOAD, lane, str(lane), 0)
        with self.assertRaises(ValueError):
            self.requests.issue(LOAD, 0, 'ninth', 0)
        self.assertEqual(len(self.requests.pending), 8)

    def test_issue_admits_lanes_8_through_11_for_9_to_12_lane_devices(self):
        # Regression: issue() used to cap at `lane >= 8`, a leftover from
        # before selector devices with more than 8 lanes existed (ERCF 1.1's
        # 9, Tradrack's 10, KM_MAX_LANES widens to 12 for any 9-12 lane
        # image, mcu/Makefile). _issue_local (PRELOAD/UNPRELOAD/EJECT/HOME)
        # already used the correct `lane >= 12` bound; issue() (LOAD/UNLOAD/
        # FOLLOW/CALIBRATE) did not, so a whole-stack sim's final
        # OAMSM_LOAD_FILAMENT on gate 8 (ERCF 1.1) or gate 9 (Tradrack)
        # failed with "Invalid MMU lane" every time -- a real 9+ lane
        # device could PRELOAD a high lane (the local path) but never
        # LOAD/UNLOAD/CALIBRATE it (this path). Every lane up to 11 must be
        # accepted now; 12 (KM_MAX_LANES's own ceiling) must still be
        # refused, exactly like _issue_local's own bound.
        r = Requests(INSTANCE, SESSION, 0)
        for lane in range(8, 12):
            identity = r.issue(LOAD, lane, 'fps%d' % lane, 0)
            self.assertEqual(identity.lane, lane)
        with self.assertRaises(ValueError):
            r.issue(LOAD, 12, 'fps12', 0)

    def test_every_identity_component_and_connection_must_match(self):
        r = self.requests
        identity = r.issue(LOAD, 3, 'fps', 0)
        for field in ('client', 'session', 'request', 'operation', 'lane'):
            wrong = dataclasses.replace(identity, **{field: getattr(identity, field) + 1})
            self.assertIsNone(r.event(event(wrong), r.generation))
        self.assertIsNone(r.event(event(identity, instance=INSTANCE + 1), r.generation))
        self.assertIsNone(r.event(event(identity), r.generation + 1))
        match = r.event(event(identity, value=0xffffffff), r.generation)
        self.assertTrue(self.deliver(match))
        self.assertEqual(self.calls, [('fps', 0, 0xffffffff, 0)])
        self.assertFalse(self.deliver(match))
        self.assertIsNone(r.event(event(identity), r.generation))

    def test_reused_token_across_physical_bays_does_not_alias(self):
        r = self.requests
        old = r.issue(LOAD, 0, 'fps', 7)
        matched_before_replacement = r.event(event(old), r.generation)
        new = r.issue(LOAD, 3, 'fps', 7)
        self.assertIsNone(r.event(event(old), r.generation))
        self.assertFalse(self.deliver(matched_before_replacement))
        self.assertTrue(self.deliver(r.event(event(new), r.generation)))

    def test_admission_never_treats_acceptance_or_history_as_completion(self):
        r = self.requests
        identity = r.issue(LOAD, 0, 'fps', 6)
        for code in (0, 1, 10):
            match = r.admission(Admission(INSTANCE, identity, code, 0), r.generation)
            self.assertIsNone(match)
            self.assertFalse(self.deliver(match))
        self.assertEqual(r.cancel_target('fps'), identity)
        rejected = r.admission(Admission(INSTANCE, identity, 4, 0), r.generation)
        self.assertTrue(self.deliver(rejected))
        self.assertEqual(self.calls, [('fps', 4, 0, 6)])
        self.assertIsNone(r.event(event(identity), r.generation))

    def test_rejection_codes_agree_with_actual_portable_c(self):
        with tempfile.TemporaryDirectory(prefix='km-request-codes-') as work:
            source, binary = Path(work) / 'codes.c', Path(work) / 'codes'
            source.write_text('''#include "klipper_mmu/protocol_wire.h"
#include <stdio.h>
int main(void) {
    for (int a=2; a<10; ++a)
        for (int f=0; f<12; ++f)
            printf("%d %d %u\\n", a, f,
                (unsigned)km_protocol_result_code((km_admission_result_t)a, (km_fault_t)f));
    return 0;
}
''')
            subprocess.run(['gcc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-pedantic',
                '-Wconversion', '-Wsign-conversion', '-fsanitize=address,undefined',
                '-fno-sanitize-recover=all', '-fno-pie', '-no-pie',
                '-I' + str(ROOT / 'include'), str(source), str(ROOT / 'src/protocol_wire.c'),
                '-o', str(binary)], check=True, timeout=30)
            lines = subprocess.check_output([str(binary)], text=True, timeout=10).splitlines()
        r = self.requests
        identity = r.issue(LOAD, 0, 'fps', 8)
        self.assertEqual(len(lines), 96)
        for line in lines:
            admission, fault, expected = map(int, line.split())
            match = r.admission(Admission(INSTANCE, identity, admission, fault), r.generation)
            self.assertEqual(match.code, expected)

    def test_only_matching_operation_terminal_can_complete(self):
        r = self.requests
        for operation, kinds in ((LOAD, (1, 2, 3)), (UNLOAD, (4, 5, 6)), (CALIBRATE, (8, 9, 10))):
            identity = r.issue(operation, 2, 'fps', 12)
            for kind, code, expected in ((kinds[0], 0, 0), (kinds[1], 6, 6),
                                         (kinds[2], 7, 7), (kinds[2], 0, 1)):
                self.assertEqual(r.event(event(identity, kind, code), r.generation).code, expected)
            for unrelated in (7, 11, 12):
                self.assertIsNone(r.event(event(identity, unrelated), r.generation))
            with self.assertRaises(ValueError):
                r.event(event(identity, kinds[0], 7), r.generation)
        with self.assertRaises(TypeError):
            r.event(snapshot(), r.generation)

    def test_callback_failure_preserves_match_and_reentrancy_preserves_new_request(self):
        r = self.requests
        identity = r.issue(LOAD, 0, 'fps', 1)
        match = r.event(event(identity), r.generation)
        def fail(*args):
            raise RuntimeError('consumer failed before committing')
        with self.assertRaises(RuntimeError):
            r.deliver(match, fail)
        self.assertEqual(r.cancel_target('fps'), identity)
        new = []
        def replace(*args):
            new.append(r.issue(UNLOAD, 0, 'fps', 2))
        self.assertTrue(r.deliver(match, replace))
        self.assertEqual(r.cancel_target('fps'), new[0])
        self.assertFalse(self.deliver(match))

    def test_disconnect_guards_both_unmatched_and_already_matched_callbacks(self):
        r = self.requests
        identity = r.issue(LOAD, 0, 'fps', 1)
        old_generation = r.generation
        match = r.event(event(identity), old_generation)
        r.disconnect()
        with self.assertRaises(ConnectionError):
            r.issue(LOAD, 1, 'fps', 2)
        with self.assertRaises(ConnectionError):
            r.cancel_target('fps')
        self.assertFalse(self.deliver(match))
        r.resume(snapshot(request=0x12345, lane_active=True, terminal_pending=False,
                          path_occupied=True, system_fault=10))
        self.assertEqual(r.cancel_target('fps'), identity)
        self.assertEqual(self.calls, [])  # Status/fault/terminal_pending never settled it.
        self.assertIsNone(r.event(event(identity), old_generation))
        self.assertFalse(self.deliver(match))
        self.assertTrue(self.deliver(r.event(event(identity), r.generation)))
        self.assertEqual(r.issue(FOLLOW, 0).request, 0x12346)

    def test_new_controller_or_newer_session_remains_unknown(self):
        r = self.requests
        identity = r.issue(LOAD, 0, 'fps', 1)
        r.disconnect()
        for status in (snapshot(instance=INSTANCE + 1), snapshot(session=SESSION + 1)):
            with self.assertRaises(ConnectionError):
                r.resume(status)
            self.assertFalse(r.connected)
            self.assertEqual(r.pending['fps'][0], identity)
            self.assertEqual(self.calls, [])
        r.resume(snapshot(request=0))
        self.assertEqual(r.last_request, identity.request)  # Unacknowledged IDs not reused.
        with self.assertRaises(RuntimeError):
            r.resume(snapshot())

    def test_cross_controller_matched_object_cannot_deliver(self):
        identity = self.requests.issue(LOAD, 0, 'fps', 1)
        other = Requests(INSTANCE + 1, SESSION, 0x10000)
        self.assertEqual(other.issue(LOAD, 0, 'fps', 1), identity)
        candidate = other.event(event(identity, instance=INSTANCE + 1), other.generation)
        self.assertFalse(self.deliver(candidate))
        self.assertEqual(self.calls, [])

    def test_stale_malformed_reply_is_ignored_before_current_semantics(self):
        r = self.requests
        identity = r.issue(LOAD, 0, 'fps', 1)
        r.disconnect()
        r.resume(snapshot())
        self.assertIsNone(r.admission(Admission(INSTANCE, identity, 255, 255), 1))
        self.assertIsNone(r.event(event(identity, code=255), 1))
        with self.assertRaises(ValueError):
            r.event(event(identity, code=255), r.generation)


class OpenAMSRuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(os.environ['OPENAMS_SOURCE'])
        spec = importlib.util.spec_from_file_location('openams_runtime_fixtures', source / 'test/test_oams_runtime.py')
        cls.F = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.F)
        cls.S = cls.F.S

    def setUp(self):
        self.requests = Requests(INSTANCE, SESSION, 0x10000)
        self.sent, self.cancels = [], []
        owner = self
        class Device:
            """Effect fixture only: records transport intents, never firmware success."""
            lane = 0
            def start_load_spool(self, bay, gen):
                self.lane = bay
                owner.sent.append(owner.requests.issue(LOAD, bay, 'fps1', gen))
            def start_unload_spool(self, gen):
                owner.sent.append(owner.requests.issue(UNLOAD, self.lane, 'fps1', gen))
            def start_calibrate(self, kind, bay, gen):
                if kind != 'ptfe':
                    raise ValueError('Unsupported calibration')
                owner.sent.append(owner.requests.issue(CALIBRATE, bay, 'fps1', gen))
            def load_spool_cancel(self):
                owner.cancels.append(owner.requests.cancel_target('fps1'))
            def set_oams_follower(self, enable, direction):
                pass  # Printer following/ownership is outside this matcher's scope.
        self.runtime, self.reactor, _, self.effects = self.F.make_runtime(oam=Device())
        self.runtime.set_firmware_liveness(True)

    def callback(self, fps, code, value, token):
        self.runtime.dispatch(self.S.OpCompleted(fps, code, value, gen=token))

    def terminal(self, identity, kind=1, code=0, value=0, generation=None):
        r = self.requests
        match = r.event(event(identity, kind, code, value=value),
                        r.generation if generation is None else generation)
        return r.deliver(match, self.callback)

    def test_load_unload_calibration_use_real_runtime_waiters(self):
        S = self.S
        load = self.runtime.request('fps1', S.LoadBay('fps1', (1, 3)))
        identity = self.sent[-1]
        for admission in (0, 1, 10):
            match = self.requests.admission(Admission(INSTANCE, identity, admission, 0), self.requests.generation)
            self.assertFalse(self.requests.deliver(match, self.callback))
            self.assertFalse(load.test())
        self.assertTrue(self.terminal(identity))
        self.assertTrue(load.wait().ok)
        self.assertEqual(self.runtime.get_state().lanes['fps1'].op, S.OP_LOADED)
        unload = self.runtime.request('fps1', S.Unload('fps1'))
        self.assertFalse(self.terminal(identity))
        self.assertFalse(unload.test())
        self.assertTrue(self.terminal(self.sent[-1], 4))
        self.assertTrue(unload.wait().ok)
        calibration = self.runtime.request('fps1', S.Calibrate('fps1', 1, 3, 'ptfe'))
        self.assertTrue(self.terminal(self.sent[-1], 8, value=123456))
        self.assertTrue(calibration.wait().ok)
        self.assertEqual(self.runtime._deadlines, {})

    def test_generation_wrap_does_not_let_old_terminal_complete_new_load(self):
        S = self.S
        first = None
        for _ in range(256):
            completion = self.runtime.request('fps1', S.LoadBay('fps1', (1, 0)))
            if first is None:
                first = self.sent[-1]
            self.assertTrue(self.terminal(self.sent[-1], 3, 1))
            self.assertFalse(completion.wait().ok)
        current = self.runtime.request('fps1', S.LoadBay('fps1', (1, 3)))
        # The reducer's logical generation is 32 bits wide; this device keeps
        # the one-byte token, which the runtime narrows to and maps back from.
        self.assertEqual(self.runtime.get_state().lanes['fps1'].op_gen, 257)
        self.assertEqual(self.runtime._narrowed['fps1'], (257, 1))
        self.assertEqual(self.sent[-1].request - first.request, 256)
        self.assertFalse(self.terminal(first))
        self.assertFalse(current.test())
        self.assertTrue(self.terminal(self.sent[-1]))
        self.assertTrue(current.wait().ok)

    def test_cancel_keeps_full_identity_and_waits_for_cancelled_terminal(self):
        S = self.S
        completion = self.runtime.request('fps1', S.LoadBay('fps1', (1, 2)))
        identity = self.sent[-1]
        self.runtime.dispatch(S.Cancel('fps1'))
        self.assertEqual(self.cancels, [identity])
        self.assertGreater(identity.request, 255)
        self.assertFalse(completion.test())
        self.assertTrue(self.terminal(identity, 2, 6))
        self.assertFalse(completion.wait().ok)

    def test_queued_reply_from_old_connection_cannot_complete_after_resume(self):
        S = self.S
        completion = self.runtime.request('fps1', S.LoadBay('fps1', (1, 0)))
        identity = self.sent[-1]
        generation = self.requests.generation
        self.reactor.register_async_callback(lambda when: self.terminal(identity, generation=generation))
        self.requests.disconnect()
        self.requests.resume(snapshot(request=identity.request))
        self.reactor.run_async_callbacks()
        self.assertFalse(completion.test())
        self.assertTrue(self.terminal(identity))
        self.assertTrue(completion.wait().ok)

    def test_rejected_start_settles_failure_without_waiting_for_timeout(self):
        S = self.S
        completion = self.runtime.request('fps1', S.LoadBay('fps1', (1, 0)))
        identity = self.sent[-1]
        match = self.requests.admission(Admission(INSTANCE, identity, 4, 0), self.requests.generation)
        self.assertTrue(self.requests.deliver(match, self.callback))
        self.assertTrue(completion.test())
        self.assertFalse(completion.wait().ok)
        self.assertFalse(self.terminal(identity))


if __name__ == '__main__':
    unittest.main()
