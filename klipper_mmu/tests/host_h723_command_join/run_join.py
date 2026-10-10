#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Send the pinned Klipper host wrappers into the BoxTurtle H723 fixture."""
import collections
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location(
    'klipper_host_wire', ROOT / 'tests/klipper_host_wire_pinned/run_tests.py')
wire = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wire)
from host.calibration import encode_calibration
from host.command_transport import send_heartbeat, send_operation
from host.event_delivery import EventDelivery, send_pending_ack
from host.event_reader import peek_event
from host.protocol import Identity
from host.physical_state_reader import read_physical_state
from host.openams_sensor_projection import project_sensor_presence
from host.wire import EventType, Fault, Stage
from host import KlipperMMU

INSTANCE = 0x123456789abcdef0
SESSION = 0xabcdef01
MODE = sys.argv[2] if len(sys.argv) == 3 else 'diff'
if MODE not in ('diff', 'switched', 'fps'):
    raise SystemExit('mode must be diff, switched or fps')
SWITCHED = MODE == 'switched'
FPS = MODE == 'fps'  # The analog image as built: FPS arrival, no toolhead switch.
STEPS_PER_MM = 727272 / 1000  # Pinned AFC BoxTurtle reference, not fitted calibration.
HUB_STEPS = round(155 * STEPS_PER_MM)
BOWDEN_STEPS = round(1725 * STEPS_PER_MM)
TOOLHEAD_CLEAR_STEPS = round(20 * STEPS_PER_MM)  # Synthetic switch edge.
HUB_CLEAR_STEPS = round(65 * STEPS_PER_MM)
CALIBRATION = {
    'channel_map': 'a_low_b_high', 'vref_mv': 3300,
    'min_valid_mv': 100, 'max_valid_mv': 3200,
    'path_tolerance_counts': 80,
    'points': [
        {'a_raw': 100, 'b_raw': 2100, 'position_permille': 0},
        {'a_raw': 1900, 'b_raw': 4000, 'position_permille': 1000},
    ],
}
# Raw DiffA/DiffB pairs on CALIBRATION's line. REST projects just before its
# first point, so it reads 0 (uncompressed) while above min_valid_mv; INVALID
# exceeds max_valid_mv.
REST, HELD, COMPRESSED, INVALID = (150, 2050), (1000, 3050), (1540, 3620), (4095, 4095)


class NativeH723Serial(wire.WireSerial):
    ORDER = {
        'km_transport_query': ('challenge',),
        'km_diagnostic_query': ('challenge',),
        'km_heartbeat': ('instance_hi', 'instance_lo', 'session'),
        'km_start': ('instance_hi', 'instance_lo', 'client', 'session',
                     'request', 'operation', 'lane', 'direction'),
        'km_event_peek': ('instance_hi', 'instance_lo'),
        'km_event_ack': ('instance_hi', 'instance_lo', 'client', 'session',
                         'request', 'operation', 'lane', 'seq'),
        'km_snapshot_part': ('instance_hi', 'instance_lo', 'query', 'part'),
        'km_sensor_part': ('instance_hi', 'instance_lo', 'query', 'part'),
        'km_diag_part': ('instance_hi', 'instance_lo', 'query', 'part'),
        'km_path_reconcile': ('instance_hi', 'instance_lo', 'query',
                              'occupied', 'lane'),
        'km_load_attempts_query': ('instance_hi', 'instance_lo', 'client',
                                   'session', 'request', 'operation', 'lane'),
    }

    def __init__(self, reactor, process):
        super().__init__(reactor)
        self.process = process

    def raw_send_wait_ack(self, frame, *unused):
        tag, _ = self.parser.msgid_parser.parse(frame, 0)
        command = self.parser.messages_by_id[tag]
        params, end = command.parse(frame, 0)
        assert end == len(frame) and command.name in self.ORDER, command.name
        self.frames.append((command.name, params, len(frame)))
        args = ' '.join(str(params[key]) for key in self.ORDER[command.name])
        self.process.stdin.write(command.name + ' ' + args + '\n')
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        assert line, 'native H723 fixture exited: ' + self.process.stderr.read()
        row = json.loads(line)
        response = self.parser.messages_by_name[row['format'].split()[0]]
        encoded = response.encode(row['values'])
        decoded, end = response.parse(encoded, 0)
        assert end == len(encoded)
        callback = self.callbacks.get((response.name, None))
        assert callback is not None, response.name
        callback(decoded)


class NativeH723MCU(wire.fixture.MCU):
    def __init__(self, reactor, process):
        super().__init__(reactor)
        self.serial = NativeH723Serial(reactor, process)
        self.config_commands = []

    def add_config_cmd(self, command, is_init=False):
        self.config_commands.append(command)

    def create_oid(self):
        return len(self.config_commands) + 1

    def get_constants(self):
        return wire.WIRE_DICTIONARY['config']

    def get_enumerations(self):
        return wire.WIRE_DICTIONARY.get('enumerations', {})

    def register_config_callback(self, callback):
        self.config_callback = callback

    def lookup_query_command(self, command, response):
        mcu = self

        class Query:
            def send(self, args):
                replies = []
                subscription = mcu.register_serial_response(
                    replies.append, response)
                try:
                    mcu.lookup_command(command).send(args)
                finally:
                    subscription.unregister()
                if len(replies) != 1:
                    raise ValueError('Expected one native MCU query reply')
                return replies[0]

        return Query()


class ObserverPrinter:
    command_error = ValueError
    config_error = ValueError

    def __init__(self, reactor, mcu):
        self.reactor, self.mcu = reactor, mcu
        self.events = {}
        self.objects = {}
        self.extruder = types.SimpleNamespace(last_position=0.0)
        self.gcode_commands = {}
        self.gcode_scripts = []
        self.gcode_script_hook = None
        self.console = []
        self.gcode = types.SimpleNamespace(
            respond_info=self.console.append,
            register_mux_command=lambda name, key, value, handler, **kw:
                self._register_gcode_command(name, handler),
            register_command=self._register_gcode_command,
            run_script_from_command=self._run_gcode_script,
            run_script=self._run_gcode_script)
        self.webhooks = types.SimpleNamespace(
            register_endpoint=lambda *args, **kw: None)

    def _register_gcode_command(self, name, handler, **kwargs):
        self.gcode_commands[name] = handler

    def _run_gcode_script(self, script):
        self.gcode_scripts.append(script)
        if self.gcode_script_hook is not None:
            self.gcode_script_hook(script)

    def get_reactor(self):
        return self.reactor

    def lookup_objects(self, name=None, module=None):
        return self.objects.get(module or name, [])

    def lookup_object(self, name, default=None):
        if name == 'gcode':
            return self.gcode
        if name == 'webhooks':
            return self.webhooks
        if name == 'idle_timeout':
            return None
        if name == 'extruder':
            return self.extruder
        for entries in self.objects.values():
            for full, obj in entries:
                if full == name:
                    return obj
        raise AssertionError('Unexpected Klipper object: ' + name)

    def load_object(self, config, name):
        assert name in ('fps fps0', 'klipper_mmu box', 'filament_group feed')

    def register_event_handler(self, name, callback):
        self.events.setdefault(name, []).append(callback)


class ManagedReactor(wire.fixture.Reactor):
    NOW = 0.0
    NEVER = 1e30

    def __init__(self):
        super().__init__()
        self.timers = {}
        self.pause_callback = None
        self.pump = None  # Advances the fixture while a G-code waiter blocks.

    def completion(self):
        reactor = self

        class Completion(wire.fixture.Completion):
            def wait(self, deadline=None):
                while deadline is None and self.value is None and reactor.pump:
                    reactor.pump()
                return self.value if deadline is None else super().wait(deadline)

        return Completion(self)

    def register_timer(self, callback, when):
        self.timers[callback] = when
        return callback

    def unregister_timer(self, callback):
        self.timers.pop(callback, None)

    def pause(self, deadline):
        self.now = min(deadline, self.now + .01)
        if self.pause_callback is not None:
            self.pause_callback()


def managed_observer(printer, **files):
    """KlipperMMU for [klipper_mmu box] with openams_motion on the fixture MCU."""
    lease_ms = wire.WIRE_DICTIONARY['config'].get('KLIPPER_MMU_HOST_LEASE_MS')
    if type(lease_ms) is not int or not 2 <= lease_ms <= 60000:
        raise RuntimeError('saved H723 dictionary must publish a valid '
                           'KLIPPER_MMU_HOST_LEASE_MS')
    options = dict(openams_idx='0', openams_fps='fps0', openams_family='boxturtle',
                   openams_bays='4', openams_path_length_mm='1725',
                   openams_host_watchdog_ms=str(lease_ms),
                   openams_lease_max_age_ms=str(min(500, lease_ms - 1)), **files)

    class Config:
        def get_printer(self):
            return printer

        def get_name(self):
            return 'klipper_mmu box'

        def get(self, name, default=None):
            return options.get(name, default)

        def getboolean(self, name, default=False):
            return name in ('diagnostic_startup', 'openams_motion',
                            'path_reconciliation')

        def getfloat(self, name, default, **limits):
            return default

    with patch.dict(sys.modules, {'mcu': types.SimpleNamespace(
            get_printer_mcu=lambda p, name: p.mcu)}):
        return KlipperMMU(Config())


def actual_openams_manager(printer, observer):
    source = os.environ.get('OPENAMS_SOURCE')
    if not source:
        return None, None
    src = Path(source).resolve() / 'src'
    if not all((src / name).is_file() for name in
               ('oams_state.py', 'oams_runtime.py', 'oams_manager.py')):
        raise ValueError('OPENAMS_SOURCE must contain the OpenAMS manager source')
    package = types.ModuleType('ams_family_join_source')
    package.__path__ = [str(src)]
    sys.modules[package.__name__] = package
    state = importlib.import_module(package.__name__ + '.oams_state')
    manager_class = importlib.import_module(package.__name__ + '.oams_manager').OAMSManager
    fps_class = importlib.import_module(package.__name__ + '.fps').FPS

    class Config:
        error = ValueError

        def __init__(self, name):
            self.name = name

        def get_printer(self):
            return printer

        def get_name(self):
            return self.name

        def get(self, name, default=None):
            if self.name == 'fps fps0' and name == 'source_oams':
                return 'klipper_mmu box'
            return default

        def getint(self, name, default=None):
            return default

        def getfloat(self, name, default, **kwargs):
            return default

        def getboolean(self, name, default=False):
            return default

        def get_prefix_sections(self, prefix):
            assert prefix == ''
            return [types.SimpleNamespace(get_name=lambda name=name: name)
                    for name in ('fps fps0', 'klipper_mmu box',
                                 'filament_group feed')]

    fps = fps_class(Config('fps fps0'))
    printer.objects = {
        'fps': [('fps fps0', fps)],
        'klipper_mmu': [('klipper_mmu box', observer)],
        'filament_group': [('filament_group feed', types.SimpleNamespace(
            bay_specs=(('box', 0),)))],
    }
    return manager_class(Config('oams_manager')), state


# Stand-ins for the OpenAMS actions the FPS timeline requests without OPENAMS_SOURCE.
S = types.SimpleNamespace(
    LoadBay=collections.namedtuple('LoadBay', 'fps unit'),
    Unload=collections.namedtuple('Unload', 'fps'),
    Calibrate=collections.namedtuple('Calibrate', 'fps oams_idx bay kind'))


class StubRuntime:
    """OpenAMS runtime stand-in: starts the unit's operation, settles its waiter,
    and refreshes the unit periodically as OpenAMS's monitor does."""

    def __init__(self, reactor, unit):
        self.reactor, self.unit = reactor, unit
        self.waiter, self.gen, self.lane = None, 0, None

    def request(self, fps, action):
        self.waiter, self.gen = self.reactor.completion(), self.gen + 1
        if isinstance(action, S.LoadBay):
            self.lane = action.unit[1]
            self.unit.start_load_spool(self.lane, gen=self.gen)
        elif isinstance(action, S.Unload):
            self.unit.start_unload_spool(gen=self.gen)
        else:
            self.unit.start_calibrate(action.kind, action.bay, gen=self.gen)
        return self.waiter

    def accept_terminal(self, fps, code, value, gen):
        if self.waiter is None or gen != self.gen:
            return False
        self.waiter.complete(types.SimpleNamespace(ok=code == 0, value=value))
        self.waiter = None
        return True

    def get_state(self):
        return types.SimpleNamespace(lanes={'fps0': types.SimpleNamespace(
            unit=(0, self.lane), following=False)})

    def monitor(self, eventtime):
        self.unit.refresh()
        return eventtime + 1.

    def dispatch(self, action):
        pass

    accept_reload_terminal = accept_retired_terminal = lambda self, *args: False
    accept_verified_follower = accept_firmware_fault = lambda self, *args, **kwargs: True


class GCmd:
    def __init__(self, **params):
        self.params, self.responses = params, []

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_int(self, name, default=None, minval=None, maxval=None):
        value = int(self.params.get(name, default))
        if (minval is not None and value < minval
                or maxval is not None and value > maxval):
            raise ValueError('%s out of range' % name)
        return value

    def respond_info(self, message):
        self.responses.append(message)


class FpsRig:
    """One native MCU process joined to fresh host objects over a filament tip.

    The tip moves with the feeder model position from its bay. HUB asserts
    past the reference lane-to-HUB distance. The FPS compresses only while
    LOAD or CALIBRATE feeds past the reference Bowden, is held at mid-range
    while following, and reads 0 otherwise (slack, or uncoupled in rewind).
    A previous rig restarts only the host: its MCU process and filament stay.
    """

    def __init__(self, test, files, previous=None):
        self.test, self.trace, self.invalid = test, [], False
        if previous is None:
            self.last, self.buffer, self.lane, self.origin, self.tips = None, None, 0, 0, {}
            self.process = subprocess.Popen(
                [sys.argv[1], 'host_session', '0'], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=dict(os.environ, ASAN_OPTIONS='detect_leaks=0:halt_on_error=1'))
        else:
            for name in ('process', 'last', 'buffer', 'lane', 'origin', 'tips', 'bounds',
                        'calibrated_steps'):
                setattr(self, name, getattr(previous, name))
        self.reactor = reactor = ManagedReactor()
        reactor.pump = self.advance
        self.mcu = NativeH723MCU(reactor, self.process)
        self.printer = ObserverPrinter(reactor, self.mcu)
        self.observer = managed_observer(self.printer, **files)
        self.unit = self.observer._openams_unit
        self.manager, self.state = actual_openams_manager(self.printer, self.observer)
        if self.manager is None:
            self.runtime, self.state = StubRuntime(reactor, self.unit), S
            self.unit.bind_runtime(self.runtime, 'fps0')
            reactor.register_timer(self.runtime.monitor, reactor.NOW)
        else:
            self.runtime = self.manager.runtime
        # A powered-on MCU gets Klipper's configuration, then its init.
        self.mcu.config_callback()
        if previous is None:
            for command in self.mcu.config_commands:
                name, *fields = command.split()
                values = dict(field.split('=') for field in fields)
                if name == 'config_km_fps_steps':
                    self.fixture('config_km_fps_steps %(oid)s %(steps)s' % values)
                elif name == 'config_km_diff_calibration':
                    calibration = values['data']
            started = self.fixture('startup ' + calibration)
            self.bounds = (started['fps_min_steps'], started['fps_max_steps'])
            self.calibrated_steps = started['calibrated_steps']
        for callback in self.printer.events['klippy:connect']:
            callback()
        if self.manager is not None:
            self.manager.handle_ready()
        self.advance(10)
        test.assertTrue(self.unit.motion_available)

    def fixture(self, command):
        self.process.stdin.write(command + '\n')
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        assert line, 'native H723 fixture exited: ' + self.process.stderr.read()
        return json.loads(line)

    def tip(self):
        return self.last['model_position'] - self.origin

    def advance(self, ms=200):
        self.test.assertLess(self.reactor.now, 3600., 'FPS timeline exceeded its bound')
        tip = self.tip() if self.last else 0
        stage = self.last['stage'] if self.last else Stage.STOPPED
        buffer = (INVALID if self.invalid
                  else COMPRESSED if (stage == Stage.LOAD_FEED_TO_BUFFER
                                      and tip >= HUB_STEPS + BOWDEN_STEPS)
                  else HELD if stage == Stage.FOLLOW else REST)
        if buffer != self.buffer:
            self.fixture('fixture_buffer %d %d' % buffer)
            self.buffer = buffer
        self.last = self.fixture('fixture_advance %d %d 0' % (ms, tip >= HUB_STEPS))
        self.trace.append(self.last)
        reactor = self.reactor
        reactor.now += ms / 1000.
        while reactor.pending:
            reactor.pending.pop(0)(reactor.now)
        for callback, when in list(reactor.timers.items()):
            if when <= reactor.now and callback in reactor.timers:
                reactor.timers[callback] = callback(reactor.now)
        return self.last

    def select(self, lane):
        self.tips[self.lane] = self.tip()
        self.fixture('fixture_lane %d' % lane)
        self.lane, self.origin = lane, self.last['model_position'] - self.tips.get(lane, 0)
        self.advance()  # Debounce the seated spool's BAY and LOAD switches.

    def run(self, action):
        """Complete one OpenAMS request; its terminal is consumed and ACKed."""
        start, self.trace = self.last, []
        waiter = self.runtime.request('fps0', action)
        self.test.assertFalse(waiter.test(), waiter.test() and waiter.wait())
        result = waiter.wait()
        self.test.assertTrue(result.ok, result)
        self.test.assertIsNone(peek_event(self.mcu, self.reactor, INSTANCE, lambda: True))
        self.test.assertTrue(all(e['other_lanes_still'] for e in self.trace))
        return start

    def load(self, lane):
        start = self.run(self.state.LoadBay('fps0', (0, lane)))
        # Arrival only after compression at the reference Bowden, then FOLLOW.
        self.test.assertEqual(self.last['stage'], Stage.FOLLOW)
        self.test.assertTrue(HUB_STEPS + BOWDEN_STEPS <= self.tip()
                             < HUB_STEPS + BOWDEN_STEPS + round(20 * STEPS_PER_MM))
        self.test.assertGreaterEqual(self.last['active_steps'] - start['active_steps'],
                                     self.tip() - (start['model_position'] - self.origin))
        # The host read the arrival record over the image's own query.
        self.test.assertRegex(self.observer.last_load['attempts'][0],
                              r'^attempt 1, HUB to extruder: arrived at 17\d\d mm past HUB')
        for _ in range(5):  # FOLLOW holds the buffer at mid-range.
            self.test.assertEqual(self.advance()['stage'], Stage.FOLLOW)
            self.test.assertEqual(self.last['buffer'], 500)
        self.test.assertTrue(self.unit.motion_available)

    def unload(self):
        self.run(self.state.Unload('fps0'))
        self.test.assertEqual(self.last['stage'], Stage.STOPPED)
        # The buffer reads 0 through the rewind; the tip clears HUB by the
        # configured post-HUB distance.
        self.test.assertTrue(all(e['buffer'] == 0 for e in self.trace[1:]
                                 if e['stage'] != Stage.STOPPED))
        self.test.assertLessEqual(self.tip(), HUB_STEPS - HUB_CLEAR_STEPS)

    def close(self):
        self.process.stdin.close()
        self.test.assertEqual(self.process.wait(timeout=5), 0, self.process.stderr.read())

    def kill(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if not stream.closed:
                stream.close()


class HostH723JoinTest(unittest.TestCase):
    @unittest.skipUnless(FPS, 'run with MODE=fps over the analog actuation image')
    def test_fps_arrival_timeline(self):
        with tempfile.TemporaryDirectory() as scratch:
            files = dict(diff_calibration_file=os.path.join(scratch, 'diff.json'),
                         bowden_estimate_file=os.path.join(scratch, 'bowden.json'))
            Path(files['diff_calibration_file']).write_text(json.dumps(CALIBRATION))
            constants = wire.WIRE_DICTIONARY['config']
            rig = FpsRig(self, files)
            try:
                # The image's outer travel limits; they never narrow, with or
                # without a calibrated distance.
                self.assertEqual(rig.bounds, (constants['KLIPPER_MMU_FPS_MIN_STEPS'],
                                              constants['KLIPPER_MMU_FPS_MAX_STEPS']))
                self.assertEqual(rig.calibrated_steps, 0)  # Nothing restored yet.
                # LOAD is refused (KM_REQ_UNAVAILABLE) until this session's own
                # CALIBRATE measures a HUB-to-arrival distance; calibrate first.
                command = GCmd(MMU='box', BAY='0')
                rig.printer.gcode_commands['KLIPPER_MMU_CALIBRATE'](command)
                steps = json.loads(Path(files['bowden_estimate_file']).read_text())[
                    'estimate']['steps']
                # HUB-to-arrival pulses: the reference Bowden within sampling.
                self.assertLess(abs(steps - BOWDEN_STEPS), round(20 * STEPS_PER_MM))
                self.assertIn('%d feeder microsteps' % steps, command.responses[0])
                self.assertLess(rig.tip(), HUB_STEPS)
                # Adopted at once: this session's LOAD aims at it with no restart.
                rig.load(0)
                rig.unload()
                rig.select(1)  # Tool change.
                rig.load(1)
                rig.unload()
                rig.select(0)
                rig.close()
            finally:
                rig.kill()
            # Restart: the stored estimate is restored as the calibrated
            # distance, and LOAD uses it right away; the outer travel limits
            # still never narrow.
            rig = FpsRig(self, files)
            try:
                self.assertEqual(rig.bounds, (constants['KLIPPER_MMU_FPS_MIN_STEPS'],
                                              constants['KLIPPER_MMU_FPS_MAX_STEPS']))
                self.assertEqual(rig.calibrated_steps, steps)
                rig.load(0)
                session = rig.observer._prepared_host_session.requests.session
                rig.invalid = True  # The DiffA/DiffB pair fails during FOLLOW.
                while rig.observer.retained_fault_diagnostic is None:
                    rig.advance()
                fault = rig.observer.retained_fault_diagnostic
                self.assertEqual((fault['event_type'], fault['value'], fault['lane']),
                                 (EventType.FOLLOW_FAULT, Fault.BUFFER_SIGNAL, 0))
                self.assertFalse(rig.unit.motion_available)
                self.assertTrue(rig.fixture('fixture_outputs')['outputs_off'])
                commands = rig.printer.gcode_commands
                commands['KLIPPER_MMU_ACK_FAULT'](GCmd(MMU='box'))
                self.assertIsNone(peek_event(rig.mcu, rig.reactor, INSTANCE, lambda: True))
                # The post-operation diagnostic explains the fault in plain words.
                self.assertTrue(any('FPS buffer signal failed' in line
                                    for line in rig.printer.console))
                rig.invalid = False  # Pair repaired; the filament is still loaded.
                rig.advance()
                adopt = GCmd(MMU='box', ACTION='ADOPT', LANE='0')
                commands['KLIPPER_MMU_RECONCILE_PATH'](adopt)
                self.assertIn('fault cleared; path adopted by lane 0', adopt.responses[0])
                commands['KLIPPER_MMU_RECOVER_SESSION'](GCmd(MMU='box'))
                while not rig.unit.motion_available:
                    rig.advance()
                self.assertEqual(rig.observer._prepared_host_session.requests.session,
                                 session + 1)
                if rig.manager is not None:
                    if callable(getattr(rig.runtime, 'accept_firmware_fault_cleared',
                                        None)):
                        # The OpenAMS source has the verified clear hook: the
                        # RECONCILE_PATH ADOPT above already used it, so the
                        # lane is usable again without restarting the host.
                        self.assertIn('OpenAMS lane cleared', adopt.responses[0])
                        self.assertNotIn(0, rig.runtime.get_state().firmware_faults)
                    else:
                        # Older OpenAMS latches the lane on an accepted firmware
                        # fault and has no verified clear, so restart the host
                        # (not the MCU); its connect prepares the adopted lane's
                        # session.
                        self.assertIn('restart Klipper', adopt.responses[0])
                        self.assertIn(0, rig.runtime.get_state().firmware_faults)
                        self.assertIn('PAUSE', rig.printer.gcode_scripts)
                        rig = FpsRig(self, files, previous=rig)
                rig.unload()
                self.assertTrue(rig.fixture('fixture_outputs')['outputs_off'])
                rig.close()
            finally:
                rig.kill()

    @unittest.skipIf(FPS, 'drives the toolhead switch, which FPS mode lacks')
    def test_managed_load_reaches_toolhead_from_cold_staged_lane(self):
        self._run_managed_load_case(exercise_gcode_handoff=False)

    @unittest.skipIf(FPS, 'drives the toolhead switch, which FPS mode lacks')
    def test_registered_unload_waits_for_verified_reverse_follow(self):
        if not os.environ.get('OPENAMS_SOURCE'):
            self.skipTest('set OPENAMS_SOURCE to run the OpenAMS G-code route')
        self._run_managed_load_case(exercise_gcode_handoff=True)

    def _run_managed_load_case(self, exercise_gcode_handoff):
        self.assertIn(len(sys.argv), (2, 3), 'pass the built host_h723_join binary and optional mode')
        process = subprocess.Popen([sys.argv[1], 'host_session', '0'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True,
                                   env=dict(os.environ, ASAN_OPTIONS='detect_leaks=0:halt_on_error=1'))
        try:
            self._startup(process)
            reactor = ManagedReactor()
            mcu = NativeH723MCU(reactor, process)
            printer = ObserverPrinter(reactor, mcu)
            observer = managed_observer(printer)
            unit = observer._openams_unit
            accepted = []
            manager, state = actual_openams_manager(printer, observer)
            if manager is None:
                runtime = types.SimpleNamespace(
                    dispatch=lambda action: None,
                    accept_terminal=lambda *args: accepted.append(args) or True,
                    accept_reload_terminal=lambda *args: True,
                    accept_verified_follower=lambda *args: True,
                    accept_retired_terminal=lambda *args: False,
                    accept_firmware_fault=lambda *args, **kwargs: False)
                unit.bind_runtime(runtime, 'fps0')
            else:
                runtime = manager.runtime
            for callback in printer.events['klippy:connect']:
                callback()
            if manager is None:
                self.assertTrue(unit.refresh())
            else:
                manager.handle_ready()
                manager._monitor(reactor.now)
                self.assertTrue(manager.ready)
                self.assertTrue(runtime.get_state().fw_owns_liveness)
                self.assertEqual(manager.fpss['fps0'].source_oams, unit)
                # Exercise the real registered manual command entry point and
                # its production validation path. An unknown group must fail
                # before a request or firmware command is issued.
                self.assertIn('OAMSM_LOAD_TO_TOOLHEAD', printer.gcode_commands)

                class GCodeCommand:
                    def get(self, name, default=None):
                        return 'missing_fixture_group' if name == 'GROUP' else default

                    def error(self, message):
                        return ValueError(message)

                    def respond_info(self, message):
                        raise AssertionError('unexpected command response: ' + message)

                with self.assertRaisesRegex(ValueError, 'does not exist'):
                    printer.gcode_commands['OAMSM_LOAD_TO_TOOLHEAD'](GCodeCommand())
                self.assertEqual(printer.gcode_scripts, [])
                self.assertEqual(runtime.get_state().lanes['fps0'].op,
                                 state.OP_UNLOADED)
            self.assertEqual(unit.load_present, (True, False, False, False))
            self.assertEqual(unit.hub_hes_value, (False,) * 4)
            self.assertTrue(0.0 <= unit.fps_value <= 1.0)
            prepared = observer._prepared_host_session
            self.assertEqual(prepared.requests.instance, INSTANCE)
            self.assertEqual(prepared.requests.session, 1)
            self.assertTrue(unit.motion_available)
            next_monitor = [reactor.now + manager.monitor_interval] if manager else None

            def service_managed():
                self.assertNotEqual(unit._service(reactor.now), reactor.NEVER)
                if next_monitor is not None and reactor.now >= next_monitor[0]:
                    next_monitor[0] = manager._monitor(reactor.now)
                    self.assertTrue(manager.ready)
                    self.assertEqual(manager.fpss['fps0'].get_value(), unit.fps_value)

            if state is None:
                started = unit.start_load_spool(0, gen=1)
                self.assertEqual(started.response.admission, 0)
                self.assertEqual(started.identity.lane, 0)
            else:
                load_waiter = runtime.request('fps0', state.Load('fps0', 'feed'))
                self.assertFalse(load_waiter.test())
                self.assertEqual(runtime.get_state().lanes['fps0'].op,
                                 state.OP_LOADING)
                self.assertEqual(runtime.get_state().lanes['fps0'].unit, (0, 0))
            process.stdin.write('fixture_advance 100 0 0\n')
            process.stdin.flush()
            evidence = json.loads(process.stdout.readline())
            self.assertGreater(evidence['active_steps'], 0)
            reactor.now += .1
            self.assertTrue(unit.refresh())
            self.assertTrue(unit.motion_available)
            for _ in range(10):
                process.stdin.write('fixture_advance 200 0 0\n')
                process.stdin.flush()
                evidence = json.loads(process.stdout.readline())
                self.assertGreater(evidence['active_steps'], 0)
                reactor.now += .2
                service_managed()
                self.assertTrue(unit.motion_available)
            for _ in range(40):
                if (evidence['model_position'] >= HUB_STEPS
                        and evidence['active_steps'] >= HUB_STEPS):
                    break
                process.stdin.write('fixture_advance 200 0 0\n')
                process.stdin.flush()
                evidence = json.loads(process.stdout.readline())
                reactor.now += .2
                service_managed()
                self.assertTrue(unit.motion_available)
            else:
                self.fail('managed LOAD did not reach reference lane-to-HUB distance')
            process.stdin.write('fixture_advance 100 1 0\n')
            process.stdin.flush()
            hub_entry = json.loads(process.stdout.readline())
            self.assertTrue(hub_entry['hub'])
            reactor.now += .1
            self.assertTrue(unit.refresh())
            self.assertTrue(unit.motion_available)
            self.assertEqual(unit.current_spool, 0)
            service_managed()
            for _ in range(400):
                if (evidence['model_position'] - hub_entry['model_position']
                        >= BOWDEN_STEPS
                        and evidence['active_steps'] - hub_entry['active_steps']
                        >= BOWDEN_STEPS):
                    break
                process.stdin.write('fixture_advance 200 1 0\n')
                process.stdin.flush()
                evidence = json.loads(process.stdout.readline())
                reactor.now += .2
                service_managed()
                self.assertTrue(unit.motion_available)
            else:
                self.fail('managed LOAD did not traverse reference Bowden distance')
            process.stdin.write('fixture_advance 100 1 1\n')
            process.stdin.flush()
            self.assertTrue(json.loads(process.stdout.readline())['toolhead'])
            reactor.now += .1
            service_managed()
            if state is None:
                self.assertEqual(len(accepted), 1)
                self.assertEqual(accepted[0][0:2], ('fps0', 0))
            else:
                self.assertTrue(load_waiter.test())
                self.assertTrue(load_waiter.wait().ok)
                self.assertEqual(runtime.get_state().lanes['fps0'].op,
                                 state.OP_LOADED)
            self.assertIsNone(peek_event(mcu, reactor, INSTANCE, lambda: True))
            self.assertTrue(unit.motion_available)
            if manager is not None and exercise_gcode_handoff:
                # Route the toolhead unload through the actual registered G-code
                # handler. Stop before the autonomous rewind request so this
                # probe isolates the required reverse-FOLLOW -> macro handoff.
                # The macro is safe to run only after the physical follower
                # command has been observed as applied by the managed unit.
                self.assertIn('OAMSM_UNLOAD_FROM_TOOLHEAD',
                              printer.gcode_commands)
                loaded_unit = runtime.get_state().lanes['fps0'].unit
                reverse_macro_state = []

                def observe_unload_macro(script):
                    if script != manager.toolhead_unload_macro:
                        return
                    lane = runtime.get_state().lanes['fps0']
                    reverse_macro_state.append({
                        'pending': unit._pending_follower,
                        'following': lane.following,
                        'direction': lane.direction,
                        'unit': lane.unit,
                        'uncertain': lane.uncertain_follower_units,
                    })

                printer.gcode_script_hook = observe_unload_macro
                def service_reverse_follow(toolhead=1):
                    process.stdin.write('fixture_advance 10 1 %d\n' % toolhead)
                    process.stdin.flush()
                    json.loads(process.stdout.readline())
                    service_managed()

                reactor.pause_callback = service_reverse_follow
                low_level_unload = manager.cmd_UNLOAD_FILAMENT
                manager.cmd_UNLOAD_FILAMENT = lambda gcmd: None
                try:
                    printer.gcode_commands['OAMSM_UNLOAD_FROM_TOOLHEAD'](
                        types.SimpleNamespace(
                            error=lambda message: ValueError(message),
                            get=lambda name, default=None: default))
                finally:
                    manager.cmd_UNLOAD_FILAMENT = low_level_unload
                    printer.gcode_script_hook = None
                    reactor.pause_callback = None
                self.assertEqual(len(reverse_macro_state), 1)
                handoff = reverse_macro_state[0]
                self.assertIsNone(
                    handoff['pending'],
                    'unload macro ran before reverse FOLLOW was verified')
                self.assertIs(handoff['following'], True)
                self.assertEqual(handoff['direction'], state.FOLLOWER_REVERSE)
                self.assertEqual(handoff['unit'], loaded_unit)
                self.assertNotIn(loaded_unit, handoff['uncertain'])
                self.assertEqual(printer.gcode_scripts[-2:],
                                 [manager.toolhead_unload_macro, 'M400'])
                self.assertGreater(
                    sum(1 for name, _, _ in mcu.serial.frames
                        if name == 'km_heartbeat'), 2,
                    'host lease was not renewed during the managed LOAD')
                self.assertIsNone(peek_event(mcu, reactor, INSTANCE,
                                             lambda: True),
                                  'managed LOAD terminal was not ACKed')
                process.stdin.close()
                self.assertEqual(process.wait(timeout=5), 0,
                                 process.stderr.read())
                return
            unload_origin = evidence
            if state is None:
                unloaded = unit.start_unload_spool(gen=2)
                self.assertEqual(unloaded.response.admission, 0)
            else:
                unload_waiter = runtime.request('fps0', state.Unload('fps0'))
                self.assertFalse(unload_waiter.test())
                self.assertEqual(runtime.get_state().lanes['fps0'].op,
                                 state.OP_UNLOADING)

            def advance_managed(ms, hub, toolhead):
                process.stdin.write('fixture_advance %d %d %d\n' %
                                    (ms, hub, toolhead))
                process.stdin.flush()
                observed = json.loads(process.stdout.readline())
                reactor.now += ms / 1000.
                service_managed()
                self.assertTrue(unit.motion_available)
                return observed

            for _ in range(20):
                evidence = advance_managed(200, 1, 1)
                if (unload_origin['model_position'] - evidence['model_position']
                        >= TOOLHEAD_CLEAR_STEPS
                        and evidence['active_steps'] - unload_origin['active_steps']
                        >= TOOLHEAD_CLEAR_STEPS):
                    break
            else:
                self.fail('managed UNLOAD did not reach toolhead-clear distance')
            advance_managed(100, 1, 0)
            for _ in range(400):
                evidence = advance_managed(200, 1, 0)
                if (unload_origin['model_position'] - evidence['model_position']
                        >= BOWDEN_STEPS
                        and evidence['active_steps'] - unload_origin['active_steps']
                        >= BOWDEN_STEPS):
                    break
            else:
                self.fail('managed UNLOAD did not traverse reference Bowden distance')
            before_clear = evidence
            for _ in range(60):
                evidence = advance_managed(200, 0, 0)
                if (len(accepted) == 2 if state is None
                        else unload_waiter.test()):
                    break
            else:
                self.fail('managed UNLOAD did not complete post-HUB clearance')
            if state is None:
                self.assertEqual(accepted[1][0:2], ('fps0', 0))
            else:
                self.assertTrue(unload_waiter.wait().ok)
                self.assertEqual(runtime.get_state().lanes['fps0'].op,
                                 state.OP_UNLOADED)
            self.assertIsNone(peek_event(mcu, reactor, INSTANCE, lambda: True))
            self.assertGreaterEqual(evidence['active_steps']
                                    - before_clear['active_steps'], HUB_CLEAR_STEPS)
            self.assertTrue(unit.refresh())
            self.assertIsNone(unit.current_spool)
            self.assertTrue(unit.motion_available)
            self.assertEqual(mcu.serial.callbacks, {})
            process.stdin.close()
            self.assertEqual(process.wait(timeout=5), 0, process.stderr.read())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()

    @unittest.skipIf(FPS, 'drives the toolhead switch, which FPS mode lacks')
    def test_host_drives_each_boxturtle_lane_and_acks_events(self):
        self.assertIn(len(sys.argv), (2, 3), 'pass the built host_h723_join binary and optional mode')
        for lane in range(4):
            with self.subTest(lane=lane):
                self._run_lane(sys.argv[1], lane)

    def test_host_lease_expires_during_reference_load(self):
        self.assertIn(len(sys.argv), (2, 3), 'pass the built host_h723_join binary and optional mode')
        process = subprocess.Popen([sys.argv[1], 'host_session', '0'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True,
                                   env=dict(os.environ, ASAN_OPTIONS='detect_leaks=0:halt_on_error=1'))
        try:
            def fixture(command):
                process.stdin.write(command + '\n')
                process.stdin.flush()
                return json.loads(process.stdout.readline())

            if FPS:
                # This test only cares about lease expiry once LOAD is
                # admitted, not arrival; any calibrated distance within the
                # outer travel does. Delivered before startup, like the
                # host's own config commands.
                fixture('config_km_fps_steps 2 109091')
            self._startup(process)
            reactor = wire.fixture.Reactor()
            mcu = wire.fixture.MCU(reactor)
            mcu.serial = NativeH723Serial(reactor, process)
            current = lambda: True
            self.assertEqual(send_heartbeat(mcu, reactor, INSTANCE, SESSION,
                                            current).result, 0)
            identity = Identity(1, SESSION, 1, 1, 0)
            self.assertEqual(send_operation(mcu, reactor, INSTANCE, identity,
                                            current, direction=1).admission, 0)
            moving = fixture('fixture_advance 500 0 0')
            self.assertGreater(moving['active_steps'], 0)
            self.assertGreater(moving['model_position'], 0)
            fixture('fixture_advance 1000 0 0')
            stopped = fixture('fixture_advance 600 0 0')
            status, _ = read_physical_state(mcu, reactor, INSTANCE, 1, current,
                                            load_capable=True)
            self.assertEqual(status.values['system_fault'], 1)
            self.assertEqual(status.values['motion_fault'], 1)
            self.assertEqual(status.values['state'], 6)
            self.assertEqual(status.values['velocity'], 0)
            self.assertFalse(status.values['host_connected'])
            self.assertTrue(status.values['position_uncertain'])
            failed = peek_event(mcu, reactor, INSTANCE, current)
            self.assertEqual((failed.identity, failed.event_type), (identity, 3))
            self.assertNotEqual(failed.result_code, 0)
            self.assertTrue(fixture('fixture_outputs')['outputs_off'])
            later = fixture('fixture_advance 500 0 0')
            self.assertEqual(later['active_steps'], stopped['active_steps'])
            self.assertEqual(send_heartbeat(mcu, reactor, INSTANCE, SESSION,
                                            current).result, 0)
            status, _ = read_physical_state(mcu, reactor, INSTANCE, 2, current,
                                            load_capable=True)
            self.assertEqual(status.values['system_fault'], 1)
            self.assertTrue(fixture('fixture_outputs')['outputs_off'])
            self.assertEqual(peek_event(mcu, reactor, INSTANCE, current), failed)
            process.stdin.close()
            self.assertEqual(process.wait(timeout=5), 0, process.stderr.read())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

            for stream in (process.stdin, process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()

    def _run_lane(self, binary, lane):
        process = subprocess.Popen([binary, 'host_session', str(lane)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   env=dict(os.environ, ASAN_OPTIONS='detect_leaks=0:halt_on_error=1'))
        try:
            self._startup(process)

            reactor = wire.fixture.Reactor()
            mcu = wire.fixture.MCU(reactor)
            mcu.serial = NativeH723Serial(reactor, process)
            current = lambda: True
            heartbeat = send_heartbeat(mcu, reactor, INSTANCE, SESSION, current)
            self.assertEqual(heartbeat.result, 0)
            advance_count = [0]
            identity = Identity(1, SESSION, 1, 1, lane)
            admission = send_operation(mcu, reactor, INSTANCE, identity,
                                       current, direction=1)
            self.assertEqual((admission.identity, admission.admission),
                             (identity, 0))
            def fixture(command):
                process.stdin.write(command + '\n')
                process.stdin.flush()
                return json.loads(process.stdout.readline())

            def advance(ms, hub, toolhead):
                self.assertEqual(send_heartbeat(mcu, reactor, INSTANCE, SESSION,
                                                 current).result, 0)
                advance_count[0] += 1
                evidence = fixture('fixture_advance %d %d %d' %
                                   (ms, hub, toolhead))
                self.assertTrue(evidence['hub_valid'])
                self.assertTrue(evidence['toolhead_valid'])
                self.assertTrue(evidence['bay_valid'])
                self.assertTrue(evidence['load_valid'])
                self.assertTrue(evidence['bay'])
                self.assertTrue(evidence['load'])
                self.assertTrue(evidence['other_lanes_still'])
                self.assertGreater(evidence['active_steps'], 0)
                self.assertEqual(evidence['hub'], bool(hub))
                self.assertEqual(evidence['toolhead'], bool(toolhead))
                return evidence

            def travel(steps, start, direction, hub, toolhead, chunks):
                for _ in range(chunks):
                    evidence = advance(500, hub, toolhead)
                    moved = direction * (evidence['model_position']
                                         - start['model_position'])
                    pulses = evidence['active_steps'] - start['active_steps']
                    if moved >= steps and pulses >= steps:
                        return evidence
                self.fail('feeder did not traverse %d signed reference steps in time' % steps)

            query = [1]
            def physical():
                status, sensors = read_physical_state(
                    mcu, reactor, INSTANCE, query[0], current, load_capable=True)
                query[0] += 1
                presence = project_sensor_presence(sensors, INSTANCE, 4)
                self.assertIsNotNone(presence)
                self.assertEqual(status.instance, INSTANCE)
                self.assertEqual(status.query, sensors.query)
                self.assertEqual(status.values['acquired_ms'], sensors.observed_ms)
                return status.values, presence

            observed, presence = physical()
            self.assertEqual(observed['owner_lane'], lane)
            self.assertEqual(observed['stage'], 1)  # LOAD_FEED_TO_HUB
            self.assertEqual(presence.bay_present[lane], True)

            delivery = EventDelivery(INSTANCE, 1)
            def terminal(request, operation, expected_type):
                event = peek_event(mcu, reactor, INSTANCE, current)
                self.assertIsNotNone(event)
                self.assertEqual((event.identity, event.event_type),
                                 (Identity(1, SESSION, request, operation, lane),
                                  expected_type))
                self.assertEqual(event.identity.lane, lane)
                generation = delivery.generation
                ack = delivery.observe(event, generation, lambda item: True)
                self.assertEqual(ack[0], 'km_event_ack instance_hi=%u instance_lo=%u client=%c session=%u request=%u operation=%c lane=%c seq=%u')
                self.assertEqual(ack[1][6], lane)
                if request == 1:
                    rejected = reactor.completion()
                    result_format = ('km_command_result instance_hi=%u instance_lo=%u '
                                     'command=%c query=%u result=%c')
                    callback = mcu.register_serial_response(
                        lambda params: reactor.register_async_callback(
                            lambda eventtime: rejected.complete(dict(params))),
                        result_format)
                    try:
                        bad_ack = mcu.lookup_command(ack[0])
                        bad_ack.send(ack[1][:-1] + [event.seq + 1])
                        reply = rejected.wait(reactor.monotonic() + 5.0)
                        self.assertIsNotNone(reply)
                        self.assertEqual((reply['command'], reply['query']),
                                         (6, event.seq + 1))
                        self.assertNotEqual(reply['result'], 0)
                    finally:
                        callback.unregister()
                self.assertTrue(send_pending_ack(mcu, reactor, delivery,
                                                 generation, current))

            advance(50, 0, 0)
            observed, presence = physical()
            self.assertEqual(observed['stage'], 1)  # LOAD_FEED_TO_HUB
            self.assertEqual(observed['owner_lane'], lane)
            self.assertEqual(presence.bay_present[lane], True)
            if lane == 0:
                # Pinned AFC reference: 16 microsteps, 200 full steps, a 50:10
                # gearbox and 22 mm/revolution, up to 155 mm from this bay to
                # HUB. Other lanes retain the compact command-path test; this
                # one covers actual step budget.
                travel(HUB_STEPS, {'model_position': 0, 'active_steps': 0},
                       1, 0, 0, 40)
            hub_entry = advance(15, 1, 0)
            observed, presence = physical()
            self.assertEqual(observed['stage'], 2)  # LOAD_FEED_TO_BUFFER
            self.assertEqual(presence.shared_hub_present, True)
            if lane == 0:
                # Reference HUB-to-toolhead Bowden path: 1725 mm.
                travel(BOWDEN_STEPS, hub_entry, 1, 1, 0, 180)
            advance(15, 1, 1)
            observed, presence = physical()
            self.assertEqual(observed['stage'], 3)  # FOLLOW
            self.assertEqual(presence.toolhead_present, True)
            terminal(1, 1, 1)  # LOAD_COMPLETE
            follow = Identity(1, SESSION, 2, 3, lane)
            self.assertEqual(send_operation(mcu, reactor, INSTANCE, follow,
                                            current, direction=1).admission, 0)
            observed, _ = physical()
            self.assertEqual(observed['operation'], 3)
            self.assertEqual(observed['owner_lane'], lane)
            unload_origin = advance(5, 1, 1)
            unload = Identity(1, SESSION, 3, 2, lane)
            self.assertEqual(send_operation(mcu, reactor, INSTANCE, unload,
                                            current, direction=0).admission, 0)
            observed, _ = physical()
            self.assertEqual(observed['operation'], 2)
            self.assertEqual(observed['owner_lane'], lane)
            # FOLLOW is retained as STOPPED ahead of the final UNLOAD event.
            terminal(2, 3, 11)  # STOPPED
            if lane == 0:
                # The 20 mm toolhead-clear point is a synthetic switch model;
                # the full reverse Bowden distance is the AFC reference gate.
                travel(TOOLHEAD_CLEAR_STEPS, unload_origin, -1, 1, 1, 20)
                advance(10, 1, 0)
                travel(BOWDEN_STEPS, unload_origin, -1, 1, 0, 180)
                before_clear = advance(10, 1, 0)
            else:
                before_clear = advance(10, 0, 1)
            advance(250, 0, 0)
            observed, _ = physical()
            self.assertTrue(observed['path_occupied'])
            self.assertIn(observed['stage'], (5, 6))  # braking or post-HUB travel
            for _ in range(60):
                after_clear = advance(250, 0, 0)
                observed, _ = physical()
                if observed['stage'] == 0:
                    break
            else:
                self.fail('post-HUB clearance did not complete before its deadline')
            self.assertGreaterEqual(after_clear['active_steps']
                                    - before_clear['active_steps'], HUB_CLEAR_STEPS)
            self.assertGreater(before_clear['model_position'],
                               after_clear['model_position'])
            observed, presence = physical()
            self.assertFalse(observed['path_occupied'], observed)
            self.assertFalse(observed['lane_active'])
            self.assertEqual(observed['stage'], 0)  # STOPPED after UNLOAD
            self.assertEqual(observed['owner_lane'], lane)  # retained last owner
            self.assertFalse(presence.shared_hub_present)
            self.assertFalse(presence.toolhead_present)
            terminal(3, 2, 4)  # UNLOAD_COMPLETE
            result = fixture('fixture_outputs')
            self.assertTrue(result['outputs_off'])
            lifecycle = [name for name, _, _ in mcu.serial.frames
                         if name not in ('km_snapshot_part', 'km_sensor_part')]
            self.assertEqual(lifecycle.count('km_heartbeat'), 1 + advance_count[0])
            self.assertEqual([name for name in lifecycle if name != 'km_heartbeat'],
                             ['km_start', 'km_event_peek', 'km_event_ack',
                              'km_event_ack', 'km_start', 'km_start',
                              'km_event_peek', 'km_event_ack',
                              'km_event_peek', 'km_event_ack'])
            for command in ('km_snapshot_part', 'km_sensor_part'):
                parts = [params['part'] for name, params, _ in mcu.serial.frames
                         if name == command]
                self.assertEqual(parts, list(range(5)) * (query[0] - 1))
            self.assertEqual([params['lane'] for name, params, _ in mcu.serial.frames
                              if name == 'km_start'], [lane, lane, lane])
            self.assertEqual(mcu.serial.callbacks, {})
            process.stdin.close()
            self.assertEqual(process.wait(timeout=5), 0, process.stderr.read())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()

    @staticmethod
    def _startup(process):
        payload = '' if SWITCHED else ' ' + encode_calibration(CALIBRATION).hex()
        process.stdin.write('startup' + payload + '\n')
        process.stdin.flush()
        assert json.loads(process.stdout.readline())['startup'] is True


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0], '-v'])
