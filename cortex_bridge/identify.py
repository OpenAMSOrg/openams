# SPDX-License-Identifier: MIT
"""Ask one AMS what it is, through its /dev/oams/<uid> port.

Each AMS behind the bridge answers Klipper's own `identify` command with its
data dictionary, exactly as any Klipper MCU does. The dictionary's constants
name the hardware family and the number of bays, which is all the config
writer needs. Only run this while Klipper is stopped: two readers on one port
would steal each other's replies.
"""

from __future__ import annotations

import json
import os
import select
import time
import zlib

MESSAGE_MIN = 5
MESSAGE_MAX = 64
MESSAGE_HEADER = 2
MESSAGE_TRAILER = 3
MESSAGE_DEST = 0x10
MESSAGE_SEQ_MASK = 0x0F
MESSAGE_SYNC = 0x7E
IDENTIFY_REQUEST_ID = 1     # "identify offset=%u count=%c"
IDENTIFY_RESPONSE_ID = 0    # "identify_response offset=%u data=%.*s"
CHUNK = 40

FAMILY_NAMES = {"ams1": "AMS 1", "ams2": "AMS 2 Pro", "ams_ht": "AMS HT"}


class IdentifyError(RuntimeError):
    pass


def crc16_ccitt(data: bytes) -> bytes:
    crc = 0xFFFF
    for byte in data:
        byte ^= crc & 0xFF
        byte ^= (byte & 0x0F) << 4
        byte &= 0xFF
        crc = (((byte << 8) | (crc >> 8)) ^ (byte >> 4) ^ (byte << 3)) & 0xFFFF
    return bytes((crc >> 8, crc & 0xFF))


def encode_vlq(value: int) -> bytes:
    """Klipper's variable-length integer encoding (msgproto PT_uint32)."""
    signed = value
    out = bytearray()
    if signed >= 0xC000000 or signed < -0x4000000:
        out.append(((signed >> 28) & 0x7F) | 0x80)
    if signed >= 0x180000 or signed < -0x80000:
        out.append(((signed >> 21) & 0x7F) | 0x80)
    if signed >= 0x3000 or signed < -0x1000:
        out.append(((signed >> 14) & 0x7F) | 0x80)
    if signed >= 0x60 or signed < -0x20:
        out.append(((signed >> 7) & 0x7F) | 0x80)
    out.append(signed & 0x7F)
    return bytes(out)


def decode_vlq(data: bytes, pos: int):
    byte = data[pos]
    pos += 1
    value = byte & 0x7F
    if (byte & 0x60) == 0x60:
        value |= -0x20
    while byte & 0x80:
        byte = data[pos]
        pos += 1
        value = (value << 7) | (byte & 0x7F)
    return value, pos


def encode_block(sequence: int, content: bytes) -> bytes:
    length = MESSAGE_HEADER + len(content) + MESSAGE_TRAILER
    if length > MESSAGE_MAX:
        raise IdentifyError("message too long")
    head = bytes((length, MESSAGE_DEST | (sequence & MESSAGE_SEQ_MASK)))
    body = head + content
    return body + crc16_ccitt(body) + bytes((MESSAGE_SYNC,))


def split_blocks(buffer: bytearray) -> list:
    """Remove and return every complete, CRC-valid block in `buffer`."""
    blocks = []
    while buffer:
        if buffer[0] == MESSAGE_SYNC:
            del buffer[0]
            continue
        length = buffer[0]
        if length < MESSAGE_MIN or length > MESSAGE_MAX:
            del buffer[0]
            continue
        if len(buffer) < length:
            break
        block = bytes(buffer[:length])
        if block[-1] != MESSAGE_SYNC or \
                crc16_ccitt(block[:-3]) != block[-3:-1]:
            del buffer[0]
            continue
        del buffer[:length]
        blocks.append(block)
    return blocks


class KlipperPort:
    """The smallest Klipper host: enough to fetch the data dictionary."""

    def __init__(self, path: str, *, clock=time.monotonic):
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self.clock = clock
        self.sequence = 0
        self.buffer = bytearray()
        try:
            import termios
            import tty
            tty.setraw(self.fd)
            attributes = termios.tcgetattr(self.fd)
            termios.tcsetattr(self.fd, termios.TCSANOW, attributes)
        except Exception:
            pass

    def close(self) -> None:
        os.close(self.fd)

    def _read_blocks(self, deadline: float) -> list:
        while True:
            remaining = deadline - self.clock()
            if remaining <= 0:
                return []
            readable, _, _ = select.select([self.fd], [], [], remaining)
            if not readable:
                return []
            try:
                data = os.read(self.fd, 4096)
            except BlockingIOError:
                continue
            except OSError as exc:
                raise IdentifyError("port read failed: %s" % exc)
            self.buffer.extend(data)
            blocks = split_blocks(self.buffer)
            if blocks:
                return blocks

    def request(self, content: bytes, timeout: float = 1.0) -> bytes:
        """Send one command; return the content of the first reply."""
        for _attempt in range(6):
            os.write(self.fd, encode_block(self.sequence, content))
            deadline = self.clock() + timeout
            while True:
                blocks = self._read_blocks(deadline)
                if not blocks:
                    break
                resent = False
                for block in blocks:
                    payload = block[MESSAGE_HEADER:-MESSAGE_TRAILER]
                    received = block[1] & MESSAGE_SEQ_MASK
                    if payload:
                        self.sequence = received
                        return payload
                    if received != ((self.sequence + 1) & MESSAGE_SEQ_MASK):
                        # A plain ack carrying a different sequence is the
                        # MCU saying which sequence it expects: adopt it and
                        # send again.
                        self.sequence = received
                        resent = True
                        break
                if resent:
                    break
        raise IdentifyError("no reply from the AMS")

    def data_dictionary(self) -> dict:
        data = bytearray()
        while True:
            content = encode_vlq(IDENTIFY_REQUEST_ID) + \
                encode_vlq(len(data)) + encode_vlq(CHUNK)
            reply = self.request(content)
            msg_id, pos = decode_vlq(reply, 0)
            if msg_id != IDENTIFY_RESPONSE_ID:
                continue
            offset, pos = decode_vlq(reply, pos)
            if offset & 0xFFFFFFFF != len(data):
                continue
            # A buffer parameter is one raw length byte, then the bytes.
            length = reply[pos]
            pos += 1
            chunk = reply[pos:pos + length]
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > 1024 * 1024:
                raise IdentifyError("data dictionary is too large")
        try:
            return json.loads(zlib.decompress(bytes(data)))
        except (zlib.error, ValueError) as exc:
            raise IdentifyError("unreadable data dictionary: %s" % exc)


def describe_unit(path: str) -> dict:
    """Return {"family", "bays", "name", "version"} for one AMS port."""
    port = KlipperPort(path)
    try:
        dictionary = port.data_dictionary()
    finally:
        port.close()
    constants = dictionary.get("config", {})
    family = str(constants.get("AMS_FAMILY") or "").strip().lower()
    if family not in FAMILY_NAMES:
        board = str(constants.get("BOARD_NAME") or "").strip().lower()
        family = {"ams-ht": "ams_ht"}.get(board, board)
    try:
        bays = int(constants.get("AMS_BAYS"))
    except (TypeError, ValueError):
        bays = 1 if family == "ams_ht" else 4
    return {
        "family": family if family in FAMILY_NAMES else "unknown",
        "bays": bays,
        "name": FAMILY_NAMES.get(family, "unknown AMS"),
        "version": str(dictionary.get("version", "")),
    }
