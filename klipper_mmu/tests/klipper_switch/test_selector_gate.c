// SPDX-License-Identifier: GPL-3.0-or-later
/* INDEXED selector's per-gate index switches (klipper_mmu/selector.h; BTT
 * ViViD, Claymore): proves mcu/switch_gpio.c's real setup/sampling/debounce
 * path claims and publishes KLIPPER_MMU_PINS_SELECTOR_GATE to the real core
 * (km_core_set_selector_gate_switch_at) one gate at a time, the same way
 * test_switch.c covers switch_gpio.c's other roles and
 * tests/klipper_board_startup/test_selector_endstop.c covers the LINEAR/
 * ROTARY home endstop. This binary is built WITH
 * CONFIG_KLIPPER_MMU_SELECTOR_KIND_INDEXED=1 (see this directory's Makefile,
 * test-gate target); plain test_switch.c stays built without it and proves
 * the opposite: an image with no room for gate switches refuses to claim
 * them even when a profile asks for the INDEXED kind. */
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
static unsigned setups;
static int32_t pulls[128];
static bool levels[128];
static uint32_t read_cost;

struct gpio_in gpio_in_setup(uint32_t pin, int32_t pull)
{
    assert(!test_shim_irq_is_disabled());
    assert(pin < 128 && pull >= -1 && pull <= 1);
    ++setups;
    pulls[pin] = pull;
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

/* Three gates (BTT ViViD's own shortest-hop shape: a real gate_order), no
 * BAY/LOAD switches so the fixture stays focused on the gate inputs
 * alongside the other roles every INDEXED image still carries (HUB,
 * toolhead, a switched buffer). */
static void fixture(void)
{
    memset(levels, 0, sizeof(levels));
    memset(&config, 0, sizeof(config));
    shim_reset(520000000u, 1, 1, 3);
    shim_run_init();
    shim_set_board_ready(true);
    shim_run_task();

    km_device_profile_t profile;
    km_profile_init_boxturtle(&profile);
    profile.lane_count = 3;
    profile.has_bay_sensors = false;
    profile.has_selector = true;
    profile.selector = (km_selector_profile_t){
        .kind = KM_SELECTOR_INDEXED,
        .home = {.speed_steps_s = 4800u, .acceleration_steps_s2 = 96000u,
                 .deceleration_steps_s2 = 96000u, .max_travel_steps = 90000u,
                 .deadline_us = 10000000u, .max_switch_age_us = 20000u,
                 .home_direction = KM_DIR_REVERSE},
        .velocity_steps_s = 16000u, .acceleration_steps_s2 = 96000u,
        .gate_order = {0, 2, 1}, .center_nudge_steps = 200u,
    };
    assert(km_core_init(&core, &profile) == KM_OK);

    config.hub = (km_switch_input_t){10, 1, true};
    config.toolhead = (km_switch_input_t){11, -1, false};
    config.buffer_low = (km_switch_input_t){12, 0, true};
    config.buffer_high = (km_switch_input_t){13, 1, false};
    config.selector_gate[0] = (km_switch_input_t){20, 1, true};   /* Active-high. */
    config.selector_gate[1] = (km_switch_input_t){21, -1, false}; /* Active-low. */
    config.selector_gate[2] = (km_switch_input_t){22, 0, true};   /* Active-high, no pull. */
    levels[11] = true; /* Inactive active-low toolhead. */
    levels[13] = true; /* Inactive active-low buffer_high. */
    levels[21] = true; /* Inactive active-low gate 1. */
}
static void sample_step(void)
{
    shim_advance_ticks(timer_from_us(1000));
    km_switch_gpio_sample(&core, NULL);
}
static void sample_stable(void)
{ for (unsigned i = 0; i < 8; ++i) sample_step(); /* > 5 ms debounce window. */ }

/* Config: a missing (unassigned) gate pin and a gate pin duplicated across
 * gates are both refused, with the core and binding untouched; a complete,
 * distinct set of per-gate pins/levels/pulls is then claimed, and each
 * gate's own pull reaches gpio_in_setup exactly as configured for it. */
static void gate_config(void)
{
    fixture();
    config.selector_gate[2].pin = KM_SWITCH_PIN_UNUSED;
    assert(km_switch_gpio_setup(&core, &config) == KM_ERR_INVALID_PARAM && setups == 0);
    config.selector_gate[2].pin = 22;
    config.selector_gate[1].pin = config.selector_gate[0].pin; /* Gate 1 duplicates gate 0. */
    assert(km_switch_gpio_setup(&core, &config) == KM_ERR_CONFLICT && setups == 0);
    config.selector_gate[1].pin = 21;
    assert(km_switch_gpio_setup(&core, &config) == KM_OK && setups == 3 + 1 + 1 + 2);
    for (unsigned i = 0; i < 3; ++i)
        assert(pulls[config.selector_gate[i].pin] == config.selector_gate[i].pull);
}

/* Sampling: each gate's own level, in its own polarity, reaches the core as
 * that gate's reading, and never as another gate's -- toggling one gate's
 * physical level leaves every other gate's published reading unchanged. */
static void gate_sampling_and_isolation(void)
{
    fixture();
    assert(km_switch_gpio_setup(&core, &config) == KM_OK);
    for (unsigned i = 0; i < 3; ++i) assert(!core.selector_gate_valid[i]);

    levels[20] = true;  /* Gate 0 (active-high): asserted. */
    levels[21] = false; /* Gate 1 (active-low): asserted. */
    levels[22] = false; /* Gate 2 (active-high): not asserted. */
    sample_stable();
    assert(core.selector_gate_valid[0] && core.selector_gate_asserted[0]);
    assert(core.selector_gate_valid[1] && core.selector_gate_asserted[1]);
    assert(core.selector_gate_valid[2] && !core.selector_gate_asserted[2]);
    km_core_timepoint_t now;
    assert(km_clock_get_time(&now) == KM_OK);
    assert(core.selector_gate_sample_us[0] && core.selector_gate_sample_us[0] <= now.us);

    levels[20] = false; /* Only gate 0 changes. */
    sample_stable();
    assert(!core.selector_gate_asserted[0]);
    assert(core.selector_gate_asserted[1] && !core.selector_gate_asserted[2]);

    levels[22] = true; /* Only gate 2 changes. */
    sample_stable();
    assert(core.selector_gate_asserted[2]);
    assert(!core.selector_gate_asserted[0] && core.selector_gate_asserted[1]);
}

/* Debounce behaves like every other switch role, and its own diagnostic
 * bounce counter is per-gate: a brief, rejected excursion on one gate never
 * shows up as bounce on another. */
static void gate_debounce_and_bounce(void)
{
    fixture();
    assert(km_switch_gpio_setup(&core, &config) == KM_OK);
    uint16_t bounce[KM_MAX_LANES];
    km_switch_gpio_get_selector_gate_bounce(bounce);
    assert(bounce[0] == 0 && bounce[1] == 0 && bounce[2] == 0);

    sample_step();      /* Gate 0 first observation: a reset, not a bounce. */
    levels[20] = true;  sample_step(); /* Brief active pulse. */
    levels[20] = false; sample_step();
    sample_stable();
    assert(core.selector_gate_valid[0] && !core.selector_gate_asserted[0]);
    km_switch_gpio_get_selector_gate_bounce(bounce);
    assert(bounce[0] == 2 && bounce[1] == 0 && bounce[2] == 0);

    levels[21] = false; /* Gate 1 becomes active, held long enough to qualify cleanly. */
    for (unsigned i = 0; i < 6; ++i) sample_step();
    assert(core.selector_gate_valid[1] && core.selector_gate_asserted[1]);
    km_switch_gpio_get_selector_gate_bounce(bounce);
    assert(bounce[1] == 0 && bounce[0] == 2 && bounce[2] == 0);
}

/* A stale sampling gap invalidates every gate's evidence, like every other
 * switch role, and fresh stable samples recover it afterward. */
static void gate_stale_sample(void)
{
    fixture();
    assert(km_switch_gpio_setup(&core, &config) == KM_OK);
    levels[20] = true; /* Gate 0 asserted. */
    sample_stable();
    assert(core.selector_gate_valid[0] && core.selector_gate_asserted[0]);
    assert(core.selector_gate_valid[1] && core.selector_gate_valid[2]);

    shim_advance_ticks(timer_from_us(KM_SWITCH_MAX_SAMPLE_GAP_US + 1000u));
    km_switch_gpio_sample(&core, NULL);
    assert(!core.selector_gate_valid[0] && !core.selector_gate_valid[1]
           && !core.selector_gate_valid[2]);

    sample_stable();
    assert(core.selector_gate_valid[0] && core.selector_gate_asserted[0]
           && core.selector_gate_valid[1] && core.selector_gate_valid[2]);
}

static void run(unsigned which)
{
    switch (which) {
    case 0: gate_config(); break;
    case 1: gate_sampling_and_isolation(); break;
    case 2: gate_debounce_and_bounce(); break;
    default: gate_stale_sample(); break;
    }
}
int main(void)
{
    for (unsigned i = 0; i < 4; ++i) {
        pid_t child = fork(); assert(child >= 0);
        if (!child) { run(i); _exit(0); }
        int status; assert(waitpid(child, &status, 0) == child);
        if (!WIFEXITED(status) || WEXITSTATUS(status)) {
            fprintf(stderr, "selector gate scenario %u failed\n", i);
            return 1;
        }
    }
    puts("4 selector gate scenarios passed");
    return 0;
}
