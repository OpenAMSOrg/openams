# SPDX-License-Identifier: GPL-3.0-or-later
"""Round-trip MMU host commands through pinned Klipper's actual wire wrappers.

Run with an explicit timeout. This fixture never opens a printer serial device.
"""
import importlib.util
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
fixture_path = ROOT / 'tests/klipper_terminal_pinned/run_tests.py'
spec = importlib.util.spec_from_file_location('pinned_terminal_fixture', fixture_path)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)

from host.command_transport import send_operation, send_heartbeat, send_stop
from host.event_delivery import EventDelivery, send_pending_ack
from host.event_reader import peek_event
from host.protocol import Identity
from host.physical_state_reader import read_physical_state
from host.sensor_reader import read_sensors
from host.wire import (DIFF_PROBE_QUERY, DIFF_PROBE_STATE, SENSOR_REPLIES,
                       STATUS_REPLIES, check_enumerations)

INSTANCE = 0x87654321fedcba98
IDENTITY = Identity(1, 0xabcdef01, 0x87654321, 1, 7)
SEQ = 0xfedcba98

# Older diagnostic dictionaries predate follower status and LOAD sensors.
# With an explicit release-image dictionary, require its actual reply formats;
# synthesizing them would hide a broken host/firmware build contract.
WIRE_DICTIONARY = dict(fixture.dictionary)
WIRE_DICTIONARY['responses'] = dict(fixture.dictionary['responses'])
responses = WIRE_DICTIONARY['responses']
if os.environ.get('KM_WIRE_DICTIONARY'):
    required = (STATUS_REPLIES[1], SENSOR_REPLIES[3], SENSOR_REPLIES[4])
    missing = [fmt for fmt in required if fmt not in responses]
    if missing:
        raise RuntimeError('Release-image dictionary is missing %s' % missing)
else:
    old_motion = next(fmt for fmt in responses if fmt.startswith('km_status_motion '))
    if old_motion != STATUS_REPLIES[1]:
        responses[STATUS_REPLIES[1]] = responses.pop(old_motion)
    for fmt in SENSOR_REPLIES[3:]:
        if fmt not in responses:
            responses[fmt] = max(responses.values()) + 1


class WireSerial(fixture.Serial):
    def __init__(self, reactor):
        super().__init__(reactor)
        self.parser = fixture.msgproto.MessageParser()
        self.parser.process_identify(fixture.json.dumps(WIRE_DICTIONARY).encode(),
                                     decompress=False)
        self.frames = []
        self.reply_for = None

    def raw_send_wait_ack(self, wire, *args):
        tag, _ = self.parser.msgid_parser.parse(wire, 0)
        fmt = self.parser.messages_by_id[tag]
        params, end = fmt.parse(wire, 0)
        assert end == len(wire)
        assert len(wire) <= fixture.msgproto.MESSAGE_PAYLOAD_MAX
        self.frames.append((fmt.name, params, len(wire)))
        name, fields = self.reply_for(fmt.name, params)
        response = self.parser.messages_by_name[name]
        encoded = response.encode_by_name(**fields)
        assert len(encoded) <= fixture.msgproto.MESSAGE_PAYLOAD_MAX
        decoded, end = response.parse(encoded, 0)
        assert end == len(encoded)
        callback = self.callbacks.get((name, None))
        assert callback is not None, name
        callback(decoded)


class WireTest(unittest.TestCase):
    def setUp(self):
        self.reactor = fixture.Reactor()
        self.mcu = fixture.MCU(self.reactor)
        self.mcu.serial = WireSerial(self.reactor)

    def exchange(self, action, response):
        self.mcu.serial.reply_for = response
        try:
            return action()
        finally:
            self.assertEqual(self.mcu.serial.callbacks, {})

    def test_raw_probe_is_confined_to_receive_only_image(self):
        expected = os.environ.get('KM_WIRE_EXPECT_DIFF_PROBE')
        if expected is None:
            self.skipTest('no release-image probe expectation')
        commands = WIRE_DICTIONARY['commands']
        responses = WIRE_DICTIONARY['responses']
        present = (DIFF_PROBE_QUERY in commands
                   and 'km_diff_probe_start' in commands
                   and DIFF_PROBE_STATE in responses)
        self.assertEqual(present, expected == '1')
        if expected != '1':
            self.assertFalse(any(fmt.startswith('km_diff_probe_')
                                 for fmt in commands))
            self.assertFalse(any(fmt.startswith('km_diff_probe_')
                                 for fmt in responses))
            return

        parser = self.mcu.serial.parser
        query = parser.lookup_command(DIFF_PROBE_QUERY)
        wire = query.encode([0xfedcba98])
        self.assertLessEqual(len(wire), fixture.msgproto.MESSAGE_PAYLOAD_MAX)
        decoded, end = query.parse(wire, 0)
        self.assertEqual(end, len(wire))
        self.assertEqual(decoded['challenge'], 0xfedcba98)

        response = parser.messages_by_name['km_diff_probe_state']
        self.assertEqual(response.msgformat, DIFF_PROBE_STATE)
        frame = response.encode_by_name(
            challenge=0xfedcba98, state=2, result=0, a_raw=4095,
            b_raw=4095, a_attempt_us=0xffffffff, b_ready_us=0xffffffff,
            observed_us=0xffffffff)
        self.assertLessEqual(len(frame), fixture.msgproto.MESSAGE_PAYLOAD_MAX)
        parsed, end = response.parse(frame, 0)
        self.assertEqual(end, len(frame))
        self.assertEqual(parsed['a_raw'], 4095)
        self.assertEqual(parsed['observed_us'], 0xffffffff)

    def test_dictionary_enumerations_match_host_and_leave_fields_numeric(self):
        parser = self.mcu.serial.parser
        # A release image must export them; the retained default dictionary
        # predates them and is accepted only as a protocol 1 legacy image.
        self.assertEqual(check_enumerations(parser.get_enumerations()),
                         bool(os.environ.get('KM_WIRE_DICTIONARY')))
        self.assertEqual(WIRE_DICTIONARY['config'].get(
            'KLIPPER_MMU_PROTOCOL_VERSION'), 1)
        for name, message in parser.messages_by_name.items():
            if 'km_' in name:
                for param, kind in message.param_names:
                    self.assertNotIsInstance(kind, fixture.msgproto.Enumeration,
                                             '%s %s' % (name, param))

    def test_start_cancel_heartbeat_stop_with_full_width_fields(self):
        def respond(name, fields):
            base = dict(instance_hi=INSTANCE >> 32,
                        instance_lo=INSTANCE & 0xffffffff)
            if name in ('km_start', 'km_cancel'):
                self.assertEqual((fields['session'], fields['request'], fields['lane']),
                                 (IDENTITY.session, IDENTITY.request, IDENTITY.lane))
                return ('km_admission', dict(base, client=IDENTITY.client,
                                             session=IDENTITY.session,
                                             request=IDENTITY.request,
                                             operation=IDENTITY.operation,
                                             lane=IDENTITY.lane,
                                             admission=0, fault=0))
            kind, query = {'km_heartbeat': (3, IDENTITY.session),
                           'km_stop': (4, 0)}[name]
            return ('km_command_result', dict(base, command=kind,
                                              query=query, result=0))

        actions = [lambda: send_operation(self.mcu, self.reactor, INSTANCE,
                                          IDENTITY, lambda: True, direction=1),
                   lambda: send_operation(self.mcu, self.reactor, INSTANCE,
                                          IDENTITY, lambda: True, cancel=True),
                   lambda: send_heartbeat(self.mcu, self.reactor, INSTANCE,
                                          IDENTITY.session, lambda: True),
                   lambda: send_stop(self.mcu, self.reactor, INSTANCE,
                                     lambda: True)]
        replies = [self.exchange(action, respond) for action in actions]
        self.assertEqual([r.__class__.__name__ for r in replies],
                         ['Admission', 'Admission', 'CommandResult', 'CommandResult'])
        self.assertEqual([name for name, _, _ in self.mcu.serial.frames],
                         ['km_start', 'km_cancel', 'km_heartbeat', 'km_stop'])
        for _, fields, _ in self.mcu.serial.frames:
            self.assertEqual((fields['instance_hi'], fields['instance_lo']),
                             (INSTANCE >> 32, INSTANCE & 0xffffffff))

    def test_peek_and_ack_exact_retained_head(self):
        fields = dict(instance_hi=INSTANCE >> 32,
                      instance_lo=INSTANCE & 0xffffffff, seq=SEQ,
                      timestamp_ms=0xfedcba98, client=IDENTITY.client,
                      session=IDENTITY.session, request=IDENTITY.request,
                      operation=IDENTITY.operation, lane=IDENTITY.lane,
                      event_type=1, result_code=0, value=0xffffffff, flags=0)

        def respond(name, params):
            if name == 'km_event_peek':
                return 'km_event', fields
            self.assertEqual(name, 'km_event_ack')
            self.assertEqual((params['session'], params['request'], params['seq']),
                             (IDENTITY.session, IDENTITY.request, SEQ))
            return ('km_command_result', dict(instance_hi=INSTANCE >> 32,
                                              instance_lo=INSTANCE & 0xffffffff,
                                              command=6, query=SEQ, result=0))

        retained = self.exchange(lambda: peek_event(self.mcu, self.reactor,
                                                     INSTANCE, lambda: True), respond)
        self.assertEqual((retained.identity, retained.seq, retained.value),
                         (IDENTITY, SEQ, 0xffffffff))
        delivery = EventDelivery(INSTANCE, 5)
        self.assertIsNotNone(delivery.observe(fields, 5, lambda event: event == retained))
        self.assertTrue(self.exchange(lambda: send_pending_ack(
            self.mcu, self.reactor, delivery, 5, lambda: True), respond))
        self.assertEqual(delivery.last_acked, SEQ)
        self.assertEqual([name for name, _, _ in self.mcu.serial.frames],
                         ['km_event_peek', 'km_event_ack'])

    def test_sensor_capture_uses_three_pinned_klipper_frames(self):
        query = 0xfedcba98
        base = dict(instance_hi=INSTANCE >> 32,
                    instance_lo=INSTANCE & 0xffffffff, query=query)
        replies = (
            ('km_sensor_flags', dict(base, lane_count=8,
                                     observed_ms=0xffffffff,
                                     configured=0x3ff, present=0x101,
                                     valid=0x3ff, fresh=0x1ff)),
            ('km_sensor_bays0', dict(base, sample0=0xffffffff,
                                     sample1=2, sample2=3, sample3=4)),
            ('km_sensor_bays1', dict(base, sample4=5, sample5=6,
                                     sample6=7, sample7=8,
                                     hub_sample_ms=0xffffffff,
                                     toolhead_sample_ms=0xffffffff)),
        )

        def respond(name, fields):
            self.assertEqual(name, 'km_sensor_part')
            self.assertEqual((fields['instance_hi'], fields['instance_lo'],
                              fields['query']),
                             (base['instance_hi'], base['instance_lo'], query))
            return replies[fields['part']]

        capture = self.exchange(
            lambda: read_sensors(self.mcu, self.reactor, INSTANCE, query,
                                 lambda: True), respond)
        self.assertEqual((capture.instance, capture.query, capture.lane_count),
                         (INSTANCE, query, 8))
        self.assertEqual(capture.bays[0].sample_ms, 0xffffffff)
        self.assertTrue(capture.bays[0].present)
        self.assertFalse(capture.bays[1].present)
        self.assertTrue(capture.hub.present)
        self.assertFalse(capture.toolhead.fresh)
        self.assertEqual([fields['part'] for _, fields, _ in
                          self.mcu.serial.frames], [0, 1, 2])
        self.assertTrue(all(size <= fixture.msgproto.MESSAGE_PAYLOAD_MAX
                            for _, _, size in self.mcu.serial.frames))

    def test_load_capable_sensor_capture_uses_five_pinned_klipper_frames(self):
        query = 0xfedcba98
        base = dict(instance_hi=INSTANCE >> 32,
                    instance_lo=INSTANCE & 0xffffffff, query=query)
        replies = (
            ('km_sensor_flags', dict(base, lane_count=8, observed_ms=42,
                                     configured=0x3ff, present=0x101,
                                     valid=0x3ff, fresh=0x1ff)),
            ('km_sensor_bays0', dict(base, sample0=1, sample1=2,
                                     sample2=3, sample3=4)),
            ('km_sensor_bays1', dict(base, sample4=5, sample5=6,
                                     sample6=7, sample7=8,
                                     hub_sample_ms=9, toolhead_sample_ms=10)),
            ('km_sensor_loads0', dict(base, configured=0x03, present=0x01,
                                      valid=0x03, fresh=0x01,
                                      sample0=11, sample1=12,
                                      sample2=0, sample3=0)),
            ('km_sensor_loads1', dict(base, sample4=0, sample5=0,
                                      sample6=0, sample7=0)),
        )

        def respond(name, fields):
            self.assertEqual(name, 'km_sensor_part')
            return replies[fields['part']]

        capture = self.exchange(
            lambda: read_sensors(self.mcu, self.reactor, INSTANCE, query,
                                 lambda: True, load_capable=True), respond)
        self.assertEqual(len(capture.loads), 8)
        self.assertTrue(capture.loads[0].present)
        self.assertFalse(capture.loads[1].present)
        self.assertEqual(capture.loads[0].sample_ms, 11)
        self.assertEqual([fields['part'] for _, fields, _ in
                          self.mcu.serial.frames], list(range(5)))
        self.assertTrue(all(size <= fixture.msgproto.MESSAGE_PAYLOAD_MAX
                            for _, _, size in self.mcu.serial.frames))

    def test_status_and_sensors_share_one_pinned_capture(self):
        query = 0xfedcba98
        base = dict(instance_hi=INSTANCE >> 32,
                    instance_lo=INSTANCE & 0xffffffff, query=query)
        status = (
            ('km_status_identity', dict(base, state=0, stage=0, operation=0,
                                        client=0, session=0, request=0, lane=255,
                                        owner_lane=255, occupant_lane=255,
                                        flags=0, queued_events=0, head_event_seq=0)),
            ('km_status_motion', dict(base, velocity_bits=0, position_hi=0,
                                      position_lo=0, buffer_position_bits=0,
                                      system_fault=0, motion_fault=0,
                                      follower_state=0, follower_direction=0,
                                      follower_fault=0)),
            ('km_status_time', dict(base, commanded_current_ma=0,
                                    measured_current_ma=0,
                                    measured_current_sample_ms=0,
                                    control_ms=0xffffffff,
                                    control_us=0xffffffff,
                                    acquired_ms=0xffffffff,
                                    acquired_us=0xffffffff)),
            ('km_status_sessions', dict(base, client0=0, client1=0xffffffff,
                                        client2=0, client3=0)),
            ('km_status_requests', dict(base, client0=0, client1=0xfffffffe,
                                        client2=0, client3=0)),
        )
        sensors = (
            ('km_sensor_flags', dict(base, lane_count=8,
                                     observed_ms=0xffffffff,
                                     configured=0x3ff, present=0x101,
                                     valid=0x3ff, fresh=0x1ff)),
            ('km_sensor_bays0', dict(base, sample0=0xffffffff, sample1=2,
                                     sample2=3, sample3=4)),
            ('km_sensor_bays1', dict(base, sample4=5, sample5=6,
                                     sample6=7, sample7=8,
                                     hub_sample_ms=0xffffffff,
                                     toolhead_sample_ms=0xffffffff)),
        )

        def respond(name, fields):
            self.assertEqual((fields['instance_hi'], fields['instance_lo'],
                              fields['query']),
                             (base['instance_hi'], base['instance_lo'], query))
            return (status if name == 'km_snapshot_part' else sensors)[fields['part']]

        (state, observation) = self.exchange(
            lambda: read_physical_state(self.mcu, self.reactor, INSTANCE,
                                        query, lambda: True), respond)
        self.assertEqual((state.instance, state.query,
                          observation.instance, observation.query),
                         (INSTANCE, query, INSTANCE, query))
        self.assertEqual(state.values['acquired_ms'], observation.observed_ms)
        self.assertEqual(observation.bays[0].sample_ms, 0xffffffff)
        self.assertEqual([(name, fields['part']) for name, fields, _ in
                          self.mcu.serial.frames],
                         [('km_snapshot_part', i) for i in range(5)] +
                         [('km_sensor_part', i) for i in range(3)])
        self.assertTrue(all(size <= fixture.msgproto.MESSAGE_PAYLOAD_MAX
                            for _, _, size in self.mcu.serial.frames))

        def different_time(name, fields):
            response, values = respond(name, fields)
            if response == 'km_sensor_flags':
                values = dict(values, observed_ms=0xfffffffe)
            return response, values

        with self.assertRaisesRegex(ValueError, 'acquisition times differ'):
            self.exchange(lambda: read_physical_state(
                self.mcu, self.reactor, INSTANCE, query, lambda: True),
                different_time)


if __name__ == '__main__':
    unittest.main()
