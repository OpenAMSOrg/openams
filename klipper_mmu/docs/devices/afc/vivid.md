# AFC ViViD — the unit as its own files describe it

ViViD is the unit where AFC decided the hub does not need a switch, because the four
lane load switches already are one. Everything below is read straight out of the pinned
AFC tree (revision `5ea84ea6fab75b9b49db090f309154d1e52272bb`, extracted read-only under
`build/upstream/afc/`); paths are relative to the AFC tree root and every claim carries a
`file:line`. Sections 1–3 are what the files say; section 4 is the klipper-mmu mapping
(observation vs inference); section 5 is where AFC's own files disagree with themselves.

## 1. Files that define it

Unit class and base chain:

- `extras/AFC_vivid.py:34` — `class AFC_vivid(afcBoxTurtle)`.
- `extras/AFC_BoxTurtle.py:29` — `class afcBoxTurtle(afcUnit)`.
- `extras/AFC_unit.py:46` — `class afcUnit` (base; the per-lane hooks live here).
- `extras/AFC_vivid.py:353` — `load_config_prefix` returns `AFC_vivid`, i.e. the
  `[AFC_vivid <name>]` section type.

Templates:

- `templates/AFC_Vivid_1.cfg` — the working config: `[include mcu/Vivid_1.cfg]` (`:1`),
  `[AFC_vivid Vivid_1]` (`:3`), unit-level drive/selector steppers, four `[AFC_lane]`
  blocks, `[AFC_buffer]`, `[AFC_hub]`, two `[AFC_led]` chains, plus board housekeeping
  (controller fan, PTC thermistor, two AHT3X dryer sensors, dryer heater, `verify_heater`,
  heater fan).

MCU alias files:

- `config/mcu/Vivid.cfg` — `[mcu Vivid_1]` (`:1`), `[mcu Vivid_1_buffer]` (`:8`),
  `[board_pins Vivid_1]` (`:15`), `[board_pins Vivid_1_buffer]` (`:30`). So ViViD is a
  **two-MCU** unit: `Vivid_1` carries drive/selector/prep/RGB/dryer, and `Vivid_1_buffer`
  carries the four load sensors and the buffer's ADV/TRAIL switches (`config/mcu/Vivid.cfg:33-34`).
- The template's `mcu/Vivid_1.cfg` include name is produced by the installer, not shipped:
  `include/unit_functions.sh:200-203` copies `config/mcu/Vivid.cfg` to `mcu/${name}.cfg`
  and rewrites the include; also `include/install_functions.sh:82-84`. The template default
  unit name is `Vivid_1` (`include/unit_functions.sh:68-71`).

## 2. Hooks it overrides relative to its base

Base is `afcBoxTurtle`. ViViD overrides:

- `_move_lane` (`extras/AFC_vivid.py:90`) — replaces BoxTurtle's wiggle test with a real
  load: if the lane's prep switch is set, home filament forward to the lane's **load**
  sensor over `dist_hub` at SHORT speed (`:102-103`), set `loaded_to_hub` on success, and
  retract `hub_clear_move_dis` when the lane is not in the toolhead (`:106-108`). A
  missing prep trigger or a failed home resets the lane to unloaded and clears the
  `loaded_to_hub`/`status`/`td1_data` state (`:110-120`).
- `system_Test` (`:127`) — calls the base test with `enable_movement=False`, so a ViViD
  self-test never spins the drive (`:128`).
- `select_lane` (`:159`) — the selector-cam hook (detail in §3).
- `prep_load` (`:201`) — full override of the preload sequence (detail in §3).
- `prep_post_load` (`:255`) — an explicit no-op (`:259-260`): ViViD does not run
  BoxTurtle's "move to hub after prep" step (`extras/AFC_BoxTurtle.py:611`).
- `unselect_lane` (`:262`, default `move_distance=50`) — rotates the selector forward by
  `move_distance` at selector homing speed/accel to release the filament (`:269-270`).
- `eject_lane` (`:272`) — selects the lane, then homes filament **backwards** over
  `dist_hub` (with a longer-Bowden correction when `dist_hub > 400`, `:285-287`) to the
  lane's **prep** endstop, then unselects and disables both steppers (`:288-292`).
- `move_to_hub` (`:294`) — moves with `endstop=lane.load_es` (`:320`): ViViD homes to the
  **lane load switch**, not a hub switch; docstring notes ViViD has no spoolers (`:299`).
- `calibrate_lane` (`:323`) — ejects the lane, clears `loaded_to_hub`/`calibrated_lane`,
  sets the "not ready" LED, and always returns `(True, "calibration_lane", 0)` (`:337-340`).
- `calibration_lane_message` (`:342`) — the "reinsert filament to calibrate" text.
- Two helpers its base does not have: `_get_lane_selector_state` (`:130`) and
  `_get_selector_enabled` (`:143`).

Not overridden, so base behaviour applies: `return_to_home` (`extras/AFC_unit.py:633`),
`load_then_home` (`extras/AFC_unit.py:861`), and `unit_load_lane`/`unit_unload_lane` — only
OpenAMS defines those, so ViViD takes `AFC.py`'s standard load/unload path
(`extras/AFC.py:1650-1651`, `:2051-2052`).

## 3. Template facts

From `templates/AFC_Vivid_1.cfg` and `config/mcu/Vivid.cfg`.

- **Lanes:** four (`[AFC_lane lane1..lane4]`, `:43`/`:51`/`:59`/`:67`); `unit:` is
  `Vivid_1:N` (`:44`/`:52`/`:60`/`:68`).
- **Per-lane pins:**
  - load — `Vivid_1_buffer:LOAD1..4` (`:47`/`:55`/`:63`/`:71`), aliases `LOAD1..4 = PA0..PA3`
    (`config/mcu/Vivid.cfg:33`); active-high as written (no `!`).
  - prep — `Vivid_1:PREP1..4` (`:48`/`:56`/`:64`/`:72`), aliases `PREP1..4 = PA0..PA3` on the
    *main* MCU (`config/mcu/Vivid.cfg:21`); active-high.
  - selector — `!Vivid_1:SEL1..4` (`:49`/`:57`/`:65`/`:73`), aliases
    `SEL1=PD0, SEL2=PA15, SEL3=PD1, SEL4=PC13` (`config/mcu/Vivid.cfg:22`); **inverted**.
  - there are **no per-lane step/dir/en/UART pins and no per-lane rewind/espooler pins** —
    both motors are unit-level.
  - `dist_hub: 2000` on every lane (`:45`/`:53`/`:61`/`:69`).
  - `led_index:` splits the two 14-LED chains: lane1/2 `AFC_Indicator_1:1-7` / `:8-14`
    (`:46`/`:54`), lane3/4 `AFC_Indicator_2:1-7` / `:8-14` (`:62`/`:70`).
  - `park_dist` is never set; the default is `10` and the source itself annotates it
    "Currently unused" (`extras/AFC_lane.py:202`).
- **Unit-level steppers:**
  - drive `[AFC_stepper Vivid_1_drive]` (`:13`): step/dir/en `Vivid_1:MDRV_STEP/DIR/EN`
    (`:15-17`, enable inverted), `microsteps: 16` (`:18`), `rotation_distance: 360` (`:19`),
    `gear_ratio: 43:1` (`:20`); tmc2209 `Vivid_1:MDRV_UART` (`:22-23`), `run_current: 0.8`
    (`:25`), `sense_resistor: 0.110` (`:26`).
  - selector `[AFC_stepper Vivid_1_selector]` (`:28`): `Vivid_1:MSEL_STEP/DIR/EN` (`:30-32`),
    `microsteps: 16` (`:33`), `rotation_distance: 400` (`:34`), `gear_ratio: 2.5:1` (`:35`);
    tmc2209 `run_current: 1.0` (`:40`), `sense_resistor: 0.110` (`:41`).
- **Hub:** `[AFC_hub Vivid_1]` (`:81`), `afc_bowden_length: 2000` (`:82`), `switch_pin:
  virtual` (`:83`). Virtual means the hub state is "any lane's load switch is triggered"
  (`extras/AFC_hub.py:82-91`, `:141-149`). `hub_clear_move_dis` is unset → default `65`
  (`extras/AFC_hub.py:43`); `afc_unload_bowden_length` defaults to `afc_bowden_length`
  (`:46`); `use_dist_hub` is unset → default `False` (`extras/AFC_hub.py:66`) — see §5.
- **Buffer:** `[AFC_buffer Vivid_1_buffer]` (`:75`), type unset → default `"switched"`
  (`extras/AFC_buffer.py:47`), `advance_pin: Vivid_1_buffer:ADV` / `trailing_pin:
  Vivid_1_buffer:TRAIL` (`:76-77`; aliases `TRAIL=PA4`, `ADV=PA5`, `config/mcu/Vivid.cfg:34`),
  `multiplier_high: 1.15`, `multiplier_low: 0.90` (`:78-79`). Only `switched` and `FPS_PSF`
  are accepted (`extras/AFC_buffer.py:1584-1592`); the installer keeps ViViD off the FPS_PSF
  path on purpose (`include/install_functions.sh:228-229`). Switched-buffer defaults, for
  contrast, are 1.1/0.9 (`extras/AFC_buffer.py:93-94`).
- **Speeds/accels:** unit `long_moves_speed: 120` (`:9`), `long_moves_accel: 50` (`:10`);
  this overrides the global defaults (`config/AFC.cfg:7-8`). `short_moves_speed: 50`,
  `short_moves_accel: 300`, `short_move_dis: 10` come from `config/AFC.cfg:9-11`.
  `enable_buffer_tool_check: True` (`:11`) overrides the base default `False`
  (`extras/AFC_unit.py:112`). Per-lane `dist_hub_move_speed/accel` therefore resolve to the
  LONG values because `dist_hub >= 200` (`extras/AFC_lane.py:691-692`).
- **Selector settings:** `selector_homing_speed: 150`, `selector_homing_accel: 150`,
  `max_selector_movement: 800` are class defaults, not set in the template
  (`extras/AFC_vivid.py:74-76`); `VALID_CAM_ANGLES = [30,45,60]` (`:51`),
  `CALIBRATION_DISTANCE = 5000` (`:52`), `LANE_OVERSHOOT = 200` (`:53`),
  `_eject_to_calibrate = True` (`:77`). Per-lane `selector_cal_distance` defaults to `0.0`
  (`extras/AFC_lane.py:284`). Selector homing is a real `do_homing_move` over
  `max_selector_movement * sel_dir` against the lane's `selector_endstop_name`
  (`extras/AFC_vivid.py:185-190`); `sel_prep=True` reverses the cam direction (`:169`, `:222`).
- **Extruder / arrival:** `templates/AFC_Hardware-AFC.cfg:4` — `[AFC_extruder extruder]`
  (`pin_tool_start: enter_tool_pin` `:5`, `tool_stn: 72` `:7`, `tool_stn_unload: 100` `:8`,
  `tool_sensor_after_extruder: 0` `:9`, `tool_unload_speed: 25` `:10`, `tool_load_speed: 25`
  `:11`). Globals
  `tool_homing_distance: 200`, `home_to_tool: True`, `homing_enabled: True`
  (`extras/AFC.py:237`, `:241`, `:242`).
- **No servo, no rewind:** `[AFC_hub]` has no `cut:`/servo block here, and the lanes have no
  `afc_motor_*` keys. `AFC_Hardware-AFC.cfg` only contains a *commented* `[servo tool_cut]`
  (`templates/AFC_Hardware-AFC.cfg`, commented block).
- Housekeeping present but not motion: controller fan (`:104`), PTC thermistor (`:109`),
  two AHT3X dryer sensors (`:116`, `:125`), `heater_generic Vivid_1_dryer` (`:135`),
  `verify_heater` (`:149`), heater fan (`:156`).

## 4. What klipper-mmu's firmware would have to reproduce

Labels: **observation** = directly readable in AFC; **inference** = a mapping judgement.

- **Lanes and per-lane switches — observation.** Four lanes, each with a prep switch and a
  load switch (`templates/AFC_Vivid_1.cfg:47-48` and siblings). Mapping: four lanes, a
  per-lane LOAD switch, and a per-lane PREP/TRG switch. The prep switch is the insert
  trigger; the load switch is the homing endstop (`extras/AFC_vivid.py:320`).
- **Shared path and hub — observation + inference.** Four lanes feed one hub and one Bowden
  (`afc_bowden_length: 2000`) to the toolhead (`templates/AFC_Vivid_1.cfg:81-83`). Mapping:
  the single shared path and the HUB concept. But AFC's hub here has **no switch** — the
  hub is "loaded" iff any lane load switch is on (`extras/AFC_hub.py:141-149`). Inference:
  klipper-mmu's firmware must synthesise HUB state from lane load switches for this unit,
  or not require a dedicated hub sensor at all.
- **PRELOAD — observation.** `prep_load` (`extras/AFC_vivid.py:201`) is the real preload:
  rotate the cam backwards with `sel_prep=True` (`:222`), then home forward to the load
  switch and, on first load, record `dist_hub = round(distance, 2) + LANE_OVERSHOOT` and
  write it back to the config (`:238-243`). Mapping: PRELOAD, with distance capture in the
  spirit of `KLIPPER_MMU_CALIBRATE MMU=<name> BAY=n`.
- **LOAD / UNLOAD — observation.** ViViD has no `unit_load_lane`, so `AFC.py`'s standard
  path runs (`extras/AFC.py:1650-1651`); the toolhead distance is `dist_hub` when the hub's
  `use_dist_hub` is true, else `afc_bowden_length` (`extras/AFC.py:1714`); unload uses
  `afc_unload_bowden_length` (`extras/AFC.py:2173`). Mapping: LOAD/UNLOAD = bowden length
  plus hub-clearance retract — the same shape klipper-mmu already has.
- **EJECT — observation.** `eject_lane` homes backward to the **prep** switch, then frees
  the selector (`extras/AFC_vivid.py:272-292`). Mapping: EJECT. Observation: it needs the
  per-lane prep switch *and* a bidirectional selector.
- **FOLLOW / buffer — observation + inference.** `[AFC_buffer]` is a **switched** two-pin
  buffer (ADV/TRAIL switches, 1.15/0.90) modulating extruder speed (`templates/AFC_Vivid_1.cfg:75-79`).
  Mapping: FOLLOW / FPS. Inference: a switched two-pin buffer is not an analog differential
  buffer, so klipper-mmu's FPS/DiffA-DiffB input would have to treat advance/trailing as a
  bang-bang tension signal, or this unit needs the two-switch FOLLOW variant.
- **Rewind / espooler — observation; no gap.** ViViD has none (`extras/AFC_vivid.py:299`;
  no `afc_motor_*` keys in the template). Mapping: *no* rewind outputs required. This is a
  unit where the rewind-output concept correctly has no counterpart.
- **Selector / cam — the big gap — observation + inference.** A dedicated stepper rotates a
  cam that homes against each lane's selector switch (`extras/AFC_vivid.py:159-199`),
  with a per-lane `selector_endstop_name` and a `selector_cal_dis` fine move
  (`:194-196`). Observation: klipper-mmu has no selector primitive; its own rollout plan
  files ViViD under "shared selector machines" needing "linear, rotary, servo or indexed
  selector/grip primitives" (`docs/plan/device-and-stm32-rollout.md:97`), with the ViViD row
  reading "Indexed selection and gate order, homing and collision checks"
  (`docs/plan/device-and-stm32-rollout.md:128`). Inference: ViViD cannot become an
  implemented profile until
  that primitive exists — none of LOAD/UNLOAD/HUB covers it.
- **Second MCU — observation.** Load and buffer switches live on `Vivid_1_buffer`, apart
  from the motor MCU. Mapping: fits klipper-mmu's model where BAY/HUB switches can sit on a
  different MCU than the lane motors — a supported arrangement, not a gap.
- **Servo / cut — observation.** Not present for ViViD (no `cut:` on the hub, no servo
  section). No counterpart needed.

## 5. Surprising or contradictory, in AFC's own files

- The template includes `mcu/Vivid_1.cfg` (`templates/AFC_Vivid_1.cfg:1`) but the shipped
  alias file is `config/mcu/Vivid.cfg`. Not a bug — the installer copies and rewrites the
  name (`include/unit_functions.sh:200-203`) — but it means the raw template cannot be
  included as-is.
- **`use_dist_hub` is not set for ViViD**, so it defaults `False` (`extras/AFC_hub.py:66`),
  yet ViViD's preload exists to calibrate and write back `dist_hub`
  (`extras/AFC_vivid.py:238-243`), and the standard load path then prefers
  `afc_bowden_length` over that measured `dist_hub` (`extras/AFC.py:1714`). They start equal
  (both 2000, `templates/AFC_Vivid_1.cfg:45`/`:82`), so the mismatch is invisible until a
  user changes one.
- `prep_post_load` is deliberately empty (`extras/AFC_vivid.py:255-260`) even though
  `prep_load` already moves filament toward the hub — the "post" half of prep is unused
  here, unlike BoxTurtle (`extras/AFC_BoxTurtle.py:611`).
- The `HUB_LOAD` G-code command is **not registered for a "ViViD"-typed unit** —
  `AFC.py` skips it when `unit_obj.type == "ViViD"` (`extras/AFC.py:391-392`). Consistent
  with a hub you cannot home to, but a ViViD user has no per-lane "load into hub" command.
- `park_dist` exists, defaults to `10`, and the source says "Currently unused"
  (`extras/AFC_lane.py:202`).
- ViViD's drive is `rotation_distance: 360`, `gear_ratio: 43:1`
  (`templates/AFC_Vivid_1.cfg:19-20`) — a different machine from Claymore's
  `35.1`/`40:20` (`templates/AFC_Claymore_1-AFC_Lite.cfg:27-28`) even though Claymore
  subclasses ViViD. The hierarchy is about control flow, not hardware.
- `eject_lane` reasons about `dist_hub > 400` using `hub_clear_move_dis` and a per-lane
  `homing_overshoot` (`extras/AFC_vivid.py:285-287`); with the shipped `dist_hub: 2000` this
  branch is the one that runs, so the correction is the *normal* path here, not an edge case.
