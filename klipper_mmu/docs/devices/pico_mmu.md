# pico_mmu

Vendor: **PicoMMU**, 4 gates, selector `ServoSelector`. Traced against Happy Hare `c54c736`, harness profile `pico_mmu`.

## Decisions

- **selector**: ServoSelector (`select SERVO_SELECTOR`, installer/mmu_types/Kconfig.pico_mmu:7): one servo both selects the gate and grips the filament through a cam -- the type's help says it 'uses a single servo and cam mechanism to both select and grip the filament in the chosen gate. It is a simple compact design but is limited to about 4 gates' (Kconfig.pico_mmu:3-6). The type also `select`s PARAM_FILAMENT_ALWAYS_GRIPPED, PARAM_SYNC_TO_EXTRUDER, PARAM_SYNC_FORM_TIP and PARAM_SYNC_PURGE (Kconfig.pico_mmu:8-11), so the render carries `filament_always_gripped: 1`. The render names `selector_servo: unit0_selector_servo` with no selector `[mmu_stepper]`. klipper-mmu drives it as a SERVO selector (`KLIPPER_MMU_SELECTOR_KIND_SERVO`) on `KLIPPER_MMU_PINS_SERVO`, Klipper's scheduled software PWM on any GPIO, with the ES3004's 140 degree range and a 1.2 s dwell. It ships no gate angles, like Happy Hare: the image boots, but selecting a gate is refused ('gate N is not calibrated') until its angle is saved with `KLIPPER_MMU_SERVO` and `KLIPPER_MMU_CALIBRATE_SELECTOR`. Software candidate: it builds and passes the simulators, but has not run on real hardware.
- **lane_count**: 4 gates: `PARAM_NUM_GATES` default 4 (installer/mmu_types/Kconfig.pico_mmu:32), matching the help's 'limited to about 4 gates' (Kconfig.pico_mmu:5-6). Recorded as `device.gates: 4`.
- **gate_positions**: No gate angles ship, deliberately: `PARAM_SERVO_GATE_ANGLES` defaults to the empty string, and the type's own comment (installer/mmu_types/Kconfig.pico_mmu:51-54) says 'Gate angles depend on the particular cam build. The generic four-gate default assumes 360 degree travel, but PicoMMU's ES3004 is limited to 140 degrees. Leave them unseeded so Happy Hare boots safely and requires the user to calibrate the actual mechanism.' The render records the empty table with `servo_max_angle: 140` (the ES3004 limit), and the recorded consequence is the calibration refusal under `arrival`. So klipper-mmu cannot seed this device's per-gate angles from Happy Hare either: the table starts empty and the angles saved by `KLIPPER_MMU_CALIBRATE_SELECTOR` supply it. That is a recorded gap, not a fabrication.
- **arrival**: Nothing arrives, because the selector is uncalibrated by design. `gate_homing_endstop: mmu_shared_exit` is configured, and LOAD's only move is `Initial homing to unit0:mmu_shared_exit sensor`, which consumes `gate_homing_max: 100` mm (installer/mmu_types/Kconfig.pico_mmu:45) and never trips it: `actual: 100.0`, `homed: false`, `tip_before`/`tip_after` null, ending at `operations.load.end.tip = -40`, `filament_pos: 0`, `gate_status: -1` with 'Couldn't pick up filament at gate (gate endstop didn't trigger). Gate marked as empty.' PRELOAD, SELECT, UNLOAD and EJECT record 0 moves at all. `extruder_homing_endstop: none`, so unlike Low Rider the failure is at the gate, not the toolhead. klipper-mmu has the same requirement (an uncalibrated gate is refused until its angle is saved). Happy Hare's message: 'Calibration steps are not complete for MMU unit0: Required: Use MMU_CALIBRATE_SELECTOR to calibrate selector for gates: 0,1,2,3' (`ServoSelector.get_uncalibrated_gates`, extras/mmu/unit/selectors/mmu_servo_selector.py:262).
- **preload_park**: Configured but never reached: `gate_preload_parking_distance: -25` mm (installer/mmu_types/Kconfig.pico_mmu:47) would park PRELOAD 25 mm behind the gate switch as Low Rider does, but the uncalibrated selector means PRELOAD records 0 moves and leaves `end.tip = -40` (the filament's starting position), so the park is never performed.
- **unload_park**: Configured but never reached: `gate_parking_distance: -25` mm behind the gate switch; UNLOAD records 0 moves with `gate_status: -1`, so no park is performed.
- **eject_extra**: Configured but never reached: the render's `gate_final_eject_distance: 20` mm; EJECT records 0 moves, so no extra gate clear is performed.
- **harness_artifact**: Harness artifacts. The trace's `errors` list repeats the calibration warning ('Calibration steps are not complete ... gates: 0,1,2,3', then '... gate: 0') in every operation, and `gate_status: -1` (unknown) is the recorded gate state throughout -- HH refuses to treat the gate as selected or as empty-by-record. The layout's `mmu_pre_gate`/`mmu_entry` = -150 and the -40 tip are positions on the harness's 1-D filament path, not measurements of hardware; no endstop was ever tripped. `[mmu_sensors unit0]` declares only `mmu_shared_exit_switch_pin`, so this device has no per-gate switches either.

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
| `vendor` | `PicoMMU` |
| `version` | `1.0` |
| `num_gates` | `4` |
| `display_name` | `PicoMMU-0` |
| `toolhead` | `default` |
| `extruder_name` | `extruder` |
| `selector_servo` | `unit0_selector_servo` |
| `gear_stepper` | `unit0_gear` |
| `selector_type` | `ServoSelector` |
| `variable_bowden_lengths` | `0` |
| `variable_rotation_distances` | `0` |
| `require_bowden_move` | `1` |
| `filament_always_gripped` | `1` |
| `show_bypass` | `0` |

### `[mmu_stepper unit0_gear]`

| Key | Value |
|---|---|
| `step_pin` | `unit0:PD4` |
| `dir_pin` | `unit0:PD3` |
| `enable_pin` | `!unit0:PD5` |
| `rotation_distance` | `22.7316868` |
| `gear_ratio` | `1.28:1` |
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
| `servo_min_angle` | `0` |
| `servo_max_angle` | `140` |
| `servo_gate_angles` | `` |
| `servo_bypass_angle` | `-1` |
| `servo_release_angle` | `-1` |
| `servo_duration` | `0.6` |
| `servo_dwell` | `1.2` |
| `servo_always_active` | `0` |
| `gear_load_speed` | `100` |
| `gear_load_accel` | `100` |
| `gear_unload_speed` | `80` |
| `gear_unload_accel` | `100` |
| `gear_homing_speed` | `80` |
| `virtual_sensor_homing_speed` | `15` |
| `gear_short_move_speed` | `80` |
| `gear_short_move_accel` | `400` |
| `gear_short_move_threshold` | `70` |
| `gate_homing_endstop` | `mmu_shared_exit` |
| `gate_homing_max` | `100` |
| `gate_parking_distance` | `-25` |
| `gate_preload_endstop` | `` |
| `gate_preload_homing_max` | `100` |
| `gate_preload_parking_distance` | `-25` |
| `gate_preload_attempts` | `2` |
| `gate_final_eject_distance` | `20` |
| `bowden_homing_max` | `2000` |
| `bowden_load_homing_buffer` | `20` |
| `bowden_unload_homing_buffer` | `40` |
| `extruder_homing_endstop` | `none` |
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
| `autocal_bowden_length` | `0` |
| `autotune_bowden_length` | `0` |
| `skip_cal_rotation_distance` | `1` |
| `autotune_rotation_distance` | `0` |

Steps/mm (gear): `180.1890038358262`

## Operations

### PRELOAD

_No motion (already in the requested state)._

Errors: ['!! Warning: Calibration steps are not complete for MMU unit0:\nRequired:\n- Use MMU_CALIBRATE_SELECTOR to calibrate selector for gates: 0,1,2,3', '!! Warning: Calibration steps are not complete for MMU unit0:\nRequired:\n- Use MMU_CALIBRATE_SELECTOR to calibrate selector for gates: 0']

### SELECT

_No motion (already in the requested state)._

Errors: ['!! Warning: Calibration steps are not complete for MMU unit0:\nRequired:\n- Use MMU_CALIBRATE_SELECTOR to calibrate selector for gates: 0,1,2,3', '!! Warning: Calibration steps are not complete for MMU unit0:\nRequired:\n- Use MMU_CALIBRATE_SELECTOR to calibrate selector for gates: 0', '!! Warning: Calibration steps are not complete for MMU unit0:\nRequired:\n- Use MMU_CALIBRATE_SELECTOR to calibrate selector for gates: 0']

### LOAD

| Phase | Motor | Homing | Endstop | Dist | Speed | Accel | Actual | Homed |
|---|---|---|---|---:|---:|---:|---:|---|
| Initial homing to unit0:mmu_shared_exit sensor | gear | 1 | unit0:mmu_shared_exit | 100.00 | 80.0 | 100.0 | 100.00 | False |

Ends at tip `-40.00` = `unit0:mmu_shared_exit` - `50.00` mm (filament_pos=0, gate_status=-1).

Errors: ['!! Warning: Calibration steps are not complete for MMU unit0:\nRequired:\n- Use MMU_CALIBRATE_SELECTOR to calibrate selector for gates: 0,1,2,3', '!! Warning: Calibration steps are not complete for MMU unit0:\nRequired:\n- Use MMU_CALIBRATE_SELECTOR to calibrate selector for gates: 0', '!! Warning: Calibration steps are not complete for MMU unit0:\nRequired:\n- Use MMU_CALIBRATE_SELECTOR to calibrate selector for gates: 0', '!! Selected gate is not mapped to any tool. Will load filament but be sure to use MMU_TTG_MAP to assign tool', "!! MMU issue: Load sequence failed because:\nCouldn't pick up filament at gate (gate endstop didn't trigger)\nGate marked as empty. Use 'MMU_GATE_MAP GATE=-1 AVAILABLE=1' to reset.\nOccurred when loading tool: > Unknown"]

### UNLOAD

_No motion (already in the requested state)._

Errors: ['!! Warning: Calibration steps are not complete for MMU unit0:\nRequired:\n- Use MMU_CALIBRATE_SELECTOR to calibrate selector for gates: 0,1,2,3', '!! Warning: Calibration steps are not complete for MMU unit0:\nRequired:\n- Use MMU_CALIBRATE_SELECTOR to calibrate selector for gates: 0', '!! Warning: Calibration steps are not complete for MMU unit0:\nRequired:\n- Use MMU_CALIBRATE_SELECTOR to calibrate selector for gates: 0', '!! Selected gate is not mapped to any tool. Will load filament but be sure to use MMU_TTG_MAP to assign tool', "!! MMU issue: Load sequence failed because:\nCouldn't pick up filament at gate (gate endstop didn't trigger)\nGate marked as empty. Use 'MMU_GATE_MAP GATE=-1 AVAILABLE=1' to reset.\nOccurred when loading tool: > Unknown"]

### EJECT

_No motion (already in the requested state)._

Errors: ['!! Warning: Calibration steps are not complete for MMU unit0:\nRequired:\n- Use MMU_CALIBRATE_SELECTOR to calibrate selector for gates: 0,1,2,3', '!! Warning: Calibration steps are not complete for MMU unit0:\nRequired:\n- Use MMU_CALIBRATE_SELECTOR to calibrate selector for gates: 0', '!! Warning: Calibration steps are not complete for MMU unit0:\nRequired:\n- Use MMU_CALIBRATE_SELECTOR to calibrate selector for gates: 0', '!! Selected gate is not mapped to any tool. Will load filament but be sure to use MMU_TTG_MAP to assign tool', "!! MMU issue: Load sequence failed because:\nCouldn't pick up filament at gate (gate endstop didn't trigger)\nGate marked as empty. Use 'MMU_GATE_MAP GATE=-1 AVAILABLE=1' to reset.\nOccurred when loading tool: > Unknown"]

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
