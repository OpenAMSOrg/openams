# SPDX-License-Identifier: MIT
"""Bounded support logging for the Cortex Bridge relay.

Every record is a single JSON line whose fields come from a fixed allowlist,
so the log can be attached to a support request as it is: there is no field
through which Klipper frames, firmware bytes or anything else from the serial
streams can be written.
"""

from __future__ import annotations

import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re


LOG_SCHEMA = 1
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 4

_EVENT_FIELDS = {
    "service_start": frozenset((
        "daemon_version", "runtime_protocol", "port_selection",
        "pty_directory",
    )),
    "service_stop": frozenset(),
    "usb_connect": frozenset(("device",)),
    "usb_connected": frozenset(("device",)),
    "runtime_started": frozenset(("runtime_protocol",)),
    "runtime_attach": frozenset(),
    "runtime_detach": frozenset(),
    "runtime_roster": frozenset((
        "generation", "nodes", "online",
    )),
    "stale_device_removed": frozenset(("node", "device_link")),
    "node_created": frozenset(("node", "device_link")),
    "traffic_summary": frozenset((
        "generation", "nodes", "online", "bridge_to_host_messages",
        "bridge_to_host_bytes", "host_to_bridge_messages",
        "host_to_bridge_bytes", "invalid_host_frames",
        "klipper_resync_markers", "klipper_startup_preambles",
    )),
    "session_retry": frozenset(("phase", "reason")),
    "bridge_waiting": frozenset(("reason",)),
    "update_start": frozenset(("firmware_version", "source")),
    "update_phase": frozenset(("phase",)),
    "update_result": frozenset(("firmware_version", "result", "reason")),
}

_CODE_FIELDS = frozenset((
    "port_selection", "phase", "reason", "result", "source",
))
_PATH_FIELDS = frozenset(("device", "device_link", "pty_directory"))
_CODE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{0,63}$")
_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9._/+:-]{1,191}$")
_NODE_PATTERN = re.compile(r"^[0-9a-f]{12}$")


class SupportLogError(ValueError):
    """The caller attempted to write something outside the schema."""


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """Create every log generation with mode 0640."""

    def _open(self):
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.baseFilename, flags, 0o640)
        os.fchmod(descriptor, 0o640)
        return open(
            descriptor, self.mode, encoding=self.encoding,
            errors=self.errors, closefd=True,
        )


def _validated_value(field: str, value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value < 0 or value > 0xFFFFFFFFFFFFFFFF:
            raise SupportLogError(f"support-log integer is invalid: {field}")
        return value
    if not isinstance(value, str):
        raise SupportLogError(f"support-log value type is invalid: {field}")
    if field == "node":
        valid = _NODE_PATTERN.fullmatch(value)
    elif field in _CODE_FIELDS:
        valid = _CODE_PATTERN.fullmatch(value)
    elif field in _PATH_FIELDS:
        valid = _PATH_PATTERN.fullmatch(value)
    else:
        valid = None
    if valid is None:
        raise SupportLogError(f"support-log string is invalid: {field}")
    return value


class GatewaySupportLog:
    """Write only approved diagnostic facts to a small rotating JSONL file."""

    def __init__(self, path: Path, *, max_bytes: int = DEFAULT_MAX_BYTES,
                 backup_count: int = DEFAULT_BACKUP_COUNT):
        self.path = Path(path)
        if max_bytes < 256 or not 1 <= backup_count <= 16:
            raise SupportLogError("support-log rotation bounds are invalid")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        self._logger = logging.getLogger(
            f"openams.cortex_bridge.support.{id(self):x}"
        )
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self._handler = _PrivateRotatingFileHandler(
            self.path, maxBytes=max_bytes, backupCount=backup_count,
            encoding="utf-8",
        )
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(self._handler)

    def record(self, event_name: str, **fields) -> None:
        allowed = _EVENT_FIELDS.get(event_name)
        if allowed is None or set(fields) != allowed:
            raise SupportLogError("support-log event does not match schema")
        record = {
            "timestamp": datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "schema": LOG_SCHEMA,
            "record": event_name,
        }
        for field in sorted(fields):
            record[field] = _validated_value(field, fields[field])
        self._logger.info(json.dumps(
            record, sort_keys=True, separators=(",", ":"),
        ))

    def close(self) -> None:
        if self._handler is None:
            return
        self._logger.removeHandler(self._handler)
        self._handler.close()
        self._handler = None


__all__ = [
    "DEFAULT_BACKUP_COUNT", "DEFAULT_MAX_BYTES", "GatewaySupportLog",
    "LOG_SCHEMA", "SupportLogError",
]
