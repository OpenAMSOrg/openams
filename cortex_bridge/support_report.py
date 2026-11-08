# SPDX-License-Identifier: MIT
"""The Cortex Bridge support report: what support needs, nothing private.

    python3 ~/openams/tools/cortex-bridge.py support

One text file, written to the printer's log folder (Mainsail lists it under
Machine > Log files). It holds, in this order:

- the summary `cortex-bridge.py status` prints, with the next step it names;
- the OpenAMS version and commit, Python and the operating system;
- the bridge: USB state, the relay service and its status file, the firmware
  version against the newest bundled one, the configured AMS units;
- the bridge's own diagnostic snapshot (bridge firmware 9 and later), with a
  plain reading of it, or why there is none (``bridge_support_snapshot``);
- Klipper and Moonraker: state, message and versions;
- the OpenAMS sections of the config ([mcu] for each AMS, [oams], [fps],
  [oams_manager], [filament_group]), the section names of every config file,
  and the OpenAMS sections of moonraker.conf;
- the relay service's status, install.sh's last output, the end of klippy.log
  (the last Klipper start), moonraker.log and the relay's support log;
- what was removed, by kind.

Every part passes through ``cortex_bridge.redact.Redactor`` before it is
written, and a last check removes any line that still looks private. Writing
the report sends nothing: ``cortex_bridge.support_submit`` does that, only
when its owner sets it up and says yes.
"""

from __future__ import annotations

import datetime
import getpass
import json
import os
from pathlib import Path
import platform
import re
import socket
import subprocess
import sys
import urllib.error

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cortex_bridge import bridge_snapshot  # noqa: E402
from cortex_bridge import cli  # noqa: E402
from cortex_bridge import redact  # noqa: E402

REPORT_FORMAT = 1
REPORT_TITLE = "OpenAMS Cortex Bridge support report"
INSTALL_LOG_NAME = "openams-install.log"

KLIPPY_MAX_LINES = 2000         # of the last Klipper start: head and tail
KLIPPY_HEAD_LINES = 400
KLIPPY_MIN_LINES = 300          # reach back into the previous start if short
KLIPPY_STATS_KEPT = 3
MOONRAKER_LINES = 400
INSTALL_LINES = 300
SUPPORT_LOG_LINES = 400
TAIL_BYTES = 8 * 1024 * 1024    # read at most this much of the end of a log

OAMS_SECTION_KINDS = ("oams", "fps", "oams_manager", "filament_group")
MOONRAKER_SECTION_KINDS = ("openams_bridge", "openams_spoolman",
                           "openams_taglib", "spoolman")
PRINTER_INFO_FIELDS = ("state", "state_message", "software_version", "app",
                       "cpu_info")
SERVER_INFO_FIELDS = ("klippy_state", "klippy_connected", "moonraker_version",
                      "api_version_string", "components",
                      "failed_components", "warnings")
CONFIG_FILES_MAX = 80
KLIPPER_BACKUP = re.compile(r"-\d{8}_\d{6}\.cfg$")

# ---------------------------------------------------------------------------
# The bridge's own diagnostic snapshot
# ---------------------------------------------------------------------------

# The relay asks the bridge (firmware 9 and later, see
# cortex_bridge.bridge_snapshot). The bridge answers each request within its
# 1 s reply window; with the 1 s interval and two retries the relay answers
# within bridge_snapshot.WORST_CASE_S (6.2 s). The client waits a little
# longer than that.
SNAPSHOT_METHOD = bridge_snapshot.SNAPSHOT_METHOD
SNAPSHOT_TIMEOUT = bridge_snapshot.WORST_CASE_S + 3.0


def _unavailable(reason: str, text: str) -> dict:
    return {"available": False, "reason": reason,
            "text": "unavailable: " + text, "snapshot": None, "reading": None}


def bridge_support_snapshot(run_dir: Path, *, request=None,
                            timeout: float = SNAPSHOT_TIMEOUT) -> dict:
    """The bridge's diagnostic snapshot through the relay, or why there is
    none. Always returns ``available``, ``reason``, ``text``, ``snapshot``
    (only the fields ``bridge_snapshot.allowlisted`` keeps, or None) and
    ``reading`` (``bridge_snapshot.classify``, or None)."""
    request = request or cli.control_request
    try:
        reply = request({"method": SNAPSHOT_METHOD}, run_dir=Path(run_dir),
                        timeout=timeout)
    except OSError as exc:
        return _unavailable("relay-not-running",
                            "the relay did not answer (%s)" %
                            (exc.strerror or str(exc) or
                             exc.__class__.__name__))
    except ValueError:
        return _unavailable("no-reply", "the relay's answer was not JSON")
    if not isinstance(reply, dict) or not reply:
        return _unavailable("no-reply", "the relay sent no answer")
    if reply.get("ok") and isinstance(reply.get("snapshot"), dict):
        snapshot = bridge_snapshot.allowlisted(reply["snapshot"])
        return {"available": True, "reason": "", "text": "",
                "snapshot": snapshot,
                "reading": bridge_snapshot.classify(snapshot)}
    error = str(reply.get("error") or "")
    if error == "unknown-method":
        return _unavailable(
            "relay-without-snapshot",
            "this OpenAMS relay does not ask the bridge for a snapshot "
            "(update OpenAMS)")
    if error == "unsupported-firmware":
        minimum = reply.get("minimum_firmware")
        return _unavailable(
            "firmware-too-old", str(reply.get("message") or "") or
            ("bridge firmware older than %s" % minimum
             if isinstance(minimum, int) else
             "the bridge firmware does not offer a snapshot"))
    if reply.get("ok"):
        return _unavailable("malformed", "the relay's answer had no snapshot")
    return _unavailable(error or "refused",
                        str(reply.get("message") or error or "refused"))


def snapshot_text(result: dict, redactor: redact.Redactor) -> str:
    """The snapshot section: the plain reading first, then the fields."""
    lines = []
    reading = result.get("reading")
    if result.get("available") and reading:
        lines.append("Reading: " + reading["summary"])
        if reading.get("action"):
            lines.append("What to do: " + reading["action"])
        # The odometer around each failed load or calibration.
        lines.extend(reading.get("feed_failures") or [])
        # The dryer vent loop (bridge firmware 10).
        lines.extend(reading.get("dryers") or [])
    else:
        lines.append(result.get("text") or "unavailable")
    lines.append("")
    # In the wire's order (system, line, master, ...), not sorted.
    lines.append(json.dumps(redactor.redact_data(
        {key: result.get(key) for key in
         ("available", "reason", "reading", "snapshot")}), indent=1))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reading logs and config files
# ---------------------------------------------------------------------------

def read_tail(path: Path, max_bytes: int = TAIL_BYTES):
    """The last ``max_bytes`` of a file as lines, or None if unreadable."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError:
        return None
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]                   # the first line is cut
    return lines


def klippy_session(lines: list) -> tuple:
    """(lines, note): klippy.log from the last Klipper start, reaching back
    into the previous one when the last is short; periodic Stats lines but
    the last few dropped; the middle elided past ``KLIPPY_MAX_LINES``."""
    starts = [i for i, line in enumerate(lines)
              if "Start printer at " in line]
    begin = starts[-1] if starts else 0
    if len(lines) - begin < KLIPPY_MIN_LINES:
        begin = max(0, len(lines) - KLIPPY_MIN_LINES)
    chosen = lines[begin:]
    stats = [i for i, line in enumerate(chosen)
             if line.startswith("Stats ")]
    dropped = set(stats[:-KLIPPY_STATS_KEPT] if KLIPPY_STATS_KEPT else stats)
    chosen = [line for i, line in enumerate(chosen) if i not in dropped]
    notes = ["from the last Klipper start" if starts else "the end of the log"]
    if dropped:
        notes.append("%d periodic Stats lines left out" % len(dropped))
    if len(chosen) > KLIPPY_MAX_LINES:
        tail = KLIPPY_MAX_LINES - KLIPPY_HEAD_LINES
        omitted = len(chosen) - KLIPPY_MAX_LINES
        chosen = chosen[:KLIPPY_HEAD_LINES] + \
            ["... %d lines left out ..." % omitted] + chosen[-tail:]
        notes.append("%d lines in the middle left out" % omitted)
    return chosen, "; ".join(notes)


def without_moonraker_config_dump(lines: list) -> tuple:
    """Moonraker writes its whole configuration to its log at start; that
    block is left out (the OpenAMS sections are reported on their own)."""
    out = []
    skipping = False
    dropped = 0
    for line in lines:
        if not skipping and "Moonraker Configuration" in line and \
                "#" in line:
            skipping = True
            dropped += 1
            continue
        if skipping:
            dropped += 1
            if re.fullmatch(r"\s*#{10,}\s*", line):
                skipping = False
            continue
        out.append(line)
    return out, dropped


_SECTION = re.compile(r"^\[(?P<name>[^\]]+)\]")


def config_sections(text: str) -> list:
    """[(section name, [lines])] of an INI-style file, without comment
    lines; lines before the first section are left out."""
    sections = []
    current = None
    for line in text.splitlines():
        header = _SECTION.match(line.strip())
        if header:
            current = (header.group("name").strip(), [])
            sections.append(current)
            continue
        stripped = line.strip()
        if current is None or not stripped or stripped[0] in "#;":
            continue
        current[1].append(line.rstrip())
    return sections


def config_files(config_dir: Path) -> list:
    """The printer's config files (two levels deep), without Klipper's
    SAVE_CONFIG backups."""
    config_dir = Path(config_dir)
    found = []
    for pattern in ("*.cfg", "*/*.cfg", "*/*/*.cfg"):
        for path in sorted(config_dir.glob(pattern)):
            relative = path.relative_to(config_dir)
            if any(part.startswith(".") for part in relative.parts) or \
                    KLIPPER_BACKUP.search(path.name):
                continue
            found.append(path)
    return found[:CONFIG_FILES_MAX]


def oams_config(config_dir: Path) -> tuple:
    """(text of the OpenAMS sections, {file: [section names]})."""
    config_dir = Path(config_dir)
    blocks = []
    names = {}
    for path in config_files(config_dir):
        try:
            text = path.read_text("utf-8", errors="replace")
        except OSError:
            continue
        relative = str(path.relative_to(config_dir))
        sections = config_sections(text)
        names[relative] = [name for name, _ in sections]
        for name, body in sections:
            kind = name.split()[0].lower()
            joined = "\n".join(body)
            if kind in OAMS_SECTION_KINDS or (kind == "mcu" and (
                    path.name == "oams.cfg" or "/dev/oams/" in joined)):
                blocks.append("# %s\n[%s]\n%s" % (relative, name, joined))
    return "\n\n".join(blocks), names


def moonraker_config(config_dir: Path) -> str:
    try:
        text = (Path(config_dir) / "moonraker.conf").read_text(
            "utf-8", errors="replace")
    except OSError:
        return "(no moonraker.conf in the config folder)"
    blocks = []
    for name, body in config_sections(text):
        kind = name.split()[0].lower()
        if kind in MOONRAKER_SECTION_KINDS or (
                kind == "update_manager" and
                re.search(r"openams|cortex", name, re.I)):
            blocks.append("[%s]\n%s" % (name, "\n".join(body)))
    return "\n\n".join(blocks) or "(no OpenAMS sections in moonraker.conf)"


# ---------------------------------------------------------------------------
# Where the facts come from
# ---------------------------------------------------------------------------

def _run(command: list, timeout: float = 20.0) -> str:
    try:
        return subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return "(%s: %s)\n" % (command[0], exc.__class__.__name__)


class Sources:
    """Everything the report reads from the printer. Tests replace methods
    or point the folders at a fixture."""

    def __init__(self, args):
        self.args = args
        self.config_dir = Path(args.config_dir)
        self.logs_dir = Path(getattr(args, "logs_dir", None) or
                             cli.default_logs_dir())
        self.run_dir = Path(args.run_dir)
        self.moonraker_url = args.moonraker
        self.support_log = Path(getattr(
            args, "log_file", "~/printer_data/logs/cortex-bridge.jsonl")
        ).expanduser()

    def facts(self) -> dict:
        return cli.gather(self.args)

    def openams(self) -> dict:
        git = ["git", "-C", str(cli.OPENAMS_DIR)]

        def ask(*words):
            return _run(git + list(words), timeout=10).strip()
        if ask("rev-parse", "--is-inside-work-tree") != "true":
            # A copy without git (unpacked from an archive) has no version
            # to report; say so once instead of a git error in every field.
            return {"checkout": "not a git checkout: %s" % cli.OPENAMS_DIR}
        status = ask("status", "--porcelain", "--untracked-files=no")
        return {
            "describe": ask("describe", "--always", "--dirty", "--tags"),
            "commit": ask("rev-parse", "HEAD"),
            "branch": ask("rev-parse", "--abbrev-ref", "HEAD"),
            "committed": ask("log", "-1", "--format=%cI"),
            "changed_files": len([line for line in status.splitlines()
                                  if line.strip()]),
            "origin": ask("remote", "get-url", "origin"),
        }

    def system(self) -> dict:
        pretty = ""
        try:
            for line in Path("/etc/os-release").read_text().splitlines():
                if line.startswith("PRETTY_NAME="):
                    pretty = line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
        return {"os": pretty, "kernel": platform.release(),
                "machine": platform.machine(),
                "python": platform.python_version()}

    def klipper(self) -> dict:
        result = {}
        for path, fields, name in (
                ("/printer/info", PRINTER_INFO_FIELDS, "printer"),
                ("/server/info", SERVER_INFO_FIELDS, "moonraker")):
            try:
                answer = cli.moonraker(self.moonraker_url, path)
            except (OSError, ValueError, urllib.error.URLError) as exc:
                result[name] = {"reachable": False,
                                "error": exc.__class__.__name__}
                continue
            answer = answer.get("result", {}) if isinstance(answer, dict) \
                else {}
            result[name] = {key: answer[key] for key in fields
                            if key in answer}
        return result

    def snapshot(self) -> dict:
        return bridge_support_snapshot(self.run_dir)

    def relay_service(self) -> str:
        return _run(["systemctl", "status", cli.SERVICE + ".service",
                     "--no-pager", "-n", "40"])

    def hostnames(self) -> list:
        names = [socket.gethostname()]
        try:
            names.append(Path("/etc/hostname").read_text().strip())
        except OSError:
            pass
        return [n for name in names if name
                for n in (name, name.split(".")[0])]

    def usernames(self) -> list:
        names = []
        for get in (getpass.getuser, lambda: Path.home().name,
                    lambda: os.environ.get("SUDO_USER", "")):
            try:
                names.append(get())
            except Exception:  # noqa: BLE001 - a name we cannot read
                continue
        return [name for name in names if name]

    def wifi_networks(self) -> list:
        """The Wi-Fi network this computer is on, only to remove its name."""
        output = _run(["iwgetid", "-r"], timeout=3).strip()
        if not output or output.startswith("(") or "\n" in output:
            return []
        return [output]

    def salt(self) -> bytes:
        return redact.load_or_create_salt()

    def secrets(self) -> set:
        return redact.collect_secrets(self.config_dir)


# ---------------------------------------------------------------------------
# Building the report
# ---------------------------------------------------------------------------

class Report:
    def __init__(self, text: str, sections: list, counts: dict,
                 removed_lines: int, next_step: str):
        self.text = text
        self.sections = sections
        self.counts = counts
        self.removed_lines = removed_lines
        self.next_step = next_step

    def removed_summary(self) -> str:
        parts = ["%s (%d)" % (name, count)
                 for name, count in sorted(self.counts.items()) if count]
        if self.removed_lines:
            parts.append("lines the last check removed (%d)" %
                         self.removed_lines)
        return ", ".join(parts) or "nothing private was found"


def _bridge_serials(facts: dict) -> list:
    serials = [device.get("serial", "") for device in facts.get("usb") or []]
    bridge = (facts.get("relay") or {}).get("bridge") or {}
    return serials + [bridge.get("serial", "")]


def _log_section(lines, count: int, missing: str) -> tuple:
    if lines is None:
        return "(%s)" % missing, ""
    chosen = lines[-count:]
    return "\n".join(chosen), "last %d lines" % len(chosen)


def make_redactor(sources: Sources, facts: dict) -> redact.Redactor:
    return redact.Redactor(
        salt=sources.salt(), hostnames=sources.hostnames(),
        usernames=sources.usernames(),
        secrets=set(sources.secrets()) | set(sources.wifi_networks()),
        bridge_serials=_bridge_serials(facts))


def build(sources: Sources, *, now=None) -> Report:
    """Gather, redact and render one report."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    facts = sources.facts()
    redactor = make_redactor(sources, facts)
    snapshot = sources.snapshot()
    facts["snapshot"] = snapshot              # the next step can name it
    step = cli.next_step(facts)
    sections = []

    def add(title, content, note=""):
        sections.append((title, content, note))

    add("summary", "\n".join(cli.status_lines(facts)) +
        "\n\nNext step: " + step)
    add("openams", sources.openams())
    add("system", sources.system())
    add("bridge", {key: facts.get(key) for key in (
        "usb", "relay_service", "relay", "installed_firmware",
        "bundled_firmware", "configured")})
    add("bridge snapshot", snapshot_text(snapshot, redactor))
    klipper = sources.klipper()
    klipper["service"] = facts.get("klipper_service")
    add("klipper", klipper)
    oams_text, names = oams_config(sources.config_dir)
    add("config: OpenAMS sections",
        oams_text or "(no OpenAMS sections found in %s)" %
        sources.config_dir)
    add("config: section names by file", names)
    add("moonraker.conf: OpenAMS sections",
        moonraker_config(sources.config_dir))
    add("relay service", sources.relay_service())

    install = read_tail(sources.logs_dir / INSTALL_LOG_NAME, 1024 * 1024)
    text, note = _log_section(
        install, INSTALL_LINES,
        "no install log: install.sh keeps its last output in %s from this "
        "OpenAMS version on" % INSTALL_LOG_NAME)
    add("install.sh, last run (%s)" % INSTALL_LOG_NAME, text, note)

    klippy = read_tail(sources.logs_dir / "klippy.log")
    if klippy is None:
        add("klippy.log", "(no klippy.log in %s)" % sources.logs_dir)
    else:
        chosen, note = klippy_session(klippy)
        add("klippy.log", "\n".join(chosen), note)

    moon = read_tail(sources.logs_dir / "moonraker.log", 2 * 1024 * 1024)
    if moon is None:
        add("moonraker.log", "(no moonraker.log in %s)" % sources.logs_dir)
    else:
        moon, dropped = without_moonraker_config_dump(moon)
        text, note = _log_section(moon, MOONRAKER_LINES, "")
        if dropped:
            note += "; its configuration dump left out"
        add("moonraker.log", text, note)

    support = read_tail(sources.support_log, 1024 * 1024)
    text, note = _log_section(support, SUPPORT_LOG_LINES,
                              "no support log at %s" % sources.support_log)
    add("relay support log (cortex-bridge.jsonl)", text, note)

    body = render(sections, redactor)
    body = redactor.redact_text(body)           # a second pass, to be sure
    body, removed = remove_leaks(body, redactor)
    header = [
        REPORT_TITLE,
        "format %d, written %s, installation %s" % (
            REPORT_FORMAT, now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            redactor.installation_id()),
        "Private details were removed on this printer before this file was "
        "written (passwords, keys,",
        "addresses, names, AMS serials, tag data, spool details); see "
        "docs/guide/support-report.md.",
        "Writing it sent nothing anywhere.",
        "",
        "",
    ]
    report = Report("", [title for title, _, _ in sections],
                    dict(redactor.counts), removed, step)
    footer = ["", "== removed", report.removed_summary(), ""]
    report.text = "\n".join(header) + body + "\n".join(footer)
    return report


def render(sections: list, redactor: redact.Redactor) -> str:
    out = []
    for title, content, note in sections:
        out.append("== %s%s" % (title, " (%s)" % note if note else ""))
        if isinstance(content, (dict, list)):
            out.append(json.dumps(redactor.redact_data(content), indent=1,
                                  sort_keys=True, default=str))
        else:
            out.append(redactor.redact_text(str(content)).rstrip("\n"))
        out.append("")
    return "\n".join(out) + "\n"


def remove_leaks(text: str, redactor: redact.Redactor) -> tuple:
    """Remove every line the last check flags; (text, lines removed)."""
    findings = redactor.find_leaks(text)
    if not findings:
        return text, 0
    flagged = {}
    for check, number, _ in findings:
        flagged.setdefault(number, check)
    lines = text.splitlines()
    for number, check in flagged.items():
        lines[number - 1] = "<line removed by the last check: %s>" % check
    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), \
        len(flagged)


def check_text(text: str, sources: Sources) -> list:
    """Findings of the last check for a report file someone may have
    edited, with this printer's names, secrets and AMS units."""
    redactor = make_redactor(sources, sources.facts())
    return redactor.find_leaks(text)


__all__ = [
    "INSTALL_LOG_NAME", "REPORT_FORMAT", "Report", "SNAPSHOT_METHOD",
    "Sources", "bridge_support_snapshot", "build", "check_text",
    "config_sections", "klippy_session", "oams_config", "remove_leaks",
    "render", "without_moonraker_config_dump",
]
