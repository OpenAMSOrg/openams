# Update the Cortex Bridge firmware

## What it is, and why

The Cortex Bridge runs its own firmware: the part that talks to your AMS units.
New versions are released as **signed, encrypted packages** that ship with
OpenAMS. Updating OpenAMS brings them to your printer; installing one on the
bridge is a separate step that you start, so it never happens in the middle of
a print.

A package is opaque to your computer. Only the bridge can decrypt it, and its
boot code checks the whole package before it changes anything. It refuses a
package that is damaged, that was not made for this board, or that is not
newer than what it runs. A refused package leaves the installed firmware
untouched.

You do not have to update. Install a new version when its release note fixes
something you need, or when support asks you to.

## Check whether your bridge is up to date

Update OpenAMS first (Mainsail: **Machine → Update Manager**, entry
`openams`), so it knows about the newest firmware. Then ask the bridge which
version it runs:

```bash
python3 ~/openams/tools/cortex-bridge.py version
```

```text
Cortex Bridge CB0123456789AB
  Installed:  version 7
  Newest:     version 7 (released 2026-11-03), bundled with this OpenAMS
Up to date.
```

`Installed` is what the bridge itself reports; `Newest` is the newest firmware
that came with your OpenAMS. When they differ, the last lines say what to do:

```text
Cortex Bridge CB0123456789AB
  Installed:  version 4
  Newest:     version 7 (released 2026-11-03), bundled with this OpenAMS
Version 7 is available: Stock AMS 1: loads at full speed to the toolhead (about 12 s instead of 17 s) and reads Bambu spool tags.
Install it with `cortex-bridge.py update` while the printer and the AMS are idle.
```

`The bridge runs newer firmware than this OpenAMS bundles` means OpenAMS
itself is out of date: update it from the Update Manager.

Checking changes nothing and does not disturb a print. The same answer
appears in two other places:

- `cortex-bridge.py status`, on its `Firmware:` line, for example
  `version 4; version 7 is available`.
- `cortex-bridge.py firmware`, which lists every bundled version and marks
  the one the bridge runs as `(installed)`.

### When the version is unknown

`Installed:  unknown:` is followed by the reason:

| The reason says | What it means | What to do |
|---|---|---|
| `the bridge has not answered the version request yet` | The relay has just connected | Run `version` again in a few seconds. |
| `the bridge is busy (an AMS is working) ...` | It answers when no AMS is loading, unloading or drying, and no spool is loaded to the toolhead | Unload the loaded spool, or wait until the AMS are idle, and run `version` again. |
| `the bridge did not answer the version request` | The bridge stayed silent | Unplug its USB cable for 10 seconds, plug it back in and try again. If it stays, write a support report (below). |
| `this bridge runs lab (development) firmware ...` | Development firmware has no release number | If you did not expect that, contact support. |
| `the bridge could not read its own identity ...` or `the bridge failed its authenticity check` | The bridge's security check did not pass | Write a support report (below) and contact support. |
| `the bridge answered in a format this OpenAMS does not know` | The bridge's firmware is newer than your OpenAMS | Update OpenAMS. |
| `pyserial is missing ...` | The relay is stopped, and this command cannot open the bridge itself | `sudo systemctl start cortex-bridge`, then try again. |

`version` also tells you when the bridge is not on USB, reports a fault, or
is waiting for firmware after an interrupted update; each message names the
next step.

### For scripts and other tools

`version --json` prints one JSON object and exits with status 0 when the
installed version is known, 1 when it is not. Abridged:

```json
{
 "installed": 4,
 "latest": 7,
 "update_available": true,
 "verdict": "update-available",
 "reason_text": "",
 "serial": "CB0123456789AB"
}
```

`verdict` is one of `up-to-date`, `update-available`, `newer-than-bundled`,
`nothing-bundled` or `unknown`; with `unknown`, `reason_text` is the reason
above. Moonraker answers the same at `GET /server/openams/bridge/status`
(fields `installed`, `verdict`, `update_available` and
`installed_unknown_reason`).

## What you need

- OpenAMS installed with the bridge relay, as in
  [Set up a Cortex Bridge printer](cortex-bridge-setup.md).
- The printer idle: not printing and not paused.
- The AMS idle: not loading, unloading or drying, and **no filament loaded
  to the toolhead**. A loaded spool keeps the AMS following the extruder,
  which counts as busy: unload it first (a toolchange's unload, with the cut).
  The bridge does not switch to update mode while an AMS is busy.
- USB and 24 V connected for the whole update, which takes about a minute.

## Install it

```bash
python3 ~/openams/tools/cortex-bridge.py update
```

```text
This installs Cortex Bridge firmware version 7 (the bridge runs version 4).
Klipper loses the AMS for about a minute and is restarted afterwards. Keep the bridge powered until it finishes.
Continue? [y/N] y
  Asking the bridge to enter update mode
  Waiting for the bridge's update receiver
  The bridge is checking the package (100%)
  Writing the firmware (100%)
  The bridge is restarting
  Done
Installed. Waiting for the AMS to come back...
  1 AMS online
The bridge now runs version 7.
Klipper restarted (FIRMWARE_RESTART).
```

What happens, in order:

1. The command asks the bridge which version it runs. If that is already the
   newest, it stops there and changes nothing:

   ```text
   The bridge already runs version 7.
   Nothing was changed.
   Nothing to do: the bridge is up to date.
   ```

2. The relay releases the bridge's USB port. Klipper loses the AMS and stops
   with an MCU error; that is expected.
3. The bridge restarts into its update receiver and reads the package twice:
   once to check it, once to write it.
4. The bridge restarts into the new firmware, the relay reconnects, the AMS
   appear again, and the bridge is asked for its version once more:
   `The bridge now runs version 7.` confirms the install.
5. Klipper is restarted (`FIRMWARE_RESTART`) and is **Ready** again.

**Check:** `python3 ~/openams/tools/cortex-bridge.py version` says
`Up to date.`

To install a package you were sent rather than a bundled one, give its path:
`cortex-bridge.py update --file ~/bridge-7.oac2`. A file that is not newer than
what the bridge runs is turned down before the bridge is touched.

## If it fails

Nothing you can do here damages the bridge. A failed update prints three
sentences: what happened, what changed (usually nothing), and what to do. A
`Details:` line may follow for support. For example:

```text
Update failed: The bridge did not switch to update mode.
Nothing was changed; the bridge still runs version 4.
The bridge switches only while every AMS is idle: not loading, unloading or moving filament, and not starting or stopping a dryer. Wait until they are idle and run `cortex-bridge.py update` again.
```

| The first line says | What it means |
|---|---|
| `The bridge did not switch to update mode.` | An AMS was busy. Nothing changed. |
| `The bridge left its firmware, but its update mode did not appear on USB.` | USB trouble during the switch. `status` shows whether it is waiting for firmware. |
| `This file cannot be installed: ...` | The file given with `--file` is not Cortex Bridge firmware, or it is damaged. Nothing was sent. |
| `The bridge checked version N and refused it. It runs version M, so the version is not the reason ...` | The file was made for another board revision, or it is not signed for this bridge. |
| `The transfer to the bridge stopped part-way.` | USB or power was interrupted. |
| `The bridge reports fault F-...` | The bridge's own startup check failed. The code says which. |
| `No Cortex Bridge is visible on USB.` | Cable, port or power. |
| `The bridge accepted the firmware but did not come back on USB.` | It may be installed; reconnect USB and run `version`. |
| `More than one Cortex Bridge is connected.` | Leave only the one to update. |
| `The printer is printing.` or `The printer is paused.` | Finish or cancel the print first. |

The sentences after the first line say what to do in each case.

After an interrupted update the bridge waits for firmware, the AMS stay
offline, and `version` says `the bridge is waiting for firmware`. The bridge
keeps waiting for a complete package, so running `update` again finishes the
job.

To send support everything they need, run
`python3 ~/openams/tools/cortex-bridge.py support`; the report includes the
version the bridge reported and the result of the last update.

## Where to go next

- [Set up a Cortex Bridge printer](cortex-bridge-setup.md#troubleshooting) —
  everything else that can go wrong, and the support report.
- [The Cortex Bridge board](cortex-bridge-hardware.md) — connectors and LEDs.
