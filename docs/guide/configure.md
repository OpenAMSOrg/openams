# Configuring OpenAMS

## What this covers

Everything OpenAMS reads lives in one file, `oams.cfg`, in your Klipper config
directory. `printer.cfg` includes it with one line above `[printer]`, which the
installer adds:

```ini
[include oams.cfg]
```

This page covers the options you have to decide. Every other option, with its
default, is in the [configuration reference](reference.md); the annotated file
is [`oams_sample.cfg`](../../oams_sample.cfg).

Klipper will tell you exactly what is wrong. It will tell you in a log file
40,000 lines long.

## Requirements

[OpenAMS installed](install.md), the printer idle for each restart, and one CAN
UUID (or serial path) per board. A tag key is needed only for RFID.

## MCUs and CAN UUIDs

**With a Cortex Bridge** there is no CAN and no FPS board. Each AMS is a
serial MCU at `/dev/oams/<uid>`, the buffer is read through the AMS
(`[fps] source_oams:`), and the installer writes those sections for you.
[Set up a Cortex Bridge printer](cortex-bridge-setup.md) has the details:

```ini
[mcu ams2]
serial: /dev/oams/0a1b2c3d4e5f
restart_method: command
```

With OpenAMS boards, each AMS board and each FPS board is its own Klipper MCU.
Give each one a `[mcu ...]` section:

```ini
[mcu fps]
canbus_uuid: <your_unique_FPS_UUID>

[mcu oams_mcu1]
canbus_uuid: <your_unique_OAMS_MCU1_UUID>
```

Find the UUIDs with Klipper's own bus query:

```bash
~/klippy-env/bin/python ~/klipper/scripts/canbus_query.py can0
```

Each UUID is 12 hex digits. A placeholder left in place, or the same UUID in two
sections, stops Klipper with `Invalid canbus_uuid` or `Duplicate canbus_uuid`.
Use the short name from the header (`oams_mcu1`, `fps`) in `mcu:` and `pin:`. A
flaky bus is a CAN problem, not an OpenAMS one; the optional
[Klipper CAN patches guide](../../klipper_patches/can/GUIDE.md) covers it.

## `[oams <name>]`

One section per unit, named `oams1`, `oams2` and so on. A minimal one:

```ini
[oams oams1]
mcu: oams_mcu1
device_kind: auto
fps_upper_threshold: 0.7
fps_lower_threshold: 0.3
fps_is_reversed: true
f1s_hes_on: 0.1, 0.1, 0.1, 0.1
f1s_hes_is_above: false
hub_hes_on: 0.85, 0.85, 0.85, 0.85
hub_hes_is_above: true
ptfe_length: 0
oams_idx: 1
```

`oams_idx:` is the unit's number, unique across every `[oams]` and `[follower]`
(a follower calls it `unit_idx:`). The per-unit `OAMS_*` commands take it as
`OAMS=`; the group-editing commands take the unit's *name* (`OAMS=oams1`). The
sensor lists answer "is filament in this bay" and "has it reached the hub"; the
thresholds answer "how compressed is the buffer".

`f1s_hes_on:` and `hub_hes_on:` take one value or four. Any other count stops
Klipper with `f1s_hes_on requires one value (single-bay AMS HT) or four
comma-separated values`.

`ptfe_length: 0` is correct for a fresh machine. Run
`OAMS_CALIBRATE_PTFE_LENGTH` and OpenAMS writes the result back. Until then a
runout pauses the print instead of reloading the next spool.

### Hardware families

`device_kind:` is `auto` by default and resolves from what the unit's firmware
publishes. Set it explicitly during bring-up to fix the topology; the host warns
when the firmware disagrees. The values are `ams1` (4 bays, no active dryer),
`ams_ht` (1 bay, autonomous follower, one dryer channel, two vents) and `ams2`
(AMS 2 Pro, 4 bays, two heating zones). One installation can mix all three.

`fps:` names the lane a unit feeds. Leave it out with one `[fps]`. Set it on every
unit when you define several named lanes (IDEX, multi-tool). Every bay in one
filament group must sit on the same lane.

## `[fps]`

The FPS is the buffer and pressure sensor between the units and the toolhead.
It reads 0.0 (no pressure) to 1.0 (fully compressed).

```ini
[fps]
pin: fps:PA2
reversed: false
extruder: extruder
```

Give exactly one source, `pin:` or `source_oams:`. Neither or both fails with
`[fps]: specify exactly one of 'pin' or 'source_oams'`.

A unit that reports its own buffer reading (an AMS behind a Cortex Bridge)
needs no wired pin: `source_oams: oams1` mirrors that reading. For several lanes, name them
(`[fps fps1]`, `[fps fps2]`, each with its own `pin:` and `extruder:`) and point
each unit at one with `fps:`; the sample file has an example.

### Host-confirmed arrival from a relayed FPS

A unit that cannot sense toolhead arrival itself (C1
`AMS_CAP_HOST_ARRIVAL_CONFIRM=1`) needs the host to confirm it. Normally that
comes from an `[fps]` lane with a `pin:`. Some units instead relay a
toolhead-side FPS/buffer sensor and report it as their `fps_value`; such a unit
advertises `AMS_CAP_FPS_TOOLHEAD=1`, and its lane is configured with
`source_oams:`:

```ini
[fps fps1]
source_oams: oams1
arrival_threshold: 0.65
arrival_debounce: 0.3
extruder: extruder
```

The load is confirmed once the relayed value has been seen below
`arrival_threshold` and then holds at or above it for `arrival_debounce`
seconds. For a `source_oams` unit without the capability the value is its own
measurement, no arrival is confirmed from it, and loads on that lane are
refused as before.

The default of 0.65 sits below where a typical buffer sensor saturates: with
the slide bottomed out, a relayed FPS often reads only 0.74 to 0.77, never
1.0, so a higher threshold never confirms and the unit keeps feeding until its
motion timeout. If a load times out and the log says the reading stayed just
below `arrival_threshold`, lower it under the peak the warning names.

### FPS orientation

The host reads the buffer through `pin:`; the unit reads the same buffer itself.
If the two are inverses (0.21 against 0.82, adding to about 1), loading still
works but every UI draws the pressure bar backwards. Flip `reversed:` in `[fps]`.
The manager warns once it has seen ten such samples on an idle, loaded lane.
`fps_is_reversed` in `[oams]` is the unit's own, separate setting.

## Filament groups

A filament group is a named list of bays. The slicer's `T0` loads group `T0`.

```ini
[filament_group T0]
group: oams1-0

[filament_group T1]
group: oams1-1
```

`group:` lists `<unit-name>-<bay>` entries, so `group: oams1-0, oams2-1` spans
two units. Startup enforces three rules:

- Names are unique: `Duplicate filament_group 'T1'; names must be unique.`
- A bay is in one group:
  `bay oams1-0 is in both 'T0' and 'T1'; a bay may belong to only one filament_group.`
- A group's bays share one FPS lane.

For a fifth tool, add a group and uncomment `T4` at the bottom of
`oams_macros.cfg`. The file has `T4` to `T19` ready; past that, copy the pattern.

## `[gcode_macro _oams_macro_variables]`

Per-printer values the toolchange macros read. Most machines change only the
cutter coordinates (`pre_cut_x`/`_y`, `post_cut_x`/`_y`, `cut_x`/`_y`) and the
speeds; the full table is in
[reference](reference.md#gcode_macro-_oams_macro_variables). Three decide what
temperature a toolchange heats to:

- `minimum_extrude_temperature: 0` relies on Klipper's own `min_extrude_temp`.
  Raise it if that is lower than your filament needs. It is also the floor: a
  flow never heats below it.
- `unknown_material_temperature: 235` is what a flow heats to when no
  temperature is known for the bay. Lower it if your hotend cannot hold it.
- `max_operation_temperature: 0` adds no ceiling. Set `240` on a PTFE-lined
  hotend; the heater's own `max_temp - 5` applies either way.

And two decide whether a toolchange checks anything:

- `fs_extruder_in` and `fs_extruder_out` default to `False`. Set one to `True`,
  and uncomment its `[filament_switch_sensor ...]` section, if your extruder has
  that switch. A failed check then pauses the toolchange.

## RFID readers and the tag key

Readers need mainboard firmware 2.0.25 or newer. One `[mfrc522 openams]` section
owns both readers on one software-SPI bus.

```ini
[bambu_lab_tag_processor]
key: <32_HEX_CHARACTERS>

[mfrc522 openams]
oams: 1
cs_pin_a: oams_mcu1:PB3
reset_pin_a: oams_mcu1:PD2
cs_pin_b: oams_mcu1:PB2
reset_pin_b: oams_mcu1:PB0
spi_software_sclk_pin: oams_mcu1:PA8
spi_software_mosi_pin: oams_mcu1:PA9
spi_software_miso_pin: oams_mcu1:PA10
spi_speed: 100000
```

`oams:` is the unit's `oams_idx`. Both readers share one bus and **must be on one
MCU**; pins on two MCUs fail with `both OpenAMS RFID readers must use one MCU`.
Readers cover bays `0,1` and `2,3` by default; `bays_a:` and `bays_b:` change
that.

`key:` is the Bambu Lab master key, exactly 32 hex characters; it derives each
tag's MIFARE sector keys. Anything else fails with
`bambu_lab_tag_processor key must contain exactly 16 hexadecimal bytes`. The
repository ships a placeholder, never a key; keep yours in your own config.

## `[oams_manager]`

The manager owns topology validation, the runout monitor and the load callbacks.
Nothing here is required. Two options are easy to get wrong:

- `toolhead_load_macro:` and `toolhead_unload_macro:` (default `LOAD_FILAMENT`
  and `UNLOAD_FILAMENT`) are extruder-only callbacks. They must not call an
  `OAMSM_*` command, and the name must be one macro (letters, digits and
  underscores), or Klipper says `toolhead_load_macro must name one non-OAMSM
  G-code macro (letters, digits, underscores only)`.

  The unload macro must heat the extruder and eject the filament past the
  extruder gears (about 100 mm) before the unit rewinds it. A stock
  `UNLOAD_FILAMENT` retracts 30 to 40 mm, which leaves the tip in the gears and
  ends the unload in a unit fault. `oams_macros.cfg` ships
  `_OAMS_TOOLHEAD_EJECT` (heat to the loaded lane's `filament_temp_c`, soften,
  retract 100 mm in 25 mm moves; tune `variable_eject_distance` and friends);
  set `toolhead_unload_macro: _OAMS_TOOLHEAD_EJECT` to use it.

- `openams_config_path:` (default `oams.cfg` beside `printer.cfg`) is where group
  edits and calibration results are written. Set it when your `[oams]` and
  `[filament_group]` sections live elsewhere, or writebacks land in the wrong
  file.

The remaining options and the six policy switches are in
[reference](reference.md#oams_manager).

## Customizing the toolchange

Two printers, two toolchanges. Some of the work belongs to your machine, not
to OpenAMS, so there are places to put it — in **your own file**, never in
`oams_macros.cfg`, which is a link to the checkout and changes with every update. `oams.cfg` is as good a place as
any, and it is yours.

**The cutter.** Define `OAMS_CUT` and it makes the cut instead of the
coordinates in `_oams_macro_variables`; the progress bar still says `cut`:

```ini
[gcode_macro OAMS_CUT]
gcode:
    {% set v = printer["gcode_macro _oams_macro_variables"] %}
    G90
    G0 X{v.pre_cut_x} Y{v.pre_cut_y} F{v.pre_cut_speed}
    G0 X{v.cut_x} Y{v.cut_y} F{v.cut_speed}
    G0 X{v.post_cut_x} Y{v.post_cut_y} F{v.pre_cut_speed}
    M400
```

**The nozzle.** `CLEAN_NOZZLE` is your macro too: the toolchange calls it
inside the load step to wipe or brush the nozzle.

**Four hook points** cover everything else — a park move, a brush, a purge. A
hook is an ordinary macro that the toolchange calls at a step boundary, and
OpenAMS ships none of them, so a printer only has the ones it writes:

| Hook | Runs |
|---|---|
| `_OAMS_HOOK_BEFORE_HEAT` | before the heat step, or where it would be |
| `_OAMS_HOOK_AFTER_CUT` | after the cut, before the retract |
| `_OAMS_HOOK_AFTER_RETRACT` | after the retract, before the load (a swap only) |
| `_OAMS_HOOK_AFTER_FEED` | after the load succeeded |

Give a hook `variable_step` and it becomes a step of its own on the progress
bar, in the right place, with `variable_label` as its text. Klipper reads a
`variable_` option as a literal, so quote the values:

```ini
[gcode_macro _OAMS_HOOK_AFTER_CUT]
variable_step: "wipe"
variable_label: "Wiping the nozzle"
gcode:
    G0 X240 Y100 F3000
```

Leave those two options out and the hook just runs inside the step around it,
with nothing added to the display. A hook whose point a flow does not pass
never runs: a load into an empty toolhead has no retract, so
`_OAMS_HOOK_AFTER_RETRACT` is not part of it. A plan holds 8 steps at most, so
if your hooks would need more, the last ones stay off the display (they still
run) and the console names them. An error inside a hook fails its step, like
any other error during a toolchange.

`config_audit.py` tells you when a macro is *your* customization and where it
belongs (`E_MACRO_CUSTOMIZED`); see
[troubleshooting](troubleshooting.md#2-config_auditpy--what-is-wrong-in-the-config).

## Optional hardware

- **Ambient sensor.** A bare `[hdc1080]` section plus a `[temperature_sensor ...]`
  with `sensor_type: HDC1080` and `i2c_address: 64`. With several sensors, pick
  one with `environment_sensor:` in `[oams]`.
- **Inline follower.** A `[follower]` is a stepper on a spare stepper port, acting
  as a single-bay unit (`<name>-0` in groups). It needs follower-enabled Klipper
  firmware; see [FOLLOWER_PROTOCOL.md](../../FOLLOWER_PROTOCOL.md).
  `path_length: 0` means uncalibrated, so runout pauses.

## Check that it works

```bash
python3 ~/openams/tools/agent/config_audit.py --config-dir ~/printer_data/config
python3 ~/openams/tools/agent/bringup_check.py --selftest
```

Success is a final `AUDIT errors=0 ...` line from the first (`E_PLACEHOLDER`
findings are normal until you replace the UUIDs and key), Klipper started clean,
and a `BRINGUP ... fail=0` line from the second. Both are described in
[troubleshooting](troubleshooting.md#the-three-read-only-checks).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Invalid canbus_uuid` | Placeholder or typo | Use the UUIDs from `canbus_query.py` |
| `Duplicate filament_group 'T1'; names must be unique.` | Two sections, one name | Rename or remove one |
| Group edits land in the wrong file | `[oams]` sections are not in `oams.cfg` | Set `openams_config_path:` |

Every startup error is in [troubleshooting](troubleshooting.md#klipper-startup-errors).

## Update and uninstall

An update never rewrites `oams.cfg`, and new options have defaults. Uninstalling
asks before it deletes the file ([install](install.md#uninstall)).

## Gotchas

- `SAVE_CONFIG` cannot edit an included file. OpenAMS writes group edits and
  calibration results into `oams.cfg` itself, so keep that file writable.
- `oams_macros.cfg` is a link to the checkout and updates with it. Do not edit
  it; use the hooks ([above](#customizing-the-toolchange)).

## Where to go next

- [Configuration reference](reference.md) — every option, with defaults.
- [Using OpenAMS](using.md) — load, unload, toolchange, calibrate.
- [Troubleshooting](troubleshooting.md) — every config error and what to do.
- [docs/stm32f072.md](../stm32f072.md) — config differences on the original mainboard.
