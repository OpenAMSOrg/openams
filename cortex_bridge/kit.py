"""The CyberBrick Time-lapse Kit through a Cortex Bridge (firmware 40 and
later): the requests the relay sends the bridge (turn the kit service on or
off, fire a shot) and the reports it sends back (whether the service is on,
whether the kit is there, the bridge's shot count and a request's status).
"""

from __future__ import annotations

import struct

from .cortexd import KIT_REPORT_PAYLOAD_SIZE, Message, MessageType

MIN_FIRMWARE = 40
METHOD = "kit"

_OP_SERVICE = 1
_OP_SHOT = 2

EVENTS = {0: "change", 1: "service", 2: "shot"}
SHOT_STATUS = {1: "queued", 3: "refused", 5: "not-permitted"}

REPORT_FORMAT = 2
REPORT_SIZE = KIT_REPORT_PAYLOAD_SIZE
_FLAG_SERVICE = 0x01
_FLAG_PRESENT = 0x02


def service(on: bool = True) -> Message:
    """The request that turns the kit service on (or off)."""
    return Message(MessageType.KIT_REQUEST,
                   bytes((_OP_SERVICE, 1 if on else 0)))


def shot(p: int = 1) -> Message:
    """The request that fires the kit, with M1004 S5's P value."""
    if not 0 <= p <= 0xFF:
        raise ValueError("P is 0 to 255")
    return Message(MessageType.KIT_REQUEST, bytes((_OP_SHOT, p)))


def decode(payload: bytes) -> dict:
    """A report as a JSON-able dict: the event, whether the service is on,
    whether the kit is there, the bridge's shot count, and a shot's status.
    Raises ValueError when it is not one this host understands."""
    if len(payload) != REPORT_SIZE or payload[0] != REPORT_FORMAT:
        raise ValueError("not a kit report this relay reads")
    event = EVENTS.get(payload[1], "unknown")
    flags = payload[3]
    report = {
        "event": event,
        "service": bool(flags & _FLAG_SERVICE),
        "present": bool(flags & _FLAG_PRESENT),
        "shots": struct.unpack_from("<H", payload, 4)[0],
    }
    if event == "shot":
        report["shot"] = SHOT_STATUS.get(payload[2], "unknown")
    return report
