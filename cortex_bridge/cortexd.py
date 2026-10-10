# SPDX-License-Identifier: MIT
"""Expose the AMS units behind a Cortex Bridge as stable Klipper PTYs.

The bridge multiplexes one Klipper serial session per AMS unit over its USB
serial port (the "runtime mux"). This module passes that Klipper traffic
through unchanged and limits itself to strict framing, the unit roster, flow
control, and routing each unit to its own PTY, linked as <device_dir>/<uid>.
"""

from __future__ import annotations

import dataclasses
import errno
import fcntl
import os
import pathlib
import selectors
import time
import tty
from collections import deque
from enum import IntEnum
from typing import Callable, Deque, Iterable, Protocol

VERSION = 4
HEADER_SIZE = 4
UID_SIZE = 6
MAX_NODES = 4
MAX_CREDITS = 4
MAX_SERIAL = 64
ROSTER_ENTRY_SIZE = UID_SIZE + 1
ROSTER_PAYLOAD_SIZE = 5 + MAX_NODES * ROSTER_ENTRY_SIZE
NODE_DATA_PAYLOAD_SIZE = UID_SIZE + 1 + MAX_SERIAL
MAX_PAYLOAD = max(ROSTER_PAYLOAD_SIZE, NODE_DATA_PAYLOAD_SIZE)
MAX_MESSAGE = HEADER_SIZE + MAX_PAYLOAD
NODE_FLAG_ONLINE = 0x01
PTY_READ_SIZE = 512
PTY_WRITE_MAX = 4096
MAX_CLEARTEXT_BUFFER = MAX_MESSAGE * 32
DAEMON_SERVICE_INTERVAL = 0.5
PTY_REOPEN_RETRY_INTERVAL = 0.25
KLIPPER_STK500V2_LEAVE = b"\x1b\x01\x00\x01\x0e\x11\x04"

_connection_epoch = 0


def _new_connection_epoch() -> int:
    """Return a process-local nonzero epoch for one opened USB transport."""
    global _connection_epoch
    _connection_epoch = (_connection_epoch + 1) & 0xFFFFFFFF
    if _connection_epoch == 0:
        _connection_epoch = 1
    return _connection_epoch


class ProtocolError(RuntimeError):
    """The peer violated the runtime mux contract."""


class MessageType(IntEnum):
    ROSTER = 1
    HOST_DATA = 2
    NODE_DATA = 3


class Direction(IntEnum):
    HOST_TO_BRIDGE = 0
    BRIDGE_TO_HOST = 1


@dataclasses.dataclass(frozen=True)
class NodeStatus:
    uid: bytes
    online: bool


def _valid_uid(uid: bytes) -> bool:
    return len(uid) == UID_SIZE and uid != bytes(UID_SIZE) and \
        uid != b"\xff" * UID_SIZE


def _validate_node(node: NodeStatus) -> None:
    if not _valid_uid(node.uid):
        raise ProtocolError("invalid node UID")


def _validate_serial(data: bytes, allow_multiple: bool) -> None:
    if not 5 <= len(data) <= MAX_SERIAL:
        raise ProtocolError("invalid serial payload length")
    offset = 0
    frames = 0
    while offset < len(data):
        length = data[offset]
        if length < 5 or length > len(data) - offset or \
                data[offset + length - 1] != 0x7E:
            raise ProtocolError("invalid Klipper frame boundary")
        offset += length
        frames += 1
    if offset != len(data) or (not allow_multiple and frames != 1):
        raise ProtocolError("invalid Klipper serial stream")


@dataclasses.dataclass(frozen=True)
class Message:
    kind: MessageType
    payload: bytes

    def encode(self, direction: Direction) -> bytes:
        _validate_message(self, direction)
        return bytes((VERSION, int(self.kind))) + \
            len(self.payload).to_bytes(2, "big") + self.payload

    @staticmethod
    def decode(encoded: bytes, direction: Direction) -> "Message":
        if not HEADER_SIZE <= len(encoded) <= MAX_MESSAGE:
            raise ProtocolError("invalid runtime mux message length")
        if encoded[0] != VERSION:
            raise ProtocolError("unsupported runtime mux version")
        try:
            kind = MessageType(encoded[1])
        except ValueError as exc:
            raise ProtocolError("invalid runtime mux message type") from exc
        length = int.from_bytes(encoded[2:4], "big")
        if length > MAX_PAYLOAD or len(encoded) != HEADER_SIZE + length:
            raise ProtocolError("runtime mux payload length mismatch")
        message = Message(kind, bytes(encoded[HEADER_SIZE:]))
        _validate_message(message, direction)
        return message


def _validate_message(message: Message, direction: Direction) -> None:
    if direction == Direction.HOST_TO_BRIDGE:
        if message.kind != MessageType.HOST_DATA:
            raise ProtocolError("bridge-only runtime mux message from host")
    elif message.kind not in (MessageType.ROSTER, MessageType.NODE_DATA):
        raise ProtocolError("host-only runtime mux message from bridge")
    if len(message.payload) > MAX_PAYLOAD:
        raise ProtocolError("runtime mux payload is too large")

    payload = message.payload
    if message.kind == MessageType.ROSTER:
        parse_roster(message)
    elif message.kind == MessageType.HOST_DATA:
        if len(payload) <= UID_SIZE or not _valid_uid(payload[:UID_SIZE]):
            raise ProtocolError("invalid host-data UID or payload")
        _validate_serial(payload[UID_SIZE:], False)
    elif message.kind == MessageType.NODE_DATA:
        if len(payload) < UID_SIZE + 1 or \
                not _valid_uid(payload[:UID_SIZE]):
            raise ProtocolError("invalid node-data UID or payload")
        returned = payload[UID_SIZE]
        if returned > MAX_CREDITS:
            raise ProtocolError("invalid returned node credits")
        frames = payload[UID_SIZE + 1:]
        if not frames:
            if returned == 0:
                raise ProtocolError("empty node-data message")
        else:
            _validate_serial(frames, True)


def make_roster(generation: int, nodes: Iterable[NodeStatus]) -> Message:
    entries = tuple(nodes)
    if len(entries) > MAX_NODES:
        raise ProtocolError("roster is too large")
    payload = bytearray((generation & 0xFFFFFFFF).to_bytes(4, "big"))
    payload.append(len(entries))
    seen: set[bytes] = set()
    for node in entries:
        _validate_node(node)
        if node.uid in seen:
            raise ProtocolError("duplicate UID in roster")
        seen.add(node.uid)
        payload.extend(node.uid)
        payload.append(NODE_FLAG_ONLINE if node.online else 0)
    return Message(MessageType.ROSTER, bytes(payload))


def parse_roster(message: Message) -> tuple[int, tuple[NodeStatus, ...]]:
    payload = message.payload
    if message.kind != MessageType.ROSTER or len(payload) < 5:
        raise ProtocolError("invalid roster message")
    count = payload[4]
    if count > MAX_NODES or len(payload) != 5 + count * ROSTER_ENTRY_SIZE:
        raise ProtocolError("invalid roster length")
    nodes: list[NodeStatus] = []
    seen: set[bytes] = set()
    for index in range(count):
        offset = 5 + index * ROSTER_ENTRY_SIZE
        uid = payload[offset:offset + UID_SIZE]
        flags = payload[offset + UID_SIZE]
        if flags & ~NODE_FLAG_ONLINE:
            raise ProtocolError("reserved roster flags are nonzero")
        node = NodeStatus(uid, bool(flags & NODE_FLAG_ONLINE))
        _validate_node(node)
        if uid in seen:
            raise ProtocolError("duplicate UID in roster")
        seen.add(uid)
        nodes.append(node)
    return int.from_bytes(payload[:4], "big"), tuple(nodes)


def make_host_data(uid: bytes, frame: bytes) -> Message:
    if not _valid_uid(uid):
        raise ProtocolError("invalid host-data UID")
    _validate_serial(frame, False)
    return Message(MessageType.HOST_DATA, uid + frame)


def parse_host_data(message: Message) -> tuple[bytes, bytes]:
    if message.kind != MessageType.HOST_DATA:
        raise ProtocolError("not a host-data message")
    _validate_message(message, Direction.HOST_TO_BRIDGE)
    return message.payload[:UID_SIZE], message.payload[UID_SIZE:]


def make_node_data(uid: bytes, returned: int, frames: bytes = b"") -> Message:
    if not _valid_uid(uid) or not 0 <= returned <= MAX_CREDITS:
        raise ProtocolError("invalid node-data UID or credits")
    if frames:
        _validate_serial(frames, True)
    elif returned == 0:
        raise ProtocolError("empty node-data message")
    return Message(MessageType.NODE_DATA, uid + bytes((returned,)) + frames)


def parse_node_data(message: Message) -> tuple[bytes, int, bytes]:
    if message.kind != MessageType.NODE_DATA:
        raise ProtocolError("not a node-data message")
    _validate_message(message, Direction.BRIDGE_TO_HOST)
    return (message.payload[:UID_SIZE], message.payload[UID_SIZE],
            message.payload[UID_SIZE + 1:])


class KlipperFrameDecoder:
    """Split an untrusted PTY byte stream into bounded Klipper frames."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.reset_metrics()

    def reset_metrics(self) -> None:
        """Start counters for a new bridge USB connection epoch."""
        self.errors = 0
        self.resync_markers = 0
        self.startup_preambles = 0

    def feed(self, data: bytes) -> list[bytes]:
        self.buffer.extend(data)
        frames: list[bytes] = []
        while self.buffer:
            # Klipper writes this raw stk500v2 leave-programming command before
            # its first framed identify request on every UART connection. It
            # is not a Klipper frame, but its first byte looks like a plausible
            # 27-byte length and would otherwise head-block the real request.
            if self.buffer.startswith(KLIPPER_STK500V2_LEAVE):
                del self.buffer[:len(KLIPPER_STK500V2_LEAVE)]
                self.startup_preambles += 1
                continue
            # Klipper prepends a bare sync byte when retransmitting its
            # pending UART frames. The mux already restores a trusted frame
            # boundary before forwarding each retransmission, so consume the
            # marker here without treating normal recovery as malformed data.
            if self.buffer[0] == 0x7E:
                del self.buffer[0]
                self.resync_markers += 1
                continue
            length = self.buffer[0]
            if length < 5 or length > MAX_SERIAL:
                del self.buffer[0]
                self.errors += 1
                continue
            if len(self.buffer) < length:
                break
            if self.buffer[length - 1] != 0x7E:
                del self.buffer[0]
                self.errors += 1
                continue
            frames.append(bytes(self.buffer[:length]))
            del self.buffer[:length]
        if len(self.buffer) > MAX_SERIAL:
            self.buffer.clear()
            self.errors += 1
        return frames

    def clear(self) -> None:
        self.buffer.clear()


class NodeEndpoint:
    """One stable UID PTY with credit-controlled host reads."""

    def __init__(self, uid: bytes, device_dir: pathlib.Path):
        if not _valid_uid(uid):
            raise ProtocolError("invalid PTY UID")
        self.uid = uid
        self.online = False
        self.credits = 0
        self.frames: Deque[bytes] = deque()
        self.to_host = bytearray()
        self.decoder = KlipperFrameDecoder()
        self.host_read_retry_at = 0.0
        device_dir.mkdir(parents=True, exist_ok=True)
        master_fd, slave_fd = os.openpty()
        try:
            tty.setraw(master_fd)
            tty.setraw(slave_fd)
            os.set_blocking(master_fd, False)
            # devpts normally creates 0620 root:tty-style endpoints. Use the
            # managed directory's group as the access contract so Klipper can
            # open the PTY through its dialout membership while the daemon
            # remains unprivileged.
            os.fchown(slave_fd, -1, device_dir.stat().st_gid)
            os.fchmod(slave_fd, 0o660)
            slave_name = os.ttyname(slave_fd)
        except BaseException:
            os.close(master_fd)
            raise
        finally:
            os.close(slave_fd)
        self.master_fd = master_fd
        self.path = device_dir / uid.hex()
        if self.path.is_symlink():
            self.path.unlink()
        elif self.path.exists():
            os.close(self.master_fd)
            raise ProtocolError(
                f"refusing to replace non-symlink device path {self.path}"
            )
        self.path.symlink_to(slave_name)

    def apply(self, status: NodeStatus) -> None:
        if status.uid != self.uid:
            raise ProtocolError("PTY status UID mismatch")
        _validate_node(status)
        was_online = self.online
        self.online = status.online
        if not self.online:
            self.credits = 0
            self.frames.clear()
            # A reply that could not be written before this authoritative
            # offline transition belongs to the old node session. Never flush
            # it after the node disappears or into a later online session.
            self.to_host.clear()
            self.decoder.clear()
            self._discard_host_input()
        elif not was_online:
            # Bytes typed while the target was absent have no reconnect-safe
            # destination and must not spring to life on rediscovery.
            self.frames.clear()
            self.decoder.clear()
            self._discard_host_input()
            self.credits = MAX_CREDITS

    def read_host(self) -> None:
        # collect_outbound() may call this directly after the selector path
        # already observed an unopened PTY. Enforce the retry deadline here,
        # not only at selector registration, so one service iteration cannot
        # perform a second EIO read and extend the cooldown indefinitely.
        if not self.wants_host_read():
            return
        try:
            data = os.read(self.master_fd, PTY_READ_SIZE)
        except OSError as exc:
            if exc.errno == errno.EIO:
                # A PTY master reports EIO while no process has its slave
                # open. Select keeps reporting that condition as readable, so
                # temporarily remove it from the selector to avoid a CPU spin.
                self.host_read_retry_at = time.monotonic() + \
                    PTY_REOPEN_RETRY_INTERVAL
                return
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            raise
        if data:
            self.host_read_retry_at = 0.0
            self.frames.extend(self.decoder.feed(data))

    def wants_host_read(self, now: float | None = None) -> bool:
        if now is None:
            now = time.monotonic()
        # Keep one complete frame outside the PTY even when every bridge
        # credit is in flight. Klipper closes the slave immediately after its
        # one-way reset command; bytes left only in the PTY can disappear at
        # that close and leave the MCU on the old serial sequence forever.
        # The decoder and one-frame gate keep this eager read strictly bounded.
        return self.online and not self.frames and \
            now >= self.host_read_retry_at

    def take_frames(self) -> list[bytes]:
        output: list[bytes] = []
        while self.online and self.credits and self.frames:
            output.append(self.frames.popleft())
            self.credits -= 1
        return output

    def return_credits(self, returned: int) -> None:
        if not self.online or not 0 <= returned <= MAX_CREDITS or \
                self.credits + returned > MAX_CREDITS:
            raise ProtocolError("invalid returned node credits")
        self.credits += returned

    def write_host(self, frames: bytes) -> None:
        _validate_serial(frames, True)
        if len(self.to_host) + len(frames) > PTY_WRITE_MAX:
            raise ProtocolError("PTY output backpressure exceeded")
        self.to_host.extend(frames)
        self.flush_host()

    def flush_host(self) -> None:
        while self.to_host:
            try:
                written = os.write(self.master_fd, self.to_host)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    # No slave is open. A reply from the previous Klipper
                    # connection must not be delivered to the next opener.
                    self.to_host.clear()
                    return
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    return
                raise
            if written <= 0:
                return
            del self.to_host[:written]

    def revoke(self) -> None:
        self.online = False
        self.credits = 0
        self.frames.clear()
        self.to_host.clear()
        self.decoder.clear()
        self._discard_host_input()

    def _discard_host_input(self) -> None:
        while True:
            try:
                if not os.read(self.master_fd, PTY_READ_SIZE):
                    break
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK,
                                  errno.EIO):
                    if exc.errno == errno.EIO:
                        self.host_read_retry_at = time.monotonic() + \
                            PTY_REOPEN_RETRY_INTERVAL
                    break
                raise

    def close(self) -> None:
        os.close(self.master_fd)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class CortexCore:
    """Roster/PTY state; it never probes the RS485 network."""

    def __init__(self, device_dir: pathlib.Path,
                 report_event: Callable[..., None] | None = None):
        self.device_dir = device_dir
        self.nodes: dict[bytes, NodeEndpoint] = {}
        self.connection_epoch: int | None = None
        self.roster_generation: int | None = None
        self.report_event = report_event
        self.bridge_to_host_messages = 0
        self.bridge_to_host_bytes = 0
        self.host_to_bridge_messages = 0
        self.host_to_bridge_bytes = 0
        self.device_dir.mkdir(parents=True, exist_ok=True)
        self.owner_fd = os.open(
            self.device_dir / ".cortexd.lock",
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC,
            0o600,
        )
        try:
            fcntl.flock(self.owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self.owner_fd)
            self.owner_fd = -1
            raise ProtocolError(
                f"another daemon owns {self.device_dir}"
            ) from exc
        self._remove_stale_managed_links()

    def _remove_stale_managed_links(self) -> None:
        """Remove UID links orphaned when a prior process died uncleanly."""
        for path in self.device_dir.iterdir():
            try:
                uid = bytes.fromhex(path.name)
            except ValueError:
                continue
            if len(uid) != UID_SIZE or uid.hex() != path.name or \
                    not path.is_symlink():
                continue
            path.unlink()
            self._report(
                "stale_device_removed", node=path.name,
                device_link=str(path),
            )

    def _report(self, event: str, **fields) -> None:
        if self.report_event is not None:
            self.report_event(event, **fields)

    def attach(self, connection_epoch: int) -> None:
        if not 1 <= connection_epoch <= 0xFFFFFFFF:
            raise ProtocolError("invalid USB connection epoch")
        if self.connection_epoch is not None and \
                self.connection_epoch != connection_epoch:
            raise ProtocolError("another USB connection is attached")
        for endpoint in self.nodes.values():
            endpoint.revoke()
            endpoint.decoder.reset_metrics()
        self.connection_epoch = connection_epoch
        self.roster_generation = None
        self.bridge_to_host_messages = 0
        self.bridge_to_host_bytes = 0
        self.host_to_bridge_messages = 0
        self.host_to_bridge_bytes = 0
        self._report("runtime_attach")

    def detach(self, connection_epoch: int) -> None:
        if self.connection_epoch is None or \
                self.connection_epoch != connection_epoch:
            raise ProtocolError("connection-exact detach required")
        for endpoint in self.nodes.values():
            endpoint.revoke()
        self.connection_epoch = None
        self.roster_generation = None
        self._report("runtime_detach")

    def _endpoint(self, uid: bytes) -> NodeEndpoint:
        endpoint = self.nodes.get(uid)
        if endpoint is None:
            endpoint = NodeEndpoint(uid, self.device_dir)
            self.nodes[uid] = endpoint
            self._report(
                "node_created", node=uid.hex(),
                device_link=str(endpoint.path),
            )
        return endpoint

    def accept(self, encoded: bytes) -> None:
        if self.connection_epoch is None:
            raise ProtocolError("runtime mux message before USB attach")
        message = Message.decode(encoded, Direction.BRIDGE_TO_HOST)
        self.bridge_to_host_messages += 1
        self.bridge_to_host_bytes += len(encoded)
        if message.kind == MessageType.ROSTER:
            generation, statuses = parse_roster(message)
            present = {status.uid for status in statuses}
            for uid, endpoint in self.nodes.items():
                if uid not in present:
                    endpoint.apply(NodeStatus(uid, False))
            for status in statuses:
                self._endpoint(status.uid).apply(status)
            self.roster_generation = generation
            self._report(
                "runtime_roster", generation=generation,
                nodes=len(statuses),
                online=sum(1 for status in statuses if status.online),
            )
            return
        if self.roster_generation is None:
            raise ProtocolError("node data before roster snapshot")
        if message.kind == MessageType.NODE_DATA:
            uid, returned, frames = parse_node_data(message)
            endpoint = self.nodes.get(uid)
            if endpoint is None:
                raise ProtocolError("node data from unknown UID")
            if not endpoint.online:
                raise ProtocolError("node data from offline UID")
            endpoint.return_credits(returned)
            if frames:
                endpoint.write_host(frames)

    def collect_outbound(self) -> list[bytes]:
        if self.connection_epoch is None or self.roster_generation is None:
            return []
        output: list[bytes] = []
        for uid in sorted(self.nodes):
            endpoint = self.nodes[uid]
            endpoint.read_host()
            for frame in endpoint.take_frames():
                encoded = make_host_data(uid, frame).encode(
                    Direction.HOST_TO_BRIDGE
                )
                output.append(encoded)
                self.host_to_bridge_messages += 1
                self.host_to_bridge_bytes += len(encoded)
        return output

    def support_snapshot(self) -> dict[str, int]:
        """Return aggregate transport facts; never return payload material."""
        return {
            "generation": self.roster_generation or 0,
            "nodes": len(self.nodes),
            "online": sum(
                1 for endpoint in self.nodes.values() if endpoint.online
            ),
            "bridge_to_host_messages": self.bridge_to_host_messages,
            "bridge_to_host_bytes": self.bridge_to_host_bytes,
            "host_to_bridge_messages": self.host_to_bridge_messages,
            "host_to_bridge_bytes": self.host_to_bridge_bytes,
            "invalid_host_frames": sum(
                endpoint.decoder.errors for endpoint in self.nodes.values()
            ),
            "klipper_resync_markers": sum(
                endpoint.decoder.resync_markers
                for endpoint in self.nodes.values()
            ),
            "klipper_startup_preambles": sum(
                endpoint.decoder.startup_preambles
                for endpoint in self.nodes.values()
            ),
        }

    def close(self) -> None:
        for endpoint in tuple(self.nodes.values()):
            endpoint.close()
        self.nodes.clear()
        self.connection_epoch = None
        self.roster_generation = None
        if self.owner_fd >= 0:
            fcntl.flock(self.owner_fd, fcntl.LOCK_UN)
            os.close(self.owner_fd)
            self.owner_fd = -1


class RuntimeTransport(Protocol):
    """Validated runtime-mux transport."""

    connection_epoch: int

    def fileno(self) -> int: ...
    def recv_messages(self) -> list[bytes]: ...
    def send_message(self, message: bytes) -> None: ...
    def service(self) -> None: ...
    def close(self) -> None: ...


class RecordPort(Protocol):
    """Common pyserial/socket-file byte-stream surface."""

    def fileno(self) -> int: ...
    def read(self, length: int) -> bytes: ...
    def write(self, data: bytes) -> int | None: ...
    def flush(self) -> None: ...
    def close(self) -> None: ...


class CleartextMuxTransport:
    """Unencrypted runtime_mux messages over the locally attached bridge USB."""

    def __init__(self, port: RecordPort,
                 connection_epoch: int | None = None,
                 initial: bytes = b""):
        if connection_epoch is None:
            connection_epoch = _new_connection_epoch()
        if not 1 <= connection_epoch <= 0xFFFFFFFF:
            raise ProtocolError("invalid USB connection epoch")
        self.port = port
        self.connection_epoch = connection_epoch
        if len(initial) > MAX_CLEARTEXT_BUFFER:
            raise ProtocolError("initial runtime mux input is oversized")
        self.input = bytearray(initial)
        self.output = bytearray()
        self.closed = False

    @classmethod
    def connect(cls, port: RecordPort,
                initial: bytes = b"") -> "CleartextMuxTransport":
        set_nonblocking = getattr(port, "set_nonblocking", None)
        if callable(set_nonblocking):
            set_nonblocking()
        return cls(port, initial=initial)

    def fileno(self) -> int:
        return self.port.fileno()

    def recv_messages(self) -> list[bytes]:
        if self.closed:
            raise ProtocolError("runtime transport is closed")
        try:
            try:
                chunk = self.port.read(MAX_CLEARTEXT_BUFFER - len(self.input))
            except (BlockingIOError, InterruptedError):
                chunk = b""
            if chunk:
                self.input.extend(chunk)
            if len(self.input) > MAX_CLEARTEXT_BUFFER:
                raise ProtocolError("runtime mux input overflow")
            output: list[bytes] = []
            while len(self.input) >= HEADER_SIZE:
                length = HEADER_SIZE + int.from_bytes(self.input[2:4], "big")
                if length > MAX_MESSAGE:
                    raise ProtocolError("runtime mux message is oversized")
                if len(self.input) < length:
                    break
                encoded = bytes(self.input[:length])
                del self.input[:length]
                Message.decode(encoded, Direction.BRIDGE_TO_HOST)
                output.append(encoded)
            return output
        except Exception as exc:
            self.close()
            if isinstance(exc, ProtocolError):
                raise
            raise ProtocolError("runtime mux receive failed") from exc

    def send_message(self, message: bytes) -> None:
        if self.closed:
            raise ProtocolError("runtime transport is closed")
        try:
            Message.decode(message, Direction.HOST_TO_BRIDGE)
            if len(self.output) + len(message) > MAX_CLEARTEXT_BUFFER:
                raise ProtocolError("runtime mux output backpressure exceeded")
            self.output.extend(message)
            self._flush_output()
        except Exception as exc:
            self.close()
            if isinstance(exc, ProtocolError):
                raise
            raise ProtocolError("runtime mux send failed") from exc

    def _flush_output(self) -> None:
        # pyserial with write_timeout=0 may write only a prefix or zero bytes.
        # Raw nonblocking file ports may instead return None or raise EAGAIN.
        # Never report those bytes as sent or carry them into a new connection.
        while self.output:
            try:
                written = self.port.write(bytes(self.output))
            except (BlockingIOError, InterruptedError):
                return
            if written is None or written == 0:
                return
            if written < 0 or written > len(self.output):
                raise ProtocolError("runtime mux write failed")
            del self.output[:written]

    def service(self) -> None:
        if self.closed:
            raise ProtocolError("runtime transport is closed")
        try:
            service = getattr(self.port, "service", None)
            if callable(service):
                service()
            self._flush_output()
        except Exception as exc:
            self.close()
            if isinstance(exc, ProtocolError):
                raise
            raise ProtocolError("runtime mux service failed") from exc

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.input.clear()
        self.output.clear()
        self.port.close()


class CortexDaemon:
    """Small event loop; bridge events, not host timers, drive topology."""

    def __init__(self, transport: RuntimeTransport,
                 device_dir: pathlib.Path,
                 core: CortexCore | None = None):
        self.transport = transport
        if core is not None and core.device_dir != device_dir:
            raise ProtocolError("retained cortexd device directory changed")
        owns_core = core is None
        self.core = CortexCore(device_dir) if owns_core else core
        self.selector = None
        try:
            self.core.attach(transport.connection_epoch)
            self.selector = selectors.DefaultSelector()
            self.selector.register(transport.fileno(), selectors.EVENT_READ,
                                   "bridge")
        except BaseException:
            if self.selector is not None:
                self.selector.close()
            if self.core.connection_epoch == transport.connection_epoch:
                self.core.detach(transport.connection_epoch)
            if owns_core:
                self.core.close()
            self.transport.close()
            raise
        self.registered_ptys: set[int] = set()
        self.running = True

    def _sync_ptys(self) -> None:
        now = time.monotonic()
        wanted = {
            endpoint.master_fd for endpoint in self.core.nodes.values()
            if endpoint.wants_host_read(now)
        }
        for fd in self.registered_ptys - wanted:
            self.selector.unregister(fd)
        for endpoint in self.core.nodes.values():
            if endpoint.master_fd in wanted - self.registered_ptys:
                self.selector.register(endpoint.master_fd,
                                       selectors.EVENT_READ, endpoint)
        self.registered_ptys = wanted

    def _send_available(self) -> None:
        for message in self.core.collect_outbound():
            self.transport.send_message(message)

    def step(self, timeout: float | None = None) -> None:
        self.transport.service()
        self._sync_ptys()
        service_timeout = DAEMON_SERVICE_INTERVAL if timeout is None else \
            min(timeout, DAEMON_SERVICE_INTERVAL)
        for key, _ in self.selector.select(service_timeout):
            if key.data == "bridge":
                for message in self.transport.recv_messages():
                    self.core.accept(message)
                self._send_available()
            else:
                endpoint: NodeEndpoint = key.data
                endpoint.read_host()
                self._send_available()
        for endpoint in self.core.nodes.values():
            endpoint.flush_host()
        self.transport.service()
        self._sync_ptys()

    def _close(self, close_core: bool) -> None:
        if not self.running:
            return
        self.running = False
        if self.core.connection_epoch is not None:
            self.core.detach(self.transport.connection_epoch)
        self.selector.close()
        if close_core:
            self.core.close()
        self.transport.close()

    def detach(self) -> None:
        """Detach one bridge link while retaining stable UID PTYs."""
        self._close(False)

    def close(self) -> None:
        self._close(True)


__all__ = [
    "CleartextMuxTransport", "RuntimeTransport", "CortexCore",
    "CortexDaemon", "Direction",
    "KlipperFrameDecoder", "Message", "MessageType", "NodeEndpoint",
    "NodeStatus", "ProtocolError", "make_host_data", "make_node_data",
    "make_roster", "parse_host_data", "parse_node_data", "parse_roster",
]
