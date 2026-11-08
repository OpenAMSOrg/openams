# SPDX-License-Identifier: MIT
"""Remove private details from a support report before it is written.

A support report collects logs and config files from the customer's printer.
Everything in it passes through one ``Redactor`` first, which replaces:

- passwords, API keys and tokens, wherever they appear as ``name: value`` or
  ``"name": "value"`` (moonraker.conf, Moonraker's secrets file, notifier and
  webhook URLs, the ``key`` of ``[bambu_lab_tag_processor]``), credential
  shapes (GitHub, Slack, AWS, Google, JWT and similar), Bearer and
  Authorization headers, and every value actually found in the printer's own
  secret files, wherever it appears;
- SSH private and public keys;
- Wi-Fi network names and passphrases;
- IP addresses (except loopback, 0.0.0.0 and anything in 0.0.0.0/8, which is
  how an AMS version such as 0.0.6.49 reads) and MAC addresses, with
  placeholders numbered in order of appearance, so the same address reads the
  same throughout one report (``<lan-ip-1>``);
- host names (this computer's, any ``*.local`` name, and any URL host that is
  not a public project site) with ``<host>`` or ``<host-N>``;
- user names in home paths and on their own, with ``<user>``;
- e-mail addresses;
- AMS serial numbers, with a salted short hash, ``sn-1a2b3c4d``: the salt
  stays on the printer, so two reports from the same printer name the same
  unit the same way, and nobody else can reverse it;
- RFID tag UIDs and tag payloads;
- Spoolman spool contents, except the IDs.

Kept as they are (the owner's decisions of 2026-11-08): the bridge's own USB
serial number (``CB...``), which support needs to identify the bridge; the
AMS unit IDs (the 12 hex digits of the roster and of ``/dev/oams/<uid>``),
which ``cortex-bridge.py status`` prints; the 4-character AMS tag suffix
(``N573``); and the four version bytes an AMS reports.

``find_leaks`` is the last check: it runs these same rules over each line of
finished text and names every line they would still change, so it is
exactly as strong as the redaction. The report builder removes any line it
names, and ``--submit --file`` refuses a file it finds anything in.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets as _secrets

REDACTED = "<redacted>"

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# A key is a secret when its last word is one of these: moonraker_api_key,
# mqtt_password, bot_token, wifi_ssid. Not "webhooks", "tokens" or
# "secrets_path", whose last word is something else.
_SECRET_TAIL = (
    r"password|passwd|passphrase|secret|token|apikey|psk|ssid|cookie|"
    r"credentials?|bearer|(?:api|access|private|auth)[ _-]?key|"
    r"client[_-]?secret|session[_-]?id"
)
_SECRET_NAME = r"(?:[A-Za-z0-9]+[_.-])*(?:%s)" % _SECRET_TAIL
_SECRET_NAME_RE = re.compile(r"(?i)^%s$" % _SECRET_NAME)
# A secret's value: a quoted string (escaped quotes included); for a quoted
# (JSON) key, the bare value up to the next "," "}" or "]"; for a bare key
# (config files, log lines), everything to the end of the line, so a
# passphrase of several words goes whole. Never a JSON object or list.
_SECRET_VALUE = (
    r"(?P<value>\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.|'')*'|"
    r"(?(q)(?![{\[])[^\s,}\]]+(?:[ \t]+[^\s,}\]]+)*|(?![{\[])\S[^\n]*))")
_SECRET_KV = re.compile(
    r"(?i)(?:(?P<q>[\"']))?(?<![\w.-])(?P<name>%s)(?![A-Za-z0-9_.-])"
    r"(?(q)(?P=q))(?P<sep>[ \t]*[:=][ \t]*)%s" % (_SECRET_NAME,
                                                   _SECRET_VALUE))
# Credentials on a command line, as a config's shell command carries them
# into klippy.log: -u/--user user:pass, and --password, --token, --api-key and
# the like. A bare -p is left alone (it is a port as often as a password).
_CLI_USER = re.compile(
    r"(?<!\S)(?P<flag>-u|--user|--proxy-user)(?P<sep>[ \t]+|=)"
    r"(?P<value>\"[^\"\n]*:[^\"\n]*\"|'[^'\n]*:[^'\n]*'|[^\s:\"']+:\S+)")
_CLI_SECRET = re.compile(
    r"(?i)(?<!\S)(?P<flag>--?[a-z-]*(?:password|passwd|pass|token|secret|"
    r"api-?key|apikey|auth))(?P<sep>[ \t]+|=)"
    r"(?P<value>\"[^\"\n]*\"|'[^'\n]*'|(?![-<])\S+)")
# user:password@host outside a URL (rsync, a pasted remote).
_USERPASS_AT = re.compile(
    r"(?<![\w/:@.<-])(?P<user>[A-Za-z0-9._-]+):(?P<pw>[^\s:@/<>]+)@"
    r"(?=[A-Za-z0-9\[])")
# An option named exactly "key" at the start of a line: the Bambu tag key in
# a config file, and in the copy of the config Klipper writes to klippy.log.
_KEY_OPTION = re.compile(
    r"(?im)^(?P<indent>[ \t]*)(?P<name>key)(?P<sep>[ \t]*[:=][ \t]*)"
    r"(?P<value>\S.*)$")

_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
    r"(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S)
_SSH_PUBLIC_KEY = re.compile(
    r"\b(?:ssh-(?:rsa|ed25519|dss)|ecdsa-sha2-nistp\d+|"
    r"sk-(?:ssh-ed25519|ecdsa-sha2-nistp256)@openssh\.com)"
    r"[ \t]+AAAA[0-9A-Za-z+/=]+(?:[ \t]+[^\s]+)?")
# Generic credential shapes, the same list tools/public_release/check.py
# scans an export for.
_CREDENTIALS = tuple(re.compile(pattern) for pattern in (
    r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})",
    r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
    r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    r"\bAIza[0-9A-Za-z_-]{35}\b",
    r"\bshp(?:at|ss|ca|pa)_[0-9a-fA-F]{20,}",
    r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}",
    r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
))
_AUTH_HEADER = re.compile(
    r"(?i)\b(?P<name>authorization|proxy-authorization|x-api-key|api-key|"
    r"x-auth-token)(?P<sep>[\"']?[ \t]*[:=][ \t]*[\"']?)"
    r"(?P<value>(?:bearer|basic|token|digest)[ \t]+[^\s\"',;]+|[^\s\"',;]+)")
_BEARER = re.compile(r"(?i)\b(bearer)[ \t]+(?!<)[A-Za-z0-9._~+/=-]{12,}")
# Long base64 runs (keys, tokens); a path of the same alphabet is mostly
# lower case and slashes, and is left alone (see Redactor._base64).
_LONG_BASE64 = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{60,}={0,2}"
                          r"(?![A-Za-z0-9+/=])")

_URL = re.compile(r"\b(?P<scheme>[A-Za-z][A-Za-z0-9+.-]{1,15})://"
                  r"(?P<rest>[^\s\"'<>()\[\]{}`]+)")
_EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+"
                    r"(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")

_TAG_KV = re.compile(
    r"(?i)(?P<q>[\"']?)\b(?P<name>(?:tag|tray|card|nfc|rfid)[_ -]?"
    r"(?:uids?|uuids?|data|payload|blocks?|keys?)|block[_ -]?data|"
    r"sector[_ -]?data|payload)(?P=q)(?P<sep>[ \t]*[:=][ \t]*)"
    r"(?P<value>\"[^\"\n]*\"|'[^'\n]*'|\[[^\]\n]*\]|"
    r"[0-9A-Fa-f]{2}(?:[ :-]?[0-9A-Fa-f]{2})+|(?![{\[])[^\s,;}\])]+)")
# "uid" with a hex value: an AMS unit ID (12 digits, kept) or a tag UID.
_UID_KV = re.compile(
    r"(?i)(?P<q>[\"']?)\b(?P<name>uid)(?P=q)(?P<sep>[ \t]*[:=][ \t]*)"
    r"(?P<vq>[\"']?)(?P<value>[0-9A-Fa-f]{2}(?:[ :-]?[0-9A-Fa-f]{2})+)"
    r"(?P=vq)(?![\w])")
# A tag UID a reader logs in free text: "MFRC522 <name> card 04a1b2c3d4e5f6"
# (src/mfrc522.py), 4, 7 or 10 bytes after "card", "tag" or "uid". A bare
# 8-digit hex word anywhere else (a commit) is left alone.
_TAG_WORD_UID = re.compile(
    r"(?i)\b(?P<pre>(?:card|tag|uid)[ \t]*[:=]?[ \t]*)"
    r"(?P<value>[0-9a-f]{20}|[0-9a-f]{14}|[0-9a-f]{8})(?![0-9a-z])")
# A tag's data block, as OAMSM_RFID_READ answers it ("BLOCK=0102...").
_TAG_BLOCK = re.compile(r"(?i)\b(?P<pre>block[ \t]*[:=][ \t]*)"
                        r"(?P<value>[0-9a-f]{8,})(?![0-9a-z])")
_AMS_UID = re.compile(r"^[0-9A-Fa-f]{12}$")
_SERIAL_KV = re.compile(
    r"(?i)(?P<q>[\"']?)\b(?P<name>(?:ams[_ -]?)?(?:serial(?:[_ -]?"
    r"(?:number|no|num))?|sn))(?P=q)(?P<sep>[ \t]*[:=][ \t]*)"
    r"(?P<vq>[\"']?)(?P<value>[A-Za-z0-9]{6,32})(?P=vq)(?![\w/.-])")
# An AMS serial in free text: the 24 hex digits of the unit identity
# (src/oams.py) after "serial" or "identity", and the identity reply as
# Klipper's message dumps print it ("uid0=... uid1=... uid2=...").
_SERIAL_WORD = re.compile(
    r"(?i)\b(?P<pre>(?:serial|identity)\b[^\n]{0,40}?)(?<![0-9A-Za-z])"
    r"(?P<value>[0-9A-Fa-f]{24})(?![0-9A-Za-z])")
_IDENTITY_REPLY = re.compile(r"\buid0=(\d+)[ ,]+uid1=(\d+)[ ,]+uid2=(\d+)")
BRIDGE_SERIAL = re.compile(r"^CB[0-9A-Fa-f]{6,}$")

_HEX32 = re.compile(r"\b[0-9a-fA-F]{32}\b")
_HEX_LONG = re.compile(r"\b[0-9a-fA-F]{48,}\b")
_MAC = re.compile(r"(?<![\w:])[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}(?![\w:])")
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w|\.\d)")
_IPV6 = re.compile(r"(?<![\w:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}"
                   r"(?:%[A-Za-z0-9_.-]+)?(?![\w:])")
_MDNS = re.compile(r"(?<![<\w.-])[A-Za-z0-9][A-Za-z0-9-]*"
                   r"\.(?:local|lan|home|internal|localdomain|home\.arpa)\b",
                   re.I)
_JOURNAL_HOST = re.compile(
    r"(?m)^(?P<pre>[A-Z][a-z]{2} [ 0-9]\d \d\d:\d\d:\d\d )(?P<host>[^\s<]+)"
    r"(?= )")
_HOST_KV = re.compile(
    r"(?i)(?P<q>[\"']?)\b(?P<name>host[_ ]?name|node[_ ]?name|fqdn)(?P=q)"
    r"(?P<sep>[ \t]*[:=][ \t]*)(?P<vq>[\"']?)(?P<value>[A-Za-z0-9][\w.-]*)")
_AT_HOST = re.compile(r"(?<=[A-Za-z0-9_>])@(?P<host>[A-Za-z0-9]"
                      r"(?:[\w-]|\.(?=[\w-]))*)(?=[\s:/]|$)", re.M)
_HOME = re.compile(r"(?<![\w/])/home/(?P<user>[A-Za-z0-9._-]+)")
_TILDE_USER = re.compile(r"(?<![\w~])~(?P<user>[a-z_][a-z0-9_-]{1,31})"
                         r"(?=/)")

# Lines about Spoolman spools: every field but the IDs is removed.
_SPOOL_LINE = re.compile(r"(?i)spoolman|\"(?:spool|filament)\"[ \t]*:[ \t]*\{")
_SPOOL_FIELD = re.compile(
    r"(?P<pre>\"(?P<name>[A-Za-z_][\w]*)\"[ \t]*:[ \t]*)"
    r"(?P<value>\"(?:[^\"\\\n]|\\.)*\"|-?\d+(?:\.\d+)?)")
SPOOL_ID_FIELDS = frozenset(("id", "spool_id", "filament_id", "vendor_id",
                             "lane", "bay", "group", "unit"))
SPOOL_KEYS = frozenset(("spool", "spools", "filament", "active_spool",
                        "spool_info", "spoolman"))

# Public sites a URL may name as they are.
PUBLIC_HOSTS = (
    "github.com", "githubusercontent.com", "si-forge.com", "bambulab.com",
    "pypi.org", "pythonhosted.org", "debian.org", "raspberrypi.com",
    "raspberrypi.org", "klipper3d.org", "readthedocs.io", "kalico.gg",
    # Services whose secret is in the path, which is redacted on its own.
    "discord.com", "discordapp.com", "slack.com", "telegram.org",
)
# Host names that are words in this project's own logs. A computer called
# "klipper" keeps its name except where it is plainly a host name (a journal
# prefix, user@host, a URL), so that "klipper" the service stays readable.
COMMON_HOSTNAMES = frozenset((
    "localhost", "raspberrypi", "mainsailos", "fluiddpi", "klipper",
    "moonraker", "mainsail", "fluidd", "printer", "voron", "ratos", "octopi",
    "pi", "debian", "ubuntu", "armbian", "orangepi", "bigtreetech", "biqu",
    "mks", "kalico", "openams", "ams", "host", "server", "linux", "cb1",
    "cb2", "manta", "rpi", "sonic", "rock", "radxa", "oams", "bridge",
    "cortex",
))


def distinctive_hostname(name: str) -> bool:
    """Whether a host name can be replaced wherever it appears as a word.
    Only a name no ordinary word could be: with a digit, "-", "_" or ".",
    or of 10 letters or more, and not a common one. Any other (a printer
    called "oams" or "garage") is replaced only where it is plainly a host
    name: a journal prefix, user@host, "hostname: x", a URL, name.local."""
    name = name.lower()
    if name in COMMON_HOSTNAMES or len(name) < 3:
        return False
    return bool(re.search(r"[\d_.-]", name)) or len(name) >= 10
# User names too common to be private, and too common as words to replace
# outside a home path.
COMMON_USERS = frozenset((
    "pi", "root", "klipper", "biqu", "mks", "orangepi", "debian", "ubuntu",
    "admin", "user", "printer", "rock", "radxa", "armbian", "fly", "ratos",
    "voron", "dietpi", "linaro", "odroid", "test", "nobody",
))
_NOT_SECRET = frozenset(("", "true", "false", "yes", "no", "none", "null",
                         "on", "off"))


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _requote(original: str, replacement: str) -> str:
    if len(original) >= 2 and original[0] == original[-1] and \
            original[0] in "\"'":
        return original[0] + replacement + original[0]
    return replacement


def _private_address(text: str):
    """The address in ``text`` when it identifies a machine, else None. Not
    loopback, 0.0.0.0, a netmask, or 0.x.x.x: no machine has one, and the
    version bytes an AMS reports read that way (0.0.6.49)."""
    bare = text.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(bare)
    except ValueError:
        return None
    if address.is_loopback or address.is_unspecified:
        return None
    if address.version == 4 and (bare.startswith("255.") or
                                 int(address) >> 24 == 0):
        return None
    return address


def mask(text: str) -> str:
    """A finding shown without repeating it: first and last character."""
    text = text.strip()
    if len(text) <= 2:
        return "*" * len(text)
    return text[0] + "*" * (len(text) - 2) + text[-1]


# ---------------------------------------------------------------------------
# The salt and the secrets found on this printer
# ---------------------------------------------------------------------------

def default_salt_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "openams" / "support-report-salt"


def load_or_create_salt(path: Path | None = None) -> bytes:
    """This printer's salt for AMS hashes: random, kept private (0600), and
    created on first use. A salt that cannot be stored is used once."""
    path = Path(path) if path else default_salt_path()
    try:
        text = path.read_text("ascii").strip()
        if re.fullmatch(r"[0-9a-f]{32,128}", text):
            return bytes.fromhex(text)
    except OSError:
        pass
    salt = _secrets.token_bytes(16)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                             0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(salt.hex() + "\n")
    except OSError:
        pass
    return salt


_INI_SECTION = re.compile(r"^\s*\[(?P<name>[^\]]+)\]")
_INI_OPTION = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+)[ \t]*[:=][ \t]*"
                         r"(?P<value>.*?)\s*$")


def _usable_secret(value: str) -> bool:
    value = _strip_quotes(value.strip())
    if value.lower() in _NOT_SECRET or len(value) < 6:
        return False
    if value.startswith("<") and value.endswith(">"):
        return False                        # a sample's placeholder
    if value.startswith("{") and "secrets" in value:
        return False                        # a Moonraker {secrets.x} template
    return not re.fullmatch(r"[\d.]+", value)


def secrets_in_ini(text: str) -> set:
    """Values of secret-named options (and of ``key``) in an INI-style file."""
    found = set()
    for line in text.splitlines():
        if line.lstrip().startswith(("#", ";")) or _INI_SECTION.match(line):
            continue
        match = _INI_OPTION.match(line)
        if not match:
            continue
        name = match.group("name")
        value = match.group("value").split(" #", 1)[0].strip()
        if (name.lower() == "key" or _SECRET_NAME_RE.match(name)) and \
                _usable_secret(value):
            found.add(_strip_quotes(value))
    return found


def secrets_in_secrets_file(text: str) -> set:
    """Every value in a Moonraker secrets file (JSON or INI)."""
    found = set()
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if data is not None:
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, str) and _usable_secret(item):
                found.add(item)
        return found
    for line in text.splitlines():
        if line.lstrip().startswith(("#", ";")) or _INI_SECTION.match(line):
            continue
        match = _INI_OPTION.match(line)
        if match and _usable_secret(match.group("value")):
            found.add(_strip_quotes(match.group("value").strip()))
    return found


def collect_secrets(config_dir: Path) -> set:
    """Secret values found in this printer's config folder and Moonraker's
    secrets file. They are only used to find and remove those values."""
    config_dir = Path(config_dir)
    found = set()
    secrets_files = [config_dir.parent / "moonraker.secrets",
                     config_dir / "moonraker.secrets",
                     config_dir / ".secrets"]
    paths = sorted(config_dir.glob("*.cfg")) + \
        sorted(config_dir.glob("*.conf"))
    for path in paths:
        try:
            text = path.read_text("utf-8", errors="replace")
        except OSError:
            continue
        found |= secrets_in_ini(text)
        section = None
        for line in text.splitlines():
            header = _INI_SECTION.match(line)
            if header:
                section = header.group("name").strip().lower()
                continue
            match = _INI_OPTION.match(line)
            if section == "secrets" and match and \
                    match.group("name") == "secrets_path":
                secrets_files.append(Path(match.group("value")).expanduser())
    for path in secrets_files:
        try:
            found |= secrets_in_secrets_file(
                path.read_text("utf-8", errors="replace"))
        except OSError:
            continue
    return found


# ---------------------------------------------------------------------------
# The redactor
# ---------------------------------------------------------------------------

class Redactor:
    """One report's redaction: placeholders stay the same throughout it."""

    def __init__(self, *, salt: bytes, hostnames=(), usernames=(),
                 secrets=(), bridge_serials=()):
        if not salt:
            raise ValueError("a salt is required")
        self.salt = bytes(salt)
        self.hostnames = sorted({h.strip().lower() for h in hostnames
                                 if h and h.strip()}, key=len, reverse=True)
        self.usernames = [u for u in dict.fromkeys(
            u.strip() for u in usernames if u and u.strip())]
        self.secrets = sorted({s for s in secrets if _usable_secret(s)},
                              key=len, reverse=True)
        self.bridge_serials = {s.strip() for s in bridge_serials
                               if s and s.strip()}
        self._maps = {}
        self.counts = {}
        for index, user in enumerate(self.usernames):
            self._maps.setdefault("user", {})[user] = \
                "<user>" if index == 0 else "<user-%d>" % (index + 1)
        self._known_secrets = [re.compile(re.escape(s)) for s in self.secrets]
        self._distinct_hosts = self._word_pattern(
            [h for h in self.hostnames if distinctive_hostname(h)], re.I)
        self._distinct_users = self._word_pattern(
            [u for u in self.usernames
             if u.lower() not in COMMON_USERS and len(u) >= 3])
        self._checker = None

    def _probe(self) -> "Redactor":
        """A redactor with the same settings and its own placeholders and
        counts, for the last check."""
        if self._checker is None:
            self._checker = Redactor(
                salt=self.salt, hostnames=self.hostnames,
                usernames=self.usernames, secrets=self.secrets,
                bridge_serials=self.bridge_serials)
        return self._checker

    @staticmethod
    def _word_pattern(words, flags=0):
        """``words`` wherever no letter or digit touches them: a name joined
        to others by "-", "_" or "." (``backup-jdoe``, ``jdoe_old``) is
        still found."""
        if not words:
            return None
        return re.compile(r"(?<![A-Za-z0-9])(?:%s)(?![A-Za-z0-9])" % "|".join(
            re.escape(w) for w in sorted(words, key=len, reverse=True)), flags)

    def _count(self, category: str, amount: int = 1) -> None:
        self.counts[category] = self.counts.get(category, 0) + amount

    def _placeholder(self, kind: str, value: str, label: str | None = None):
        table = self._maps.setdefault(kind, {})
        if value not in table:
            table[value] = "<%s-%d>" % (label or kind, len(table) + 1)
        return table[value]

    # -- hashes --------------------------------------------------------------

    def _digest(self, value: str) -> str:
        return hmac.new(self.salt, value.strip().lower().encode("utf-8"),
                        hashlib.sha256).hexdigest()[:8]

    def serial_hash(self, serial: str) -> str:
        """An AMS serial number as ``sn-xxxxxxxx``."""
        self._count("ams serials")
        return "sn-" + self._digest("sn:" + serial)

    def installation_id(self) -> str:
        """Which printer a report comes from, without saying anything else."""
        return self._digest("installation")

    def is_bridge_serial(self, value: str) -> bool:
        return value in self.bridge_serials or bool(BRIDGE_SERIAL.match(value))

    # -- addresses and names -----------------------------------------------

    def ip(self, text: str) -> str:
        address = _private_address(text)
        if address is None:
            return text
        self._count("ip addresses")
        if address.version == 6:
            return self._placeholder("ipv6", str(address))
        if address.is_private or address.is_link_local:
            return self._placeholder("lan-ip", str(address))
        return self._placeholder("ip", str(address))

    def host(self, name: str) -> str:
        lowered = name.lower().rstrip(".")
        if lowered in ("localhost", "") or lowered.startswith("<"):
            return name
        try:
            ipaddress.ip_address(lowered.strip("[]"))
            return self.ip(lowered.strip("[]"))
        except ValueError:
            pass
        if any(lowered == site or lowered.endswith("." + site)
               for site in PUBLIC_HOSTS):
            return name
        self._count("host names")
        if lowered.split(".")[0] in self.hostnames:
            return "<host>"                  # this computer, as name.local
        return self._placeholder("host", lowered)

    def user(self, name: str) -> str:
        if name.startswith("<"):
            return name
        self._count("user names")
        return self._placeholder("user", name)

    # -- URLs ------------------------------------------------------------------

    _QUERY_PARAM = re.compile(r"(?P<name>[^&=#]+)=(?P<value>[^&#]*)")

    def url(self, match) -> str:
        scheme = match.group("scheme")
        rest = match.group("rest")
        lower = scheme.lower()
        if lower == "file":
            return match.group(0)
        if lower not in ("http", "https", "ws", "wss", "ssh", "git", "ftp",
                         "mqtt", "mqtts", "tcp", "udp"):
            # Notifier URLs (Apprise: tgram://, discord://, mailto:// ...)
            # carry their tokens in the host and path.
            self._count("secrets")
            return "%s://%s" % (scheme, REDACTED)
        trail = ""
        while rest and rest[-1] in ".,;:!?":
            trail = rest[-1] + trail
            rest = rest[:-1]
        cut = min([rest.index(c) for c in "/?#" if c in rest] or [len(rest)])
        authority, path = rest[:cut], rest[cut:]
        prefix = ""
        if "@" in authority:
            self._count("secrets")
            prefix = REDACTED + "@"
            authority = authority.rsplit("@", 1)[1]
        host, colon, port = authority.partition(":")
        if path:
            path = self._url_path(host.lower(), path)
        return scheme + "://" + prefix + self.host(host) + colon + port + \
            path + trail

    def _url_path(self, host: str, path: str) -> str:
        """``path`` starts with "/", "?" or "#"."""
        if ("discord" in host and "/webhooks/" in path) or \
                host.startswith("hooks.slack.com") or \
                "webhook" in path.lower():
            self._count("secrets")
            return "/" + REDACTED
        path_part, question, query = path.partition("?")
        segments = []
        for segment in path_part.split("/"):
            if re.fullmatch(r"[A-Za-z0-9_-]{24,}", segment) and \
                    not re.fullmatch(r"[0-9a-f]{40}", segment):
                self._count("secrets")
                segment = REDACTED
            segments.append(segment)
        path_part = "/".join(segments)
        if question:
            def param(match):
                if _SECRET_NAME_RE.match(match.group("name")) or \
                        match.group("name").lower() in (
                            "key", "sig", "signature", "auth", "code",
                            "access_token", "pass", "pw"):
                    self._count("secrets")
                    return match.group("name") + "=" + REDACTED
                return match.group(0)
            query = self._QUERY_PARAM.sub(param, query)
        return path_part + question + query

    # -- text ------------------------------------------------------------------

    def redact_text(self, text: str) -> str:
        """Return ``text`` with every private detail replaced."""
        if not text:
            return text
        text = self._scrub_known(text)
        text = self._secrets_and_keys(text)
        text = _URL.sub(self.url, text)
        text = _USERPASS_AT.sub(self._userpass, text)
        text = _EMAIL.sub(self._email, text)
        text = self._key_values(text)
        text = self._spool_lines(text)
        text = self._ams(text)
        text = _HEX32.sub(self._hex, text)
        text = _HEX_LONG.sub(self._hex, text)
        text = _MAC.sub(self._mac, text)
        text = _IPV6.sub(self._ipv6, text)
        text = _IPV4.sub(lambda m: self.ip(m.group(0)), text)
        text = self._hosts(text)
        text = self._users(text)
        return text

    def _scrub_known(self, text: str) -> str:
        for pattern in self._known_secrets:
            text, count = pattern.subn(REDACTED, text)
            if count:
                self._count("secrets", count)
        return text

    def _sub_count(self, pattern, replacement, text, category):
        text, count = pattern.subn(replacement, text)
        if count:
            self._count(category, count)
        return text

    def _secrets_and_keys(self, text: str) -> str:
        text = self._sub_count(_PRIVATE_KEY_BLOCK, "<redacted private key>",
                               text, "keys")
        text = self._sub_count(_SSH_PUBLIC_KEY, "<redacted ssh key>", text,
                               "keys")
        # Headers first, so a header's token is removed with its scheme.
        text = _AUTH_HEADER.sub(self._auth, text)
        text = self._sub_count(_BEARER, r"\1 " + REDACTED, text, "secrets")
        text = _CLI_USER.sub(self._cli, text)
        text = _CLI_SECRET.sub(self._cli, text)
        for pattern in _CREDENTIALS:
            text = self._sub_count(pattern, "<redacted token>", text,
                                   "secrets")
        text = _LONG_BASE64.sub(self._base64, text)
        return text

    def _base64(self, match) -> str:
        run = match.group(0)
        mixed = re.search(r"[A-Z]", run) and re.search(r"[a-z]", run) and \
            re.search(r"\d", run)
        if not mixed or run.count("/") * 5 > len(run):
            return run
        self._count("secrets")
        return "<redacted base64>"

    def _auth(self, match) -> str:
        if "<" in match.group("value"):
            return match.group(0)            # already a placeholder
        self._count("secrets")
        return match.group("name") + match.group("sep") + REDACTED

    def _cli(self, match) -> str:
        if _strip_quotes(match.group("value")).startswith("<"):
            return match.group(0)
        self._count("secrets")
        return match.group("flag") + match.group("sep") + REDACTED

    def _userpass(self, match) -> str:
        self._count("secrets")
        return REDACTED + "@"

    def _email(self, match) -> str:
        if match.group(0).startswith("git@"):
            return match.group(0)            # git@github.com:owner/repo
        self._count("e-mail addresses")
        return "<email>"

    def _kv_redact(self, match, replacement, category) -> str:
        value = match.group("value")
        if _strip_quotes(value).startswith("<"):
            return match.group(0)
        self._count(category)
        return match.group(0)[:match.start("value") - match.start(0)] + \
            _requote(value, replacement)

    def _key_values(self, text: str) -> str:
        text = _SECRET_KV.sub(
            lambda m: self._kv_redact(m, REDACTED, "secrets"), text)
        text = _KEY_OPTION.sub(
            lambda m: self._kv_redact(m, REDACTED, "secrets"), text)
        text = _TAG_KV.sub(lambda m: self._kv_redact(m, "<tag>", "tag data"),
                           text)
        return text

    def _spool_lines(self, text: str) -> str:
        if not _SPOOL_LINE.search(text):
            return text
        lines = text.split("\n")
        for index, line in enumerate(lines):
            if _SPOOL_LINE.search(line):
                lines[index] = _SPOOL_FIELD.sub(self._spool_field, line)
        return "\n".join(lines)

    def _spool_field(self, match) -> str:
        if match.group("name").lower() in SPOOL_ID_FIELDS or \
                match.group("value").startswith("\"<"):
            return match.group(0)
        self._count("spool details")
        return match.group("pre") + "\"%s\"" % REDACTED

    def _ams(self, text: str) -> str:
        # AMS unit IDs stay as they are; tag UIDs and AMS serials do not.
        text = _UID_KV.sub(self._uid, text)
        text = _TAG_WORD_UID.sub(self._tag_value, text)
        text = _TAG_BLOCK.sub(self._tag_value, text)
        text = _IDENTITY_REPLY.sub(self._identity_reply, text)
        text = _SERIAL_KV.sub(self._serial, text)
        text = _SERIAL_WORD.sub(self._serial, text)
        return text

    def _tag_value(self, match) -> str:
        self._count("tag data")
        return match.group("pre") + "<tag>"

    def _identity_reply(self, match) -> str:
        # The serial as src/oams.py makes it: three little-endian words.
        try:
            raw = b"".join(int(word).to_bytes(4, "little")
                           for word in match.groups())
        except OverflowError:
            raw = " ".join(match.groups()).encode("ascii")
        return "serial=" + self.serial_hash(raw.hex().upper())

    def _uid(self, match) -> str:
        value = match.group("value")
        if _AMS_UID.match(value):
            return match.group(0)            # an AMS unit ID, kept
        head = match.group(0)[:match.start("value") - match.start(0)]
        tail = match.group(0)[match.end("value") - match.start(0):]
        self._count("tag data")
        return head + "<tag>" + tail

    def _serial(self, match) -> str:
        value = match.group("value")
        if self.is_bridge_serial(value):
            return match.group(0)
        head = match.group(0)[:match.start("value") - match.start(0)]
        tail = match.group(0)[match.end("value") - match.start(0):]
        return head + self.serial_hash(value) + tail

    def _hex(self, match) -> str:
        self._count("secrets")
        return "<redacted hex>"

    def _mac(self, match) -> str:
        self._count("mac addresses")
        return self._placeholder("mac", match.group(0).lower())

    def _ipv6(self, match) -> str:
        text = match.group(0)
        if text.count(":") < 2:
            return text
        return self.ip(text)

    def _hosts(self, text: str) -> str:
        text = _JOURNAL_HOST.sub(
            lambda m: m.group("pre") + self.host(m.group("host")), text)
        text = _HOST_KV.sub(self._host_kv, text)
        text = _AT_HOST.sub(lambda m: "@" + self.host(m.group("host")), text)
        text = _MDNS.sub(lambda m: self.host(m.group(0)), text)
        if self._distinct_hosts is not None:
            text = self._distinct_hosts.sub(
                lambda m: self.host(m.group(0)), text)
        return text

    def _host_kv(self, match) -> str:
        head = match.group(0)[:match.start("value") - match.start(0)]
        return head + self.host(match.group("value"))

    def _users(self, text: str) -> str:
        text = _HOME.sub(lambda m: "/home/" + self.user(m.group("user")), text)
        text = _TILDE_USER.sub(lambda m: "~" + self.user(m.group("user")),
                               text)
        if self._distinct_users is not None:
            text = self._distinct_users.sub(
                lambda m: self.user(m.group(0)), text)
        return text

    # -- structured data -------------------------------------------------------

    def redact_data(self, value, key: str = ""):
        """A copy of JSON-like ``value`` with private details replaced, using
        each field's name as well as its text."""
        lowered = key.lower()
        if isinstance(value, dict):
            if lowered in SPOOL_KEYS:
                return self._spool_ids(value)
            return {self.redact_text(str(k)): self.redact_data(v, str(k))
                    for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            if lowered in SPOOL_KEYS:
                return [self._spool_ids(v) for v in value]
            return [self.redact_data(v, key) for v in value]
        if not isinstance(value, str):
            if lowered in SPOOL_KEYS and value is not None and \
                    not isinstance(value, (int, bool)):
                return REDACTED
            return value
        if not value:
            return value
        if lowered == "key" or _SECRET_NAME_RE.match(lowered):
            if value.startswith("<"):
                return value
            self._count("secrets")
            return REDACTED
        if lowered == "reply" and DRYER_REPLY_RE.fullmatch(value):
            return value      # the AMS's dryer status bytes (owner's decision)
        if lowered in ("hostname", "host_name", "fqdn", "nodename"):
            return self.host(value)
        if lowered in ("uid", "node") and _AMS_UID.match(value):
            return value                     # an AMS unit ID, kept
        if re.fullmatch(r"(?:ams_?)?(?:serial(?:_?(?:number|no))?|sn)",
                        lowered) and re.fullmatch(r"[A-Za-z0-9]{6,32}", value) \
                and not self.is_bridge_serial(value):
            return self.serial_hash(value)
        if re.fullmatch(r"(?:tag|tray|card|nfc|rfid)_?(?:uid|uuid|data|"
                        r"payload|blocks?)|payload", lowered):
            self._count("tag data")
            return "<tag>"
        return self.redact_text(value)

    def _spool_ids(self, value):
        if isinstance(value, dict):
            kept = {}
            for k, v in value.items():
                if str(k).lower() in SPOOL_ID_FIELDS and \
                        isinstance(v, (int, str)) and not isinstance(v, bool):
                    kept[str(k)] = v if isinstance(v, int) else \
                        self.redact_text(v)
                elif isinstance(v, (dict, list)):
                    kept[str(k)] = self._spool_ids(v)
                elif v is not None:
                    self._count("spool details")
                    kept[str(k)] = REDACTED
            return kept
        if isinstance(value, list):
            return [self._spool_ids(v) for v in value]
        if isinstance(value, int) and not isinstance(value, bool):
            return value                     # a bare spool ID
        if value is None:
            return None
        self._count("spool details")
        return REDACTED

    # -- the last check ----------------------------------------------------------

    def find_leaks(self, text: str) -> list:
        """(kinds, line number, masked text) for each line of ``text`` that
        the redaction rules would still change: the last check is exactly as
        strong as the redaction itself. ``kinds`` names what the rules would
        remove ("secrets", "ip addresses", ...). Empty for a properly
        redacted report."""
        probe = self._probe()
        findings = []
        for number, line in enumerate(text.splitlines(), 1):
            if DRYER_REPLY_LINE_RE.fullmatch(line):
                continue
            before = dict(probe.counts)
            changed = probe.redact_text(line)
            if changed == line:
                continue
            kinds = sorted(kind for kind, count in probe.counts.items()
                           if count > before.get(kind, 0)) or ["changed"]
            findings.append((", ".join(kinds), number,
                             mask(_first_difference(line, changed))))
        return findings


# The bridge snapshot's dryer row carries the AMS's dryer status answer as 110
# lowercase hex digits: temperatures, flags and vent and fan states, no
# identifiers. The owner allowed it in reports (2026-11-08), so a field named
# "reply" in exactly that form, and its line in the report's JSON, are kept;
# every other long hex string is still a secret.
DRYER_REPLY_RE = re.compile(r"[0-9a-f]{110}")
DRYER_REPLY_LINE_RE = re.compile(r'\s*"reply": "[0-9a-f]{110}",?\s*')


def _first_difference(original: str, changed: str) -> str:
    """The word of ``original`` where ``changed`` starts to differ."""
    index = 0
    limit = min(len(original), len(changed))
    while index < limit and original[index] == changed[index]:
        index += 1
    start = index
    while start > 0 and not original[start - 1].isspace():
        start -= 1
    end = index
    while end < len(original) and not original[end].isspace() and \
            end - start < 40:
        end += 1
    return original[start:end] or original[start:start + 40]


__all__ = [
    "BRIDGE_SERIAL", "COMMON_HOSTNAMES", "COMMON_USERS", "PUBLIC_HOSTS",
    "REDACTED", "Redactor", "collect_secrets", "default_salt_path",
    "distinctive_hostname", "load_or_create_salt", "mask", "secrets_in_ini",
    "secrets_in_secrets_file",
]
