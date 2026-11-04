# SPDX-License-Identifier: MIT
"""What a bridge firmware update did, in the words a customer reads.

Every update outcome becomes three sentences: what happened (``summary``),
whether anything changed and what state the bridge is in now (``outcome``),
and what to do next (``action``). The relay attaches them to its result line,
so `cortex-bridge.py`, Moonraker and any UI show the same text; the technical
message stays in ``detail`` for support.

``explain`` takes the error class name (or one of the relay's own refusals),
the error's ``reason_code``, and what is known: the version the bridge runs,
the package's version and the state the bridge was left in.
"""

from __future__ import annotations

SUPPORT = ("run `cortex-bridge.py support` and send the report it writes to "
           "support")
AGAIN = "run `cortex-bridge.py update` again"


def _still_runs(running) -> str:
    if running is None:
        return "Nothing was changed."
    return "Nothing was changed; the bridge still runs version %d." % running


def _left_in(post_state: str, running) -> str:
    if post_state == "receiver":
        return ("The bridge is waiting for firmware, so the AMS stay offline "
                "until an update finishes.")
    if post_state == "application":
        return _still_runs(running)
    if post_state == "fault":
        return "The bridge reports a fault and does not run its firmware."
    return "The bridge is not visible on USB right now."


def explain(error: str, *, reason_code: str = "", detail: str = "",
            running=None, package=None, post_state: str = "",
            fault_code: str = "", fault_text: str = "") -> dict:
    """Return {"summary", "outcome", "action"} for one update failure."""
    version = "version %d" % package if package else "this firmware"

    if error == "AlreadyInstalled":
        return {
            "summary": "The bridge already runs %s." % version,
            "outcome": "Nothing was changed.",
            "action": "Nothing to do: the bridge is up to date.",
        }
    if error == "OlderThanInstalled":
        return {
            "summary": "The bridge runs version %s, which is newer than %s."
                       % (running, version),
            "outcome": "Nothing was changed: a bridge only installs a version "
                       "newer than the one it runs.",
            "action": "Nothing to do. Newer firmware arrives with OpenAMS "
                      "updates.",
        }
    if error == "PrinterBusy":
        return {
            "summary": "The printer is %s." % (detail or "busy"),
            "outcome": "Nothing was changed.",
            "action": "Update the bridge when the printer is idle.",
        }
    if error == "UpdateModeRefused":
        if reason_code == "no-receiver":
            return {
                "summary": "The bridge left its firmware, but its update mode "
                           "did not appear on USB.",
                "outcome": "Nothing was installed.",
                "action": "Wait a minute, then run `cortex-bridge.py status`. "
                          "If it says the bridge is waiting for firmware, %s. "
                          "If the bridge is not on USB, unplug its USB cable "
                          "for 10 seconds and plug it back in." % AGAIN,
            }
        return {
            "summary": "The bridge did not switch to update mode.",
            "outcome": _still_runs(running),
            "action": "The bridge switches only while every AMS is idle: not "
                      "loading, unloading or moving filament, and not starting "
                      "or stopping a dryer. Wait until they are idle and %s."
                      % AGAIN,
        }
    if error == "PackageRefused":
        if reason_code == "boot-abi":
            return {
                "summary": "This firmware needs a newer boot than this bridge "
                           "has.",
                "outcome": "Nothing was installed; the bridge returns to its "
                           "firmware by itself within 30 seconds.",
                "action": "Contact support: %s." % SUPPORT,
            }
        return {
            "summary": "This file cannot be installed: %s."
                       % (detail or "it is not Cortex Bridge firmware"),
            "outcome": "Nothing was sent to the bridge.",
            "action": "Run `cortex-bridge.py update` without --file to install "
                      "the firmware bundled with OpenAMS. If support sent you "
                      "this file, ask them for a new copy.",
        }
    if error == "ReceiverRejected":
        if reason_code == "lab-boot":
            return {
                "summary": "This bridge has a development boot, which does not "
                           "install release firmware.",
                "outcome": "Nothing was installed; the bridge returns to its "
                           "firmware by itself within 30 seconds.",
                "action": "Contact support: %s." % SUPPORT,
            }
        outcome = _left_in(post_state, running)
        if running is not None and package is not None and package <= running:
            return {
                "summary": "The bridge refused %s because it already runs "
                           "version %d." % (version, running),
                "outcome": outcome,
                "action": "Nothing to do.",
            }
        if running is not None:
            return {
                "summary": "The bridge checked %s and refused it. It runs "
                           "version %d, so the version is not the reason: the "
                           "file was made for another board revision, or it "
                           "is not signed for this bridge." % (version, running),
                "outcome": outcome,
                "action": "If you gave a file with --file, run the update "
                          "without it to install the firmware bundled with "
                          "OpenAMS. Otherwise %s." % SUPPORT,
            }
        return {
            "summary": "The bridge checked %s and refused it. A bridge refuses "
                       "firmware that is not newer than the version it runs, "
                       "and firmware made for another board revision or not "
                       "signed for it." % version,
            "outcome": outcome,
            "action": "Run `cortex-bridge.py version` to see the version it "
                      "runs. If that is already this version or a newer one, "
                      "there is nothing to do. Otherwise %s." % SUPPORT,
        }
    if error == "TransferInterrupted":
        summary = ("The bridge accepted the firmware but did not finish "
                   "installing it." if reason_code == "not-finished" else
                   "The transfer to the bridge stopped part-way.")
        return {
            "summary": summary,
            "outcome": _left_in(post_state or "receiver", running),
            "action": "Keep USB and 24 V connected and %s; the bridge "
                      "finishes the update then." % AGAIN,
        }
    if error == "BridgeFault":
        code = fault_code or "unknown"
        return {
            "summary": "The bridge reports fault %s%s." % (
                code, ": %s" % fault_text if fault_text else ""),
            "outcome": "It does not run or install firmware in this state.",
            "action": "Unplug USB and 24 V for 10 seconds and reconnect them. "
                      "If the fault comes back, run `cortex-bridge.py "
                      "support` and send support the report with the code "
                      "%s." % code,
        }
    if error == "BridgeNotFound":
        if reason_code == "did-not-return":
            return {
                "summary": "The bridge accepted the firmware but did not come "
                           "back on USB.",
                "outcome": "The firmware may be installed.",
                "action": "Unplug the bridge's USB cable for 10 seconds and "
                          "plug it back in, then run `cortex-bridge.py "
                          "version`.",
            }
        return {
            "summary": "No Cortex Bridge is visible on USB.",
            "outcome": "Nothing was changed.",
            "action": "Check the USB-C cable (a data cable, not charge-only) "
                      "and that the bridge's white LED is on, then %s." % AGAIN,
        }
    if error == "BridgeAmbiguous":
        return {
            "summary": "More than one Cortex Bridge is connected.",
            "outcome": "Nothing was changed.",
            "action": "Disconnect every bridge except the one to update and "
                      "%s." % AGAIN,
        }
    if reason_code == "port-in-use":
        return {
            "summary": "Another program is using the bridge's USB port.",
            "outcome": "Nothing was changed.",
            "action": "Start the relay (`sudo systemctl start cortex-bridge`) "
                      "and %s, or close the other program first." % AGAIN,
        }
    if reason_code == "unverified":
        return {
            "summary": "The bridge accepted the firmware, but the transfer "
                       "did not complete as expected.",
            "outcome": "Treat the bridge as not updated.",
            "action": "%s. If it fails the same way, %s."
                      % (AGAIN[0].upper() + AGAIN[1:], SUPPORT),
        }
    return {
        "summary": "The update failed: %s." % (detail or error),
        "outcome": _left_in(post_state, running) if post_state else
                   "The bridge may not have been changed.",
        "action": "%s. If it fails the same way, %s."
                  % (AGAIN[0].upper() + AGAIN[1:], SUPPORT),
    }


def explain_exception(exc, *, running=None, package=None) -> dict:
    """``explain`` for an exception raised by ``cortex_bridge.updater``."""
    return explain(
        exc.__class__.__name__,
        reason_code=getattr(exc, "reason_code", "") or "",
        detail=getattr(exc, "detail", "") or str(exc),
        running=running, package=package,
        post_state=getattr(exc, "post_state", "") or "",
        fault_code=getattr(exc, "code", "") or "",
        fault_text=getattr(exc, "description", "") or "")


def technical_detail(error: str, message: str) -> str:
    """The technical message worth showing under the explanation: none for
    a refused file, whose reason the summary already gives."""
    return "" if error == "PackageRefused" else (message or "")


def lines(explanation: dict, detail: str = "") -> list:
    """The explanation as printed lines, with the technical detail last."""
    out = [explanation["summary"], explanation["outcome"],
           explanation["action"]]
    if detail and detail not in explanation["summary"]:
        out.append("Details: %s" % detail)
    return [line for line in out if line]


__all__ = ["explain", "explain_exception", "lines", "technical_detail"]
