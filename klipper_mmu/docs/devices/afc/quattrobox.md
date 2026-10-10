# AFC QuattroBox: what the unit class and its templates actually make the machine do

Source: the pinned AFC archive `AFC-Klipper-Add-On-5ea84ea6fab75b9b49db090f309154d1e52272bb`,
extracted read-only to `build/upstream/afc/`. Every `file:line` below is relative to that
archive. Nothing here is executed or qualified; it is a hand-read of AFC's own files, which
is the cross-check column for klipper-mmu's per-device behaviour.

The short version: QuattroBox is a BoxTurtle with a different lid. Its class adds an LED
logo and nothing else, so every motion, load, unload, calibrate and test behaviour is
`afcBoxTurtle`'s. The interesting part is the four TMC2209-UART lane drivers and what the
templates do to `dir_pin` and `rotation_distance`.

## 1. Files that define it

Unit class and the inheritance chain (each `class` line is where the chain is welded):

| Class | File:line | Inherits from |
| --- | --- | --- |
| `afcUnit` | `extras/AFC_unit.py:46` | — |
| `afcBoxTurtle` | `extras/AFC_BoxTurtle.py:29` | `afcUnit` |
| `afcNightOwl` | `extras/AFC_NightOwl.py:16` | `afcBoxTurtle` |
| `afcQuattroBox` | `extras/AFC_QuattroBox.py:12` | `afcNightOwl` |

Why the NightOwl hop matters: `afcNightOwl` defines only `__init__`
(`extras/AFC_NightOwl.py:17-19`, `type` default `'Night_Owl'`) and `handle_connect`
(`extras/AFC_NightOwl.py:21-37`), which prints an ASCII owl
(`extras/AFC_NightOwl.py:30-35`). It adds no mechanism. QuattroBox overrides
`type` to `'Quattro_Box'` (`extras/AFC_QuattroBox.py:15`) and its own `handle_connect`
(`extras/AFC_QuattroBox.py:17-28`).

There is a unit test that asserts the chain, so this is not inference:
`tests/test_AFC_QuattroBox.py:79` checks `issubclass(afcQuattroBox, afcNightOwl)`, `:50`
checks the type string, `:169`/`:175` check the logo strings.

Templates and installer files:

- `templates/AFC_QuattroBox_14.cfg` (NEMA14 variant) and `templates/AFC_QuattroBox_17.cfg`
  (NEMA17 variant) — the actual unit definitions, selected by motor type in
  `include/install_functions.sh:123-124` (NEMA_14) and `:135-136` (NEMA_17).
- `templates/AFC_Hardware-QuattroBox.cfg` — copied to `AFC_Hardware.cfg`
  (`include/install_functions.sh:121`).
- `templates/qb_macros/Eject_buttons.cfg` — copied to `macros/Eject_buttons.cfg`
  (`include/install_functions.sh:122`).
- `include/unit_functions.sh:219-255` is the second, per-unit installer path that does the
  same job with different sed rewrites.

MCU pin-alias files, one per board revision:

- `config/mcu/MMB_1.0_QB.cfg`, `config/mcu/MMB_1.1_QB.cfg`, `config/mcu/MMB_2.0_QB.cfg`.
- The templates do **not** name these files. Both say `[include mcu/MMB_QB.cfg]`
  (`templates/AFC_QuattroBox_14.cfg:3`, `templates/AFC_QuattroBox_17.cfg:3`), and
  `config/mcu/MMB_QB.cfg` does not exist in the archive. The installer rewrites the include
  with sed (`include/install_functions.sh:127`, `:130`, `:133`, `:139`, `:142`, `:145`;
  `include/unit_functions.sh:227`, `:231`, `:235`, `:242`, `:246`, `:250`), and the
  per-unit path additionally renames the alias file and rewrites `mcu: QuattroBox_1`
  (`include/unit_functions.sh:228-229` and friends), finishing with a blanket
  `s/QuattroBox_1/$boxturtle_name/g` over the unit config (`include/unit_functions.sh:255`).
  So `[include mcu/MMB_QB.cfg]` is a template placeholder that only becomes a real file
  after install.

## 2. Hooks QuattroBox overrides relative to its base

`afcQuattroBox` overrides exactly four methods, all presentation:

- `handle_connect` (`extras/AFC_QuattroBox.py:17-28`) — sets the ready logo
  (`:25`) and a fault logo (`:27`), then pushes the logo colour (`:28`).
- `lane_loading` (`extras/AFC_QuattroBox.py:30-38`) — calls `super().lane_loading` (`:37`)
  then sets the logo to `led_logo_loading` (`:38`).
- `lane_tool_loaded` (`extras/AFC_QuattroBox.py:40-48`) — after `super`, sets the logo to
  the lane colour (`:48`).
- `lane_tool_unloaded` (`extras/AFC_QuattroBox.py:50-58`) — after `super`, restores
  `led_logo_color` (`:58`).

It overrides no motion hook, so the following are inherited unchanged from
`afcBoxTurtle` and are the behaviour a klipper-mmu device profile must match:

- `system_Test` (`extras/AFC_BoxTurtle.py:79-154`).
- `calibrate_bowden` (`:156`), `calibrate_td1` (`:306`), `calibrate_hub` (`:407`),
  `move_until_state` (`:439`), `calc_position` (`:475`).
- `calibrate_lane` (`:502-578`) — refuses if the hub is not clear (`:505-507`), if the lane
  is not loaded (`:508-510`) or not prepped (`:511-513`); retracts `dist_hub + 100`
  (`:518`); then either homes to the hub (`:551-555`) or uses `calibrate_hub` (`:560`);
  always does a hub-clear retract (`:568`); computes `cal_dist = hub_pos - hub_clear_move_dis`
  (`:570`), rewrites `dist_hub` in the config file (`:575`) and returns home (`:577`).
- `prep_load` (`:580-609`) — with homing on, one long homing move of
  `short_move_dis * MAX_NUM_MOVES` (`:594`) to the lane load endstop; then short moves of
  `short_move_dis` until the raw load switch closes (`:598-603`); after
  `MAX_NUM_MOVES` (=40, `:30`) attempts it raises an AFC error (`:604-609`).
- `prep_post_load` (`:611-628`) — if the lane is not a direct-to-hub lane, is set to load to
  hub, is not already there, and both load and prep switches are still set, it moves
  `lane.dist_hub` (`:627`) and marks `loaded_to_hub = True` (`:628`).
- `eject_lane` (`:630-652`) — retracts `dist_hub` to the load switch (`:638`), then loops
  `move_advanced(hub_obj.move_dis * -1)` while the load switch is set (`:642-649`), then
  retracts `extruder_clear_dis` (`:650`), and disables the lane in `finally` (`:651-652`).

Hooks that are **not** overridden anywhere in the QuattroBox chain — and which therefore
fall back to `afcUnit`'s deliberately inert or generic versions:

- `select_lane` — `afcUnit.select_lane` returns `(True, 0.0)` and does nothing
  (`extras/AFC_unit.py:622-631`). QuattroBox has no selector, so "selecting" a lane is a
  no-op; the lane's own `[AFC_stepper]` is the motor.
- `return_to_home` — `afcUnit.return_to_home` is an empty `return`
  (`extras/AFC_unit.py:633-637`). There is no unit home position to return to.
- `check_runout` — `afcUnit.check_runout` returns `False` (`extras/AFC_unit.py:639-643`),
  so the call site at `extras/AFC_lane.py:1176` never takes the unit-runout branch for a
  BoxTurtle-family lane. Only HTLF provides real logic (`extras/AFC_HTLF.py:241-250`).
- `move_to_hub` — inherited from `afcUnit` (`extras/AFC_unit.py:799-828`), which delegates
  to `lane.move_to(...)` with the hub endstop (`:827-828`).
- `load_then_home` — inherited from `afcUnit` (`extras/AFC_unit.py:861-898`). It splits the
  move only when `homing_enabled and home_to_tool` (`:884`), `load_then_home_var` is set and
  the distance exceeds `load_undershoot` (`:887`), and the extruder's `tool_start` is
  `"buffer"` (`:889`).
- `unit_load_lane` / `unit_unload_lane` — **neither exists in the QuattroBox chain**.
  They are defined only in `extras/AFC_OpenAMS.py`, and are dispatched with `hasattr` at
  `extras/AFC.py:1650-1651` (load) and `extras/AFC.py:2051-2052` (unload); otherwise the
  generic path runs (`extras/AFC.py:1653-1700` for load, `:2054`+ for unload), with
  `move_to_hub` at `:1673` and the hub-crossing loop at `:1691-1692`.

Because every lane motion re-enters `self.unit_obj.select_lane(self)`
(`extras/AFC_lane.py:881` in `move`, `:925` in `move_to`), QuattroBox pays a no-op
`select_lane` call on every move. Cheap, but worth knowing when tracing.

## 3. Template facts

### Lanes

Both templates define four lanes. In `_14`, the lane blocks start at
`templates/AFC_QuattroBox_14.cfg:30` (lane1), `:54` (lane2), `:78` (lane3), `:102`
(lane4); in `_17` at `:30`, `:53`, `:76`, `:99`.

| Fact | `_14` (NEMA14) | `_17` (NEMA17) |
| --- | --- | --- |
| `unit` | `QuattroBox_1:n` (`_14:31/:55/:79/:103`) | same (`_17:31/:54/:77/:100`) |
| `step_pin` | `QuattroBox_1:Mn_STEP` (`_14:33/:57/:81/:105`) | same (`_17:33/:56/:79/:102`) |
| `dir_pin` | lanes 1–2 plain `M1_DIR`/`M2_DIR` (`_14:34`, `:58`); lanes 3–4 `!M3_DIR`/`!M4_DIR` (`_14:82`, `:106`) | all four inverted `!Mn_DIR` (`_17:34`, `:57`, `:80`, `:103`) |
| `enable_pin` | `!QuattroBox_1:Mn_EN` (`_14:35/:59/:83/:107`) | same (`_17:35/:58/:81/:104`) |
| `microsteps` | 16 (`_14:36`) | 16 (`_17:36`) |
| `gear_ratio` | `50:10` (`_14:37/:61/:85/:109`) | **absent** |
| `rotation_distance` | `22.7316868` (`_14:38/:62/:86/:110`) | `22.7316868` (`_17:37/:60/:83/:106`) |
| `dist_hub` | 95 / 45 / 40 / 90 (`_14:39/:63/:87/:111`) | 130 / 80 / 85 / 130 (`_17:38/:61/:84/:107`) |
| `park_dist` | 10 (`_14:40/:64/:88/:112`) | 10 (`_17:39/:62/:85/:108`) |
| `load` | `^QuattroBox_1:EXTn`, pull-up active-high (`_14:41/:65/:89/:113`) | same (`_17:40/:63/:86/:109`) |
| `prep` | `^QuattroBox_1:TRGn`, pull-up active-high (`_14:42/:66/:90/:114`) | same (`_17:41/:64/:87/:110`) |
| `print_current` | 0.4 (`_14:45/:69/:93/:117`) | 0.4 (`_17:44/:67/:90/:113`) |

The `gear_ratio` asymmetry is a real difference, not a typo I invented: with `50:10` the
`_14` mechanism gets a 5:1 reduction applied to the same nominal rotation distance, so the
two variants do not move the same distance per motor revolution. Treat `_14` and `_17` as
different mechanics (a geared NEMA14 and a direct NEMA17), and confirm against AFC upstream
before copying either number.

Per-lane TMC2209 blocks: `uart_pin: QuattroBox_1:Mn_UART`, `uart_address: 0`,
`sense_resistor: 0.110` in both variants (`_14:48-52`, `_17:47-51`). `run_current` and
`hold_current` differ: `_14` is `0.7` / `0.2` (`_14:50-51`), `_17` is `1.0` / `0.2`
(`_17:49-50`).

LED indices are sequential and are the only place the LED chain is mapped to lanes:
`led_index: AFC_Indicator:1` / `led_spool_index: AFC_Indicator:2` for lane1
(`_14:43-44`), then `:3`/`:4`, `:5`/`:6`, `:7`/`:8` (`_14:67-68`, `:91-92`, `:115-116`;
`_17:42-43`, `:65-66`, `:88-89`, `:111-112`).

### Buffer

`[AFC_buffer QuattroBox_1]` (`templates/AFC_QuattroBox_14.cfg:14-18`,
`templates/AFC_QuattroBox_17.cfg:14-18`): `advance_pin` and `trailing_pin` are
placeholders (`_14:15-16`), and `multiplier_high: 1.05` / `multiplier_low: 0.95`
(`_14:17-18`). No `type` key, so it is the default switched/"Turtleneck" style, which is
the only style that reads `advance_pin`/`trailing_pin` (`extras/AFC_buffer.py:97-100`).
Note the template's `1.05`/`0.95` differs from the `AFCBuffer` defaults of `1.1`/`0.9`
(`extras/AFC_buffer.py:93-94`). The unit config wires `buffer: QuattroBox_1`
(`templates/AFC_QuattroBox_14.cfg:24`), and the comment calls Turtleneck the standard
buffer.

### Hub

`[AFC_hub QuattroBox_Hub]`: `_14` at `:126-142`, `_17` at `:122-138`.

| Key | `_14` | `_17` |
| --- | --- | --- |
| `afc_bowden_length` | 1145.0 (`:127`) | 1110.0 (`:123`) |
| `move_dis` | 75 (`:128`) | 75 (`:124`) |
| `cut` | `False` (`:129`) | `False` (`:125`) |
| `hub_clear_move_dis` | 65 (`:130`) | 65 (`:126`) |
| `cut_cmd` | `AFC` (`:135`) | `AFC` (`:131`) |
| `cut_dist` | 200 (`:136`) | 200 (`:132`) |
| `cut_clear` | 120 (`:137`) | 120 (`:133`) |
| `cut_min_length` | 300.0 (`:138`) | 300.0 (`:134`) |
| `cut_servo_pass_angle` | 10 (`:139`) | 10 (`:135`) |
| `cut_servo_clip_angle` | 180 (`:140`) | 180 (`:136`) |
| `cut_servo_prep_angle` | 80 (`:141`) | 80 (`:137`) |
| `switch_pin` | `^QuattroBox_1:HUB` (`:142`) | `^QuattroBox_1:HUB` (`:138`) |

The cut block is present but inert: `cut: False` means AFC will not cut, and the angle
keys only matter when a cutter is enabled (`extras/AFC_hub.py:49-59`). `cut_servo_name`
is not set, so it defaults to `'cut'` (`extras/AFC_hub.py:52`). No `use_dist_hub` key, so
the default `False` applies (`extras/AFC_hub.py:66`) and the hub length comes from
`afc_bowden_length`, not per-lane `dist_hub`.

### Extruder / toolhead arrival

`templates/AFC_Hardware-QuattroBox.cfg`: `[force_move] enable_force_move: True`
(`:1-2`), then `[AFC_extruder extruder]` (`:4-11`) with
`pin_tool_start: enter_tool_pin` (a placeholder, `:5`), `tool_stn: 40` (`:7`),
`tool_stn_unload: 70` (`:8`), `tool_sensor_after_extruder: 0` (`:9`),
`tool_unload_speed: 35` (`:10`), `tool_load_speed: 25` (`:11`). A `bypass` switch
(`:13-15`) and a tip-cut servo (`:20-32`) are commented out, so neither is configured by
default. The servo values shown there are `minimum_pulse_width: 0.00053` (`:25`),
`maximum_pulse_width: 0.0023` (`:28`), `maximum_servo_angle: 180` (`:30`).

### Speeds and accelerations

The unit templates set **no** speed or acceleration keys. Those come from the global
`config/AFC.cfg`: `long_moves_speed: 150` (`:7`), `long_moves_accel: 250` (`:8`),
`short_moves_speed: 50` (`:9`), `short_moves_accel: 300` (`:10`),
`short_move_dis: 10` (`:11`). QuattroBox overrides none of them (no keys in either
template). `afc_bowden_length` is per-hub, not global.

### Selector, servo, rewind

- Selector: none. No `selector_stepper`, no `home_pin`, no lobe/cam keys.
- Servo: only the commented-out tip-cut servo in the hardware template.
- Rewind/espooler: none. A search for `espooler`, `afc_motor`, `rewind` and `spooler`
  over both unit templates and the hardware template returns nothing, and neither
  `afc_motor_rwd` nor `afc_motor_fwd` nor `afc_motor_enb` — the `Espooler` pins
  (`extras/AFC_assist.py:410`, `:433-435`) — is set anywhere in the QuattroBox files.

### LED and buttons

`[AFC_led AFC_Indicator]`: `pin: QuattroBox_1:RGB` (`templates/AFC_QuattroBox_14.cfg:146`),
`chain_count: 36` (`:148`), `color_order:` 36 comma-separated entries — 8×`GRBW` then
28×`GRB` (`:150`). The unit's logo is `led_logo_index: AFC_Indicator: 9-36`
(`templates/AFC_QuattroBox_14.cfg:25`, note the space after the colon) with a custom
`led_logo_color: 1,0.15,0.66,0` (`:26`); the lane LEDs occupy indices 1–8.

`templates/qb_macros/Eject_buttons.cfg` defines four `[gcode_button]` sections
(`:1`, `:5`, `:9`, `:13`) on `QuattroBox_1:BUTTON1..4` (`:2`, `:6`, `:10`, `:14`), each
issuing `BT_LANE_EJECT LANE=n` (`:3`, `:7`, `:11`, `:15`).

### MCU aliases

`config/mcu/MMB_1.0_QB.cfg`: `[board_pins mmb_1.0_QB]`, `mcu: QuattroBox_1` (`:1-2`);
`M1_STEP=PB15, M1_DIR=PB14, M1_EN=PA8, M1_UART=PA10` (`:4`), M2–M4 at `:5-7`,
`HUB=PB11` (`:9`), `TRG1=PA3, TRG2=PA4, TRG3=PB9, TRG4=PB8` (`:10`),
`EXT1=PC15, EXT2=PC13, EXT3=PC14, EXT4=PB12` (`:11`),
`BUTTON1=PB4, BUTTON2=PB3, BUTTON3=PB10, BUTTON4=PB2` (`:12`),
`RGB=PA2, PSF=PA1` (`:14`).

`config/mcu/MMB_1.1_QB.cfg` is byte-for-byte the same except two aliases swapped:
`M1_EN=PB8` (`:4`) and `TRG4=PA8` (`:10`).

`config/mcu/MMB_2.0_QB.cfg` is re-pinned throughout: `M1`–`M4` at `:4-7`,
`HUB=PA15` (`:9`), `TRG1=PC7, TRG2=PA9, TRG3=PB12, TRG4=PB10` (`:10`),
`EXT1=PC6, EXT2=PA8, EXT3=PB11, EXT4=PB2` (`:11`),
`TN_ADV=PA10, TN_TRL=PD9` (`:12`),
`BUTTON1=PB1, BUTTON2=PC5, BUTTON3=PC0, BUTTON4=PC4` (`:14`),
`RGB=PC3, PSF=PC2` (`:16`).

## 4. What klipper-mmu firmware must reproduce, and the gaps

Mappings are observations unless marked inference; "gap" means AFC has a capability with
no klipper-mmu counterpart in the current scope.

- **Lanes / gates** — observation: four independent lane drive motors with independent
  TMC2209 UART configuration, four lane admission switches (`EXTn`) and four bay switches
  (`TRGn`). klipper-mmu lanes and its step/dir + TMC backends cover this directly. The
  per-lane `dist_hub` (95/45/40/90 or 130/80/85/130) is the lane-to-hub tail, and
  `afc_bowden_length` (1145 or 1110) is the shared run to the toolhead.
- **BAY and LOAD switches** — observation: `prep` (`TRGn`) is the bay-present switch and
  `load` (`EXTn`) is the near-lane load switch; these map to klipper-mmu's BAY and LOAD
  inputs. Both are `^` (pull-up) and so active-high in the template's sense.
- **HUB switch** — observation: a real `switch_pin` on `QuattroBox_1:HUB`
  (`templates/AFC_QuattroBox_14.cfg:142`), not `virtual`; klipper-mmu's HUB input matches.
- **Toolhead / arrival** — observation: `[AFC_extruder]` supplies `tool_stn`,
  `tool_stn_unload` and `tool_sensor_after_extruder` rather than a pin; if the user sets
  `pin_tool_start` the arrival sensor is real. klipper-mmu's toolhead switch and arrival
  logic cover this; the placeholder means a profile cannot assume a toolhead sensor exists.
- **Shared path** — observation: hub-to-toolhead is `afc_bowden_length`; lane-to-hub is the
  per-lane tail. klipper-mmu's shared path is the same hub-to-toolhead segment, so the
  per-lane tails belong in the lane model.
- **Rewind outputs** — gap: none configured. AFC's optional `Espooler`
  (`extras/AFC_assist.py:410`, pins `:433-435`) exists in code but no QuattroBox template
  sets `afc_motor_rwd`/`afc_motor_fwd`/`afc_motor_enb`, so there is no rewind binding to
  reproduce. If a user adds one, it is an AFC feature with no QuattroBox-documented wiring.
- **Buffer** — gap/inference: AFC uses a switched Turtleneck-style buffer here
  (`advance_pin`/`trailing_pin`), not the analog FPS. klipper-mmu's reference buffer is
  DiffA/DiffB analog on AFC Lite SW1/PC4 and SW2/PC5 (`docs/architecture.md`), so a
  switched-discrete buffer is a distinct input shape (two digital switches vs one analog
  channel) that the profile must declare. No `[AFC_buffer]` output pins exist, so there is
  nothing for firmware to drive on the buffer itself.
- **LOAD / UNLOAD** — observation: the generic path (`extras/AFC.py:1653-1700` load,
  `:2054`+ unload), which includes the hub-crossing loop (`:1691-1692`) and a
  `move_to_hub` call per attempt (`:1673`). PRELOAD splits into `prep_load` +
  `prep_post_load` (`extras/AFC_BoxTurtle.py:580-628`).
- **EJECT** — observation: `eject_lane` (`extras/AFC_BoxTurtle.py:630-652`) is a bounded
  retract: lane to load switch, repeated hub-length retracts, final extruder-clear retract.
  It matches klipper-mmu's bounded EJECT slice (reverse to fresh BAY-clear evidence), but
  AFC never verifies a fresh BAY transition at the end — it loops on the load switch — so
  the "fresh evidence" guarantee is an inference, not something AFC enforces.
- **FOLLOW** — observation/inference: following comes from the buffer logic; with a
  switched buffer the multiplier is applied on switch state, not on an analog set point, so
  klipper-mmu's analog FPS controller tuning has no QuattroBox number to copy.
- **Gaps with no counterpart:** the 36-LED logo chain with per-lane and spool indices
  (`chain_count: 36`, `led_logo_index: AFC_Indicator: 9-36`), the four eject buttons, and
  the `PSF`/`TN_ADV`/`TN_TRL` alias names. klipper-mmu's feature map lists
  LEDs/buttons as optional bounded MCU I/O plus host policy
  (`docs/plan/feature-map.md`), so these are host-side extras, not control requirements.
  Cut/servo parameters exist in the hub block but are disabled, so they are not a
  requirement either. `unit_load_lane`/`unit_unload_lane` do not exist here.

## 5. Surprises and contradictions in AFC's own files

1. **The unit template includes a file that does not exist.** `[include mcu/MMB_QB.cfg]`
   (`templates/AFC_QuattroBox_14.cfg:3`, `_17.cfg:3`) has no counterpart in `config/mcu/`.
   It only works because the installer rewrites it. A read of the templates alone gives a
   false impression of a shared ABL-style alias file.
2. **`dir_pin` polarity is not consistent between motor variants.** `_14` leaves lanes 1–2
   non-inverted and inverts lanes 3–4 (`templates/AFC_QuattroBox_14.cfg:34`, `:58`, `:82`,
   `:106`); `_17` inverts all four (`_17.cfg:34`, `:57`, `:80`, `:103`). Either the `_14`
   harness reverses two motors or two of those lines are wrong. Do not copy one variant's
   polarity to the other.
3. **`gear_ratio` exists only in `_14`.** `_14` sets `50:10` (`:37`); `_17` omits it while
   keeping the same `rotation_distance: 22.7316868`. The two variants therefore differ in
   effective mm per motor revolution by the gear ratio. This is the single most
   consequential template difference for motion reproduction.
4. **`MMB_1.0_QB` and `MMB_1.1_QB` silently swap two aliases.** `M1_EN` is `PA8` in 1.0
   (`config/mcu/MMB_1.0_QB.cfg:4`) and `PB8` in 1.1 (`MMB_1.1_QB.cfg:4`); `TRG4` is `PB8`
   in 1.0 (`MMB_1.0_QB.cfg:10`) and `PA8` in 1.1 (`MMB_1.1_QB.cfg:10`). A board-revision
   swap with no comment.
5. **`MMB_2.0_QB` defines aliases nothing consumes.** `TN_ADV=PA10`/`TN_TRL=PD9`
   (`config/mcu/MMB_2.0_QB.cfg:12`) suggest a Turtleneck buffer wired to the 2.0 board, but
   the QuattroBox buffer block still uses `<insert_advance_pin>` placeholders
   (`templates/AFC_QuattroBox_14.cfg:15-16`). `PSF` is defined in all three QB alias files
   (`MMB_1.0_QB.cfg:14`, `MMB_1.1_QB.cfg:14`, `MMB_2.0_QB.cfg:16`) and referenced by no
   QuattroBox config.
6. **Cutter parameters are documented but disabled.** The hub block carries `cut_cmd`,
   `cut_dist`, `cut_clear`, `cut_min_length` and three servo angles
   (`templates/AFC_QuattroBox_14.cfg:135-141`) next to `cut: False` (`:129`). Nothing in the
   QuattroBox install path defines a `[servo]`, so the values are inert unless a user
   changes them.
7. **`park_dist` is set but documented as unused.** Both templates set `park_dist: 10`
   (`templates/AFC_QuattroBox_14.cfg:40`), and the lane code calls it "Currently unused"
   (`extras/AFC_lane.py:202`).
8. **The unit config names a buffer that the hardware template does not define under that
   name** — `buffer: QuattroBox_1` (`templates/AFC_QuattroBox_14.cfg:24`) is matched by
   `[AFC_buffer QuattroBox_1]` in the same unit template (`:14`), so this one is
   self-consistent; unlike HTLF (see `htlf.md`), which names a buffer that no file defines.
