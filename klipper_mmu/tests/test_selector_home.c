// SPDX-License-Identifier: GPL-3.0-or-later
#include "klipper_mmu/selector_home.h"
#include "mock_driver.h"
#include "fake_clock.h"
#include <assert.h>
#include <stdio.h>

static km_selector_home_config_t cfg(void)
{
    return (km_selector_home_config_t){.speed_steps_s = 1000,
                                       .acceleration_steps_s2 = 10000,
                                       .deceleration_steps_s2 = 10000,
                                       .max_travel_steps = 4,
                                       .deadline_us = 200000,
                                       .max_switch_age_us = 1000,
                                       .home_direction = KM_DIR_REVERSE};
}
static km_selector_home_switch_t sample(bool asserted, uint32_t time)
{
    return (km_selector_home_switch_t){asserted, time, true};
}
static void setup(mock_driver_t *d, km_trajectory_t *t, km_fake_clock_t *clock)
{
    mock_driver_init(d, 2, 10, false);
    assert(km_trajectory_init(t, 1000, 10000, 10000) == KM_OK);
    km_fake_clock_init(clock, 100);
}
static void failed(const km_selector_home_t *h, const mock_driver_t *d, const km_trajectory_t *t)
{
    assert(h->state == KM_SELECTOR_HOME_FAILED);
    assert(!h->position_known);
    assert(!d->base.enabled);
    assert(km_trajectory_is_position_uncertain(t));
}
/* Runs the trajectory through the tick API (window/commit/observe) via the
 * shared fake clock, the same loop shape the MCU adapter uses; km_selector_home
 * only reads trajectory state, it does not step the driver itself anymore
 * (that per-edge servicing is gone, replaced by the tick loop + reference
 * executor). "Did it step" reads the reference's position, not the driver's
 * own (now dead) edge counters. */
static uint32_t first_pulse(km_selector_home_t *h, km_trajectory_t *t, km_fake_clock_t *clock)
{
    uint32_t tick = clock->now_us;
    for (unsigned i = 0; i < 100000 && km_ref_position(&clock->ref) == 0; ++i)
    {
        tick += 1000;
        assert(km_fake_clock_run_trajectory(clock, t, tick) == KM_OK);
        assert(km_selector_home_service(h, sample(false, tick), tick) == KM_OK);
    }
    /* home_direction is REVERSE: completed steps count down. */
    assert(km_ref_position(&clock->ref) != 0);
    return tick;
}
int main(void)
{
    mock_driver_t d;
    km_trajectory_t t;
    km_fake_clock_t clock;
    km_selector_home_t h = {0};
    km_selector_home_config_t c = cfg();
    setup(&d, &t, &clock);
    assert(km_selector_home_start(&h, &t, &d.base, &c, sample(true, 100), 100) ==
           KM_ERR_FAULTED);
    failed(&h, &d, &t);
    setup(&d, &t, &clock);
    h = (km_selector_home_t){0};
    assert(km_selector_home_start(&h, &t, &d.base, &c, sample(false, 100), 100) == KM_OK);
    assert(d.base.enabled);
    assert(km_selector_home_service(&h, sample(true, 101), 101) == KM_ERR_FAULTED);
    failed(&h, &d, &t);
    setup(&d, &t, &clock);
    h = (km_selector_home_t){0};
    assert(km_selector_home_start(&h, &t, &d.base, &c, sample(false, 100), 100) == KM_OK);
    uint32_t home_tick = first_pulse(&h, &t, &clock);
    assert(km_selector_home_service(&h, sample(true, home_tick + 1), home_tick + 1) == KM_OK);
    assert(h.state == KM_SELECTOR_HOME_HOMED && h.position_known && !d.base.enabled &&
           km_trajectory_get_position(&t) == 0);
    setup(&d, &t, &clock);
    h = (km_selector_home_t){0};
    assert(km_selector_home_start(&h, &t, &d.base, &c, sample(false, 100), 100) == KM_OK);
    home_tick = first_pulse(&h, &t, &clock);
    assert(km_selector_home_service(&h, sample(true, home_tick), home_tick + 1) == KM_ERR_STALE);
    failed(&h, &d, &t);
    setup(&d, &t, &clock);
    h = (km_selector_home_t){0};
    assert(km_selector_home_start(&h, &t, &d.base, &c, sample(false, 100), 100) == KM_OK);
    assert(km_selector_home_service(&h, sample(false, 100), 1102) == KM_ERR_STALE);
    failed(&h, &d, &t);
    assert(km_selector_home_start(&h, &t, &d.base, &c, sample(true, 1103), 1103) ==
           KM_ERR_FAULTED);
    failed(&h, &d, &t);
    setup(&d, &t, &clock);
    h = (km_selector_home_t){0};
    assert(km_selector_home_start(&h, &t, &d.base, &c, sample(false, 100), 100) == KM_OK);
    assert(km_selector_home_service(&h, sample(false, 200100), 200100) == KM_ERR_TIMEOUT);
    failed(&h, &d, &t);
    setup(&d, &t, &clock);
    h = (km_selector_home_t){0};
    assert(km_selector_home_start(&h, &t, &d.base, &c, sample(false, 100), 100) == KM_OK);
    assert(km_selector_home_cancel(&h) == KM_ERR_CONFLICT);
    failed(&h, &d, &t);
    setup(&d, &t, &clock);
    h = (km_selector_home_t){0};
    assert(km_selector_home_start(&h, &t, &d.base, &c, sample(false, 100), 100) == KM_OK);
    d.injected_fault = KM_FAULT_DRIVER_FAULT;
    assert(km_selector_home_service(&h, sample(false, 101), 101) == KM_ERR_FAULTED);
    failed(&h, &d, &t);
    d.injected_fault = KM_FAULT_NONE;
    setup(&d, &t, &clock);
    h = (km_selector_home_t){0};
    assert(km_selector_home_start(&h, &t, &d.base, &c, sample(true, 0), 1002) == KM_ERR_STALE);
    failed(&h, &d, &t);
    setup(&d, &t, &clock);
    h = (km_selector_home_t){0};
    c = cfg();
    c.max_travel_steps = 1;
    c.deadline_us = 1000000;
    assert(km_selector_home_start(&h, &t, &d.base, &c, sample(false, 100), 100) == KM_OK);
    uint32_t tick = 100;
    for (unsigned i = 0; i < 100000 && h.state == KM_SELECTOR_HOME_RUNNING; ++i)
    {
        tick += 1000;
        assert(km_fake_clock_run_trajectory(&clock, &t, tick) == KM_OK);
        assert(km_selector_home_service(&h, sample(false, tick), tick) == KM_OK || h.state == KM_SELECTOR_HOME_FAILED);
    }
    failed(&h, &d, &t);
    assert(km_ref_position(&clock.ref) == -1); /* home_direction REVERSE */

    /* Regression: a forward-only selector's driver step count never resets
     * between selections (mcu/stepdir_gpio.c's s->position, an ever-growing
     * raw accumulator), so a caller re-baselines the shared trajectory to
     * its own running absolute position with km_trajectory_recover before
     * every search (operation_core.c's enter_select_home_ex). That baseline
     * must survive km_selector_home_start: max_travel_steps bounds only
     * this search's own travel from where the caller left it, never the
     * whole session's accumulated total. */
    setup(&d, &t, &clock);
    h = (km_selector_home_t){0};
    clock.ref.position = 100000; /* stands in for many prior forward-only searches */
    assert(km_trajectory_recover(&t, 100000, 100) == KM_OK);
    c = cfg();
    c.max_travel_steps = 10;
    c.deadline_us = 1000000;
    c.home_direction = KM_DIR_FORWARD;
    assert(km_selector_home_start(&h, &t, &d.base, &c, sample(false, 100), 100) == KM_OK);
    assert(km_fake_clock_run_trajectory(&clock, &t, 101) == KM_OK);
    assert(km_selector_home_service(&h, sample(false, 101), 101) == KM_OK);
    assert(h.state == KM_SELECTOR_HOME_RUNNING);
    puts("selector home tests passed");
    return 0;
}
