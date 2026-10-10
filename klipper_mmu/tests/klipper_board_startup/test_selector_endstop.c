// SPDX-License-Identifier: GPL-3.0-or-later
/* Selector home endstop (klipper_mmu/selector.h): proves mcu/switch_gpio.c's
 * real sampling/debounce path publishes the endstop to the real core
 * (km_core_set_selector_endstop_at) with the right level, validity and
 * timestamp, and counts its own bounce. */
#include "switch_gpio.h"
#include "controller.h"
#include "clock.h"
#include "../fake_clock.h"
#include "../mock_driver.h"
#include "shim_klipper.h"
#include "board/irq.h"
#include "board/misc.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <sys/wait.h>

static km_operation_core_t core;
static km_switch_gpio_config_t config;
static bool levels[128];
static uint32_t read_cost;


struct gpio_in gpio_in_setup(uint32_t pin, int32_t pull)
{
    assert(!test_shim_irq_is_disabled());
    assert(pin < 128 && pull >= -1 && pull <= 1);
    return (struct gpio_in){pin ^ 0x5a5a};
}
uint8_t gpio_in_read(struct gpio_in g)
{
    uint32_t pin = g.encoded ^ 0x5a5a;
    assert(pin < 128 && !test_shim_irq_is_disabled());
    bool level = levels[pin];
    shim_advance_ticks(read_cost);
    return level ? 1u : 0u;
}

/* One lane, a selector, a real (non-virtual) hub and toolhead switch, and a
 * switched buffer: nightowl's own shape, plus a selector, so this exercises
 * every other switch role alongside the new selector_endstop one. */
static void fixture(void)
{
    memset(levels, 0, sizeof(levels));
    memset(&config, 0, sizeof(config));
    shim_reset(520000000u, 1, 1, 1);
    shim_run_init();
    shim_set_board_ready(true);
    shim_run_task();

    km_device_profile_t profile;
    km_profile_init_nightowl(&profile);
    profile.lane_count = 1u;
    profile.has_selector = true;
    profile.selector = (km_selector_profile_t){
        .kind = KM_SELECTOR_LINEAR,
        .home = {.speed_steps_s = 4800u, .acceleration_steps_s2 = 96000u,
                 .deceleration_steps_s2 = 96000u, .max_travel_steps = 15000u,
                 .deadline_us = 10000000u, .max_switch_age_us = 20000u,
                 .home_direction = KM_DIR_REVERSE},
        .gate_steps = {320},
        .velocity_steps_s = 16000u, .acceleration_steps_s2 = 96000u,
    };
    assert(km_core_init(&core, &profile) == KM_OK);

    config.bay[0] = (km_switch_input_t){0, 1, true};
    config.hub = (km_switch_input_t){1, 1, true};
    config.toolhead = (km_switch_input_t){5, -1, false};
    config.buffer_low = (km_switch_input_t){2, 0, true};
    config.buffer_high = (km_switch_input_t){3, 1, false};
    config.selector_endstop = (km_switch_input_t){4, 1, true};
    levels[3] = true; /* Inactive active-low buffer_high input. */
    levels[5] = true; /* Inactive active-low toolhead input. */
}

static void sample_step(void)
{
    shim_advance_ticks(timer_from_us(1000));
    km_switch_gpio_sample(&core, NULL);
}
static void sample_stable(void)
{
    for (unsigned i = 0; i < 8; ++i) sample_step(); /* > 5ms debounce window. */
}

static void endstop_publishes(void)
{
    fixture();
    assert(km_switch_gpio_setup(&core, &config) == KM_OK);
    assert(!core.selector_endstop_valid); /* Setup samples nothing by itself. */

    levels[4] = false; /* Active-high endstop: currently open. */
    sample_stable();
    assert(!core.selector_endstop_asserted && core.selector_endstop_valid);
    uint16_t bounce_before;
    km_switch_gpio_get_selector_bounce(&bounce_before);
    assert(bounce_before == 0);

    levels[4] = true; /* The carriage reaches the endstop. */
    sample_stable();
    assert(core.selector_endstop_asserted && core.selector_endstop_valid);
    km_core_timepoint_t now;
    assert(km_clock_get_time(&now) == KM_OK);
    assert(core.selector_endstop_sample_us && core.selector_endstop_sample_us <= now.us);

    /* A rejected, sub-debounce excursion counts as bounce but never
     * publishes a level change (docs/diagnostics.md). */
    levels[4] = false;
    sample_step();
    levels[4] = true;
    sample_step();
    assert(core.selector_endstop_asserted); /* Still the qualified level. */
    uint16_t bounce_after;
    km_switch_gpio_get_selector_bounce(&bounce_after);
    assert(bounce_after > bounce_before);
}

/* mcu/switch_gpio.h: the home endstop qualifies after its own 1 ms
 * confirmation (two consecutive 1 ms samples), while an ordinary presence
 * switch (the hub, here) still needs the full 5 ms KM_SWITCH_DEBOUNCE_US. */
static void endstop_faster_than_presence(void)
{
    fixture();
    assert(km_switch_gpio_setup(&core, &config) == KM_OK);

    levels[4] = false; /* Endstop open. */
    levels[1] = false; /* Hub clear. */
    sample_stable();
    assert(!core.selector_endstop_asserted && !core.filament_present_hub);

    levels[4] = true;
    levels[1] = true;
    sample_step(); /* First sample of the new level on both inputs. */
    sample_step(); /* A second, 1 ms later: the endstop's own debounce. */
    assert(core.selector_endstop_asserted); /* Qualified in about 1 ms. */
    assert(!core.filament_present_hub); /* The hub needs 5 ms; not yet. */

    sample_step(); sample_step(); sample_step(); /* 4 ms since the change. */
    assert(!core.filament_present_hub);
    sample_step(); /* 5 ms since the change: the hub qualifies too. */
    assert(core.filament_present_hub);
}

static void no_selector_never_calls_it(void)
{
    fixture();
    core.profile.has_selector = false; /* Setup below reads has_selector once. */
    assert(km_switch_gpio_setup(&core, &config) == KM_OK);
    levels[4] = true; /* Would assert the endstop if it were sampled. */
    sample_stable();
    assert(!core.selector_endstop_valid && !core.selector_endstop_sample_us);
    uint16_t bounce;
    km_switch_gpio_get_selector_bounce(&bounce);
    assert(bounce == 0);
}

/* A SERVO-kind selector (MMX) has no selector endstop: setup must accept
 * an unused endstop pin instead of refusing the whole switch set. */
static void servo_selector_has_no_endstop(void)
{
    fixture();
    core.profile.selector = (km_selector_profile_t){
        .kind = KM_SELECTOR_SERVO, .has_servo = true,
        .servo_gate_us = {1500}, .servo_dwell_ms = 250u,
    };
    assert(km_profile_validate(&core.profile) == KM_OK);
    config.selector_endstop.pin = KM_SWITCH_PIN_UNUSED;
    assert(km_switch_gpio_setup(&core, &config) == KM_OK);
    sample_stable();
    assert(!core.selector_endstop_valid && !core.selector_endstop_sample_us);
}

static void run(unsigned which)
{
    switch (which) {
    case 0: endstop_publishes(); break;
    case 1: no_selector_never_calls_it(); break;
    case 2: servo_selector_has_no_endstop(); break;
    default: endstop_faster_than_presence(); break;
    }
}

int main(void)
{
    for (unsigned i = 0; i < 4; ++i) {
        pid_t child = fork(); assert(child >= 0);
        if (!child) { run(i); _exit(0); }
        int status; assert(waitpid(child, &status, 0) == child);
        if (!WIFEXITED(status) || WEXITSTATUS(status)) {
            fprintf(stderr, "selector endstop scenario %u failed\n", i);
            return 1;
        }
    }
    puts("4 selector endstop scenarios passed");
    return 0;
}
