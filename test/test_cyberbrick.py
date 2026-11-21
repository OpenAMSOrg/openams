"""The CyberBrick Time-lapse Kit's Klipper side (src/cyberbrick.py).

CYBERBRICK_SHOT and M1004 S5 P<n> fire the kit through the Cortex Bridge
relay's control socket, a status object shows it, the kit service is turned
on at ready and after every bridge re-attach, moonraker-timelapse frames fire
it when cyberbrick_timelapse_frames is set, and with no relay, an older bridge
or lab firmware nothing is sent and nothing is logged.

    python3 -m unittest discover -s test -p 'test_cyberbrick.py' -v
"""

import importlib
import json
import logging
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _path in (os.path.join(_ROOT, "tools"), os.path.join(_ROOT, "src"),
              _ROOT, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

if "oamspkg" not in sys.modules:
    _pkg = types.ModuleType("oamspkg")
    _pkg.__path__ = [os.path.join(_ROOT, "src")]
    sys.modules["oamspkg"] = _pkg
CB = importlib.import_module("oamspkg.cyberbrick")
from cortex_bridge import kit as kit_protocol      # noqa: E402

RUNNING = {"state": "running", "bridge": {"image": "production"},
           "firmware": {"state": "known", "version": 40}}
KIT = {"present": True, "shots": 0}
STATUS_KEYS = {"available", "present", "shots", "kit_shots",
               "last_shot_time", "last_shot_result", "rate_limited"}


class ConfigError(Exception):
    pass


class FakeReactor(object):
    NOW = 0.0
    NEVER = 9e99

    def __init__(self):
        self.now = 100.0
        self.timers = []
        self.async_calls = []
        self.lock = threading.Lock()

    def monotonic(self):
        return self.now

    def register_timer(self, callback, waketime=NEVER):
        timer = [callback, waketime]
        self.timers.append(timer)
        return timer

    def register_async_callback(self, callback):
        with self.lock:
            self.async_calls.append(callback)

    def advance(self, seconds):
        end = self.now + seconds
        while True:
            due = [t for t in self.timers if t[1] <= end]
            if not due:
                break
            timer = min(due, key=lambda t: t[1])
            self.now = max(self.now, timer[1])
            timer[1] = timer[0](self.now)
        self.now = end
        self.drain()

    def drain(self):
        with self.lock:
            calls, self.async_calls = self.async_calls, []
        for call in calls:
            call(self.now)


class FakeGcode(object):
    """Klipper's register_command: None unregisters and returns the old."""

    def __init__(self):
        self.ready_gcode_handlers = {}
        self.gcode_help = {}

    def register_command(self, name, func, when_not_ready=False, desc=None):
        if func is None:
            old = self.ready_gcode_handlers.pop(name, None)
            self.gcode_help.pop(name, None)
            return old
        if name in self.ready_gcode_handlers:
            raise ConfigError("gcode command %s already registered" % name)
        self.ready_gcode_handlers[name] = func
        if desc is not None:
            self.gcode_help[name] = desc
        return None

    def run(self, line):
        words = line.split()
        params = dict(w.split("=", 1) if "=" in w else (w[0], w[1:])
                      for w in words[1:])
        gcmd = FakeGcmd(params)
        self.ready_gcode_handlers[words[0]](gcmd)
        return gcmd


class FakeGcmd(object):
    def __init__(self, params):
        self.params = params
        self.responses = []

    def get_int(self, name, default=None, minval=None, maxval=None):
        if name not in self.params:
            return default
        value = int(self.params[name])
        if (minval is not None and value < minval) or (
                maxval is not None and value > maxval):
            raise ConfigError("%s out of range" % name)
        return value

    def respond_info(self, text):
        self.responses.append(text)


class FakePrinter(object):
    def __init__(self, reactor, gcode):
        self.reactor = reactor
        self.gcode = gcode
        self.objects = {}
        self.handlers = {}

    def get_reactor(self):
        return self.reactor

    def add_object(self, name, obj):
        self.objects[name] = obj

    def lookup_object(self, name, default=None):
        if name == "gcode":
            return self.gcode
        return self.objects.get(name, default)

    def register_event_handler(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def send_event(self, event):
        for handler in self.handlers.get(event, []):
            handler()


class FakeConfig(object):
    error = ConfigError

    def __init__(self, **options):
        self.options = options

    def getboolean(self, name, default=None):
        return bool(self.options.get(name, default))

    def _bounded(self, name, default, cast, minval=None, maxval=None):
        value = cast(self.options.get(name, default))
        if (minval is not None and value < minval) or (
                maxval is not None and value > maxval):
            raise ConfigError("%s out of range" % name)
        return value

    def getint(self, name, default=None, minval=None, maxval=None):
        return self._bounded(name, default, int, minval, maxval)

    def getfloat(self, name, default=None, minval=None, maxval=None):
        return self._bounded(name, default, float, minval, maxval)


class FakeRelay(object):
    """The relay: a status file's content and scripted control answers."""

    def __init__(self, status=None, present=True):
        self.status = status
        self.present = present
        self.requests = []
        self.answers = []

    def available(self):
        return self.present

    def read_status(self):
        return self.status

    def request(self, request):
        self.requests.append(request)
        if self.answers:
            return self.answers.pop(0)
        if request.get("service"):
            return dict(KIT, event="kit", ok=True)
        return {"event": "kit", "ok": True, "shot": "queued"}


class InlineWorker(object):
    def submit(self, call, done):
        done(call())


class Rig(object):
    def __init__(self, status=None, present=True, **options):
        self.reactor = FakeReactor()
        self.gcode = FakeGcode()
        self.printer = FakePrinter(self.reactor, self.gcode)
        self.relay = FakeRelay(status, present)
        config = FakeConfig(**options)
        kit = CB.load(config, self.printer, self.gcode)
        if kit is not None:
            kit.client = self.relay
            kit.worker = InlineWorker()
        self.kit = kit

    def start(self):
        self.printer.send_event("klippy:connect")
        self.printer.send_event("klippy:ready")
        self.reactor.advance(0.1)

    def shots(self):
        return [r for r in self.relay.requests if "shot" in r]

    def services(self):
        return [r for r in self.relay.requests if "service" in r]


def running(kit=None):
    status = dict(RUNNING)
    status["kit"] = kit
    return status


class ShotTest(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(running(dict(KIT)))
        self.rig.start()

    def test_cyberbrick_shot_fires_through_the_relay(self):
        gcmd = self.rig.gcode.run("CYBERBRICK_SHOT")
        self.assertEqual(self.rig.shots(), [{"method": "kit", "shot": 1}])
        self.assertEqual(gcmd.responses, ["CyberBrick: shot sent (P=1)"])
        status = self.rig.kit.get_status()
        self.assertEqual(status["shots"], 1)
        self.assertEqual(status["last_shot_result"], "queued")
        self.assertIsNotNone(status["last_shot_time"])

    def test_p_and_quiet(self):
        gcmd = self.rig.gcode.run("CYBERBRICK_SHOT P=3 QUIET=1")
        self.assertEqual(self.rig.shots(), [{"method": "kit", "shot": 3}])
        self.assertEqual(gcmd.responses, [])

    def test_m1004_s5_fires_and_the_rest_do_nothing(self):
        run = self.rig.gcode.run
        self.assertEqual(run("M1004 S5 P2").responses, [])
        self.assertEqual(self.rig.shots(), [{"method": "kit", "shot": 2}])
        self.rig.reactor.advance(1.0)
        run("M1004 S5")                  # no P: nothing, as on the printer
        run("M1004 S1 P1")               # another extension-board command
        run("M1004 S7")
        self.assertEqual(len(self.rig.shots()), 1)

    def test_shots_are_rate_limited(self):
        run = self.rig.gcode.run
        run("CYBERBRICK_SHOT")
        self.rig.reactor.advance(0.2)
        gcmd = run("CYBERBRICK_SHOT")
        self.assertIn("too soon", gcmd.responses[0])
        self.assertEqual(len(self.rig.shots()), 1)
        status = self.rig.kit.get_status()
        self.assertEqual(status["rate_limited"], 1)
        self.assertEqual(status["last_shot_result"], "rate-limited")
        self.rig.reactor.advance(0.4)
        run("CYBERBRICK_SHOT")
        self.assertEqual(len(self.rig.shots()), 2)

    def test_a_refused_or_unanswered_shot_is_recorded_quietly(self):
        self.rig.relay.answers = [
            {"event": "kit", "ok": False, "shot": "refused"},
            {"event": "kit", "ok": False, "error": "no-answer"}]
        with self.assertNoLogs(level="WARNING"):
            self.rig.gcode.run("CYBERBRICK_SHOT QUIET=1")
            self.assertEqual(self.rig.kit.get_status()["last_shot_result"],
                             "refused")
            self.rig.reactor.advance(1.0)
            self.rig.gcode.run("CYBERBRICK_SHOT QUIET=1")
        status = self.rig.kit.get_status()
        self.assertEqual(status["last_shot_result"], "no-answer")
        self.assertEqual(status["shots"], 0)

    def test_the_status_object(self):
        self.assertIs(self.rig.printer.objects["cyberbrick"], self.rig.kit)
        status = self.rig.kit.get_status()
        self.assertEqual(set(status), STATUS_KEYS)
        self.assertTrue(status["available"])
        self.assertTrue(status["present"])
        self.assertEqual(status["kit_shots"], 0)
        json.dumps(status)

    def test_no_version_or_serial_ever_reaches_the_status(self):
        """Whatever else a relay's entry carries, the status object shows
        only its own fields."""
        self.rig.relay.status = running(dict(
            KIT, versions=[{"name": "x", "version": "1"}], serial="x",
            id="x", raw="x", shots=5))
        self.rig.reactor.advance(5.0)
        status = self.rig.kit.get_status()
        self.assertEqual(set(status), STATUS_KEYS)
        self.assertEqual(status["kit_shots"], 5)
        self.assertEqual(set(self.rig.kit.kit), {"present", "shots"})


class ServiceTest(unittest.TestCase):
    SERVICE = {"method": "kit", "service": True}

    def test_the_service_at_ready_once(self):
        rig = Rig(running(None))
        rig.start()
        self.assertEqual(rig.services(), [self.SERVICE])
        rig.relay.status = running(dict(KIT))
        rig.reactor.advance(10.0)
        self.assertEqual(len(rig.services()), 1)

    def test_again_after_every_bridge_reattach(self):
        rig = Rig(running(dict(KIT)))
        rig.start()
        self.assertEqual(len(rig.services()), 1)
        rig.kit.bridge_reattached()
        rig.reactor.advance(2.5)
        self.assertEqual(len(rig.services()), 2)
        rig.reactor.advance(60.0)
        self.assertEqual(len(rig.services()), 2)

    def test_a_relay_without_a_report_is_asked_no_faster_than_30_s(self):
        rig = Rig(running(None))
        rig.relay.answers = [{"event": "kit", "ok": False,
                              "error": "no-answer"}] * 10
        rig.start()
        rig.reactor.advance(29.0)
        self.assertEqual(len(rig.services()), 1)
        rig.reactor.advance(3.0)
        self.assertEqual(len(rig.services()), 2)

    def test_the_host_never_names_a_service_value(self):
        rig = Rig(running(None))
        rig.start()
        self.assertEqual(rig.services(), [self.SERVICE])


class OptionalTest(unittest.TestCase):
    def assert_inert(self, rig):
        with self.assertNoLogs(level="INFO"):
            rig.start()
            rig.reactor.advance(120.0)
            gcmd = rig.gcode.run("CYBERBRICK_SHOT")
            rig.gcode.run("M1004 S5 P1")
        self.assertEqual(rig.relay.requests, [])
        self.assertIn("not available", gcmd.responses[0])
        self.assertFalse(rig.kit.get_status()["available"])
        return gcmd.responses[0]

    def test_no_relay(self):
        text = self.assert_inert(Rig(None, present=False))
        self.assertIn("no Cortex Bridge relay", text)

    def test_a_bridge_before_40(self):
        status = running(None)
        status["firmware"] = {"state": "known", "version": 39}
        self.assertIn("firmware 40", self.assert_inert(Rig(status)))

    def test_lab_firmware(self):
        status = running(None)
        status["bridge"] = {"image": "lab"}
        self.assertIn("lab firmware", self.assert_inert(Rig(status)))

    def test_a_bridge_not_connected(self):
        status = running(None)
        status["state"] = "waiting"
        self.assertIn("not connected", self.assert_inert(Rig(status)))

    def test_turned_off(self):
        rig = Rig(running(dict(KIT)), cyberbrick=False)
        self.assertIsNone(rig.kit)
        self.assertNotIn("CYBERBRICK_SHOT", rig.gcode.ready_gcode_handlers)
        self.assertNotIn("cyberbrick", rig.printer.objects)

    def service_off(self, status, present=True):
        reactor = FakeReactor()
        printer = FakePrinter(reactor, FakeGcode())
        relay = FakeRelay(status, present)
        CB.ServiceOff(printer, client=relay, worker=InlineWorker())
        printer.send_event("klippy:ready")
        reactor.advance(60.0)
        return relay.requests

    def test_turned_off_sends_the_service_off_once(self):
        self.assertEqual(self.service_off(running(dict(KIT))),
                         [{"method": "kit", "service": False}])

    def test_turned_off_sends_nothing_where_nothing_serves_the_kit(self):
        older = running(None)
        older["firmware"] = {"state": "known", "version": 39}
        self.assertEqual(self.service_off(older), [])
        self.assertEqual(self.service_off(None, present=False), [])


class PresenceTest(unittest.TestCase):
    def test_coming_and_going_logged_once_each_without_the_serial(self):
        rig = Rig(running(dict(KIT, present=False)))
        with self.assertLogs(level="INFO") as logs:
            rig.start()
            rig.relay.status = running(dict(KIT))
            rig.reactor.advance(10.0)                # comes
            rig.relay.status = running(dict(KIT, present=False))
            rig.reactor.advance(10.0)                # goes
            rig.relay.present = False                # the relay stops
            rig.reactor.advance(10.0)
        text = "\n".join(logs.output)
        self.assertEqual(text.count("CyberBrick kit is connected"), 1)
        self.assertEqual(text.count("CyberBrick kit is gone"), 1)

    def test_the_relay_going_away_while_present_says_gone_once(self):
        rig = Rig(running(dict(KIT)))
        rig.start()
        with self.assertLogs(level="INFO") as logs:
            rig.relay.present = False
            rig.reactor.advance(30.0)
        self.assertEqual(sum("gone" in line for line in logs.output), 1)
        self.assertFalse(rig.kit.get_status()["present"])

    def test_presence_follows_the_relay(self):
        rig = Rig(running(dict(KIT)))
        rig.start()
        rig.reactor.advance(60.0)
        self.assertTrue(rig.kit.get_status()["present"])


class M1004OwnerTest(unittest.TestCase):
    def test_a_config_m1004_is_left_alone(self):
        reactor = FakeReactor()
        gcode = FakeGcode()
        printer = FakePrinter(reactor, gcode)
        mine = []
        gcode.register_command("M1004", lambda gcmd: mine.append(gcmd))
        kit = CB.load(FakeConfig(), printer, gcode)
        kit.client = FakeRelay(running(dict(KIT)))
        kit.worker = InlineWorker()
        printer.send_event("klippy:connect")
        gcode.run("M1004 S5 P1")
        self.assertEqual(len(mine), 1)
        self.assertFalse(kit.m1004)
        self.assertEqual(kit.client.requests, [])


class TimelapseHookTest(unittest.TestCase):
    def rig_with_timelapse(self, **options):
        rig = Rig(running(dict(KIT)), **options)
        frames = []
        rig.gcode.register_command("_TIMELAPSE_NEW_FRAME",
                                   lambda gcmd: frames.append(gcmd),
                                   desc="moonraker-timelapse frame")
        rig.start()
        return rig, frames

    def test_frames_fire_the_kit_after_the_macro(self):
        rig, frames = self.rig_with_timelapse(
            cyberbrick_timelapse_frames=True)
        rig.gcode.run("_TIMELAPSE_NEW_FRAME HYPERLAPSE=0")
        self.assertEqual(len(frames), 1)
        self.assertEqual(rig.shots(), [{"method": "kit", "shot": 1}])
        self.assertTrue(rig.kit.timelapse_hooked)
        self.assertEqual(rig.gcode.gcode_help["_TIMELAPSE_NEW_FRAME"],
                         "moonraker-timelapse frame")

    def test_off_by_default(self):
        rig, frames = self.rig_with_timelapse()
        rig.gcode.run("_TIMELAPSE_NEW_FRAME")
        self.assertEqual(len(frames), 1)
        self.assertEqual(rig.shots(), [])

    def test_without_moonraker_timelapse_nothing_breaks(self):
        rig = Rig(running(dict(KIT)), cyberbrick_timelapse_frames=True)
        rig.start()
        self.assertFalse(rig.kit.timelapse_hooked)
        self.assertNotIn("_TIMELAPSE_NEW_FRAME",
                         rig.gcode.ready_gcode_handlers)


class RealRelayTest(unittest.TestCase):
    """RelayClient and ThreadWorker against a relay's socket and status
    file, the way the relay serves them."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cyberbrick-")
        self.addCleanup(shutil.rmtree, self.dir)
        with open(os.path.join(self.dir, CB.STATUS_FILE), "w") as handle:
            json.dump(running(dict(KIT)), handle)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(os.path.join(self.dir, CB.CONTROL_SOCKET))
        self.server.listen(4)
        self.addCleanup(self.server.close)
        self.seen = []
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            with connection:
                line = connection.makefile("rb").readline()
                request = json.loads(line.decode("utf-8"))
                self.seen.append(request)
                time.sleep(0.05)
                connection.sendall((json.dumps(
                    {"event": "kit", "ok": True, "shot": "queued"})
                    + "\n").encode("utf-8"))

    def test_a_shot_and_the_status_through_the_worker(self):
        reactor = FakeReactor()
        client = CB.RelayClient(self.dir)
        worker = CB.ThreadWorker(reactor)
        results = []
        self.assertTrue(client.available())
        worker.submit(client.read_status, results.append)
        worker.submit(lambda: client.request({"method": "kit", "shot": 1}),
                      results.append)
        deadline = time.time() + 5.0
        while len(results) < 2 and time.time() < deadline:
            reactor.drain()
            time.sleep(0.01)
        self.assertTrue(results[0]["kit"]["present"])
        self.assertEqual(results[1]["shot"], "queued")
        self.assertEqual(self.seen, [{"method": "kit", "shot": 1}])

    def test_a_relay_that_is_not_there(self):
        client = CB.RelayClient(os.path.join(self.dir, "nowhere"))
        self.assertFalse(client.available())
        self.assertIsNone(client.read_status())
        reply = client.request({"method": "kit", "shot": 1})
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "relay-unreachable")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    unittest.main()
