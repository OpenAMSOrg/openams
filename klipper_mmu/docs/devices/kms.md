# kms

Vendor: **KMS**, 4 gates, selector `VirtualSelector`. Traced against Happy Hare `c54c736`, harness profile `kms`.

## Decisions

- **arrival**: LOAD's last homing move is the gear homing to the toolhead `filament_compression` switch (`extruder_homing_endstop: filament_compression`): 50 mm/s to endstop_pos 709.1 (= layout.extruder 700 + 9.1), then the extruder reverses off it. That is a plain toolhead switch -- no PSF analog, no buffer ramp -- so klipper-mmu's local toolhead switch input (`KLIPPER_MMU_PINS_TOOLHEAD`) can reproduce the sensing itself.
- **buffer_type**: `[mmu_buffer unit0]` is a switched (tension/compression) buffer: `tension_pin: !unit0_buffer:PA1`, `compression_pin: !unit0_buffer:PA0`, `buffer_range 8`, `buffer_maxrange 13`, `buffer_spring_state: tension`. No proportional/PSF section. klipper-mmu carries switched buffer pins for follow (PINS_BUFFER_A/B) but no buffer ramming, so switched-buffer ramming is a gap, not a blocker.
- **encoder**: `[mmu_unit unit0]` declares `encoder: unit0`, and KMS switches encoder checking on (`encoder_move_validation: 1`, `bowden_allowable_encoder_delta: 20.0`, `gate_endstop_to_encoder: 14`, `skip_cal_encoder: 1`). klipper-mmu's encoder input (`KLIPPER_MMU_ENCODER`) is optional and off by default for KMS, so the image runs without encoder checks; turning it on needs a wired `KLIPPER_MMU_PINS_ENCODER_A`.
- **espooler_assist**: `[mmu_unit unit0]` also declares `espooler: unit0` (`espooler_operations: rewind, assist, print`, 100% burst power, `espooler_max_stepper_speed: 300`). Powered rewind is likewise optional (`KLIPPER_MMU_POWERED_REWIND`, off by default for KMS), so klipper-mmu's KMS runs without an espooler -- recorded as a gap.
- **preload_park**: PRELOAD starts with a 300 mm homing move that ends ON `mmu_exit_0` (tip 0.0, 50 mm/s), then 'Final parking' pulls back 10 mm: `operations.preload.end.tip = -10.0` (`gate_preload_parking_distance: -10`). Rest position: 10 mm behind the gate exit switch. KMS is not in AFC, so there is no second number to weigh.
- **unload_park**: `operations.unload.end.tip = -20.0` (`gate_parking_distance: -20`): the unload reverse-homes onto `mmu_exit_0` and parks 20 mm behind it. HH's number stands alone (KMS is not in AFC).
- **eject_extra**: EJECT asks for `gate_final_eject_distance: 300` but homes backwards, so it stops on the `mmu_entry` switch: `operations.eject.end.tip = -150.0` after only -130 mm of travel from the -20 park. The 300 mm budget is a ceiling; the entry switch is the real stop.

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
| `vendor` | `KMS` |
| `version` | `1.0` |
| `num_gates` | `4` |
| `display_name` | `KMS-0` |
| `encoder` | `unit0` |
| `buffer` | `unit0` |
| `espooler` | `unit0` |
| `toolhead` | `default` |
| `extruder_name` | `extruder` |
| `environment_sensor` | `unit0_environment` |
| `filament_heater` | `unit0_heater` |
| `gear_steppers` | `unit0_gear, unit0_gear_1, unit0_gear_2, unit0_gear_3` |
| `selector_type` | `VirtualSelector` |
| `variable_bowden_lengths` | `0` |
| `variable_rotation_distances` | `1` |
| `require_bowden_move` | `1` |
| `filament_always_gripped` | `1` |
| `show_bypass` | `0` |

### `[mmu_stepper unit0_gear]`

| Key | Value |
|---|---|
| `step_pin` | `unit0:PB5` |
| `dir_pin` | `unit0:PB6` |
| `enable_pin` | `!unit0:PB3` |
| `rotation_distance` | `22.7316868` |
| `gear_ratio` | `50:17` |
| `microsteps` | `16` |
| `full_steps_per_rotation` | `200` |

### `[mmu_sensors unit0]`

| Key | Value |
|---|---|
| `mmu_entry_switch_pin_0` | `unit0:PA0` |
| `mmu_entry_switch_pin_1` | `unit0:PA1` |
| `mmu_entry_switch_pin_2` | `unit0:PA2` |
| `mmu_entry_switch_pin_3` | `unit0:PA3` |
| `mmu_exit_switch_pin_0` | `unit0:PA5` |
| `mmu_exit_switch_pin_1` | `unit0:PA6` |
| `mmu_exit_switch_pin_2` | `unit0:PA7` |
| `mmu_exit_switch_pin_3` | `unit0:PC4` |
| `register_mmu_sensors` | `1` |

### `[mmu_buffer unit0]`

| Key | Value |
|---|---|
| `buffer_range` | `8` |
| `buffer_maxrange` | `13` |
| `tension_pin` | `!unit0_buffer:PA1` |
| `compression_pin` | `!unit0_buffer:PA0` |
| `buffer_spring_state` | `tension` |
| `register_buffer_sensors` | `1` |

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
| `bypass_autoload` | `0` |
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
| `gear_load_speed` | `100` |
| `gear_load_accel` | `100` |
| `gear_unload_speed` | `80` |
| `gear_unload_accel` | `100` |
| `gear_homing_speed` | `50` |
| `virtual_sensor_homing_speed` | `15` |
| `gear_short_move_speed` | `80` |
| `gear_short_move_accel` | `400` |
| `gear_short_move_threshold` | `70` |
| `gate_homing_endstop` | `mmu_exit` |
| `gate_homing_max` | `300` |
| `gate_parking_distance` | `-20` |
| `gate_preload_endstop` | `mmu_exit` |
| `gate_preload_homing_max` | `300` |
| `gate_preload_parking_distance` | `-10` |
| `gate_preload_attempts` | `2` |
| `gate_endstop_to_encoder` | `14` |
| `gate_autoload` | `1` |
| `gate_final_eject_distance` | `300` |
| `bowden_homing_max` | `2000` |
| `bowden_load_homing_buffer` | `20` |
| `bowden_unload_homing_buffer` | `40` |
| `bowden_apply_correction` | `0` |
| `bowden_allowable_encoder_delta` | `20.0` |
| `bowden_pre_unload_test` | `1` |
| `bowden_pre_unload_error_tolerance` | `50` |
| `bowden_move_error_tolerance` | `60` |
| `extruder_homing_endstop` | `filament_compression` |
| `extruder_homing_max` | `100` |
| `extruder_collision_homing_current` | `30` |
| `toolhead_unload_safety_margin` | `10` |
| `toolhead_post_load_tighten` | `60` |
| `toolhead_post_load_tension_adjust` | `1` |
| `toolhead_entry_tension_test` | `1` |
| `toolhead_move_error_tolerance` | `60` |
| `sync_to_extruder` | `1` |
| `sync_gear_current` | `100` |
| `sync_form_tip` | `1` |
| `sync_purge` | `1` |
| `sync_feedback_enabled` | `1` |
| `sync_feedback_speed_multiplier` | `5` |
| `sync_feedback_boost_multiplier` | `3` |
| `sync_feedback_extrude_threshold` | `5` |
| `sync_feedback_debug_log` | `0` |
| `flowguard_enabled` | `1` |
| `flowguard_max_relief` | `40` |
| `tangle_prevention_enabled` | `1` |
| `tangle_prevention_threshold` | `0.3` |
| `tangle_prevention_release` | `0.2` |
| `flowguard_encoder_mode` | `0` |
| `flowguard_encoder_max_motion` | `20` |
| `espooler_min_distance` | `30` |
| `espooler_max_stepper_speed` | `300` |
| `espooler_min_stepper_speed` | `0` |
| `espooler_speed_exponent` | `0.5` |
| `espooler_assist_reduced_speed` | `50` |
| `espooler_printing_power` | `0` |
| `espooler_operations` | `rewind, assist, print` |
| `espooler_assist_extruder_move_length` | `100` |
| `espooler_assist_burst_power` | `100` |
| `espooler_assist_burst_duration` | `0.4` |
| `espooler_assist_burst_trigger` | `0` |
| `espooler_assist_burst_trigger_max` | `3` |
| `espooler_rewind_burst_power` | `100` |
| `espooler_rewind_burst_duration` | `0.4` |
| `heater_max_temp` | `65` |
| `heater_default_dry_temp` | `45` |
| `heater_default_dry_time` | `300` |
| `heater_default_dry_humidity` | `25` |
| `heater_vent_macro` | `_MMU_VENT` |
| `heater_vent_interval` | `0` |
| `heater_rotate_interval` | `5` |
| `autocal_bowden_length` | `1` |
| `autotune_bowden_length` | `0` |
| `skip_cal_rotation_distance` | `1` |
| `autotune_rotation_distance` | `0` |
| `skip_cal_encoder` | `1` |
| `encoder_move_validation` | `1` |

Steps/mm (gear): `414.0372330786448`

## Operations

### PRELOAD

| Phase | Motor | Homing | Endstop | Dist | Speed | Accel | Actual | Homed |
|---|---|---|---|---:|---:|---:|---:|---|
| Initial homing to mmu_exit_0 sensor | gear | 1 | mmu_exit_0 | 300.00 | 50.0 | 100.0 | 40.00 | True |
| Final parking | gear | 0 |  | -10.00 | 80.0 | 400.0 | -10.00 | False |

Ends at tip `-10.00` = `mmu_exit_0` - `10.00` mm (filament_pos=0, gate_status=1).

### SELECT

_No motion (already in the requested state)._

### LOAD

| Phase | Motor | Homing | Endstop | Dist | Speed | Accel | Actual | Homed |
|---|---|---|---|---:|---:|---:|---:|---|
| Initial homing to mmu_exit_0 sensor | gear | 1 | mmu_exit_0 | 300.00 | 50.0 | 100.0 | 10.00 | True |
| Fast loading move through bowden | gear | 0 |  | 680.00 | 100.0 | 100.0 | 680.00 | False |
| Homing filament to extruder endstop | gear | 1 | filament_compression | 120.00 | 50.0 | 100.0 | 29.10 | True |
| Reverse homing off compression sensor | extruder | -1 | filament_compression | 26.00 | 16.0 | 1500.0 | 0.00 | True |
| Loading filament to nozzle | gear+extruder | 0 |  | 68.00 | 18.0 | 400.0 | 68.00 | False |

Ends at tip `777.10` = `mmu_exit_0` + `777.10` mm (filament_pos=10, gate_status=1).

### UNLOAD

| Phase | Motor | Homing | Endstop | Dist | Speed | Accel | Actual | Homed |
|---|---|---|---|---:|---:|---:|---:|---|
| Unloading extruder | gear+extruder | 0 |  | -33.01 | 18.0 | 400.0 | -33.01 | False |
| Fast unloading move through bowden | gear | 0 |  | -650.00 | 80.0 | 100.0 | -650.00 | False |
| Reverse homing off mmu_exit_0 sensor | gear | -1 | mmu_exit_0 | -340.00 | 50.0 | 100.0 | -49.09 | True |
| Final parking | gear | 0 |  | -20.00 | 80.0 | 400.0 | -20.00 | False |

Ends at tip `-20.00` = `mmu_exit_0` - `20.00` mm (filament_pos=0, gate_status=2).

### EJECT

| Phase | Motor | Homing | Endstop | Dist | Speed | Accel | Actual | Homed |
|---|---|---|---|---:|---:|---:|---:|---|
| Ejecting filament out of gate | gear | -1 | mmu_entry | -300.00 | 50.0 | 100.0 | -130.00 | True |

Ends at tip `-150.00` = `mmu_entry` - `0.00` mm (filament_pos=0, gate_status=0).

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
