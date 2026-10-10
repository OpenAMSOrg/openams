# Update the Cortex Bridge firmware

## What it is, and why

The Cortex Bridge runs its own firmware: the part that talks to your AMS units.
New versions are released as **signed, encrypted packages** that ship with
OpenAMS. Updating OpenAMS brings them to your printer; installing one on the
bridge is a separate step that you start, so it never happens in the middle of
a print.

A package is opaque to your computer. Only the bridge can decrypt it, and its
boot code checks the whole package before it changes anything. It refuses a
package that is damaged, that was not made for this board, or that is older
than what it runs. A refused package leaves the installed firmware untouched.

You do not have to update. Install a new version when its release note fixes
something you need, or when support asks you to.

## What you need

- OpenAMS installed with the bridge relay, as in
  [Set up a Cortex Bridge printer](cortex-bridge-setup.md).
- The printer idle: not printing and not paused.
- The AMS idle: not loading, unloading or drying. The bridge refuses to start
  an update while the AMS is busy.
- USB and 24 V connected for the whole update, which takes about a minute.

## See what is available

Update OpenAMS first (Mainsail: **Machine → Update Manager**, entry
`openams`). Then:

```bash
python3 ~/openams/tools/cortex-bridge.py firmware
```

```text
version 3    2026-11-01 Faster AMS discovery after a power cycle.
version 2    2026-10-08 First release.
```

The newest version is listed first. `No bridge firmware is bundled with this
OpenAMS version yet` means there is nothing to install.

## Install it

Install it from the command line.

### From the command line

```bash
python3 ~/openams/tools/cortex-bridge.py update
```

```text
This installs Cortex Bridge firmware version 3.
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
Klipper restarted (FIRMWARE_RESTART).
```

What happens, in order:

1. The relay releases the bridge's USB port. Klipper loses the AMS and stops
   with an MCU error; that is expected.
2. The bridge restarts into its update receiver and reads the package twice:
   once to check it, once to write it.
3. The bridge restarts into the new firmware, the relay reconnects and the AMS
   appear again.
4. Klipper is restarted (`FIRMWARE_RESTART`) and is **Ready** again.

**Check:** `python3 ~/openams/tools/cortex-bridge.py status` ends with
`Everything is connected.`

To install a package you were sent rather than a bundled one, give its path:
`cortex-bridge.py update --file ~/bridge-3.oac2`.

## If it fails

The command names what went wrong. Nothing you can do here damages the bridge.

| Message | What it means | What to do |
|---|---|---|
| `The bridge said it is busy` | The AMS was loading, unloading or drying | Wait until it is idle, then run `update` again. |
| `The printer is printing` / `paused` | A print is running | Finish or cancel it first. |
| `The bridge relay is not running` | The `cortex-bridge` service is stopped | `sudo systemctl start cortex-bridge`, then try again. |
| `That file is not Cortex Bridge firmware` | The wrong file was given to `--file` | Use the package from OpenAMS or support. |
| `The bridge refused this package` | It is older than the bridge allows, or it is for another board | Nothing to do: the installed firmware is unchanged. |
| `The transfer stopped part-way` | USB or power was interrupted | Run `update` again. The bridge waits for firmware until it gets a complete package. |
| `The bridge reports a fault` | The bridge's own self-check failed | Run `cortex-bridge.py support` and contact support with the report. |

After an interrupted update, `cortex-bridge.py status` shows the bridge as
`waiting for firmware`, and the AMS stays offline until you run `update` again.
The bridge keeps waiting for a complete package, so the next `update` finishes
the job.

## Where to go next

- [Set up a Cortex Bridge printer](cortex-bridge-setup.md#troubleshooting) —
  everything else that can go wrong, and the support report.
- [The Cortex Bridge board](cortex-bridge-hardware.md) — connectors and LEDs.
