# The OpenAMS guide

## What it is, and why it exists

A multi-material printer needs somewhere to park several spools and a
way to put the right one in the extruder without the operator walking over.
OpenAMS is that: a unit per box, a pressure sensor between the unit and the
toolhead, and a Klipper plugin that drives the whole sequence.

The unit counts filament past each bay, watches the buffer pressure, and drives
its own motor until the pressure settles. The host plugin turns that into slicer
toolchanges: `T0`, `T1`, `T2`. It also publishes status, reads RFID spool tags,
and exposes a versioned API so a touchscreen can show what is loaded where.

## Which guide is yours

| You have | Start with |
|---|---|
| A stock Bambu Lab AMS 1, AMS 2 Pro or AMS HT and a **Cortex Bridge** board | [Set up a Cortex Bridge printer](cortex-bridge-setup.md) — wiring to first load, nothing flashed |
| An AMS with **OpenAMS boards** (OpenAMS mainboard and FPS, usually on CAN) | [Install](install.md), then [Configure](configure.md) |
| A BoxTurtle, ERCF, NightOwl or similar MMU running **klipper-mmu** | [Install](install.md#klipper-mmu) and the [klipper-mmu docs](../../klipper_mmu/docs/) |

## What it adds

- Filament switching from `T0`-`T19` macros, with cut, retract, purge and reload.
- Runout detection and automatic reload from the same filament group.
- RFID spool identification through MFRC522 readers.
- One status surface for UIs: `oams_manager`, the `openams/status` webhook and
  the [UI API](../UI_API.md).
- Optional spool-color and material data from Spoolman.
- klipper-mmu support, so a BoxTurtle, ERCF, NightOwl or similar MMU appears as
  an ordinary OpenAMS unit.

## The parts, one project

| Part | What it is | Which printers need it |
|---|---|---|
| **Host plugin** — [`src/`](../../src/), [`moonraker/`](../../moonraker/) | The MIT-licensed Klipper extras and Moonraker components. Talk to AMS units over Klipper's serial protocol. | Every printer. |
| **Cortex Bridge support** — [`cortex_bridge/`](../../cortex_bridge/) | The MIT-licensed relay service that gives Klipper one serial port per AMS behind a Cortex Bridge, and the `cortex-bridge.py` setup and firmware-update tool. | Printers with a Cortex Bridge. The installer sets it up when the bridge is connected. |
| **klipper-mmu** — [`klipper_mmu/`](../../klipper_mmu/) | The GPL-3.0-or-later Klipper MCU add-on. Runs MMU motion on the MMU's own microcontroller. | Only an MMU with its own microcontroller that wants its own motion loop. |

All of them install with the same `install.sh` and update with the same
Moonraker update entry. A printer with a plain AMS unit never enables
klipper-mmu; the symlink it creates is inert until a config section names it.

An AMS behind a Cortex Bridge and a klipper-mmu unit both look like an AMS
unit from the host's side: same status object, same commands, same UIs.
Nothing in a slicer profile or a macro has to know what is behind the unit.

## The guide

| Page | Read it when |
|---|---|
| [Set up a Cortex Bridge printer](cortex-bridge-setup.md) | You have a Cortex Bridge and a stock AMS: wiring, install, config and the first load, step by step. |
| [The Cortex Bridge board](cortex-bridge-hardware.md) | You want the bridge's connectors, pinouts and LEDs. |
| [Update the Cortex Bridge firmware](cortex-bridge-update.md) | A new bridge firmware version shipped with OpenAMS. |
| [Install](install.md) | You are setting up a printer, or moving from `klipper_openams`. |
| [Configure](configure.md) | OpenAMS is installed and you are editing `oams.cfg`. |
| [Using OpenAMS](using.md) | It runs, and you want to load, unload, toolchange, edit groups or calibrate. |
| [Configuration reference](reference.md) | You need an option, its default, or a command's arguments. |
| [Troubleshooting](troubleshooting.md) | Something is wrong and you want the error text matched to a cause. |
| [What changed in oams_macros.cfg](macro-changes.md) | You are updating an install that had copied macros, or enabling concurrent toolchanges. |
| [FAQ](faq.md) | You have a question the other pages did not answer. |
| [Klipper CAN patches](../../klipper_patches/can/GUIDE.md) | Your CAN bus drops boards or never finds them, and you want the optional fixes. |

## Requirements

- A Klipper printer with Klipper and Moonraker installed and a `printer.cfg`.
- One AMS unit (or another OpenAMS-compatible MMU): a stock AMS behind a
  Cortex Bridge on USB, or an AMS with OpenAMS boards on CAN or USB-serial.
- Python 3.9 or newer in Klipper's environment, only for the optional
  [gco-routines](install.md#gco-routines) extension.
- `git`, `curl` and `sudo` on the printer host. `unzip` too, if you let the
  installer set up Spoolman.

## Check that it works

The fastest full check is the read-only bring-up ladder, from the OpenAMS
checkout:

```bash
python3 ~/openams/tools/agent/bringup_check.py --selftest
```

Success looks like every level `PASS` or `WARN`, a final
`BRINGUP pass=… fail=0 …`, and exit status 0. The install page's
[check that it works](install.md#check-that-it-works) section has more.

## Where to go next

- [Repository README](../../README.md) — what OpenAMS is, install flags, license.
- [docs/README.md](../README.md) — contracts, testing, UI API, klipper-mmu docs.
- [install.agents.md](../../install.agents.md) — the same install written for an AI agent.
- [oams_sample.cfg](../../oams_sample.cfg) — the annotated reference config.
- [UI API](../UI_API.md) — the contract a touchscreen front end codes against.
- [klipper-mmu docs](../../klipper_mmu/docs/) — MMU firmware, protocol, devices.