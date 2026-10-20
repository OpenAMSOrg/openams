# Using OpenAMS

## What it is, and why it exists

OpenAMS turns a spool change into one G-code command. The unit does the
filament motion; the macros do the cutter, the extruder and the nozzle. You do
not need to understand the pressure control loop. It does not need to understand
you either.

Every command below moves filament. Have a person at the printer for the first
run of each.

## Requirements

`oams.cfg` complete, Klipper started clean, the bring-up ladder passing, and
filament in at least one bay.

## Loading and unloading

A screen's load and unload buttons (HelixScreen, the OpenAMS Mainsail panel
and display) run the complete toolchange, `OPENAMS_LOAD` and `OPENAMS_UNLOAD`
([below](#the-complete-toolchange)): they cut, clean the nozzle and run your
hooks, as `T<n>` does. Set the cutter before you use them.

For a load or unload without the cutter, for example while you bring a printer
up, two commands coordinate the AMS with your printer's extruder macros:

```gcode
OAMSM_LOAD_TO_TOOLHEAD GROUP=T0
OAMSM_UNLOAD_FROM_TOOLHEAD
```

`OAMSM_LOAD_TO_TOOLHEAD` loads the group (its lane follows from the group), then
runs your `toolhead_load_macro` (default `LOAD_FILAMENT`) to capture and prime
it.

`OAMSM_UNLOAD_FROM_TOOLHEAD` arms the MCU-owned reverse follower, runs your
`toolhead_unload_macro` (default `UNLOAD_FILAMENT`), waits for its motion queue,
then starts the autonomous rewind. The macro must heat and eject the filament
past the extruder gears (about 100 mm); a stock 30 to 40 mm `UNLOAD_FILAMENT`
leaves the tip in the gears and the unload ends in a unit fault. Set
`toolhead_unload_macro: _OAMS_TOOLHEAD_EJECT` to use the macro shipped in
`oams_macros.cfg`. On a multi-lane printer, add `FPS=<lane>`. A
retract-macro failure disarms the reverse follower rather than rewinding against
an unknown toolhead state.

`OAMSM_LOAD_FILAMENT` and `OAMSM_UNLOAD_FILAMENT` are the lower-level blocks for
custom macros. They move the AMS only and leave the extruder to you.

If a load reaches the toolhead but the extruder macro fails, OpenAMS says so
(`AMS load reached the toolhead, but LOAD_FILAMENT failed: <reason>. ...`) and does
not unwind. Leaving the filament loaded with its follower armed is safer
than a blind rewind.

## The complete toolchange

`T0` through `T3` are defined by `oams_macros.cfg`; `T4` through `T19` are
commented out, waiting for your groups.

```gcode
T0
T1
```

Each `T<n>` calls `_TX GROUP=T<n>`, which runs the whole sequence: check the hotend
can extrude, home, cut the old filament, retract, rewind, load the new spool,
check the inlet, purge with `CLEAN_NOZZLE`, reload, check the outlet. On failure
it stops the follower, `PAUSE`, and raises a G-code error, so an SD print ends
through `on_error_gcode` instead of continuing without filament.

Point the slicer at these names: set its toolchange G-code to `T` and the tool
index. Its own multi-material moves (ramming, cooling moves, parking, loading,
toolchange retracts) have to be off: [slicer/](../../slicer/README.md) has
OrcaSlicer profiles and the settings for OrcaSlicer and PrusaSlicer. Test with a
two-color print before trusting a long one.

For more than four tools, add a group for each and uncomment the matching
`T<n>` macro at the bottom of `oams_macros.cfg`:

```ini
[gcode_macro T4]
gcode:
  _TX GROUP=T4
```

Call `T<n>` and `OPENAMS_LOAD` from ordinary G-code, **never** inside another
`START` block. They own their background routine, and nested routines are not
supported.

### From a UI

The versioned UI surface is the same sequence under different names, which is
what a front end should call:

```gcode
OPENAMS_LOAD GROUP=T0 SLOT=0
OPENAMS_UNLOAD
OAMSM_LOAD_FILAMENT_CANCEL
OAMSM_CLEAR_ERRORS
```

`SLOT=` is the slot id from the UI API status, not a bay number. `OPENAMS_LOAD`
rejects a malformed, unknown, unavailable or wrongly grouped slot before any
homing, cutting or unloading; check one without moving anything with
`OAMSM_VALIDATE_LOAD GROUP=T0 SLOT=0`.

`OPENAMS_UNLOAD` runs `STRICT=1`: on a sensor failure it raises instead of
pausing, because a UI request has no print to protect.

A client must read command names from the status, not hard-code them; see the
[UI API](../UI_API.md).

#### Stop

A display's Stop button stops the **whole** running sequence (homing, heating,
cut, retract, feed and your hooks), not only the firmware load or unload. It
cannot be a G-code command, which would wait behind the running macro, so it is
the Klipper webhook `openams/stop` (`POST /server/openams/stop` through
Moonraker, optional `lane`). The firmware load or unload is canceled at once,
the heat wait ends within half a second, and any other step ends at the next
boundary; a move already queued (a `G1`, the cutter, homing) finishes first.
The step that was running shows as failed with "Stopped by the user", and a
heater the sequence turned on goes back to where it was, unless a print is
running (a print's temperatures are never touched).

## Groups and runout

A filament group is the set of bays that can serve one `T<n>`. Put two spools of
the same color in one group and the toolchange never has to change material.

Runout is automatic. The host watches the extruder position against the
calibrated path length, and when the filament ends it:

1. stops the follower and coasts the remaining filament out of the buffer;
2. picks a ready spare from the same group that is not the bay that just ran out;
3. reloads it, prints normally.

Anything that stops it pauses the print with a reason: no spare bay in the
group, a spare bay with no PTFE length, `ptfe_length` still 0, or
`auto_reload_on_runout` off. The exact messages are in
[troubleshooting](troubleshooting.md#runout-and-calibration).

To turn auto-reload off, run `OAMSM_SET_OPTION NAME=auto_reload_on_runout
VALUE=False`; it writes the value back to `[oams_manager]` in `oams.cfg`.

### Editing groups at runtime

Group edits take effect immediately and are written straight to `oams.cfg` — no
`SAVE_CONFIG`, which cannot edit an included subfile. They survive a restart.

```gcode
OAMSM_CREATE_GROUP GROUP=T4
OAMSM_ASSIGN_BAY GROUP=T4 OAMS=oams1 BAY=0
OAMSM_UNASSIGN_BAY GROUP=T4 OAMS=oams1 BAY=0
OAMSM_DELETE_GROUP GROUP=T4
```

`OAMS=` here is the unit's name, not its index. A bay moves rather than copies:
assigning a bay that another group holds removes it from that group. A group
name may use letters, digits, `_`, `-` and `.`.

## Status and spool commands

The complete list, with arguments, is in
[reference](reference.md#g-code-command-reference).

`OAMSM_SELFTEST` is read-only and is the first thing to run when something is
wrong. It lists every FPS lane and unit, the negotiated protocol, the sensor
snapshot, the validated groups and the live lane state, then a last line reading
`RESULT: PASS` or `RESULT: WARN -> <reasons>`.

`OAMSM_STAGE` publishes a macro's plan and step (`PLAN=`, `NAME=`, `FAILED=1`,
`DONE=1`) so a UI can show where a toolchange is. It never raises.

- A spool may be linked to only one bay. `OAMSM_SET_BAY_SPOOL OAMS=oams1 BAY=0
  SPOOL=12` clears whichever other bay claims spool 12.
- The dryer and fault commands exist only where the firmware advertises them,
  and refuse otherwise.

### Units that read their own tags

Some units read and decode their spools' tags themselves, instead of exposing a
raw reader to OpenAMS. Such a unit reports, per bay, a decoded summary: the
filament code (for example a Bambu code like `GFB60`), the material, the color
and the nozzle range. OpenAMS shows it as the bay's tag with the format
`unit_summary`. It has **no UID**, so every spool of the same filament looks the
same.

- **When it is read.** If the unit can be told to read, the usual triggers apply:
  a spool inserted (once positioning ends), and at startup for a spool the unit
  has not read yet, according to `read_tag_on_insertion` and
  `read_tags_at_startup`. **A read may turn the spool**, so reads go one at a
  time, and a spool the unit has already read is not read again at startup.
  `OAMS_RFID_SCAN OAMS=<unit> BAY=<n>` reads one bay on demand (`BAY` is
  required). A spool with no tag, or a tag the unit could not read, simply shows
  no tag.
- **How it links to Spoolman.** Without a UID, OpenAMS never guesses between
  look-alike spools:
  1. If the bay is already linked to a spool whose material and color match the
     summary (and whose vendor is Bambu Lab, for a Bambu code), the link stays.
  2. Otherwise it looks for Spoolman spools with the same material and color
     that are not archived and not linked to another bay. If there is exactly
     one, it links it. A spool whose location is already this bay wins over
     others.
  3. If several match, the bay stays unlinked and the spools are offered as
     suggestions: pick one with Assign spool (or `OAMSM_SET_BAY_SPOOL`).
  4. If none match and `auto_create_spoolman_spools` is on, it creates the vendor,
     filament and spool, links it, and asks you to confirm how full the new spool
     is (it starts at the filament's default weight, 1000 g if none). With
     creation off, assign a spool by hand.
- **Spoolman offline.** A bay whose summary was linked before is linked again
  from the cache; a new one waits and is created once Spoolman is back.

### Units that store a bay's filament

Some of those units also **store a filament setting per bay**: what they report
for a spool whose tag they cannot read, such as a cardboard spool. On a printer
you would pick the filament for the tray; with OpenAMS, **Spoolman is the source
of truth**, and OpenAMS writes the linked spool's filament into the unit.

- **What is written.** The filament's material (upper-cased, at most 8
  characters), its color, and a nozzle range of its extruder temperature ±10 °C.
  The filament code comes from the filament's **`Filament code`** extra field in
  Spoolman (`filament_code`, created by OpenAMS; for example a Bambu filament
  code such as `GFA00`). Whatever the filament does not say keeps what the unit
  already has while the material is the same; otherwise a best-effort default
  by material is used: PLA `GFL99` 190-230 °C, PLA-CF `GFL98`, PETG `GFG99`
  220-260, ABS `GFB99` 240-280, ASA `GFB98` 240-280, TPU `GFU99` 200-240, PA
  `GFN99` 260-300, PC `GFC99` 260-300, PVA `GFS99` 190-230, anything else
  `GFL99`.
- **When.** When you link a spool to the bay (or link another one), and when
  you change the spool's material, color or temperature, whether in the spool
  editor (at once) or in Spoolman itself (within a minute). OpenAMS writes only
  when the unit reports something else, waits two seconds first, and tries at
  most three times per change. A spool that OpenAMS linked **from** the unit's
  own report is left as the unit has it until you change the spool.
- **Never** for a bay without a spool, or for a spool the unit identifies by
  its tag (a spool Spoolman knows by a tray UID or card UID).
- **Turn it off** with `OAMSM_SET_OPTION NAME=sync_unit_tray_info VALUE=0`
  (on by default).
- **By hand:** `OAMSM_SET_TRAY_INFO OAMS=<unit> BAY=<n>` writes the bay's
  linked spool now, `SPOOL=<id>` another spool, or give the values yourself:
  `OAMSM_SET_TRAY_INFO OAMS=oams1 BAY=0 CODE=GFL99 MATERIAL=PLA COLOR=FF0000
  MIN=190 MAX=230` (`CODE` and `COLOR` optional). The console reports when the
  unit has stored it. A unit that refuses the code or the material (too long,
  not plain ASCII) answers `invalid_field`; change the filament and it is tried
  again.
- **Status.** The Spoolman status of each bay shows `tray_sync`: `in_sync`,
  `pending`, `kept` (the unit's own setting is left alone) or `failed`, with
  the reason.

## Calibration

Two calibrations need a human. Each moves filament, and together they teach the
machine your PTFE tube and sensor placement. Run them one at a time, with the
printer idle, and **watch the machine**. The unit's firmware owns the motion
loop, but a mis-fed path still jams.

### 1. Filament path length (do this first)

```gcode
OAMS_CALIBRATE_PTFE_LENGTH OAMS=1 SPOOL=0
```

Put filament in the bay you name. The unit feeds and measures when the filament
reaches the expected sensor state, then writes `ptfe_length` (or
`ptfe_length_<bay>` on a four-bay unit) back into `oams.cfg` for you:

```
Calibrated PTFE length to <n> (saved to <path>).
```

Until this is done, a runout pauses instead of auto-reloading. If the bay is
loaded, run `OAMS_UNLOAD_SPOOL OAMS=1` first. A failure names its likely cause;
the messages are in [troubleshooting](troubleshooting.md#runout-and-calibration).

### 2. Hub sensors

```gcode
OAMS_CALIBRATE_HUB_HES OAMS=1 SPOOL=0
```

One bay at a time, with filament in that bay. This calibrates the range of a
single hub hall-effect sensor and writes `hub_hes_on` back. Unsupported units say
so: `Hub HES calibration is unsupported by this unit`.

Repeat for each bay.

### 3. Sensors, by hand

Not a calibration command. With no filament loaded the FPS should read near 0;
if the host and the unit disagree, flip `reversed:` as described in
[configure](configure.md#fps-orientation). The shipped `f1s_hes_on` and
`hub_hes_on` values work on most machines. If a bay reads as permanently empty
or full, adjust those lists in `oams.cfg` rather than recalibrating.

### 4. The first toolchange, watched

```gcode
OAMSM_LOAD_TO_TOOLHEAD GROUP=T0
OAMSM_UNLOAD_FROM_TOOLHEAD
```

Then one `T1` and back to `T0`, watching the cutter, the retract and the purge.
Set the cutter coordinates in `_oams_macro_variables` first; they are the values
most likely to be wrong. Once that is reliable,
`OAMS_TORTURE_TEST LOOPS=5` (every bay of unit 1, load and unload) and
`OAMS_TOOLCHANGE_TORTURE_TEST LOOPS=5` (`T0` to `T3`, with purging) repeat the
cycle. Stay in the room anyway.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `OpenAMS cannot heat <extruder>: minimum_extrude_temperature ...` | The floor is above the ceiling, so the flow has nowhere legal to heat | See [runtime errors](troubleshooting.md#runtime-errors) |
| `Unknown OpenAMS filament group <name>` | `T<n>` has no `[filament_group]` | Add the group |
| `Multiple FPS present; specify FPS=<name>` | Several lanes, no `FPS=` | Add `FPS=` |
| Runout pauses with `ptfe_length is not calibrated (0)` | Path length never calibrated | `OAMS_CALIBRATE_PTFE_LENGTH` |
| A UI load stops at `Filament not detected at toolhead extruder inlet` | The inlet switch never triggered | Clear the jam, retry |

The full list is in [troubleshooting](troubleshooting.md).

## Gotchas

- `oams_macros.cfg` is a link to the checkout, so an update refreshes it
  ([install](install.md#update)). Keep your changes in your own file.
- A toolchange or unload heats the nozzle itself, to the filament's printing
  temperature, and shows it as the `heat` step. Homing shows up as `home` and
  runs first, because a homing override can change the temperature.
- On a cold idle printer the nozzle goes cold again once the flow succeeds; a
  failed flow leaves it hot so the retry is quick.
- `CLEAN_NOZZLE` runs after the OpenAMS load and before the toolhead reload.
  The macros give it no `Z` clearance. Give it one.
- On a multi-toolhead printer, `T<n>` assumes the toolchanger has activated that
  tool first, so the toolhead sensors and cutter belong to it.

## Where to go next

- [Configuration reference](reference.md) — every option and command.
- [Troubleshooting](troubleshooting.md) — error text matched to causes.
- [FAQ](faq.md) — the questions that do not belong anywhere else.
- [UI API](../UI_API.md) — building a front end.
- [docs/contracts/C3_commands.md](../contracts/C3_commands.md) — the command contract.