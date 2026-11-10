# The Cortex Bridge support report

## What it is, and why

When a Cortex Bridge printer does not find its AMS, or a load or a firmware
update fails, support needs the same handful of facts every time: which
OpenAMS and bridge firmware you run, what the bridge and its relay see, what
Klipper says, and the end of the logs. One command collects all of them into
one file, so you do not have to take screenshots or answer the same questions
one at a time.

The report is written on your printer, with private details removed before
the file is written. **Writing it sends nothing anywhere.** You read it,
then attach it to your support request yourself.

## Write a report

```bash
python3 ~/openams/tools/cortex-bridge.py support
```

```text
Wrote /home/<you>/printer_data/logs/cortex-bridge-support-20261108-101500.txt
  It holds: summary, openams, system, bridge, bridge snapshot, klipper, ...
  Removed before writing: ip addresses (4), secrets (3), user names (6), ...
  Next step `status` names: No AMS answers. Check 24 V at PWR IN ...
Nothing was sent. Mainsail lists the report under Machine > Log files; attach it to your support request.
```

Mainsail lists the file under **Machine → Log files**, where you can open it,
read it and download it. `cortex-bridge.py status` reminds you of this command
whenever its last line names a problem, and so does the installer when it
finds no AMS.

Options:

| Option | What it does |
|---|---|
| `--output FILE` | Write the report to `FILE` instead. |
| `--print` | Also print the report in the terminal. |
| `--logs-dir DIR` | Klipper's log folder, if it is not `~/printer_data/logs`. |
| `--submit` | After writing it, offer to send it (see [Send it directly](#send-it-directly)). |

## What is in it

The report is plain text, one section after another:

| Section | What it holds |
|---|---|
| summary | The lines `cortex-bridge.py status` prints, and the next step it names. |
| openams | The OpenAMS version, commit and branch, and whether files were changed. |
| system | Operating system, kernel, processor type and Python version. |
| bridge | The bridge on USB (state, serial number, firmware image), the relay service and its status, the firmware version it runs against the newest one bundled, and the AMS units in your config. |
| bridge snapshot | With bridge firmware 9 or later: what the bridge itself saw on the AMS line, read out in one sentence first (see [The bridge snapshot](#the-bridge-snapshot)), then its counters. With older firmware, a line saying why there is none. |
| klipper | Klipper's and Moonraker's state, error message and versions. |
| config | The OpenAMS sections of your config (`[mcu]` for each AMS, `[oams]`, `[fps]`, `[oams_manager]`, `[filament_group]`), the section names of every config file, and the OpenAMS and Spoolman sections of `moonraker.conf`. |
| relay service | `systemctl status cortex-bridge`. |
| install.sh, last run | The end of the installer's last output. From this version on, `install.sh` keeps it in `openams-install.log` in the log folder. |
| klippy.log | The log from Klipper's last start, without its once-a-second `Stats` lines. |
| moonraker.log | The end of Moonraker's log, without the copy of its configuration it writes at start. |
| relay support log | The end of the relay's own log, `cortex-bridge.jsonl`. |
| removed | How many details of each kind were removed. |

## The bridge snapshot

When no AMS comes online, Klipper has nothing to log. A bridge running
firmware 9 or later keeps its own count of what happened on the AMS line,
and the report asks it for them. Its first line is a plain reading, such as:

| The report says | What it means |
|---|---|
| The bridge sends on the AMS data line and nothing comes back. | The data pair between the bridge and the AMS is open: a pushed-back pin, a bad crimp, a broken cable, or an AMS that has not started. Lit lanes only prove that power gets through. |
| Bytes arrive from the AMS but none of them decode. | Check the data cable: its orientation, its crimps, and that it is the right cable. |
| An AMS answers but the bridge refuses it: ... | The AMS reached the bridge and was turned away; the sentence gives the reason, for example an AMS index kept from another printer (power the AMS off and on). |
| The bridge offers AMS `<id>` but Klipper does not use it. | The AMS is online. Check that an `[mcu]` section in `oams.cfg` has `serial: /dev/oams/<id>`, then **FIRMWARE RESTART**. |
| Last load of AMS `<id>` failed: the AMS rewound from 1200 mm to 601 mm and then stopped moving for 4 s, 6.2 s into the feed; ... | What the AMS's own odometer showed around the failed load (or calibration): where it started, where it was when it failed, how long it had stopped moving, and whether it was still rewinding. Support reads it with the rest of the report. |
| Last unload of AMS `<id>` failed: the odometer stood at 2000 mm while the AMS pulled the filament back, and then it stopped moving for 4 s, ... | Bridge firmware 14 or later: the AMS pulled the filament back after a cut but it did not move, and the bridge stopped it. Most often the cut did not sever the filament; see [troubleshooting](troubleshooting.md). |
| Dryer of the AMS at index 0: drying, 4 exhaust requests this dry, vents open (the AMS opened them on a request); ... | Bridge firmware 10 or later: what the dryer did during the last dry (the bridge's requests to vent, whether the AMS opened its vents, how the dry ended). The report also carries the AMS's own dryer status bytes for support. |

`cortex-bridge.py status` asks for the same reading when no AMS answers, and
its last line names it. The bridge is only asked when it runs firmware 9 or
later: older firmware does not know the question.

## What is removed

Before anything is written, every section passes through a filter that
replaces:

| Removed | Shown as |
|---|---|
| Passwords, API keys and tokens, in any config or log line (`moonraker.conf`, Moonraker's secrets file, notifier and webhook URLs, the Bambu tag `key` in `[bambu_lab_tag_processor]`, a password on a command line such as `curl -u name:password`), and every value from your secrets files wherever it appears | `<redacted>` |
| SSH keys, private and public | `<redacted ssh key>` |
| Wi-Fi network names and passphrases | `<redacted>` |
| IP addresses (not `127.0.0.1` or `0.0.0.0`) and MAC addresses | `<lan-ip-1>`, `<ip-1>`, `<mac-1>`: the same address is the same placeholder throughout one report |
| This computer's host name, `.local` names and private web addresses | `<host>`, `<host-1>` |
| Your user name, in paths and on its own | `<user>` |
| E-mail addresses | `<email>` |
| AMS serial numbers | `sn-1a2b3c4d`: a short code made with a random key that stays on your printer, so two reports from your printer name the same AMS the same way, and nobody can work back to the real number |
| RFID tag UIDs and tag contents | `<tag>` |
| Spoolman spool details (names, vendors, lots, comments, weights) | `<redacted>`; only the spool and filament IDs stay |

What stays:

- the bridge's own USB serial number (`CB...`), which support needs to
  identify the bridge;
- the AMS unit IDs, the 12 digits after `/dev/oams/` that `status` prints, so
  support can match them with your `[mcu]` sections;
- the AMS's 4-character tag suffix (such as `N573`), which names its model,
  not the unit, and the version the AMS reports;
- the dryer's status bytes (bridge firmware 10 or later): temperatures, flags
  and vent and fan states, nothing that identifies you or the unit;
- versions, states, error messages, and public addresses such as
  `github.com`.

After the filter, a last check reads the whole file once more. Any line that
still looks private (an address, a key, a name from your printer) is replaced
by `<line removed by the last check: ...>`. The `removed` section at the end
counts what was taken out.

The filter is careful, not perfect. Open the report in Mainsail and read it
before you share it, and delete any line you would rather keep to yourself.

## Send it directly

Sending is **off** until two things are true:

1. **A destination is set up.** None is by default. Support tells you the
   address when direct sending is available; you add it to the
   `[openams_bridge]` section of `moonraker.conf`:

   ```ini
   [openams_bridge]
   support_url: https://<address support gives you>
   ```

   or name it for one run with `--to https://...`.
2. **You say yes, in a terminal.** Run:

   ```bash
   python3 ~/openams/tools/cortex-bridge.py support --submit
   ```

   It writes a fresh report, then shows the file, its size, where it would go
   and what it contains, and asks `Send it? [y = send, v = view it here, N =
   no]`. `v` prints the whole report. Only `y` sends it; anything else, or
   pressing Enter, sends nothing and keeps the file.

What is sent is exactly the report file, as it is at that moment, over an
encrypted (https) connection, to that address only: if the destination
answers with a redirect elsewhere, nothing more is sent and the command says
where it pointed. To send a report you have already written, and
perhaps edited, use `--submit --file <report>`; it is checked again first,
and refused if something in it still looks private.

Without a terminal (from a script or a Mainsail macro), `--submit` sends
nothing and says so.

## Where to go next

- [Set up a Cortex Bridge printer](cortex-bridge-setup.md#troubleshooting):
  the bridge's table of symptoms and fixes.
- [Update the Cortex Bridge firmware](cortex-bridge-update.md).
- [Troubleshooting](troubleshooting.md): Klipper's error messages matched to
  their causes.
- For developers: [the support report note](../contracts/support_report.md)
  (format, redaction rules, the bridge snapshot interface and the sender).
