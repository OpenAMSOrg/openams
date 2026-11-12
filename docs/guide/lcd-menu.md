# The printer's LCD menu

## What it is, and why

A printer with a Klipper display (a 12864 or 2004 LCD with a knob) can run
OpenAMS from the printer itself: load a tool, unload, retry a failed
toolchange, re-read a tag, calibrate, start the dryer. `oams_menu.cfg` adds an
**OpenAMS** entry near the top of the display's main menu, and a **Stop
OpenAMS** row at the very top while a sequence runs. It runs the same commands
as the screens and the Mainsail panel, so a load from the knob cuts, heats,
cleans the nozzle and runs your hooks like a `T<n>`.

The rule throughout is that a click acts. An entry never only "sets"
something that you then have to go and start somewhere else, and a confirm
appears only where a mistake costs something.

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
- **A picker** (Load tool, Unload, Retry, Start dryer and the others that need
  a target) works like an input: click it, turn the knob to the tool, spool,
  lane or unit, which shows with a `?`, and click again to run it. That second
  click is the confirmation. Turned back to the entry's own name, the second
  click runs nothing. Only a short click runs a picker.
- **A target that cannot run now** shows why instead of the `?`: `T4 busy`,
  `ams2 3 empty`, `T1 loaded`. Clicking it runs nothing, and says why in the
  console.
- **A setting** is one click: its name shows its state (`Autoreload: on`), and
  a click switches it.
- **Spools** are named by their unit and their place in it, counted from 1,
  as the console names them: `ams2 3` is ams2 Spool 3. **Tools** are your
  filament groups, in `oams.cfg` order.
- **Hidden entries.** An entry that cannot run at all is hidden. While a print
  is printing, every entry that moves filament is hidden, which leaves Stop,
  Dryer, Settings and Status. A paused print shows them again, for recovery.
  With stock Klipper the menu decides what to show each time you open a list;
  with [our Klipper](#on-openamsorgklipper-optional-improvements) a list
  follows the printer while it is open.

The menu reads the units, bays and groups from the live printer, so it fits
any number of units of any family: OpenAMS 1, AMS 1, AMS 2 Pro and AMS HT behind
a Cortex Bridge, or klipper-mmu units.

## Where it sits

`OpenAMS` takes the fourth place among Klipper's own main entries:

| Klipper | Idle | Printing | Paused |
|---|---|---|---|
| Stock | SD Card, **OpenAMS**, Control, … | Tune, SD Card, **OpenAMS**, … | SD Card, **OpenAMS**, … |
| OpenAMSOrg/klipper-optional-improvements | **OpenAMS**, SD Card, Control, … | Pause print, Cancel print, **OpenAMS**, Tune, … | Resume print, Cancel print, **OpenAMS**, … |

While a toolchange, load or unload runs, **Stop OpenAMS** comes first on
either Klipper. Open the menu, turn once and click: three actions.

Opening Stop OpenAMS is the stop, and it takes the row under `..` the moment
a sequence starts. With our Klipper the rows follow the printer, so a row you
saw a moment ago can move down: look at the row under the cursor before you
click. A stop by mistake during a print's toolchange pauses the print, and
RESUME finishes the toolchange.

## The entries

| Entry | What it runs | Shown when |
|---|---|---|
| **Stop OpenAMS** (main menu) | Opening it stops the whole running sequence (see Stop below) | a toolchange, load or unload is running |
| **Stop** | The same stop, inside OpenAMS | always |
| AMS status | `SET_DISPLAY_GROUP GROUP=oams_tiles`, then leaves the menu: the AMS status screen (below) | a 128x64 display, the screen not showing |
| Printer status | `SET_DISPLAY_GROUP` back to Klipper's own screen (your `[display] display_group`, else `_default_16x4`), then leaves the menu | the AMS status screen showing |
| Screen: auto | A picker: `OAMSM_SCREEN UNIT=<unit>` keeps the AMS status screen on one unit, `UNIT=AUTO` lets it follow the rules below | a 128x64 display, two or more units |
| **Load** › Load tool | `OAMSM_TOOLCHANGE GROUP=<tool>`, the toolchange `T<n>` runs | not printing, at least one group |
| **Load** › Load spool | `OPENAMS_LOAD GROUP=<its tool> SLOT=<slot>`: that spool, in a group | same |
| **Load** › Load, no cut | `OAMSM_LOAD_TO_TOOLHEAD GROUP=<tool>`: the AMS load and your `toolhead_load_macro`, into an empty toolhead | same |
| **Unload** › Unload | `OPENAMS_UNLOAD FPS=<lane>`: cut, retract, rewind | not printing, a lane loaded |
| **Unload** › Unload, no cut | `OAMSM_UNLOAD_FROM_TOOLHEAD FPS=<lane>`: your `toolhead_unload_macro`, then the rewind | same |
| **Unload** › Rewind | `OAMS_UNLOAD_SPOOL OAMS=<unit> SPOOL=<bay>`: an AMS unit rewinds its loaded spool, with no cut and no extruder motion; use it once the filament is clear of the extruder | same |
| Extrude 10mm | 10 mm forward at 4 mm/s (`M83`, `G1 E10 F240`), with the G-code state put back: the purge after a load | not printing; shows `Extrude (cold)` and runs nothing until the nozzle may extrude |
| Retry | `OAMSM_RETRY FPS=<lane>`: continue the failed toolchange or unload from where it stopped | not printing, a failed toolchange |
| Reset errors | `OAMSM_CLEAR_ERRORS` | not printing |
| Cancel load | `OAMSM_LOAD_FILAMENT_CANCEL` | a load in flight (a runout reload, for example) |
| Clear fault | `OAMS_CLEAR_FAULT OAMS=<unit>` | not printing, a unit fault that the unit can clear |
| **Tags** › Re-read tag | `OAMS_RFID_SCAN OAMS=<unit> BAY=<bay>` | not printing, a unit that reads tags on request |
| **Tags** › It is this slot | `OAMS_RFID_SCAN OAMS=<unit> BAY=<bay> ASSERT=1`: the tag in this bay's reader field is this bay's | a unit whose tag reads the host attributes (OpenAMS 1 readers) |
| **Calibrate** › Calibrate path | `OAMS_CALIBRATE_PTFE_LENGTH OAMS=<unit> SPOOL=<bay>` | not printing, a unit that calibrates its path |
| **Calibrate** › Calibrate hub | `OAMS_CALIBRATE_HUB_HES OAMS=<unit> SPOOL=<bay>` | not printing, a unit with hub sensors to calibrate |
| **Dryer** › Start dryer | A preset: `OAMS_DRYER_START OAMS=<unit> TARGET=<°C> DURATION=<s>` | a unit with a dryer |
| **Dryer** › Stop dryer | `OAMS_DRYER_STOP OAMS=<unit>` | same |
| **Dryer** › Custom › Dry temp, Dry time | Set the temperature and time Start custom uses | same |
| **Dryer** › Custom › Start custom | `OAMS_DRYER_START` with those | same |
| **Follower** › Follower on | `OAMSM_FOLLOWER ENABLE=1 DIRECTION=1 FPS=<lane>` | not printing |
| **Follower** › Follower off | `OAMSM_FOLLOWER ENABLE=0 DIRECTION=0`, every lane | not printing |
| **Settings** › Autoreload, Tag insert, Tag start, New spools, Apply PA, Sync trays | One click: `OAMSM_SET_OPTION NAME=<setting> VALUE=<the other one>` for `auto_reload_on_runout`, `read_tag_on_insertion`, `read_tags_at_startup`, `auto_create_spoolman_spools`, `apply_pa_on_load` and `sync_unit_tray_info` | always |
| **Status** › Self-test | `OAMSM_SELFTEST`; the answer is in the console | always |
| **Status** › Loaded tools | `OAMSM_CURRENT_LOADED_GROUP`; the answer is in the console | always |

Each command keeps its own guards: the menu only chooses the target. What each
one does is in [Using OpenAMS](using.md), and its exact arguments and refusals
in [the command contract](../contracts/C3_commands.md).

**Dryer.** Start dryer picks a preset and starts in one go: `PETG 65C 12h?`
with one dryer unit, `ams2 PETG 65C?` (the unit's name, its first four
characters) when there are several. Each unit type has its own list, Bambu
Lab's AMS drying presets (°C / hours idle, and °C while a print runs):

| Row | Material | AMS 2 Pro | AMS HT | During a print |
|---|---|---|---|---|
| PLA | PLA | 45 / 12 | 45 / 12 | 45 |
| PETG | PETG | 65 / 12 | 65 / 12 | 55 |
| TPU | TPU | 65 / 12 | 75 / 18 | 45 |
| ABS | ABS, ASA | 65 / 16 * | 80 / 8 | AMS 2 Pro 65, AMS HT 75 |
| PC | PC | 65 / 16 * | 80 / 8 | AMS 2 Pro 65, AMS HT 80 |
| PETCF | PET-CF | 65 / 24 * | 80 / 12 | AMS 2 Pro 65; idle only on the AMS HT |
| PA | PA, PAHT-CF | 65 / 24 * | 85 / 12 | AMS 2 Pro 65; idle only on the AMS HT |
| PVA | PVA | 65 / 36 * | 85 / 18 | AMS 2 Pro 65, AMS HT 70 |
| PLA-S | Support for PLA | 60 / 12 | 60 / 12 | 50 |

The rows marked * go beyond Bambu's list: Bambu calls ABS, ASA, PC, PET-CF, PA
and PVA not compatible with the AMS 2 Pro. OpenAMS offers them there at 65 °C,
the unit's maximum, for twice the AMS HT time, which may still not dry them
fully. PPS-CF and PPA-CF are offered on neither unit. An AMS 1 has no dryer
and offers no presets.

During a print a preset runs at its print-time temperature, the time
unchanged, and the row shows that temperature. A preset with none (PET-CF and
PA on the AMS HT) shows `idle only` and runs nothing. Every temperature is
kept within the unit's own limits: 45 to 65 °C on an AMS 2 Pro, 45 to 85 °C on
an AMS HT.

Bambu's conditions still apply: take out spools whose softening temperature
is below the set temperature; do not dry ABS together with PLA in one unit;
PVA, PPS-CF and PPA-CF may not dry fully even in an AMS HT; and the AMS 2 Pro
may not dry TPU fully, so drying TPU there is your choice.

Sources: Appendix I of
<https://wiki.bambulab.com/en/filament-acc/filament/dry-filament>, and the
`filament_dev_ams_drying_*` fields of Bambu Studio's
`resources/profiles/BBL/filament/*.json` (as of November 2026), read in
`src/slic3r/GUI/DeviceCore/DevUtilBackend.cpp` and `AMSDryControl.cpp`.

Change a unit type's list, or give one unit a list of its own, from your own
file. A unit takes `dryer_presets_<its name>` when you define one, otherwise
`dryer_presets_<its family>` (`ams2` for an AMS 2 Pro, `ams_ht` for an AMS
HT). Each row is `[row, °C, hours, °C during a print]`, with `None` for "not
during a print":

```ini
[gcode_macro _OAMS_MENU]
variable_dryer_presets_ams_ht: [["PLA", 50, 8, 45], ["PA", 85, 12, None]]
variable_dryer_presets_ams3: [["PETG", 70, 10, 55]]
```

For anything else, Custom keeps a temperature and a time (1 to 72 hours) until
Klipper restarts. A unit that cannot start shows why: `drying`, `no power`
(the dryer's adapter is unplugged), or `loaded` (the unit dries only with no
filament out).

**Stop.** A G-code command would wait behind the running macro, so Stop is
not one: opening the entry is the stop. OpenAMS handles that menu event the
way it handles the screens' Stop button (the `openams/stop` webhook): the
firmware load or unload is canceled at once, and the sequence ends at the next
step. A stop during a print's toolchange pauses the print, and RESUME finishes
the toolchange. The entry then shows **Stop sent**; click it to go back.

## Stock entries OpenAMS takes out

Klipper's own **Filament** list and the **Move E** rows of Control move the
extruder by a raw distance: "Load Fil." is 50 mm forward and "Unload Fil." is
50 mm back, with no cut and no AMS. On an OpenAMS printer such an unload leaves
the tip in the gears while the lane still counts as loaded. OpenAMS takes them
out of the menu when Klipper starts. Load, Unload and Extrude 10mm in the
OpenAMS entry replace them.

It leaves them alone when your own config defines one of them, or adds
entries under the Filament list: those stay as you made them. This is done by
OpenAMS when Klipper starts, not by a `[menu]` section in `oams_menu.cfg`.
Klipper merges two sections of one name, and a merge with your own entry of
another kind would keep Klipper from starting.

## The AMS status screen

**OpenAMS › AMS status** swaps the display's status screen for one about
the AMS: one unit at a time, with no printer temperatures or progress. Click
the knob to open the menu as usual; **OpenAMS › Printer status** goes back to
Klipper's own screen. It needs a 128x64 display (a 12864 such as the
Mini12864 or the RepRapDiscount Full Graphic); a 2004 character LCD cannot
draw it, and the entries stay hidden there. Klipper keeps the screen until
it restarts. To start on it, add `display_group: oams_tiles` to your
`[display]` section.

The top line names the unit: its letter (`A`, `B`, ... in unit order,
inverted), its name, the humidity and the temperature. While its dryer runs
it shows the dryer, the time left, the humidity and the temperature instead.
While a bay of it loads or unloads it shows the tool and the step, from
the first step on (`T5 homing 1/4`, `T5 heating 2/4`, `T5 feeding 4/4`, or
your macro's own label for a step), with an arrow for a load or an unload.
On a fault the whole line is inverted and names it (`!T5 feed failed`).

Below, each bay is a framed tile with three lines:

- a ring that fills clockwise with what is left on the spool, in eighths,
  and the bay's tool (`T5`; tools from `T10` show `10` to `15`). A dashed
  ring is an empty bay, a `?` a spool OpenAMS knows nothing about (no tag
  and no Spoolman link), an arrow a bay loading or unloading, a disc with
  `!` a bay a fault names;
- the material (`PLA`, `PETG`, `ABS`, `ASA`, `TPU`, `PA`, `PC`, `PVA`,
  `PET`, `HIPS`, `PCTG`, `PP`, `PPS`, `PEEK`; `PLA-CF` or `PLA+` shows as
  `PLA`, another name as its first three letters), or `?`;
- what is left, `0%` to `100%`, or `--` when unknown.

**The loaded bay is a solid black tile** with its text in reverse. A bay
being loaded switches between the solid and the framed tile every second. A
bay a fault names has a heavier frame.

Material and what is left come from Spoolman, through the
`openams_spoolman` component (the spool's material, and its remaining
weight over its initial weight); a spool from a tag the unit decodes itself
shows that tag's material. After a Klipper restart they come back on the
component's next pass.

**Which unit it shows:**

- during a print (printing or paused), the unit holding the loaded bay, with
  no paging;
- otherwise a unit with a fault, then one loading or unloading, counted
  from the first step (homing and heating come before the AMS moves; a load
  into an empty toolhead shows the unit holding the tool it loads);
- otherwise each unit for 5 seconds in turn;
- **Screen** in the OpenAMS menu, or `OAMSM_SCREEN UNIT=<unit>` from the
  console, keeps it on one unit; `OAMSM_SCREEN UNIT=AUTO` goes back to the
  rules. A restart goes back to the rules too.

The screen is `oams_screen.cfg`, generated by
`tools/lcd_screen/gen_oams_screen.py`; `./install.sh` links it beside
`oams_menu.cfg`, which includes it. A checkout updated without running
`./install.sh` again starts without it, and the entries stay hidden.

## A failed toolchange on the status screen

When a toolchange fails during a print and OpenAMS pauses the print, the
status screen's bottom line says so: `T4 failed: feed` (the tool and the step
that failed). The console has the full reason. The RESUME that finishes the
toolchange clears the line, and so does the end of the print. A message
something else put there since is left alone.

## On OpenAMSOrg/klipper-optional-improvements

Our Klipper brings the rest of the menu to the same rule. Nothing here needs
OpenAMS.

- **Pause print, Resume print and Cancel print** come first in the main menu
  while a print is printing or paused. Each leaves the menu after its click,
  so the status screen shows what happened. Cancel asks first:
  `Cancel print` › `Yes, cancel it`.
- **SD Card:** a file click opens `..` and `Print it`. "Print it" starts the
  print and goes to the status screen. There is no more "select, go back,
  Start printing". No file is offered while a print is printing or paused,
  and "Print it" checks again at the click, because selecting or starting a
  file during a pause throws the paused print away.
- **Nothing ends a paused print by accident.** Homing, levelling, the bed mesh,
  Move, Steppers off, Cooldown, the Filament list and the whole Setup list are
  hidden while a print is printing or paused. Klipper used to show them in a
  pause. "Start printing" is never shown in a pause: it resumed the file
  without RESUME.
- **Confirms** for Save config (shown only with changes pending; it restarts
  Klipper), Restart host and Restart FW.
- **Lists follow the printer** while they are open. When the entry under the
  cursor goes away, the cursor moves to `..`, never onto the entry that slid
  into its place. When a list's own gate closes while it is open (Setup, or
  a restart confirm in it, when a print starts), the menu leaves it for the
  deepest list still shown, cursor on `..`. The `[display]` option
  `menu_live_update: False` turns this off.
- **"While printing or paused"** means Klipper's print state says printing or
  paused, or the printer is busy (idle_timeout says Printing). A print
  streamed by OctoPrint counts too. Preheat is hidden like Cooldown.
- **No file is loaded on a paused print.** `M23` and `SDCARD_PRINT_FILE`
  are refused while a print is paused, from the display, a macro or a host:
  loading a file would throw the paused print away.
- **A long press never runs an entry.** It used to act like a click, so a long
  press on "Restart FW" restarted. Now it goes back one level, as the `..` row
  does, and leaves the menu from its top level. `..` stays the first row of
  every list, and the cursor still starts on it. An open input keeps its long
  press. The `[display]` option `menu_long_press_back: False` brings the old
  behaviour back.

## Hiding entries

`oams_menu.cfg` is a link to the checkout, so do not edit it: an update
replaces it. To hide a list, disable it from your own file, included after
`oams_menu.cfg` (in `printer.cfg`, below the OpenAMS includes):

```ini
[menu __main __openams __dryer]
type: disabled
```

That works for the lists (`__load`, `__unload`, `__tags`, `__calibrate`,
`__dryer`, `__follower`, `__settings`, `__status`), for the main-menu Stop
(`[menu __main __oams_stop]`) and for the whole OpenAMS entry
(`[menu __main __openams]`). A single picker cannot be disabled this way:
Klipper merges your section with the shipped one, and a disabled entry does
not read the picker's options, which Klipper then refuses. To drop the menu
entirely, run `./install.sh --no-lcd-menu`.

## Where to go next

- [Using OpenAMS](using.md) — what each command does.
- [Install](install.md) — the installer and its flags.
- [Troubleshooting](troubleshooting.md) — error text matched to causes.
