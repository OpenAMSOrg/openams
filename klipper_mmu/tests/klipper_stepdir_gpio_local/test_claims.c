// SPDX-License-Identifier: GPL-3.0-or-later
#include "stepdir_gpio.h"
#include "clock.h"
#include "shim_klipper.h"
#include "stm32/gpio_local.h"

#include <assert.h>
#include <setjmp.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum {
    PIN_UNCLAIMED = 0,
    PIN_ORDINARY = 1,
    PIN_LOCAL = 2
};

enum {
    ROLE_STEP = 0,
    ROLE_DIR = 1,
    ROLE_ENABLE = 2
};

static uint8_t pin_claim[SHIM_MAX_PINS];
static unsigned ready_calls;

struct gpio_out __real_gpio_out_setup(uint32_t pin, uint32_t val);

int gpio_pin_is_local(uint32_t pin)
{
    if (pin >= SHIM_MAX_PINS)
        return 0;
    return pin_claim[pin] == PIN_LOCAL;
}

struct gpio_out __wrap_gpio_out_setup(uint32_t pin, uint32_t val)
{
    (void)pin;
    (void)val;
    fputs("ordinary gpio_out_setup called\n", stderr);
    abort();
}

struct gpio_out gpio_out_setup_local(uint32_t pin, uint32_t val)
{
    if (!gpio_pin_is_local(pin)) {
        fputs("gpio_out_setup_local on pin that is not local\n", stderr);
        abort();
    }
    return __real_gpio_out_setup(pin, val);
}

static void *sentinel(void)
{
    return (void *)(uintptr_t)0x51u;
}

static bool counting_ready(void *context)
{
    (void)context;
    ready_calls++;
    return true;
}

static km_step_dir_config_t default_stepdir_config(void)
{
    return (km_step_dir_config_t){
        .enable_active_high = true,
        .step_active_high = true,
        .forward_high = true,
        .pulse_high_us = 10u,
        .pulse_low_us = 10u,
        .maximum_velocity_steps_s = 20000u
    };
}

static void boot(void)
{
    memset(pin_claim, 0, sizeof pin_claim);
    ready_calls = 0u;
    shim_reset(100000000u, 1u, 1u, 4u);
    shim_set_ticks(10000u);
    shim_run_init();
    shim_set_board_ready(true);
    shim_run_task();
    assert(km_clock_get_state() == KM_CLOCK_STATE_READY);
    shim_gpio_fail_pin = 0xffffffffu;
    shim_gpio_clear_events();
    test_shim_use_longjmp = false;
}

static void claim_local(uint32_t step, uint32_t dir, uint32_t enable)
{
    assert(step < SHIM_MAX_PINS && dir < SHIM_MAX_PINS && enable < SHIM_MAX_PINS);
    pin_claim[step] = PIN_LOCAL;
    pin_claim[dir] = PIN_LOCAL;
    pin_claim[enable] = PIN_LOCAL;
}

static void assert_no_adapter_slot(void)
{
    uint32_t index;
    for (index = 0u; index < KM_MAX_LANES; ++index) {
        uint32_t step = 9u, dir = 9u, enable = 9u;
        assert(km_stepdir_gpio_get_slot_pins(index, &step, &dir, &enable) == KM_ERR_UNAVAILABLE);
        assert(km_stepdir_gpio_get_driver(index) == NULL);
        assert(!km_stepdir_gpio_is_slot_active(index));
    }
}

static void assert_three_setups(uint32_t enable, uint32_t enable_val,
                                uint32_t step, uint32_t step_val,
                                uint32_t dir, uint32_t dir_val)
{
    assert(shim_gpio_events_count == 3u);
    assert(shim_gpio_events[0].is_setup && shim_gpio_events[0].pin == enable
           && shim_gpio_events[0].val == enable_val);
    assert(shim_gpio_events[1].is_setup && shim_gpio_events[1].pin == step
           && shim_gpio_events[1].val == step_val);
    assert(shim_gpio_events[2].is_setup && shim_gpio_events[2].pin == dir
           && shim_gpio_events[2].val == dir_val);
}

static void assert_pins(uint32_t slot, uint32_t step, uint32_t dir, uint32_t enable)
{
    uint32_t got_step = 0u, got_dir = 0u, got_enable = 0u;
    assert(km_stepdir_gpio_get_slot_pins(slot, &got_step, &got_dir, &got_enable) == KM_OK);
    assert(got_step == step && got_dir == dir && got_enable == enable);
}

static void reject_one(int role, uint8_t bad_claim)
{
    const uint32_t step = 1u, dir = 2u, enable = 3u;
    const uint32_t missing = role == ROLE_DIR ? dir : (role == ROLE_ENABLE ? enable : step);
    km_step_dir_config_t config;
    void *force_off;
    uint8_t saved[SHIM_MAX_PINS];
    uint32_t step_before, dir_before, enable_before;

    boot();
    claim_local(step, dir, enable);
    pin_claim[missing] = bad_claim;
    memcpy(saved, pin_claim, sizeof saved);
    config = default_stepdir_config();
    force_off = sentinel();
    assert(km_stepdir_gpio_prepare(0u, step, dir, enable, &config,
                                   counting_ready, NULL, NULL, &force_off)
           == KM_ERR_CONFLICT);
    assert(force_off == sentinel());
    assert(shim_gpio_events_count == 0u);
    assert(ready_calls == 0u);
    assert_no_adapter_slot();
    assert(memcmp(pin_claim, saved, sizeof saved) == 0);

    pin_claim[missing] = PIN_LOCAL;
    shim_gpio_clear_events();
    step_before = shim_gpio_setup_count[step];
    dir_before = shim_gpio_setup_count[dir];
    enable_before = shim_gpio_setup_count[enable];
    force_off = sentinel();
    assert(km_stepdir_gpio_prepare(0u, step, dir, enable, &config,
                                   counting_ready, NULL, NULL, &force_off)
           == KM_OK);
    assert(force_off != NULL);
    assert(ready_calls == 0u);
    assert(shim_gpio_setup_count[enable] == enable_before + 1u);
    assert(shim_gpio_setup_count[step] == step_before + 1u);
    assert(shim_gpio_setup_count[dir] == dir_before + 1u);
    /* DIR starts low whatever the polarity (Klipper's stepper.c toggles it). */
    assert_three_setups(enable, 0u, step, 0u, dir, 0u);
    assert_pins(0u, step, dir, enable);
    assert(km_stepdir_gpio_get_driver(0u) == NULL);
    assert(!km_stepdir_gpio_is_slot_active(0u));
    assert(pin_claim[step] == PIN_LOCAL);
    assert(pin_claim[dir] == PIN_LOCAL);
    assert(pin_claim[enable] == PIN_LOCAL);
}

static void prepare_polarity(uint32_t slot, uint32_t step, uint32_t dir, uint32_t enable, bool high)
{
    km_step_dir_config_t config = default_stepdir_config();
    void *force_off = sentinel();
    uint32_t enable_inactive = high ? 0u : 1u;
    uint32_t step_inactive = high ? 0u : 1u;
    uint32_t dir_level = 0u; /* DIR starts low whatever the polarity */
    unsigned ready_before = ready_calls;

    config.enable_active_high = high;
    config.step_active_high = high;
    config.forward_high = high;
    claim_local(step, dir, enable);
    shim_gpio_clear_events();
    assert(km_stepdir_gpio_prepare(slot, step, dir, enable, &config,
                                   counting_ready, NULL, NULL, &force_off)
           == KM_OK);
    assert(force_off != NULL);
    assert(ready_calls == ready_before);
    assert_three_setups(enable, enable_inactive, step, step_inactive, dir, dir_level);
    assert_pins(slot, step, dir, enable);
    assert(km_stepdir_gpio_get_driver(slot) == NULL);
    assert(!km_stepdir_gpio_is_slot_active(slot));
    assert(km_stepdir_gpio_activate(slot) == KM_OK);
    assert(km_stepdir_gpio_get_driver(slot) != NULL);
    assert(km_stepdir_gpio_is_slot_active(slot));
    km_stepdir_gpio_force_off(force_off);
    assert(shim_gpio_current_level[enable] == enable_inactive);
    assert_pins(slot, step, dir, enable);
}

static void physical_fail(int fail_dir)
{
    const uint32_t step = 20u, dir = 21u, enable = 22u;
    km_step_dir_config_t config;
    uint8_t saved[SHIM_MAX_PINS];
    volatile int saw_jump = 0;
    uint32_t events_after;
    void *force_off;

    boot();
    claim_local(step, dir, enable);
    memcpy(saved, pin_claim, sizeof saved);
    config = default_stepdir_config();
    shim_gpio_fail_pin = fail_dir ? dir : step;
    test_shim_use_longjmp = true;
    if (setjmp(test_shim_shutdown_jump) == 0) {
        void *out = sentinel();
        (void)km_stepdir_gpio_prepare(0u, step, dir, enable, &config,
                                      counting_ready, NULL, NULL, &out);
        assert(saw_jump == 0);
        abort();
    } else {
        saw_jump = 1;
    }
    assert(saw_jump == 1);
    assert(km_stepdir_gpio_is_shutdown());
    assert(ready_calls == 0u);
    assert(shim_gpio_events_count >= 1u);
    assert(shim_gpio_events[0].is_setup);
    assert(shim_gpio_events[0].pin == enable);
    assert(shim_gpio_events[0].val == 0u);
    assert(shim_gpio_is_setup[enable]);
    assert(shim_gpio_current_level[enable] == 0u);
    if (fail_dir) {
        assert(shim_gpio_is_setup[step]);
        assert(shim_gpio_current_level[step] == 0u);
        assert(!shim_gpio_is_setup[dir]);
    } else {
        assert(!shim_gpio_is_setup[step]);
        assert(!shim_gpio_is_setup[dir]);
    }
    assert(km_stepdir_gpio_get_driver(0u) == NULL);
    assert(!km_stepdir_gpio_is_slot_active(0u));
    assert_pins(0u, step, dir, enable);
    assert(memcmp(pin_claim, saved, sizeof saved) == 0);

    events_after = shim_gpio_events_count;
    test_shim_use_longjmp = false;
    force_off = sentinel();
    assert(km_stepdir_gpio_prepare(0u, step, dir, enable, &config,
                                   counting_ready, NULL, NULL, &force_off)
           == KM_ERR_FAULTED);
    assert(force_off == sentinel());
    assert(shim_gpio_events_count == events_after);
    assert(ready_calls == 0u);
    assert_pins(0u, step, dir, enable);
    assert(km_stepdir_gpio_get_driver(0u) == NULL);
    assert(memcmp(pin_claim, saved, sizeof saved) == 0);
}

static void missing_claim_distinct_slots(void)
{
    const uint32_t step0 = 1u, dir0 = 2u, enable0 = 3u;
    const uint32_t step1 = 4u, dir1 = 5u, enable1 = 6u;
    km_step_dir_config_t config;
    uint8_t saved[SHIM_MAX_PINS];
    void *force_off;

    boot();
    claim_local(step0, dir0, enable0);
    claim_local(step1, dir1, enable1);
    pin_claim[step0] = PIN_UNCLAIMED;
    pin_claim[dir1] = PIN_ORDINARY;
    memcpy(saved, pin_claim, sizeof saved);
    config = default_stepdir_config();

    force_off = sentinel();
    assert(km_stepdir_gpio_prepare(0u, step0, dir0, enable0, &config,
                                   counting_ready, NULL, NULL, &force_off)
           == KM_ERR_CONFLICT);
    assert(force_off == sentinel());
    force_off = sentinel();
    assert(km_stepdir_gpio_prepare(1u, step1, dir1, enable1, &config,
                                   counting_ready, NULL, NULL, &force_off)
           == KM_ERR_CONFLICT);
    assert(force_off == sentinel());
    assert(shim_gpio_events_count == 0u);
    assert(ready_calls == 0u);
    assert_no_adapter_slot();
    assert(memcmp(pin_claim, saved, sizeof saved) == 0);

    pin_claim[step0] = PIN_LOCAL;
    pin_claim[dir1] = PIN_LOCAL;
    force_off = sentinel();
    assert(km_stepdir_gpio_prepare(0u, step0, dir0, enable0, &config,
                                   counting_ready, NULL, NULL, &force_off)
           == KM_OK);
    assert(force_off != NULL);
    assert(ready_calls == 0u);
    assert_pins(0u, step0, dir0, enable0);
    force_off = sentinel();
    assert(km_stepdir_gpio_prepare(1u, step1, dir1, enable1, &config,
                                   counting_ready, NULL, NULL, &force_off)
           == KM_OK);
    assert(force_off != NULL);
    assert_pins(1u, step1, dir1, enable1);
    assert(km_stepdir_gpio_get_driver(0u) == NULL);
    assert(km_stepdir_gpio_get_driver(1u) == NULL);
}

static void reject_step_unclaimed(void) { reject_one(ROLE_STEP, PIN_UNCLAIMED); }
static void reject_step_ordinary(void) { reject_one(ROLE_STEP, PIN_ORDINARY); }
static void reject_dir_unclaimed(void) { reject_one(ROLE_DIR, PIN_UNCLAIMED); }
static void reject_dir_ordinary(void) { reject_one(ROLE_DIR, PIN_ORDINARY); }
static void reject_enable_unclaimed(void) { reject_one(ROLE_ENABLE, PIN_UNCLAIMED); }
static void reject_enable_ordinary(void) { reject_one(ROLE_ENABLE, PIN_ORDINARY); }

static void local_setup_polarities(void)
{
    boot();
    prepare_polarity(0u, 1u, 2u, 3u, true);
    prepare_polarity(1u, 4u, 5u, 6u, false);
}

static void physical_fail_step(void) { physical_fail(0); }
static void physical_fail_dir(void) { physical_fail(1); }

int main(int argc, char **argv)
{
    static const struct {
        const char *name;
        void (*fn)(void);
    } cases[] = {
        {"reject_step_unclaimed", reject_step_unclaimed},
        {"reject_step_ordinary", reject_step_ordinary},
        {"reject_dir_unclaimed", reject_dir_unclaimed},
        {"reject_dir_ordinary", reject_dir_ordinary},
        {"reject_enable_unclaimed", reject_enable_unclaimed},
        {"reject_enable_ordinary", reject_enable_ordinary},
        {"local_setup_polarities", local_setup_polarities},
        {"physical_fail_step", physical_fail_step},
        {"physical_fail_dir", physical_fail_dir},
        {"missing_claim_distinct_slots", missing_claim_distinct_slots},
    };
    size_t index;

    if (argc != 2) {
        fputs("usage: test_claims CASE\n", stderr);
        return 2;
    }
    for (index = 0u; index < sizeof cases / sizeof cases[0]; ++index) {
        if (strcmp(argv[1], cases[index].name) == 0) {
            cases[index].fn();
            printf("%s passed\n", cases[index].name);
            return 0;
        }
    }
    fprintf(stderr, "unknown case %s\n", argv[1]);
    return 2;
}
