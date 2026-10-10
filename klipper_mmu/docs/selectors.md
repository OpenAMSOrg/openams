# Selectors

Welcome to the shared-drive multiplex. A "selector machine" is what happens when you decide that buying one feeder stepper per filament lane is too mainstream, and you'd rather build an intricate, glorious contraption to switch one motor between them.

The `klipper-mmu` module natively understands four major selector families:
- **`KLIPPER_MMU_SELECTOR_KIND_LINEAR`**: A stepper axis moves a carriage left and right until it hits an endstop, and an RC servo pinches the filament against the drive gear (engage, release, move). This runs the ERCF 1.1, 2.0, 3.0, and Tradrack.
- **`KLIPPER_MMU_SELECTOR_KIND_ROTARY`**: A stepper axis moves the selector, and a single motor drives different gates by turning different ways. Used for the 3D Chameleon, MMX6, and HTLF.
- **`KLIPPER_MMU_SELECTOR_KIND_SERVO`**: No selector stepper at all. One RC servo swings around to select the gate. The MMX is the patron saint of this approach, with the Low Rider and Pico MMU in the same congregation.
- **`KLIPPER_MMU_SELECTOR_KIND_INDEXED`**: A stepper axis again, but this one refuses to keep a map. Instead every gate gets its own index switch, and the selector just drives toward whichever one it wants. BTT ViViD and AFC's Claymore both belong to this club, though Claymore's own selector is a good deal more stubborn still -- see [Indexed Gates](#indexed-gates-no-position-table) below, and its own [forward-only](#forward-only-no-gate-order-either) coda.

Independent feeder setups without a shared selector use `KLIPPER_MMU_SELECTOR_KIND_NONE`.

**A word on arrival wiring, regardless of selector kind:** this firmware ends LOAD locally -- a local toolhead switch (`KLIPPER_MMU_PINS_TOOLHEAD`) or FPS arrival, never a remote host sensor relayed over USB. Some devices' stock wiring puts the arrival switch on the printer's own toolhead MCU instead of this board: QIDI Box's Happy Hare profile wires `extruder_switch_pin` to `THR` (docs/devices/qidi_box.md), which is not supported here. Move or duplicate that switch onto a pin this board actually reads before building a QIDI image.

## Configuration and Wiring

When you run `make menuconfig`, you'll find the device presets under **Device, board and lanes**. Picking your hardware there configures the underlying machine kind and its mathematical constants.

The actual pins live under **Configure and reserve MMU pins**. Because the gear motor acts as the feeder for every lane, you wire its stepper exactly as you would lane 0 (`KLIPPER_MMU_PINS_STEP`, `KLIPPER_MMU_PINS_DIR`, `KLIPPER_MMU_PINS_ENABLE`).

Then, under **Selector and servo pins**, you declare the motion:
- `KLIPPER_MMU_PINS_SELECTOR_STEP`, `KLIPPER_MMU_PINS_SELECTOR_DIR`, `KLIPPER_MMU_PINS_SELECTOR_ENABLE` drive the selector carriage.
- `KLIPPER_MMU_PINS_SELECTOR_ENDSTOP` is the physical switch it crashes into to find home. INDEXED devices don't use this one -- see below.
- `KLIPPER_MMU_PINS_SERVO` is your grip servo. This uses standard Klipper scheduled software PWM, meaning you can slap it onto literally any free GPIO pin on your board without begging the hardware timer gods for an allocation.
- `KLIPPER_MMU_PINS_SELECTOR_GATE` is INDEXED-only: one pin per lane, its own index switch, with `KLIPPER_MMU_SELECTOR_GATE_ACTIVE_HIGH`/`KLIPPER_MMU_SELECTOR_GATE_PULL` electrical settings shaped exactly like BAY's.

## Gate Positions and Calibration

Firmware needs to know where the gates physically live. Your `make menuconfig` sets a default (`KLIPPER_MMU_SELECTOR_GATE_POSITIONS_MM`, in millimetres from home). For ROTARY machines this is Happy Hare's own quick-calibration layout: first gate at 4 mm, then one every 25 mm. For LINEAR machines (like ERCF) it is strictly an *estimate* computed from Happy Hare's CAD geometry (`KLIPPER_MMU_SELECTOR_GATE0_POS_UM` and `KLIPPER_MMU_SELECTOR_GATE_WIDTH_UM`, plus ERCF 1.1's bearing blocks), because Happy Hare itself measures linear selectors rather than shipping positions.

Do not trust either to line up your filament. Measure, then give the host your real positions in selector microsteps, one per gate, in your `[klipper_mmu]` section:

```ini
[klipper_mmu box]
selector_gate_steps: 320, 2160, 4000, 5840, 7680, 9520, 11360, 13200
```

The count must equal the lane count or Klipper refuses the config, and the firmware refuses a position past the selector's travel. The host sends them at startup, so they replace the menu defaults without a rebuild.

A gate can also be deliberately left uncalibrated (`-1` for a LINEAR/ROTARY position, `0` for a SERVO angle -- Happy Hare's own convention for "never measured"). That no longer keeps the whole MMU from starting: the firmware boots fine, and only *selecting* that specific gate is refused, with a plain "gate N is not calibrated" instead of a working stepper axis mysteriously not moving there.

## Homing

The `klipper-mmu` controller is fiercely independent about knowing where it is. If the selector loses position—whether because you just booted up or because of a selector fault—the next gate selection will automatically force a home first.

If you want to manually trigger this, use `KLIPPER_MMU_HOME MMU=box` (your `[klipper_mmu]` name). It is refused while filament sits in the shared path, because a selector that moves with filament through it shears it. If the selector is already resting on its endstop when homing begins, it will thoughtfully back off first before re-approaching, rather than just grinding against the switch and declaring victory -- and because the net travel is measured from before that backoff to the switch it finds again, a selector that started right at home reports a travel close to zero, not the backoff-and-return distance.

Turning the selector's motors off -- any fault that stops the machine, not just a deliberate emergency stop -- also un-homes it, the same as Happy Hare's own "assume the position moved once the motor is disabled" rule. A selector that got nudged by hand while its motor was off is never trusted; the next selection just homes again first.

## Indexed Gates (No Position Table)

Every other selector kind in this file trusts a map: home once, then dead-reckon to a remembered position for every gate afterward. `KLIPPER_MMU_SELECTOR_KIND_INDEXED` looks at that whole arrangement and says "what if we just... didn't." Every gate gets its own index switch, and a selection is simply a search for the one you want.

There's no `selector_gate_steps` here -- `KLIPPER_MMU_SELECTOR_GATE_ORDER` replaces it, a comma list naming which lane sits at each physical position going around the selector (BTT ViViD's own order is `0,3,1,2`, because apparently gates don't have to be in a sensible sequence to be indexed sensibly). Given that order and whichever gate the selector last confirmed it was at, it works out the shorter of the two directions around and drives that way until the target's own switch trips. Then it nudges half of `KLIPPER_MMU_SELECTOR_ENDSTOP_WIDTH_MM` further in, to land on the switch's center instead of its leading edge -- the "centering nudge" you may see referenced elsewhere.

Homing is nothing special here either: it's the exact same search, just always aimed at gate 0. If gate 0's switch is already pressed when the search starts (a boot position landed right on it, say), the firmware backs off the full switch width first and searches again, because the underlying search primitive flatly refuses to start on an already-triggered switch. Every other homing/un-homing rule above (auto-home on first selection or after a fault, motors-off un-homes) applies to INDEXED unchanged.

One consequence worth knowing: `KLIPPER_MMU_SELECTOR_HOMING_SPEED_MM_S` is quietly ignored for this kind. Happy Hare's own IndexedSelector never had a separate homing speed to begin with -- every move, search or otherwise, runs at `KLIPPER_MMU_SELECTOR_MOVE_SPEED_MM_S`/`KLIPPER_MMU_SELECTOR_ACCEL_MM_S2` -- so that's what this firmware uses too. The whole-axis travel safety bound also works differently: instead of a CAD gate-position formula, it's `KLIPPER_MMU_SELECTOR_INDEXED_MAX_TRAVEL_MM`, a full-drum-sweep number (BTT ViViD's is 720 mm: 4 gates times a 90 mm CAD pitch times two possible rotations) that ships with no generic default -- a device this catalogue doesn't have CAD evidence for simply refuses to start rather than borrow someone else's number.

## Forward-Only (No Gate Order Either)

BTT ViViD picks the shorter of two directions because it has a map of the whole drum (`selector_gate_order`) to pick a shorter direction *from*. AFC's Claymore doesn't have one, because AFC's own selector code (`extras/AFC_vivid.py`'s `select_lane`, which `AFC_Claymore` inherits unmodified) never bothered comparing directions in the first place -- it always drives the same way, every single time, and lets the search find whichever gate happens to be next.

Leave `KLIPPER_MMU_SELECTOR_GATE_ORDER` empty and this firmware treats that as data, not as "you forgot to configure something": every selection searches `KM_DIR_FORWARD` unconditionally, gate 0 included, the shorter-reverse-hop logic never gets a vote, and `KLIPPER_MMU_SELECTOR_ENDSTOP_WIDTH_MM`'s centering nudge is allowed to be 0 as well (AFC's own `selector_cal_dis` is an optional per-lane fine-tune that Claymore's template simply never sets). BTT ViViD's own real order and nonzero nudge keep working exactly as before -- this is a second valid shape for the same INDEXED kind, not a replacement for the first, and not a new kind of its own.

The practical upshot: on a forward-only machine, selecting gate 1 right after gate 3 drives *forward* past gates 0, 1, 2 and 3 again to reach it, even though spinning the other way would have gotten there in one hop. That is not a bug you get to fix in firmware -- it is AFC's own selector faithfully doing what its source does. If your Claymore's mechanism ever grows a real, calibrated gate order and a switch-width measurement, wire them up here and you get ViViD's shortest-hop behaviour for free; nothing about "forward-only" is INDEXED-kind-specific.

## The Servo

If you have a servo, the firmware needs to know its geometry. The menu gives you direct control over `KLIPPER_MMU_SELECTOR_SERVO_ENGAGE_ANGLE_DEG` (gripping the filament), `KLIPPER_MMU_SELECTOR_SERVO_RELEASE_ANGLE_DEG` (letting it go), and `KLIPPER_MMU_SELECTOR_SERVO_MOVE_ANGLE_DEG` (the clearance angle while traveling).

MMX, which selects with the servo alone, uses one angle per gate instead (`KLIPPER_MMU_SELECTOR_SERVO_GATE_ANGLES_DEG`). Angles become pulse widths through `KLIPPER_MMU_SELECTOR_SERVO_MIN_PULSE_US`, `KLIPPER_MMU_SELECTOR_SERVO_MAX_PULSE_US` and `KLIPPER_MMU_SELECTOR_SERVO_MAX_ANGLE_DEG`.

You also get `KLIPPER_MMU_SELECTOR_SERVO_DWELL_MS` to dictate how long to wait for the servo to finish moving before firing the next step. Since we love our hardware, the pulse stops once the servo has had its dwell, unless you turn on `KLIPPER_MMU_SELECTOR_SERVO_ALWAYS_ACTIVE`.

## The Selector's Own TMC2209

Flip on `KLIPPER_MMU_SELECTOR_UART` (on by default for every selector-stepper device, because Happy Hare's selector is always a TMC2209) and the selector axis stops being a dumb STEP/DIR peasant and joins the gear in TMC2209 society: set up and confirmed over its own single-wire UART during startup's WAITING phase, exactly the same ceremony the gear goes through. No confirmed registers, no publish -- a selector that can't prove its current settings took hold never gets to move, same as any gear lane. Leave `KLIPPER_MMU_PINS_SELECTOR_UART` empty with the option on and the build refuses, rather than quietly demoting your selector to STEP/DIR.

It shares the gear's physical-board settings (sense resistor, current ceiling, register microsteps, interpolation, stealthChop, UART timing), because those describe the chip and board you soldered, not which axis happens to be attached to it. What's genuinely its own: `KLIPPER_MMU_SELECTOR_UART_ADDRESS` (0..3, default 0) and `KLIPPER_MMU_SELECTOR_UART_PULLUP`, plus `KLIPPER_MMU_SELECTOR_RUN_CURRENT`/`KLIPPER_MMU_SELECTOR_HOLD_CURRENT`, whose defaults come straight from Happy Hare's per-device selector stepper templates. They are requested driver settings, not measured current.

The selector can live on its *own* UART wire, distinct from the gear's `KLIPPER_MMU_PINS_UART`, or it can share the gear's wire outright -- Happy Hare's classic ERCF 1.1 EASY-BRD ties both TMC2209s to one pin at different addresses, and this firmware now speaks that dialect too. To share the wire, set `KLIPPER_MMU_PINS_SELECTOR_UART` equal to the gear's own `KLIPPER_MMU_PINS_UART` and give `KLIPPER_MMU_SELECTOR_UART_ADDRESS` a value different from the gear's own `KLIPPER_MMU_TMC_ADDRESSES` -- the two chips can only be told apart on one wire if their addresses actually differ, and the build/startup both refuse a shared pin with equal addresses just as clearly as any other pin reuse. Transactions on the shared wire are fully serialized (Klipper's own `tmc_uart.py` already does this for several TMC2209 on one pin), and each chip keeps its own confirmation, restart and fault state: a latched fault on the gear's driver never faults the selector's, or the other way around. If your board doesn't force the issue, wiring the selector to its own separate pin is simpler and needs none of this address bookkeeping.

## Calibrating a Selector Machine

Measuring `selector_gate_steps` by hand with calipers is a fine way to spend
an afternoon, but you have better things to do. `KLIPPER_MMU_CALIBRATE_SELECTOR`
does the measuring for you and writes the results to `selector_calibration_file`
(an absolute path, opt-in, in your `[klipper_mmu]` section) -- the same
load/validate/atomic-save shape as `bowden_estimate_file`, tagged with your
firmware build so a table measured against one image is never silently applied
to a different one. Its entries override both the Kconfig default and
`selector_gate_steps`, per gate:

```ini
[klipper_mmu box]
selector_calibration_file: ~/printer_data/config/box-selector.json
```

**LINEAR and ROTARY selectors:** run `KLIPPER_MMU_MOTORS_OFF` -- it is the
*selector's own* motor that needs to be free here, not the gear, so it can
be pushed or turned by hand without a fight. Slide or turn the selector
until it is sitting at gate `N`, then run `KLIPPER_MMU_CALIBRATE_SELECTOR
GATE=N`. That one call homes, waits for homing to finish, reads the travel
to the switch, and saves it as gate `N`'s position -- no second call needed.
Do gate 0 and the last gate first, and Happy Hare's own trick kicks in:
every gate in between gets spread evenly across the distance, so you never
have to eyeball the middle ones by hand. Add `SAVE=0` to just see the number
without committing it.

**The SERVO selector kind** skips homing entirely -- there is no axis to
home, only an angle to find. Swing the servo around with `KLIPPER_MMU_SERVO
ANGLE=a` until filament lines up with a gate, then lock it in with
`KLIPPER_MMU_CALIBRATE_SELECTOR GATE=n ANGLE=a`. If your gates are evenly
spaced (most are), add `SPACING=s` and it fills in every other gate `s`
degrees apart, the same shortcut as Happy Hare's own
`MMU_CALIBRATE_SERVO_SELECTOR SPACING=`. A gate you have not calibrated this
way keeps refusing to be selected, quietly and safely, until you do.

Whichever kind you have, a gate that comes out beyond the selector's own
travel or angle range is refused by the firmware and never saved -- better an
honest error than a selector convinced it can reach somewhere it can't.

**The INDEXED selector kind** has no per-gate value for `KLIPPER_MMU_CALIBRATE_SELECTOR`
to measure or save -- there is no `selector_gate_steps` entry and no servo
angle, just each gate's own switch, found fresh on every selection. Both
`KLIPPER_MMU_CALIBRATE_SELECTOR` and `config_km_selector_gate` are refused
outright for this kind. What you configure instead lives in `make menuconfig`
(`KLIPPER_MMU_SELECTOR_GATE_ORDER`, `KLIPPER_MMU_SELECTOR_ENDSTOP_WIDTH_MM`) --
see [Indexed Gates](#indexed-gates-no-position-table) above.

## ERCF's Own Project Options

The Enraged Rabbit Carrot Feeder has spent years accumulating community mods, and Happy Hare's installer still asks about five of them under "Project Options." `make menuconfig`'s **Device, board and lanes** menu offers the same five once you have picked an ERCF version, each scoped to the one version that actually has it -- picking ERCF 2.0 makes Binky, Springy, Triple Decky and Jack Rabbit vanish from the menu entirely, the same way Happy Hare's own installer only ever shows them under the matching `if MMU_TYPE_ERCF_*` block.

- **Springy** (1.1 only) is a sprung selector servo, so you never have to eyeball top-hat tuning again. Firmware-side, it moves `KLIPPER_MMU_SELECTOR_LAST_GATE_OFFSET_UM` from 2.0 mm to 1.2 mm -- and nothing else. That number only ever feeds the whole-axis travel safety bound, never an individual gate's position, so your `KLIPPER_MMU_SELECTOR_GATE_POSITIONS_MM` estimate is bit-for-bit the same with or without Springy fitted.
- **Triple Decky** (1.1 only) swaps in an alternative filament block that no longer needs a bearing block for magnetic gate opening. It changes `KLIPPER_MMU_SELECTOR_GATE_WIDTH_UM` from 21.0 mm to 23.0 mm and `KLIPPER_MMU_SELECTOR_BLOCK_WIDTH_UM` from 5.0 mm to 0 -- which, unlike Springy's change, absolutely does move every gate's estimated position: `4.2,27.2,50.2,73.2,96.2,119.2,142.2,165.2,188.2` instead of the plain 1.1 estimate's `4.2,25.2,46.2,72.2,93.2,114.2,140.2,161.2,182.2`. Fit both Springy and Triple Decky at once and you get Triple Decky's gate list with Springy's last-gate offset -- the two mods change unrelated numbers, so they combine with no surprises.
- **Thumper Blocks** (2.0 only) is a different-geometry filament block. It changes the parked distance behind the extruder encoder homing switch from -13 mm to -11 mm, which maps to `KLIPPER_MMU_UNLOAD_HUB_CLEAR_MM` below going from 13 to 11 -- ERCF has no shared HUB switch, but that option is the same "retract behind whichever switch this build treats as HUB" concept every other device's own `gate_parking_distance` already maps to.
- **Jack Rabbit** (3.0 only) throws the grip servo out entirely: the filament stays gripped by the drive gear at all times (Happy Hare's own `PARAM_FILAMENT_ALWAYS_GRIPPED`). Firmware-side, selecting it clears `KLIPPER_MMU_SELECTOR_LINEAR_HAS_SERVO`, so no servo pin gets claimed and `KLIPPER_MMU_SERVO` resolves off. The selector core does not need to learn anything new for this: a servo-less LINEAR selector already runs the exact same "arriving at the gate position is the engagement" path a ROTARY selector uses, since ROTARY never had a grip servo to begin with.
- **Binky** (1.1 only -- ERCF 2.0 and 3.0 ship it unconditionally, no option needed) swaps the original TCRT5000 opto encoder for a Binky vane wheel. That is genuinely all it changes in Happy Hare: a different `PARAM_ENCODER_RESOLUTION` seed value (millimetres per pulse). This firmware's own encoder driver (`mcu/encoder.c`) is a plain one-wire click counter with no notion of "type" or "resolution" at all -- it counts pulses and hands the host raw clicks, exactly like Klipper's stock `pulse_counter.c` does. So `KLIPPER_MMU_ERCF_BINKY` is honestly just a composition-identity flag: it records that you fitted the mod, and changes no other default, because there is nothing in this Kconfig tree for an encoder "type" to select.

None of the five mods interact with each other's Kconfig defaults except through the shared gate-geometry terms above (Triple Decky's width/block change, Springy's offset change) -- there is no sixth combination lurking anywhere.

## Status

Wondering if your glorious Rube Goldberg machine is alive? Check what `KLIPPER_MMU_QUERY` shows: whether Klipper can currently see the MMU board, whether board startup finished (with diagnostic startup on), and on a selector machine `selector_homed` and `selector_gate` -- whether the selector knows where it is, and which gate it is parked at.

## The Honest "Not Yet" List

We like to keep things grounded in reality. The following capabilities are not yet supported:
- **Hardware qualification**: none of the selector machines has run on real hardware yet. They all build and pass their native, startup and hosted-runtime tests, and every one of them -- ERCF 1.1/2.0/3.0, Jack Rabbit, Tradrack (linear), 3D Chameleon, HTLF, MMX6 (rotary), MMX, Low Rider, Pico MMU (servo), BTT ViViD and Claymore (indexed) -- now also passes the whole-stack simulator end to end.
- **Massive setups**: selector devices go up to 12 lanes (`KM_MAX_LANES`); non-selector images still cap per-lane diagnostics at 8.
- **Pico MMU's cam is uncalibrated out of the box**: Happy Hare ships no gate angles for it (it calibrates them instead), so its gates start up with no angle set. It boots fine now, but selecting any of those gates is refused ("gate N is not calibrated") until you find and save real angles with `KLIPPER_MMU_SERVO`/`KLIPPER_MMU_CALIBRATE_SELECTOR` (above).
- **BTT ViViD's and Claymore's own MCU wiring**: the INDEXED kind (both the shortest-hop and forward-only shapes) and their per-gate switch pins build and pass their native/hosted-config tests, a release-matrix row, a hosted runtime test, a whole-stack simulator device and native switch-driver tests of the per-gate index inputs (`tests/klipper_switch/test_selector_gate.c`); what they lack is only real hardware.
- **Claymore's selector calibration**: no `KLIPPER_MMU_CALIBRATE_SELECTOR`-style operation exists for a forward-only machine yet (there is no gate order or switch width to measure into in the first place, but a per-lane `selector_cal_dis`-equivalent nudge, if you ever wire one up, still has nowhere to be saved).
