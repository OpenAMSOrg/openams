# quattrobox_1_1

Vendor: **Other**, 4 gates, selector `VirtualSelector`. Traced against Happy Hare `c54c736`, harness profile `quattrobox_1_1`.

## Decisions

- **template_choice**: Cross-checked against the pinned AFC NEMA-14 template templates/AFC_QuattroBox_14.cfg through the config/mcu/MMB_2.0_QB.cfg alias. AFC's filenames encode the motor, not a hardware revision -- include/install_functions.sh:123-145 selects AFC_QuattroBox_14.cfg for NEMA-14 geared motors (gear_ratio 50:10) and _17.cfg for NEMA-17 direct drive (1:1), and neither template's own header names a version. Happy Hare's QuattroBox v1.x declares a geared train (installer/mmu_types/Kconfig.quattro_box PARAM_GEAR_GEAR_RATIO 50:17), so the geared NEMA-14 template is the match; v2.0 (PARAM_GEAR_GEAR_RATIO 1:1) pairs with _17. The rendered [mmu_stepper unit0_gear] shows gear_ratio 1:1 for every version -- the harness does not carry PARAM_GEAR_GEAR_RATIO into the trace -- so the geared/direct signal is the Kconfig header, not the rendered field.
- **arrival**: Happy Hare's LOAD homes the bowden to the shared-exit switch (unit0:mmu_shared_exit, layout 10mm) and then runs the last bowden leg as a plain move -- one hub switch serves all four lanes, which is exactly the hub-sensor role klipper-mmu's has_hub_sensor/PINS_HUB covers. AFC's arrival concept is its [AFC_extruder] pin_tool_start ('enter_tool_pin', AFC_Hardware-QuattroBox.cfg), which is not part of the lane template, so the compare shows arrival as hh-only. Default: HH's shared-exit homing; the toolhead-entrance switch stays host wiring.
- **buffer_type**: AFC configures a switched advance/trailing buffer by default (the Turtleneck its comment names: [AFC_buffer QuattroBox_1] advance_pin/trailing_pin, multiplier_high/low 1.05/0.95). Happy Hare's QuattroBox trace renders no [mmu_buffer] section at all, so the compare shows buffer_type as afc-only. klipper-mmu follows the extruder from a switched buffer like this one (`KLIPPER_MMU_PINS_BUFFER_A/B`), but does not use it for ramming arrival: LOAD ends at a local toolhead switch or at FPS arrival.
- **encoder**: The QuattroBox family header selects MMU_HAS_ENCODER (installer/mmu_types/Kconfig.quattro_box), but the rendered unit0 hardware has no encoder node or pins -- the harness does not wire one. klipper-mmu's optional encoder input (`KLIPPER_MMU_ENCODER`) stays off by default for QuattroBox, and it has no encoder-homing mode.
- **espooler**: QuattroBox is described as a Filamentalist respooler driven by the gear steppers (Kconfig.quattro_box help), but the trace renders no [mmu_espooler] section or assist pins. klipper-mmu's powered rewind (`KLIPPER_MMU_POWERED_REWIND`) is on by default only for BoxTurtle and Claymore; QuattroBox runs without espooler assist.
- **preload_park**: Happy Hare parks PRELOAD 23mm behind the shared-exit switch (measured: tip -13 = shared exit 10 - 23; gate_preload_parking_distance -23). AFC's park_dist is 10mm. Default applied: HH's 23mm (the executable reference); AFC's 10mm recorded as the difference.
- **unload_park**: Happy Hare parks UNLOAD 23mm behind the shared-exit switch as well (measured tip -13). AFC's hub_clear_move_dis is 65mm. Same direction, very different magnitude; default applied: HH's 23mm.
- **long_speed**: AFC's global long_moves_speed/accel is 150/250 mm/s (config/AFC.cfg; the QuattroBox template sets no unit override). Happy Hare's gear_load_speed/accel is 100/100. Default applied: HH's 100/100.
- **short_speed**: AFC's short_moves_speed/accel is 50/300; Happy Hare's gear_short_move_speed/accel is 80/400. Default applied: HH's 80/400 (the executable reference).
- **bowden**: AFC carries real lengths (afc_bowden_length 1145.0mm plus per-lane dist_hub 40-95mm in the NEMA-14 template); Happy Hare's dossier has no genuine bowden number (variable_bowden_lengths=0, require_bowden_move=1, autocal_bowden_length=0), so the compare row is afc-only. klipper-mmu's fps_calibrated_steps is one value per unit today, not per lane -- host-owned; gap recorded, not fabricated.

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
| `vendor` | `Other` |
| `version` | `1.0` |
| `num_gates` | `4` |
| `display_name` | `unit0` |
| `toolhead` | `default` |
| `extruder_name` | `extruder` |
| `gear_steppers` | `unit0_gear, unit0_gear_1, unit0_gear_2, unit0_gear_3` |
| `selector_type` | `VirtualSelector` |
| `variable_bowden_lengths` | `0` |
| `variable_rotation_distances` | `0` |
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
| `gear_ratio` | `1:1` |
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
| `gate_homing_max` | `100` |
| `gate_parking_distance` | `-23` |
| `gate_preload_endstop` | `` |
| `gate_preload_homing_max` | `100` |
| `gate_preload_parking_distance` | `-23` |
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
| `sync_to_extruder` | `0` |
| `sync_gear_current` | `100` |
| `sync_form_tip` | `0` |
| `sync_purge` | `0` |
| `autocal_bowden_length` | `0` |
| `autotune_bowden_length` | `0` |
| `skip_cal_rotation_distance` | `1` |
| `autotune_rotation_distance` | `0` |

Steps/mm (gear): `140.77265924673924`

## Operations

### PRELOAD

| Phase | Motor | Homing | Endstop | Dist | Speed | Accel | Actual | Homed |
|---|---|---|---|---:|---:|---:|---:|---|
| Initial homing to unit0:mmu_shared_exit sensor | gear | 1 | unit0:mmu_shared_exit | 100.00 | 50.0 | 100.0 | 50.00 | True |
| Final parking | gear | 0 |  | -23.00 | 80.0 | 400.0 | -23.00 | False |

Ends at tip `-13.00` = `unit0:mmu_shared_exit` - `23.00` mm (filament_pos=0, gate_status=1).

### SELECT

_No motion (already in the requested state)._

### LOAD

| Phase | Motor | Homing | Endstop | Dist | Speed | Accel | Actual | Homed |
|---|---|---|---|---:|---:|---:|---:|---|
| Initial homing to unit0:mmu_shared_exit sensor | gear | 1 | unit0:mmu_shared_exit | 100.00 | 50.0 | 100.0 | 23.00 | True |
| Fast loading move through bowden | gear | 0 |  | 700.00 | 100.0 | 100.0 | 700.00 | False |
| Slow move to extruder entrance | gear | 0 |  | 0.00 | 80.0 | 400.0 | 0.00 | False |
| Loading filament to nozzle | gear+extruder | 0 |  | 68.00 | 18.0 | 400.0 | 68.00 | False |

Ends at tip `778.00` = `unit0:mmu_shared_exit` + `768.00` mm (filament_pos=10, gate_status=1).

### UNLOAD

| Phase | Motor | Homing | Endstop | Dist | Speed | Accel | Actual | Homed |
|---|---|---|---|---:|---:|---:|---:|---|
| Unloading extruder | gear+extruder | 0 |  | -33.01 | 18.0 | 400.0 | -33.01 | False |
| Fast unloading move through bowden | gear | 0 |  | -650.00 | 80.0 | 100.0 | -650.00 | False |
| Reverse homing off unit0:mmu_shared_exit sensor | gear | -1 | unit0:mmu_shared_exit | -140.00 | 50.0 | 100.0 | -39.99 | True |
| Final parking | gear | 0 |  | -23.00 | 80.0 | 400.0 | -23.00 | False |

Ends at tip `-13.00` = `unit0:mmu_shared_exit` - `23.00` mm (filament_pos=0, gate_status=2).

### EJECT

| Phase | Motor | Homing | Endstop | Dist | Speed | Accel | Actual | Homed |
|---|---|---|---|---:|---:|---:|---:|---|
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
