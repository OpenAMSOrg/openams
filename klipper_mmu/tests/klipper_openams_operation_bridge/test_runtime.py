# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise the load callback against the OPENAMS_SOURCE runtime."""

from pathlib import Path
import sys
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from host.openams_operation_bridge import (OpenAMSLoadCommandBridge,
                                           OpenAMSUnloadCommandBridge,
                                           OpenAMSPTFECalibrationCommandBridge)
from host.protocol import Admission, CommandResult
from host.requests import Requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from openams_source import load

S, runtime_module = load('oams_state', 'oams_runtime')


class Completion:
    def __init__(self):
        self.result = None

    def test(self):
        return self.result is not None

    def complete(self, result):
        self.result = result

    def wait(self):
        return self.result


class Reactor:
    def __init__(self):
        self.callbacks = []

    def monotonic(self):
        return 0.0

    def completion(self):
        return Completion()

    def register_async_callback(self, callback):
        self.callbacks.append(callback)

    def register_timer(self, callback, when):
        return callback

    def unregister_timer(self, timer):
        pass

    def drain(self):
        while self.callbacks:
            self.callbacks.pop(0)(0.0)


class Printer:
    def __init__(self, reactor):
        self.reactor = reactor

    def get_reactor(self):
        return self.reactor


class RuntimeBridgeTest(unittest.TestCase):
    S, Runtime = S, runtime_module.Runtime

    def setUp(self):
        self.reactor = Reactor()
        self.requests = Requests(0x1020304050607080, 8, 0)
        self.runtime = None
        self.bridge = OpenAMSLoadCommandBridge(
            object(), self.reactor, self.requests, 'fps', 4,
            lambda: True, lambda: True,
            lambda fps, code, value, gen: self.runtime.accept_terminal(
                fps, code, value, gen))
        self.runtime = self.Runtime(
            Printer(self.reactor), ['fps'], lambda now: self.S.World(),
            lambda idx: self.bridge if idx == 1 else None)

    def start(self):
        return self.runtime.request('fps', self.S.LoadBay('fps', (1, 0)))

    def test_rejected_admission_settles_then_retry_waits_for_terminal(self):
        calls = []

        def send(mcu, reactor, instance, identity, current, direction):
            calls.append(identity)
            return Admission(instance, identity, 2 if len(calls) == 1 else 0, 0)

        with patch('host.openams_operation_bridge.send_operation', side_effect=send):
            first = self.start()
            self.assertFalse(first.test())
            self.assertEqual(self.requests.pending['fps'][1], 1)
            self.reactor.drain()
            self.assertTrue(first.test())
            self.assertFalse(first.wait().ok)
            self.assertEqual(first.wait().code, 2)
            self.assertFalse(self.requests.pending)

            retry = self.start()
            self.assertFalse(retry.test())
            self.assertEqual(len(calls), 2)
            self.assertEqual((calls[0].request, calls[1].request), (1, 2))
            self.assertEqual(self.requests.pending['fps'][1], 2)

    def test_administrative_rejection_fails_runtime_and_retires_match(self):
        def send(mcu, reactor, instance, identity, current, direction):
            return CommandResult(instance, 1, identity.request, 7)

        with patch('host.openams_operation_bridge.send_operation', side_effect=send):
            waiter = self.start()
        self.assertFalse(waiter.test())
        self.assertEqual(len(self.reactor.callbacks), 2)
        self.reactor.drain()
        self.assertTrue(waiter.test())
        self.assertFalse(waiter.wait().ok)
        self.assertEqual(waiter.wait().code, 5)
        self.assertFalse(self.requests.pending)

    def test_unload_rejection_settles_real_runtime_waiter(self):
        self.bridge = OpenAMSUnloadCommandBridge(
            object(), self.reactor, self.requests, 'fps', 4,
            lambda: 2, lambda: True, lambda: True,
            lambda fps, code, value, gen: self.runtime.accept_terminal(
                fps, code, value, gen))
        # A failed UNLOAD settles only after a confirmed follower STOP.
        self.bridge.set_oams_follower = Mock(
            return_value=SimpleNamespace(follower_confirmed=True))
        lane = self.runtime.system.lanes['fps']
        self.runtime.system = replace(
            self.runtime.system,
            lanes={'fps': replace(lane, op=self.S.OP_LOADED, unit=(1, 2))})

        def send(mcu, reactor, instance, identity, current, direction):
            self.assertEqual((identity.operation, identity.lane, direction),
                             (2, 2, 0))
            return Admission(instance, identity, 2, 0)

        with patch('host.openams_operation_bridge.send_operation', side_effect=send):
            waiter = self.runtime.request('fps', self.S.Unload('fps'))
        self.assertFalse(waiter.test())
        self.reactor.drain()
        self.assertTrue(waiter.test())
        self.assertFalse(waiter.wait().ok)
        self.assertEqual(waiter.wait().code, 2)
        self.assertFalse(self.requests.pending)
        self.assertEqual(self.bridge.set_oams_follower.call_args.args[0], 0)

    def test_ptfe_rejection_settles_real_runtime_waiter(self):
        self.bridge = OpenAMSPTFECalibrationCommandBridge(
            object(), self.reactor, self.requests, 'fps', 4,
            lambda: True, lambda: True,
            lambda fps, code, value, gen: self.runtime.accept_terminal(
                fps, code, value, gen))

        def send(mcu, reactor, instance, identity, current, direction):
            self.assertEqual((identity.operation, identity.lane, direction),
                             (4, 2, 1))
            return Admission(instance, identity, 2, 0)

        with patch('host.openams_operation_bridge.send_operation', side_effect=send):
            waiter = self.runtime.request(
                'fps', self.S.Calibrate('fps', 1, 2, 'ptfe'))
        self.assertFalse(waiter.test())
        self.reactor.drain()
        self.assertTrue(waiter.test())
        self.assertFalse(waiter.wait().ok)
        self.assertEqual(waiter.wait().code, 2)
        self.assertFalse(self.requests.pending)

    def test_hub_hes_refuses_real_runtime_without_request(self):
        self.bridge = OpenAMSPTFECalibrationCommandBridge(
            object(), self.reactor, self.requests, 'fps', 4,
            lambda: True, lambda: True,
            lambda fps, code, value, gen: self.runtime.accept_terminal(
                fps, code, value, gen))
        with patch('host.openams_operation_bridge.send_operation') as send:
            waiter = self.runtime.request(
                'fps', self.S.Calibrate('fps', 1, 2, 'hub_hes'))
            self.reactor.drain()
            send.assert_not_called()
        self.assertTrue(waiter.test())
        self.assertFalse(waiter.wait().ok)
        self.assertFalse(self.requests.pending)


if __name__ == '__main__':
    unittest.main()
