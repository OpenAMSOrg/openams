# low_rider

Vendor: **LowRider**, 6 gates, selector `ServoSelector`. Traced against Happy Hare `c54c736`, harness profile `low_rider`.

## Decisions

- **selector**: ServoSelector (extras/mmu/unit/selectors/mmu_servo_selector.py:75): `MMU_TYPE_LOW_RIDER_1_0` does `select SERVO_SELECTOR` (installer/mmu_types/Kconfig.low_rider:6) and trades the selector stepper for one RC servo -- `[mmu_unit unit0] selector_servo: unit0_selector_servo`, with no selector `[mmu_stepper]` section -- beside the gear stepper. The servo part is HH's `CHOICE_SERVO_TYPE` default `SERVO_TYPE_DM996` (Kconfig.low_rider:49-50), duration 0.6, dwell 1.0, always_active 0, bypass -1, release 0, max 180 degrees (Kconfig.low_rider:53-63); the type's `imply`s also give `filament_always_gripped: 1` and `variable_rotation_distances: 1` (Kconfig.low_rider:9-10). klipper-mmu drives it as a SERVO selector (`KLIPPER_MMU_SELECTOR_KIND_SERVO`) on `KLIPPER_MMU_PINS_SERVO`, Klipper's scheduled software PWM on any GPIO: one angle per gate, seeded from the table below and replaced by angles saved with `KLIPPER_MMU_CALIBRATE_SELECTOR`, release angle 0 and a 1.0 s dwell, the same as Happy Hare. There is no selector axis to home. Software candidate: it builds and passes the simulators, but has not run on real hardware.
- **lane_count**: 6 gates: `PARAM_NUM_GATES` default 6 (installer/mmu_types/Kconfig.low_rider:33), recorded as `device.gates: 6` and `[mmu_unit unit0] num_gates: 6`. The shipped angle table is written per gate count (Kconfig.low_rider:68-72), so the 6-gate default is the table below and nothing is rescaled.
- **gate_positions**: Servo angles, one per gate, shipped but not calibrated: `PARAM_SERVO_GATE_ANGLES` default `30, 60, 90, 120, 150, 180` (installer/mmu_types/Kconfig.low_rider:67-72) -- gate 0 at 30 degrees, gate 5 at 180, with 0 reserved for the release position. The file's own comment: 'Reserve 0 degrees for the release position, then distribute gates evenly over the remaining selector travel up to 180 degrees'. The render carries the same table (`selector_servo` plus `servo_gate_angles`), and these are klipper-mmu's default gate angles (`KLIPPER_MMU_SELECTOR_SERVO_GATE_ANGLES_DEG`) until angles measured with `KLIPPER_MMU_CALIBRATE_SELECTOR` replace them.
- **arrival**: Arrival is an extruder-mounted switch, and this profile wires none, so LOAD records the real failure: PRELOAD and LOAD both home onto `unit0:mmu_shared_exit` (the only switch the render declares, `mmu_shared_exit_switch_pin`), then LOAD runs `Homing filament to extruder endstop` on endstop `extruder` with `endstop_pos: 700` (layout `extruder` = 700) and never trips it -- the harness records `!! Endstop 'extruder' not found` and `Failed to reach extruder 'extruder' endstop after moving 0.0mm`, leaving `operations.load.end.tip = 682`, `filament_pos: 4`, `gate_status: 1`, with that error latched into UNLOAD and EJECT too. That is HH's configured arrival, not a harness defect: the type `imply MMU_HAS_SENSOR_EXTRUDER` (installer/mmu_types/Kconfig.low_rider:7) and the render sets `extruder_homing_endstop: extruder`, while this baseline profile declares `[mmu_toolhead default] extruder_switch_pin` empty. klipper-mmu does not fabricate the missing switch either: LOAD ends at a local toolhead switch or at FPS arrival, so a build wires one of the two.
- **preload_park**: PRELOAD homes onto `unit0:mmu_shared_exit` (layout 10.0) and parks `gate_preload_parking_distance: -25` mm behind it: `operations.preload.end.tip = -15` (`end.offset = -25.0` behind the switch). Same -25 as Pico MMU; note ViViD parks forward +10 instead.
- **unload_park**: UNLOAD fast-retracts down the bowden (682 -> 22), reverse-homes onto `unit0:mmu_shared_exit` (10.0) and parks `gate_parking_distance: -25` mm behind it: `operations.unload.end.tip = -15.000001`, `gate_status: 2`. The `errors` recorded here are the latched LOAD extruder-endstop failure, not this operation's own.
- **eject_extra**: EJECT retracts `gate_final_eject_distance: 20` mm from the -15.000001 park to `operations.eject.end.tip = -35.000001` with no endstop (`homing: 0`) -- the same 20 mm as MMX, not ERCF's longer gate clear. Its recorded error is the latched LOAD failure.
- **harness_artifact**: Harness artifacts. SELECT records 0 moves: a ServoSelector's swing is a servo command, not a gear move, so the angle table above is the record of selection and the filament model never exercises the servo (there is no selector `[mmu_stepper]` section for this type). `[mmu_sensors unit0]` declares only `mmu_shared_exit_switch_pin`, so the device's gate sensing is that single shared switch -- PRELOAD has no gate switch of its own, and the layout's `mmu_entry`/`mmu_exit` positions are the harness's 1-D path, not per-gate hardware.

## Hardware (rendered)

### `[mmu_toolhead default]`

| Key | Value |
|---|---|
| `extruder_switch_pin` | `` |
| `toolhead_switch_pin` | `` |
| `register_toolhead_sensors` | `1` |
| `toolhead_extruder_to_nozzle` | `72` |
| `toolhead_sensor_to_nozzle` | `62` |
| `toolhead_entry_to_extruder` | `8` |
| `toolhead_residual_filament` | `2` |
| `toolhead_ooze_reduction` | `0` |

### `[mmu_unit unit0]`

| Key | Value |
|---|---|
| `vendor` | `LowRider` |
| `version` | `1.0` |
| `num_gates` | `6` |
| `display_name` | `LowRider-0` |
| `toolhead` | `default` |
| `extruder_name` | `extruder` |
| `selector_servo` | `unit0_selector_servo` |
| `gear_stepper` | `unit0_gear` |
| `selector_type` | `ServoSelector` |
| `variable_bowden_lengths` | `0` |
| `variable_rotation_distances` | `1` |
| `require_bowden_move` | `1` |
| `filament_always_gripped` | `1` |
| `show_bypass` | `0` |

### `[mmu_stepper unit0_gear]`

| Key | Value |
|---|---|
| `step_pin` | `unit0:PD4` |
| `dir_pin` | `unit0:PD3` |
| `enable_pin` | `!unit0:PD5` |
| `rotation_distance` | `23` |
| `gear_ratio` | `80:20` |
| `microsteps` | `16` |
| `full_steps_per_rotation` | `200` |
| `extra_endstops` | `` |
| `mmu_gear_touch=tmc2209_unit0_gear` | `virtual_endstop` |

### `[mmu_sensors unit0]`

| Key | Value |
|---|---|
| `mmu_shared_exit_switch_pin` | `^unit0:PA15` |
| `register_mmu_sensors` | `1` |

## Parameters

| Key | Value |
|---|---|
| `log_level` | `1` |
| `log_file_level` | `2` |
| `log_statistics` | `1` |
| `log_visual` | `1` |
| `log_startup_status` | `1` |
| `log_m117_messages` | `1` |
| `suppress_klipper_warnings` | `0` |
| `extruder_load_speed` | `16` |
| `extruder_unload_speed` | `16` |
| `extruder_sync_load_speed` | `18` |
| `extruder_sync_unload_speed` | `18` |
| `extruder_homing_speed` | `16` |
| `extruder_accel` | `1500` |
| `macro_toolhead_max_accel` | `0` |
| `macro_toolhead_min_cruise_ratio` | `0.5` |
| `force_form_tip_standalone` | `1` |
| `form_tip_macro` | `_MMU_FORM_TIP` |
| `extruder_form_tip_current` | `100` |
| `slicer_tip_park_pos` | `0` |
| `force_purge_standalone` | `1` |
| `purge_macro` | `_MMU_PURGE` |
| `extruder_purge_current` | `100` |
| `endless_spool_enabled` | `0` |
| `endless_spool_on_load` | `0` |
| `endless_spool_eject_gate` | `-1` |
| `endless_spool_groups` | `` |
| `spoolman_support` | `off` |
| `spoolman_pending_id_timeout` | `20` |
| `spoolman_led_segment` | `gate_status` |
| `spoolman_nfc_auto_create` | `0` |
| `t_macro_color` | `slicer` |
| `console_stat_columns` | `unload, load, post_load, total` |
| `console_stat_rows` | `total, total_average, job, job_average, last` |
| `console_gate_stat` | `emoticon` |
| `console_always_output_full` | `1` |
| `console_show_colored_text` | `1` |
| `console_show_filament_color` | `1` |
| `timeout_pause` | `72000` |
| `disable_heater` | `600` |
| `default_extruder_temp` | `200` |
| `extruder_temp_variance` | `2` |
| `startup_reset_ttg_map` | `0` |
| `show_error_dialog` | `1` |
| `strict_filament_recovery` | `0` |
| `filament_recovery_on_pause` | `1` |
| `retry_tool_change_on_error` | `0` |
| `bypass_autoload` | `1` |
| `print_start_detection` | `1` |
| `gcode_load_sequence` | `0` |
| `gcode_unload_sequence` | `0` |
| `drying_data` | `{` |
| `'pla'` | `(45, 300),` |
| `'pla+'` | `(55, 300),` |
| `'petg'` | `(60, 300),` |
| `'tpu'` | `(55, 300),` |
| `'abs'` | `(70, 300),` |
| `'abs+'` | `(75, 300),` |
| `'asa'` | `(65, 300),` |
| `'nylon'` | `(75, 600),` |
| `'pc'` | `(75, 600),` |
| `'pva'` | `(75, 600),` |
| `'hips'` | `(75, 600)` |
| `update_trsync` | `0` |
| `update_bit_max_time` | `1` |
| `update_aht10_commands` | `0` |
| `pause_macro` | `PAUSE` |
| `action_changed_macro` | `_MMU_ACTION_CHANGED` |
| `print_state_changed_macro` | `_MMU_PRINT_STATE_CHANGED` |
| `mmu_event_macro` | `_MMU_EVENT` |
| `post_preload_macro` | `_MMU_POST_PRELOAD` |
| `pre_unload_macro` | `_MMU_PRE_UNLOAD` |
| `post_form_tip_macro` | `_MMU_POST_FORM_TIP` |
| `post_unload_macro` | `_MMU_POST_UNLOAD` |
| `pre_load_macro` | `_MMU_PRE_LOAD` |
| `post_load_macro` | `_MMU_POST_LOAD` |
| `unload_sequence_macro` | `_MMU_UNLOAD_SEQUENCE` |
| `load_sequence_macro` | `_MMU_LOAD_SEQUENCE` |
| `servo_min_angle` | `0` |
| `servo_max_angle` | `180` |
| `servo_gate_angles` | `30, 60, 90, 120, 150, 180` |
| `servo_bypass_angle` | `-1` |
| `servo_release_angle` | `0` |
| `servo_duration` | `0.6` |
| `servo_dwell` | `1.0` |
| `servo_always_active` | `0` |
| `gear_load_speed` | `100` |
| `gear_load_accel` | `100` |
| `gear_unload_speed` | `80` |
| `gear_unload_accel` | `100` |
| `gear_homing_speed` | `50` |
| `virtual_sensor_homing_speed` | `15` |
| `gear_short_move_speed` | `80` |
| `gear_short_move_accel` | `400` |
| `gear_short_move_threshold` | `70` |
| `gate_homing_endstop` | `mmu_shared_exit` |
| `gate_homing_max` | `1000` |
| `gate_parking_distance` | `-25` |
| `gate_preload_endstop` | `` |
| `gate_preload_homing_max` | `1000` |
| `gate_preload_parking_distance` | `-25` |
| `gate_preload_attempts` | `2` |
| `gate_final_eject_distance` | `20` |
| `bowden_homing_max` | `2000` |
| `bowden_load_homing_buffer` | `20` |
| `bowden_unload_homing_buffer` | `40` |
| `extruder_homing_endstop` | `extruder` |
| `extruder_homing_max` | `100` |
| `extruder_collision_homing_current` | `30` |
| `toolhead_unload_safety_margin` | `10` |
| `toolhead_post_load_tighten` | `60` |
| `toolhead_post_load_tension_adjust` | `1` |
| `toolhead_entry_tension_test` | `1` |
| `sync_to_extruder` | `1` |
| `sync_gear_current` | `100` |
| `sync_form_tip` | `0` |
| `sync_purge` | `0` |
| `autocal_bowden_length` | `1` |
| `autotune_bowden_length` | `0` |
| `skip_cal_rotation_distance` | `1` |
| `autotune_rotation_distance` | `0` |

Steps/mm (gear): `556.5217391304348`

## Operations

### PRELOAD

| Phase | Motor | Homing | Endstop | Dist | Speed | Accel | Actual | Homed |
|---|---|---|---|---:|---:|---:|---:|---|
| Initial homing to unit0:mmu_shared_exit sensor | gear | 1 | unit0:mmu_shared_exit | 1000.00 | 50.0 | 100.0 | 50.00 | True |
| Final parking | gear | 0 |  | -25.00 | 80.0 | 400.0 | -25.00 | False |

Ends at tip `-15.00` = `unit0:mmu_shared_exit` - `25.00` mm (filament_pos=0, gate_status=1).

### SELECT

_No motion (already in the requested state)._

### LOAD

| Phase | Motor | Homing | Endstop | Dist | Speed | Accel | Actual | Homed |
|---|---|---|---|---:|---:|---:|---:|---|
| Initial homing to unit0:mmu_shared_exit sensor | gear | 1 | unit0:mmu_shared_exit | 1000.00 | 50.0 | 100.0 | 25.00 | True |
| Fast loading move through bowden | gear | 0 |  | 672.00 | 100.0 | 100.0 | 672.00 | False |
| Homing filament to extruder endstop | gear | 1 | extruder | 128.00 | 50.0 | 100.0 | 0.00 | False |

Ends at tip `682.00` = `extruder` - `18.00` mm (filament_pos=4, gate_status=1).

Errors: ["!! Endstop 'extruder' not found", "!! MMU issue: Load sequence failed because:\nFailed to reach extruder 'extruder' endstop after moving 0.0mm.\nOccurred when loading tool: > T0"]

### UNLOAD

| Phase | Motor | Homing | Endstop | Dist | Speed | Accel | Actual | Homed |
|---|---|---|---|---:|---:|---:|---:|---|
| Fast unloading move through bowden | gear | 0 |  | -660.00 | 80.0 | 100.0 | -660.00 | False |
| Reverse homing off unit0:mmu_shared_exit sensor | gear | -1 | unit0:mmu_shared_exit | -1040.00 | 50.0 | 100.0 | -12.00 | True |
| Final parking | gear | 0 |  | -25.00 | 80.0 | 400.0 | -25.00 | False |

Ends at tip `-15.00` = `unit0:mmu_shared_exit` - `25.00` mm (filament_pos=0, gate_status=2).

Errors: ["!! Endstop 'extruder' not found", "!! MMU issue: Load sequence failed because:\nFailed to reach extruder 'extruder' endstop after moving 0.0mm.\nOccurred when loading tool: > T0"]

### EJECT

| Phase | Motor | Homing | Endstop | Dist | Speed | Accel | Actual | Homed |
|---|---|---|---|---:|---:|---:|---:|---|
| Ejecting filament out of gate | gear | 0 |  | -20.00 | 80.0 | 400.0 | -20.00 | False |

Errors: ["!! Endstop 'extruder' not found", "!! MMU issue: Load sequence failed because:\nFailed to reach extruder 'extruder' endstop after moving 0.0mm.\nOccurred when loading tool: > T0"]

## Concept mapping

| Upstream concept | AFC | Happy Hare | klipper-mmu today | Status |
|---|---|---|---|---|
| Lane / gate | [AFC_stepper laneN], unit: X:N | gate index, unit0_gear_<i> | lane_count, per-lane feeder driver | mapped |
| Prep / entry switch | prep: (TRG) | mmu_entry_<i> | has_bay_sensors, KLIPPER_MMU_PINS_BAY | mapped |
| Load / exit switch | load: (LOAD) | mmu_exit_<i> | has_load_sensors, KLIPPER_MMU_PINS_LOAD | mapped |
| Hub / shared exit | [AFC_hub] switch_pin | mmu_shared_exit | has_hub_sensor, PINS_HUB | mapped |
| Virtual hub | switch_pin: virtual + use_dist_hub | no shared exit + gate_homing_endstop: mmu_exit | load_is_virtual_hub | mapped |
| Bowden length | afc_bowden_length / dist_hub per lane | mmu_calibration_bowden_lengths (per gate if variable) | fps_calibrated_steps (one per unit) | gap: per-lane |
| Toolhead switch | pin_tool_start / pin_tool_end, tool_stn | [mmu_toolhead] toolhead_switch_pin, toolhead_sensor_to_nozzle | has_toolhead_sensor, PINS_TOOLHEAD; nozzle distances host-owned | mapped (boundary), host (distances) |
| Switched buffer | [AFC_buffer] advance_pin/trailing_pin + multipliers | [mmu_buffer] compression_pin/tension_pin | has_proportional_buffer=false, PINS_BUFFER_A/B switched | mapped for follow; gap: ramming arrival |
| Proportional buffer (PSF) | AFCFPSBuffer adc_pin, neutral/low/high, P+I | analog_pin, max_compression/tension, threshold; EKF/two-level | DiffA/DiffB pair only (km_diff_adc_config_t) | gap: single-ended ADC frontend |
| FPS (OpenAMS) | AFC_FPS.cfg | n/a | DiffA/DiffB + fps_arrival | mapped |
| Espooler / assist | AFC_assist.py, espooler.assist(pwm) | [mmu_espooler] burst/rewind | has_powered_rewind (KLIPPER_MMU_POWERED_REWIND), PWM duty, current regulator | mapped (on by default for BoxTurtle and Claymore); EMU none |
| Encoder | n/a in EMU | [mmu_encoder], encoder homing | has_encoder (KLIPPER_MMU_ENCODER), PINS_ENCODER_A, optional B: clog/stall and progress checks, CALIBRATE to a toolhead switch | mapped (on by default for ERCF); encoder homing not a mode |
| Selector | HTLF lobes, ViViD/Claymore selector switches | SELECTOR_REGISTRY classes | has_selector, KLIPPER_MMU_SELECTOR_KIND_LINEAR/ROTARY/SERVO/INDEXED; homes before first use; KLIPPER_MMU_CALIBRATE_SELECTOR | mapped |
| Grip / servo | n/a EMU | servo_* | KLIPPER_MMU_PINS_SERVO (Klipper software PWM, any GPIO): LINEAR grip angles, SERVO per-gate angles | mapped |
| Speeds | long/short/hub/calibration modes | load/unload/short/homing/virtual-sensor speeds | feed/rewind velocity, accel/decel, cruise/crawl | mapped in shape; per-device numbers vary |
| Currents | run_current, print_current (follow) | run/hold, sync_gear_current % | run/hold only | gap: follow current |
| Homing budget / attempts | homing_overshoot, homing_delta, attempt loops | gate_homing_max, *_attempts, bowden_homing_max | timeouts in ms, KM_LOAD_ATTEMPTS 3, arrival min/max steps | mapped as deadlines; distance vs time is a translation choice |
| Parking / clearance | hub_clear_move_dis, extruder_clear_dis, park_dist | gate_parking_distance, gate_final_eject_distance | unload_hub_clear_steps; EJECT to BAY-clear | partly mapped; decision |
| Tip forming, cut, purge, park | AFC macros, AFC_form_tip.py | _MMU_FORM_TIP, _MMU_PARK, purge | host/OpenAMS macros | host-only |
| Runout / endless spool | handle_prep_runout, _perform_infinite_runout | mmu_endless_spool.py | host reducer + firmware runout evidence | host-only (with firmware evidence) |
| LEDs, buttons, fans, environment, heater, NFC, Spoolman | many | many | out of scope for firmware | not-applicable |

_Not modelled by this dossier: motion timing/step generation, macro bodies, espooler and FlowGuard behaviour, real sensor electrics, and calibration (seeded, not measured)._
