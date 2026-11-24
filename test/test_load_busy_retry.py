# A load the unit refuses Busy right after its own unload is sent again.
#
# A toolchange sends the load for the new bay the moment the unload ends, and
# a unit that has just rewound can still answer Busy for a moment while it
# settles (a stock AMS behind a Cortex Bridge reports the unload done while it
# still says it is moving). Busy is the unit's admission refusal: nothing
# started. So the driver holds the refusal back and sends the same load again
# every second, until OAMS.REWIND_BUSY_RETRY_S after the unload; only a
# refusal after that reaches the reducer, worded from what the host knows.
# These tests drive the driver alone (oams.py) on each protocol: the *2 family
# a Cortex Bridge speaks, the legacy in-order replies of the OpenAMS 1
# mainboard, and protocol v4.
#
#   cd openams && python3 -m unittest discover -s test -p 'test_load_busy_retry.py' -v

import importlib
import os
import sys
import types
import unittest
from collections import deque

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if "oamspkg" not in sys.modules:
    _pkg = types.ModuleType("oamspkg")
    _pkg.__path__ = [_SRC]
    sys.modules["oamspkg"] = _pkg
# Stub the Klipper `mcu` module that oams.py imports at top level.
sys.modules.setdefault("mcu", types.ModuleType("mcu"))

oams = importlib.import_module("oamspkg.oams")
OAMS = oams.OAMS
S = importlib.import_module("oamspkg.oams_state")

LOADING = oams.OAMS_STATUS_LOADING
UNLOADING = oams.OAMS_STATUS_UNLOADING
BUSY = S.OAMS_OP_CODE_ERROR_BUSY
SUCCESS = S.OAMS_OP_CODE_SUCCESS
CANCEL = S.OAMS_OP_CODE_CANCEL
LENGTH = 2400   # encoder clicks
BAY = 1


class FakeCmd(object):
    def __init__(self):
        self.sent = []

    def send(self, args=None):
        self.sent.append(args)


class FakeReactor(object):
    """Klipper's reactor timers on a clock the test moves."""

    NEVER = 9999999999999999.0
    NOW = 0.0

    def __init__(self):
        self.now = 100.0
        self.timers = []

    def monotonic(self):
        return self.now

    def register_timer(self, callback, waketime=NEVER):
        timer = [callback, waketime]
        self.timers.append(timer)
        return timer

    def unregister_timer(self, timer):
        if timer in self.timers:
            self.timers.remove(timer)

    def register_callback(self, callback, waketime=NOW):
        def once(eventtime):
            callback(eventtime)
            return self.NEVER
        return self.register_timer(once, self.now)

    def advance(self, seconds, step=0.05):
        end = self.now + seconds
        while True:
            due = [t for t in self.timers if t[1] <= self.now]
            for timer in due:
                if timer in self.timers:
                    timer[1] = timer[0](self.now)
                    if timer[1] >= self.NEVER and timer in self.timers:
                        self.timers.remove(timer)
            if self.now >= end - 1e-9:
                return
            self.now = min(end, self.now + step)


def make_unit(protocol):
    """A bare OAMS with the fields a load, an unload and their replies use.
    `protocol`: "bridge" (the *2 family with the atomic load, as a Cortex
    Bridge speaks it), "legacy" (the OpenAMS 1 mainboard: in-order replies,
    no generation) or "v4"."""
    o = OAMS.__new__(OAMS)
    o.oams_idx = 1
    o.name = "oams ams2"
    o.device_family = "ams2"
    o.capabilities = {}
    o.reactor = FakeReactor()
    o.bay_count = 4
    o.hub_hes_value = [0, 0, 0, 0]
    o.f1s_hes_value = [1, 1, 1, 1]
    o.filament_path_length = LENGTH
    o._gen_queue = deque()
    o._pending_bay = None
    o.current_spool = None
    o.action_status = None
    o.action_status_code = None
    o.action_status_value = None
    o.status_loading = LOADING
    o.status_unloading = UNLOADING
    o.status_calibrating = oams.OAMS_STATUS_CALIBRATING
    o.status_error = oams.OAMS_STATUS_ERROR
    for name in ("oams_load_spool_cmd", "oams_unload_spool_cmd",
                 "oams_load_spool2_cmd", "oams_unload_spool2_cmd",
                 "oams_load_spool_cancel_cmd", "oams_ptfe_length_cmd"):
        setattr(o, name, FakeCmd())
    o.oams_load_spool3_cmd = None
    o._use_gen_protocol = protocol != "legacy"
    o.protocol_version = {"bridge": 3, "legacy": None, "v4": 4}[protocol]
    if protocol == "bridge":
        o.oams_load_spool3_cmd = FakeCmd()
    if protocol == "v4":
        o._use_wide_gen_protocol = True
        for name in ("oams_load_spool4_cmd", "oams_unload_spool4_cmd",
                     "oams_cancel4_cmd"):
            setattr(o, name, FakeCmd())
    o.completions = []
    # As bind_runtime does: the reason is worded when the completion goes.
    o.on_action_complete = lambda code, value, gen: o.completions.append(
        (code, gen, o._failure_reason(code, value)))
    return o


class RetryCase(unittest.TestCase):
    PROTOCOL = "bridge"

    def setUp(self):
        self.o = make_unit(self.PROTOCOL)
        self.reactor = self.o.reactor

    # The generation the unit echoes for a logical one (None: legacy, which
    # answers in order).
    def wire(self, gen):
        if self.PROTOCOL == "legacy":
            return None
        return gen

    def reply(self, action, code, gen):
        self.o._apply_action_status(
            {"action": action, "code": code, "value": 0},
            wire_gen=self.wire(gen))

    def unload(self, gen=1):
        self.o.start_unload_spool(gen=gen)
        self.reply(UNLOADING, SUCCESS, gen)
        self.assertEqual(self.o.completions, [(SUCCESS, gen, None)])
        del self.o.completions[:]

    def load_sends(self):
        o = self.o
        if self.PROTOCOL == "bridge":
            return o.oams_load_spool3_cmd.sent
        if self.PROTOCOL == "v4":
            return o.oams_load_spool4_cmd.sent
        return o.oams_load_spool_cmd.sent

    def sent_load(self, gen):
        """The load command's arguments on this protocol."""
        if self.PROTOCOL == "bridge":
            return [BAY, gen, LENGTH]
        if self.PROTOCOL == "v4":
            return [BAY, gen]
        return [BAY]

    def cancel_sends(self):
        o = self.o
        if self.PROTOCOL == "v4":
            return o.oams_cancel4_cmd.sent
        return o.oams_load_spool_cancel_cmd.sent

    def busy_until_it_goes_through(self, gen, limit=20.0):
        """Answer every load Busy until a refusal reaches the reducer."""
        start = self.reactor.now
        sends = len(self.load_sends())
        self.reply(LOADING, BUSY, gen)
        while not self.o.completions:
            self.assertLess(self.reactor.now - start, limit, "never refused")
            self.reactor.advance(0.05)
            if len(self.load_sends()) > sends:
                sends = len(self.load_sends())
                self.reply(LOADING, BUSY, gen)
        return self.reactor.now - start


class BridgeRetryTests(RetryCase):
    """The *2 protocol a Cortex Bridge speaks: the unit echoes the
    generation, and the load is the atomic load with its length."""

    def test_busy_after_the_unload_is_sent_again_and_the_load_goes_on(self):
        o = self.o
        self.unload()
        o.start_load_spool(BAY, gen=2)
        self.reply(LOADING, BUSY, 2)
        # Held: the reducer still sees the load in flight, so the
        # toolchange waits in its feed step.
        self.assertEqual(o.completions, [])
        self.assertEqual(o.action_status, LOADING)
        self.assertEqual(o.load_retry_since(self.reactor.now),
                         self.reactor.now)
        self.reactor.advance(0.5)
        self.assertEqual(len(self.load_sends()), 1)
        self.reactor.advance(0.6)
        self.assertEqual(len(self.load_sends()), 2)
        self.assertEqual(o.load_retry_since(self.reactor.now + 5.0),
                         o._last_load["at"])
        self.reply(LOADING, BUSY, 2)
        self.reactor.advance(1.1)
        self.assertEqual(len(self.load_sends()), 3)
        # The same load each time: its bay, its generation, its length.
        self.assertEqual(self.load_sends(), [self.sent_load(2)] * 3)
        self.reply(LOADING, SUCCESS, 2)
        self.assertEqual(o.completions, [(SUCCESS, 2, None)])
        self.assertEqual(o.current_spool, BAY)
        self.assertIsNone(o.action_status)

    def test_busy_past_the_window_says_the_unit_did_not_settle(self):
        o = self.o
        self.unload()
        o.start_load_spool(BAY, gen=2)
        waited = self.busy_until_it_goes_through(2)
        self.assertLessEqual(waited, o.REWIND_BUSY_RETRY_S + 1.5)
        self.assertGreaterEqual(waited, o.REWIND_BUSY_RETRY_S - 1.0)
        self.assertTrue(9 <= len(self.load_sends()) <= 12,
                        self.load_sends())
        [(code, gen, reason)] = o.completions
        self.assertEqual((code, gen), (BUSY, 2))
        self.assertEqual(reason, "ams2 (AMS 2 Pro) was still busy after its"
                         " unload and did not settle within 10 s; retry")
        self.assertIsNone(o.action_status)
        self.reactor.advance(3.0)
        self.assertEqual(len(o.completions), 1)       # nothing more sent

    def test_a_cancel_while_waiting_sends_nothing_and_ends_the_load(self):
        o = self.o
        self.unload()
        o.start_load_spool(BAY, gen=2)
        self.reply(LOADING, BUSY, 2)
        o.load_spool_cancel()
        self.assertEqual(self.cancel_sends(), [])     # nothing runs on it
        self.reactor.advance(0.1)
        self.assertEqual(o.completions, [(CANCEL, 2, None)])
        self.assertIsNone(o.action_status)
        self.reactor.advance(3.0)
        self.assertEqual(len(self.load_sends()), 1)   # never sent again
        self.assertEqual(len(o.completions), 1)

    def test_the_runtimes_cancel_of_a_waiting_load_is_not_sent_either(self):
        o = self.o
        self.unload()
        o.start_load_spool(BAY, gen=2)
        self.reply(LOADING, BUSY, 2)
        o.cancel_operation(o.status_loading, 2)
        self.reactor.advance(0.1)
        self.assertEqual(self.cancel_sends(), [])
        self.assertEqual(o.completions, [(CANCEL, 2, None)])

    def test_a_cancel_once_the_load_runs_again_goes_to_the_unit(self):
        o = self.o
        self.unload()
        o.start_load_spool(BAY, gen=2)
        self.reply(LOADING, BUSY, 2)
        self.reactor.advance(1.1)                      # sent again
        o.load_spool_cancel()
        self.assertEqual(len(self.cancel_sends()), 1)
        self.assertEqual(o.completions, [])

    def test_busy_with_no_unload_before_goes_through_at_once(self):
        o = self.o
        o.start_load_spool(BAY, gen=2)
        self.reply(LOADING, BUSY, 2)
        self.assertEqual(o.completions, [(BUSY, 2, None)])
        self.assertIsNone(o.load_retry_since(self.reactor.now))
        self.reactor.advance(3.0)
        self.assertEqual(len(self.load_sends()), 1)

    def test_busy_long_after_the_unload_goes_through_at_once(self):
        o = self.o
        self.unload()
        self.reactor.advance(o.REWIND_BUSY_RETRY_S + 1.0)
        o.start_load_spool(BAY, gen=2)
        self.reply(LOADING, BUSY, 2)
        self.assertEqual(o.completions, [(BUSY, 2, None)])
        self.reactor.advance(3.0)
        self.assertEqual(len(self.load_sends()), 1)

    def test_a_failed_load_after_the_retry_is_not_held(self):
        o = self.o
        self.unload()
        o.start_load_spool(BAY, gen=2)
        self.reply(LOADING, BUSY, 2)
        self.reactor.advance(1.1)
        self.reply(LOADING, S.OAMS_OP_CODE_TIMEOUT, 2)
        self.assertEqual([c[:2] for c in o.completions],
                         [(S.OAMS_OP_CODE_TIMEOUT, 2)])

    def test_positioning_seen_keeps_the_positioning_text(self):
        o = self.o
        self.unload()
        o.start_load_spool(BAY, gen=2)
        # H16: a presence edge on another bay, which the unit positions.
        o._position_edge = (3, self.reactor.now)
        self.busy_until_it_goes_through(2)
        [(code, gen, reason)] = o.completions
        self.assertEqual((code, gen), (BUSY, 2))
        self.assertEqual(reason, "ams2 (AMS 2 Pro) is positioning a newly"
                         " inserted spool; retry when it finishes")

    def test_a_reset_drops_the_waiting_load(self):
        o = self.o
        o.set_led_error = lambda bay, value: None
        o.determine_current_spool = lambda: None
        self.unload()
        o.start_load_spool(BAY, gen=2)
        self.reply(LOADING, BUSY, 2)
        o.clear_errors()
        self.reactor.advance(3.0)
        self.assertEqual(len(self.load_sends()), 1)
        self.assertEqual(o.completions, [])

    def test_a_load_superseded_meanwhile_is_not_sent_again(self):
        o = self.o
        self.unload()
        o.start_load_spool(BAY, gen=2)
        self.reply(LOADING, BUSY, 2)
        o.start_unload_spool(gen=3)                    # another operation
        self.reactor.advance(3.0)
        self.assertEqual(len(self.load_sends()), 1)
        self.assertEqual(o.completions, [])

    def test_after_a_calibration_too(self):
        o = self.o
        o.reactor.now = 50.0
        o.action_status = oams.OAMS_STATUS_CALIBRATING
        if self.PROTOCOL == "v4":
            o._active_action, o._active_gen = o.status_calibrating, 1
        o._apply_action_status(
            {"action": o.status_calibrating, "code": SUCCESS,
             "value": 2400}, wire_gen=self.wire(1))
        del o.completions[:]
        o.start_load_spool(BAY, gen=2)
        self.busy_until_it_goes_through(2)
        self.assertIn("still busy after its calibration",
                      o.completions[0][2])

    def test_the_calibration_retry_is_as_long(self):
        self.assertEqual(OAMS.CALIBRATION_BUSY_RETRY_S,
                         OAMS.REWIND_BUSY_RETRY_S)


class LegacyRetryTests(BridgeRetryTests):
    """The OpenAMS 1 mainboard: replies come in order with no generation, so
    a held refusal takes the load's place in the queue, and the next send
    queues it again."""

    PROTOCOL = "legacy"

    def test_the_reply_queue_stays_in_step(self):
        o = self.o
        self.unload()
        o.start_load_spool(BAY, gen=2)
        self.assertEqual(list(o._gen_queue), [2])
        self.reply(LOADING, BUSY, 2)
        self.assertEqual(list(o._gen_queue), [])
        self.reactor.advance(1.1)
        self.assertEqual(list(o._gen_queue), [2])
        self.assertEqual(self.load_sends(), [self.sent_load(2)] * 2)
        self.reply(LOADING, SUCCESS, 2)
        self.assertEqual(o.completions, [(SUCCESS, 2, None)])
        self.assertEqual(list(o._gen_queue), [])

    def test_a_reply_owed_to_an_older_operation_is_not_held(self):
        o = self.o
        self.unload()
        o._gen_queue.append(7)                 # an abandoned operation's
        o.start_load_spool(BAY, gen=2)
        self.reply(LOADING, BUSY, 2)
        self.assertEqual(o.completions, [(BUSY, 7, None)])


class V4RetryTests(BridgeRetryTests):
    """Protocol v4 (no released firmware speaks it yet): the full
    generation is echoed and named by the cancel."""

    PROTOCOL = "v4"

    def test_the_driver_mirror_follows_the_held_load(self):
        o = self.o
        self.unload(gen=0x10001)
        o.start_load_spool(BAY, gen=0x10002)
        self.reply(LOADING, BUSY, 0x10002)
        self.assertEqual(o._active_gen, 0x10002)
        self.reactor.advance(1.1)
        self.assertEqual(self.load_sends(), [self.sent_load(0x10002)] * 2)
        self.reply(LOADING, SUCCESS, 0x10002)
        self.assertEqual(o.completions, [(SUCCESS, 0x10002, None)])
        self.assertIsNone(o._active_gen)
        self.assertEqual(o.current_spool, BAY)


if __name__ == "__main__":
    unittest.main()
