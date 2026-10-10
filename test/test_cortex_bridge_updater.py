# SPDX-License-Identifier: MIT
"""Tests for cortex_bridge/updater.py against a simulated Cortex Bridge.

Nothing here needs pyserial, a bridge or real time. A fake serial module lists
the ports, the clock moves only when the updater sleeps or a read times out,
and a model of the bridge's boot update receiver asks for the package in two
passes with the real frame format (encoded here independently of the module
under test). Every package is synthetic, built from the OAC2 header layout.
"""

from __future__ import annotations

import hashlib
import heapq
import importlib.util
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import types
import unittest
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(ROOT, "cortex_bridge", "updater.py")


def load_updater(name="cortex_bridge_updater_under_test"):
    # Loaded by path: the module is self-contained, and this keeps the test
    # independent of whatever the package's __init__ imports.
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


updater = load_updater()

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, utils
    HAVE_CRYPTOGRAPHY = True
except (KeyboardInterrupt, SystemExit):
    raise
except BaseException:      # missing, or a broken install (pyo3 panics)
    HAVE_CRYPTOGRAPHY = False


# ---- the frame format, written out again from its description ---------------

OPEN, READ, CLOSE = 0x0003, 0x0004, 0x0005
OPEN_REPLY, READ_REPLY, CLOSE_REPLY = 0x8003, 0x8004, 0x8005


def frame(message_type, sequence, payload=b""):
    head = struct.pack(">4sHHIII", b"IPGW", 1, message_type, sequence,
                       len(payload), zlib.crc32(payload) & 0xFFFFFFFF)
    return head + struct.pack(">I", zlib.crc32(head) & 0xFFFFFFFF) + payload


def unframe(data):
    magic, fmt, message_type, sequence, length, payload_crc, header_crc = \
        struct.unpack_from(">4sHHIIII", data)
    assert magic == b"IPGW" and fmt == 1, "bad frame magic or format"
    assert zlib.crc32(data[:20]) & 0xFFFFFFFF == header_crc, "bad header CRC"
    payload = bytes(data[24:24 + length])
    assert len(payload) == length, "short frame"
    assert zlib.crc32(payload) & 0xFFFFFFFF == payload_crc, "bad payload CRC"
    return message_type, sequence, payload


# ---- a synthetic OAC2 package ------------------------------------------------

SIGNATURE_DOMAIN = b"SiForge OAC2 v1 header"


def build_package(version=7, minimum_version=5, kind=1, blocks=4, boot_abi=6,
                  product_id=0x0101, hardware_revision=4, release_type=1,
                  sign=None):
    payload = bytearray()
    for index in range(blocks):
        payload += struct.pack(">IIII", index, index * 256, 256, 288)
        # Opaque stand-in for one record's 256 bytes and 16-byte tag.
        payload += bytes((index * 31 + offset) & 0xFF for offset in range(272))
    body = bytearray(512)
    body[0:4] = b"OAC2"
    struct.pack_into(">III", body, 4, 2, 512, 1)        # format, size, flags
    struct.pack_into(">II", body, 16, product_id, hardware_revision)
    struct.pack_into(">QQ", body, 24, version, minimum_version)
    struct.pack_into(">IIII", body, 40, blocks * 256, len(payload), 256,
                     blocks)
    # record and signature algorithms, key hint, app kind, epoch, table entries
    struct.pack_into(">IIIIII", body, 56, 1, 2, 1, kind, 0, 1)
    body[80:112] = bytes(range(1, 33))                    # release id
    body[144:176] = hashlib.sha256(b"image").digest()     # not used by the host
    body[176:208] = hashlib.sha256(bytes(payload)).digest()
    struct.pack_into(">IIII", body, 208, release_type, 0xA000, 0x15800,
                     boot_abi)
    body[256:320] = b"\x11" * 64                           # opaque table entry
    signature = sign(SIGNATURE_DOMAIN + bytes(body)) if sign else \
        bytes(range(64))
    return bytes(body) + signature + bytes(payload)


# ---- USB, as the host sees it ------------------------------------------------

class FakeSerialException(OSError):
    """pyserial's SerialException is an OSError too."""


class PortInfo(object):
    def __init__(self, device, pid, manufacturer, product, serial_number,
                 usb_device_path=None):
        self.device = device
        self.vid = 0x1A86
        self.pid = pid
        self.manufacturer = manufacturer
        self.product = product
        self.serial_number = serial_number
        self.usb_device_path = usb_device_path


class World(object):
    """The attached bridges, a sysfs tree and a clock that moves only when
    the updater sleeps or a read waits out its timeout."""

    def __init__(self, test):
        self.now = 1000.0
        self.sysfs = tempfile.mkdtemp(prefix="cortex-bridge-sysfs-")
        test.addCleanup(shutil.rmtree, self.sysfs, True)
        self.ports = {}
        self.units = []
        self.opened = []
        self._events = []
        self._order = 0
        list_ports = types.SimpleNamespace(
            comports=lambda: list(self.ports.values()))
        self.serial = types.SimpleNamespace(
            Serial=self.open_port, SerialException=FakeSerialException,
            tools=types.SimpleNamespace(list_ports=list_ports))

    def clock(self):
        return self.now

    def sleep(self, seconds):
        end = self.now + max(0.0, seconds)
        while self._events and self._events[0][0] <= end:
            when, _, action = heapq.heappop(self._events)
            self.now = max(self.now, when)
            action()
        self.now = end

    def later(self, delay, action):
        heapq.heappush(self._events, (self.now + delay, self._order, action))
        self._order += 1

    def open_port(self, device, baudrate=9600, **options):
        self.opened.append((device, baudrate))
        for unit in self.units:
            port = unit.open(device, baudrate, options)
            if port is not None:
                return port
        raise FakeSerialException("could not open port %s" % device)

    def add_unit(self, package, **options):
        unit = Unit(self, package, index=len(self.units), **options)
        self.units.append(unit)
        return unit

    def update(self, data, **options):
        options.setdefault("serial_module", self.serial)
        options.setdefault("clock", self.clock)
        options.setdefault("sleep", self.sleep)
        options.setdefault("sysfs_root", self.sysfs)
        return updater.update_bridge(data, **options)


class Unit(object):
    """One bridge: its firmware, its boot's update receiver or its fault
    device, always on the same USB port."""

    def __init__(self, world, package, index, uid="0123456789AB",
                 state="application", busy=False, accept=True,
                 after="application", after_cut="receiver",
                 fault_code="F-STATE-01", receiver_bcd="0200",
                 cut_after_reads=None, sysfs_paths=True):
        self.world = world
        self.package = package
        self.app_serial = "CB" + uid
        self.receiver_serial = uid[:10]       # the first five ID bytes
        self.app_device = "/dev/ttyACM%d" % (2 * index)
        self.receiver_device = "/dev/ttyACM%d" % (2 * index + 1)
        self.usb_name = "1-1.%d" % (index + 2)
        self.busy = busy
        self.accept = accept
        self.after = after
        self.after_cut = after_cut
        self.fault_code = fault_code
        self.receiver_bcd = receiver_bcd
        self.cut_after_reads = cut_after_reads
        self.sysfs_paths = sysfs_paths
        self.state = None
        self.session = None
        self.touches = 0
        self.boots = []
        self.show(state)

    @property
    def sysfs_dir(self):
        return os.path.join(self.world.sysfs, self.usb_name)

    def show(self, state):
        self.hide()
        self.state = state
        if state == "application":
            pid, identity, serial, device = (
                0xFE11, ("SiForge", "Cortex Bridge"), self.app_serial,
                self.app_device)
        elif state == "receiver":
            pid, identity, serial, device = (
                0xFE10, ("SiForge", "Cortex Updater"), self.receiver_serial,
                self.receiver_device)
        else:
            pid, identity, serial, device = (
                0xFE10, ("SiForge", "Cortex Fault"), self.fault_code, None)
        os.makedirs(self.sysfs_dir)
        files = {"idVendor": "1a86", "idProduct": "%04x" % pid,
                 "manufacturer": identity[0], "product": identity[1],
                 "serial": serial}
        if state == "receiver":
            files["bcdDevice"] = self.receiver_bcd
        for name, value in files.items():
            with open(os.path.join(self.sysfs_dir, name), "w") as handle:
                handle.write(value + "\n")
        if device is not None:            # a fault device has no serial port
            self.world.ports[device] = PortInfo(
                device, pid, identity[0], identity[1], serial,
                self.sysfs_dir if self.sysfs_paths else None)

    def hide(self):
        self.world.ports.pop(self.app_device, None)
        self.world.ports.pop(self.receiver_device, None)
        shutil.rmtree(self.sysfs_dir, ignore_errors=True)
        self.state = None
        self.session = None

    def open(self, device, baudrate, options):
        if self.state == "application" and device == self.app_device:
            return TouchPort(self, baudrate)
        if self.state == "receiver" and device == self.receiver_device:
            self.session = BootReceiver(self)
            self.boots.append(self.session)
            return ReceiverPort(self, self.session, options.get("timeout"))
        return None

    def touched(self):
        self.touches += 1
        if not self.busy:                 # a busy bridge refuses silently
            self.world.later(0.3, self.hide)
            self.world.later(1.3, lambda: self.show("receiver"))

    def finished(self, accepted):
        # The boot resets after the close; after a rejection it first shows
        # its fault indication for 1 s.
        self.world.later(0.1 if accepted else 1.0, self.hide)
        self.world.later(1.5 if accepted else 2.5,
                         lambda: self.show(self.after))

    def cut(self):
        self.hide()
        self.world.later(1.5, lambda: self.show(self.after_cut))


class TouchPort(object):
    """The application's port: it watches for DTR falling at 1200 baud."""

    def __init__(self, unit, baudrate):
        self.unit = unit
        self.baudrate = baudrate
        self._dtr = True                  # pyserial raises DTR on open
        self.closed = False

    @property
    def dtr(self):
        return self._dtr

    @dtr.setter
    def dtr(self, value):
        if self._dtr and not value and self.baudrate == 1200 and \
                self.unit.state == "application":
            self.unit.touched()
        self._dtr = bool(value)

    def close(self):
        self.closed = True


class ReceiverPort(object):
    def __init__(self, unit, boot, timeout):
        self.unit = unit
        self.boot = boot
        self.timeout = timeout
        self.closed = False

    def _check(self):
        if self.closed or self.unit.session is not self.boot:
            raise FakeSerialException("device disconnected")

    def read(self, size=1):
        self._check()
        if not self.boot.outgoing:
            self.unit.world.sleep(self.timeout or 0.25)
            return b""
        size = min(size, 64, len(self.boot.outgoing))     # USB packets
        chunk = bytes(self.boot.outgoing[:size])
        del self.boot.outgoing[:size]
        return chunk

    def write(self, data):
        self._check()
        self.boot.receive(bytes(data))
        return len(data)

    def flush(self):
        self._check()

    def close(self):
        self.closed = True


class BootReceiver(object):
    """The bridge's boot update receiver. It is the requester: it opens the
    package, reads the header and every record (authentication pass), reads
    it all again from offset 0 (write pass) and closes with its verdict. A
    rejecting boot closes with 0 after the first pass."""

    def __init__(self, unit):
        self.unit = unit
        self.outgoing = bytearray()
        self.incoming = bytearray()
        self.sequence = 0
        self.reads = 0
        self.passes = []
        self.closed_with = None
        reads = [(0, 320), (320, 256)]
        offset = 576
        while offset < len(unit.package):
            reads.append((offset, 288))
            offset += 288
        self.plan = [("open",)] + [("read",) + item for item in reads]
        if unit.accept:
            self.plan += [("read",) + item for item in reads]
        self.plan.append(("close", 1 if unit.accept else 0))
        self._ask()

    def _ask(self):
        step = self.plan[0]
        self.sequence += 1
        if step[0] == "open":
            request = frame(OPEN, self.sequence)
        elif step[0] == "read":
            request = frame(READ, self.sequence,
                            struct.pack(">II", step[1], step[2]))
        else:
            request = frame(CLOSE, self.sequence, struct.pack(">I", step[1]))
        self.outgoing += request

    def receive(self, data):
        self.incoming += data
        while len(self.incoming) >= 24:
            length = struct.unpack_from(">I", self.incoming, 12)[0]
            if len(self.incoming) < 24 + length:
                return
            reply = bytes(self.incoming[:24 + length])
            del self.incoming[:24 + length]
            self._answered(reply)

    def _answered(self, reply):
        message_type, sequence, payload = unframe(reply)
        step = self.plan.pop(0)
        package = self.unit.package
        assert sequence == self.sequence, "the reply must echo the sequence"
        if step[0] == "open":
            assert message_type == OPEN_REPLY
            assert payload == struct.pack(">I", len(package)) + \
                hashlib.sha256(package).digest()
        elif step[0] == "read":
            offset, capacity = step[1], step[2]
            assert message_type == READ_REPLY
            echoed, amount = struct.unpack_from(">II", payload)
            data = payload[8:]
            assert echoed == offset, "the reply must echo the offset"
            assert amount == len(data) == min(capacity, len(package) - offset)
            assert data == package[offset:offset + amount], "wrong bytes"
            if offset == 0:
                self.passes.append(bytearray())
            self.passes[-1] += data
            self.reads += 1
        else:
            assert message_type == CLOSE_REPLY and payload == b""
            self.closed_with = step[1]
            self.unit.finished(step[1] == 1)
            return
        if self.unit.cut_after_reads is not None and \
                self.reads >= self.unit.cut_after_reads:
            self.unit.world.later(0.05, self.unit.cut)
            return
        self._ask()


# ---- the tests -------------------------------------------------------------

class UpdateBridgeTest(unittest.TestCase):
    def setUp(self):
        self.package = build_package()
        self.world = World(self)
        self.phases = []

    def progress(self, phase, done, total):
        self.phases.append((phase, done, total))

    def phase_order(self):
        order = []
        for phase, _, _ in self.phases:
            if not order or order[-1] != phase:
                order.append(phase)
        return order

    def test_successful_update(self):
        unit = self.world.add_unit(self.package)
        report = self.world.update(self.package, progress=self.progress)

        self.assertEqual(report["result"], "installed")
        self.assertEqual(report["version"], 7)
        self.assertEqual(report["passes"], 2)
        self.assertEqual(report["opens"], 1)
        self.assertEqual(report["bytes_served"], 2 * len(self.package))
        self.assertTrue(report["accepted"])
        self.assertFalse(report["resumed"])
        self.assertEqual(report["final_state"], "application")
        self.assertEqual(report["device"], unit.app_device)
        self.assertEqual(report["serial_number"], unit.app_serial)
        self.assertEqual(report["receiver_device"], unit.receiver_device)
        self.assertEqual(set(report["durations"]),
                         {"entering", "transfer", "restarting", "total"})
        json.dumps(report)                 # the relay sends it as JSON

        self.assertEqual(unit.touches, 1)
        self.assertEqual(len(unit.boots), 1)
        boot = unit.boots[0]
        self.assertEqual([bytes(item) for item in boot.passes],
                         [self.package, self.package])
        self.assertEqual(boot.closed_with, 1)
        self.assertEqual(self.world.opened, [(unit.app_device, 1200),
                                             (unit.receiver_device, 115200)])

        self.assertEqual(self.phase_order(), [
            "entering", "waiting-receiver", "authenticating", "writing",
            "restarting", "done"])
        writing = [item for item in self.phases if item[0] == "writing"]
        self.assertEqual(writing[-1][1:], (len(self.package),
                                           len(self.package)))
        self.assertEqual(self.phases[-1], ("done", 1, 1))

    def test_refused_touch_raises_update_mode_refused(self):
        unit = self.world.add_unit(self.package, busy=True)
        started = self.world.now
        with self.assertRaises(updater.UpdateModeRefused):
            self.world.update(self.package)
        waited = self.world.now - started
        self.assertGreaterEqual(waited, 5.0)
        self.assertLess(waited, 6.0)
        self.assertEqual(unit.touches, 1)
        self.assertEqual(unit.state, "application")
        self.assertEqual(unit.boots, [])

    def test_non_oac2_file_is_refused_before_any_device(self):
        unit = self.world.add_unit(self.package)
        for data in (b"", b"\x7fELF" + bytes(2000), b"OAC1" + bytes(700),
                     self.package[:-1]):
            with self.assertRaises(updater.PackageRefused):
                self.world.update(data)
        self.assertEqual(self.world.opened, [])
        self.assertEqual(unit.touches, 0)

    def test_receiver_rejecting_at_close_raises_receiver_rejected(self):
        unit = self.world.add_unit(self.package, accept=False)
        with self.assertRaises(updater.ReceiverRejected) as caught:
            self.world.update(self.package)
        error = caught.exception
        self.assertEqual(error.post_state, "application")
        self.assertIn("authentication pass", error.stopped_at)
        self.assertIs(error.report["accepted"], False)
        self.assertEqual(error.report["passes"], 1)
        self.assertEqual(unit.boots[0].closed_with, 0)
        self.assertEqual(unit.state, "application")

    def test_fault_after_transfer_raises_bridge_fault_with_code(self):
        unit = self.world.add_unit(self.package, after="fault",
                                   fault_code="F-STATE-01")
        with self.assertRaises(updater.BridgeFault) as caught:
            self.world.update(self.package)
        self.assertEqual(caught.exception.code, "F-STATE-01")
        self.assertIn("F-STATE-01", str(caught.exception))
        self.assertEqual(caught.exception.report["final_state"], "fault")
        self.assertEqual(unit.boots[0].closed_with, 1)

    def test_two_bridges_without_a_filter_are_ambiguous(self):
        first = self.world.add_unit(self.package, uid="0123456789AB")
        second = self.world.add_unit(self.package, uid="A1B2C3D4E5F6")
        with self.assertRaises(updater.BridgeAmbiguous):
            self.world.update(self.package)
        self.assertEqual(self.world.opened, [])

        report = self.world.update(self.package,
                                   serial_number=second.app_serial)
        self.assertEqual(report["serial_number"], second.app_serial)
        self.assertEqual(report["device"], second.app_device)
        self.assertEqual((first.touches, second.touches), (0, 1))

    def test_serial_filter_follows_the_bridge_into_its_receiver(self):
        # Without USB paths the receiver is recognised by its serial number,
        # the first ten digits of the application's.
        first = self.world.add_unit(self.package, uid="0123456789AB",
                                    sysfs_paths=False)
        self.world.add_unit(self.package, uid="A1B2C3D4E5F6",
                            sysfs_paths=False)
        report = self.world.update(self.package,
                                   serial_number=first.app_serial.lower())
        self.assertEqual(report["device"], first.app_device)
        self.assertEqual(report["receiver_device"], first.receiver_device)
        self.assertEqual(report["notes"], [])
        self.assertEqual(updater.receiver_serial_for(first.app_serial),
                         first.receiver_serial)

    def test_bridge_already_in_its_receiver_is_resumed(self):
        unit = self.world.add_unit(self.package, state="receiver")
        report = self.world.update(self.package, progress=self.progress)
        self.assertEqual(report["result"], "installed")
        self.assertTrue(report["resumed"])
        self.assertEqual(report["serial_number"], unit.app_serial)
        self.assertEqual(unit.touches, 0)
        self.assertEqual(self.world.opened, [(unit.receiver_device, 115200)])
        self.assertEqual(self.phase_order()[0], "waiting-receiver")

    def test_cut_transfer_is_not_resent(self):
        unit = self.world.add_unit(self.package, cut_after_reads=8)
        with self.assertRaises(updater.TransferInterrupted) as caught:
            self.world.update(self.package)
        self.assertEqual(caught.exception.post_state, "receiver")
        self.assertEqual(caught.exception.report["passes"], 2)
        self.assertEqual(len(unit.boots), 1)
        self.assertEqual(unit.state, "receiver")

    def test_receiver_of_another_boot_format_gets_nothing(self):
        unit = self.world.add_unit(self.package, receiver_bcd="0100")
        with self.assertRaises(updater.PackageRefused):
            self.world.update(self.package)
        self.assertEqual(unit.touches, 1)
        self.assertEqual(unit.boots, [])

    def test_bridge_in_fault_is_reported_without_a_touch(self):
        self.world.add_unit(self.package, state="fault",
                            fault_code="F-BOARD-01")
        with self.assertRaises(updater.BridgeFault) as caught:
            self.world.update(self.package)
        self.assertEqual(caught.exception.code, "F-BOARD-01")
        self.assertEqual(self.world.opened, [])

    def test_no_bridge(self):
        with self.assertRaises(updater.BridgeNotFound):
            self.world.update(self.package)

    def test_failing_progress_callback_does_not_stop_the_install(self):
        self.world.add_unit(self.package)

        def broken(phase, done, total):
            raise RuntimeError("display gone")

        report = self.world.update(self.package, progress=broken)
        self.assertEqual(report["result"], "installed")

    def test_find_bridges_reports_each_state(self):
        app = self.world.add_unit(self.package, uid="0123456789AB")
        receiver = self.world.add_unit(self.package, uid="A1B2C3D4E5F6",
                                       state="receiver")
        self.world.add_unit(self.package, uid="111111111111", state="fault",
                            fault_code="F-USB-01")
        found = updater.find_bridges(self.world.serial,
                                     sysfs_root=self.world.sysfs)
        by_state = {bridge.state: bridge for bridge in found}
        self.assertEqual(sorted(by_state),
                         ["application", "fault", "receiver"])
        self.assertEqual(by_state["application"].device, app.app_device)
        self.assertEqual(by_state["application"].pid, 0xFE11)
        self.assertEqual(by_state["application"].product, "Cortex Bridge")
        self.assertEqual(by_state["receiver"].serial_number,
                         receiver.receiver_serial)
        self.assertEqual(by_state["receiver"].manufacturer, "SiForge")
        self.assertIsNone(by_state["fault"].device)
        self.assertEqual(by_state["fault"].code, "F-USB-01")


class InspectPackageTest(unittest.TestCase):
    def test_valid_synthetic_header(self):
        package = build_package(version=9, minimum_version=3, blocks=5,
                                product_id=0x0101, hardware_revision=4)
        info = updater.inspect_package(package)
        self.assertEqual(info["magic"], "OAC2")
        self.assertEqual(info["version"], 9)
        self.assertEqual(info["minimum_version"], 3)
        self.assertEqual(info["app_kind"], 1)
        self.assertEqual(info["app_kind_name"], "bridge")
        self.assertEqual(info["product_id"], 0x0101)
        self.assertEqual(info["hardware_revision"], 4)
        self.assertEqual(info["required_boot_abi"], 6)
        self.assertEqual(info["payload_size"], 5 * 288)
        self.assertEqual(info["file_size"], 576 + 5 * 288)
        self.assertEqual(info["sha256"], hashlib.sha256(package).hexdigest())
        self.assertIs(info["signature_checked"], False)
        json.dumps(info)

    def test_refusals(self):
        good = build_package()
        damaged = bytearray(good)
        damaged[-1] ^= 1
        reserved = bytearray(good)
        reserved[240] = 1
        cases = {
            "another kind": build_package(kind=2),
            "unknown kind": build_package(kind=9),
            "not an application": build_package(release_type=2),
            "older than its minimum": build_package(version=3,
                                                    minimum_version=4),
            "truncated header": good[:500],
            "truncated records": good[:-1],
            "trailing bytes": good + b"\0",
            "damaged record": bytes(damaged),
            "reserved bytes": bytes(reserved),
            "too large": build_package(blocks=2100),
        }
        for name, data in cases.items():
            with self.subTest(name):
                with self.assertRaises(updater.PackageRefused):
                    updater.inspect_package(data)

    @unittest.skipUnless(HAVE_CRYPTOGRAPHY, "cryptography is not installed")
    def test_signature_checked_with_a_public_key(self):
        key = _private_key()
        package = build_package(sign=_signer(key))
        info = updater.inspect_package(package, _raw_public(key))
        self.assertIs(info["signature_checked"], True)
        pem = key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo)
        self.assertIs(
            updater.inspect_package(package, pem)["signature_checked"], True)
        with self.assertRaises(updater.PackageRefused):
            updater.inspect_package(package, _raw_public(_private_key()))

    def test_signature_skipped_without_cryptography(self):
        result = run_isolated(BLOCK_CRYPTOGRAPHY % "ImportError" + r'''
info = updater.inspect_package(package, bytes(64))
print(info["signature_checked"], "cryptography" in info["signature_note"])
''', build_package())
        self.assertEqual(result, "False True")

    def test_broken_cryptography_install_is_skipped(self):
        # A binding built for another Python fails with a BaseException
        # (pyo3's PanicException), not ImportError.
        result = run_isolated(BLOCK_CRYPTOGRAPHY % "Panic" + r'''
info = updater.inspect_package(package, bytes(64))
print(info["signature_checked"])
''', build_package())
        self.assertEqual(result, "False")


class ImportTest(unittest.TestCase):
    def test_imports_and_inspects_without_pyserial(self):
        result = run_isolated(r'''
info = updater.inspect_package(package)
try:
    updater.find_bridges()
except updater.UpdateError:
    print(info["app_kind_name"], "needs pyserial")
''', build_package(), block_serial=True)
        self.assertEqual(result, "bridge needs pyserial")


# Each import check runs in a fresh interpreter, so this process's module
# table is never touched. The script gets the module's path and the package.
ISOLATED_PRELUDE = r'''
import importlib.util, sys
%s
spec = importlib.util.spec_from_file_location("updater", sys.argv[1])
updater = importlib.util.module_from_spec(spec)
sys.modules["updater"] = updater
spec.loader.exec_module(updater)
package = sys.stdin.buffer.read()
'''

BLOCK_CRYPTOGRAPHY = r'''
class Panic(BaseException):
    pass

class Blocked(object):
    def find_spec(self, name, path=None, target=None):
        if name == "cryptography" or name.startswith("cryptography."):
            raise %s("blocked for this test")
        return None

sys.meta_path.insert(0, Blocked())
'''


def run_isolated(script, package, block_serial=False):
    prelude = ISOLATED_PRELUDE % (
        'sys.modules["serial"] = None  # as if pyserial were not installed'
        if block_serial else "")
    completed = subprocess.run(
        [sys.executable, "-I", "-c", prelude + script, MODULE_PATH],
        input=package, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=60)
    if completed.returncode != 0:
        raise AssertionError(completed.stderr.decode("utf-8", "replace"))
    return completed.stdout.decode("ascii").strip()


def _private_key():
    try:
        return ec.generate_private_key(ec.SECP256R1())
    except TypeError:          # cryptography before 3.1 needs a backend
        from cryptography.hazmat.backends import default_backend
        return ec.generate_private_key(ec.SECP256R1(), default_backend())


def _signer(key):
    def sign(message):
        r, s = utils.decode_dss_signature(
            key.sign(message, ec.ECDSA(hashes.SHA256())))
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return sign


def _raw_public(key):
    numbers = key.public_key().public_numbers()
    return numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")


if __name__ == "__main__":
    unittest.main()
