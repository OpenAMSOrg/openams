# The Cortex Bridge support report: format, redaction, snapshot, sending

**Status: note, not a numbered contract (2026-11-08).** The report, its
redaction and the relay side of the bridge snapshot EXIST; the owner's
privacy decisions of 2026-11-08 are applied (§6); sending is built but has NO
destination until the owner decides one (§6). Markers follow the
[contracts legend](README.md#legend).

The customer guide is [docs/guide/support-report.md](../guide/support-report.md).
Code: `cortex_bridge/support_report.py` (builder), `cortex_bridge/redact.py`
(redaction and the last check), `cortex_bridge/bridge_snapshot.py` (the
snapshot: decoding, gate, query, reading, allowlist), `cortex_bridge/relay.py`
and `cortex_bridge/cortexd.py` (asking the bridge),
`cortex_bridge/support_submit.py` (sending), `cortex_bridge/cli.py`
`cmd_support`. Tests: `test/test_cortex_bridge_support_report.py`,
`test/test_cortex_bridge_snapshot.py`, `test/test_cortex_bridge_relay.py`.

## 1. Why

On 2026-11-08 a customer's stock AMS 2 Pro lit up behind a Cortex Bridge but
no AMS was found. `status` showed USB running (production firmware), the relay
"bridge connected; no AMS answering", firmware 2 with 7 available, and Klipper
in error `No [fps] section found` (expected before an AMS is added); the
installer had also printed gco-routines warnings. Diagnosing it took a
screenshot and a human. Every one of those facts is now in one file, written
by one command, with nothing private in it.

## 2. Format (format 1) — EXISTS

UTF-8 text. A header (title; `format 1, written <UTC>, installation <id>`; a
three-line notice), then sections, each a line `== <title>[ (<note>)]`
followed by its body and a blank line, then `== removed`. Section titles are
stable; a tool can split on `\n== `. Structured bodies are JSON (indent 1,
sorted keys, except the snapshot, which keeps the wire's order after its
plain reading); the others are text.

| Section | Source |
|---|---|
| `summary` | `cli.status_lines(facts)` and `cli.next_step(facts)`: the same lines `status` prints |
| `openams` | `git describe --always --dirty --tags`, `HEAD`, branch, commit date, count of changed tracked files, `origin` URL |
| `system` | `/etc/os-release` `PRETTY_NAME`, kernel, machine, Python |
| `bridge` | `cli.gather()`: `usb`, `relay_service`, `relay` (the relay's `status.json`), `installed_firmware`, `bundled_firmware`, `configured` |
| `bridge snapshot` | `bridge_support_snapshot()` (§4) |
| `klipper` | Moonraker `/printer/info` (`state`, `state_message`, `software_version`, `app`, `cpu_info`) and `/server/info` (`klippy_state`, `klippy_connected`, `moonraker_version`, `api_version_string`, `components`, `failed_components`, `warnings`), and the Klipper service state. Only these fields are copied |
| `config: OpenAMS sections` | From every `*.cfg` up to two folders deep (Klipper's `printer-YYYYMMDD_HHMMSS.cfg` backups skipped): `[oams]`, `[fps]`, `[oams_manager]`, `[filament_group]`, and `[mcu]` in `oams.cfg` or with a `/dev/oams/` serial; comment lines dropped |
| `config: section names by file` | Every section header of those files (shows `[include]`s, `[exclude_object]`, a stray OpenAMS 1 section) |
| `moonraker.conf: OpenAMS sections` | `[openams_bridge]`, `[openams_spoolman]`, `[openams_taglib]`, `[spoolman]`, `[update_manager]` sections naming openams or cortex |
| `relay service` | `systemctl status cortex-bridge --no-pager -n 40` |
| `install.sh, last run` | Last 300 lines of `openams-install.log` (install.sh tees its output there from this version) |
| `klippy.log` | From the last `Start printer at` (or the last 300 lines if that is shorter), periodic `Stats` lines except the last 3 dropped, capped at 2000 lines (first 400 + last 1600) |
| `moonraker.log` | Last 400 lines, Moonraker's `Moonraker Configuration` dump removed |
| `relay support log` | Last 400 lines of `cortex-bridge.jsonl` |
| `removed` | Counts by kind, and the lines the last check removed |

The file goes to the log folder as `cortex-bridge-support-<local time>.txt`,
where Moonraker lists it (Mainsail: Machine → Log files).

## 3. Redaction — EXISTS

Every section passes through one `redact.Redactor`, structured data field by
field (`redact_data`) and text line by line (`redact_text`); then the whole
rendered file passes again, then `find_leaks` runs and `remove_leaks`
replaces any flagged line. The footer is generated after that and holds only
counts.

| Kind | Rule | Placeholder |
|---|---|---|
| Known secrets | Values collected before the report is built: secret-named options and `key` in every `*.cfg`/`*.conf` of the config folder, every value of Moonraker's secrets file (`<data>/moonraker.secrets`, or `[secrets] secrets_path`), the current Wi-Fi SSID (`iwgetid -r`). Replaced wherever they appear | `<redacted>` |
| Secret options | `name: value`, `name = value`, `"name": "value"` where the name's last word is password, passwd, passphrase, secret, token, apikey, psk, ssid, cookie, credential(s), bearer, api/access/private/auth key, client secret or session id (so `mqtt_password` and `bot_token`, not `webhooks` or `tokens`); an option named exactly `key` at the start of a line (the Bambu tag key, in `oams.cfg` and in Klipper's config copy in `klippy.log`). The whole value goes: to the end of the line for a bare key, the whole quoted string (escaped quotes included) for a quoted one | `<redacted>` |
| Command lines | `-u`/`--user`/`--proxy-user` with `user:pass`; `--password`, `--token`, `--api-key`, `--secret`, `--auth` and similar flags with their value; `user:pass@host` outside a URL. A bare `-p` is left alone (often a port) | `<redacted>` |
| Credentials | GitHub, Slack, AWS and Google tokens, `sk-` keys, JWTs and similar token shapes; `Authorization:`/`X-Api-Key:` headers; `Bearer <token>`; mixed-case base64 runs of 60+ characters; 32-hex strings (the tag key's and Moonraker's API key's shape) and hex runs of 48+ | `<redacted token>`, `<redacted>`, `<redacted base64>`, `<redacted hex>` |
| Keys | PEM private key blocks; SSH public keys | `<redacted private key>`, `<redacted ssh key>` |
| URLs | User info removed; host replaced unless loopback or a public site (`redact.PUBLIC_HOSTS`); secret-named query values; path segments of 24+ token characters; Discord/Slack/webhook paths; any non-web scheme (Apprise `tgram://`, `discord://`, ...) whole | `<redacted>@`, `<host-N>`, `scheme://<redacted>` |
| Addresses | IPv4 except loopback, `0.0.0.0`, `255.*` netmasks and `0.*` (no machine has one, and the version bytes an AMS reports read as `0.0.6.49`); IPv6 except `::1`; MAC. Numbered per report in order of first appearance | `<lan-ip-N>`, `<ip-N>`, `<ipv6-N>`, `<mac-N>` |
| Host names | This computer's (`gethostname`, `/etc/hostname`) wherever it appears as a word, but only when no ordinary word could be it (a digit, `-`, `_` or `.`, or 10 letters or more, and not in `redact.COMMON_HOSTNAMES`); any host name in a journal prefix, after `@`, as `hostname: x`, and as `<name>.local`; other `*.local/.lan/.home` names. A printer called `oams` keeps `/dev/oams/` readable | `<host>`, `<host-N>` |
| Users | `/home/<name>`, `~name/`, and the current user name as a whole word unless common (`redact.COMMON_USERS`, e.g. `pi`) | `<user>`, `<user-N>` |
| E-mail | Any address except the `git@` remote form | `<email>` |
| AMS serials | `serial`, `serial_number`, `sn`, `ams_serial` with a 6-32 character alphanumeric value (digits only too), unless it is the bridge's own serial (`CB` + hex, or one sysfs or the relay reports); 24 hex digits after "serial" or "identity" in free text; the identity reply `uid0=... uid1=... uid2=...` in Klipper's message dumps | `sn-` + HMAC-SHA256(salt, `"sn:" + serial`)[:8], the same for one unit in every form |
| Tag data | `tag_uid`, `tray_uuid`, `card_uid(s)`, `tag_data`, `payload`, `block_data`...; `uid` with a value that is not a 12-digit AMS unit ID; 4, 7 or 10 bytes of hex after "card", "tag" or "uid" in free text (`MFRC522 <name> card 04a1b2c3d4e5f6`); `BLOCK=<hex>` of the RFID read answer. A bare 8-hex word elsewhere (a commit) stays | `<tag>` |
| Spoolman | On lines naming Spoolman or carrying a `"spool"`/`"filament"` object: every JSON field except `id`, `spool_id`, `filament_id`, `vendor_id`, `lane`, `bay`, `group`, `unit`. In structured data, under `spool`, `filament`, `active_spool`...: the same | `"<redacted>"` |

Kept as they are, by the owner's decision of 2026-11-08 (DECIDED, section 6):
the bridge's USB serial; the AMS unit IDs (the roster UID `status` prints, the
12 hex digits of `/dev/oams/<uid>`, in the config, the logs and the
snapshot); the AMS's 4-character tag suffix (`N573`); all four version bytes
the AMS reports. The AMS serial stays hashed.

The salt is 16 random bytes in `~/.config/openams/support-report-salt`
(`$XDG_CONFIG_HOME` respected), created on first use with mode 0600. The
`installation` value in the header is HMAC(salt, "installation")[:8]: it
says two reports come from one printer and nothing else.

`find_leaks`, the last check, runs the same rules over each line of the
finished text (with its own placeholders and counts) and flags every line
they would still change, naming what they would remove. It is exactly as
strong as the redaction: there is no second, weaker set of patterns to fall
behind. It gates the report's final pass and `--submit --file`. Findings are
masked (`s********7`), so the check never repeats a secret.

The test `ReportContentsTests` builds a realistic printer (moonraker.conf
with MQTT, notifier, Spoolman and secrets file; `oams.cfg` with the tag key;
`printer.cfg` with shell commands carrying a JWT and a `curl -u` password;
klippy, Moonraker, install and relay logs with addresses, Wi-Fi, SSH keys, tag
UIDs (as fields and as a reader logs them), spool details, AMS serials (as a
field and as the unit identity), AMS unit IDs and a tag suffix) and asserts
that none of 30 private values survives, that `find_leaks` finds nothing in
the whole file, that the unit IDs, the suffix and the AMS version are still
there, and that the facts which diagnosed the 2026-11-08 case are all there.
Its fake secrets are assembled at test time so no tracked file holds a
credential-shaped string.

## 4. The bridge snapshot — EXISTS

When no AMS comes online, Klipper has nothing to log. Production bridge
firmware 9 and later answer a support request with counters, enums and
reason codes that say what happened on the AMS line. The relay asks for them;
the report and `status` read them.

**On the mux stream** (`cortexd.py`): `SUPPORT_REQUEST` (type 8, host to
bridge) carries a 4-byte token; `SUPPORT_REPORT` (type 9, bridge to host)
carries the token, a status byte (0 ok, 1 rate-limited, 4 busy) and, on 0, a
body of up to 507 bytes. cortexd checks only the framing (5 to 512 bytes) and
hands each report to `CortexCore.support_handler`; a report that does not
parse, or a handler error, never touches the AMS traffic.

**The gate** (`bridge_snapshot.gate`): the relay sends type 8 only when the
bridge's USB manufacturer is `SiForge` **and** its identity report in this
USB session gave version 9 or later. Firmware 7 and 8 treat type 8 as a
stream fault and drop every AMS for a moment, which Klipper sees as units
going offline. A lab image, an unknown image, or a bridge whose identity has
not answered yet is never asked. The relay tests drive a fake bridge through
firmware 8, lab firmware and an unanswered identity and assert that no type-8
message reaches it.

**Asking** (`bridge_snapshot.SnapshotQuery`, driven from the relay's session
loop): one request at a time with a fresh random token, an answer awaited for
1.0 s (other tokens ignored; the bridge answers in one pass of its main loop,
before its next roster or unit message), status 1 or 4 retried after 1.1 s,
at most 3 attempts, and never more than one request a second. The relay
therefore answers the control socket within `WORST_CASE_S`, 6.2 s (the 1 s
interval, three attempts, two retry waits); the report waits 3 s longer than
that. Control connections that
ask while a request is out share its answer; a session that ends answers
them `not-connected`. Each finished request adds one `bridge_snapshot` record
(`result`: `ok` or the failure code) to the relay's support log.

**The control-socket method**, one line on `/run/cortex-bridge/control.sock`:

```json
{"method": "support_snapshot"}
```

| Answer | When |
|---|---|
| `{"event": "support_snapshot", "ok": true, "snapshot": {...}, "raw": "<base64 body>"}` | the bridge answered |
| `{"ok": false, "error": "unsupported-firmware", "minimum_firmware": 9, "message": ...}` | firmware older than 9, a lab image or an unknown image |
| `{"ok": false, "error": "firmware-unknown", ...}` | the identity has not answered yet |
| `{"ok": false, "error": "not-connected" \| "timeout" \| "rate-limited" \| "busy" \| "malformed" \| "refused", ...}` | no session, no answer in time, still limited after 3 tries, a body this host cannot read, another status |

A relay from before this change answers `unknown-method`; the report says to
update OpenAMS.

**Decoding** (`bridge_snapshot.decode_body`): format 1, then
`id | length | value` sections in ascending order: `system`, `line`,
`master`, `discovery` and `host` once each, then `units`, `candidates`,
`feed_failures`, `dryers` (bridge firmware 10) and `refusals` rows. Unknown ids are skipped, bytes after the
known fields of a section are ignored, and a section shorter than its known
fields, a cut header, ids out of order or another format make the snapshot
malformed. Each field becomes one key, in wire order; enums become names
(`unknown_<n>` for a value this host does not know); bit fields become
objects of booleans; instants are `null` for never; the unit ID is 12
lowercase hex digits; the AMS version is a list of four integers; a tag
suffix is its four characters, `null` for none, or `0x` and hex when not
printable. The layout tables in `bridge_snapshot.py` are the reference on
this side.

**Feed failures** (section 0x12, one 24-byte row per admitted unit whose
latest failure was a load or calibration): `slot`, `index`, the odometer in
millimetres from park when the operation started, at its lowest and when it
failed (`odo_start_mm`, `odo_low_mm`, `odo_at_mm`, signed, so a reading
below park is negative), `since_progress_ms` (since the last counted
progress; `null` for none), `feed_ms` (from the start to the failure; a
duration, so 0 stays 0), `odo_flags` (`start`, `base`, `rewind_seen`,
`rewinding`, `at_valid`, `rule`) and `ams_state` at the failure. Production
firmware shows its odometer nowhere else, so this row is how a report shows
the rewind profile of, say, an AMS 1 whose load failed.

**Dryers** (section 0x13, one 73-byte row per unit whose dryer schedule has
run since it was bound). Bridge firmware 11: `slot`, `index`, `vent_state`
(`idle`, `drying`, `forcing` while an exhaust request is held, `cooldown`),
`opened_by` (`none`, `forced`: the vents opened during a request, `unit`:
without one), `closed_by` (`none`, `end`, `stopped`, `disabled`), `pulses`
(exhaust requests this dry, the start's included), `vent_flags` (`enabled`,
`timed`, `forcing`, `cooling`, `fast`, `open_at_pulse`: open during the latest
request, `vents_open`: open now), `ended` (how the last sealed cooldown went:
`cool_down`, `stop`, `unit_off`, `vents_opened`: they opened again during it,
`capped`), `last_pulse_ms`, `reply_ms`, `reply_length` and `reply`: the unit's
latest dryer status answer as 110 hex digits (55 bytes, zero past
`reply_length`). A row from firmware 10 (`system.app_version` 10) has the same
bytes with that firmware's meanings: `vent_state` `closed`/`open`, `openings`
and `ah_mgm3` (the loop's last moisture sample) instead of `pulses` and
`last_pulse_ms`. The reply is the one block of received bytes a report
carries (the owner's decision below).

**Tested against the firmware's own encoder:**
`test/fixtures/bridge_snapshot/vectors_v1.json` holds five bodies the bridge
firmware's encoder wrote (a silent line, an AMS refused at an HT index with
an unrecognised suffix, an admitted AMS Klipper has not configured, an AMS 1
whose load stalled while it was still rewinding, and an AMS 2 Pro in the
sealed cooldown after a dry, encoded by bridge firmware 11), each with its exact
decoded JSON; the decoder must reproduce it key for key and in order, and
each must read as its class.

**In the report** (`support_report.bridge_support_snapshot`): only the
fields `bridge_snapshot.allowlisted` keeps, each checked against its decoded
type (integers, booleans, enum names, the unit ID, the 4-byte version, the
suffix, a dryer reply of exactly 110 lowercase hex digits); anything else, including a field a later firmware adds, is dropped,
and `raw` is not carried. The section starts with a plain reading,
`bridge_snapshot.classify`, first match wins:

| Class | Matches | The report says (short) |
|---|---|---|
| `bridge-off-line` | boot or genuine status not 0, or the effective master bit clear | the bridge's own checks keep it off the line: contact support |
| `other-controller` | a foreign master or TX fault in `role`, or bytes received while the bridge never took the line | something else talks on the line |
| `silent-line` | messages sent, no byte received | nothing comes back: check the data pair, pins and crimps; lit lanes prove power, not data |
| `garbled-line` | bytes received, none decoded | check the data cable, its orientation and crimps |
| `refused` | no unit admitted, an AMS announced or a candidate exists, and a refusal reason is known | the reason in words, with the tag suffix when there is one |
| `not-admitted` | the same without a refusal | the identification step it stopped at |
| `unused-by-klipper` | an admitted unit Klipper has not configured or not talked to | check `[mcu] serial: /dev/oams/<uid>` and FIRMWARE_RESTART |
| `none` | nothing above | none of the known patterns |

After the class, one sentence per feed-failure row
(`bridge_snapshot.feed_failure_lines`), built from the row's fields and its
unit's `last_failure_kind` and nothing else: which operation failed, how the
odometer moved (`rewound from <start> mm to <at> mm` when a rewind was seen
or the reading fell, with the lowest reading when it is 10 mm or more from
the last; the plain readings otherwise), how long it then made no progress,
how far into the feed, whether it was still rewinding, and the AMS state.
For example: `Last load of AMS df5d1d75218e failed: the AMS rewound from
1200 mm to 601 mm and then stopped moving for 4 s, 6.2 s into the feed; it
was still rewinding. AMS state: moving.`

Then one sentence per dryer row (`bridge_snapshot.dryer_lines`): what the
dry is doing, the exhaust requests sent, whether the vents are open and
whether the AMS opened them on a request, why they last closed, and how the
last dry ended. For example: `Dryer of the AMS at index 0: in the sealed
cooldown, 1 exhaust request this dry, vents closed (the AMS opened them on a
request); vents last closed at the end of the dry; the last dry ended with the
cool-down request.`

**In `status`:** when the relay runs and no AMS is online, `status` asks for
the snapshot; if it has a reading, the next-step line becomes `No AMS
answers. The bridge reports: <summary> <action>`.

- **TO ADD:** asking a bridge directly when the relay is stopped (as
  `cortex-bridge.py version` does for the identity), and a support-log record
  of the snapshot when the roster stays empty after a session starts.

## 5. Sending — EXISTS, no destination

Nothing leaves the computer unless all of these hold:

1. a destination is configured: `support_url` in `[openams_bridge]`
   (moonraker.conf; the Moonraker component reads the option so Moonraker
   accepts it, and does nothing with it), or `--to URL` for one run.
   **None is configured by default, and the installer adds none;**
2. `cortex-bridge.py support --submit` runs with a terminal on stdin;
3. the person is shown the file, its size and line count, the destination
   and its origin, the section list, and (`v`) the whole text, and answers
   `y` or `yes`. Any other answer, Enter or end of input sends nothing.

No flag skips the question. What is sent is the report file's bytes as they
are when the person answers. `--submit --file <report>` sends an existing,
possibly edited report after running `find_leaks` on it with this printer's
names and secrets; any finding refuses it.

**Sender interface:** `support_submit.SENDERS` maps a URL scheme to a factory
`factory(url, opener=None)` returning an object with `send(data: bytes) ->
str` (a one-line answer to show) that raises `SendError` with plain words.
Built in: `HttpsPost` for `https` (and `http` to loopback only, for a local
test server): `POST` with `Content-Type: text/plain; charset=utf-8` and
`X-OpenAMS-Report: cortex-bridge-support/1`, 30 s timeout, no user info in
the URL, a 2xx answer required. Redirects are not followed (urllib would
turn a redirected POST into a GET without the report and call the final
answer a success): any 3xx is a failure that names the redirect's host, and
nothing more is sent. Any other scheme is refused. The tests replace
urllib's opener with one that fails the test, and check the default opener
against a server on 127.0.0.1 that answers 302.

## 6. Decisions

### Decided (the owner, 2026-11-08)

- **AMS unit IDs: DECIDED, shown raw.** The roster UID that `status` prints
  and `/dev/oams/<uid>` names appears as it is, in the config sections, the
  logs and the snapshot, so support can match a unit with its `[mcu]`.
- **AMS serials: DECIDED, hashed with the per-printer salt.** Reports from
  one printer name a unit alike; a unit moved to another printer does not
  correlate, and the hash cannot be brute-forced without the salt.
- **The AMS tag suffix: DECIDED, allowed raw.** The 4 characters (`N573`,
  `N460`, ...) name a model class, not a unit; the snapshot carries them, and
  the redactor leaves them alone.
- **The version bytes the AMS reports: DECIDED, all four allowed.**
- **The AMS's dryer status reply: DECIDED, allowed raw** (an exception to "no
  received bytes", limited to that reply). Its 54 or 55 bytes hold
  temperatures, flags and vent and fan states, no identifiers; they show where
  an AMS HT reports its vents and whether the end-of-dry cool-down request
  avoids a vent purge.
- Everything else the redactor removes stays removed.

### Open (the owner's)

1. **The destination.** The code needs only a URL and, for anything but an
   HTTPS POST, a sender class. The options:
   - **A SiForge HTTPS endpoint** (fits the built-in sender as it is): no
     credential on the printer, the server can cap size, rate-limit, answer
     with a case number, and keep reports private. Needs a server, storage
     and a retention policy.
   - **A GitHub issue:** needs a token on every printer (extractable) or a
     proxy service; an issue in a public repository would publish the logs.
     Only through a proxy into a private repository.
   - **A Discord webhook:** the webhook URL is a credential anyone can lift
     from a printer and spam; the channel's visibility decides who reads the
     logs.
   - **E-mail:** needs SMTP credentials on the printer or a relay; reports
     land in spam filters.
2. **Whether a release ships a default `support_url`** (the consent question
   would still be asked every time). Today: none.
3. **A retention and privacy statement** for received reports, to publish
   before any destination is enabled.
4. **A Mainsail button:** sending from Moonraker would need its own consent
   screen; not built, and `--submit` refuses without a terminal.
5. **An upload size limit** (a report is typically tens of kilobytes; the
   klippy.log cap bounds it at a few hundred).
6. **The first firmware with the snapshot.** The gate assumes version 9
   (`bridge_snapshot.MINIMUM_FIRMWARE`); if another release carries it first,
   change that number, never lower it below a release that has it.
