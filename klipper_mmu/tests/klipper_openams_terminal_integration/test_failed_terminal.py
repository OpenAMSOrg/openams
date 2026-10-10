# SPDX-License-Identifier: GPL-3.0-or-later
"""A failed OpenAMS terminal crosses runtime, managed unit, and ACK boundary."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests/klipper_openams_hook'),
                str(ROOT / 'tests/klipper_openams_device'),
                str(ROOT / 'tests/klipper_event_cycle')]
sys.dont_write_bytecode = True

from host.event_cycle import run_event_cycle
from host.protocol import Admission
from host.requests import LOAD
import test_runtime_hook as runtime_fixture
import test_managed_unit as unit_fixture
import test_cycle as cycle_fixture


class FailedTerminalIntegration(unittest.TestCase):
    def test_retained_failure_waits_for_new_physical_stop_then_acks(self):
        state = runtime_fixture.S
        oam = runtime_fixture.Oam()
        runtime = runtime_fixture.RuntimeHook.Runtime(
            runtime_fixture.Printer(), ['fps'], lambda now: state.World(),
            lambda idx: oam if idx == 1 else None)
        waiter = runtime.request('fps', state.LoadBay('fps', (1, 0)))
        gen = oam.gen
        runtime.system = state.replace(
            runtime.system,
            lanes={'fps': state.replace(runtime.system.lanes['fps'],
                                        following=True)})

        observer, unit = unit_fixture.ManagedUnitTests().make_bound()
        observer.reactor.completion = cycle_fixture.Completion
        observer.reactor.register_async_callback = lambda callback: callback(
            observer.reactor.now)
        requests = unit._event_binding[1].requests
        delivery = unit._event_binding[1].delivery
        identity = requests.issue(LOAD, 0, 'fps', gen)
        observer.owner = identity
        observer.armed = True
        observer.follower_state = 1
        self.assertTrue(unit.refresh())
        unit._runtime = runtime
        unit._follower_verifier.runtime = runtime
        runtime.resolve_oam = lambda idx: unit if idx == 1 else None

        event = dict(instance_hi=requests.instance >> 32,
                     instance_lo=requests.instance & 0xffffffff,
                     seq=42, timestamp_ms=12, client=identity.client,
                     session=identity.session, request=identity.request,
                     operation=identity.operation, lane=identity.lane,
                     event_type=2, result_code=7, value=0, flags=0)
        stop_sends = []
        ack_sends = []

        def respond(mcu, name):
            if name == 'km_event_peek':
                mcu.emit('km_event', event)
            elif name == 'km_event_ack':
                ack_sends.append(name)
                mcu.emit('km_command_result', dict(
                    instance_hi=requests.instance >> 32,
                    instance_lo=requests.instance & 0xffffffff,
                    command=6, query=42, result=0))
            else:
                self.fail('unexpected command ' + name)

        mcu = cycle_fixture.MCU(respond)
        def cycle():
            return run_event_cycle(mcu, observer.reactor, delivery, requests,
                                   runtime, unit._event_current, timeout=.1)

        with patch('host.openams_follow_bridge.send_operation',
                   side_effect=lambda m, r, instance, owner, current, **kw:
                   stop_sends.append(owner) or Admission(instance, owner, 0, 0)):
            self.assertEqual(cycle(), 'blocked')
            self.assertEqual(len(stop_sends), 1)
            self.assertFalse(waiter.test())
            self.assertEqual(ack_sends, [])
            self.assertEqual(cycle(), 'blocked')
            self.assertEqual(len(stop_sends), 1)
            observer.query += 1
            observer.reactor.now += .01
            self.assertTrue(unit.refresh())
            unit._verify_pending_follower(observer.reactor.now)
            self.assertEqual(cycle(), 'blocked')
            self.assertFalse(waiter.test())
            self.assertEqual(ack_sends, [])
            observer.query += 1
            observer.armed = False
            observer.follower_state = 0
            observer.owner = None
            observer.reactor.now += .01
            self.assertTrue(unit.refresh())
            unit._verify_pending_follower(observer.reactor.now)
            self.assertEqual(cycle(), 'acked')
            self.assertTrue(waiter.test())
            self.assertEqual(waiter.wait().code, 6)
            self.assertFalse(runtime.accept_terminal('fps', 7, 0, gen))
            self.assertNotIn('fps', requests.pending)
            self.assertEqual(len(stop_sends), 1)
            self.assertEqual(ack_sends, ['km_event_ack'])


if __name__ == '__main__':
    result = unittest.TextTestRunner().run(
        unittest.defaultTestLoader.loadTestsFromTestCase(FailedTerminalIntegration))
    sys.exit(not result.wasSuccessful())
