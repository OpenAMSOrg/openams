// SPDX-License-Identifier: GPL-3.0-or-later
// Test-only: a filament plant for the sim board -- phase 2b-1's first slice
// of docs/plan/linux-mcu-integration.md's "plant in the loop". Models one
// number per lane: the filament tip's position along the feed path, in
// micrometres. The feeder's STEP/DIR pins move it; the BAY/LOAD/HUB switch
// pins read it back through the exact polarity the firmware is configured
// for (mcu/Kconfig.switch), so klipper-mmu's own admission and progress
// checks (src/operation_core.c) run against a plant that reacts the way a
// real spool and its prep/load/hub switches would.
//
// Pins and electrical settings below are transcribed from the one topology
// this sim board targets (tests/linux_mcu/README.md): BoxTurtle device
// profile, AFC Lite wiring preset, DiffA/DiffB buffer -- the exact preset
// mcu/Kconfig.pins/.switch/.feeder default for
// KLIPPER_MMU_DEVICE_BOXTURTLE && KLIPPER_MMU_BOARD_AFC_LITE, confirmed
// against a resolved build's autoconf.h rather than guessed:
//   STEP    PD4,PC12,PE2,PD15   (active level 1, KLIPPER_MMU_FEEDER_STEP_HIGH)
//   DIR     PD3,PC11,PE3,PD14   (0 = forward, KLIPPER_MMU_FEEDER_FORWARD_HIGH)
//   ENABLE  PD6,PD1,PE0,PC7     (0 = enabled, KLIPPER_MMU_FEEDER_ENABLE_HIGH;
//                                 tracked here for ground truth only -- see
//                                 the note on "gripped" below)
//   BAY     PE13,PB0,PB1,PB2    (active level 0, KLIPPER_MMU_BAY_ACTIVE_HIGH)
//   LOAD    PE8,PE9,PE10,PE11   (active level 1, KLIPPER_MMU_LOAD_ACTIVE_HIGH)
//   HUB     PE12 (shared)       (active level 1, KLIPPER_MMU_HUB_ACTIVE_HIGH)
// A non-selector image with more than 4 lanes (EMU, 5) extends this same
// bench preset instead of a fifth board: lane 4's STEP/DIR/ENABLE/BAY/LOAD
// are PD2/PD7/PD9/PD11/PD12, free on the AFC Lite/BoxTurtle wiring above and
// on the selector topology's own repurposed pins below (the two are never
// compiled together, but keeping them disjoint means a future image cannot
// collide by mixing the comments up). tests/linux_mcu/configs/emu-h723-sim-
// motion.config's KLIPPER_MMU_PINS_UART carries a matching PD10 for lane 4;
// this file does not model TMC UART traffic at all, so no pin table below
// needs it.
//
// This build has no TOOLHEAD switch (CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR=0);
// LOAD_SENSORS and POWERED_REWIND are both on, so PRELOAD/UNPRELOAD target
// the per-lane LOAD switch, not HUB (src/operation_core.c
// km_core_request_preload / request_unload's local_preload path) -- see the
// geometry note below for why HUB still needs a position.
//
// KLIPPER_MMU_FEEDER_STEPS_PER_M (727272 for BoxTurtle, i.e. 727.272
// microsteps/mm) is the firmware image's own conversion, derived from the menu
// mechanics (200 full steps, 16 microsteps, 50:10 gear ratio, 22 mm
// rotation distance; mcu/feeder_mechanics.h); step-to-micrometre math
// below uses it exactly. The three switches' *spacing* along the path is
// not a firmware constant at all -- klipper-mmu only ever watches switch
// levels, never a compiled distance between them -- so the plan
// (docs/plan/linux-mcu-integration.md, "Real time, ... continuous
// integration uses short sim geometry") is explicit that this is the
// sim's own choice, not a transcribed hardware measurement. The values
// below are sim-only, short enough that PRELOAD/UNPRELOAD/EJECT finish in
// well under a second of (wall-anchored) virtual time at the profile's
// 18909 steps/s feed speed (26 mm/s); a nightly soak would use real
// BoxTurtle distances instead, which this phase does not add.
//
// Past the HUB the path keeps going: to the extruder's drive gears and,
// between the feeder and those gears, a compression buffer whose position
// the DiffA/DiffB pair reports. Both are modeled below, plus the extruder's
// own STEP/DIR consumption, so LOAD's FPS arrival and the follower have a
// real buffer to push against instead of a constant (phase 2b-2a).
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <unistd.h>
#include <fcntl.h>
#include "sched.h"               // DECL_INIT
#include "stm32/internal.h"      // GPIO()
#include "board/misc.h"          // timer_read_time()
#include "autoconf.h"
#include "klipper_mmu/types.h"                 // KM_MAX_LANES
#include "klipper_mmu/mcu/feeder_mechanics.h"    // KM_FEEDER_STEPS_PER_M
#include "klipper_mmu/mcu/selector_mechanics.h"  // KM_SELECTOR_*_TO_STEPS/_SERVO_US
#include "plant.h"

#define KM_SIM_PLANT_STEPS_PER_M KM_FEEDER_STEPS_PER_M

// ---- Selector mode (see plant.h and the block near the bottom) -----------
// A build with a selector stepper and/or servo (selector.h's three
// families) replaces the per-lane feeder wiring below with one shared gear
// plus a selector axis and/or servo. A saved header from before the
// selector add-on has neither symbol at all.
#ifndef CONFIG_KLIPPER_MMU_SELECTOR_STEPPER
#define CONFIG_KLIPPER_MMU_SELECTOR_STEPPER 0
#endif
#ifndef CONFIG_KLIPPER_MMU_SERVO
#define CONFIG_KLIPPER_MMU_SERVO 0
#endif
// INDEXED has no separate home endstop pin at all (KLIPPER_MMU_SELECTOR_
// HAS_ENDSTOP is false for it, mcu/Kconfig.selector), so its Kconfig entry
// `depends on KLIPPER_MMU_SELECTOR_HAS_ENDSTOP` -- an INDEXED autoconf.h
// need not define this. mcu/board_startup.c already guards the identical
// case the same way; switch_base_level() below only ever consults this for
// a pin no INDEXED fragment wires (KM_SIM_PLANT_SELECTOR_ENDSTOP_PIN), so
// the fallback value is never actually read there either.
#ifndef CONFIG_KLIPPER_MMU_SELECTOR_ENDSTOP_ACTIVE_HIGH
#define CONFIG_KLIPPER_MMU_SELECTOR_ENDSTOP_ACTIVE_HIGH (-1)
#endif
#define KM_SIM_PLANT_SELECTOR (CONFIG_KLIPPER_MMU_SELECTOR_STEPPER || CONFIG_KLIPPER_MMU_SERVO)

// Every image -- selector and non-selector alike -- tracks its own
// configured lane count (up to KM_MAX_LANES), the way a selector image
// already did. A non-selector image's per-lane feeder tables below are
// sized at KM_MAX_LANES for the same reason the selector topology's own
// bay_pins/gate_switch_pins already are: every lookup bounds its loop at
// KM_SIM_PLANT_LANES, never the array's declared size, so a four-lane
// image's unused tail entries are simply never read.
#define KM_SIM_PLANT_LANES CONFIG_KLIPPER_MMU_LANE_COUNT

// Sim-only geometry (see header comment): distances from the lane's zero
// point (nothing inserted) along the feed path toward the hub.
#define KM_SIM_PLANT_BAY_UM   5000LL   /* 5 mm: prep switch, feeder grip point */
#define KM_SIM_PLANT_LOAD_UM  15000LL  /* 15 mm: per-lane LOAD switch */
#define KM_SIM_PLANT_HUB_UM   80000LL  /* 80 mm: shared HUB switch */

// How far past the HUB the extruder's drive gears sit (where the tip stops
// being pushed and starts being pulled) and how much filament the buffer
// between the two can hold before it is fully compressed. The first is
// overridable with KM_SIM_PLANT_TOOLHEAD_MM; the second with
// KM_SIM_PLANT_BUFFER_MM. The default 15 mm is the real BoxTurtle FPS
// buffer's stroke, which is short enough that a 18000 steps/s follower
// cannot stop inside it: hitting the end is what the LOAD trace shows.
static int64_t km_sim_plant_toolhead_um = 150000LL;      /* 150 mm default */
static int64_t km_sim_plant_buffer_travel_um = 15000LL;  /* 15 mm default */

// The extruder's own STEP/DIR conversion, for how much filament the
// extruder has pulled: rotation_distance 22.6789511 mm and 3200
// microsteps/rev (mcu/config's [extruder] in tests/linux_mcu).
#define KM_SIM_EXTRUDER_UM_PER_STEP_NUM 226789511LL
#define KM_SIM_EXTRUDER_UM_PER_STEP_DEN 32000000LL

static int64_t um_to_steps(int64_t um) {
    return (um * KM_SIM_PLANT_STEPS_PER_M) / 1000000LL;
}
static int64_t steps_to_um(int64_t steps) {
    return (steps * 1000000LL) / KM_SIM_PLANT_STEPS_PER_M;
}

// A lane is "gripped" once its tip has reached the feeder, which sits
// right at the BAY prep switch on this wiring -- an empty lane's STEP
// pulses are observed (steps_seen) but do not move a tip that was never
// inserted far enough to reach the pinch point.
#define KM_SIM_PLANT_GRIP_UM KM_SIM_PLANT_BAY_UM

#define KM_SIM_PLANT_STEP_ACTIVE_LEVEL 1  /* KLIPPER_MMU_FEEDER_STEP_HIGH */
#define KM_SIM_PLANT_DIR_FORWARD_LEVEL 0  /* KLIPPER_MMU_FEEDER_FORWARD_HIGH */

typedef struct {
    uint32_t pin;
} km_sim_plant_pin1_t;

#if !KM_SIM_PLANT_SELECTOR
// Fixed at KM_MAX_LANES regardless of this image's own lane count, same as
// the selector topology's bay_pins/gate_switch_pins below: every lookup
// bounds its loop at KM_SIM_PLANT_LANES, so a four-lane image's own unused
// tail entries (index 4 on) are simply never read. Index 4 is EMU's real
// fifth lane (see the header comment); indices 5-7 just repeat index 4's
// pins since no non-selector image today configures more than 5, and
// mcu/Makefile's own KM_MAX_LANES only widens past 8 for a lane count of
// 9-12 (a selector-only range: no non-selector image asks for it).
static const km_sim_plant_pin1_t step_pins[KM_MAX_LANES] = {
    {GPIO('D', 4)}, {GPIO('C', 12)}, {GPIO('E', 2)}, {GPIO('D', 15)},
    {GPIO('D', 2)}, {GPIO('D', 2)}, {GPIO('D', 2)}, {GPIO('D', 2)},
};
static const km_sim_plant_pin1_t dir_pins[KM_MAX_LANES] = {
    {GPIO('D', 3)}, {GPIO('C', 11)}, {GPIO('E', 3)}, {GPIO('D', 14)},
    {GPIO('D', 7)}, {GPIO('D', 7)}, {GPIO('D', 7)}, {GPIO('D', 7)},
};
static const km_sim_plant_pin1_t enable_pins[KM_MAX_LANES] = {
    {GPIO('D', 6)}, {GPIO('D', 1)}, {GPIO('E', 0)}, {GPIO('C', 7)},
    {GPIO('D', 9)}, {GPIO('D', 9)}, {GPIO('D', 9)}, {GPIO('D', 9)},
};
static const km_sim_plant_pin1_t bay_pins[KM_MAX_LANES] = {
    {GPIO('E', 13)}, {GPIO('B', 0)}, {GPIO('B', 1)}, {GPIO('B', 2)},
    {GPIO('D', 11)}, {GPIO('D', 11)}, {GPIO('D', 11)}, {GPIO('D', 11)},
};
static const km_sim_plant_pin1_t load_pins[KM_MAX_LANES] = {
    {GPIO('E', 8)}, {GPIO('E', 9)}, {GPIO('E', 10)}, {GPIO('E', 11)},
    {GPIO('D', 12)}, {GPIO('D', 12)}, {GPIO('D', 12)}, {GPIO('D', 12)},
};
#else
// Shared-drive selector topology (selector.h): one gear drives whichever
// lane the selector is at, instead of one feeder per lane. Pins below are
// this sim board's own choice for that topology (not a hardware
// measurement, same as the four-lane table above) -- the gear reuses the
// original lane-0 feeder pins, the selector axis and servo reuse three of
// the now-unused lane-1..3 feeder pins, and up to 12 per-gate BAY switches
// reuse the original 4-lane BAY pins plus the now-unused 4-lane LOAD pins
// plus 4 fresh ones, all still within the AFC Lite/BoxTurtle bench wiring
// tests/linux_mcu/README.md documents -- no two selector-image pins here
// collide with each other or with DiffA/DiffB (PC4/PC5) or the extruder
// (PF0-3, PA3).
#define KM_SIM_PLANT_GEAR_STEP_PIN     GPIO('D', 4)
#define KM_SIM_PLANT_GEAR_DIR_PIN      GPIO('D', 3)
#define KM_SIM_PLANT_GEAR_ENABLE_PIN   GPIO('D', 6)
#define KM_SIM_PLANT_SELECTOR_STEP_PIN    GPIO('C', 12)
#define KM_SIM_PLANT_SELECTOR_DIR_PIN     GPIO('C', 11)
#define KM_SIM_PLANT_SELECTOR_ENABLE_PIN  GPIO('D', 1)
#define KM_SIM_PLANT_SELECTOR_ENDSTOP_PIN GPIO('E', 2)
#define KM_SIM_PLANT_SERVO_PIN            GPIO('D', 15)
// Fixed at KM_MAX_LANES regardless of this image's own lane count: every
// lookup below bounds its loop at KM_SIM_PLANT_LANES, never this array's
// size, so the unused tail is simply never read.
static const km_sim_plant_pin1_t bay_pins[KM_MAX_LANES] = {
    {GPIO('E', 13)}, {GPIO('B', 0)}, {GPIO('B', 1)}, {GPIO('B', 2)},
    {GPIO('E', 8)}, {GPIO('E', 9)}, {GPIO('E', 10)}, {GPIO('E', 11)},
    {GPIO('D', 8)}, {GPIO('D', 9)}, {GPIO('D', 10)}, {GPIO('D', 11)},
};
#if CONFIG_KLIPPER_MMU_SELECTOR_KIND_INDEXED
// INDEXED's own per-gate index switches (klipper_mmu/selector.h: no gate
// position table -- a switch per lane instead). Both devices this
// catalogue implements (BTT ViViD, Claymore) are 4 lanes, so only the
// first four are real; the tail is never read (bounded at KM_SIM_PLANT_
// LANES like every other lookup here) and just repeats the last real pin.
// The four real ones reuse the now-unused four-lane LOAD preset's own
// pins (PE8-11), free on this topology because INDEXED devices leave
// KLIPPER_MMU_LOAD_SENSORS off, same as every other selector kind.
static const km_sim_plant_pin1_t gate_switch_pins[KM_MAX_LANES] = {
    {GPIO('E', 8)}, {GPIO('E', 9)}, {GPIO('E', 10)}, {GPIO('E', 11)},
    {GPIO('E', 11)}, {GPIO('E', 11)}, {GPIO('E', 11)}, {GPIO('E', 11)},
    {GPIO('E', 11)}, {GPIO('E', 11)}, {GPIO('E', 11)}, {GPIO('E', 11)},
};
#endif
#endif
#define KM_SIM_PLANT_HUB_PIN GPIO('E', 12)
// QIDI Box's own shape (tests/linux_mcu/devices.py, docs/devices/qidi_box.md):
// a real physical toolhead-arrival switch (no FPS/DiffA-DiffB pair) and a
// tension-only buffer switch (KLIPPER_MMU_BUFFER_SINGLE_SWITCH). Neither
// collides with the DiffA/DiffB pair (PC4/PC5) above, the extruder
// (PF0-3, PA3) or the four-lane feeder table: QIDI has no gear TMC UART and
// no LOAD switches, so its own PINS_TOOLHEAD/PINS_BUFFER_A reuse the AFC
// Lite bench's own PE7 (toolhead) and PC4 (buffer) connector positions --
// the same "board wiring, not device behaviour" bench every device row
// here shares, just wired to a different signal on this device.
#define KM_SIM_PLANT_TOOLHEAD_PIN GPIO('E', 7)
#define KM_SIM_PLANT_BUFFER_TENSION_PIN GPIO('C', 4)

// ---- Shared-path filament encoder (mcu/encoder.c) -------------------------
// One wire (KLIPPER_MMU_ENCODER_DUAL_PIN off, the default, matching Happy
// Hare's own single-pin encoder): PD0 is free on both the four-lane preset's
// selector-mode branch (that preset's own PD0, used as lane 1's TMC UART pin,
// only exists in the OTHER (!KM_SIM_PLANT_SELECTOR) branch above -- the two
// never compile into the same image) and the 12-entry BAY table in every
// selector-kind fragment that turns the encoder on: ERCF 1.1/2.0/3.0 (the
// only devices with CONFIG_KLIPPER_MMU_ENCODER on, mcu/Kconfig.control) are
// 9/8/8 lanes, so BAY only ever reaches index 8 at most (PD8) -- PD8 itself
// is NOT free (ERCF 1.1's own 9th BAY switch sits there), which is why this
// pin is PD0 and not PD8: an earlier version of this comment claimed PD8 was
// free because "ERCF 2.0 only fills its first 8" BAY slots, true for ERCF 2.0
// alone but not for ERCF 1.1's 9 lanes once that device gained a sim (this
// pass's own finding). mcu/encoder.c counts a click on every level change on
// its pin, so the plant toggles this one: once per KM_SIM_PLANT_ENCODER_UM_PER_
// CLICK of real, gripped, unjammed shaft travel -- km_sim_plant_encoder_step()
// is called only from the exact position_steps update every STEP edge
// already gates on grip/jam (see both branches of km_sim_plant_notify_write
// below), so a jam or an ungripped lane produces zero clicks, the same way
// a real encoder never sees filament that has not reached it.
//
// The resolution itself: mcu/encoder.h says plainly that a click is "one
// tick per calibrated resolution mm" -- but that mm-per-click number is the
// HOST's calibrated value (klippy's mmu_encoder/Happy Hare's own
// MmuEncoder.set_resolution), never compiled into this firmware. Checked --
// mcu/Kconfig.pins/.control and include/klipper_mmu/device_profile.h carry
// no encoder resolution field at all, only pins and a pull setting. This is
// this pass's finding: the image has no resolution to read the way this
// file already reads KM_FEEDER_STEPS_PER_M or CONFIG_KLIPPER_MMU_SELECTOR_
// GATE_POSITIONS_MM for its other geometry, so the sim cannot take one from
// it either. What follows is sim-only test geometry instead, exactly like
// KM_SIM_PLANT_TOOLHEAD_MM/BUFFER_MM above (overridable, not a firmware
// value) -- anchored to a real part rather than invented outright: ERCF
// 2.0's own Happy Hare oracle default encoder, a Binky-8 vane wheel at
// 1.469 mm/count (installer/mmu_types/Kconfig.ercf:224-231 in the pinned
// Happy Hare checkout, already cited docs/devices/ercf_2_0.json:1293).
#define KM_SIM_PLANT_ENCODER_A_PIN GPIO('D', 0)
#define KM_SIM_PLANT_ENCODER_UM_PER_CLICK_DEFAULT 1469LL /* 1.469 mm, in um */
static int64_t km_sim_plant_encoder_um_per_click = KM_SIM_PLANT_ENCODER_UM_PER_CLICK_DEFAULT;
static int64_t km_sim_plant_encoder_accum_steps; /* remainder, 0 <= x < per-click steps */
static uint8_t km_sim_plant_encoder_level;
static uint64_t km_sim_plant_encoder_clicks;     /* ground truth only; not read by firmware */

typedef struct {
    int64_t position_steps;
    uint64_t steps_seen;
    uint32_t dir_changes;
    int8_t last_move_dir;   /* 0 = no step yet, else +1/-1 */
    uint8_t dir_level;      /* last written DIR pin level */
    uint8_t enable_level;   /* last written ENABLE pin level (informational) */
    uint8_t step_level;     /* last written STEP pin level, for edge detect */
    /* Injected trouble (see "Injected trouble" below): a forward jam, and a
     * spool that has run out. block_steps < 0 means "no jam". The strand's
     * trailing end only means anything once runout is set. */
    int64_t block_steps;    /* forward motion refuses to pass this position */
    int64_t block_um;       /* the same limit as requested, for ground truth */
    int64_t tail_steps;     /* trailing end of the strand while runout is set */
    uint8_t runout;         /* spool empty: strand is tail_steps..position_steps */
    /* KM_SIM_PLANT_LANE_ROTATION_UM: this lane's real gear moves rot_num/rot_den
     * reference steps per motor step (0 = exactly one); rot_acc carries the
     * remainder so positions stay exact integers. */
    uint32_t rot_num, rot_den;
    int64_t rot_acc;
} km_sim_plant_lane_t;

static km_sim_plant_lane_t lanes[KM_SIM_PLANT_LANES];
static char ground_truth_path[512];
static int have_ground_truth_path;

// The extruder is shared, not per lane: one filament is loaded into it at a
// time, and only its STEP/DIR pins (PF0/PF1, forward = DIR high) say how much
// it has pulled. See km_sim_plant_notify_extruder_write().
static int64_t extruder_position_steps;
static uint8_t extruder_dir_level;
static uint8_t extruder_step_level;
static int64_t extruder_consumed_um;

static int find_lane(const km_sim_plant_pin1_t *table, uint32_t pin) {
    for (int i = 0; i < KM_SIM_PLANT_LANES; ++i)
        if (table[i].pin == pin) return i;
    return -1;
}

static int64_t bay_threshold_steps(void) { return um_to_steps(KM_SIM_PLANT_BAY_UM); }
static int64_t load_threshold_steps(void) { return um_to_steps(KM_SIM_PLANT_LOAD_UM); }
static int64_t hub_threshold_steps(void) { return um_to_steps(KM_SIM_PLANT_HUB_UM); }
// A device with no per-lane BAY switch at all (KLIPPER_MMU_BAY_SENSORS=n:
// Angry Beaver, 3MS, QuattroBox 1.0/1.1/2 -- their traced Happy Hare
// profiles render no per-gate entry sensor, and their own device_profile
// carries "filament_always_gripped": true) has nothing to have been
// inserted up to: the gear already holds the tip from position 0. Gating
// every step on the ordinary BAY-relative grip point would then refuse
// forward motion forever for a lane that starts at "empty" (position 0),
// which is exactly how such a device's lane starts (no BAY switch to have
// arrived at) -- so the grip point itself is 0 without a BAY sensor.
static int64_t grip_threshold_steps(void) {
#if CONFIG_KLIPPER_MMU_BAY_SENSORS
    return um_to_steps(KM_SIM_PLANT_GRIP_UM);
#else
    return 0;
#endif
}
static int64_t toolhead_threshold_steps(void) {
    return um_to_steps(KM_SIM_PLANT_HUB_UM + km_sim_plant_toolhead_um);
}

// One qualifying shaft step has just actually moved filament (called right
// after a position_steps update in either direction -- retracting clicks
// too, exactly like a real encoder that cannot tell load from unload).
// Accumulating whole steps and converting only the per-click *threshold*
// once (like every other geometry check in this file converts a threshold,
// never a running position) keeps this exact integer math, with no
// per-step rounding drift over a long feed.
static void km_sim_plant_encoder_step(void) {
    int64_t per_click_steps = um_to_steps(km_sim_plant_encoder_um_per_click);
    if (per_click_steps <= 0) return;
    if (++km_sim_plant_encoder_accum_steps < per_click_steps) return;
    km_sim_plant_encoder_accum_steps -= per_click_steps;
    km_sim_plant_encoder_level ^= 1;
    ++km_sim_plant_encoder_clicks;
}

// One motor step moves the lane's filament by its own gear (rot_num/rot_den).
static void lane_advance(km_sim_plant_lane_t *l, int8_t dir) {
    int64_t moved = dir;
    if (l->rot_num) {
        l->rot_acc += (int64_t)dir * l->rot_num;
        moved = l->rot_acc / (int64_t)l->rot_den;
        l->rot_acc -= moved * (int64_t)l->rot_den;
    }
    l->position_steps += moved;
    for (int64_t i = moved < 0 ? -moved : moved; i > 0; --i)
        km_sim_plant_encoder_step();
    if (l->runout) l->tail_steps += moved;
}

#if KM_SIM_PLANT_SELECTOR
// ---- Selector axis, gear and servo (selector.h's shared-drive machines) --
// The selector axis's own position, in microsteps from its home endstop
// (0 at/below home; the axis never goes negative -- a real endstop is a
// hard stop, not a spring). The gear's DIR/STEP/ENABLE levels are tracked
// the same way the four-lane table above tracked each lane's, just with
// only one lane's worth of state (there is only one gear).
static int64_t selector_position_steps;
static uint8_t selector_dir_level, selector_step_level, selector_enable_level;
static uint8_t gear_dir_level, gear_enable_level, gear_step_level;

// The servo's pulse train: km_servo_pwm_setup (mcu/board_startup.c) drives
// it with Klipper's own scheduled software PWM, so what the plant sees is a
// plain GPIO toggling with the pulse high for pulse_us and then low for the
// rest of the period. servo_pulse_us is the width of the most recently
// *completed* pulse (updated on the falling edge); it is sticky once the
// servo stops pulsing (servo_hold false, PARAM servo_always_active off) --
// exactly what a person reading the last commanded position would see.
static uint32_t servo_pulse_us;
static uint32_t servo_rising_time;
static uint8_t servo_pin_level;

// Never negative: a real home endstop is a hard mechanical stop, not a
// spring, and integer step positions need no clamping to land exactly on
// it. Forward motion is unbounded here (the core's own selector_home.h
// enforces max_travel_steps; the plant does not need a second copy of that
// bound to model the physical axis).
static void selector_move(int8_t dir) {
    selector_position_steps += dir;
#if !CONFIG_KLIPPER_MMU_SELECTOR_KIND_INDEXED
    if (selector_position_steps < 0) selector_position_steps = 0;
#endif
    /* INDEXED's axis is a drum with no endstop at all (klipper_mmu/
     * selector.h: home is just gate 0's own switch) -- it goes negative
     * freely and indexed_gate_asserted() below wraps it, unlike LINEAR/
     * ROTARY's real endstop-anchored travel clamped above. */
}

// The selector axis has its own microsteps-per-metre (KM_SELECTOR_STEPS_PER_M,
// selector_mechanics.h) -- a different ratio from the gear's KM_FEEDER_
// STEPS_PER_M steps_to_um() above uses, so reporting the selector's own
// position needs this, not that one. 0 on a pure SERVO image (no axis).
static int64_t selector_steps_to_um(int64_t steps) {
#if CONFIG_KLIPPER_MMU_SELECTOR_STEPPER
    return (steps * 1000000LL) / KM_SELECTOR_STEPS_PER_M;
#else
    (void)steps;
    return 0;
#endif
}

// A tick-count difference to microseconds, using the same CONFIG_CLOCK_FREQ
// sim/board/timer.c's timer_from_us() does the inverse of. 32-bit wraparound
// is not a concern for one servo pulse (well under a second).
static uint32_t ticks_to_us(uint32_t ticks) {
    return ticks / (uint32_t)(CONFIG_CLOCK_FREQ / 1000000u);
}

// A small, permissive comma-list reader for the same Kconfig strings
// mcu/config.c's parse_scaled_list resolves the firmware's own selector
// profile from (CONFIG_KLIPPER_MMU_SELECTOR_GATE_POSITIONS_MM and friends):
// this is not a second copy of the values, just a second, less strict
// parse of the identical build-time string, good enough for a test-only
// sim (mcu/config.c's own strict grammar already gates a real build).
// Returns the number of entries read.
static unsigned parse_double_list(const char *s, double *out, unsigned max) {
    unsigned n = 0;
    while (*s && n < max) {
        out[n++] = atof(s);
        while (*s && *s != ',') ++s;
        if (*s == ',') ++s;
    }
    return n;
}

// Per-gate selector positions, in microsteps from home, converted with the
// firmware's own KM_SELECTOR_UM_TO_STEPS (selector_mechanics.h) -- the same
// formula mcu/config.c applies to the same string, so a gate the host has
// not (re)calibrated with config_km_selector_gate lands exactly where this
// build's firmware would put it.
static int32_t gate_steps[KM_MAX_LANES];
#if CONFIG_KLIPPER_MMU_SELECTOR_KIND_ROTARY
// NOT a selector approach direction: the gear's own drive direction while
// gate g is gripped (klipper_mmu/selector.h, gate_direction). 0 normal,
// 1 reverse -- the core inverts the trajectory's DIR sense for a reverse
// gate, so from this sim's own DIR-pin-level point of view forward filament
// motion at a reverse gate arrives on the electrical level this table says,
// not KM_SIM_PLANT_DIR_FORWARD_LEVEL.
static uint8_t gate_reverse[KM_MAX_LANES];
#endif
#if CONFIG_KLIPPER_MMU_SELECTOR_KIND_LINEAR
static uint16_t servo_engage_us, servo_release_us, servo_move_us;
#endif
#if CONFIG_KLIPPER_MMU_SELECTOR_KIND_SERVO
// Pure SERVO selection has no selector axis at all: one servo angle per
// gate stands in for a whole selector move.
static uint16_t servo_gate_us[KM_MAX_LANES];
#endif
#if CONFIG_KLIPPER_MMU_SELECTOR_KIND_INDEXED
// INDEXED (selector.h, docs/selectors.md "Indexed Gates"): the firmware
// keeps no gate position table at all -- it only ever searches for a
// switch. The plant still needs *some* physical layout to decide when each
// gate's own index switch is asserted, and that layout is sim wiring, not a
// device fact (this file's own header comment): a drum that wraps, one
// gate every gate_pitch_steps around it, gate 0's window centred on the
// home reference (raw axis 0), built from what the image *does* carry --
// gate_order (which lane sits at which physical position) and the whole-
// axis travel bound -- rather than inventing a bare pitch from nothing.
// gate_pitch_steps: Happy Hare's own IndexedSelector hardcodes exactly two
// possible drum rotations for every device it knows
// (extras/mmu/unit/selectors/mmu_indexed_selector.py:45-49
// cad_max_rotations=2, unoverridden anywhere), and its whole-axis bound is
// num_gates * cad_gate_width * cad_max_rotations
// (KLIPPER_MMU_SELECTOR_INDEXED_MAX_TRAVEL_MM). Inverting that with the
// same "2" gives one drum circumference (num_gates * cad_gate_width) per
// image, and dividing by the lane count gives the pitch -- for BTT ViViD
// this reconstructs the real CAD pitch exactly (720 mm / 2 / 4 gates =
// 90 mm, docs/devices/vivid.json's own "gate_positions" decision); Claymore
// carries no CAD data at all to reconstruct (mcu/Kconfig.selector's own
// help text), so its pitch here is this sim's own documented invention,
// not a measurement (see this device's Kconfig fragment and this
// directory's README).
static int32_t indexed_position_of_lane[KM_MAX_LANES]; /* physical slot index */
static int32_t indexed_gate_pitch_steps;
static int32_t indexed_circumference_steps; /* lane_count * pitch; the wrap */
static int32_t indexed_half_width_steps;    /* ENDSTOP_WIDTH_MM / 2 */
#endif

// Small tolerances, not firmware constants: "close enough" for a sim that
// only ever sees whole microsteps and a servo pulse rounded to microseconds
// (KM_SELECTOR_SERVO_US), never the fractional values a real potentiometer
// or encoder would report.
#define KM_SIM_PLANT_SELECTOR_TOLERANCE_STEPS \
    ((int64_t)KM_SELECTOR_UM_TO_STEPS(100) + 1) /* 0.1 mm */
#define KM_SIM_PLANT_SERVO_TOLERANCE_US 20

static void selector_load_profile(void) {
    unsigned lanes_n = KM_SIM_PLANT_LANES;
    double mm[KM_MAX_LANES];
#if CONFIG_KLIPPER_MMU_SELECTOR_STEPPER && !CONFIG_KLIPPER_MMU_SELECTOR_KIND_INDEXED
    /* INDEXED keeps no such table at all -- mcu/config.c's own km_fill_
     * selector never reads this macro for that kind either (config.c:242-
     * 265 vs. :266-276), and CONFIG_KLIPPER_MMU_SELECTOR_GATE_POSITIONS_MM's
     * Kconfig entry `depends on !KLIPPER_MMU_SELECTOR_KIND_INDEXED`, so an
     * INDEXED autoconf.h need not even define it. */
    unsigned got = parse_double_list(CONFIG_KLIPPER_MMU_SELECTOR_GATE_POSITIONS_MM,
                                     mm, lanes_n);
    for (unsigned i = 0; i < got; ++i)
        gate_steps[i] = KM_SELECTOR_UM_TO_STEPS((int64_t)(mm[i] * 1000.0 + 0.5));
#endif
#if CONFIG_KLIPPER_MMU_SELECTOR_KIND_INDEXED
    {
        /* Drum geometry (see the static declarations above for the "why").
         * gate_order defaults to identity (lane i physically at slot i)
         * when the image leaves it empty -- AFC's own forward-only sentinel
         * (klipper_mmu/selector.h, device_profile.c) -- which is a fine sim
         * placement since a forward-only device never picks a direction
         * from it anyway; only this plant's own bookkeeping needs *a*
         * layout to place switches at all. */
        int64_t circumference_um =
            (int64_t)CONFIG_KLIPPER_MMU_SELECTOR_INDEXED_MAX_TRAVEL_MM * 1000LL / 2;
        int64_t pitch_um = circumference_um / (int64_t)(lanes_n ? lanes_n : 1);
        indexed_gate_pitch_steps = KM_SELECTOR_UM_TO_STEPS(pitch_um);
        indexed_circumference_steps = indexed_gate_pitch_steps * (int32_t)lanes_n;
        indexed_half_width_steps = KM_SELECTOR_UM_TO_STEPS(
            (int64_t)CONFIG_KLIPPER_MMU_SELECTOR_ENDSTOP_WIDTH_MM * 1000LL / 2);

        for (unsigned i = 0; i < lanes_n; ++i) indexed_position_of_lane[i] = (int32_t)i;
        unsigned norder = parse_double_list(CONFIG_KLIPPER_MMU_SELECTOR_GATE_ORDER,
                                            mm, lanes_n);
        if (norder == lanes_n) {
            int32_t order[KM_MAX_LANES];
            uint16_t seen = 0;
            int valid = 1;
            for (unsigned i = 0; i < lanes_n; ++i) {
                order[i] = (int32_t)(mm[i] + 0.5);
                if (order[i] < 0 || (unsigned)order[i] >= lanes_n
                    || (seen & (1u << order[i]))) { valid = 0; break; }
                seen |= (uint16_t)(1u << order[i]);
            }
            if (valid)
                for (unsigned i = 0; i < lanes_n; ++i)
                    indexed_position_of_lane[order[i]] = (int32_t)i;
        }
    }
#endif
#if CONFIG_KLIPPER_MMU_SELECTOR_KIND_ROTARY
    double dir[KM_MAX_LANES];
    unsigned ndir = parse_double_list(CONFIG_KLIPPER_MMU_SELECTOR_GATE_DIRECTIONS,
                                      dir, lanes_n);
    for (unsigned i = 0; i < ndir; ++i) gate_reverse[i] = dir[i] != 0.0;
#endif
#if CONFIG_KLIPPER_MMU_SELECTOR_KIND_LINEAR && CONFIG_KLIPPER_MMU_SERVO
    /* A servo-less LINEAR selector (Jack Rabbit, ERCF 3.0's mod that clears
     * KLIPPER_MMU_SELECTOR_LINEAR_HAS_SERVO) has no KM_SELECTOR_SERVO_US
     * macro at all -- selector_mechanics.h only defines it `#if
     * CONFIG_KLIPPER_MMU_SERVO`. This block used to guard only on KIND_
     * LINEAR, which linked fine for every LINEAR device that has a servo
     * but failed with "undefined reference to KM_SELECTOR_SERVO_US" the
     * first time a servo-less LINEAR image was ever built (this pass's own
     * finding, tests/linux_mcu/configs/jack-rabbit-h723-sim-motion.config).
     */
    servo_engage_us = KM_SELECTOR_SERVO_US(CONFIG_KLIPPER_MMU_SELECTOR_SERVO_ENGAGE_ANGLE_DEG);
    servo_release_us = KM_SELECTOR_SERVO_US(CONFIG_KLIPPER_MMU_SELECTOR_SERVO_RELEASE_ANGLE_DEG);
    servo_move_us = KM_SELECTOR_SERVO_US(CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MOVE_ANGLE_DEG);
#endif
#if CONFIG_KLIPPER_MMU_SELECTOR_KIND_SERVO
    double deg[KM_MAX_LANES];
    unsigned nservo = parse_double_list(CONFIG_KLIPPER_MMU_SELECTOR_SERVO_GATE_ANGLES_DEG,
                                        deg, lanes_n);
    for (unsigned i = 0; i < nservo; ++i)
        servo_gate_us[i] = KM_SELECTOR_SERVO_US((int32_t)(deg[i] + 0.5));
    /* Pico MMU ships this table empty on purpose (mcu/Kconfig.selector:
     * "Happy Hare calibrates them instead", docs/selectors.md) -- the
     * firmware's own boot-time table stays empty here too, so a build with
     * this image genuinely refuses an uncalibrated gate the same way a real
     * one would. But this plant can only ever drive a lane's filament for a
     * gate whose servo angle it recognizes (selector_engaged_gate() below),
     * and it has no visibility into the host's own runtime calibration
     * (km_selector_gate_set is a wire command to the firmware, never to this
     * board) -- so a scenario that calibrates gates at runtime and then
     * expects real feed motion needs to tell this sim-only board what to
     * expect too, exactly the same kind of override as KM_SIM_PLANT_
     * TOOLHEAD_MM above. Pulse widths in microseconds, one per lane; 0 (or a
     * lane past the list) is "still uncalibrated", the same sentinel the
     * compiled table already uses. Never set for any other device's image;
     * a device that ships a real compiled table needs no override and gets
     * none. */
    const char *pulse_us = getenv("KM_SIM_PLANT_SERVO_GATE_PULSE_US");
    if (pulse_us && *pulse_us) {
        double raw[KM_MAX_LANES];
        unsigned n = parse_double_list(pulse_us, raw, lanes_n);
        for (unsigned i = 0; i < n; ++i) servo_gate_us[i] = (uint16_t)(raw[i] + 0.5);
    }
#endif
}

#if CONFIG_KLIPPER_MMU_SELECTOR_KIND_INDEXED
// True while the carriage sits inside `lane`'s own index-switch window: the
// drum position folded to its shortest signed distance from that gate's
// centre (so gate 0's window, centred on the wrap seam, reads asserted
// whether the raw axis is just above 0 or just below a full circumference,
// exactly the "drum that wraps" behaviour), within half the switch width
// either side.
static int indexed_gate_asserted(unsigned lane) {
    if (indexed_circumference_steps <= 0) return 0;
    int64_t center = (int64_t)indexed_position_of_lane[lane] * indexed_gate_pitch_steps;
    int64_t d = (selector_position_steps - center) % indexed_circumference_steps;
    if (d > indexed_circumference_steps / 2) d -= indexed_circumference_steps;
    else if (d < -indexed_circumference_steps / 2) d += indexed_circumference_steps;
    return d >= -indexed_half_width_steps && d <= indexed_half_width_steps;
}
#endif

// The gate the gear may currently drive, or -1: LINEAR needs the axis
// within tolerance of a gate AND the servo holding its engage pulse; ROTARY
// needs only the axis within tolerance (no servo); INDEXED needs the axis
// inside that gate's own index-switch window (no position table, no
// servo); pure SERVO needs the servo pulse within tolerance of that gate's
// own angle (no axis at all).
static int selector_engaged_gate(void) {
#if CONFIG_KLIPPER_MMU_SELECTOR_KIND_INDEXED
    for (unsigned g = 0; g < KM_SIM_PLANT_LANES; ++g)
        if (indexed_gate_asserted(g)) return (int)g;
    return -1;
#elif CONFIG_KLIPPER_MMU_SELECTOR_STEPPER
    for (unsigned g = 0; g < KM_SIM_PLANT_LANES; ++g) {
        int64_t d = selector_position_steps - gate_steps[g];
        if (d < 0) d = -d;
        if (d > KM_SIM_PLANT_SELECTOR_TOLERANCE_STEPS) continue;
#if CONFIG_KLIPPER_MMU_SELECTOR_KIND_LINEAR
        if (CONFIG_KLIPPER_MMU_SERVO) {
            int32_t pd = (int32_t)servo_pulse_us - (int32_t)servo_engage_us;
            if (pd < 0) pd = -pd;
            if (pd > KM_SIM_PLANT_SERVO_TOLERANCE_US) continue;
        }
#endif
        return (int)g;
    }
    return -1;
#else
    for (unsigned g = 0; g < KM_SIM_PLANT_LANES; ++g) {
        int32_t pd = (int32_t)servo_pulse_us - (int32_t)servo_gate_us[g];
        if (pd < 0) pd = -pd;
        if (pd <= KM_SIM_PLANT_SERVO_TOLERANCE_US) return (int)g;
    }
    return -1;
#endif
}

// True while the gate direction convention for `gate` reads DIR levels
// inverted from the plain KM_SIM_PLANT_DIR_FORWARD_LEVEL sense (ROTARY's
// reverse gates only; see gate_reverse above).
static uint8_t gate_dir_reversed(int gate) {
#if CONFIG_KLIPPER_MMU_SELECTOR_KIND_ROTARY
    return gate >= 0 && gate_reverse[gate];
#else
    (void)gate;
    return false;
#endif
}
#endif /* KM_SIM_PLANT_SELECTOR */

// ---- Injected trouble: jams, runouts and forced switches ----------------
// Driven by sim/board/control.c's control socket (docs/plan/linux-mcu-
// integration.md, "A control socket"). This lives next to the position and
// switch logic it perturbs because that is where its meaning does: a jam is
// a statement about the filament path, and a forced switch level is the same
// pin answer km_sim_plant_switch_level() already owns.

#define KM_SIM_PLANT_SW_FREE   0  /* the plant answers for itself */
#define KM_SIM_PLANT_SW_STUCK  1  /* always the given electrical level */
#define KM_SIM_PLANT_SW_BOUNCE 2  /* square wave, period_us per half cycle */
#define KM_SIM_PLANT_MAX_OVERRIDES (KM_SIM_PLANT_LANES * 2 + 1) /* BAY+LOAD+HUB */

typedef struct {
    uint32_t pin;
    uint8_t mode;
    uint8_t level;      /* stuck level */
    uint32_t period_us; /* bounce half-cycle */
    uint32_t start;     /* timer_read_time() when the override was armed */
} km_sim_plant_override_t;

static km_sim_plant_override_t switch_overrides[KM_SIM_PLANT_MAX_OVERRIDES];

static km_sim_plant_override_t *find_override(uint32_t pin) {
    for (unsigned i = 0; i < KM_SIM_PLANT_MAX_OVERRIDES; ++i)
        if (switch_overrides[i].mode != KM_SIM_PLANT_SW_FREE
            && switch_overrides[i].pin == pin)
            return &switch_overrides[i];
    return NULL;
}

// Does lane `lane`'s strand cover a point on the path at `threshold_steps`?
// The tip does from the moment it arrives; so does the trailing end -- until
// a runout has moved it past, which is how a spool's tail stops holding the
// BAY switch made while the tip itself keeps travelling toward the hub.
static int segment_covers(int lane, int64_t threshold_steps) {
    const km_sim_plant_lane_t *l = &lanes[lane];
    if (l->position_steps < threshold_steps) return 0;
    if (l->runout && l->tail_steps > threshold_steps) return 0;
    return 1;
}

// The polarity-corrected level the plant itself answers with, ignoring any
// injected override: the one place that knows which switch pin is which.
static int switch_base_level(uint32_t pin) {
    if (pin == KM_SIM_PLANT_HUB_PIN) {
        for (int i = 0; i < KM_SIM_PLANT_LANES; ++i)
            if (segment_covers(i, hub_threshold_steps())) return 1; /* active high */
        return 0;
    }
    if (pin == KM_SIM_PLANT_TOOLHEAD_PIN) {
        /* Real physical toolhead switch (QIDI Box; docs/devices/qidi_box.md's
         * arrival note): same aggregate-over-lanes shape as HUB above,
         * because only one lane is ever actually fed this far in this sim. */
        for (int i = 0; i < KM_SIM_PLANT_LANES; ++i)
            if (segment_covers(i, toolhead_threshold_steps())) return 1; /* active high */
        return 0;
    }
    if (pin == KM_SIM_PLANT_BUFFER_TENSION_PIN)
        /* Tension-only switch (KLIPPER_MMU_BUFFER_SINGLE_SWITCH): this
         * bench's short scenarios never run the buffer dry, so the real
         * switch never trips -- constant at rest (active high, so 0). */
        return 0;
    int lane = find_lane(bay_pins, pin);
    if (lane >= 0)
        return segment_covers(lane, bay_threshold_steps()) ? 0 : 1; /* active low */
#if KM_SIM_PLANT_SELECTOR
#if CONFIG_KLIPPER_MMU_SELECTOR_STEPPER
    if (pin == KM_SIM_PLANT_SELECTOR_ENDSTOP_PIN)
        return selector_position_steps <= 0
            ? (CONFIG_KLIPPER_MMU_SELECTOR_ENDSTOP_ACTIVE_HIGH > 0)
            : (CONFIG_KLIPPER_MMU_SELECTOR_ENDSTOP_ACTIVE_HIGH <= 0);
#endif
#if CONFIG_KLIPPER_MMU_SELECTOR_KIND_INDEXED
    lane = find_lane(gate_switch_pins, pin);
    if (lane >= 0)
        return indexed_gate_asserted((unsigned)lane) ? 1 : 0; /* active high, LOAD's own convention */
#endif
#else
    lane = find_lane(load_pins, pin);
    if (lane >= 0)
        return segment_covers(lane, load_threshold_steps()) ? 1 : 0; /* active high */
#endif
#if CONFIG_KLIPPER_MMU_ENCODER
    /* Only an image with the encoder owns this pin: on the four-lane preset
     * PD0 is lane 1's TMC UART. */
    if (pin == KM_SIM_PLANT_ENCODER_A_PIN) return km_sim_plant_encoder_level;
#endif
    return -1;
}

static int apply_override(uint32_t pin, int base) {
    const km_sim_plant_override_t *o = find_override(pin);
    if (!o) return base;
    if (o->mode == KM_SIM_PLANT_SW_STUCK) return o->level;
    /* A bounce needs one clock shared by the firmware and the injection, and
     * timer_read_time() is it: virtual inside a timer callback (where the
     * control socket services commands), wall-anchored otherwise, monotonic
     * either way (see sim/board/timer.c). */
    return (int)(((timer_read_time() - o->start) / o->period_us) & 1u);
}

// ---- Initial state (KM_SIM_PLANT) ----------------------------------------

static void set_initial_state(int lane, const char *state) {
    if (lane < 0 || lane >= KM_SIM_PLANT_LANES) return;
    if (!strcmp(state, "bay")) {
        /* Inserted up to the BAY switch: exactly at its threshold, so BAY
         * reads active and LOAD/HUB do not. */
        lanes[lane].position_steps = bay_threshold_steps();
    } else if (!strcmp(state, "load")) {
        /* Inserted up to the per-lane LOAD switch: BAY and LOAD active, HUB
         * (and the toolhead past it) not. What a spool that just fed to its
         * own LOAD sensor looks like, and how load_check.py starts. */
        lanes[lane].position_steps = load_threshold_steps();
    } else if (!strcmp(state, "hub")) {
        /* Inserted up to the shared HUB switch. */
        lanes[lane].position_steps = hub_threshold_steps();
    } else {
        /* "empty" (and anything unrecognized, conservatively): nothing
         * inserted. */
        lanes[lane].position_steps = 0;
    }
}

static void load_initial_state(void) {
    const char *path = getenv("KM_SIM_PLANT");
    if (!path || !*path) return;
    FILE *f = fopen(path, "r");
    if (!f) return;
    char line[256];
    while (fgets(line, sizeof(line), f)) {
        int lane = -1;
        char state[32] = {0};
        if (sscanf(line, " lane = %d state = %31s", &lane, state) == 2
            || sscanf(line, " lane=%d state=%31s", &lane, state) == 2)
            set_initial_state(lane, state);
    }
    fclose(f);
}

// ---- Ground truth (KM_SIM_PLANT_OUT, control socket "truth") -------------

// One lane's fields, no newline. Shared by the SIGUSR1 dump and the control
// socket's "truth" reply so the two can never drift apart. bay/load/hub are
// physical presence (1 = filament there), not pin levels: ground truth is
// where the filament is, so a stuck or bouncing switch shows up as the
// firmware disagreeing with it. buffer_permille is the shared buffer's
// compression (see km_sim_plant_buffer_permille), toolhead whether this
// lane's strand reaches the extruder, and extruder_consumed_um the shared
// extruder's total draw -- the last two repeat on every lane line because
// the hardware is shared.
static int format_lane_fields(char *buf, size_t size, int i) {
    int hub = 0;
    for (int j = 0; j < KM_SIM_PLANT_LANES; ++j)
        if (segment_covers(j, hub_threshold_steps())) hub = 1;
#if KM_SIM_PLANT_SELECTOR
    return snprintf(buf, size,
        "lane=%d position_um=%lld steps=%llu dir_changes=%u"
        " bay=%d load=%d hub=%d block_um=%lld runout=%u"
        " buffer_permille=%d toolhead=%d extruder_consumed_um=%lld"
        " selector_position_um=%lld servo_pulse_us=%u engaged_gate=%d"
        " encoder_clicks=%llu encoder_um_per_click=%lld",
        i, (long long)steps_to_um(lanes[i].position_steps),
        (unsigned long long)lanes[i].steps_seen,
        (unsigned)lanes[i].dir_changes,
        segment_covers(i, bay_threshold_steps()),
        segment_covers(i, load_threshold_steps()), hub,
        (long long)lanes[i].block_um, (unsigned)lanes[i].runout,
        km_sim_plant_buffer_permille(),
        segment_covers(i, toolhead_threshold_steps()),
        (long long)extruder_consumed_um,
        (long long)selector_steps_to_um(selector_position_steps),
        (unsigned)servo_pulse_us, selector_engaged_gate(),
        (unsigned long long)km_sim_plant_encoder_clicks,
        (long long)km_sim_plant_encoder_um_per_click);
#else
    return snprintf(buf, size,
        "lane=%d position_um=%lld steps=%llu dir_changes=%u"
        " bay=%d load=%d hub=%d block_um=%lld runout=%u"
        " buffer_permille=%d toolhead=%d extruder_consumed_um=%lld"
        " encoder_clicks=%llu encoder_um_per_click=%lld",
        i, (long long)steps_to_um(lanes[i].position_steps),
        (unsigned long long)lanes[i].steps_seen,
        (unsigned)lanes[i].dir_changes,
        segment_covers(i, bay_threshold_steps()),
        segment_covers(i, load_threshold_steps()), hub,
        (long long)lanes[i].block_um, (unsigned)lanes[i].runout,
        km_sim_plant_buffer_permille(),
        segment_covers(i, toolhead_threshold_steps()),
        (long long)extruder_consumed_um,
        (unsigned long long)km_sim_plant_encoder_clicks,
        (long long)km_sim_plant_encoder_um_per_click);
#endif
}

// Chain snprintf() appends without ever letting the offset leave the buffer,
// so a truncated reply is still NUL-terminated and still a valid C string.
static size_t append_str(char *buf, size_t size, size_t off, int n) {
    if (n < 0 || !size) return off;
    off += (size_t)n;
    if (off >= size) off = size - 1;
    return off;
}

static void dump_ground_truth(void) {
    if (!have_ground_truth_path) return;
    int fd = open(ground_truth_path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) return;
    for (int i = 0; i < KM_SIM_PLANT_LANES; ++i) {
        char buf[400]; /* two new encoder_* fields since selector mode added; see the guard below */
        int n = format_lane_fields(buf, sizeof(buf) - 1, i);
        if (n <= 0 || (size_t)n > sizeof(buf) - 2) continue;
        buf[n++] = '\n';
        ssize_t written = write(fd, buf, (size_t)n);
        (void)written; /* best-effort test evidence, not a protected write */
    }
    close(fd);
}

static void handle_sigusr1(int signo) {
    (void)signo;
    dump_ground_truth();
}

void km_sim_plant_init(void) {
    memset(lanes, 0, sizeof(lanes));
    for (int i = 0; i < KM_SIM_PLANT_LANES; ++i) {
        lanes[i].block_steps = -1; /* no injected jam until asked for one */
        lanes[i].block_um = -1;
    }
    /* Sim-only per-lane gear: um per output turn, comma list by lane; empty or
     * 0 keeps the unit's own. */
    const char *rot = getenv("KM_SIM_PLANT_LANE_ROTATION_UM");
    for (int i = 0; rot && *rot && i < KM_SIM_PLANT_LANES; ++i) {
        char *end;
        long um = strtol(rot, &end, 10);
        if (um > 0 && um != CONFIG_KLIPPER_MMU_FEEDER_ROTATION_DISTANCE_UM) {
            lanes[i].rot_num = (uint32_t)um;
            lanes[i].rot_den = CONFIG_KLIPPER_MMU_FEEDER_ROTATION_DISTANCE_UM;
        }
        rot = *end == ',' ? end + 1 : "";
    }
    load_initial_state();
#if KM_SIM_PLANT_SELECTOR
    selector_load_profile();
#endif

    /* Sim-only geometry knobs, for a scenario that wants the real Bowden
     * path length or a different buffer stroke. Read before anything can
     * ask for a position, so the thresholds see them. */
    const char *th = getenv("KM_SIM_PLANT_TOOLHEAD_MM");
    if (th && *th) km_sim_plant_toolhead_um = (int64_t)(atof(th) * 1000.0);
    const char *buf = getenv("KM_SIM_PLANT_BUFFER_MM");
    if (buf && *buf) km_sim_plant_buffer_travel_um = (int64_t)(atof(buf) * 1000.0);
    if (km_sim_plant_buffer_travel_um < 1) km_sim_plant_buffer_travel_um = 1;

    /* The shared-path encoder's own sim-only resolution (see the header
     * comment by KM_SIM_PLANT_ENCODER_A_PIN for why this is not a firmware
     * value): reset every run, same as the lane table above, since a
     * native test may call km_sim_plant_init() more than once per process. */
    km_sim_plant_encoder_um_per_click = KM_SIM_PLANT_ENCODER_UM_PER_CLICK_DEFAULT;
    const char *enc = getenv("KM_SIM_PLANT_ENCODER_MM_PER_CLICK");
    if (enc && *enc) km_sim_plant_encoder_um_per_click = (int64_t)(atof(enc) * 1000.0 + 0.5);
    if (km_sim_plant_encoder_um_per_click < 1) km_sim_plant_encoder_um_per_click = 1;
    km_sim_plant_encoder_accum_steps = 0;
    km_sim_plant_encoder_level = 0;
    km_sim_plant_encoder_clicks = 0;

    const char *out = getenv("KM_SIM_PLANT_OUT");
    if (out && *out) {
        snprintf(ground_truth_path, sizeof(ground_truth_path), "%s", out);
        have_ground_truth_path = 1;
        atexit(dump_ground_truth);
        struct sigaction sa;
        memset(&sa, 0, sizeof(sa));
        sa.sa_handler = handle_sigusr1;
        sigaction(SIGUSR1, &sa, NULL);
    }
}
DECL_INIT(km_sim_plant_init);

// ---- gpio.c hooks -----------------------------------------------------

#if !KM_SIM_PLANT_SELECTOR
void km_sim_plant_notify_write(uint32_t pin, uint8_t level, uint32_t time) {
    (void)time;
    int lane = find_lane(dir_pins, pin);
    if (lane >= 0) { lanes[lane].dir_level = level; return; }
    lane = find_lane(enable_pins, pin);
    if (lane >= 0) { lanes[lane].enable_level = level; return; }
    lane = find_lane(step_pins, pin);
    if (lane < 0) return;
    km_sim_plant_lane_t *l = &lanes[lane];
    uint8_t was = l->step_level;
    l->step_level = level;
    if (was == level || level != KM_SIM_PLANT_STEP_ACTIVE_LEVEL) return;
    /* A qualifying rising edge to the active STEP level: one pulse. */
    l->steps_seen++;
    int8_t dir = l->dir_level == KM_SIM_PLANT_DIR_FORWARD_LEVEL ? 1 : -1;
    if (l->last_move_dir != 0 && l->last_move_dir != dir) l->dir_changes++;
    l->last_move_dir = dir;
    if (l->position_steps < grip_threshold_steps()) return;
    /* An injected jam refuses forward motion only: retracting a blocked
     * strand still works, exactly as it would on hardware. Integer step
     * positions mean the refusal needs no clamping to land on. */
    if (dir > 0 && l->block_steps >= 0 && l->position_steps >= l->block_steps)
        return;
    lane_advance(l, dir);
}
#else
// Selector-mode edges: the selector axis moves on its own STEP/DIR (LINEAR/
// ROTARY only); the servo's pulse width comes from its pin's own rising-
// to-falling gap (LINEAR grip or pure SERVO selection); the one shared gear
// STEP/DIR moves whichever lane is currently engaged (selector_engaged_gate),
// exactly the way each lane's own STEP/DIR used to move that lane directly.
void km_sim_plant_notify_write(uint32_t pin, uint8_t level, uint32_t time) {
    if (pin == KM_SIM_PLANT_GEAR_DIR_PIN) { gear_dir_level = level; return; }
    if (pin == KM_SIM_PLANT_GEAR_ENABLE_PIN) { gear_enable_level = level; return; }
#if CONFIG_KLIPPER_MMU_SELECTOR_STEPPER
    if (pin == KM_SIM_PLANT_SELECTOR_DIR_PIN) { selector_dir_level = level; return; }
    if (pin == KM_SIM_PLANT_SELECTOR_ENABLE_PIN) { selector_enable_level = level; return; }
    if (pin == KM_SIM_PLANT_SELECTOR_STEP_PIN) {
        uint8_t was = selector_step_level;
        selector_step_level = level;
        if (was == level || level != KM_SIM_PLANT_STEP_ACTIVE_LEVEL) return;
        selector_move(selector_dir_level == KM_SIM_PLANT_DIR_FORWARD_LEVEL ? 1 : -1);
        return;
    }
#endif
#if CONFIG_KLIPPER_MMU_SERVO
    if (pin == KM_SIM_PLANT_SERVO_PIN) {
        uint8_t was = servo_pin_level;
        servo_pin_level = level;
        if (was == level) return;
        if (level) { servo_rising_time = time; return; }
        servo_pulse_us = ticks_to_us(time - servo_rising_time);
        return;
    }
#endif
    if (pin != KM_SIM_PLANT_GEAR_STEP_PIN) return;
    uint8_t was = gear_step_level;
    gear_step_level = level;
    if (was == level || level != KM_SIM_PLANT_STEP_ACTIVE_LEVEL) return;
    /* A qualifying rising edge: one gear pulse, but it only reaches a lane
     * while that lane's gate is actually engaged (selector.h: "the selector
     * moves only while the shared path is clear ... or the requested lane
     * is already selected" -- the core never steps the gear otherwise, but
     * the plant enforces it too rather than trusting that). */
    int gate = selector_engaged_gate();
    if (gate < 0) return;
    km_sim_plant_lane_t *l = &lanes[gate];
    l->steps_seen++;
    int8_t raw_dir = gear_dir_level == KM_SIM_PLANT_DIR_FORWARD_LEVEL ? 1 : -1;
    int8_t dir = gate_dir_reversed(gate) ? (int8_t)-raw_dir : raw_dir;
    if (l->last_move_dir != 0 && l->last_move_dir != dir) l->dir_changes++;
    l->last_move_dir = dir;
    if (l->position_steps < grip_threshold_steps()) return;
    if (dir > 0 && l->block_steps >= 0 && l->position_steps >= l->block_steps)
        return;
    lane_advance(l, dir);
}
#endif /* KM_SIM_PLANT_SELECTOR */

// The extruder's own STEP/DIR pins (PF0/PF1, from [extruder] in the sim
// printer config). PF1 is DIR -- high is forward on this wiring -- and PF0
// is STEP: count a rising edge as one microstep of filament pulled, signed
// by the DIR level, and keep the total in micrometres.
void km_sim_plant_notify_extruder_write(uint32_t pin, uint8_t level, uint32_t time) {
    (void)time;
    if (pin == GPIO('F', 1)) {
        extruder_dir_level = level;
        return;
    }
    if (pin != GPIO('F', 0)) return;
    uint8_t was = extruder_step_level;
    extruder_step_level = level;
    if (was == level || level != 1) return;
    int8_t dir = extruder_dir_level ? 1 : -1;
    extruder_position_steps += dir;
    extruder_consumed_um = (extruder_position_steps
                            * KM_SIM_EXTRUDER_UM_PER_STEP_NUM)
        / KM_SIM_EXTRUDER_UM_PER_STEP_DEN;
}

// How compressed the buffer between the feeder and the extruder is, 0..1000
// permille (0 = uncompressed rest, 1000 = the end of its stroke, which is
// what the firmware's BUFFER_SIGNAL reads). The feeder pushes filament past
// the HUB toward the extruder; the extruder pulls it back out. Compression
// is therefore how far the farthest tip has been pushed past the extruder's
// grip point minus what the extruder has since consumed. Before the tip
// reaches the extruder the buffer is slack: 0.
int km_sim_plant_buffer_permille(void) {
    int64_t max_past_hub = 0;
    for (int i = 0; i < KM_SIM_PLANT_LANES; ++i) {
        int64_t past_hub = steps_to_um(lanes[i].position_steps) - KM_SIM_PLANT_HUB_UM;
        if (past_hub > max_past_hub) max_past_hub = past_hub;
    }
    if (max_past_hub < km_sim_plant_toolhead_um) return 0;
    int64_t buffer_um = (max_past_hub - km_sim_plant_toolhead_um)
        - extruder_consumed_um;
    if (buffer_um < 0) buffer_um = 0;
    if (buffer_um > km_sim_plant_buffer_travel_um)
        buffer_um = km_sim_plant_buffer_travel_um;
    return (int)((buffer_um * 1000LL) / km_sim_plant_buffer_travel_um);
}

int km_sim_plant_switch_level(uint32_t pin) {
    int base = switch_base_level(pin);
    return base < 0 ? -1 : apply_override(pin, base);
}

int64_t km_sim_plant_position_um(uint8_t lane) {
    if (lane >= KM_SIM_PLANT_LANES) return 0;
    return steps_to_um(lanes[lane].position_steps);
}

uint64_t km_sim_plant_encoder_click_count(void) { return km_sim_plant_encoder_clicks; }
int64_t km_sim_plant_encoder_resolution_um(void) { return km_sim_plant_encoder_um_per_click; }

int64_t km_sim_plant_selector_position_um(void) {
#if KM_SIM_PLANT_SELECTOR
    return selector_steps_to_um(selector_position_steps);
#else
    return 0;
#endif
}

uint32_t km_sim_plant_servo_pulse_us(void) {
#if KM_SIM_PLANT_SELECTOR
    return servo_pulse_us;
#else
    return 0;
#endif
}

int km_sim_plant_engaged_gate(void) {
#if KM_SIM_PLANT_SELECTOR
    return selector_engaged_gate();
#else
    return -1;
#endif
}

// ---- Control socket hooks (sim/board/control.c) --------------------------

int km_sim_plant_set_block(uint8_t lane, int64_t um) {
    if (lane >= KM_SIM_PLANT_LANES) return -1;
    lanes[lane].block_steps = um < 0 ? -1 : um_to_steps(um);
    lanes[lane].block_um = um < 0 ? -1 : um;
    return 0;
}

int km_sim_plant_set_runout(uint8_t lane) {
    if (lane >= KM_SIM_PLANT_LANES) return -1;
    lanes[lane].runout = 1;
    lanes[lane].tail_steps = lanes[lane].position_steps;
    return 0;
}

int km_sim_plant_set_switch_override(uint32_t pin, uint8_t mode, uint8_t level,
                                     uint32_t period_us) {
    if (switch_base_level(pin) < 0) return -1; /* not a switch the plant owns */
    if (mode != KM_SIM_PLANT_SW_FREE && mode != KM_SIM_PLANT_SW_STUCK
        && mode != KM_SIM_PLANT_SW_BOUNCE) return -1;
    if (mode == KM_SIM_PLANT_SW_BOUNCE && !period_us) return -1;
    km_sim_plant_override_t *slot = find_override(pin);
    if (!slot) {
        for (unsigned i = 0; i < KM_SIM_PLANT_MAX_OVERRIDES; ++i)
            if (switch_overrides[i].mode == KM_SIM_PLANT_SW_FREE) {
                slot = &switch_overrides[i];
                break;
            }
        if (!slot) return -1; /* table full; cannot happen for 9 real pins */
    }
    slot->pin = pin;
    slot->mode = mode;
    slot->level = level;
    slot->period_us = period_us;
    slot->start = timer_read_time();
    return 0;
}

int km_sim_plant_format_truth(char *buf, size_t size) {
    size_t off = 0;
    if (!buf || !size) return 0;
    off = append_str(buf, size, off, snprintf(buf, size, "ok"));
    for (int i = 0; i < KM_SIM_PLANT_LANES; ++i) {
        off = append_str(buf, size, off, snprintf(buf + off, size - off, " "));
        off = append_str(buf, size, off,
                         format_lane_fields(buf + off, size - off, i));
    }
    off = append_str(buf, size, off, snprintf(buf + off, size - off, "\n"));
    return (int)off;
}
