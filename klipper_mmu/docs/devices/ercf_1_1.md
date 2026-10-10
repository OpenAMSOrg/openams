# ercf_1_1

Vendor: **ERCF**, 9 gates, selector `LinearServoSelector`. Traced against Happy Hare `c54c736`, harness profile `ercf_1_1`.

## Decisions

- **selector**: The committed render declares `device.selector_type: LinearServoSelector` (the same on `[mmu_unit unit0]`), with ERCF's real selector hardware coupled to it: `selector_stepper: unit0_selector`, `selector_servo: unit0_selector_servo`, `encoder: unit0`. That is what Happy Hare's family Kconfig specifies -- `if MMU_FAMILY_ERCF` does `imply LINEAR_SERVO_SELECTOR` and `select MMU_HAS_ENCODER` (installer/mmu_types/Kconfig.ercf:6-8) -- and the version choice is nested under that family, so this dossier is traced with `MMU_FAMILY_ERCF` + `MMU_TYPE_ERCF_1_1` (plus the MMB 2.0 shared-exit symbols). A bare `MMU_TYPE_ERCF_1_1` without the family renders Happy Hare's 'Safety' VirtualSelector instead. Rendered selector motion: `selector_move_speed: 200`, `selector_homing_speed: 60`, `selector_accel: 1200`, `servo_buzz_gear_on_down: 3`, `servo_move_angle: 30`. klipper-mmu drives it as a LINEAR selector with a grip servo (`KLIPPER_MMU_SELECTOR_KIND_LINEAR`, `KLIPPER_MMU_SELECTOR_LINEAR_HAS_SERVO`): the carriage homes to its endstop before the first selection and after a fault, moves at the same 200 mm/s (60 mm/s homing, 1200 mm/s^2), and the servo grips at 100 degrees, releases at 30 and travels at 30, driven by Klipper's scheduled software PWM on any GPIO (`KLIPPER_MMU_PINS_SERVO`). Gate positions start from an estimate computed from Happy Hare's CAD geometry (`4.2,25.2,46.2,72.2,93.2,114.2,140.2,161.2,182.2` mm) until `KLIPPER_MMU_CALIBRATE_SELECTOR` measures the real ones. ERCF 1.1's Springy, Triple Decky and Binky options are offered in menuconfig (`KLIPPER_MMU_ERCF_SPRINGY`, `KLIPPER_MMU_ERCF_TRIPLE_DECKY`, `KLIPPER_MMU_ERCF_BINKY`). Software candidate: it builds and passes the simulators, but has not run on real hardware.
- **arrival**: LOAD completes and arrival is encoder-measured, not blind: after the bowden feed the extruder approach is a collision-homing loop of nine 2.4 mm moves (`operations.load.moves`; each step is 2.4 mm = 3.0 x ceil(0.7059 x 10)/10 for the 0.7059 mm TCRT5000 count) followed by the final 'Loading filament to nozzle' push, and the operation ends at `operations.load.end.tip = 789.774300`, `filament_pos: 10`, `gate_status: 1`, `errors: []`. The render configures `extruder_homing_endstop: encoder`, `gate_homing_endstop: encoder`, `gate_endstop_to_encoder: 10`, `encoder_move_validation: 1`, `bowden_allowable_encoder_delta: 20.0`, `skip_cal_encoder: 0`. The recorded end-stop read-out is empty for all five operations here (`end.sensor: null`, `end.offset: null`), where 2.0/3.0 record `mmu_entry` / 137.577 and `mmu_entry` / 134.121 on EJECT.
- **encoder**: An encoder is bound (`encoder: unit0`) and its checks are on: `encoder_move_validation: 1`, `bowden_allowable_encoder_delta: 20.0`, `skip_cal_encoder: 0`, `gate_endstop_to_encoder: 10`, with both `gate_homing_endstop` and `extruder_homing_endstop` set to `encoder`. The render does not name the physical part; ERCF 1.1's Kconfig default is a TCRT5000 opto (Binky-8 only with the MOD_BINKY mod, installer/mmu_types/Kconfig.ercf:180-186), i.e. the 0.7059 mm/count device the harness models -- and the resolution is what makes this device's collision homing converge. 2.0's Binky-8 (1.469 mm) and 3.0's Binky-12 (0.979 mm) fail the same loop (see version_diff). klipper-mmu turns its encoder input on by default for every ERCF (`KLIPPER_MMU_ENCODER`, because Happy Hare's ERCF family selects MMU_HAS_ENCODER): a one-wire click counter on `KLIPPER_MMU_PINS_ENCODER_A`, which needs that pin and `KLIPPER_MMU_ENCODER_PULL` set before the image starts. It is used for clog detection (no click over `KLIPPER_MMU_ENCODER_DETECTION_LENGTH_MM`, 10 mm, of gear travel), for progress checks during an operation, and to measure the Bowden length with `KLIPPER_MMU_CALIBRATE` when a toolhead switch is fitted. Happy Hare's encoder homing (`gate_homing_endstop: encoder`) is not a klipper-mmu mode: LOAD ends at a local toolhead switch or at FPS arrival.
- **buffer_type**: No `[mmu_buffer unit0]` renders and no buffer key appears anywhere in the dossier, although the family Kconfig implies `MMU_HAS_FILAMENT_BUFFER` (installer/mmu_types/Kconfig.ercf:9). klipper-mmu has no 'no buffer' build: every image follows the extruder from a buffer input -- a switched pair (`KLIPPER_MMU_PINS_BUFFER_A/B`), a single tension switch (`KLIPPER_MMU_BUFFER_SINGLE_SWITCH`) or the analog DiffA/DiffB pair, so an ERCF build needs one wired even though this render declares none.
- **preload_park**: PRELOAD ends at `operations.preload.end.tip = -2.882600` with `gate_preload_parking_distance: -23` and an empty `gate_preload_endstop`, and SELECT leaves the filament there (`operations.select.end.tip = -2.882600`). The -23 is ERCF 1.1's own default (-13 with Thumper blocks, installer/mmu_types/Kconfig.ercf:174-176).
- **unload_park**: UNLOAD re-homes to the same park: `operations.unload.end.tip = -2.647500` with `gate_parking_distance: -23`, `filament_pos: 0`, `gate_status: 2`, `errors: []`. 1.1's -23 is the version's own value; 2.0 parks -13 (-11 with Thumper blocks, Kconfig.ercf:220-222) and 3.0 -16 (Kconfig.ercf:264).
- **eject_extra**: EJECT adds a flat `gate_final_eject_distance: 20` mm past the park with homing off, ending at `operations.eject.end.tip = -22.647500` (`filament_pos: 0`, `gate_status: 0`, `errors: []`).
- **version_diff**: The three versions are no longer identical renders, and their recorded behaviour differs. This one (1.1): 9 gates, `show_bypass: 0`, `gear_ratio: 80:20` -> `steps_per_mm: 563.090637`, `servo_move_angle: 30`, parking -23, no `gate_autoload`, no per-gate entry sensor, PRELOAD/UNLOAD end tips -2.883/-2.648, EJECT tip -22.648, LOAD ending at `filament_pos: 10` with no errors. 2.0: 8 gates, `show_bypass: 1`, 80:20 (same `steps_per_mm`), `servo_move_angle: 50`, parking -13, `gate_autoload: 1`, per-gate `mmu_entry_switch_pin_0..7`, end tips 6.717/7.577/-12.423, and LOAD/UNLOAD/EJECT each end with the recorded error 'Failed to reach extruder encoder endstop after moving 112.5mm'. 3.0: 8 gates, `show_bypass: 1`, `gear_ratio: 1:1` -> `steps_per_mm: 140.772659`, `servo_move_angle: 50`, parking -16, `gate_autoload: 1`, per-gate entry sensors, end tips 4.210/4.121/-15.879, and the same error at 117.0mm. Encoder defaults by version: TCRT5000 / Binky-8 / Binky-12 (Kconfig.ercf:180-186, :224-231, :267-274). The 2.0/3.0 failure tracks the encoder model, not the machine: controlled probes hold it to the encoder choice alone -- 1.1 with Binky-8 fails identically at 112.5 mm, and 2.0/3.0 with TCRT5000 complete on either board (EASY_BRD_RP2040 or MMB_2_0) -- the harness collision-homing loop cannot converge at the coarser Binky step. The devices' real encoders are kept as shipped; swapping them to TCRT5000 to make the harness pass would falsify the hardware. v3.0's optional Jack Rabbit mod renders a plain `LinearSelector` (no servo) and sets `filament_always_gripped` (Kconfig.ercf:98-105); it is not selected here.

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
| `vendor` | `ERCF` |
| `version` | `1.1` |
| `num_gates` | `9` |
| `display_name` | `ERCF-0` |
| `encoder` | `unit0` |
| `toolhead` | `default` |
| `extruder_name` | `extruder` |
| `selector_stepper` | `unit0_selector` |
| `selector_servo` | `unit0_selector_servo` |
| `gear_stepper` | `unit0_gear` |
| `selector_type` | `LinearServoSelector` |
| `variable_bowden_lengths` | `0` |
| `variable_rotation_distances` | `1` |
| `require_bowden_move` | `1` |
| `filament_always_gripped` | `0` |
| `show_bypass` | `0` |

### `[mmu_stepper unit0_gear]`

| Key | Value |
|---|---|
| `step_pin` | `unit0:PD4` |
| `dir_pin` | `unit0:PD3` |
| `enable_pin` | `!unit0:PD5` |
| `rotation_distance` | `22.7316868` |
| `gear_ratio` | `80:20` |
| `microsteps` | `16` |
| `full_steps_per_rotation` | `200` |
| `extra_endstops` | `` |
| `mmu_gear_touch=tmc2209_unit0_gear` | `virtual_endstop` |

### `[mmu_sensors unit0]`

| Key | Value |
|---|---|
| `mmu_shared_exit_switch_pin` | `` |
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
| `selector_move_speed` | `200` |
| `selector_homing_speed` | `60` |
| `selector_touch_speed` | `80` |
| `selector_accel` | `1200` |
| `selector_touch_enabled` | `0` |
| `servo_up_angle` | `30` |
| `servo_down_angle` | `100` |
| `servo_move_angle` | `30` |
| `servo_buzz_gear_on_down` | `3` |
| `servo_duration` | `0.4` |
| `servo_dwell` | `0.5` |
| `servo_always_active` | `0` |
| `servo_active_down` | `0` |
| `gear_load_speed` | `100` |
| `gear_load_accel` | `100` |
| `gear_from_filament_buffer_speed` | `150` |
| `gear_from_filament_buffer_accel` | `400` |
| `gear_unload_speed` | `80` |
| `gear_unload_accel` | `100` |
| `gear_homing_speed` | `50` |
| `virtual_sensor_homing_speed` | `15` |
| `gear_short_move_speed` | `80` |
| `gear_short_move_accel` | `400` |
| `gear_short_move_threshold` | `70` |
| `gate_homing_endstop` | `encoder` |
| `gate_homing_max` | `100` |
| `gate_parking_distance` | `-23` |
| `gate_preload_endstop` | `` |
| `gate_preload_homing_max` | `100` |
| `gate_preload_parking_distance` | `-23` |
| `gate_preload_attempts` | `2` |
| `gate_endstop_to_encoder` | `10` |
| `gate_load_attempts` | `2` |
| `gate_final_eject_distance` | `20` |
| `bowden_homing_max` | `2000` |
| `bowden_load_homing_buffer` | `20` |
| `bowden_unload_homing_buffer` | `40` |
| `bowden_apply_correction` | `0` |
| `bowden_allowable_encoder_delta` | `20.0` |
| `bowden_pre_unload_test` | `1` |
| `bowden_pre_unload_error_tolerance` | `50` |
| `bowden_move_error_tolerance` | `60` |
| `extruder_homing_endstop` | `encoder` |
| `extruder_homing_max` | `100` |
| `extruder_collision_homing_current` | `30` |
| `toolhead_unload_safety_margin` | `10` |
| `toolhead_post_load_tighten` | `60` |
| `toolhead_post_load_tension_adjust` | `1` |
| `toolhead_entry_tension_test` | `1` |
| `toolhead_move_error_tolerance` | `60` |
| `sync_to_extruder` | `0` |
| `sync_gear_current` | `100` |
| `sync_form_tip` | `0` |
| `sync_purge` | `0` |
| `flowguard_enabled` | `1` |
| `flowguard_encoder_mode` | `0` |
| `flowguard_encoder_max_motion` | `20` |
| `autocal_bowden_length` | `0` |
| `autotune_bowden_length` | `0` |
| `skip_cal_rotation_distance` | `1` |
| `autotune_rotation_distance` | `0` |
| `skip_cal_encoder` | `0` |
| `startup_home_selector` | `0` |
| `encoder_move_validation` | `1` |

Steps/mm (gear): `563.090636986957`

## Operations

### PRELOAD

| Phase | Motor | Homing | Endstop | Dist | Speed | Accel | Actual | Homed |
|---|---|---|---|---:|---:|---:|---:|---|
| None | gear | 0 |  | 0.80 | 25.0 | 1000.0 | 0.80 | False |
| None | gear | 0 |  | -0.80 | 25.0 | 1000.0 | -0.80 | False |
| None | gear | 0 |  | 0.80 | 25.0 | 1000.0 | 0.80 | False |
| None | gear | 0 |  | -0.80 | 25.0 | 1000.0 | -0.80 | False |
| None | gear | 0 |  | 0.80 | 25.0 | 1000.0 | 0.80 | False |
| None | gear | 0 |  | -0.80 | 25.0 | 1000.0 | -0.80 | False |
| Initial load into encoder | gear | 0 |  | 100.00 | 100.0 | 100.0 | 100.00 | False |
| Unloading step #1 from encoder | gear | 0 |  | -15.00 | 80.0 | 400.0 | -15.00 | False |
| Unloading step #2 from encoder | gear | 0 |  | -15.00 | 80.0 | 400.0 | -15.00 | False |
| Unloading step #3 from encoder | gear | 0 |  | -15.00 | 80.0 | 400.0 | -15.00 | False |
| Final parking | gear | 0 |  | -17.88 | 80.0 | 400.0 | -17.88 | False |

### SELECT

_No motion (already in the requested state)._

### LOAD

| Phase | Motor | Homing | Endstop | Dist | Speed | Accel | Actual | Homed |
|---|---|---|---|---:|---:|---:|---:|---|
| None | gear | 0 |  | 0.80 | 25.0 | 1000.0 | 0.80 | False |
| None | gear | 0 |  | -0.80 | 25.0 | 1000.0 | -0.80 | False |
| None | gear | 0 |  | 0.80 | 25.0 | 1000.0 | 0.80 | False |
| None | gear | 0 |  | -0.80 | 25.0 | 1000.0 | -0.80 | False |
| None | gear | 0 |  | 0.80 | 25.0 | 1000.0 | 0.80 | False |
| None | gear | 0 |  | -0.80 | 25.0 | 1000.0 | -0.80 | False |
| Initial load into encoder | gear | 0 |  | 100.00 | 100.0 | 100.0 | 100.00 | False |
| Fast loading move through bowden | gear | 0 |  | 603.06 | 100.0 | 100.0 | 603.06 | False |
| Homing step #1 | gear | 0 |  | 2.40 | 50.0 | 400.0 | 2.40 | False |
| Homing step #2 | gear | 0 |  | 2.40 | 50.0 | 400.0 | 2.40 | False |
| Homing step #3 | gear | 0 |  | 2.40 | 50.0 | 400.0 | 2.40 | False |
| Homing step #4 | gear | 0 |  | 2.40 | 50.0 | 400.0 | 2.40 | False |
| Homing step #5 | gear | 0 |  | 2.40 | 50.0 | 400.0 | 2.40 | False |
| Homing step #6 | gear | 0 |  | 2.40 | 50.0 | 400.0 | 2.40 | False |
| Homing step #7 | gear | 0 |  | 2.40 | 50.0 | 400.0 | 2.40 | False |
| Homing step #8 | gear | 0 |  | 2.40 | 50.0 | 400.0 | 2.40 | False |
| Homing step #9 | gear | 0 |  | 2.40 | 50.0 | 400.0 | 2.40 | False |
| Loading filament to nozzle | gear+extruder | 0 |  | 68.00 | 18.0 | 400.0 | 68.00 | False |

### UNLOAD

| Phase | Motor | Homing | Endstop | Dist | Speed | Accel | Actual | Homed |
|---|---|---|---|---:|---:|---:|---:|---|
| Unloading extruder | extruder | 0 |  | -33.01 | 16.0 | 1500.0 | -33.01 | False |
| None | gear | 0 |  | 0.80 | 25.0 | 1000.0 | 0.80 | False |
| None | gear | 0 |  | -0.80 | 25.0 | 1000.0 | -0.80 | False |
| None | gear | 0 |  | 0.80 | 25.0 | 1000.0 | 0.80 | False |
| None | gear | 0 |  | -0.80 | 25.0 | 1000.0 | -0.80 | False |
| None | gear | 0 |  | 0.80 | 25.0 | 1000.0 | 0.80 | False |
| None | gear | 0 |  | -0.80 | 25.0 | 1000.0 | -0.80 | False |
| Fast unloading move through bowden | gear | 0 |  | -660.00 | 80.0 | 100.0 | -660.00 | False |
| Unloading step #1 from encoder | gear | 0 |  | -15.00 | 80.0 | 400.0 | -15.00 | False |
| Unloading step #2 from encoder | gear | 0 |  | -15.00 | 80.0 | 400.0 | -15.00 | False |
| Unloading step #3 from encoder | gear | 0 |  | -15.00 | 80.0 | 400.0 | -15.00 | False |
| Final parking | gear | 0 |  | -9.41 | 80.0 | 400.0 | -9.41 | False |

### EJECT

| Phase | Motor | Homing | Endstop | Dist | Speed | Accel | Actual | Homed |
|---|---|---|---|---:|---:|---:|---:|---|
| None | gear | 0 |  | 0.80 | 25.0 | 1000.0 | 0.80 | False |
| None | gear | 0 |  | -0.80 | 25.0 | 1000.0 | -0.80 | False |
| None | gear | 0 |  | 0.80 | 25.0 | 1000.0 | 0.80 | False |
| None | gear | 0 |  | -0.80 | 25.0 | 1000.0 | -0.80 | False |
| None | gear | 0 |  | 0.80 | 25.0 | 1000.0 | 0.80 | False |
| None | gear | 0 |  | -0.80 | 25.0 | 1000.0 | -0.80 | False |
| Ejecting filament out of gate | gear | 0 |  | -20.00 | 80.0 | 400.0 | -20.00 | False |

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
