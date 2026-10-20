# Configuration reference

The full option tables for `oams.cfg`. [Configure](configure.md) says which
values you need to set; [`oams_sample.cfg`](../../oams_sample.cfg) is the
annotated file.

## `[oams <name>]`

One section per unit. Required unless marked otherwise.

| Option | Required | Default | Meaning |
|---|---|---|---|
| `mcu:` | yes | — | Which `[mcu ...]` section carries this unit |
| `oams_idx:` | yes | — | Unit number, unique across every `[oams]` and `[follower]` (`unit_idx:`) |
| `fps_upper_threshold:` | yes | — | FPS HES value at which the hub motor runs at full speed |
| `fps_lower_threshold:` | yes | — | FPS HES value below which the hub motor stops |
| `fps_is_reversed:` | yes | — | Whether this unit's own FPS reading is inverted |
| `f1s_hes_on:` | yes | — | First-stage sensor thresholds: one value or four |
| `f1s_hes_is_above:` | yes | — | Whether the switch reads ON above the threshold |
| `hub_hes_on:` | yes | — | Hub sensor thresholds: one value or four |
| `hub_hes_is_above:` | yes | — | Whether the switch reads ON above the threshold |
| `ptfe_length:` | yes | — | Filament path length; `0` until you run `OAMS_CALIBRATE_PTFE_LENGTH` |
| `device_kind:` | no | `auto` | `auto`, `ams1`, `ams_ht` or `ams2` |
| `fps:` | no | sole lane | Which FPS lane this unit feeds |
| `environment_sensor:` | no | sole HDC1080 | Host sensor object for ambient temperature and humidity |
| `fps_target:` | no | `0.5` | Pressure set point, between the two thresholds |
| `kp:` / `ki:` / `kd:` | no | `6.0` / `0.0` / `0.0` | Hub motor PID |
| `current_target:` | no | `0.3` | Rewind current target, 0.1 to 0.4 |
| `current_kp:` / `current_ki:` / `current_kd:` | no | `0.375` / `0.0` / `0.0` | Rewind current PID |
| `ptfe_length_<bay>:` | no | shared `ptfe_length` | Per-bay path length override |
| `spool_id_<bay>:` | no | — | Spoolman spool linked to that bay |
| `link_source_<bay>:` | no | `manual` | `tag` or `manual`; written by the plugin |

A threshold list must hold one value or four. Anything else stops Klipper with
`<option> requires one value (single-bay AMS HT) or four comma-separated values`.

`device_kind: auto` resolves from the capabilities the unit's firmware
publishes. An explicit value fixes the topology and is validated against those
firmware facts. The valid set also includes the klipper-mmu device families; an
unknown value reports `unknown device_kind '<value>'`.

## Hardware families

`ams1` (4 bays, no active dryer), `ams_ht` (1 bay, autonomous follower, one
dryer channel, two vents) and `ams2` (AMS 2 Pro, 4 bays, two heating zones) are
described in [configure](configure.md#hardware-families). The `openams/status`
and `openams/topology` webhooks publish a versioned capability model. Build a UI
from `capabilities` and `supported_actions`, never from product-name checks; see
the [UI API](../UI_API.md).

## `[fps]`

Exactly one source: `pin:` or `source_oams:`.

| Option | Default | Meaning |
|---|---|---|
| `pin:` | — | The FPS MCU's ADC pin, for a directly wired board |
| `source_oams:` | — | Mirror a unit's own buffer reading instead (Cortex Bridge) |
| `reversed:` | `false` | Invert the reading |
| `extruder:` | `extruder` | Which toolhead extruder this lane feeds |
| `sample_count:` | `5` | ADC samples per callback |
| `sample_time:` | `0.005` | ADC sample interval |
| `report_time:` | `0.100` | ADC report interval |
| `arrival_threshold:` | `0.65` | Reading at which a load is confirmed arrived, for a unit whose firmware cannot sense it (needs `pin:`, or `source_oams:` of a unit reporting `AMS_CAP_FPS_TOOLHEAD`; see C1 §7.2) |
| `arrival_debounce:` | `0.3` | Seconds the reading must stay at or above the threshold |
| `max_speed:`, `accel:`, `set_point:` | `300.0`, `0.0`, `0.5` | Reserved, kept for config compatibility |
| `use_kalico:` | `false` | Accepted and ignored; the plugin detects the ADC API |

An unknown `extruder:` is reported at connect:
`[fps]: extruder '<name>' not found; check the 'extruder:' option`.

## `[oams_manager]`

Nothing here is required.

| Option | Default | Meaning |
|---|---|---|
| `reload_before_toolhead_distance:` | `0.0` | Extra margin (mm) before the runout auto-reload point |
| `monitor_interval:` | `1.0` | Runout/health monitor period |
| `positioning_wait:` | `10.0` | Seconds to let a unit finish positioning a freshly inserted spool (0 disables) |
| `toolhead_load_macro:` | `LOAD_FILAMENT` | Extruder-only capture/prime callback of `OAMSM_LOAD_TO_TOOLHEAD` |
| `toolhead_unload_macro:` | `UNLOAD_FILAMENT` | Extruder-only retract callback of `OAMSM_UNLOAD_FROM_TOOLHEAD`; must heat and eject past the extruder gears (about 100 mm), see `_OAMS_TOOLHEAD_EJECT` in `oams_macros.cfg` |
| `openams_config_path:` | `oams.cfg` beside `printer.cfg` | Where group edits and calibration results are written |

### Policy switches

Read from `[oams_manager]` at startup and changed at runtime by
`OAMSM_SET_OPTION NAME=<setting> VALUE=<1|0|true|false|on|off>`, which writes the
value back to that section. Defaults: `read_tag_on_insertion` `True`,
`read_tags_at_startup` `True`, `auto_reload_on_runout` `True`,
`auto_create_spoolman_spools` `True`, `apply_pa_on_load` `False`,
`sync_unit_tray_info` `True` (write each bay's linked Spoolman spool into a unit
that stores a per-bay filament setting). An unknown name gives
`Unknown OpenAMS setting '<name>'`.

## `[gcode_macro _oams_macro_variables]`

Per-printer values the toolchange macros read. Sample defaults, from
`oams_sample.cfg`:

| Variable | Sample | Meaning |
|---|---|---|
| `pre_cut_x` / `pre_cut_y` | `20` / `355` | Position before the cut move |
| `post_cut_x` / `post_cut_y` | `81` / `355` | Position after the cut move |
| `pre_cut_speed` | `5000` | Travel speed to the cut position |
| `cut_x` / `cut_y` | `2` / `355` | Where the cutter actuates |
| `cut_speed` | `1000` | Cutting speed |
| `hotend_meltzone_compensation` | `0` | Extra melt-zone length |
| `retract_length` | `5` | Retract at toolchange |
| `extrusion_reload_length` | `26.2` | Reload extrusion |
| `extrusion_unload_length` | `32` | Retract after the cut |
| `reload_speed` | `1000` | Reload speed |
| `unload_speed` | `1000` | Speed of the post-cut retract |
| `extrusion_unload_additional_length` | `250` | Extra retract before the unit rewinds |
| `additional_unload_speed` | `5000` | Speed of that extra retract |
| `minimum_extrude_temperature` | `0` | Lowest hotend temperature a flow heats to, and at which cut and retract may run |
| `unknown_material_temperature` | `235` | What a flow heats to when no bay temperature is known |
| `max_operation_temperature` | `0` | Extra ceiling on what a flow heats to, below the heater's `max_temp - 5` (`0` adds none) |
| `fs_extruder_in` / `fs_extruder_out` | `False` | Whether those filament switches exist |

The four unload-related variables and the three temperature variables have those
defaults when the line is missing, so an older config keeps working.

The sample's toolhead values suit one printer. For starting values measured
in your toolhead's CAD (`retract_length`, `extrusion_unload_length`,
`extrusion_reload_length`), see the [toolhead database](../../toolheads/README.md):
`python3 tools/toolhead_defaults.py list`.

## `[mfrc522 <name>]`

One driver owns both readers on one software-SPI bus; needs mainboard firmware
2.0.25 or newer.

| Option | Default | Meaning |
|---|---|---|
| `oams:` | — | The unit's `oams_idx` |
| `cs_pin_a:` / `cs_pin_b:` | — | Chip select, reader A and B |
| `reset_pin_a:` / `reset_pin_b:` | — | Reset / NPD pin, reader A and B |
| `spi_software_sclk_pin:` | — | Shared clock |
| `spi_software_mosi_pin:` | — | Shared MOSI |
| `spi_software_miso_pin:` | — | Shared MISO |
| `spi_speed:` | `100000` | Bus speed |
| `bays_a:` / `bays_b:` | `0,1` / `2,3` | Which bays each reader covers |
| `read_block:` | — | Authenticated MIFARE block read |
| `key:` / `key_b:` | `FFFFFFFFFFFF` / `false` | Per-reader sector key, and whether it is key B |
| `poll_interval:` | `0.5` | Seconds between polls (minimum 0.1) |

Both readers must share one MCU (`both OpenAMS RFID readers must use one MCU`),
and two drivers cannot claim the same unit (`duplicate MFRC522 driver for
OAMS=<n>`).

## `[follower <name>]`

An inline single-bay unit on a spare stepper port. All control tunables
(`fps_target`, `kp`, `ki`, `kd`, `max_speed`, `accel`, `load_speed`,
`unload_speed`, …) have working defaults; read them in
[`src/follower.py`](../../src/follower.py) and
[FOLLOWER_PROTOCOL.md](../../FOLLOWER_PROTOCOL.md).

| Option | Default | Meaning |
|---|---|---|
| `type:` | `stepper` | `stepper` today; `bldc` reserved |
| `mcu:` | `mcu` | MCU with the spare stepper port |
| `unit_idx:` | — | Unique across every `[oams]` and `[follower]` |
| `fps:` | sole lane | Required only when several lanes exist |
| `step_pin:` / `dir_pin:` / `enable_pin:` | — | Stepper wiring |
| `rotation_distance:` | — | Stepper geometry |
| `microsteps:` | `16` | Must be a power of two, 256 or less |
| `gear_ratio:` | — | Optional |
| `pre_switch_pin:` | — | Filament staged at follower entry ("ready") |
| `post_switch_pin:` | — | Filament fed past the follower ("loaded") |
| `path_length:` | `0` | mm, post switch to extruder gears; 0 = uncalibrated |
| `uart_pin:`, `run_current:` | — | Embedded TMC bring-up; omit `uart_pin` to run the driver standalone |

An invalid `microsteps:` gives `[<section>] microsteps must be a power of two <= 256`.

## `[hdc1080]` and the ambient sensor

The bare `[hdc1080]` section must exist or `sensor_type: HDC1080` is an unknown
type. The sensor itself is a `[temperature_sensor ...]` section. The sample uses
`i2c_address: 64` (the address as given by TI), `i2c_mcu: oams_mcu1`,
`i2c_bus: i2c0`, `i2c_speed: 200000`, `temp_offset` and `humidity_offset` of
`0.0`, and `temp_resolution` and `humidity_resolution` of `14` bits.

## G-code command reference

Every command below is registered by the host plugin. The full contract, with
parameters and payload schemas, is [C3](../contracts/C3_commands.md). `OAMS=` takes
the unit's `oams_idx`, except in the four group and spool-link commands and
`OAMSM_SET_TRAY_INFO` below, which take its name.

### `oams_manager`

| Command | Arguments |
|---|---|
| `OAMSM_SELFTEST` | none (read-only) |
| `OAMSM_LOAD_TO_TOOLHEAD` | `GROUP=<name>` |
| `OAMSM_UNLOAD_FROM_TOOLHEAD` | `FPS=<lane>` (optional) |
| `OAMSM_LOAD_FILAMENT` | `GROUP=<name>`, `SLOT=<n>` (optional) |
| `OAMSM_UNLOAD_FILAMENT` | `FPS=<lane>` (optional) |
| `OAMSM_VALIDATE_LOAD` | `GROUP=<name>`, `SLOT=<n>` |
| `OAMSM_LOAD_FILAMENT_CANCEL` | `FPS=<lane>` (optional) |
| `OAMSM_CLEAR_ERRORS` | none |
| `OAMSM_CURRENT_LOADED_GROUP` | none |
| `OAMSM_STAGE` | exactly one of `PLAN=`, `NAME=`, `FAILED=1`, `DONE=1`; optional `FPS=` or `GROUP=` |
| `OAMSM_FOLLOWER` | `ENABLE=<0\|1>`, `DIRECTION=<0\|1>` |
| `OAMSM_CREATE_GROUP` | `GROUP=<name>` |
| `OAMSM_DELETE_GROUP` | `GROUP=<name>` |
| `OAMSM_ASSIGN_BAY` | `GROUP=<name> OAMS=<unit name, e.g. oams1> BAY=<0-3>` |
| `OAMSM_UNASSIGN_BAY` | `GROUP=<name> OAMS=<unit name> BAY=<0-3>` |
| `OAMSM_SET_BAY_SPOOL` | `OAMS=<unit name> BAY=<0-3> SPOOL=<id> SOURCE=tag\|manual` (`SOURCE` optional, default `manual`) |
| `OAMSM_CLEAR_BAY_SPOOL` | `OAMS=<unit name> BAY=<0-3>` |
| `OAMSM_SET_TRAY_INFO` | `OAMS=<unit name> BAY=<0-3>`, then `SPOOL=<id>` (optional, default the linked spool) or `MATERIAL=<text> MIN=<C> MAX=<C>` with optional `CODE=<code>` (default: the material's generic code) and `COLOR=<RRGGBB[AA]>`; units with a stored per-bay filament setting only |
| `OAMSM_SET_OPTION` | `NAME=<setting> VALUE=<value>` |

### Per unit, `OAMS=<index>`

| Command | Arguments |
|---|---|
| `OAMS_LOAD_SPOOL` | `SPOOL=<bay>` |
| `OAMS_UNLOAD_SPOOL` | `SPOOL=<bay>` (optional; refused if another bay is loaded) |
| `OAMS_FOLLOWER` | `ENABLE=<0\|1>`, `DIRECTION=<0\|1>` |
| `OAMS_CALIBRATE_HUB_HES` | `SPOOL=<bay>` |
| `OAMS_CALIBRATE_PTFE_LENGTH` | `SPOOL=<bay>` |
| `OAMS_PID_SET` | `P=`, `I=`, `D=`, `TARGET=` (optional) |
| `OAMS_CURRENT_PID_SET` | `P=`, `I=`, `D=`, `TARGET=` (optional) |
| `OAMS_PID_AUTOTUNE` | none; always answers that autotune is unsupported |
| `OAMS_DRYER_START` | `TARGET=<C>`, `DURATION=<s>` (default 3600), `HOLD_AFTER=<0\|1>`, `ROTATE=<0\|1>` |
| `OAMS_DRYER_STOP` | none |
| `OAMS_CLEAR_FAULT` | none |
| `OAMS_RFID_SCAN` | `BAY=<bay>`: asks a unit that reads its own tags to read that bay ([using](using.md#units-that-read-their-own-tags)) |
| `OAMSM_RFID_READ` | `OAMS=<index> RFID_CARD=<0\|1>` |
| `MFRC522_QUERY` | `READER=rfid_a\|rfid_b` (no `OAMS=`) |

### klipper-mmu operations, `OAMS=<index> SPOOL=<bay>`

| Command | Meaning |
|---|---|
| `OAMS_PRELOAD` | Stage a bay's filament to the hub |
| `OAMS_UNPRELOAD` | Release a bay's staged filament |
| `OAMS_EJECT` | Eject a bay's filament out of the unit |
| `OAMS_HOME` | Home the selector, on selector builds only (no `SPOOL=`) |

A unit that cannot do the operation refuses with a reason, not an
unknown-command error.

### Inline follower, `FOLLOWER=<index>`

| Command | Meaning |
|---|---|
| `FOLLOWER_LOAD_SPOOL` | Load filament through this follower |
| `FOLLOWER_UNLOAD_SPOOL` | Unload filament from this follower |
| `FOLLOWER_SET` | Enable or disable the firmware follow loop |

## Where to go next

- [Configure](configure.md) — which values to set.
- [Using OpenAMS](using.md) — what to run, and when a human must watch.
- [C3: commands](../contracts/C3_commands.md) — the versioned command contract.