# AFC Claymore — the unit as its own files describe it

For Claymore, AFC is not merely the cross-check — it is the only written source. The
Claymore class is a thin subclass of ViViD that rips out ViViD's moving-parts hooks and
points them back at BoxTurtle, adds four powered rewind motors, and drops the prep sensor
entirely. Everything below is read from the pinned AFC tree (revision
`5ea84ea6fab75b9b49db090f309154d1e52272bb`, extracted read-only under `build/upstream/afc/`);
paths are relative to the AFC tree root and every claim carries a `file:line`.

## 1. Files that define it

Unit class and base chain:

- `extras/AFC_Claymore.py:35` — `class AFC_Claymore(AFC_vivid)`.
- `extras/AFC_vivid.py:34` → `extras/AFC_BoxTurtle.py:29` → `extras/AFC_unit.py:46`
  (`AFC_vivid(afcBoxTurtle)`, `afcBoxTurtle(afcUnit)`).
- `extras/AFC_Claymore.py:128` — `load_config_prefix` returns `AFC_Claymore`, i.e. the
  `[AFC_Claymore <name>]` section type.

Templates:

- `templates/AFC_Claymore_1-AFC_Lite.cfg` — the only shipped Claymore template (AFC-Lite
  board variant): `[include mcu/AFC_Lite_Claymore.cfg]` (`:1`), the unit's own
  `[mcu Claymore_1]` block (`:3`; `serial`/`canbus_uuid` left as placeholders, `:4-5`), a
  `[temperature_sensor Claymore_1]` (`:7`), `[AFC_Claymore Claymore_1]` (`:11`),
  drive/selector steppers, four `[AFC_lane]` blocks with rewinds, `[AFC_hub]`,
  `[AFC_buffer]`, one `[AFC_led]` chain. No fan/dryer/heater housekeeping. The default unit
  name is `Claymore_1` (`include/unit_functions.sh:35-38`).
- Unlike ViViD, the Claymore template declares its **own MCU inside the template**, so the
  include target is the board-pins alias file only.

MCU alias files:

- `config/mcu/AFC_Lite_Claymore.cfg` — `[board_pins afc_lite_claymore]` (`:1`) for
  `mcu: Claymore_1` (`:2`): `M1..M4_STEP/DIR/EN/UART` (`:4-7`), `LOAD1..4` (`:9`),
  `SEL1..4` (`:10`), `HUB`/`TRL`/`ADV` (`:12`), `MOT1..MOT4_RWD/FWD/EN` (`:14-17`),
  `RGB1..4` (`:19`), and `PSF=PC4` (`:23`).

## 2. Hooks it overrides relative to its base

Base is `AFC_vivid` (which itself inherits `afcBoxTurtle`). Claymore overrides:

- `__init__` (`extras/AFC_Claymore.py:38`) — only sets the type default to `'Claymore'`
  (`:40`).
- `handle_connect` (`:42`) — calls `super().handle_connect()` and replaces the logo strings
  (`:51-52`). No behaviour change.
- `_move_lane` (`:54`) — reduces to `return lane.load_state` (`:56`): **no motion at all**.
  ViViD's "move to load sensor and check" self-test is gone; Claymore trusts the load
  switch as the answer.
- `system_Test` (`:58`) — sets `cur_lane.prep_state = cur_lane.load_state` (`:59`) and then
  calls the base test with movement forced off (`:60`). This is how a unit with no prep
  sensor still satisfies the base test's `prep_state` branches.
- `unselect_lane` (`:62`) — default `move_distance=5` (ViViD uses 50) and otherwise
  delegates (`:65`). Smaller nudge because of the different selector geometry.
- `eject_lane` (`:71`) — delegates **directly to `afcBoxTurtle.eject_lane`** (`:81`),
  bypassing ViViD's prep-homing eject. See §5 for the consequence.
- `move_to_hub` (`:83`) — delegates to `afcBoxTurtle.move_to_hub` (`:109`), which is
  `afcUnit.move_to_hub` using the lane's **hub** endstop (`extras/AFC_unit.py:799`, `:823`)
  — i.e. the real `Claymore_1:HUB` switch, not ViViD's `lane.load_es` override.
- `calibrate_lane` (`:111`) — delegates to `afcBoxTurtle.calibrate_lane` (`:122`).
- `calibration_lane_message` (`:125`) — delegates to `afcBoxTurtle.calibration_lane_message`
  (`:126`). Note that `afcBoxTurtle` does not define it, so the call resolves to
  `afcUnit.calibration_lane_message`, which returns the empty string
  (`extras/AFC_unit.py:678-679`) — see §5.

Inherited unchanged from ViViD (and therefore surprising for Claymore — see §5):
`select_lane` (`extras/AFC_vivid.py:159`), `prep_load` (`:201`), `prep_post_load` (no-op,
`:255`), and the `AFC_UNSELECT_LANE` mux registration (`:81`).

Not overridden anywhere in the chain: `return_to_home` (`extras/AFC_unit.py:633`),
`load_then_home` (`extras/AFC_unit.py:861`), and `unit_load_lane`/`unit_unload_lane` — only
OpenAMS defines those, so Claymore takes `AFC.py`'s standard load/unload path
(`extras/AFC.py:1650-1651`, `:2051-2052`).

## 3. Template facts

From `templates/AFC_Claymore_1-AFC_Lite.cfg` and `config/mcu/AFC_Lite_Claymore.cfg`.

- **Lanes:** four (`[AFC_lane lane1..lane4]`, `:51`/`:64`/`:76`/`:88`); `unit:` is
  `Claymore_1:N` (`:52`/`:65`/`:77`/`:89`).
- **Per-lane pins:**
  - load — `^Claymore_1:LOAD1..4` (`:60`/`:73`/`:85`/`:97`), aliases
    `LOAD1=PB0, LOAD2=PB1, LOAD3=PB2, LOAD4=PE7` (`config/mcu/AFC_Lite_Claymore.cfg:9`);
    the `^` is a pull-up, and no inversion.
  - **prep — absent.** No lane has a `prep:` key at all (contrast ViViD
    `templates/AFC_Vivid_1.cfg:48`). `prep_state` therefore starts `False`
    (`extras/AFC_lane.py:225`) and is only synthesised in `system_Test`
    (`extras/AFC_Claymore.py:59`) — see §5.
  - selector — `!Claymore_1:SEL1..4` (`:61`/`:74`/`:86`/`:98`), aliases
    `SEL1..4 = PE10..PE13` (`config/mcu/AFC_Lite_Claymore.cfg:10`); inverted.
  - rewind/espooler (per lane) — `afc_motor_rwd/fwd/enb` on the lane:
    `MOT1_RWD/FWD/EN = PA0/PA1/PA2` (lane1, `:55-57`; alias `config/mcu/AFC_Lite_Claymore.cfg:14`),
    `MOT2_* = PA6/PA7/PA5` (lane2, `:68-70`; `:15`), `MOT3_* = PB14/PB15/PB13` (lane3,
    `:80-82`; `:16`), `MOT4_* = PD12/PD13/PD11` (lane4, `:92-94`; `:17`).
  - `fwd_speed_multiplier: 0.3` and `pwm: True` on all four lanes (`:58-59`, `:71-72`,
    `:83-84`, `:95-96`). Those are `AFC_assist` `Espooler` options (`extras/AFC_assist.py:433-440`);
    the class defaults are `0.5`/`0.5` (`:438`, `:440`), so Claymore deliberately slows the
    forward assist.
  - **no per-lane step/dir/en/UART pins** — the two motors are unit-level.
  - `dist_hub:` is `100` for lane1/3/4 (`:53`/`:78`/`:90`) but **`29.83` for lane2** (`:66`).
  - `led_index:` is one LED per lane into a single chain of four:
    `Claymore_Indicator_1:1..4` (`:54`/`:67`/`:79`/`:91`).
  - `park_dist` is never set; the default `10` is annotated "Currently unused"
    (`extras/AFC_lane.py:202`).
- **Unit-level steppers:**
  - drive `[AFC_stepper Claymore_drive]` (`:21`): step `Claymore_1:M1_STEP` (`:23`),
    `dir_pin: !Claymore_1:M1_DIR` (`:24`), `enable_pin: !Claymore_1:M1_EN` (`:25`),
    `microsteps: 16` (`:26`), `gear_ratio: 40:20  # 20T` (`:27`),
    `rotation_distance: 35.1  # 20T` (`:28`); tmc2209 `M1_UART` (`:30-31`),
    `run_current: 0.8` (`:33`), `sense_resistor: 0.110` (`:34`).
  - selector `[AFC_stepper Claymore_selector]` (`:36`): step `M2_STEP` (`:38`),
    `dir_pin: !Claymore_1:M2_DIR  # counter clockwise` (`:39`), `enable_pin: !M2_EN` (`:40`),
    `microsteps: 16` (`:41`), `gear_ratio: 40:20  # 20T` (`:42`),
    `rotation_distance: 35.5  # 20T` (`:43`); tmc2209 `run_current: 0.8` (`:48`),
    `sense_resistor: 0.110` (`:49`).
- **Hub:** `[AFC_hub Claymore_1]` (`:100`) — `afc_bowden_length: 1015.036` (`:101`),
  `cut: False` (`:102`), and a **real switch**: `switch_pin: Claymore_1:HUB` (`:103`; alias
  `HUB=PE8`, `config/mcu/AFC_Lite_Claymore.cfg:12`). So unlike ViViD there is a physical hub
  sensor. `hub_clear_move_dis` is unset → default `65` (`extras/AFC_hub.py:43`);
  `use_dist_hub` is unset → default `False` (`extras/AFC_hub.py:66`).
- **Buffer:** `[AFC_buffer Claymore_buffer]` (`:105`), type unset → default `"switched"`
  (`extras/AFC_buffer.py:47`), `advance_pin: ^!Claymore_1:ADV` / `trailing_pin:
  ^!Claymore_1:TRL` (`:106-107`; aliases `TRL=PE9`, `ADV=PC5`,
  `config/mcu/AFC_Lite_Claymore.cfg:12`; both pull-up **and** inverted), `multiplier_high:
  1.15`, `multiplier_low: 0.90` (`:108-109`).
- **Speeds/accels:** unit `long_moves_speed: 300` (`:17`), `long_moves_accel: 300` (`:18`)
  — 2.5×/6× ViViD's 120/50. `enable_buffer_tool_check: True` (`:19`) overrides the base
  default `False` (`extras/AFC_unit.py:112`). Short-move values are the globals
  (`config/AFC.cfg:9-11`).
- **Selector settings:** the same class defaults as ViViD — `selector_homing_speed: 150`,
  `selector_homing_accel: 150`, `max_selector_movement: 800` (`extras/AFC_vivid.py:74-76`),
  `VALID_CAM_ANGLES = [30,45,60]` (`:51`), `CALIBRATION_DISTANCE = 5000` (`:52`),
  `LANE_OVERSHOOT = 200` (`:53`); Claymore re-declares the two constants at
  `extras/AFC_Claymore.py:36-37`. Selector homing is ViViD's `do_homing_move` over
  `max_selector_movement * sel_dir` against `lane.selector_endstop_name`
  (`extras/AFC_vivid.py:185-190`).
- **Extruder / arrival:** `templates/AFC_Hardware-AFC.cfg:4` — `[AFC_extruder extruder]`
  (`pin_tool_start: enter_tool_pin` `:5`, `tool_stn: 72` `:7`, `tool_stn_unload: 100` `:8`,
  `tool_sensor_after_extruder: 0` `:9`, `tool_unload_speed: 25` `:10`, `tool_load_speed: 25`
  `:11`); globals `tool_homing_distance: 200`,
  `home_to_tool: True`, `homing_enabled: True` (`extras/AFC.py:237`, `:241`, `:242`).
  This template uses a **buffer** tool-start sensor, not a real one
  (`enable_buffer_tool_check: True`, `:19`).
- **Servo / cut:** none. `cut: False` on the hub (`:102`), no servo section.
- **LED:** `[AFC_led Claymore_Indicator_1]` (`:111`), `pin: Claymore_1:RGB1` (`:112`),
  `chain_count: 4` (`:113`), `color_order: GRBW` (`:114`). Aliases `RGB1..4 = PE14/PE15/PB10/PB11`
  (`config/mcu/AFC_Lite_Claymore.cfg:19`).

## 4. What klipper-mmu's firmware would have to reproduce

Labels: **observation** = directly readable in AFC; **inference** = a mapping judgement.

- **Lanes and per-lane switches — observation.** Four lanes, each with a **load** switch and
  a **selector** switch, and **no prep switch** (`templates/AFC_Claymore_1-AFC_Lite.cfg:60-61`).
  Mapping: four lanes with a LOAD switch; the missing PREP/TRG switch means klipper-mmu
  must not require a per-lane insert trigger for this unit (or must treat the load switch as
  the trigger). This is a real behavioural difference from ViViD.
- **Shared path and hub — observation.** Four lanes into one hub with a **real** `HUB`
  switch and a 1015.036 mm Bowden (`:100-103`). Mapping: shared path + the HUB switch
  klipper-mmu already has; no virtual-hub synthesis needed here, unlike ViViD.
- **Rewind / espooler — observation (this is the Claymore differentiator).** Four per-lane
  powered rewind motors with `rwd`/`fwd`/`en` pins and PWM forward assist
  (`:55-59`; `extras/AFC_assist.py:433-440`). Mapping: klipper-mmu's **rewind outputs** —
  this is a unit that genuinely needs them, forward *and* reverse, with a speed multiplier
  and PWM. Compare ViViD, where rewind has no counterpart at all.
- **PRELOAD — observation + inference.** Claymore **inherits ViViD's `prep_load`**
  (`extras/AFC_vivid.py:201`), which is gated on `lane.prep_state` (`:224-229`) and rotate
  the cam with `sel_prep=True` (`:222`), measuring `dist_hub` on first load. But Claymore has
  no `prep:` pin, so `prep_state` stays `False` except where `system_Test` fakes it
  (`extras/AFC_Claymore.py:59`). Inference: on a stock Claymore the inherited prep path
  cannot complete; the working entry point is the tool-load / `AFC_LANE` path via `AFC.py`,
  not the prep-trigger callback (`extras/AFC_lane.py:1194`). klipper-mmu should therefore
  model Claymore PRELOAD as a commanded action, not a switch-triggered one.
- **LOAD / UNLOAD — observation.** The standard `AFC.py` path runs
  (`extras/AFC.py:1650-1651`, `:2051-2052`); hub movement homes to the real HUB switch via
  `afcUnit.move_to_hub` (`extras/AFC_unit.py:799`, `:823`); the toolhead distance is
  `dist_hub` if `use_dist_hub` else `afc_bowden_length` (`extras/AFC.py:1714`), and unload
  uses `afc_unload_bowden_length` (`:2173`). Mapping: LOAD/UNLOAD with a bowden constant, a
  real hub endstop, and a hub-clearance retract.
- **EJECT — observation + inference.** Claymore's `eject_lane` calls
  `afcBoxTurtle.eject_lane` (`extras/AFC_Claymore.py:80`), i.e. retract `dist_hub` toward
  the **hub/load** side with `hub.move_dis` and `extruder_clear_dis` motion — *not* ViViD's
  "home back to the prep switch" (`extras/AFC_vivid.py:272-292`). Inference: Claymore's
  EJECT is a distance-based retract plus the rewind motor, which is closer to klipper-mmu's
  EJECT than ViViD's endstop-homing one.
- **FOLLOW / buffer — observation + inference.** As ViViD: a **switched** two-pin buffer
  (`^!` on both inputs, 1.15/0.90; `:105-109`), with `enable_buffer_tool_check: True`
  (`:19`) so the tool-start check is done by the buffer. Inference: same caveat as ViViD —
  a switched two-pin buffer is not an analog FPS/DiffA-DiffB differential buffer, so
  klipper-mmu's FOLLOW/FPS has to accept a bang-bang tension pair for this unit.
- **Selector / cam — the big gap — observation + inference.** A dedicated selector stepper
  homes against per-lane `SEL` switches (`:61`; class code `extras/AFC_vivid.py:159-199`),
  with `unselect_lane` nudging 5 mm (`extras/AFC_Claymore.py:62`). Observation: klipper-mmu
  has no selector primitive; its rollout plan files Claymore under "shared selector
  machines" (`docs/plan/device-and-stm32-rollout.md:97`) and notes "Rotating selector plus
  shared drive, lane selector/LOAD switches and powered rewind"
  (`docs/plan/device-and-stm32-rollout.md:129`), with the template evidence called out at
  `docs/plan/device-and-stm32-rollout.md:110-112`. Inference: neither LOAD/UNLOAD nor HUB
  covers an indexed rotary selector with per-lane cam endstops — this is the blocker for
  Claymore.
- **Second MCU — observation.** Not applicable: Claymore is a single MCU (`Claymore_1`,
  `templates/AFC_Claymore_1-AFC_Lite.cfg:3`). klipper-mmu does not need the two-MCU split
  that ViViD has.
- **Servo / cut — observation.** `cut: False` and no servo (`:102`). No counterpart needed.
- **PSF ADC pins — observation (no AFC use).** The alias file names `PC4`/`PC5`/`PB0`/`PB1`
  as the AFC-Lite PSF/SW ADC pins and defines `PSF=PC4`
  (`config/mcu/AFC_Lite_Claymore.cfg:21-23`), but `ADV`/`TRL` are also `PC5`/`PE9` — see §5.

## 5. Surprising or contradictory, in AFC's own files

- **`eject_lane` and the inherited prep story do not agree.** Claymore's `eject_lane`
  delegates to `afcBoxTurtle.eject_lane` (`extras/AFC_Claymore.py:80`), whose docstring and
  code retract toward the hub and then walk back until the **load** sensor clears
  (`extras/AFC_BoxTurtle.py:630-651`) — while the *delegating* method's own docstring still
  says "uses homing to move spool to prep sensor" (`extras/AFC_Claymore.py:72-73`). With no
  prep sensor on Claymore, the copied docstring is simply untrue.
- **`calibration_lane_message` silently returns ""**. Claymore calls
  `afcBoxTurtle.calibration_lane_message` (`extras/AFC_Claymore.py:126`), but `afcBoxTurtle`
  does not define that method, so it resolves to `afcUnit.calibration_lane_message`, which
  returns an empty string (`extras/AFC_unit.py:678-679`). Claymore's user gets no calibration
  guidance text at all — unlike ViViD, which supplies one (`extras/AFC_vivid.py:342-349`).
- **ViViD's `prep_load` is inherited but is dead on stock Claymore.** It is gated on
  `lane.prep_state` (`extras/AFC_vivid.py:224`) and calls `select_lane(lane, sel_prep=True)`
  (`:222`), yet Claymore defines no `prep:` pin (`templates/AFC_Claymore_1-AFC_Lite.cfg`),
  so `prep_state` is `False` everywhere except the one line that fakes it during a self-test
  (`extras/AFC_Claymore.py:59`). Observation: the inheritance looks like a copy that was
  never revisited for a unit without a prep sensor.
- **`system_Test` overwrites `prep_state` with `load_state`** (`extras/AFC_Claymore.py:59`)
  before calling `afcBoxTurtle.system_Test`, which then branches on `prep_state`
  (`extras/AFC_BoxTurtle.py:86`). Observation: the self-test's "EMPTY READY FOR SPOOL" vs
  "LOCKED" verdict for Claymore is really a load-sensor verdict wearing a prep-sensor
  label.
- **`HUB_LOAD` is available for Claymore but not ViViD.** `AFC.py` registers the per-lane
  `HUB_LOAD` command unless the unit type is `"ViViD"` (`extras/AFC.py:391-392`), and
  Claymore's type is `'Claymore'` (`extras/AFC_Claymore.py:40`), so Claymore gets it. That
  is consistent with Claymore having a real hub switch and ViViD not.
- **Lane 2's `dist_hub` is `29.83` while lanes 1/3/4 are `100`**
  (`templates/AFC_Claymore_1-AFC_Lite.cfg:66` vs `:53`/`:78`/`:90`). Observation: either a
  genuine mechanical asymmetry or a hand-calibrated leftover; the file does not say.
- **`LOAD1=PB0`/`LOAD2=PB1` collide with the file's own PSF note.** The same alias file says
  the ADC pins for the PSF sensor are `SW1(PC4), SW2(PC5), SW3(PB0), SW4(PB1)` and defines
  `PSF=PC4` (`config/mcu/AFC_Lite_Claymore.cfg:21-23`), while `LOAD1`/`LOAD2` are also
  `PB0`/`PB1` (`:9`) and `ADV` is `PC5` (`:12`). Observation: the comments describe a
  different pin assignment than the aliases actually use.
- **Very different mechanics behind the same class.** Claymore's drive/selector are
  `rotation_distance: 35.1`/`35.5` with `gear_ratio: 40:20`
  (`templates/AFC_Claymore_1-AFC_Lite.cfg:28`, `:43`) versus ViViD's `360`/`400` with
  `43:1`/`2.5:1` (`templates/AFC_Vivid_1.cfg:19-20`, `:34-35`). Subclassing says nothing
  about the hardware.
- `park_dist` is present, defaults to `10`, and the source says "Currently unused"
  (`extras/AFC_lane.py:202`).
