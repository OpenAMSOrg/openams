# AFC HTLF: what the unit class and its templates actually make the machine do

Source: the pinned AFC archive `AFC-Klipper-Add-On-5ea84ea6fab75b9b49db090f309154d1e52272bb`,
extracted read-only to `build/upstream/afc/`. Every `file:line` below is relative to that
archive. Nothing here is executed or qualified; it is a hand-read of AFC's own files.

The short version: HTLF is a BoxTurtle with the four lane motors replaced by one drive
motor and one selector motor that rotate a shaft with cam lobes until the chosen lobe
engages the chosen lane. The lanes have a LOAD switch and nothing else. That single change
drags in a home endstop, an `AFC_HOME_UNIT` command, a lobe-angle calculation, and a
deliberate no-op where the base class loads to the prep switch.

## 1. Files that define it

| Class | File:line | Inherits from |
| --- | --- | --- |
| `afcUnit` | `extras/AFC_unit.py:46` | — |
| `afcBoxTurtle` | `extras/AFC_BoxTurtle.py:29` | `afcUnit` |
| `AFC_HTLF` | `extras/AFC_HTLF.py:32` | `afcBoxTurtle` |

HTLF is **not** a `afcNightOwl` or `afcQuattroBox`. It reuses `afcBoxTurtle`'s calibration
and eject code and overrides the lane-selection seam.

Templates and installer files:

- `templates/AFC_HTLF_1-ERB.cfg` (ERB board) and `templates/AFC_HTLF_1-MMB.cfg` (MMB board).
  The installer picks the board and copies the matching MCU alias file and template
  (`include/install_functions.sh:104-110`): it copies `config/mcu/HTLF_<board>.cfg`
  (`:106`), remaps `MMB_1.0`/`MMB_1.1` to `MMB` (`:107`), then copies
  `templates/AFC_HTLF_1-<board>.cfg` (`:108`) and `AFC_Hardware-HTLF.cfg` (`:109`).
  The per-unit stage repeats it (`include/unit_functions.sh:204-212`): it makes
  `mcu/` (`:206`), copies the chosen alias file (`:207`), remaps `MMB_1.0`/`MMB_1.1`
  to `MMB` (`:208-210`), copies the template as `AFC_<board>_<name>.cfg` (`:211`)
  and rewrites every `HTLF_1` in it to the user's unit name (`:212`).
- `templates/AFC_Hardware-HTLF.cfg` — the shared hardware block, copied to `AFC_Hardware.cfg`.
- MCU pin-alias files: `config/mcu/HTLF_ERB.cfg`, `config/mcu/HTLF_MMB_1.0.cfg`,
  `config/mcu/HTLF_MMB_1.1.cfg`.

The include line differs between the two templates: `_ERB` names the file exactly
(`templates/AFC_HTLF_1-ERB.cfg:1`, `[include mcu/HTLF_ERB.cfg]`) while `_MMB` uses a glob
(`templates/AFC_HTLF_1-MMB.cfg:1`, `[include mcu/HTLF_MMB*.cfg]`), so a directory holding
both MMB alias files would include both. In practice the installer copies only the selected
one (`include/install_functions.sh:106`).

## 2. Hooks HTLF overrides relative to its base

All of these are HTLF's own definitions over `afcBoxTurtle`/`afcUnit`:

- `__init__` (`extras/AFC_HTLF.py:34-90`) — reads `drive_stepper` (`:37`),
  `selector_stepper` (`:38`), `mm_move_per_rotation` default 32 (`:41`), required
  `cam_angle` (`:42`), `home_pin` (`:43`), `MAX_ANGLE_MOVEMENT` default 215 (`:44`),
  `selector_movement_speed`/`selector_movement_accel` defaults 50/50 (`:46-47`); validates
  `cam_angle` against `[30,45,60]` and raises a config error otherwise (`:53-54`,
  `VALID_CAM_ANGLES` at `:33`). It registers a button on `home_pin` (`:58-59`), creates a
  filament switch on the same pin (`:66-67`), claims the pin as multi-use (`:69`), parses
  it (`:70`), builds an endstop (`:71`) named `f"{self.name}_home"` (`:72`), registers it
  with `query_endstops` (`:73-78`), calls the inherited `_lookup_objects` (`:80`), attaches
  the selector stepper to that endstop (`:82-85`) and registers `AFC_HOME_UNIT` (`:87-90`).
- `handle_connect` (`:92-102`) — after `super`, sets the HTLF ready/fault logo strings
  (`:101-102`).
- `system_Test` (`:104-111`) — **HTLF has no prep switch**, so it fakes one:
  `cur_lane.prep_state = cur_lane.load_state` (`:105`). If the selector is not yet homed it
  first calls `return_to_home(prep=True, disable_selector=False)` (`:106-107`), then runs
  the base test (`:108`), then returns home (`:109`), and reports success only if both the
  selector homed and the base test passed (`:111`).
- `home_callback` (`:113-117`) — records the home switch state (`:117`).
- `cmd_AFC_HOME_UNIT` (`:119-136`) — `AFC_HOME_UNIT UNIT=<name>`, default `HTLF_1`
  (`:121`); the body calls `return_to_home()` (`:136`).
- `_move_selector_home` (`:138-158`) — one selector move towards home; uses
  `do_homing_move` when homing is enabled (`:145-153`), plain `move` otherwise
  (`:155-158`).
- `return_to_home(prep=False, disable_selector=True)` (`:160-198`) — if a lane is selected
  and the switch is not already set, makes one fast 200 mm move (`move_distance = 200` at
  `:171`, guard at `:172`); then loops one-move-at-a-time until the home switch closes
  (`:178-189`); fails if the total exceeds
  `(mm_move_per_rotation/360) * (MAX_ANGLE_MOVEMENT + cam_angle)` (`:186-189`) with an AFC
  error (`:187-188`); sets `prep_homed = True` (`:191`); optionally disables the selector
  (`:195-196`); clears `current_selected_lane` (`:197`). It deliberately does **not** pause
  the reactor or disable the stepper to hold position, with a comment saying doing so
  crashes Klipper under the new motion queuing (`:192-194`).
- `calculate_lobe_movement(lane_index)` (`:200-209`) — `angle_movement =
  MAX_ANGLE_MOVEMENT - (lane_index - 1) * cam_angle` (`:207`), returns
  `(mm_move_per_rotation/360) * angle_movement` (`:209`). Lane 1 gets the full
  `MAX_ANGLE_MOVEMENT`, each later lane one cam angle less.
- `select_lane(lane, disable_selector=False)` (`:211-239`) — returns `(False, 0.0)` for any
  lane whose full name contains "stepper" (`:219-220`); if the requested lane is not the
  one currently selected (`:222`), it homes first with `disable_selector=False` (`:224`),
  then moves the selector by `calculate_lobe_movement(lane.index)` at
  `selector_movement_speed`/`accel` (`:225-228`), records the lane (`:230`) and returns
  `(True, self._homed_distance)`; a failed home returns `(False, 0.0)` (`:232-234`); the
  `finally` disables the selector only when `disable_selector` was requested
  (`:235-237`).
- `check_runout` (`:241-250`) — returns true when the lane is the current lane, the printer
  is printing, and the lane is neither ejecting nor calibrating (`:247-250`).
- `prep_load` (`:252-254`) and `prep_post_load` (`:256-258`) — **deliberate no-ops**, each
  with the comment "HTLF does not have prep switches returning".

Hooks HTLF does **not** override:

- `move_to_hub` — inherited from `afcUnit` (`extras/AFC_unit.py:799-828`); it calls
  `lane.move_to(...)` with the hub endstop (`:827-828`), which itself re-enters
  `select_lane` (`extras/AFC_lane.py:925`). So a hub move rotates the selector.
- `load_then_home` — inherited (`extras/AFC_unit.py:861-898`).
- `eject_lane` — inherited from `afcBoxTurtle` (`extras/AFC_BoxTurtle.py:630-652`).
- `calibrate_lane` — inherited (`extras/AFC_BoxTurtle.py:502-578`).
- `unit_load_lane` / `unit_unload_lane` — **not defined in HTLF**. Only `AFC_OpenAMS.py`
  defines them; dispatch is `hasattr` at `extras/AFC.py:1650-1651` (load) and `:2051-2052`
  (unload), else the generic path (`:1653-1700` load, `:2054`+ unload). The generic unload
  path calls `cur_lane.select_lane()` (`extras/AFC.py:2070`), which is HTLF's override, so
  unloading re-homes and re-selects the lobe.

The lane seam: `[AFC_lane]` sections in both HTLF templates have **no `step_pin`**, which
makes `AFCLane.only_lane = True` and borrows `unit_obj.drive_stepper_obj`
(`extras/AFC_lane.py:728-732`). HTLF is in `ONLY_LOAD_TYPES = ["HTLF", "Claymore",
"OpenAMS"]` (`extras/AFC_lane.py:39`), hence in `EXCLUDE_TYPES` (`:40`), which is what
binds the unit drive stepper and, when a `selector:` key is present, the selector stepper
(`extras/AFC_lane.py:746-756`). Neither HTLF template sets a per-lane `selector:` key even
though `AFCLane` supports one (`extras/AFC_lane.py:235`); the unit's `select_lane` uses
`selector_stepper_obj` directly, so the key is unused here.

## 3. Template facts

### Lanes

Four lanes in each template (`templates/AFC_HTLF_1-ERB.cfg:61`, `:67`, `:73`, `:79`;
`templates/AFC_HTLF_1-MMB.cfg:61`, `:67`, `:73`, `:79`). Every lane has only four keys:

| Fact | ERB | MMB |
| --- | --- | --- |
| `unit` | `HTLF_1:n` (`_ERB:62/:68/:74/:80`) | same (`_MMB:62/:68/:74/:80`) |
| `dist_hub` | 140 / 130 / 130 / 140 (`_ERB:63/:69/:75/:81`) | 100 for all four (`_MMB:63/:69/:75/:81`) |
| `led_index` | `AFC_Indicator_HTLF_1:n` (`_ERB:64/...`) | same (`_MMB:64/...`) |
| `load` | `^!ERB:LOADn` — pull-up **and** inverted (`_ERB:65/:71/:77/:83`) | `!MMB_HTLF:LOADn` — inverted, no pull-up (`_MMB:65/:71/:77/:83`) |

Missing from every HTLF lane, by comparison with the BoxTurtle lane block: `prep`, `step_pin`,
`dir_pin`, `enable_pin`, `microsteps`, `rotation_distance`, `park_dist`, `extruder`,
`print_current`, and any `espooler`/rewind key. There is no per-lane motor; the drives live
on the unit.

### Unit block

`[AFC_HTLF HTLF_1]` (`templates/AFC_HTLF_1-ERB.cfg:11-24`,
`templates/AFC_HTLF_1-MMB.cfg:11-24`): `hub: HTLF_1` (`:12`), `extruder: extruder` (`:13`),
`drive_stepper: HTLF_Drive` (`:15`), `selector_stepper: HTLF_Selector` (`:16`),
`home_pin` (`_ERB:17` = `ERB:HOME_POS`; `_MMB:17` = `MMB_HTLF:HOME_POS`),
`cam_angle: 60` (`:18`), `MAX_ANGLE_MOVEMENT: 220` (`:19`),
`mm_move_per_rotation: 40` (`:22`, with `32` commented for a 16T gear at `:21`),
`long_moves_speed: 150` (`:23`), `long_moves_accel: 50` (`:24`).

`buffer:` differs: `Turtle_1` in ERB (`templates/AFC_HTLF_1-ERB.cfg:14`) and `TurtleNeck`
in MMB (`templates/AFC_HTLF_1-MMB.cfg:14`). See the surprises — neither name is defined
anywhere in the archive.

### Drive and selector steppers

| Fact | ERB (`templates/AFC_HTLF_1-ERB.cfg`) | MMB (`templates/AFC_HTLF_1-MMB.cfg`) |
| --- | --- | --- |
| drive step/dir/en | `ERB:M1_STEP` (`:28`), `ERB:M1_DIR` **not inverted** (`:29`), `!ERB:M1_EN` (`:30`) | `MMB_HTLF:M1_*`, dir **inverted** `!MMB_HTLF:M1_DIR` (`:29`) |
| drive microsteps / rotation_distance / gear_ratio | 16 (`:31`), `23.4624` (`:34`), `80:20` (`:35`) | 16 (`:31`), `23.4624` (`:34`), `80:20` (`:35`) |
| drive TMC2209 | uart `ERB:M1_UART` (`:38`), addr 0 (`:39`), run_current 1.0 (`:40`), sense 0.110 (`:41`) | uart `MMB_HTLF:M1_UART` (`:38`), addr 0 (`:39`), run_current 0.8 (`:40`), sense 0.110 (`:41`) |
| selector step/dir/en | `ERB:M2_*` (`:45-47`), dir not inverted | `MMB_HTLF:M2_*` (`:45-47`), dir inverted (`:46`) |
| selector microsteps / rotation_distance / gear_ratio | 16 (`:48`), `40` (`:51`), `80:20` (`:52`) | 16 (`:48`), `40` (`:51`), `80:20` (`:52`) |
| selector TMC2209 | run_current 0.8 (`:57`), hold_current 0.6 (`:58`), sense 0.110 (`:59`) | run_current 0.8 (`:57`), hold_current 0.6 (`:58`), sense 0.110 (`:59`) |

`selector_movement_speed` / `selector_movement_accel` are read by the code with defaults
50/50 (`extras/AFC_HTLF.py:46-47`) and are set by **neither** template, so the selector
always moves at those defaults regardless of the `long_moves_speed` 150 in the unit block.

### Hub

`[AFC_hub HTLF_1]` (`templates/AFC_HTLF_1-ERB.cfg:85-89`,
`templates/AFC_HTLF_1-MMB.cfg:85-89`): `afc_bowden_length` 1990.0 in ERB (`:86`) versus
1750 in MMB (`:86`); `move_dis: 75` (`:87`); `hub_clear_move_dis: 65` (`:88`);
`switch_pin: ^ERB:HUB` (`_ERB:89`) or `^MMB_HTLF:HUB` (`_MMB:89`). There is **no `cut:` key
and no cut parameters at all** (contrast QuattroBox), so `AFC_hub`'s defaults apply:
`cut: False`, `cut_dist: 50`, `cut_clear: 120`, `cut_min_length: 200`, servo angles 0/160/75
(`extras/AFC_hub.py:50-58`), and `use_dist_hub: False` (`:66`). `td1_bowden_length` defaults
to `afc_bowden_length - 50` (`extras/AFC_hub.py:45`).

### Extruder / toolhead arrival

`templates/AFC_Hardware-HTLF.cfg`: `[force_move] enable_force_move: True` (`:1-2`),
`[AFC_extruder extruder]` (`:4-11`) with `pin_tool_start: enter_tool_pin` (`:5`),
`tool_stn: 72` (`:7`), `tool_stn_unload: 100` (`:8`),
`tool_sensor_after_extruder: 0` (`:9`), `tool_unload_speed: 25` (`:10`),
`tool_load_speed: 25` (`:11`). A `bypass` switch (`:13-15`) and a tip-cut servo
(`:26-38`) are commented out.

### Buffer

`[AFC_buffer TN]` (`templates/AFC_Hardware-HTLF.cfg:17-21`):
`advance_pin: <enter_pin>` and `trailing_pin: <enter_pin>` placeholders (`:18-19`),
`multiplier_high: 1.05` (`:20`), `multiplier_low: 0.95` (`:21`). No `type` key, so it is
the default switched/"Turtleneck" style (`extras/AFC_buffer.py:47`, `:97-100`).

### Speeds, accelerations, LED, rewind

- The unit templates set only `long_moves_speed`/`long_moves_accel` (150/50,
  `templates/AFC_HTLF_1-ERB.cfg:23-24`). Short-move values come from the global
  `config/AFC.cfg`: `long_moves_speed: 150` (`:7`), `long_moves_accel: 250` (`:8`),
  `short_moves_speed: 50` (`:9`), `short_moves_accel: 300` (`:10`),
  `short_move_dis: 10` (`:11`).
- `[AFC_led AFC_Indicator_HTLF_1]`: pin `ERB:RGB1` (`templates/AFC_HTLF_1-ERB.cfg:92`) or
  `MMB_HTLF:RGB1` (`templates/AFC_HTLF_1-MMB.cfg:92`), `chain_count: 4` (`:93`),
  `color_order: GRBW` (`:94`). Four LEDs, one per lane.
- Rewind/espooler: **none**. No `afc_motor_rwd`/`afc_motor_fwd`/`afc_motor_enb`
  (`extras/AFC_assist.py:433-435`) in either unit template or the hardware template.

### MCU aliases

`config/mcu/HTLF_ERB.cfg`: `[board_pins erb_htlf]`, `mcu: ERB` (`:1-2`);
`M1_STEP=gpio16, M1_DIR=gpio15, M1_EN=gpio14, M1_UART=gpio17` (`:4`),
`M2_STEP=gpio10, M2_DIR=gpio9, M2_EN=gpio8, M2_UART=gpio11` (`:5`),
`HUB=gpio25` (`:7`), `HOME_POS=gpio24` (`:8`),
`LOAD1=gpio12, LOAD2=gpio3, LOAD3=gpio6, LOAD4=gpio27` (`:9`),
`RGB1=gpio21` (`:11`), and `PSF=gpio26` (`:15`) under a comment listing gpio26–gpio29 as
the board's ADC pins (`:13-14`).

`config/mcu/HTLF_MMB_1.0.cfg`: `HUB=PC13` (`:9`), `HOME_POS=PA3` (`:10`),
`LOAD1=PA4, LOAD2=PB9, LOAD3=PB8, LOAD4=PC15` (`:11`, with the comment
`# LOAD3=PA8 => PB8`), `SPARE1=PC14, SPARE2=PB12, SPARE3=PA0` (`:12`),
`SPARE5=PB3 ... SPARE9=PB2` (`:13-14` — note there is no `SPARE4`),
`RGB1=PA2` (`:16`), `PSF=PA1` (`:18`).

`config/mcu/HTLF_MMB_1.1.cfg`: same as 1.0 except `LOAD3=PA8` (`:11`) and
`SPARE4=PA1` (`:12`) — which collides with `PSF=PA1` (`:16`).

## 4. What klipper-mmu firmware must reproduce, and the gaps

- **Lanes / gates** — observation: four load-only gates fed by one drive motor. klipper-mmu
  must model a lane whose admission sensor is LOAD-only, with no BAY/prep switch. AFC makes
  this explicit: HTLF zeroes `prep_load` and `prep_post_load`
  (`extras/AFC_HTLF.py:252-258`) and fakes `prep_state` from `load_state` in `system_Test`
  (`:105`). This is the biggest structural difference from QuattroBox.
- **Selector mechanism** — **gap**. This is a real motorised selector with a home endstop,
  lobe-angle geometry and a homing routine (`extras/AFC_HTLF.py:138-239`). klipper-mmu's
  architecture states "The portable selector homing primitive has no board binding or
  profile support yet" (`docs/architecture.md`), and the feature map lists AFC `AFC_HTLF.py`
  under selectors/gripping, explicitly warning not to claim coverage from a virtual
  selector alone (`docs/plan/feature-map.md`). Nothing in the current profile factories
  reproduces a cam-lobe selector, the `AFC_HOME_UNIT` command, or the
  `MAX_ANGLE_MOVEMENT - (lane_index-1)*cam_angle` sequence.
- **HUB and LOAD switches** — observation: a real hub switch (`switch_pin` on `ERB:HUB` /
  `MMB_HTLF:HUB`) and four real LOAD switches on the inverted `LOADn` aliases. These map to
  klipper-mmu's HUB and LOAD inputs directly.
- **BAY switch** — gap/asymmetry: HTLF has no bay-present switch, so a device profile must
  not require a BAY input for this unit. klipper-mmu already treats missing sensors as
  absent rather than fabricated (`docs/architecture.md`), which is the correct behaviour
  here.
- **Toolhead / arrival** — observation: `[AFC_extruder]` values (`tool_stn: 72`,
  `tool_stn_unload: 100`, `tool_sensor_after_extruder: 0`) with a placeholder
  `pin_tool_start`. Same shape as QuattroBox, so the same mapping applies.
- **Shared path** — observation: one drive motor feeds every lane, so the entire
  lane-to-toolhead route is effectively shared; `afc_bowden_length` is 1990 (ERB) or 1750
  (MMB) and each lane's `dist_hub` is only 100–140 mm. klipper-mmu's shared path and
  per-lane tails cover this, but "shared path" here means physically one motor, so the
  selector must be positioned before any lane motion — which AFC enforces by re-selecting
  on every move (`extras/AFC_lane.py:881`, `:925`).
- **Rewind outputs** — gap: none configured, same as QuattroBox.
- **Buffer** — gap/inference: the hardware template defines a switched Turtleneck-style
  `[AFC_buffer TN]` with discrete `advance_pin`/`trailing_pin` (real switches, not analog),
  while klipper-mmu's reference buffer is analog DiffA/DiffB
  (`docs/architecture.md`). Additionally the unit block points at a **different** name, so
  the buffer is not even wired to the defined section — see the surprises.
- **LOAD / UNLOAD** — observation: generic path (`extras/AFC.py:1653-1700` load,
  `:2054`+ unload). Unload calls `select_lane()` (`extras/AFC.py:2070`), which for HTLF
  re-homes and re-rotates the selector (`extras/AFC_HTLF.py:211-239`).
- **PRELOAD** — observation: splits into selector homing (`system_Test` → `return_to_home`,
  `extras/AFC_HTLF.py:104-111`) plus a shared-drive move; the base `prep_load` body never
  runs because HTLF overrides it to a no-op (`:252-254`).
- **EJECT** — observation: inherited `afcBoxTurtle.eject_lane`
  (`extras/AFC_BoxTurtle.py:630-652`), which retracts to the lane load switch and then
  steps back hub-length increments. No selector-aware parking.
- **FOLLOW** — observation/inference: HTLF has no buffer feedback of its own; following is
  the switched-buffer multiplier. There is no HTLF-specific follow code, and the selector
  is disabled after selection (`extras/AFC_HTLF.py:195-196`, `:235-237`), so the selector
  does not actively hold during printing.

## 5. Surprises and contradictions in AFC's own files

1. **The unit config points at a buffer name that no file defines.**
   `buffer: Turtle_1` (`templates/AFC_HTLF_1-ERB.cfg:14`) and `buffer: TurtleNeck`
   (`templates/AFC_HTLF_1-MMB.cfg:14`) are not matched by any `[AFC_buffer]` section. A
   search for `^[AFC_buffer` across the archive finds only `TN` (the HTLF hardware
   template, `templates/AFC_Hardware-HTLF.cfg:17`), plus unrelated units
   (`templates/AFC_Hardware-NightOwl.cfg:18`, `templates/AFC_QuattroBox_14.cfg:14`). And
   "TurtleNeck" appears elsewhere only as an MCU alias comment
   (`community_mods/mcu/Duet2WiFi.cfg:132-133`) and as a comment in
   `tests/klippy/afc_with_buffer.cfg:3`. So an HTLF install config-references a missing
   buffer unless the user defines one, while the exported buffer section is named `TN`. The
   two names do not meet.
2. **Code defaults and template values disagree on the two selector constants.**
   `mm_move_per_rotation` defaults to 32 (`extras/AFC_HTLF.py:41`) but the templates set 40
   (`templates/AFC_HTLF_1-ERB.cfg:22`); `MAX_ANGLE_MOVEMENT` defaults to 215
   (`extras/AFC_HTLF.py:44`) but the templates set 220 (`:19`). The lobe-distance math
   (`:207-209`) uses both, so a config that omits them behaves differently from the shipped
   templates.
3. **`include mcu/HTLF_MMB*.cfg` is a glob.** `templates/AFC_HTLF_1-MMB.cfg:1` will pick up
   every matching alias file in the config directory, including both `_1.0` and `_1.1`
   revisions if both are present. The ERB template names its file exactly
   (`templates/AFC_HTLF_1-ERB.cfg:1`). This is masked only by the installer copying a single
   alias file (`include/install_functions.sh:106`).
4. **The two MMB alias revisions disagree on `LOAD3`, and 1.1 has a pin collision.**
   `LOAD3` is `PB8` in 1.0 (`config/mcu/HTLF_MMB_1.0.cfg:11`, with the comment
   `# LOAD3=PA8 => PB8`) and `PA8` in 1.1 (`config/mcu/HTLF_MMB_1.1.cfg:11`). Worse,
   `HTLF_MMB_1.1.cfg:12` defines `SPARE4=PA1` while `:16` defines `PSF=PA1`: the same pin is
   claimed twice. `HTLF_MMB_1.0.cfg` has no `SPARE4` at all (`:12-14`), which looks like the
   collision was introduced by adding one.
5. **Drive polarity and current differ between boards.** The ERB drive `dir_pin` is not
   inverted (`templates/AFC_HTLF_1-ERB.cfg:29`) while the MMB drive `dir_pin` is
   (`templates/AFC_HTLF_1-MMB.cfg:29`); ERB drive `run_current` is 1.0 (`_ERB:40`) and MMB is
   0.8 (`_MMB:40`). Same motor parameters, different electrical setup, no comment.
6. **A known upstream landmine is documented in the source.** The comment at
   `extras/AFC_HTLF.py:192-194` says adding a reactor pause or disabling the stepper motor
   "will crash klipper with newest motion queuing changes", which is why `return_to_home`
   leaves the selector enabled unless explicitly asked not to. Anyone porting this homing
   routine should read that comment twice.
7. **The selector speed is configured in code but set by no template.** `selector_movement_speed`
   and `selector_movement_accel` default to 50/50 (`extras/AFC_HTLF.py:46-47`) and appear in
   neither `templates/AFC_HTLF_1-ERB.cfg` nor `templates/AFC_HTLF_1-MMB.cfg`, so the
   selector ignores the unit's `long_moves_speed: 150` (`:23`) entirely.
8. **HTLF lanes omit keys their BoxTurtle base reads.** The base `calibrate_lane` uses
   `cur_lane.short_move_dis` and hub state (`extras/AFC_BoxTurtle.py:502-578`) and
   `system_Test` writes `cur_lane.prep_state = cur_lane.load_state`
   (`extras/AFC_HTLF.py:105`), but the lane blocks set no `extruder`, no `park_dist`, no
   `print_current` and no per-lane `microsteps`. Most of those take class defaults, but the
   missing `park_dist` is consistent with the lane code noting `park_dist` is "Currently
   unused" (`extras/AFC_lane.py:202`).
9. **No `selector:` key anywhere.** `AFCLane` reads a per-lane `selector` config key
   (`extras/AFC_lane.py:235`) and `_get_steppers` will bind a selector stepper when one is
   present (`:750-756`), but neither HTLF template sets the key. HTLF's own `select_lane`
   drives `unit_obj.selector_stepper_obj` directly, so the per-lane key is dead weight for
   this unit.
