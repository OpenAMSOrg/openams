# SPDX-License-Identifier: MIT
"""Install a firmware package on a Cortex Bridge over USB.

The bridge's firmware ships as an OAC2 package (``.oac2``): a 576-byte header
(a 512-byte body and its 64-byte P-256 signature) followed by encrypted
records. This module carries that file from the host to the bridge's boot
update receiver and reports what the bridge became. The host is only a file
server: it never decrypts, changes or re-signs anything. The bridge's boot
checks the signature, the product, the hardware revision and the version,
decrypts the records and writes the flash. ``inspect_package`` only
sanity-checks the file so that an obviously wrong file is refused before the
bridge is disturbed.

What the host sees on USB (vendor 1a86):

  application  1a86:fe11 "Cortex Bridge" (manufacturer "SiForge"; "OpenAMS" on
               lab images), serial "CB" + 12 hex digits. Normal operation.
  receiver     1a86:fe10 "Cortex Updater": the boot is waiting for a package.
               After an interrupted install the bridge stays here until a
               package installs, and ``update_bridge`` resumes from here.
  fault        1a86:fe10 "Cortex Fault": the boot refuses to run or install.
               Its serial number is the fault code (for example F-BOARD-01).
               It has no serial port; on Linux it is found through sysfs.

An update (``update_bridge``):

  1. ``inspect_package`` checks the file before any device is opened.
  2. The 1200-baud touch: the application's port is opened at 1200 baud and
     DTR is dropped. A busy bridge (motion, a load or unload, the follower
     following, a dryer transaction) refuses silently and keeps running. The
     rule: no receiver within 5 s of the touch means "busy, try again with the
     AMS idle" (``UpdateModeRefused``).
  3. The receiver requests the file (PackageOpen, PackageRead of at most 320
     bytes, PackageClose) and reads it twice: pass 1 authenticates and changes
     nothing, pass 2 re-reads the header and writes. The host answers each
     request (5 s per frame, 30 s of silence between requests, 300 s in all)
     and never resends anything by itself.
  4. The bridge resets. Back as the application (fe11) means installed; a
     "Cortex Fault" device raises ``BridgeFault`` with its code; back in the
     receiver means the install did not finish (run the update again).

The caller owns the application port and must close its own handle on it
before calling ``update_bridge``. This module opens that port only for the
touch, then opens the receiver's port for the transfer. It needs the standard
library and pyserial (imported only when a device is used). The optional
``cryptography`` package lets ``inspect_package`` check the header signature
against a public key the caller supplies; without it that check is skipped
and the result says so. The boot always verifies the signature itself.
"""

from __future__ import annotations

import hashlib
import os
import re
import struct
import time
import zlib

__all__ = [
    "UpdateError", "PackageRefused", "BridgeNotFound", "BridgeAmbiguous",
    "UpdateModeRefused", "TransferInterrupted", "ReceiverRejected",
    "BridgeFault", "Bridge", "inspect_package", "find_bridges",
    "update_bridge", "receiver_serial_for",
]

# ---- USB identities ---------------------------------------------------------

VID = 0x1A86
PID_APPLICATION = 0xFE11
PID_RECEIVER = 0xFE10            # the boot: its receiver and its fault device
APPLICATION_PRODUCT = "Cortex Bridge"
APPLICATION_MANUFACTURERS = ("SiForge", "OpenAMS")
# VID:PID alone is never trusted: manufacturer and product must both match.
RECEIVER_IDENTITY = ("SiForge", "Cortex Updater")
FAULT_IDENTITY = ("SiForge", "Cortex Fault")

STATE_APPLICATION = "application"
STATE_RECEIVER = "receiver"
STATE_FAULT = "fault"

# The receiver's bcdDevice names the package format its boot installs:
# 0x0100 is a lab boot (an older format, boot ABI 5), 0x0200 the production
# boot (OAC2, boot ABI 6). Read from sysfs; elsewhere the boot decides.
RECEIVER_BCD_BOOT_ABI = {"0100": 5, "0200": 6}
SYSFS_USB_DEVICES = "/sys/bus/usb/devices"

FAULT_CODES = {
    "F-BOARD-01": "the board failed its startup check",
    "F-ATECC-01": "the board's security check failed",
    "F-ATECC-02": "the board's security check did not start",
    "F-USB-01": "the update receiver could not start",
    "F-KEYREC-01": "the boot's security data is missing or damaged",
    "F-KEYREC-02": "the boot's security data belongs to a different board",
    "F-STATE-01": "the boot could not read its install record",
}

# The application's serial is "CB" + the 12 hex digits of the chip's six-byte
# unique ID. The receiver's is the first ten of those digits (five bytes).
_APP_SERIAL = re.compile(r"^CB([0-9A-F]{12})$")

# ---- the package: OAC2 ------------------------------------------------------

MAGIC = b"OAC2"
FORMAT_VERSION = 2
BODY_SIZE = 512
SIGNATURE_SIZE = 64
HEADER_SIZE = BODY_SIZE + SIGNATURE_SIZE          # 576
FIXED_SIZE = 256                                  # body fields; then a table
BLOCK_SIZE = 256
RECORD_SIZE = 288                                 # 16 + 256 + 16
MAX_FILE_SIZE = 576 * 1024
FLAG_ENCRYPTED_PAYLOAD = 1
RECORD_ALGORITHM = 1
SIGNATURE_ALGORITHM_P256 = 2
SIGNING_KEY_HINTS = (1, 2)
APP_KIND_BRIDGE = 1
APP_KIND_NAMES = {1: "bridge"}
RELEASE_TYPE_APPLICATION = 1
SIGNATURE_DOMAIN = b"SiForge OAC2 v1 header"      # signed: SHA-256(domain || body)

# ---- the frame layer: IPGW --------------------------------------------------

FRAME_MAGIC = b"IPGW"
FRAME_FORMAT = 1
FRAME_HEADER_SIZE = 24
FRAME_MAX_PAYLOAD = 512
PACKAGE_CHUNK = 320
PACKAGE_OPEN_REQUEST = 0x0003
PACKAGE_READ_REQUEST = 0x0004
PACKAGE_CLOSE_REQUEST = 0x0005
PACKAGE_OPEN_RESPONSE = 0x8003
PACKAGE_READ_RESPONSE = 0x8004
PACKAGE_CLOSE_RESPONSE = 0x8005
ERROR_RESPONSE = 0xFFFF
FRAME_TYPES = frozenset((
    PACKAGE_OPEN_REQUEST, PACKAGE_READ_REQUEST, PACKAGE_CLOSE_REQUEST,
    PACKAGE_OPEN_RESPONSE, PACKAGE_READ_RESPONSE, PACKAGE_CLOSE_RESPONSE,
    ERROR_RESPONSE,
))

# ---- timing -----------------------------------------------------------------

ENTRY_TIMEOUT_S = 5.0          # no receiver this long after the touch: refused
# The boot gives the host 5 s per reply and is silent for long stretches while
# it checks the signature or erases flash, so the limit between requests is
# generous and a frame that has started must finish quickly.
REPLY_BUDGET_S = 2.0           # replies slower than this are counted
IDLE_TIMEOUT_S = 30.0          # silence between requests
FRAME_TIMEOUT_S = 5.0          # one frame, first byte to last
TOTAL_TIMEOUT_S = 300.0
TOUCH_DTR_HOLD_S = 0.05
SETTLE_S = 0.2                 # let udev finish before opening or classifying
POLL_S = 0.1
INTERRUPTED_DISCONNECT_S = 10.0
RECEIVER_HOST_WAIT_S = 30.0    # an unclaimed receiver resets to the firmware


# ---- errors -----------------------------------------------------------------

class UpdateError(RuntimeError):
    """Base class of every error this module raises. ``report`` holds what
    is known about the attempt (a dict, possibly empty)."""

    def __init__(self, message: str, report: dict | None = None):
        super().__init__(message)
        self.report = report if report is not None else {}


class PackageRefused(UpdateError):
    """The file is not a valid OAC2 package for the bridge (or not for the
    boot this bridge has). Nothing was sent."""


class BridgeNotFound(UpdateError):
    """No Cortex Bridge (or no matching one) is attached."""


class BridgeAmbiguous(UpdateError):
    """More than one bridge or receiver is attached; pass ``serial_number``."""


class UpdateModeRefused(UpdateError):
    """No update receiver appeared within 5 s of the touch: the bridge is busy
    (or refused). Try again with the AMS idle."""


class TransferInterrupted(UpdateError):
    """The transfer stopped part-way. Nothing was resent. ``post_state`` is
    what the bridge became: application, receiver, fault or unknown."""

    def __init__(self, message: str, report: dict | None = None,
                 post_state: str = "unknown"):
        super().__init__(message, report)
        self.post_state = post_state


class ReceiverRejected(UpdateError):
    """The bridge's boot refused the package (for example a version that is
    not newer than the installed one, or
    one for another product or hardware revision). The boot does not say why
    over USB; ``stopped_at`` says where it stopped, from what it asked for."""

    def __init__(self, message: str, report: dict | None = None,
                 post_state: str = "unknown", stopped_at: str = ""):
        super().__init__(message, report)
        self.post_state = post_state
        self.stopped_at = stopped_at


class BridgeFault(UpdateError):
    """The bridge's boot reports a fault ("Cortex Fault"); ``code`` is the
    fault code, for example ``F-BOARD-01``."""

    def __init__(self, code: str, message: str | None = None,
                 report: dict | None = None):
        self.code = code or "unknown"
        self.description = FAULT_CODES.get(self.code, "an unknown fault code")
        super().__init__(message or (
            "the bridge's boot reports fault %s: %s. It will not run or "
            "install firmware in this state; contact support with this code"
            % (self.code, self.description)), report)


# ---- what is attached -------------------------------------------------------

class Bridge(object):
    """One Cortex Bridge as USB shows it. ``device`` is the serial port
    (None for a fault device, which has none); ``code`` is the fault code;
    ``usb_path`` the USB port name (for example ``1-1.2``) when known."""

    __slots__ = ("device", "serial_number", "manufacturer", "product", "pid",
                 "state", "lab", "code", "usb_path", "bcd_device")

    def __init__(self, device, serial_number, manufacturer, product, pid,
                 state, lab=False, code=None, usb_path=None, bcd_device=None):
        self.device = device
        self.serial_number = serial_number or ""
        self.manufacturer = manufacturer or ""
        self.product = product or ""
        self.pid = pid
        self.state = state
        self.lab = lab
        self.code = code
        self.usb_path = usb_path
        self.bcd_device = bcd_device

    def as_dict(self) -> dict:
        return {name: getattr(self, name) for name in self.__slots__}

    def describe(self) -> str:
        where = self.device or ("USB %s" % self.usb_path if self.usb_path
                                else "USB")
        return "%s (%s, %s)" % (where, self.state,
                                self.serial_number or "no serial")

    def __repr__(self):
        return "Bridge(%s)" % ", ".join(
            "%s=%r" % (name, getattr(self, name)) for name in self.__slots__)

    def __eq__(self, other):
        return isinstance(other, Bridge) and self.as_dict() == other.as_dict()

    def __hash__(self):
        return hash(_key(self))


def receiver_serial_for(serial_number: str | None) -> str | None:
    """The receiver's serial number for an application serial ("CB" + 12 hex
    digits), or None when ``serial_number`` is not one."""
    match = _APP_SERIAL.match((serial_number or "").strip().upper())
    return match.group(1)[:10] if match else None


def _serials_match(first: str | None, second: str | None) -> bool:
    first = (first or "").strip().upper()
    second = (second or "").strip().upper()
    if not first or not second:
        return False
    return (first == second or receiver_serial_for(first) == second
            or receiver_serial_for(second) == first)


def _same_unit(known: Bridge, other: Bridge) -> bool:
    """Whether ``other`` is the board ``known`` was: the same USB port, or a
    matching serial number (fault devices carry a code, not a serial)."""
    if known.usb_path and other.usb_path:
        if known.usb_path == other.usb_path:
            return True
    if STATE_FAULT in (known.state, other.state):
        return False
    return _serials_match(known.serial_number, other.serial_number)


def _key(bridge: Bridge) -> tuple:
    return (bridge.state, bridge.device, bridge.serial_number,
            bridge.usb_path)


def _classify(pid, manufacturer, product):
    """(state, lab) for a USB identity, or (None, False) when it is not one
    of the bridge's."""
    identity = (manufacturer, product)
    if pid == PID_APPLICATION and product == APPLICATION_PRODUCT and \
            manufacturer in APPLICATION_MANUFACTURERS:
        return STATE_APPLICATION, manufacturer != "SiForge"
    if pid == PID_RECEIVER:
        if identity == RECEIVER_IDENTITY:
            return STATE_RECEIVER, False
        if identity == FAULT_IDENTITY:
            return STATE_FAULT, False
        # Any other boot on this ID is not a production boot: it is listed
        # so that nothing is ever sent to it.
        return STATE_RECEIVER, True
    return None, False


def _read_sysfs(path: str) -> str | None:
    try:
        with open(path, "r") as handle:
            return handle.read().rstrip("\n")
    except (OSError, UnicodeDecodeError):
        return None


def _sysfs_devices(root: str | None) -> list:
    """Every USB device with the bridge's vendor ID under ``root``."""
    root = root or SYSFS_USB_DEVICES
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    found = []
    for name in names:
        entry = os.path.join(root, name)
        if _read_sysfs(os.path.join(entry, "idVendor")) != "%04x" % VID:
            continue
        try:
            pid = int(_read_sysfs(os.path.join(entry, "idProduct")) or "", 16)
        except ValueError:
            continue
        found.append({
            "name": name, "pid": pid,
            "manufacturer": _read_sysfs(os.path.join(entry, "manufacturer")),
            "product": _read_sysfs(os.path.join(entry, "product")),
            "serial": _read_sysfs(os.path.join(entry, "serial")),
            "bcd": _read_sysfs(os.path.join(entry, "bcdDevice")),
        })
    return found


def _serial_module(serial_module):
    if serial_module is not None:
        return serial_module
    try:
        import serial
        import serial.tools.list_ports  # noqa: F401  (loads the submodule)
    except ImportError as error:
        raise UpdateError("pyserial is required to reach the bridge (the "
                          "pyserial or python3-serial package)") from error
    return serial


def _serial_errors(module) -> tuple:
    extra = getattr(module, "SerialException", None)
    if isinstance(extra, type) and issubclass(extra, BaseException):
        return (OSError, extra)
    return (OSError,)


def find_bridges(serial_module=None, *, sysfs_root: str | None = None) -> list:
    """Every attached Cortex Bridge, as ``Bridge`` objects whose ``state`` is
    "application", "receiver" or "fault". Fault devices have no serial port
    and are found through sysfs (Linux); ``sysfs_root`` overrides its path."""
    module = _serial_module(serial_module)
    sysfs = _sysfs_devices(sysfs_root)
    result = []
    for item in module.tools.list_ports.comports():
        if getattr(item, "vid", None) != VID:
            continue
        pid = getattr(item, "pid", None)
        manufacturer = getattr(item, "manufacturer", None)
        product = getattr(item, "product", None)
        state, lab = _classify(pid, manufacturer, product)
        if state is None:
            continue
        serial_number = getattr(item, "serial_number", None) or ""
        device_dir = getattr(item, "usb_device_path", None)
        usb_path = bcd = None
        if isinstance(device_dir, str) and device_dir:
            usb_path = os.path.basename(device_dir.rstrip("/"))
            bcd = _read_sysfs(os.path.join(device_dir, "bcdDevice"))
        else:
            location = getattr(item, "location", None)
            if isinstance(location, str) and location:
                usb_path = location.split(":")[0]
        if bcd is None and state == STATE_RECEIVER:
            for entry in sysfs:
                if entry["pid"] == pid and entry["product"] == product and \
                        (entry["serial"] or "") == serial_number:
                    bcd = entry["bcd"]
                    usb_path = usb_path or entry["name"]
                    break
        code = serial_number if state == STATE_FAULT else None
        result.append(Bridge(item.device, serial_number, manufacturer,
                             product, pid, state, lab, code, usb_path, bcd))
    seen = {bridge.usb_path for bridge in result if bridge.usb_path}
    for entry in sysfs:
        state, _ = _classify(entry["pid"], entry["manufacturer"],
                             entry["product"])
        if state == STATE_FAULT and entry["name"] not in seen:
            code = entry["serial"] or "unknown"
            result.append(Bridge(None, code, entry["manufacturer"],
                                 entry["product"], entry["pid"], STATE_FAULT,
                                 code=code, usb_path=entry["name"],
                                 bcd_device=entry["bcd"]))
    return result


def _try_find(module, sysfs_root):
    """A snapshot, or None while USB is mid-change. Linux can show a device
    before all of its attributes exist, and pyserial may raise on that."""
    try:
        return find_bridges(module, sysfs_root=sysfs_root)
    except (OSError, TypeError, ValueError):
        return None


# ---- the package ------------------------------------------------------------

def _refuse(message: str):
    raise PackageRefused("refused: %s. Nothing was sent to the bridge"
                         % message)


def inspect_package(data: bytes, public_key: bytes | None = None) -> dict:
    """Sanity-check an OAC2 bridge package without any device. Returns its
    public header fields; raises ``PackageRefused`` when the file is not one.

    ``public_key`` (64-byte X||Y, 65-byte 04||X||Y, hex, or PEM) adds a check
    of the header's P-256 signature when the ``cryptography`` package is
    installed; ``signature_checked`` says whether it ran. The bridge's boot
    verifies the signature in every case."""
    data = bytes(data)
    if data[:4] != MAGIC:
        if data[:4] == b"OAC1":
            _refuse("this is an OAC1 file, an older format the bridge does "
                    "not install")
        _refuse("this is not an OAC2 firmware package")
    if len(data) > MAX_FILE_SIZE:
        _refuse("the file is %d bytes, above the %d-byte limit"
                % (len(data), MAX_FILE_SIZE))
    if len(data) < HEADER_SIZE:
        _refuse("the file is truncated (%d bytes, shorter than the %d-byte "
                "header)" % (len(data), HEADER_SIZE))
    body = data[:BODY_SIZE]
    fmt, body_size, flags = struct.unpack_from(">III", body, 4)
    if fmt != FORMAT_VERSION or body_size != BODY_SIZE:
        _refuse("unsupported OAC2 format %d" % fmt)
    if flags != FLAG_ENCRYPTED_PAYLOAD:
        _refuse("unsupported header flags 0x%x" % flags)
    product_id, hardware_revision = struct.unpack_from(">II", body, 16)
    version, minimum_version = struct.unpack_from(">QQ", body, 24)
    image_size, payload_size, block_size, block_count = \
        struct.unpack_from(">IIII", body, 40)
    record_algorithm, signature_algorithm, key_hint, kind = \
        struct.unpack_from(">IIII", body, 56)
    if record_algorithm != RECORD_ALGORITHM or \
            signature_algorithm != SIGNATURE_ALGORITHM_P256 or \
            key_hint not in SIGNING_KEY_HINTS:
        _refuse("unsupported algorithm identifiers in the header")
    if kind != APP_KIND_BRIDGE:
        _refuse("this package is for a %s, not for the Cortex Bridge"
                % APP_KIND_NAMES.get(kind, "different device (kind %d)"
                                     % kind))
    if block_size != BLOCK_SIZE or image_size == 0 or \
            image_size % BLOCK_SIZE or block_count != image_size // BLOCK_SIZE \
            or payload_size != block_count * RECORD_SIZE:
        _refuse("the header's sizes are inconsistent")
    if version == 0 or version < minimum_version or not product_id or \
            not hardware_revision:
        _refuse("the header's product or version fields are invalid")
    if body[228:FIXED_SIZE] != bytes(FIXED_SIZE - 228):
        _refuse("reserved header bytes are not zero")
    expected = HEADER_SIZE + payload_size
    if len(data) < expected:
        _refuse("the file is truncated (%d of %d bytes)"
                % (len(data), expected))
    if len(data) > expected:
        _refuse("the file has %d bytes after its last record"
                % (len(data) - expected))
    release_type, _, capacity, boot_abi = struct.unpack_from(">IIII", body, 208)
    if release_type != RELEASE_TYPE_APPLICATION:
        _refuse("this is not an application firmware release")
    if image_size > capacity:
        _refuse("the image is larger than the capacity the header names")
    if hashlib.sha256(data[HEADER_SIZE:]).digest() != body[176:208]:
        _refuse("the records do not match the header's digest; the file is "
                "damaged")
    checked, note = _check_signature(SIGNATURE_DOMAIN + body,
                                     data[BODY_SIZE:HEADER_SIZE], public_key)
    return {
        "format": "OAC2",
        "magic": MAGIC.decode("ascii"),
        "format_version": fmt,
        "version": version,
        "minimum_version": minimum_version,
        "app_kind": kind,
        "app_kind_name": APP_KIND_NAMES[kind],
        "product_id": product_id,
        "hardware_revision": hardware_revision,
        "required_boot_abi": boot_abi,
        "release_id": body[80:112].hex(),
        "image_size": image_size,
        "payload_size": payload_size,
        "block_count": block_count,
        "file_size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "signature_checked": checked,
        "signature_note": note,
    }


def _cryptography():
    """The optional ``cryptography`` modules, or None when they are missing
    or unusable. A broken install can fail with more than ImportError (a
    binding built for another Python raises pyo3's PanicException, a
    BaseException), and that must not stop an update."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, utils
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except BaseException:
        return None
    return {"InvalidSignature": InvalidSignature, "hashes": hashes,
            "serialization": serialization, "ec": ec, "utils": utils}


def _with_backend(function, *args):
    try:
        return function(*args)
    except TypeError:          # cryptography before 3.1 needs a backend
        from cryptography.hazmat.backends import default_backend
        return function(*args, default_backend())


def _p256_public_key(raw: bytes, crypto: dict):
    serialization, ec = crypto["serialization"], crypto["ec"]
    raw = bytes(raw)
    text = raw.strip()
    point = None
    if len(raw) == 64:
        point = raw
    elif len(raw) == 65 and raw[0] == 4:
        point = raw[1:]
    elif len(text) in (128, 130) and re.match(rb"^[0-9A-Fa-f]+$", text):
        decoded = bytes.fromhex(text.decode("ascii"))
        if len(decoded) == 64 or decoded[0] == 4:
            point = decoded[-64:]
    if point is not None:
        numbers = ec.EllipticCurvePublicNumbers(
            int.from_bytes(point[:32], "big"),
            int.from_bytes(point[32:], "big"), ec.SECP256R1())
        key = _with_backend(numbers.public_key)
    elif text.startswith(b"-----BEGIN"):
        key = _with_backend(serialization.load_pem_public_key, raw)
    else:
        key = _with_backend(serialization.load_der_public_key, raw)
    if not isinstance(key, ec.EllipticCurvePublicKey) or \
            key.curve.name != "secp256r1":
        raise ValueError("the public key is not a P-256 key")
    return key


def _check_signature(message: bytes, signature: bytes, public_key):
    """(checked, note); raises PackageRefused for a signature that fails."""
    if public_key is None:
        return False, ("no public key was given; the bridge's boot verifies "
                       "the signature")
    crypto = _cryptography()
    if crypto is None:
        return False, ("the cryptography package is not installed (or not "
                       "usable), so the signature was not checked here; the "
                       "bridge's boot verifies it")
    key = _p256_public_key(public_key, crypto)
    der = crypto["utils"].encode_dss_signature(
        int.from_bytes(signature[:32], "big"),
        int.from_bytes(signature[32:], "big"))
    try:
        key.verify(der, message, crypto["ec"].ECDSA(crypto["hashes"].SHA256()))
    except crypto["InvalidSignature"]:
        _refuse("the package signature does not match the public key given")
    return True, "the signature matches the public key given"


# ---- the frame layer --------------------------------------------------------

class _FrameError(ValueError):
    pass


def encode_frame(message_type: int, sequence: int, payload: bytes) -> bytes:
    """One IPGW frame: magic, format, type, sequence, payload length, payload
    CRC-32, header CRC-32 (all big endian), then the payload."""
    if message_type not in FRAME_TYPES:
        raise _FrameError("unknown frame type")
    if sequence <= 0 or sequence > 0xFFFFFFFF:
        raise _FrameError("invalid frame sequence")
    if len(payload) > FRAME_MAX_PAYLOAD:
        raise _FrameError("frame payload is too large")
    header = bytearray(FRAME_HEADER_SIZE)
    struct.pack_into(">4sHHIII", header, 0, FRAME_MAGIC, FRAME_FORMAT,
                     message_type, sequence, len(payload),
                     zlib.crc32(payload) & 0xFFFFFFFF)
    struct.pack_into(">I", header, 20, zlib.crc32(bytes(header[:20])) &
                     0xFFFFFFFF)
    return bytes(header) + bytes(payload)


def decode_frame(encoded: bytes) -> tuple:
    """(type, sequence, payload) of one complete frame, or ``_FrameError``."""
    if len(encoded) < FRAME_HEADER_SIZE:
        raise _FrameError("truncated frame")
    magic, version, message_type, sequence, length, payload_crc, header_crc = \
        struct.unpack_from(">4sHHIIII", encoded)
    if magic != FRAME_MAGIC or version != FRAME_FORMAT:
        raise _FrameError("invalid frame header")
    if message_type not in FRAME_TYPES or sequence == 0:
        raise _FrameError("invalid frame type or sequence")
    if length > FRAME_MAX_PAYLOAD or len(encoded) != FRAME_HEADER_SIZE + length:
        raise _FrameError("invalid frame length")
    if zlib.crc32(bytes(encoded[:20])) & 0xFFFFFFFF != header_crc:
        raise _FrameError("frame header CRC mismatch")
    payload = bytes(encoded[FRAME_HEADER_SIZE:])
    if zlib.crc32(payload) & 0xFFFFFFFF != payload_crc:
        raise _FrameError("frame payload CRC mismatch")
    return message_type, sequence, payload


# ---- serving the receiver ---------------------------------------------------

class _Counters(object):
    def __init__(self):
        self.opens = 0
        self.reads = 0
        self.passes = 0                # reads that started at offset 0
        self.bytes_served = 0
        self.accepted = None           # the boot's close verdict
        self.stopped_at = ""
        self.max_turnaround_s = 0.0
        self.slow_replies = 0


def _stopped_at(counters: _Counters, file_size: int) -> str:
    """Where in the install the boot stopped, from what it asked for (the
    close carries only accepted or rejected)."""
    if counters.opens == 0:
        return "before the file was opened"
    if counters.reads == 0:
        return "after opening the file, before reading it"
    if counters.passes == 1 and counters.bytes_served <= HEADER_SIZE:
        return ("while checking the header (product, hardware revision, "
                "version or signature)")
    if counters.passes == 1:
        return "during the authentication pass; nothing was changed"
    if counters.bytes_served <= file_size + HEADER_SIZE:
        return "at the start of the write pass"
    return ("during the write pass; the bridge keeps its update receiver "
            "until a package installs")


class _Progress(object):
    """Calls the caller's progress(phase, done, total) at phase changes and
    whole-percent steps. A failing callback never stops an install."""

    def __init__(self, callback):
        self.callback = callback
        self.last = None

    def __call__(self, phase: str, done: int, total: int) -> None:
        if self.callback is None:
            return
        step = (phase, (100 * done // total) if total > 0 else done)
        if step == self.last:
            return
        self.last = step
        try:
            self.callback(phase, int(done), int(total))
        except Exception:      # a display problem must not break an install
            pass


class _PackageServer(object):
    """Answers PackageOpen / PackageRead / PackageClose from one file."""

    def __init__(self, data: bytes, counters: _Counters, progress: _Progress):
        self.data = data
        self.digest = hashlib.sha256(data).digest()
        self.open = False
        self.counters = counters
        self.progress = progress
        self.served_to = None          # end offset of the last read, if any

    def process(self, message_type: int, payload: bytes) -> tuple:
        counters = self.counters
        if message_type == PACKAGE_OPEN_REQUEST:
            if payload or self.open:
                raise _FrameError("invalid package open")
            self.open = True
            counters.opens += 1
            self.served_to = 0
            return PACKAGE_OPEN_RESPONSE, (
                struct.pack(">I", len(self.data)) + self.digest)
        if message_type == PACKAGE_READ_REQUEST:
            if not self.open or len(payload) != 8:
                raise _FrameError("the package is not open")
            offset, capacity = struct.unpack(">II", payload)
            if capacity < 1 or capacity > PACKAGE_CHUNK or \
                    offset >= len(self.data):
                raise _FrameError("invalid package read")
            chunk = self.data[offset:offset + capacity]
            if offset == 0:
                counters.passes += 1
            counters.reads += 1
            counters.bytes_served += len(chunk)
            self.served_to = offset + len(chunk)
            return PACKAGE_READ_RESPONSE, (
                struct.pack(">II", offset, len(chunk)) + chunk)
        if message_type == PACKAGE_CLOSE_REQUEST:
            if not self.open or len(payload) != 4 or \
                    struct.unpack(">I", payload)[0] not in (0, 1):
                raise _FrameError("invalid package close")
            self.open = False
            counters.accepted = struct.unpack(">I", payload)[0] == 1
            counters.stopped_at = _stopped_at(counters, len(self.data))
            self.served_to = None
            return PACKAGE_CLOSE_RESPONSE, b""
        raise _FrameError("only package requests are served")

    def process_frame(self, encoded: bytes) -> bytes:
        message_type, sequence, payload = decode_frame(encoded)
        try:
            response_type, response = self.process(message_type, payload)
        except (_FrameError, OSError, ValueError):
            response_type, response = ERROR_RESPONSE, struct.pack(">I", 1)
        return encode_frame(response_type, sequence, response)

    def report_progress(self) -> None:
        if self.served_to is None:
            return
        phase = "writing" if self.counters.passes >= 2 else "authenticating"
        self.progress(phase, self.served_to, len(self.data))


class _Cut(Exception):
    """The stream was cut, stalled or unusable (converted by the caller)."""


def _read_exact(port, length: int, idle_limit: float, clock,
                frame_start: float | None) -> bytes:
    result = bytearray()
    idle_deadline = clock() + idle_limit
    while len(result) < length:
        now = clock()
        if frame_start is not None and now - frame_start > FRAME_TIMEOUT_S:
            raise TimeoutError("a frame stalled part-way")
        if frame_start is None and now > idle_deadline:
            raise TimeoutError("the receiver went quiet")
        chunk = port.read(length - len(result))
        if not chunk:
            if getattr(port, "timeout", None) is None:
                raise ConnectionError("the receiver disconnected")
            continue
        if frame_start is None:
            frame_start = clock()
        result.extend(chunk)
    return bytes(result)


def _write_all(port, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = port.write(data[offset:])
        if written is None:
            written = len(data) - offset
        if written <= 0 or written > len(data) - offset:
            raise OSError("short write")
        offset += written


def _serve(port, server: _PackageServer, clock, errors: tuple) -> None:
    """Answer the boot's requests until it closes the package. Raises ``_Cut``
    on a cut, a stall or an unusable frame; never resends anything."""
    counters = server.counters
    started = clock()
    try:
        while True:
            if clock() - started > TOTAL_TIMEOUT_S:
                raise TimeoutError("the transfer exceeded its total limit")
            prefix = _read_exact(port, 4, IDLE_TIMEOUT_S, clock, None)
            frame_start = clock()
            if prefix != FRAME_MAGIC:
                raise _FrameError("unexpected data on the receiver stream")
            header = prefix + _read_exact(port, FRAME_HEADER_SIZE - 4,
                                          FRAME_TIMEOUT_S, clock, frame_start)
            payload_length = struct.unpack_from(">I", header, 12)[0]
            if payload_length > FRAME_MAX_PAYLOAD:
                raise _FrameError("the receiver declared an oversized payload")
            request = header + _read_exact(port, payload_length,
                                           FRAME_TIMEOUT_S, clock, frame_start)
            request_type, _, _ = decode_frame(request)
            arrived = clock()
            response = server.process_frame(request)
            try:
                _write_all(port, response)
                port.flush()
            except errors + (ValueError,) as error:
                raise _Cut("the reply to request 0x%04x may not have reached "
                           "the receiver (%s)" % (request_type, error))
            turnaround = clock() - arrived
            counters.max_turnaround_s = max(counters.max_turnaround_s,
                                            turnaround)
            if turnaround > REPLY_BUDGET_S:
                counters.slow_replies += 1
            server.report_progress()
            if request_type == PACKAGE_CLOSE_REQUEST:
                return
    except _Cut:
        raise
    except (_FrameError, ConnectionError, TimeoutError) + errors as error:
        raise _Cut(str(error) or error.__class__.__name__)


# ---- entering update mode and watching USB ----------------------------------

def _keep_input_on_open(serial_class):
    """``serial_class`` without the input flush at the end of open().

    Opening the receiver's port raises DTR, which tells the boot a host is
    ready, and the boot answers at once with its first request. pyserial's
    POSIX open() raises DTR and only then flushes input, which could discard
    that request and leave the boot waiting for its I/O timeout. Nothing stale
    can be queued: the CDC driver stops reading while the port is closed."""
    if not hasattr(serial_class, "_reset_input_buffer"):
        return serial_class

    class KeepInputOnOpen(serial_class):
        _opening = False

        def open(self):
            self._opening = True
            try:
                super().open()
            finally:
                self._opening = False

        def _reset_input_buffer(self):
            if not self._opening:
                super()._reset_input_buffer()

    return KeepInputOnOpen


def _open_receiver_port(module, device: str):
    return _keep_input_on_open(module.Serial)(
        device, 115200, timeout=0.25, write_timeout=5, exclusive=True)


def _touch(module, device: str, sleep, errors: tuple) -> None:
    """The 1200-baud touch: 1200 baud, then DTR dropped."""
    try:
        port = module.Serial(device, 1200, timeout=0.25, write_timeout=1)
    except errors as error:
        raise UpdateError(
            "could not open %s to ask the bridge to enter update mode (%s). "
            "Close every program that uses the port and try again"
            % (device, error)) from error
    try:
        port.dtr = True
        sleep(TOUCH_DTR_HOLD_S)
        port.dtr = False
    except errors:
        # The bridge leaving USB right after DTR drops is the expected
        # outcome; what enumerates next tells success from refusal.
        pass
    finally:
        try:
            port.close()
        except errors:
            pass


def _select(bridges: list, serial_number: str | None, port: str) -> Bridge:
    candidates = list(bridges)
    if port != "auto":
        candidates = [b for b in candidates if b.device == port]
        if not candidates:
            raise BridgeNotFound("%s is not a Cortex Bridge or its update "
                                 "receiver" % port)
    if serial_number:
        candidates = [b for b in candidates if b.state != STATE_FAULT and
                      _serials_match(b.serial_number, serial_number)]
    if len(candidates) > 1:
        raise BridgeAmbiguous(
            "%d Cortex Bridge devices are attached (%s); pass the serial "
            "number of the one to update"
            % (len(candidates), ", ".join(b.describe() for b in candidates)))
    if not candidates:
        faults = [b.code for b in bridges if b.state == STATE_FAULT]
        raise BridgeNotFound(
            "no Cortex Bridge%s is attached over USB%s"
            % (" with serial number %s" % serial_number if serial_number
               else "",
               "; a bridge reporting fault %s is attached" % ", ".join(faults)
               if faults else ""))
    chosen = candidates[0]
    if chosen.state == STATE_FAULT:
        raise BridgeFault(chosen.code)
    return chosen


def _fault_of(unit: Bridge, snapshot: list, known: set):
    """The fault device ``unit`` became: one on its USB port when that is
    known, else the only fault device that was not there before."""
    faults = [b for b in snapshot if b.state == STATE_FAULT]
    if unit.usb_path:
        same = [b for b in faults if b.usb_path == unit.usb_path]
        if same:
            return same[0]
        if all(b.usb_path for b in faults):
            return None
    fresh = [b for b in faults if _key(b) not in known]
    return fresh[0] if len(fresh) == 1 else None


def _wait_for_receiver(module, target: Bridge, known: set, sysfs_root,
                       clock, sleep, entry_timeout: float,
                       enumeration_timeout: float, notes: list) -> Bridge:
    touched = clock()
    limit = max(entry_timeout, enumeration_timeout)
    while True:
        snapshot = _try_find(module, sysfs_root)
        elapsed = clock() - touched
        if snapshot is not None:
            mine = [b for b in snapshot
                    if b.state == STATE_RECEIVER and _same_unit(target, b)]
            if len(mine) > 1:
                raise BridgeAmbiguous("more than one update receiver matches "
                                      "the bridge; disconnect the others")
            if mine:
                return mine[0]
            running = any(b.state == STATE_APPLICATION and _same_unit(target, b)
                          for b in snapshot)
            if not running:
                fresh = [b for b in snapshot if b.state == STATE_RECEIVER and
                         _key(b) not in known]
                if len(fresh) == 1:
                    notes.append("the update receiver was recognised as the "
                                 "only new one after the touch")
                    return fresh[0]
                fault = _fault_of(target, snapshot, known)
                if fault is not None:
                    raise BridgeFault(fault.code)
            elif elapsed >= entry_timeout:
                raise UpdateModeRefused(
                    "the bridge did not enter update mode within %g s: it is "
                    "busy (moving, loading or unloading, following, or "
                    "running a dryer command). Wait until the AMS is idle and "
                    "try again" % entry_timeout)
        if elapsed >= limit:
            raise UpdateModeRefused(
                "the bridge left its firmware but no update receiver "
                "appeared within %g s. Try again in a minute; if this "
                "repeats, reconnect the bridge's USB cable" % limit)
        sleep(POLL_S)


def _locate(identities: list, snapshot: list, known: set):
    """What the unit is now: application first, then receiver, then fault."""
    for state in (STATE_APPLICATION, STATE_RECEIVER):
        for bridge in snapshot:
            if bridge.state == state and any(_same_unit(unit, bridge)
                                             for unit in identities):
                return bridge
    for unit in identities:
        fault = _fault_of(unit, snapshot, known)
        if fault is not None:
            return fault
    fresh = [b for b in snapshot
             if b.state == STATE_APPLICATION and _key(b) not in known]
    return fresh[0] if len(fresh) == 1 else None


def _observe(module, served: Bridge, identities: list, known: set,
             sysfs_root, clock, sleep, disconnect_timeout: float,
             enumeration_timeout: float):
    """Wait for the receiver to reset and for the bridge to come back.
    Returns the ``Bridge`` it became, or None if it did not reappear."""
    deadline = clock() + disconnect_timeout
    while clock() < deadline:
        snapshot = _try_find(module, sysfs_root)
        if snapshot is not None and \
                not any(_key(b) == _key(served) for b in snapshot):
            break
        sleep(POLL_S)
    # It may have reset and re-enumerated between two looks; classify
    # whatever is there now.
    deadline = clock() + enumeration_timeout
    while True:
        snapshot = _try_find(module, sysfs_root)
        if snapshot is not None:
            found = _locate(identities, snapshot, known)
            if found is not None:
                sleep(SETTLE_S)        # let a hotplug burst finish
                again = _try_find(module, sysfs_root)
                if again is not None:
                    found = _locate(identities, again, known) or found
                return found
        if clock() >= deadline:
            return None
        sleep(POLL_S)


def _describe(bridge) -> str:
    if bridge is None:
        return "not visible on USB"
    if bridge.state == STATE_APPLICATION:
        return "running its firmware"
    if bridge.state == STATE_RECEIVER:
        return "waiting in its update receiver"
    return "reporting fault %s" % bridge.code


def _check_receiver_boot(receiver: Bridge, info: dict) -> None:
    if receiver.lab:
        raise ReceiverRejected(
            "the bridge's boot is not a production boot and does not install "
            "these packages. Nothing was sent; it returns to its firmware within "
            "%d s" % RECEIVER_HOST_WAIT_S, post_state=STATE_RECEIVER,
            stopped_at="before the file was opened")
    abi = RECEIVER_BCD_BOOT_ABI.get(receiver.bcd_device or "")
    if abi is not None and abi != info["required_boot_abi"]:
        _refuse("the package needs boot ABI %d and this bridge's boot is ABI "
                "%d; it returns to its firmware within %d s"
                % (info["required_boot_abi"], abi, RECEIVER_HOST_WAIT_S))


# ---- the update -------------------------------------------------------------

def update_bridge(data: bytes, *, serial_number: str | None = None,
                  port: str = "auto", serial_module=None, progress=None,
                  clock=time.monotonic, sleep=time.sleep,
                  entry_timeout: float = ENTRY_TIMEOUT_S,
                  enumeration_timeout: float = 10.0,
                  disconnect_timeout: float = 60.0,
                  public_key: bytes | None = None,
                  sysfs_root: str | None = None) -> dict:
    """Install ``data`` (an OAC2 package) on one Cortex Bridge.

    Finds the bridge (``serial_number`` or ``port`` pick one when several are
    attached), asks it to enter update mode unless its receiver is already
    up, serves both passes and waits for it to come back. Returns a report
    whose ``result`` is "installed"; raises an ``UpdateError`` subclass
    otherwise. ``progress(phase, done, total)`` sees "entering",
    "waiting-receiver", "authenticating", "writing", "restarting" and "done".
    The caller must have closed its own handle on the bridge's port."""
    started = clock()
    info = inspect_package(data, public_key)       # no device I/O before this
    module = _serial_module(serial_module)
    errors = _serial_errors(module)
    notify = _Progress(progress)
    notes = []
    report = {
        "version": info["version"],
        "minimum_version": info["minimum_version"],
        "package_sha256": info["sha256"],
        "package_bytes": len(data),
        "signature_checked": info["signature_checked"],
        "notes": notes,
    }

    bridges = None
    deadline = clock() + 1.0
    while bridges is None:
        bridges = _try_find(module, sysfs_root)
        if bridges is None:
            if clock() >= deadline:
                raise UpdateError("could not list the USB serial ports",
                                  report)
            sleep(POLL_S)
    known = {_key(b) for b in bridges}
    target = _select(bridges, serial_number, port)
    report["serial_number"] = target.serial_number
    report["resumed"] = target.state == STATE_RECEIVER

    if target.state == STATE_RECEIVER:
        receiver = target
        notify("waiting-receiver", 1, 1)
    else:
        notify("entering", 0, 1)
        _touch(module, target.device, sleep, errors)
        notify("entering", 1, 1)
        notify("waiting-receiver", 0, 1)
        try:
            receiver = _wait_for_receiver(
                module, target, known, sysfs_root, clock, sleep,
                entry_timeout, enumeration_timeout, notes)
        except UpdateError as error:
            error.report.update(report)
            raise
        notify("waiting-receiver", 1, 1)
    entered = clock()
    report["receiver_device"] = receiver.device
    try:
        _check_receiver_boot(receiver, info)
    except UpdateError as error:
        error.report.update(report)
        raise

    sleep(SETTLE_S)
    counters = _Counters()
    server = _PackageServer(bytes(data), counters, notify)
    cut = None
    try:
        handle = _open_receiver_port(module, receiver.device)
    except errors as error:
        handle = None
        cut = "could not open %s (%s); nothing was sent" % (receiver.device,
                                                            error)
    if handle is not None:
        try:
            _serve(handle, server, clock, errors)
        except _Cut as error:
            cut = str(error)
        finally:
            try:
                handle.close()
            except Exception:  # the device may already be gone
                pass
    transferred = clock()
    report.update({
        "opens": counters.opens, "reads": counters.reads,
        "passes": counters.passes, "bytes_served": counters.bytes_served,
        "accepted": counters.accepted,
        "max_turnaround_s": round(counters.max_turnaround_s, 3),
        "slow_replies": counters.slow_replies,
    })
    if counters.slow_replies:
        notes.append("%d replies took longer than %g s; the boot waits 5 s "
                     "for each" % (counters.slow_replies, REPLY_BUDGET_S))
    identities = [target, receiver]

    if cut is not None:
        final = _observe(module, receiver, identities, known, sysfs_root,
                         clock, sleep,
                         min(disconnect_timeout, INTERRUPTED_DISCONNECT_S),
                         enumeration_timeout)
        report["final_state"] = final.state if final else "unknown"
        if final is not None and final.state == STATE_FAULT:
            raise BridgeFault(final.code, report=report)
        raise TransferInterrupted(
            "the transfer stopped (%s) after %d reads (%d bytes, %d "
            "pass(es)); nothing was resent. The bridge is now %s. Run the "
            "update again" % (cut, counters.reads, counters.bytes_served,
                              counters.passes, _describe(final)),
            report, report["final_state"])

    if counters.accepted and (counters.passes != 2 or
                              counters.bytes_served != 2 * len(data)):
        raise UpdateError(
            "the bridge accepted the package but did not read it twice (%d "
            "pass(es), %d bytes); treat the bridge as unverified and run the "
            "update again" % (counters.passes, counters.bytes_served), report)

    notify("restarting", 0, 1)
    final = _observe(module, receiver, identities, known, sysfs_root, clock,
                     sleep, disconnect_timeout, enumeration_timeout)
    finished = clock()
    report["final_state"] = final.state if final else "unknown"
    report["durations"] = {
        "entering": round(entered - started, 3),
        "transfer": round(transferred - entered, 3),
        "restarting": round(finished - transferred, 3),
        "total": round(finished - started, 3),
    }
    if final is not None and final.state == STATE_FAULT:
        raise BridgeFault(final.code, report=report)
    if not counters.accepted:
        raise ReceiverRejected(
            "the bridge's boot refused the package %s (for example a version "
            "that is not newer than the installed one, or a package for "
            "another product or hardware revision). The boot does not send "
            "its reason over USB. "
            "The bridge is now %s" % (counters.stopped_at, _describe(final)),
            report, report["final_state"], counters.stopped_at)
    if final is None:
        raise BridgeNotFound(
            "the bridge accepted the package but did not reappear on USB "
            "within %g s; reconnect its USB cable and check it"
            % enumeration_timeout, report)
    if final.state == STATE_RECEIVER:
        raise TransferInterrupted(
            "the bridge accepted the package but came back in its update "
            "receiver, so the install did not finish. Run the update again",
            report, STATE_RECEIVER)
    notify("restarting", 1, 1)
    notify("done", 1, 1)
    report.update({
        "result": "installed",
        "device": final.device,
        "serial_number": final.serial_number,
    })
    return report
