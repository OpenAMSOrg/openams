# SPDX-License-Identifier: GPL-3.0-or-later
"""Native registered-command checks; KLIPPER_SOURCE supplies the actual wire codec."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from host.protocol import (decode_admission, decode_event, decode_transport_state,
                           decode_command_result, SnapshotAssembler, SensorSnapshotAssembler)
from host.requests import Requests
from host.path_reconciliation import (decode_driver_restart_result,
                                      decode_path_reconcile_result)
from host.load_report import decode_load_attempts, query_load_attempts
from host.wire import ENUMERATIONS, check_enumerations, members

CORE = ('rate', 'trajectory', 'step_segments', 'selector_home', 'buffer_sensor', 'follower', 'operation_core', 'event_queue',
        'device_profile', 'protocol_wire', 'current_regulator', 'mcu_clock')
CASES = ('lifecycle', 'requests', 'events', 'preload', 'snapshot', 'sensor', 'diag', 'lease', 'shutdown', 'clock_fault', 'lease_expiry', 'start_permission', 'permission_upgrade', 'permission_shutdown', 'permission_board_loss', 'reconcile', 'driver_restart', 'driver_restart_selector', 'selector_commands', 'selector_gate_servo')


class CommandsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='km-commands-')
        cls.addClassCleanup(cls.temp.cleanup)
        work = Path(cls.temp.name)
        # Freeze dependencies while a separate bounded job may add a clock API.
        for directory in ('include', 'src', 'mcu', 'tests'):
            shutil.copytree(ROOT / directory, work / directory,
                            ignore=shutil.ignore_patterns('__pycache__', '*.orig', '*.rej'))
        flags = ['gcc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-pedantic',
                 '-Wconversion', '-Wsign-conversion', '-fsanitize=address,undefined',
                 '-fno-sanitize-recover=all', '-fno-pie', '-no-pie', '-g',
                 '-Itests/klipper_clock/shim_include', '-Itests/klipper_clock',
                 '-Iinclude', '-Imcu', '-Itests',
                 # TMC builds add the explicit lane driver restart command.
                 '-DCONFIG_WANT_TMCUART_LOCAL=1', '-DCONFIG_KLIPPER_MMU_TMC_UART_SHARED=1',
                 # A SERVO build (without a selector stepper) adds the runtime
                 # selector gate/servo-test commands; the profile itself
                 # (km_profile_init_nightowl below) has no selector, so this
                 # only needs to compile them, not exercise a real one.
                 '-DCONFIG_KLIPPER_MMU_SERVO=1',
                 '-DCONFIG_KLIPPER_MMU_SELECTOR_SERVO_MAX_ANGLE_DEG=180',
                 '-DCONFIG_KLIPPER_MMU_SELECTOR_SERVO_MIN_PULSE_US=500',
                 '-DCONFIG_KLIPPER_MMU_SELECTOR_SERVO_MAX_PULSE_US=2500']
        subprocess.run([*flags, '-Dcommand_sendf=unused_clock_sendf', '-c',
                        'tests/klipper_clock/shim_klipper.c', '-o', 'shim.o'],
                       cwd=work, check=True, timeout=30, capture_output=True)
        sources = [f'src/{name}.c' for name in CORE]
        # tests/commands/capture.c pulls in fake_clock.h, whose B2 tick path runs
        # its own reference executor (km_ref_*), so the reference source is part
        # of this link exactly as in the other native suites.
        command_sources = [*sources, 'mcu/clock.c', 'mcu/messages.c', 'mcu/commands.c',
                        'tests/mock_driver.c', 'tests/commands/capture.c',
                        'tests/stepper_reference.c', 'shim.o', '-lm']
        subprocess.run([*flags, '-DCONFIG_KLIPPER_MMU_ACTUATION_OPT_IN=1',
                        *command_sources, '-o', 'commands'], cwd=work, check=True,
                       timeout=60, capture_output=True)
        subprocess.run([*flags, *command_sources, '-o', 'commands-default-off'],
                       cwd=work, check=True, timeout=60, capture_output=True)
        cls.binary = work / 'commands'
        cls.binary_default_off = work / 'commands-default-off'
        source = Path(os.environ['KLIPPER_SOURCE'])
        spec = importlib.util.spec_from_file_location('commands_msgproto', source / 'klippy/msgproto.py')
        cls.codec = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.codec)

    def test_registered_command_scenarios_and_actual_wire_codec(self):
        formats = {}
        for case in CASES:
            with self.subTest(case=case):
                run = subprocess.run([str(self.binary), case], text=True, capture_output=True,
                                     timeout=15, env=dict(os.environ, ASAN_OPTIONS='detect_leaks=0:halt_on_error=1'))
                self.assertEqual(run.returncode, 0, run.stderr + run.stdout[-1000:])
                for line in run.stdout.splitlines():
                    row = json.loads(line)
                    fmt = self.codec.MessageFormat([1], row['format'])
                    formats[fmt.name] = fmt
                    frame = [0, self.codec.MESSAGE_DEST] + fmt.encode(row['values'])
                    frame[0] = len(frame) + 3
                    frame += self.codec.crc16_ccitt(frame) + [self.codec.MESSAGE_SYNC]
                    self.assertEqual(self.codec.MessageParser().check_packet(frame), len(frame))
                    params, end = fmt.parse(frame, 2)
                    self.assertEqual(end, len(frame) - 3)
                    self.assertEqual(list(params.values()), row['values'])
                    if fmt.name == 'km_admission':
                        decode_admission(params)
                    elif fmt.name == 'km_event':
                        decode_event(params)
                    elif fmt.name == 'km_transport_state':
                        decode_transport_state(params)
                    elif fmt.name == 'km_command_result':
                        decode_command_result(params)
                    elif fmt.name == 'km_path_reconcile_result':
                        decode_path_reconcile_result(params)
                    elif fmt.name == 'km_driver_restart_result':
                        decode_driver_restart_result(params)
                    elif fmt.name == 'km_load_attempts':
                        decode_load_attempts(params)
                    if fmt.name.startswith('km_status_'):
                        instance = (params['instance_hi'] << 32) | params['instance_lo']
                        # Complete assembly/lifecycle is checked separately; here
                        # every production fragment must satisfy the host decoder.
                        SnapshotAssembler(instance, params['query']).add(fmt.name, params)
                    if fmt.name in ('km_sensor_flags', 'km_sensor_bays0', 'km_sensor_bays1'):
                        instance = (params['instance_hi'] << 32) | params['instance_lo']
                        SensorSnapshotAssembler(instance, params['query']).add(fmt.name, params)
        self.assertEqual(set(formats), {'km_transport_state', 'km_command_result',
                         'km_admission', 'km_event', 'km_status_identity', 'km_status_motion',
                         'km_status_time', 'km_status_sessions', 'km_status_requests',
                         'km_sensor_flags', 'km_sensor_bays0', 'km_sensor_bays1',
                         'km_sensor_loads0', 'km_sensor_loads1',
                         'km_diag_bay_bounce', 'km_diag_bay_idle', 'km_diag_load_bounce',
                         'km_diag_load_idle', 'km_diag_tmc0', 'km_diag_tmc1',
                         'km_path_reconcile_result', 'km_driver_restart_result',
                         'km_load_attempts', 'km_selector_gate_set_result',
                         'km_selector_servo_test_result', 'km_selector_motors_off_result'})
        for name, fmt in formats.items():
            with self.subTest(message=name):
                self.assertLessEqual(10 + sum(p.max_length for p in fmt.param_types), self.codec.MESSAGE_MAX)

    def test_klipper_build_exports_the_host_enumerations(self):
        """Actual ctr.h encoding, buildcommands handler and msgproto parser."""
        source = Path(os.environ['KLIPPER_SOURCE'])
        work = Path(self.temp.name)
        subprocess.run(['gcc', '-std=gnu11', '-Wall', '-Wextra', '-Wtype-limits',
                        '-Werror', '-c', '-I' + str(source / 'src'), '-Iinclude',
                        '-Imcu', 'mcu/enumerations.c', '-o', 'enumerations.o'],
                       cwd=work, check=True, timeout=60, capture_output=True)
        subprocess.run(['objcopy', '-j', '.compile_time_request', '-O', 'binary',
                        'enumerations.o', 'enumerations.ctr'],
                       cwd=work, check=True, timeout=30, capture_output=True)
        sys.modules.setdefault('msgproto', self.codec)
        spec = importlib.util.spec_from_file_location(
            'commands_buildcommands', source / 'scripts/buildcommands.py')
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        handler = build.HandlerEnumerations
        for request in (work / 'enumerations.ctr').read_bytes().split(b'\0'):
            request = request.decode().strip()
            if request:
                handler.ctr_dispatch[request.split()[0]](request)
        data = {}
        handler.update_data_dictionary(data)
        parser = self.codec.MessageParser()
        parser.fill_enumerations(data['enumerations'])
        firmware = parser.get_enumerations()
        self.assertIs(check_enumerations(firmware), True)
        self.assertEqual(firmware, {name: members(namespace)
                                    for name, namespace in ENUMERATIONS.items()})

    def test_reconciled_boot_snapshots_enable_host_session_selection(self):
        from host.session_reconciliation import (choose_host_session_with_physical,
                                                  choose_occupied_host_session)
        run = subprocess.run([str(self.binary), 'reconciled_session'], text=True,
                             capture_output=True, timeout=15,
                             env=dict(os.environ, ASAN_OPTIONS='detect_leaks=0:halt_on_error=1'))
        self.assertEqual(run.returncode, 0, run.stderr)
        instance = 0x89abcdef12345678
        assemblers, snapshots = {}, {}
        for line in run.stdout.splitlines():
            row = json.loads(line)
            fmt = self.codec.MessageFormat([1], row['format'])
            params, _ = fmt.parse(fmt.encode(row['values']), 0)
            if fmt.name.startswith('km_status_'):
                kind, factory = 'status', SnapshotAssembler
            elif fmt.name.startswith('km_sensor_'):
                kind, factory = 'sensors', SensorSnapshotAssembler
            else:
                continue
            key = params['query'], kind
            assembler = assemblers.setdefault(key, factory(instance, params['query']))
            result = assembler.add(fmt.name, params)
            if result is not None:
                snapshots[key] = result
        for choose in (choose_host_session_with_physical, choose_occupied_host_session):
            with self.assertRaises(ValueError):
                choose(snapshots[1, 'status'], snapshots[1, 'sensors'], instance, 4)
        empty = choose_host_session_with_physical(
            snapshots[3, 'status'], snapshots[3, 'sensors'], instance, 4)
        occupied = choose_occupied_host_session(
            snapshots[5, 'status'], snapshots[5, 'sensors'], instance, 4)
        self.assertEqual((empty.session, empty.last_request), (1, 0))
        self.assertEqual((occupied.lane, occupied.session_start.session), (2, 1))

    def test_host_to_native_load_terminal(self):
        """One request crosses Klipper's wire wrapper and production C handlers."""
        if not os.environ.get('KM_WIRE_DICTIONARY'):
            self.skipTest('requires one exact release-image dictionary')
        source = ROOT / 'tests/klipper_host_wire_pinned/run_tests.py'
        spec = importlib.util.spec_from_file_location('km_native_wire', source)
        wire = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wire)
        from host.event_delivery import EventDelivery, send_pending_ack
        from host.event_reader import peek_event
        from host.command_transport import send_heartbeat, send_operation

        orders = {
            'km_heartbeat': ('instance_hi', 'instance_lo', 'session'),
            'km_start': ('instance_hi', 'instance_lo', 'client', 'session',
                         'request', 'operation', 'lane', 'direction'),
            'km_event_peek': ('instance_hi', 'instance_lo'),
            'km_event_ack': ('instance_hi', 'instance_lo', 'client', 'session',
                             'request', 'operation', 'lane', 'seq'),
            'km_load_attempts_query': ('instance_hi', 'instance_lo', 'client',
                                       'session', 'request', 'operation', 'lane'),
        }

        class NativeSerial(wire.WireSerial):
            def __init__(self, reactor, process):
                super().__init__(reactor)
                self.process = process

            def raw_send_wait_ack(self, frame, *unused):
                tag, _ = self.parser.msgid_parser.parse(frame, 0)
                fmt = self.parser.messages_by_id[tag]
                fields, end = fmt.parse(frame, 0)
                assert end == len(frame) and fmt.name in orders
                self.frames.append((fmt.name, fields, len(frame)))
                args = ' '.join(str(fields[key]) for key in orders[fmt.name])
                self.process.stdin.write(fmt.name + ' ' + args + '\n')
                self.process.stdin.flush()
                row = json.loads(self.process.stdout.readline())
                assert row['format'] in wire.WIRE_DICTIONARY['responses']
                response = self.parser.messages_by_name[row['format'].split()[0]]
                encoded = response.encode(row['values'])
                decoded, end = response.parse(encoded, 0)
                assert end == len(encoded)
                callback = self.callbacks.get((response.name, None))
                assert callback is not None
                callback(decoded)

            def tick(self, duration, hub, toolhead):
                self.process.stdin.write('tick %d %d %d\n' % (duration, hub, toolhead))
                self.process.stdin.flush()
                assert json.loads(self.process.stdout.readline()) == {'tick': True}

        proc = subprocess.Popen([str(self.binary), 'host_session'],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                env=dict(os.environ, ASAN_OPTIONS='detect_leaks=0:halt_on_error=1'))
        try:
            reactor = wire.fixture.Reactor()
            mcu = wire.fixture.MCU(reactor)
            mcu.serial = NativeSerial(reactor, proc)
            instance, session = 0x89abcdef12345678, 0xabcdef01
            requests = Requests(instance, session, 0x12345677)
            beat = send_heartbeat(mcu, reactor, instance, session, lambda: True)
            self.assertEqual(beat.result, 0)
            identity = requests.issue(1, 2, 'fps', 7)
            admission = send_operation(mcu, reactor, instance, identity,
                                       lambda: True, direction=1)
            self.assertEqual((admission.identity, admission.admission), (identity, 0))
            self.assertIsNone(requests.admission(admission, requests.generation))
            mcu.serial.tick(20, 1, 1)
            event = peek_event(mcu, reactor, instance, lambda: True)
            self.assertEqual((event.identity, event.event_type, event.result_code),
                             (identity, 1, 0))
            delivery = EventDelivery(instance, 1)
            accepted = []
            def consume(head):
                completion = requests.event(head, requests.generation)
                return requests.deliver(completion, lambda *result: accepted.append(result))
            self.assertIsNotNone(delivery.observe(event, 1, consume))
            self.assertEqual(accepted, [('fps', 0, event.value, 7)])
            self.assertTrue(send_pending_ack(mcu, reactor, delivery, 1, lambda: True))
            self.assertIsNone(peek_event(mcu, reactor, instance, lambda: True))
            attempts = query_load_attempts(mcu, reactor, instance, identity, lambda: True)
            self.assertEqual([(a.stage, a.fault) for a in attempts], [(2, 0)])
            self.assertEqual([name for name, _, _ in mcu.serial.frames],
                             ['km_heartbeat', 'km_start', 'km_event_peek',
                              'km_event_ack', 'km_event_peek', 'km_load_attempts_query'])
            self.assertEqual(mcu.serial.callbacks, {})
            proc.stdin.close()
            self.assertEqual(proc.wait(timeout=5), 0, proc.stderr.read())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_default_off_build_refuses_both_permission_paths(self):
        run = subprocess.run([str(self.binary_default_off), 'permission_disabled'],
                             text=True, capture_output=True, timeout=15,
                             env=dict(os.environ, ASAN_OPTIONS='detect_leaks=0:halt_on_error=1'))
        self.assertEqual(run.returncode, 0, run.stderr + run.stdout[-1000:])

    def test_administrative_responses_cannot_complete_operation(self):
        state = decode_transport_state(dict(challenge=0xffffffff, instance_hi=1,
            instance_lo=2, last_query=0xffffffff, state=1))
        requests = Requests(state.instance, 123)
        requests.issue(1, 2, 'fps', 0)
        reply = decode_command_result(dict(instance_hi=1, instance_lo=2, command=1,
                                           query=1, result=0))
        with self.assertRaises(TypeError):
            requests.admission(reply, requests.generation)
        with self.assertRaises(TypeError):
            requests.event(reply, requests.generation)
        self.assertEqual(len(requests.pending), 1)
        for kwargs in (dict(instance_hi=0, instance_lo=0, state=1),
                       dict(instance_hi=1, instance_lo=2, state=0),
                       dict(instance_hi=1, instance_lo=2, state=4)):
            with self.assertRaises(ValueError):
                decode_transport_state(dict(challenge=1, last_query=0, **kwargs))
        with self.assertRaises(ValueError):
            decode_command_result(dict(instance_hi=1, instance_lo=2, command=256,
                                       query=1, result=0))


if __name__ == '__main__':
    unittest.main()
