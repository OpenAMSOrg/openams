# The printer's LCD menu

## What it is, and why

A printer with a Klipper display (a 12864 or 2004 LCD with a knob) can run
OpenAMS from the printer itself: load a tool, unload, retry a failed
toolchange, re-read a tag, calibrate, start the dryer. `oams_menu.cfg` adds an
**OpenAMS** entry at the end of the display's main menu. It runs the same
commands as the screens and the Mainsail panel, so a load from the knob cuts,
heats, cleans the nozzle and runs your hooks like a `T<n>`.

## Requirements

- A `[display]` section in your Klipper config. A second display
  (`[display <name>]`) has no menu in Klipper, so it does not count.
- OpenAMS installed with `./install.sh`, which links `oams_menu.cfg` into the
  config directory and adds `[include oams_menu.cfg]` below
  `[include oams.cfg]` in `printer.cfg`.

The installer adds the include only when it finds `[display]`, in
`printer.cfg` or in a file it includes. Klipper reads `[menu]` sections only
through that display. On a printer without one, they would be sections nothing
reads, and Klipper refuses to start on those. If you remove the display later,
run `./install.sh` again: it takes the include out. `./install.sh --no-lcd-menu`
leaves the menu out on a printer that has a display, and removes the include
an earlier run added.

Your own `menu.cfg` keeps working beside it.

## Using it

- **A plain entry** runs when you click it.
- **A picker** (Load tool, Unload, Retry, Re-read tag and the others that need a
  target) works like an input: click it, turn the knob to the tool, spool, lane
  or unit, which shows with a `?`, and click again to run it. That second click
  is the confirmation. Turned back to the entry's own name, the second click runs
  nothing. Only a short click runs a picker; a long press does nothing.
- **A target that cannot run now** shows why instead of the `?`: `T4 busy`,
  `ams2 3 empty`, `T1 loaded`. Clicking it runs nothing, and says why in the
  console.
- **Spools** are named by their unit and their place in it, counted from 1,
  as the console names them: `ams2 3` is ams2 Spool 3. **Tools** are your
  filament groups, in `oams.cfg` order.
- **Hidden entries.** An entry that cannot run at all is hidden. While a print
  is printing, every entry that moves filament is hidden, which leaves Stop,
  Dryer, Settings and Status. A paused print shows them again, for recovery.
  The menu decides what to show each time you open a list.

The menu reads the units, bays and groups from the live printer, so it fits
any number of units of any family: OpenAMS 1, AMS 1, AMS 2 Pro and AMS HT behind
a Cortex Bridge, or klipper-mmu units.

## The entries

| Entry | What it runs | Shown when |
|---|---|---|
| **Stop** | Opening it stops the whole running sequence, as the screens' Stop button does (see below) | always |
| **Load** › Load tool | `OAMSM_TOOLCHANGE GROUP=<tool>`, the toolchange `T<n>` runs | not printing, at least one group |
| **Load** › Load spool | `OPENAMS_LOAD GROUP=<its tool> SLOT=<slot>`: that spool, in a group | same |
| **Load** › Load, no cut | `OAMSM_LOAD_TO_TOOLHEAD GROUP=<tool>`: the AMS load and your `toolhead_load_macro`, into an empty toolhead | same |
| **Unload** › Unload | `OPENAMS_UNLOAD FPS=<lane>`: cut, retract, rewind | not printing, a lane loaded |
| **Unload** › Unload, no cut | `OAMSM_UNLOAD_FROM_TOOLHEAD FPS=<lane>`: your `toolhead_unload_macro`, then the rewind | same |
| **Unload** › Rewind | `OAMS_UNLOAD_SPOOL OAMS=<unit> SPOOL=<bay>`: an AMS unit rewinds its loaded spool, with no cut and no extruder motion; use it once the filament is clear of the extruder | same |
| Retry | `OAMSM_RETRY FPS=<lane>`: continue the failed toolchange or unload from where it stopped | not printing, a failed toolchange |
| Reset errors | `OAMSM_CLEAR_ERRORS` | not printing |
| Cancel load | `OAMSM_LOAD_FILAMENT_CANCEL` | a load in flight (a runout reload, for example) |
| Clear fault | `OAMS_CLEAR_FAULT OAMS=<unit>` | not printing, a unit fault that the unit can clear |
| **Tags** › Re-read tag | `OAMS_RFID_SCAN OAMS=<unit> BAY=<bay>` | not printing, a unit that reads tags on request |
| **Tags** › It is this slot | `OAMS_RFID_SCAN OAMS=<unit> BAY=<bay> ASSERT=1`: the tag in this bay's reader field is this bay's | a unit whose tag reads the host attributes (OpenAMS 1 readers) |
| **Calibrate** › Calibrate path | `OAMS_CALIBRATE_PTFE_LENGTH OAMS=<unit> SPOOL=<bay>` | not printing, a unit that calibrates its path |
| **Calibrate** › Calibrate hub | `OAMS_CALIBRATE_HUB_HES OAMS=<unit> SPOOL=<bay>` | not printing, a unit with hub sensors to calibrate |
| **Dryer** › Dry temp, Dry time | Set the temperature and time the next start uses | a unit with a dryer |
| **Dryer** › Start dryer | `OAMS_DRYER_START OAMS=<unit> TARGET=<°C> DURATION=<s>` | same |
| **Dryer** › Stop dryer | `OAMS_DRYER_STOP OAMS=<unit>` | same |
| **Follower** › Follower on | `OAMSM_FOLLOWER ENABLE=1 DIRECTION=1 FPS=<lane>` | not printing |
| **Follower** › Follower off | `OAMSM_FOLLOWER ENABLE=0 DIRECTION=0`, every lane | not printing |
| **Settings** › Autoreload, Tag insert, Tag start, New spools, Apply PA, Sync trays | `OAMSM_SET_OPTION NAME=<setting> VALUE=0/1` for `auto_reload_on_runout`, `read_tag_on_insertion`, `read_tags_at_startup`, `auto_create_spoolman_spools`, `apply_pa_on_load` and `sync_unit_tray_info` | always |
| **Status** › Self-test | `OAMSM_SELFTEST`; the answer is in the console | always |
| **Status** › Loaded tools | `OAMSM_CURRENT_LOADED_GROUP`; the answer is in the console | always |

Each command keeps its own guards: the menu only chooses the target. What each
one does is in [Using OpenAMS](using.md), and its exact arguments and refusals
in [the command contract](../contracts/C3_commands.md).

**Dryer.** Set Dry temp and Dry time first; the menu keeps them until Klipper
restarts. Start dryer shows the unit with the values it will send
(`ams2 55C 4h?`), the temperature already brought within that unit's own
limits. A unit that cannot start shows why: `drying`, `no power` (the dryer's
adapter is unplugged), or `loaded` (the unit dries only with no filament out).

**Stop.** A G-code command would wait behind the running macro, so Stop is
not one: opening the entry is the stop. OpenAMS handles that menu event the
way it handles the screens' Stop button (the `openams/stop` webhook): the
firmware load or unload is canceled at once, and the sequence ends at the next
step. A stop during a print's toolchange pauses the print, and RESUME finishes
the toolchange. The entry then shows **Stop sent**; click it to go back.

## Hiding entries

`oams_menu.cfg` is a link to the checkout, so do not edit it: an update
replaces it. To hide a list, disable it from your own file, included after
`oams_menu.cfg` (in `printer.cfg`, below the OpenAMS includes):

```ini
[menu __main __openams __dryer]
type: disabled
```

That works for the lists (`__load`, `__unload`, `__tags`, `__calibrate`,
`__dryer`, `__follower`, `__settings`, `__status`) and for the whole OpenAMS
entry (`[menu __main __openams]`). A single picker cannot be disabled this
way: Klipper merges your section with the shipped one, and a disabled entry
does not read the picker's options, which Klipper then refuses. To drop the
menu entirely, run `./install.sh --no-lcd-menu`.

## Where to go next

- [Using OpenAMS](using.md) — what each command does.
- [Install](install.md) — the installer and its flags.
- [Troubleshooting](troubleshooting.md) — error text matched to causes.
