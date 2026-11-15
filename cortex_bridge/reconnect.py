# SPDX-License-Identifier: MIT
"""After a bridge firmware install: which AMS came back, and when Klipper
gets one more FIRMWARE_RESTART.

Shared by `cortex-bridge.py update` and the Moonraker component. Nothing here
waits or talks to anything; the callers poll the relay's status file and
Moonraker and ask these functions what they see.

The AMS units come back one by one after the bridge restarts (in a customer's
relay log, bridge 29, an AMS 2 Pro 1.8 s and an AMS HT 2.8 s after the
install), so the update waits for every unit that was online before it, not
for the first one. From bridge 30 the bridge first polls the bus indices the
units kept (3.6 s at most) and offers no index for 6 s; a unit that has to
be given an index comes back after that hold.
"""

from __future__ import annotations

# How long the update waits for the units that were online before it. A
# minute is far more than a unit takes, and still ends before Klipper gives
# up on a silent [mcu] port (90 s), so the report comes first.
UNITS_BACK_WAIT = 60.0
# With no unit expected (none online and none configured before the update):
# how long to look for any.
ANY_UNIT_WAIT = 15.0
# While units nobody expected may still arrive (`config` after a bridge
# start): how long the set of online units must stay the same. From bridge
# 30 a unit that must be given a bus index comes after the 6 s offer hold.
SETTLE_SECONDS = 8.0

# Klipper messages after a FIRMWARE_RESTART that one more FIRMWARE_RESTART
# clears, once every AMS is back: an AMS whose port still reports the stop of
# an earlier Klipper (the bridge keeps it until a reset from a connected
# Klipper), or one that answered after Klipper had given up on it.
SHUTDOWN_AT_START = "config as it is shutdown"
RESTART_CLEARS = (
    SHUTDOWN_AT_START,
    "Failed automated reset of MCU",
    "Unable to connect",
    "Lost communication with MCU",
)


def online_uids(status) -> list:
    """The UIDs the relay lists online, sorted."""
    if not status:
        return []
    return sorted(str(unit.get("uid")) for unit in status.get("units", [])
                  if unit.get("online") and unit.get("uid"))


def expected_uids(status_before, configured=()) -> list:
    """The units to wait for after the install: those online before it, or,
    when none was, the configured ones (a bridge that had no firmware running
    lists none online)."""
    return online_uids(status_before) or sorted(configured)


def progress(status, expected) -> dict:
    """``back`` (expected and online now), ``missing`` (expected, not online)
    and ``extra`` (online, not expected), each sorted."""
    online = set(online_uids(status))
    wanted = set(expected)
    return {"back": sorted(online & wanted),
            "missing": sorted(wanted - online),
            "extra": sorted(online - wanted)}


def label(uid: str, labels=None) -> str:
    """``uid`` with its kind when the printer config names it."""
    kind = (labels or {}).get(uid)
    return "%s (%s)" % (uid, kind) if kind else uid


def summary(expected, seen: dict, waited: float, labels=None) -> list:
    """The lines that end the wait. ``seen`` is ``progress()`` at its end.
    The count is of units online at that moment, so `status` agrees."""
    back, missing, extra = seen["back"], seen["missing"], seen["extra"]
    lines = []
    if not expected:
        if extra:
            lines.append("%d AMS online: %s." % (
                len(extra), ", ".join(label(uid, labels) for uid in extra)))
        else:
            lines.append("No AMS is online yet. Run `cortex-bridge.py status` "
                         "in a minute; it names what to check.")
        return lines
    if not missing:
        lines.append("%d of %d AMS back online." % (len(back), len(expected)))
    else:
        lines.append("%d of %d AMS back after %d s: %s not yet." % (
            len(back), len(expected), round(waited),
            ", ".join(label(uid, labels) for uid in missing)))
        one = len(missing) == 1
        lines.append(
            "%s: run `cortex-bridge.py status` in a minute. If %s offline, "
            "check %s power (an AMS HT has its own) and cables, then power "
            "%s off and on; %s back by itself once %s." % (
                "It may still be starting" if one
                else "They may still be starting",
                "it stays" if one else "they stay",
                "its" if one else "their",
                "that AMS" if one else "each of them",
                "it comes" if one else "each comes",
                "it answers" if one else "it answers"))
    if extra:
        lines.append("Also online: %s." % ", ".join(
            label(uid, labels) for uid in extra))
    return lines


def restart_clears(state, message) -> bool:
    """True when Klipper stopped in a way one more FIRMWARE_RESTART fixes."""
    return state in ("error", "shutdown") and \
        any(text in (message or "") for text in RESTART_CLEARS)


def restart_reason(message) -> str:
    """Why one more FIRMWARE_RESTART helps, in words."""
    if SHUTDOWN_AT_START in (message or "") or \
            "Failed automated reset of MCU" in (message or ""):
        return "an AMS still held the stop from before the restart"
    return "an AMS answered after Klipper had given up on it"


def first_line(message) -> str:
    message = (message or "").strip()
    return message.splitlines()[0] if message else ""
