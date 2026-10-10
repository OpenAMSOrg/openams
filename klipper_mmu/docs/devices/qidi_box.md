# qidi_box

Vendor: **QIDI**, 4 gates, selector `VirtualSelector`. Traced against Happy Hare `c54c736`, harness profile `qidi`.

## Decisions

- **arrival**: UNLOAD and EJECT fail in this trace with "Failed to reach extruder entry sensor after moving -86.0mm" (both operations' `errors`). LOAD's own moves land the tip at 1110mm, 210mm past the `extruder` endstop's recorded position (900mm, source.layout.extruder): its `Fast loading move through bowden` travels 872mm from `unit0:mmu_shared_exit` (150mm), which already overshoots `extruder_entry`/`extruder` (900mm) by 122mm -- confirmed by the immediately following `Homing filament to extruder endstop` move recording `actual: 0.0` (already triggered). UNLOAD's reverse-homing length is fixed by `toolhead_extruder_to_nozzle + toolhead_entry_to_extruder + toolhead_unload_safety_margin - toolhead_residual_filament - toolhead_ooze_reduction - toolchange_retract` (extras/mmu/mmu_filament_movement.py:2111-2118) = 72+8+10-2-0-0 = 88mm (recorded as 86mm), sized for the QIDI Q2's real ~90mm nozzle-to-switch geometry (installer/toolheads/Kconfig.qidi_q2), not for the much larger simulated overshoot LOAD actually produced. This is the shipped `qidi` harness console profile's own simulator-only `filament_layout` (test/hh/profiles.py:433-438: mmu_shared_exit 150, extruder_entry 900, extruder 900, toolhead 940) sitting short of its own bowden-load travel, not a klipper-mmu or firmware limitation -- the real machine's bowden length is calibrated per-unit (`autocal_bowden_length: 1`) against its own physical switch. Recorded as Happy Hare's real, reproducible behaviour for this profile -- a clear failure with evidence is a valid result -- not fabricated or worked around.

## Hardware (rendered)

### `[mmu_toolhead default]`

| Key | Value |
|---|---|
| `extruder_switch_pin` | `!THR:PA1` |
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
| `vendor` | `QIDI` |
| `version` | `1.0` |
| `num_gates` | `4` |
| `display_name` | `QIDI-0` |
| `buffer` | `unit0` |
| `toolhead` | `default` |
| `extruder_name` | `extruder` |
| `environment_sensor` | `unit0_Env` |
| `filament_heater` | `unit0_heater` |
| `gear_steppers` | `unit0_gear, unit0_gear_1, unit0_gear_2, unit0_gear_3` |
| `selector_type` | `VirtualSelector` |
| `variable_bowden_lengths` | `0` |
| `variable_rotation_distances` | `0` |
| `require_bowden_move` | `1` |
| `filament_always_gripped` | `1` |
| `show_bypass` | `0` |

### `[mmu_stepper unit0_gear]`

| Key | Value |
|---|---|
| `step_pin` | `unit0:PC14` |
| `dir_pin` | `unit0:PC13` |
| `enable_pin` | `!unit0:PC15` |
| `rotation_distance` | `13.6` |
| `gear_ratio` | `1:1` |
| `microsteps` | `16` |
| `full_steps_per_rotation` | `200` |

### `[mmu_sensors unit0]`

| Key | Value |
|---|---|
| `mmu_entry_switch_pin_0` | `!unit0:PA0` |
| `mmu_entry_switch_pin_1` | `!unit0:PB3` |
| `mmu_entry_switch_pin_2` | `!unit0:PA13` |
| `mmu_entry_switch_pin_3` | `!unit0:PA7` |
| `mmu_shared_exit_switch_pin` | `^!unit0:PB1` |
| `register_mmu_sensors` | `1` |

### `[mmu_buffer unit0]`

| Key | Value |
|---|---|
| `buffer_range` | `8` |
| `buffer_maxrange` | `12` |
| `tension_pin` | `^unit0:PB0` |
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
| `form_tip_macro` | `_MMU_CUT_TIP` |
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
| `gear_load_speed` | `120` |
| `gear_load_accel` | `100` |
| `gear_unload_speed` | `80` |
| `gear_unload_accel` | `100` |
| `gear_homing_speed` | `50` |
| `virtual_sensor_homing_speed` | `15` |
| `gear_short_move_speed` | `80` |
| `gear_short_move_accel` | `400` |
| `gear_short_move_threshold` | `70` |
| `gate_homing_endstop` | `mmu_shared_exit` |
| `gate_homing_max` | `1500` |
| `gate_parking_distance` | `-80` |
| `gate_preload_endstop` | `` |
| `gate_preload_homing_max` | `1500` |
| `gate_preload_parking_distance` | `-80` |
| `gate_preload_attempts` | `2` |
| `gate_autoload` | `1` |
| `gate_final_eject_distance` | `1500` |
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
| `heater_max_temp` | `65` |
| `heater_default_dry_temp` | `45` |
| `heater_default_dry_time` | `300` |
| `heater_default_dry_humidity` | `25` |
| `heater_vent_macro` | `_MMU_VENT` |
| `heater_vent_interval` | `0` |
| `autocal_bowden_length` | `1` |
| `autotune_bowden_length` | `0` |
| `skip_cal_rotation_distance` | `1` |
| `autotune_rotation_distance` | `0` |

Steps/mm (gear): `235.29411764705884`

## Operations

### PRELOAD

| Phase | Motor | Homing | Endstop | Dist | Speed | Accel | Actual | Homed |
|---|---|---|---|---:|---:|---:|---:|---|
| Initial homing to unit0:mmu_shared_exit sensor | gear | 1 | unit0:mmu_shared_exit | 1500.00 | 50.0 | 100.0 | 190.00 | True |
| Final parking | gear | 0 |  | -80.00 | 80.0 | 100.0 | -80.00 | False |

Ends at tip `70.00` = `unit0:mmu_shared_exit` - `80.00` mm (filament_pos=0, gate_status=1).

### SELECT

_No motion (already in the requested state)._

### LOAD

| Phase | Motor | Homing | Endstop | Dist | Speed | Accel | Actual | Homed |
|---|---|---|---|---:|---:|---:|---:|---|
| Initial homing to unit0:mmu_shared_exit sensor | gear | 1 | unit0:mmu_shared_exit | 1500.00 | 50.0 | 100.0 | 80.00 | True |
| Fast loading move through bowden | gear | 0 |  | 872.00 | 120.0 | 100.0 | 872.00 | False |
| Homing filament to extruder endstop | gear | 1 | extruder | 128.00 | 50.0 | 100.0 | 0.00 | True |
| Aligning filament to extruder gear | gear | 0 |  | 8.00 | 80.0 | 400.0 | 8.00 | False |
| Loading filament to nozzle | gear+extruder | 0 |  | 68.00 | 18.0 | 400.0 | 68.00 | False |
| Reverse homing off tension sensor | gear | -1 | filament_tension | 12.00 | 16.0 | 100.0 | 12.00 | False |

Ends at tip `1110.00` = `extruder` + `210.00` mm (filament_pos=10, gate_status=1).

### UNLOAD

| Phase | Motor | Homing | Endstop | Dist | Speed | Accel | Actual | Homed |
|---|---|---|---|---:|---:|---:|---:|---|
| Reverse homing off extruder sensor | gear+extruder | -1 | extruder | -86.00 | 16.0 | 400.0 | -86.00 | False |

Ends at tip `1024.00` = `extruder` + `124.00` mm (filament_pos=9, gate_status=1).

Errors: ['!! MMU issue: Unload sequence failed because:\nFailed to reach extruder entry sensor after moving -86.0mm.\nOccurred when unloading tool']

### EJECT

| Phase | Motor | Homing | Endstop | Dist | Speed | Accel | Actual | Homed |
|---|---|---|---|---:|---:|---:|---:|---|
| Reverse homing off extruder sensor | gear+extruder | -1 | extruder | -86.00 | 16.0 | 400.0 | -86.00 | False |

Ends at tip `938.00` = `extruder` + `38.00` mm (filament_pos=9, gate_status=1).

Errors: ['!! MMU issue: Unload sequence failed because:\nFailed to reach extruder entry sensor after moving -86.0mm.\nOccurred when unloading tool', '!! MMU issue: Filament eject for gate 0 failed: Unload sequence failed because:\nFailed to reach extruder entry sensor after moving -86.0mm']

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
