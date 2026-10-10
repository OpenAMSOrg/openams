# SPDX-License-Identifier: GPL-3.0-or-later
"""Run with KLIPPER_SOURCE pointing to a source checkout; never touches hardware."""
import importlib.util
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from host import protocol as P

INSTANCE = (1 << 64) - 1
QUERY = (1 << 32) - 1


class MessagesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(os.environ["KLIPPER_SOURCE"])
        spec = importlib.util.spec_from_file_location("upstream_msgproto", source / "klippy/msgproto.py")
        cls.codec = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.codec)
        cls.work = tempfile.TemporaryDirectory(prefix="km-messages-")
        cls.addClassCleanup(cls.work.cleanup)
        binary = Path(cls.work.name) / "capture"
        subprocess.run([
            os.environ.get("CC", "gcc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
            "-Wconversion", "-Wsign-conversion", "-pedantic", "-g", "-fno-pie", "-no-pie",
            "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
            "-I" + str(ROOT / "tests/messages"), "-I" + str(ROOT / "include"),
            "-I" + str(ROOT / "mcu"), str(ROOT / "mcu/messages.c"),
            str(ROOT / "tests/messages/capture.c"), "-o", str(binary)], check=True, timeout=30)
        output = subprocess.check_output([str(binary)], text=True, timeout=10,
            env=dict(os.environ, ASAN_OPTIONS="halt_on_error=1:detect_leaks=1",
                     UBSAN_OPTIONS="halt_on_error=1:abort_on_error=1"))
        cls.records = [json.loads(line) for line in output.splitlines()]
        cls.formats = {record["format"].split()[0]: record["format"] for record in cls.records}
        cls.decoded = []
        for record in cls.records:
            # Five-byte ID is a conservative envelope; real generated IDs are
            # smaller. Exercise the actual upstream integer codec in either case.
            msgid = []
            cls.codec.PT_uint32().encode(msgid, 0xffffffff)
            fmt = cls.codec.MessageFormat(msgid, record["format"])
            payload = fmt.encode(record["values"])
            frame = [len(payload) + cls.codec.MESSAGE_MIN, cls.codec.MESSAGE_DEST] + payload
            frame += cls.codec.crc16_ccitt(frame) + [cls.codec.MESSAGE_SYNC]
            if cls.codec.MessageParser().check_packet(frame) != len(frame):
                raise AssertionError("Invalid upstream frame")
            params, end = fmt.parse(frame, cls.codec.MESSAGE_HEADER_SIZE)
            if end != len(frame) - cls.codec.MESSAGE_TRAILER_SIZE:
                raise AssertionError("Incorrect parsed length")
            cls.decoded.append((fmt.name, params))
        cls.parts = dict(cls.decoded[5:10])

    def snapshot(self, parts=None):
        assembler = P.SnapshotAssembler(INSTANCE, QUERY)
        result = None
        for name, params in (self.parts if parts is None else parts).items():
            result = assembler.add(name, params)
        return result

    def test_frame_capacity(self):
        for name, text in self.formats.items():
            with self.subTest(name=name):
                fmt = self.codec.MessageFormat([1] * 5, text)
                theoretical = 5 + 5 + sum(t.max_length for t in fmt.param_types)
                self.assertLessEqual(theoretical, self.codec.MESSAGE_MAX)
                values = [-(1 << 31) if t.signed else
                          255 if t.max_length == 2 else 65535 if t.max_length == 3 else 0xffffffff
                          for t in fmt.param_types]
                self.assertLessEqual(len(fmt.encode(values)) + 5, self.codec.MESSAGE_MAX)

    def test_full_identity_and_admission_is_not_terminal(self):
        for (_, params), code in zip(self.decoded[:3], (0, 10, 3)):
            event = P.decode_admission(params)
            self.assertIsInstance(event, P.Admission)
            self.assertNotIsInstance(event, P.Event)
            self.assertEqual(event.instance, INSTANCE)
            self.assertEqual(event.identity.session, 0xffffffff)
            self.assertEqual(event.identity.request, 0xfffffffe)
            self.assertEqual(event.admission, code)
        self.assertEqual(P.decode_admission(self.decoded[2][1]).identity.lane, 255)
        with self.assertRaises(ValueError):
            P.decode_event(self.decoded[0][1])

    def test_terminal_and_unsolicited_event(self):
        event = P.decode_event(self.decoded[3][1])
        self.assertEqual(event.identity, P.Identity(1, 0xffffffff, 0xfffffffe, 1, 7))
        self.assertEqual((event.seq, event.timestamp_ms, event.value), (0xffffffff, 0x80000000, 0xffffffff))
        self.assertEqual((event.event_type, event.result_code, event.flags), (1, 0, 1))
        unsolicited = P.decode_event(self.decoded[4][1])
        self.assertEqual(unsolicited.identity, P.Identity(0, 0, 0, 0, 7))
        self.assertEqual(unsolicited.event_type, 12)

    def test_snapshot_content_and_observation_times(self):
        s = self.snapshot()
        self.assertIsInstance(s, P.Snapshot)
        self.assertNotIsInstance(s, P.Event)
        self.assertEqual((s.instance, s.query), (INSTANCE, QUERY))
        v = s.values
        self.assertEqual((v["session"], v["request"], v["head_event_seq"]),
                         (0xffffffff, 0xfffffffe, 0xffffffff))
        self.assertEqual((v["state"], v["stage"], v["operation"], v["client"]), (4, 3, 1, 1))
        self.assertEqual((v["lane"], v["owner_lane"], v["occupant_lane"]), (7, 7, 6))
        self.assertEqual((v["velocity"], v["buffer_position"]), (-(1 << 31), (1 << 31) - 1))
        self.assertEqual((v["system_fault"], v["motion_fault"], v["queued_events"]), (11, 9, 16))
        self.assertEqual((v["control_ms"], v["control_us"]), (0x89abcdef, 0xfedcba98))
        self.assertEqual((v["acquired_ms"], v["acquired_us"]), (0x10203040, 0x50607080))
        self.assertEqual(v["client_sessions"], (0, 0xffffffff, 0x80000000, 0x100))
        self.assertEqual(v["client_requests"], (0, 0xfffffffe, 0x7fffffff, 0x101))
        self.assertEqual((v["commanded_current_ma"], v["measured_current_ma"], v["measured_current_sample_ms"]),
                         (65535, 1234, 0x12345678))
        self.assertFalse(v["measured_current_available"])
        self.assertEqual((v["follower_state"], v["follower_direction"],
                          v["follower_fault"]), (1, 1, 0))

    def test_follower_state_is_retained_independent_of_motor_pulses(self):
        disarmed = P.SnapshotAssembler(INSTANCE, 123)
        for name, params in self.decoded[45:50]:
            result = disarmed.add(name, params)
        self.assertEqual((result.values["follower_state"],
                          result.values["follower_direction"]), (0, 0))
        faulted = P.SnapshotAssembler(INSTANCE, 124)
        for name, params in self.decoded[100:105]:
            result = faulted.add(name, params)
        self.assertEqual((result.values["follower_state"],
                          result.values["follower_fault"]), (3, 11))
        for field, invalid in (("follower_state", 4), ("follower_direction", 2)):
            assembler = P.SnapshotAssembler(INSTANCE, QUERY)
            with self.assertRaisesRegex(ValueError, 'follower controller status'):
                assembler.add('km_status_motion',
                              dict(self.parts['km_status_motion'], **{field: invalid}))

    def test_signed_positions(self):
        expected = (-(1 << 63), (1 << 63) - 1, -1, 0, 1, -(1 << 32), 1 << 32, -1000000000000)
        for i, position in enumerate(expected):
            assembler = P.SnapshotAssembler(INSTANCE, QUERY - i)
            for name, params in self.decoded[5 + i * 5:10 + i * 5]:
                result = assembler.add(name, params)
            self.assertEqual(result.values["position"], position)
            self.assertEqual(result.values["velocity"], (1 << 31) - 1 if i % 2 else -(1 << 31))
            self.assertEqual(result.values["buffer_position"], -(1 << 31) if i % 2 else (1 << 31) - 1)

    def test_flags_are_independent(self):
        expected = {
            "lane_active": True, "terminal_pending": False, "path_occupied": True,
            "position_uncertain": True, "buffer_valid": False, "measured_current_available": False,
            "host_connected": False, "motor_ready": True, "control_time_valid": True,
            "pending_fault_event": True,
        }
        first = self.snapshot().values
        other = P.SnapshotAssembler(INSTANCE, 123)
        for name, params in self.decoded[45:50]:
            result = other.add(name, params)
        for key, value in expected.items():
            self.assertIs(first[key], value)
            self.assertIs(result.values[key], not value)
        for bit, true_key in enumerate(expected):
            assembler = P.SnapshotAssembler(INSTANCE, 200 + bit)
            for name, params in self.decoded[50 + bit * 5:55 + bit * 5]:
                result = assembler.add(name, params)
            for key in expected:
                self.assertIs(result.values[key], key == true_key)

    def test_every_fragment_order(self):
        for order in itertools.permutations(self.parts):
            assembler = P.SnapshotAssembler(INSTANCE, QUERY)
            for name in order[:-1]:
                self.assertIsNone(assembler.add(name, self.parts[name]))
            self.assertIsInstance(assembler.add(order[-1], self.parts[order[-1]]), P.Snapshot)

    def test_unknown_occupied_source_is_preserved(self):
        assembler = P.SnapshotAssembler(INSTANCE, QUERY)
        for name, params in self.parts.items():
            if name == "km_status_identity":
                params = dict(params, occupant_lane=255)
            result = assembler.add(name, params)
        self.assertTrue(result.values["path_occupied"])
        self.assertEqual(result.values["occupant_lane"], 255)

    def test_missing_part_and_identical_replays(self):
        for missing in self.parts:
            assembler = P.SnapshotAssembler(INSTANCE, QUERY)
            for name, params in self.parts.items():
                if name != missing:
                    self.assertIsNone(assembler.add(name, params))
                    self.assertIsNone(assembler.add(name, dict(params, **{"#receive_time": 123})))
            self.assertIsInstance(assembler.add(missing, self.parts[missing]), P.Snapshot)
            for name, params in self.parts.items():
                self.assertIsNone(assembler.add(name, params))

    def test_old_query_or_controller_cannot_mix(self):
        assembler = P.SnapshotAssembler(INSTANCE, QUERY)
        for name, params in self.parts.items():
            self.assertIsNone(assembler.add(name, dict(params, query=QUERY - 1)))
            self.assertIsNone(assembler.add(name, dict(params, instance_lo=0)))
        self.assertEqual(assembler.parts, {})
        for name, params in self.parts.items():
            result = assembler.add(name, params)
        self.assertIsInstance(result, P.Snapshot)

    def test_conflicting_duplicate_invalidates_query(self):
        for name, params in self.parts.items():
            assembler = P.SnapshotAssembler(INSTANCE, QUERY)
            assembler.add(name, params)
            changed = dict(params)
            key = next(k for k in changed if k not in ("query", "instance_hi", "instance_lo"))
            changed[key] ^= 1
            with self.assertRaises(ValueError):
                assembler.add(name, changed)
            with self.assertRaises(ValueError):
                assembler.add(name, params)

    def test_invalid_widths_and_missing_fields(self):
        for decoder, params in ((P.decode_event, self.decoded[3][1]),
                                (P.decode_admission, self.decoded[0][1])):
            for key in params:
                for invalid in (None, True, "1", 1.0, -1, 1 << 32):
                    with self.subTest(key=key, value=invalid), self.assertRaises(ValueError):
                        decoder(dict(params, **{key: invalid}))
            with self.assertRaises(ValueError):
                decoder(dict(params, lane=256))
        for name, params in self.parts.items():
            for key in params:
                if key in ("query", "instance_hi", "instance_lo"):
                    continue
                assembler = P.SnapshotAssembler(INSTANCE, QUERY)
                broken = dict(params)
                del broken[key]
                with self.assertRaises(ValueError):
                    assembler.add(name, broken)
                with self.assertRaises(ValueError):
                    assembler.add(name, params)

    def test_invalid_context_or_flags(self):
        for value in (0, -1, True, 1.0, 1 << 64):
            with self.assertRaises(ValueError):
                P.SnapshotAssembler(value, QUERY)
        for value in (0, -1, True, 1.0, 1 << 32):
            with self.assertRaises(ValueError):
                P.SnapshotAssembler(INSTANCE, value)
        with self.assertRaises(ValueError):
            P.decode_event(dict(self.decoded[3][1], seq=0))
        assembler = P.SnapshotAssembler(INSTANCE, QUERY)
        with self.assertRaises(ValueError):
            assembler.add("km_admission", self.decoded[0][1])
        with self.assertRaises(ValueError):
            assembler.add("km_status_identity", dict(self.parts["km_status_identity"], flags=1024))

    def test_sensor_observation_round_trip_and_unknown_state(self):
        parts = dict(self.decoded[-3:])
        assembler = P.SensorSnapshotAssembler(INSTANCE, QUERY)
        for name, params in parts.items():
            result = assembler.add(name, params)
        self.assertEqual((result.instance, result.query, result.lane_count, result.observed_ms),
                         (INSTANCE, QUERY, 8, 0x10203040))
        self.assertEqual(result.bays[0], P.SensorSample(True, True, True, True, QUERY))
        self.assertEqual(result.bays[1], P.SensorSample(True, False, False, False, 0))
        self.assertEqual(result.bays[7], P.SensorSample(True, True, True, False, 0x80000000))
        self.assertEqual(result.hub, P.SensorSample(True, True, True, True, 23))
        self.assertEqual(result.toolhead, P.SensorSample(False, False, False, False, 0))
        self.assertFalse(result.bays[1].valid)
        self.assertFalse(result.bays[7].fresh)

    def test_load_parts_retained_identity_masks_and_samples(self):
        first, second = self.decoded[-5:-3]
        self.assertEqual((first[0], second[0]), ('km_sensor_loads0', 'km_sensor_loads1'))
        for _, params in (first, second):
            self.assertEqual((params['instance_hi'], params['instance_lo'], params['query']),
                             (0xffffffff, 0xffffffff, QUERY))
        flags = first[1]
        self.assertEqual(tuple(flags[key] for key in ('configured', 'present', 'valid', 'fresh')),
                         (0x85, 0x81, 0x05, 0x01))
        self.assertEqual(tuple(flags[f'sample{i}'] for i in range(4)),
                         (QUERY, 0, 0x80000000, 0))
        self.assertEqual(tuple(second[1][f'sample{i}'] for i in range(4, 8)),
                         (0, 0, 0, 0x12345678))

    def test_sensor_parts_reject_missing_and_conflicting_data(self):
        parts = dict(self.decoded[-3:])
        for missing in parts:
            assembler = P.SensorSnapshotAssembler(INSTANCE, QUERY)
            for name, params in parts.items():
                if name != missing:
                    self.assertIsNone(assembler.add(name, params))
            self.assertIsNotNone(assembler.add(missing, parts[missing]))
        assembler = P.SensorSnapshotAssembler(INSTANCE, QUERY)
        flags = parts['km_sensor_flags']
        self.assertIsNone(assembler.add('km_sensor_flags', flags))
        with self.assertRaises(ValueError):
            assembler.add('km_sensor_flags', dict(flags, present=flags['present'] ^ 1))
        with self.assertRaises(ValueError):
            assembler.add('km_sensor_bays0', parts['km_sensor_bays0'])
        for bad in (dict(flags, fresh=flags['fresh'] | 2),
                    dict(flags, configured=flags['configured'] | (1 << 10)),
                    dict(flags, lane_count=9)):
            with self.assertRaises(ValueError):
                P.SensorSnapshotAssembler(INSTANCE, QUERY).add('km_sensor_flags', bad)

    def test_eight_lane_snapshot_sends_exactly_old_parts(self):
        # An 8-lane image (KM_SENSOR_BAYS2/LOADS2 never requested) must emit
        # only the five original sensor parts, byte-identical to before.
        names = [name for name, _ in self.decoded[-5:]]
        self.assertEqual(names, ['km_sensor_loads0', 'km_sensor_loads1',
                                 'km_sensor_flags', 'km_sensor_bays0', 'km_sensor_bays1'])

    def test_wide_sensor_round_trip_lanes_8_to_11(self):
        parts = dict(self.decoded[-12:-5])
        self.assertEqual(set(parts), {
            'km_sensor_flags', 'km_sensor_bays0', 'km_sensor_bays1', 'km_sensor_bays2',
            'km_sensor_loads0', 'km_sensor_loads1', 'km_sensor_loads2'})
        assembler = P.SensorSnapshotAssembler(INSTANCE, 55, load_capable=True, wide_lanes=True)
        result = None
        for name, params in parts.items():
            result = assembler.add(name, params)
        self.assertEqual((result.lane_count, result.observed_ms), (12, 0x22334455))
        self.assertEqual(result.bays[0], P.SensorSample(True, True, True, True, 1))
        self.assertEqual(result.bays[8], P.SensorSample(True, True, True, False, 0x11111111))
        self.assertEqual(result.bays[9], P.SensorSample(True, False, False, False, 0))
        self.assertEqual(result.bays[10], P.SensorSample(True, True, False, False, 0x22222222))
        self.assertEqual(result.bays[11], P.SensorSample(False, False, False, False, 0))
        self.assertEqual(result.hub, P.SensorSample(True, True, True, True, 99))
        self.assertEqual(result.toolhead, P.SensorSample(True, False, False, False, 0))
        self.assertEqual(len(result.loads), 12)
        self.assertEqual(result.loads[8], P.SensorSample(True, True, True, True, 0x33333333))
        self.assertEqual(result.loads[9], P.SensorSample(True, False, True, False, 0))
        self.assertEqual(result.loads[10], P.SensorSample(False, False, False, False, 0))
        self.assertEqual(result.loads[11], P.SensorSample(True, True, False, False, 0x44444444))


if __name__ == "__main__":
    unittest.main()
