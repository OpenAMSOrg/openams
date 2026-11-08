# SPDX-License-Identifier: MIT
"""Send a support report, only when its owner says so.

    python3 ~/openams/tools/cortex-bridge.py support --submit

Nothing is sent unless all of these hold:

1. a destination is set up. None is by default: the printer's owner adds
   ``support_url: https://...`` to the ``[openams_bridge]`` section of
   moonraker.conf, or names one for a single run with ``--to``;
2. the command runs in a terminal (a person is there to answer);
3. that person has been shown what will be sent (the file, its size, where
   it goes, and on request the whole text) and answers yes.

What is sent is the report file itself, byte for byte, as it is when the
person answers. Senders are pluggable (``SENDERS``, keyed by the URL scheme);
the one built in posts the file over HTTPS. Which service receives reports
is the project owner's decision; see docs/contracts/support_report.md.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

SETTING_SECTION = "openams_bridge"
SETTING_OPTION = "support_url"
CONTENT_TYPE = "text/plain; charset=utf-8"
REPORT_HEADER = "X-OpenAMS-Report"
REPORT_KIND = "cortex-bridge-support/1"
SEND_TIMEOUT = 30.0
REPLY_LIMIT = 4096


class SendError(RuntimeError):
    """The report could not be sent; the message says why, in plain words."""


class Destination:
    """Where a report would go, and which setting said so."""

    def __init__(self, url: str, origin: str):
        self.url = url.strip()
        self.origin = origin

    def describe(self) -> str:
        return "%s (%s)" % (self.url, self.origin)


_SECTION = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")
_OPTION = re.compile(r"^(?P<name>[A-Za-z0-9_]+)\s*[:=]\s*(?P<value>.*?)\s*$")


def configured(config_dir: Path, override: str | None = None):
    """The destination, or None when none is set up (the default)."""
    if override:
        return Destination(override, "given with --to")
    try:
        text = (Path(config_dir) / "moonraker.conf").read_text(
            "utf-8", errors="replace")
    except OSError:
        return None
    section = None
    for line in text.splitlines():
        header = _SECTION.match(line)
        if header:
            section = header.group("name").strip()
            continue
        match = _OPTION.match(line)
        if section == SETTING_SECTION and match and \
                match.group("name") == SETTING_OPTION:
            value = match.group("value").split(" #", 1)[0].strip()
            if value:
                return Destination(value, "support_url in [%s], moonraker.conf"
                                   % SETTING_SECTION)
    return None


# ---------------------------------------------------------------------------
# Senders
# ---------------------------------------------------------------------------

def _is_local(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class HttpsPost:
    """POST the report file as text/plain. Plain http only to this computer
    (for a local test server): a report never crosses a network unencrypted.
    """

    def __init__(self, url: str, *, opener=None, timeout: float = SEND_TIMEOUT):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme == "http" and not _is_local(parts.hostname or ""):
            raise SendError("refusing to send a report over plain http to "
                            "%s; the destination must use https" %
                            (parts.hostname or url))
        if parts.username or parts.password:
            raise SendError("the destination URL must not carry a user name "
                            "or password")
        self.url = url
        self.opener = opener or urllib.request.urlopen
        self.timeout = timeout

    def send(self, data: bytes) -> str:
        request = urllib.request.Request(
            self.url, data=data, method="POST",
            headers={"Content-Type": CONTENT_TYPE, REPORT_HEADER: REPORT_KIND})
        try:
            with self.opener(request, timeout=self.timeout) as reply:
                status = getattr(reply, "status", None) or reply.getcode()
                body = reply.read(REPLY_LIMIT).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise SendError("the destination refused the report (HTTP %d)"
                            % exc.code)
        except (OSError, urllib.error.URLError, ValueError) as exc:
            raise SendError("the destination could not be reached (%s)" %
                            getattr(exc, "reason", exc))
        if not 200 <= int(status) < 300:
            raise SendError("the destination answered HTTP %s" % status)
        return body.strip().splitlines()[0][:200] if body.strip() else \
            "HTTP %s" % status


SENDERS = {"https": HttpsPost, "http": HttpsPost}


def sender_for(destination: Destination, *, opener=None):
    scheme = urllib.parse.urlsplit(destination.url).scheme.lower()
    factory = SENDERS.get(scheme)
    if factory is None:
        raise SendError("this OpenAMS cannot send reports to a %s:// "
                        "destination" % (scheme or "(no scheme)"))
    return factory(destination.url, opener=opener)


# ---------------------------------------------------------------------------
# Consent
# ---------------------------------------------------------------------------

NO_DESTINATION = (
    "Nothing was sent: no destination for support reports is set up on this "
    "printer. Attach the report to your support request instead "
    "(Mainsail: Machine > Log files).")
NOT_INTERACTIVE = (
    "Nothing was sent: sending a report needs your OK, so run "
    "`cortex-bridge.py support --submit` in a terminal (SSH) and answer the "
    "question.")


def preview_lines(path: Path, data: bytes, destination: Destination) -> list:
    text = data.decode("utf-8", "replace")
    sections = [line[3:] for line in text.splitlines()
                if line.startswith("== ")]
    return [
        "This sends one file, exactly as it is now:",
        "  %s (%d bytes, %d lines)" % (path, len(data),
                                       len(text.splitlines())),
        "to %s." % destination.describe(),
        "It contains: %s." % ", ".join(sections) if sections else
        "It contains no report sections.",
        "Private details were removed when it was written; read it in "
        "Mainsail (Machine > Log files) or answer v to see it here.",
    ]


def submit(path: Path, destination, *, ask=input, out=print,
           interactive: bool | None = None, opener=None) -> int:
    """Show what would be sent and send it only on a yes. Returns 0 when
    sent, 1 otherwise; nothing leaves the computer on any other answer."""
    if destination is None:
        out(NO_DESTINATION)
        return 1
    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive:
        out(NOT_INTERACTIVE)
        return 1
    try:
        sender = sender_for(destination, opener=opener)
        data = Path(path).read_bytes()
    except SendError as exc:
        out("Nothing was sent: %s." % exc)
        return 1
    except OSError as exc:
        out("Nothing was sent: the report cannot be read (%s)." %
            exc.strerror)
        return 1
    for line in preview_lines(Path(path), data, destination):
        out(line)
    while True:
        try:
            answer = ask("Send it? [y = send, v = view it here, N = no] ")
        except EOFError:
            answer = ""
        answer = answer.strip().lower()
        if answer in ("v", "view"):
            out(data.decode("utf-8", "replace"))
            continue
        if answer in ("y", "yes"):
            break
        out("Not sent. The report stays at %s." % path)
        return 1
    try:
        reply = sender.send(data)
    except SendError as exc:
        out("Not sent: %s. The report stays at %s." % (exc, path))
        return 1
    out("Sent. The destination answered: %s" % reply)
    out("Keep %s until support has replied." % path)
    return 0


__all__ = [
    "Destination", "HttpsPost", "NOT_INTERACTIVE", "NO_DESTINATION",
    "SENDERS", "SendError", "configured", "preview_lines", "sender_for",
    "submit",
]
