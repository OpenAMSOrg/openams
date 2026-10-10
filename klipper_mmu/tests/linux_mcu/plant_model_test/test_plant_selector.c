// SPDX-License-Identifier: GPL-3.0-or-later
// Native unit test for sim/board/plant.c's selector mode (selector.h's
// shared-drive machines), shaped like the ERCF 2.0 sim image
// tests/linux_mcu/devices.py builds (see shim_selector/autoconf.h and
// run.py): one gear, a LINEAR selector axis + grip servo, 8 lanes. Drives
// synthetic STEP/DIR/servo edges the way the real firmware's driver setup
// would (mcu/board_startup.c setup_selector(), src/operation_core.c) and
// checks the plant's own physical truth, not the firmware -- there is no
// firmware in this test, only the board model. Gate positions and servo
// pulse widths are computed with the same KM_SELECTOR_UM_TO_STEPS/
// _SERVO_US macros the firmware and plant.c both use, so this test can
// never silently drift from either.
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "sched.h"
#include "stm32/internal.h"
#include "board/misc.h"
#include "autoconf.h"
#include "klipper_mmu/mcu/selector_mechanics.h"
#include "plant.h"

extern uint32_t km_sim_fake_now;

#define GEAR_STEP GPIO('D', 4)
#define GEAR_DIR  GPIO('D', 3)
#define SEL_STEP  GPIO('C', 12)
#define SEL_DIR   GPIO('C', 11)
#define SEL_ENDSTOP GPIO('E', 2)
#define SERVO_PIN GPIO('D', 15)
#define BAY0_PIN  GPIO('E', 13)
#define BAY3_PIN  GPIO('B', 2)
#define HUB_PIN   GPIO('E', 12)
#define ENCODER_A_PIN GPIO('D', 0)

/* This shim's autoconf.h: DIR forward is the same electrical convention
 * plant.c hardcodes for every axis (KM_SIM_PLANT_DIR_FORWARD_LEVEL). */
#define DIR_FORWARD 0
#define DIR_REVERSE 1

static int failures;
#define CHECK(cond, fmt, ...) do { \
    if (!(cond)) { \
        failures++; \
        fprintf(stderr, "FAIL %s:%d: " fmt "\n", __FILE__, __LINE__, ##__VA_ARGS__); \
    } \
} while (0)

static void selector_step(uint8_t dir_level) {
    km_sim_fake_now += 5;
    km_sim_plant_notify_write(SEL_DIR, dir_level, km_sim_fake_now);
    km_sim_fake_now += 5;
    km_sim_plant_notify_write(SEL_STEP, 1, km_sim_fake_now);
    km_sim_fake_now += 5;
    km_sim_plant_notify_write(SEL_STEP, 0, km_sim_fake_now);
}

static void gear_step(uint8_t dir_level) {
    km_sim_fake_now += 5;
    km_sim_plant_notify_write(GEAR_DIR, dir_level, km_sim_fake_now);
    km_sim_fake_now += 5;
    km_sim_plant_notify_write(GEAR_STEP, 1, km_sim_fake_now);
    km_sim_fake_now += 5;
    km_sim_plant_notify_write(GEAR_STEP, 0, km_sim_fake_now);
}

/* One servo pulse of `pulse_us`, the same way Klipper's scheduled software
 * PWM drives the pin: high for the pulse width, then low. plant.c's
 * ticks_to_us() divides by CONFIG_CLOCK_FREQ/1e6 ticks per microsecond, so
 * the fake clock has to advance in ticks, not microseconds, for that
 * division to recover pulse_us exactly. */
#define TICKS_PER_US ((uint32_t)(CONFIG_CLOCK_FREQ / 1000000))
static void servo_pulse(uint16_t pulse_us) {
    km_sim_fake_now += 2000 * TICKS_PER_US;
    km_sim_plant_notify_write(SERVO_PIN, 1, km_sim_fake_now);
    km_sim_fake_now += (uint32_t)pulse_us * TICKS_PER_US;
    km_sim_plant_notify_write(SERVO_PIN, 0, km_sim_fake_now);
}

int main(int argc, char **argv) {
    const char *scratch = argc > 1 ? argv[1] : ".";
    char state_path[512];
    snprintf(state_path, sizeof(state_path), "%s/plant-selector-state.txt", scratch);

    /* Gates 0 and 3, like device_scenarios.py's ERCF 2.0 PRELOAD step: both
     * already parked at their own gate's BAY switch, every other gate empty. */
    FILE *f = fopen(state_path, "w");
    if (!f) { fprintf(stderr, "cannot write %s\n", state_path); return 1; }
    fprintf(f, "lane=0 state=bay\nlane=3 state=bay\n");
    for (int i = 1; i < 8; ++i) if (i != 3) fprintf(f, "lane=%d state=empty\n", i);
    fclose(f);
    setenv("KM_SIM_PLANT", state_path, 1);

    km_sim_plant_init();

    const int32_t gate0_steps = KM_SELECTOR_UM_TO_STEPS(4000);   /* 4.0 mm */
    const int32_t gate3_steps = KM_SELECTOR_UM_TO_STEPS(73000);  /* 73.0 mm */
    const uint16_t engage_us = KM_SELECTOR_SERVO_US(100);  /* servo_down_angle */
    const uint16_t release_us = KM_SELECTOR_SERVO_US(30);  /* servo_up_angle */

    /* Before any homing move, the axis has not travelled and no gate is
     * anywhere near it. */
    CHECK(km_sim_plant_selector_position_um() == 0,
         "selector should start at 0, got %lld",
         (long long)km_sim_plant_selector_position_um());
    CHECK(km_sim_plant_engaged_gate() == -1, "nothing should be engaged before homing");

    /* Drive the axis away from home, then home it (reverse) back to the
     * endstop; the axis never goes negative (a real endstop is a hard
     * stop), so over-driving reverse just leaves it at exactly 0. */
    for (int i = 0; i < 400; ++i) selector_step(DIR_FORWARD);
    CHECK(km_sim_plant_switch_level(SEL_ENDSTOP) == 0,
         "endstop should not read asserted away from home");
    for (int i = 0; i < 900; ++i) selector_step(DIR_REVERSE);
    CHECK(km_sim_plant_selector_position_um() == 0,
         "homing should land exactly on 0, got %lld",
         (long long)km_sim_plant_selector_position_um());
    CHECK(km_sim_plant_switch_level(SEL_ENDSTOP) == 1,
         "endstop should read asserted at home");

    /* Move to gate 0's configured position (the same Kconfig gate list
     * mcu/config.c resolves this build's firmware profile from). */
    for (int32_t i = 0; i < gate0_steps; ++i) selector_step(DIR_FORWARD);
    CHECK(km_sim_plant_engaged_gate() == -1,
         "gate 0 should not be engaged before the servo grips (gate=%d)",
         km_sim_plant_engaged_gate());

    servo_pulse(engage_us);
    CHECK(km_sim_plant_servo_pulse_us() == engage_us,
         "servo pulse %u != engage pulse %u", km_sim_plant_servo_pulse_us(), engage_us);
    CHECK(km_sim_plant_engaged_gate() == 0,
         "gate 0 should be engaged (axis at gate0, servo at engage pulse), got %d",
         km_sim_plant_engaged_gate());

    /* BAY switches are per-gate physical presence, independent of the
     * selector's own position: gate 0's is asserted (state=bay), gate 3's
     * is asserted too even though the selector has never visited it yet. */
    CHECK(km_sim_plant_switch_level(BAY0_PIN) == 0, "gate 0 BAY should read asserted (active-low)");
    CHECK(km_sim_plant_switch_level(BAY3_PIN) == 0, "gate 3 BAY should read asserted (active-low)");
    CHECK(km_sim_plant_switch_level(HUB_PIN) == 0, "HUB should not be active yet");

    /* The gear only ever moves the engaged gate's own filament. */
    int64_t g0_before = km_sim_plant_position_um(0);
    int64_t g3_before = km_sim_plant_position_um(3);
    int64_t per_click = km_sim_plant_encoder_resolution_um();
    uint64_t clicks_before = km_sim_plant_encoder_click_count();
    for (int i = 0; i < 3000; ++i) gear_step(DIR_FORWARD);
    CHECK(km_sim_plant_position_um(0) > g0_before,
         "gate 0's filament did not advance while engaged: before=%lld after=%lld",
         (long long)g0_before, (long long)km_sim_plant_position_um(0));
    CHECK(km_sim_plant_position_um(3) == g3_before,
         "gate 3's filament moved while a different gate was engaged");

    /* The shared-path encoder (mcu/encoder.c) only ever sees real, gripped,
     * unjammed shaft travel -- so its click count over this window should
     * track gate 0's own real travel to within one click's rounding at
     * each end (plant.c's KM_SIM_PLANT_ENCODER_A_PIN comment has the "why
     * this resolution" citation; this test never repeats that number, only
     * the accessor that reports it). */
    CHECK(per_click > 0, "encoder resolution should be positive, got %lld",
         (long long)per_click);
    int64_t g0_travel = km_sim_plant_position_um(0) - g0_before;
    uint64_t clicks_after = km_sim_plant_encoder_click_count();
    CHECK(clicks_after > clicks_before,
         "encoder produced no clicks at all while gate 0 moved %lld um",
         (long long)g0_travel);
    int64_t click_travel = (int64_t)(clicks_after - clicks_before) * per_click;
    int64_t travel_diff = click_travel - g0_travel;
    CHECK(travel_diff > -2 * per_click && travel_diff < 2 * per_click,
         "encoder clicks (%llu, %lld um) do not track gate 0's real travel "
         "(%lld um) within 2 clicks (%lld um each)",
         (unsigned long long)(clicks_after - clicks_before),
         (long long)click_travel, (long long)g0_travel, (long long)per_click);
    CHECK(km_sim_plant_switch_level(ENCODER_A_PIN) == 0
          || km_sim_plant_switch_level(ENCODER_A_PIN) == 1,
         "encoder pin should read a plain 0/1 level, got %d",
         km_sim_plant_switch_level(ENCODER_A_PIN));

    /* A jam refuses gate 0's forward motion (km_sim_plant_set_block); the
     * encoder, seeing no real travel, ticks no further -- exactly like a
     * real encoder that never sees filament stuck behind an obstruction. */
    int64_t jammed_at = km_sim_plant_position_um(0);
    CHECK(km_sim_plant_set_block(0, jammed_at) == 0, "set_block(0, ...) should be accepted");
    uint64_t clicks_jam_before = km_sim_plant_encoder_click_count();
    for (int i = 0; i < 500; ++i) gear_step(DIR_FORWARD);
    CHECK(km_sim_plant_position_um(0) == jammed_at,
         "gate 0 should not advance while jammed, moved to %lld",
         (long long)km_sim_plant_position_um(0));
    CHECK(km_sim_plant_encoder_click_count() == clicks_jam_before,
         "a jam should produce no further encoder clicks (before=%llu after=%llu)",
         (unsigned long long)clicks_jam_before,
         (unsigned long long)km_sim_plant_encoder_click_count());
    CHECK(km_sim_plant_set_block(0, -1) == 0, "clearing the jam should be accepted");

    /* Releasing the servo disengages the gate even though the axis has not
     * moved off it. */
    servo_pulse(release_us);
    CHECK(km_sim_plant_servo_pulse_us() == release_us,
         "servo pulse %u != release pulse %u", km_sim_plant_servo_pulse_us(), release_us);
    CHECK(km_sim_plant_engaged_gate() == -1,
         "gate should disengage on release even at the same axis position, got %d",
         km_sim_plant_engaged_gate());

    /* Move the selector to gate 3 and grip it there. */
    int32_t delta = gate3_steps - gate0_steps;
    for (int32_t i = 0; i < delta; ++i) selector_step(DIR_FORWARD);
    CHECK(km_sim_plant_engaged_gate() == -1,
         "should not be engaged mid-move with the servo still released");
    servo_pulse(engage_us);
    CHECK(km_sim_plant_engaged_gate() == 3,
         "gate 3 should be engaged after the move and grip, got %d",
         km_sim_plant_engaged_gate());

    int64_t g0_after_move = km_sim_plant_position_um(0);
    for (int i = 0; i < 3000; ++i) gear_step(DIR_FORWARD);
    CHECK(km_sim_plant_position_um(3) > g3_before,
         "gate 3's filament did not advance once engaged");
    CHECK(km_sim_plant_position_um(0) == g0_after_move,
         "gate 0's filament moved after the selector left it");

    /* The wire format (control.c's "truth" reply and the SIGUSR1 dump)
     * carries the three selector-mode fields plus the two encoder ones. */
    char truth[4096];
    int n = km_sim_plant_format_truth(truth, sizeof(truth));
    CHECK(n > 0, "format_truth returned nothing");
    CHECK(strstr(truth, "selector_position_um=") != NULL, "truth missing selector_position_um=: %s", truth);
    CHECK(strstr(truth, "servo_pulse_us=") != NULL, "truth missing servo_pulse_us=: %s", truth);
    CHECK(strstr(truth, "engaged_gate=3") != NULL, "truth missing engaged_gate=3: %s", truth);
    CHECK(strstr(truth, "encoder_clicks=") != NULL, "truth missing encoder_clicks=: %s", truth);
    CHECK(strstr(truth, "encoder_um_per_click=") != NULL, "truth missing encoder_um_per_click=: %s", truth);

    remove(state_path);

    if (failures) {
        fprintf(stderr, "%d check(s) failed\n", failures);
        return 1;
    }
    printf("plant selector-mode test passed.\n");
    return 0;
}
