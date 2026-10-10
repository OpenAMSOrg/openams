# emu

Vendor: **EMU**, 5 gates, selector `VirtualSelector`. Traced against Happy Hare `c54c736`, harness profile `emu`.

## Decisions

- **arrival**: PSF proportional (Happy Hare's default, measured: homes to filament_compression at layout.extruder + 11.2mm), a switched advance/trailing buffer (AFC's default template), or a plain toolhead switch -- upstream offers a full menu of ways to know filament arrived. klipper-mmu ends LOAD at a local toolhead switch or at FPS arrival read over the DiffA/DiffB pair; a single-ended PSF ADC frontend is not implemented.
- **preload_park**: Happy Hare parks PRELOAD 1mm behind mmu_exit_0 (measured, Kconfig default PARAM_GATE_PRELOAD_PARKING_DISTANCE -1) -- klipper-mmu's EMU also rests just behind it: PRELOAD backs off until that LOAD switch, its virtual hub, clears.
- **unload_park**: Happy Hare parks UNLOAD 1mm behind mmu_exit_0 (measured this trace) vs AFC's hub_clear_move_dis of 5mm -- close enough to argue about, not close enough to ignore.
- **eject_extra**: Happy Hare's EJECT retracts a flat 100mm from park (measured this trace, homed=false: it never bothers confirming a switch, just trusts the budget) vs AFC's extruder_clear_dis of 50mm -- twice as generous, no obvious reason.
- **short_speed**: 80/80 mm/s (Happy Hare's gear_short_move_speed/accel) vs 50/300 (AFC.cfg's short speed/accel) -- one upstream likes a gentle short move, the other likes to floor it and brake hard. klipper-mmu follows Happy Hare's EMU numbers (its EMU braking acceleration is the 80 mm/s^2 `gear_short_move_accel`).
- **bowden**: Per-lane calibrated bowden length storage is still open: klipper-mmu stores one measured Bowden length per unit (`bowden_estimate_file`), but EMU's variable_bowden_lengths=1 and AFC's per-lane afc_bowden_length both want one length per lane.
- **lane_count**: AFC's pinned EMU.cfg is a single-lane reference template (its own header comment: 'The installer generates a complete multi-lane config from this layout'), so a plain read sees 1 lane; Happy Hare's emu profile models the full 5-gate unit. Both upstreams put one MCU per lane -- a fact, not a discrepancy to resolve; EMU stays a single-board software candidate.
- **sensor_polarity**: AFC's EMU_SLB wiring inverts and pulls up TRG (prep: ^!emu_lane1:TRG -> PA1); Happy Hare's emu trace models the same PA1 pin pulled up only, no invert (entry=^!,exit=^ vs entry=^,exit=^). New device: adopting HH's convention (no invert) until hardware evidence says otherwise.
- **buffer_type**: AFC's pinned EMU.cfg template configures a switched advance/trailing buffer by default (its commented-out AFC_buffer EMU_1_PSF_buffer1 section is the real analog PSF option, left disabled); Happy Hare's emu profile models the analog PSF frontend instead, matching the real EMU hardware. PSF analog (single-ended) and switched-buffer ramming are both gaps, not blockers; recording HH's analog fact as the executable reference.

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
| `vendor` | `EMU` |
| `version` | `1.0` |
| `num_gates` | `5` |
| `display_name` | `EMU-0` |
| `buffer` | `unit0` |
| `toolhead` | `default` |
| `extruder_name` | `extruder` |
| `fans` | `_unit0_fan0, _unit0_fan1, _unit0_fan2, _unit0_fan3, _unit0_fan4` |
| `environment_sensors` | `unit0_Env0, unit0_Env1, unit0_Env2, unit0_Env3, unit0_Env4` |
| `gear_steppers` | `unit0_gear, unit0_gear_1, unit0_gear_2, unit0_gear_3, unit0_gear_4` |
| `selector_type` | `VirtualSelector` |
| `variable_bowden_lengths` | `1` |
| `variable_rotation_distances` | `1` |
| `require_bowden_move` | `1` |
| `filament_always_gripped` | `1` |
| `show_bypass` | `0` |

### `[mmu_stepper unit0_gear]`

| Key | Value |
|---|---|
| `step_pin` | `unit0_gate0:PB5` |
| `dir_pin` | `unit0_gate0:PB4` |
| `enable_pin` | `!unit0_gate0:PB7` |
| `rotation_distance` | `22.7316868` |
| `gear_ratio` | `1:1` |
| `microsteps` | `4` |
| `full_steps_per_rotation` | `200` |

### `[mmu_sensors unit0]`

| Key | Value |
|---|---|
| `mmu_entry_switch_pin_0` | `^unit0_gate0:PA1` |
| `mmu_entry_switch_pin_1` | `^unit0_gate1:PA1` |
| `mmu_entry_switch_pin_2` | `^unit0_gate2:PA1` |
| `mmu_entry_switch_pin_3` | `^unit0_gate3:PA1` |
| `mmu_entry_switch_pin_4` | `^unit0_gate4:PA1` |
| `mmu_exit_switch_pin_0` | `^unit0_gate0:PA0` |
| `mmu_exit_switch_pin_1` | `^unit0_gate1:PA0` |
| `mmu_exit_switch_pin_2` | `^unit0_gate2:PA0` |
| `mmu_exit_switch_pin_3` | `^unit0_gate3:PA0` |
| `mmu_exit_switch_pin_4` | `^unit0_gate4:PA0` |
| `register_mmu_sensors` | `1` |

### `[mmu_buffer unit0]`

| Key | Value |
|---|---|
| `buffer_range` | `16` |
| `buffer_maxrange` | `16` |
| `buffer_spring_state` | `tension` |
| `analog_pin` | `unit0_gate0:PA3` |
| `analog_max_compression` | `1.0` |
| `analog_max_tension` | `0.0` |
| `analog_neutral_point` | `0.5` |
| `analog_gamma` | `1.0` |
| `analog_sensor_threshold` | `0.9` |
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
| `gear_load_speed` | `250` |
| `gear_load_accel` | `120` |
| `gear_unload_speed` | `250` |
| `gear_unload_accel` | `120` |
| `gear_homing_speed` | `80` |
| `virtual_sensor_homing_speed` | `15` |
| `gear_short_move_speed` | `80` |
| `gear_short_move_accel` | `80` |
| `gear_short_move_threshold` | `100` |
| `gate_homing_endstop` | `mmu_exit` |
| `gate_homing_max` | `600` |
| `gate_parking_distance` | `-1` |
| `gate_preload_endstop` | `mmu_exit` |
| `gate_preload_homing_max` | `600` |
| `gate_preload_parking_distance` | `-1` |
| `gate_preload_attempts` | `2` |
| `gate_autoload` | `1` |
| `gate_final_eject_distance` | `100` |
| `bowden_homing_max` | `2000` |
| `bowden_load_homing_buffer` | `20` |
| `bowden_unload_homing_buffer` | `40` |
| `extruder_homing_endstop` | `filament_compression` |
| `extruder_homing_max` | `100` |
| `extruder_collision_homing_current` | `30` |
| `toolhead_unload_safety_margin` | `10` |
| `toolhead_post_load_tighten` | `60` |
| `toolhead_post_load_tension_adjust` | `1` |
| `toolhead_entry_tension_test` | `1` |
| `sync_to_extruder` | `1` |
| `sync_gear_current` | `52` |
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
| `default_fan_temperature_source` | `mcu` |
| `default_fan_on_temp` | `49.0` |
| `default_fan_off_temp` | `47.0` |
| `fan_polling_time` | `5.0` |
| `fan_control_enabled` | `1` |
| `fan_forced` | `2` |
| `autocal_bowden_length` | `1` |
| `autotune_bowden_length` | `0` |
| `skip_cal_rotation_distance` | `1` |
| `autotune_rotation_distance` | `0` |

Steps/mm (gear): `35.19316481168481`

## Operations

### PRELOAD

| Phase | Motor | Homing | Endstop | Dist | Speed | Accel | Actual | Homed |
|---|---|---|---|---:|---:|---:|---:|---|
| Initial homing to mmu_exit_0 sensor | gear | 1 | mmu_exit_0 | 600.00 | 80.0 | 120.0 | 40.00 | True |
| Final parking | gear | 0 |  | -1.00 | 80.0 | 80.0 | -1.00 | False |

Ends at tip `-1.00` = `mmu_exit_0` - `1.00` mm (filament_pos=0, gate_status=1).

### SELECT

_No motion (already in the requested state)._

### LOAD

| Phase | Motor | Homing | Endstop | Dist | Speed | Accel | Actual | Homed |
|---|---|---|---|---:|---:|---:|---:|---|
| Initial homing to mmu_exit_0 sensor | gear | 1 | mmu_exit_0 | 600.00 | 80.0 | 120.0 | 1.00 | True |
| Fast loading move through bowden | gear | 0 |  | 680.00 | 250.0 | 120.0 | 680.00 | False |
| Homing filament to extruder endstop | gear | 1 | filament_compression | 120.00 | 80.0 | 120.0 | 31.20 | True |
| Synced feed to establish extruder grip | gear+extruder | 0 |  | 8.00 | 18.0 | 400.0 | 8.00 | False |
| Proportional extruder entry validation step 1 | extruder | 0 |  | 8.00 | 16.0 | 1500.0 | 8.00 | False |
| Loading filament to nozzle | gear+extruder | 0 |  | 52.00 | 18.0 | 400.0 | 52.00 | False |
| Proportional initial adjust - extruder load | gear | 0 |  | 8.00 | 80.0 | 80.0 | 8.00 | False |
| Proportional adjust - extruder load | gear | 0 |  | 0.80 | 80.0 | 80.0 | 0.80 | False |
| Proportional adjust - extruder load | gear | 0 |  | 0.80 | 80.0 | 80.0 | 0.80 | False |
| Proportional adjust - extruder load | gear | 0 |  | 0.80 | 80.0 | 80.0 | 0.80 | False |
| Proportional adjust - extruder load | gear | 0 |  | 0.80 | 80.0 | 80.0 | 0.80 | False |
| Proportional adjust - extruder load | gear | 0 |  | 0.80 | 80.0 | 80.0 | 0.80 | False |
| Proportional adjust - extruder load | gear | 0 |  | 0.80 | 80.0 | 80.0 | 0.80 | False |
| Proportional adjust - extruder load | gear | 0 |  | 0.80 | 80.0 | 80.0 | 0.80 | False |
| Proportional adjust - extruder load | gear | 0 |  | 0.80 | 80.0 | 80.0 | 0.80 | False |
| Proportional adjust - extruder load | gear | 0 |  | 0.80 | 80.0 | 80.0 | 0.80 | False |

Ends at tip `794.40` = `filament_compression` + `83.20` mm (filament_pos=10, gate_status=1).

### UNLOAD

| Phase | Motor | Homing | Endstop | Dist | Speed | Accel | Actual | Homed |
|---|---|---|---|---:|---:|---:|---:|---|
| Unloading extruder | gear+extruder | 0 |  | -33.01 | 18.0 | 400.0 | -33.01 | False |
| Fast unloading move through bowden | gear | 0 |  | -650.00 | 250.0 | 120.0 | -650.00 | False |
| Reverse homing off mmu_exit_0 sensor | gear | -1 | mmu_exit_0 | -640.00 | 80.0 | 120.0 | -66.39 | True |
| Final parking | gear | 0 |  | -1.00 | 80.0 | 80.0 | -1.00 | False |

Ends at tip `-1.00` = `mmu_exit_0` - `1.00` mm (filament_pos=0, gate_status=2).

### EJECT

| Phase | Motor | Homing | Endstop | Dist | Speed | Accel | Actual | Homed |
|---|---|---|---|---:|---:|---:|---:|---|
| Ejecting filament out of gate | gear | -1 | mmu_entry | -100.00 | 80.0 | 120.0 | -100.00 | False |

Ends at tip `-101.00` = `mmu_entry` + `49.00` mm (filament_pos=0, gate_status=0).

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
