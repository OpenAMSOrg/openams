// SPDX-License-Identifier: GPL-3.0-or-later
#include "stepdir_gpio.h"
#include "clock.h"
#include "shim_klipper.h"

#include "klipper_mmu/trajectory.h"

#include "board/irq.h"
#include "board/misc.h"
#include "board/gpio.h"
#include "stepper.h"
#include "klipper_mmu/step_segments.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <assert.h>
#include <unistd.h>
#include <sys/wait.h>
#include <setjmp.h>

/* The image-specific scenario reads the exact BoxTurtle H723 build's pins and
 * polarities when IMAGE_AUTOCONF is supplied to the test Makefile. */
#ifdef KM_H723_IMAGE_AUTOCONF
#include KM_H723_IMAGE_AUTOCONF
#endif

static void emergency_stop(const char *reason)
{
    test_shim_use_longjmp = true;
    if (setjmp(test_shim_shutdown_jump) == 0)
        shutdown(reason);
    test_shim_use_longjmp = false;
}

/* Match the production controller's enclosing critical section. */
static km_result_t locked_enable(km_driver_t *d, bool enabled)
{
    irqstatus_t flag = irq_save();
    km_result_t result = km_driver_set_enable(d, enabled);
    irq_restore(flag);
    return result;
}

static km_result_t locked_clear_fault(km_driver_t *d)
{
    irqstatus_t flag = irq_save();
    km_result_t result = km_driver_clear_fault(d);
    irq_restore(flag);
    return result;
}

/* Scripted 32-bit position for the linked-in local stepper: inert until a
 * test opts in, so setup and every existing test see the real function. */
static bool fake_position_enabled;
static int32_t fake_position_value;
int32_t __real_stepper_local_position(struct stepper *s);
int32_t __wrap_stepper_local_position(struct stepper *s)
{
    if (fake_position_enabled)
        return fake_position_value;
    return __real_stepper_local_position(s);
}

/* Dispatch timers until the local stepper has run its queue (the clock's own
 * periodic timer never runs out, so the stepper's activity bounds the loop). */
static void drain_stepper(struct stepper *s)
{
    long guard = 200000;
    while (stepper_local_active(s) && --guard > 0)
        shim_dispatch_earliest_timer();
    assert(guard > 0);
}

/* Rising (asserted) edges recorded on a pin since the last event clear. */
static uint32_t rising_edges(uint32_t pin)
{
    uint32_t n = 0u;
    for (uint32_t e = 0u; e < shim_gpio_events_count; ++e)
        if (shim_gpio_events[e].pin == pin && shim_gpio_events[e].val == 1u
            && !shim_gpio_events[e].is_setup)
            n++;
    return n;
}

/* Dispatch timers until the stepper has emitted `want` rising STEP edges on
 * `pin` (or gone idle), so a test can freeze it mid-move. */
static void dispatch_rising_edges(struct stepper *s, uint32_t pin, uint32_t want)
{
    long guard = 200000;
    while (stepper_local_active(s) && rising_edges(pin) < want && --guard > 0)
        shim_dispatch_earliest_timer();
    assert(guard > 0);
}

/* Run the scheduler for a while with nothing queued: no stepper may move. */
static void dispatch_idle_while(void)
{
    for (unsigned i = 0u; i < 64u; ++i) {
        shim_advance_ticks(timer_from_us(500u));
        shim_dispatch_earliest_timer();
    }
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

static bool dummy_ready_true(void *ctx)
{
    (void)ctx;
    return true;
}

static bool dummy_ready_false(void *ctx)
{
    (void)ctx;
    return false;
}

static bool dynamic_ready = true;
static bool dynamic_ready_fn(void *ctx)
{
    (void)ctx;
    return dynamic_ready;
}

#ifdef KM_H723_IMAGE_AUTOCONF
static uint32_t saved_pin_at(const char *list, unsigned lane)
{
    const char *s = list;
    for (unsigned i = 0u; i < lane; ++i) {
        while (*s && *s != ',') ++s;
        assert(*s == ',');
        ++s;
    }
    assert(s[0] == 'P' && s[1] >= 'A' && s[1] <= 'I');
    uint32_t port = (uint32_t)(s[1] - 'A');
    s += 2;
    assert(*s >= '0' && *s <= '9');
    uint32_t bit = (uint32_t)(*s++ - '0');
    if (*s >= '0' && *s <= '9') {
        assert(bit == 1u && *s <= '5');
        bit = 10u + (uint32_t)(*s++ - '0');
    }
    assert(*s == '\0' || *s == ',');
    return port * 16u + bit;
}

static bool saved_level_at(const char *list, unsigned lane)
{
    const char *s = list;
    for (unsigned i = 0u; i < lane; ++i) {
        while (*s && *s != ',') ++s;
        assert(*s == ',');
        ++s;
    }
    assert((*s == '0' || *s == '1') && (s[1] == '\0' || s[1] == ','));
    return *s == '1';
}
#endif

static km_fault_t dynamic_fault = KM_FAULT_NONE;
static km_fault_t dynamic_fault_fn(void *ctx)
{
    (void)ctx;
    return dynamic_fault;
}

static void *activation_safe_context;
static unsigned activation_ready_calls;
static bool ready_with_raw_off_on_publish(void *ctx)
{
    (void)ctx;
    /* The local-stepper activate path queries readiness twice: the gate and
     * the final query under IRQ that precedes publication. Trigger the
     * safe-off on that final query. */
    if (++activation_ready_calls == 2u)
        km_stepdir_gpio_force_off(activation_safe_context);
    return true;
}

static void init_and_ready_clock(uint32_t freq_hz)
{
    shim_reset(freq_hz, 1u, 1u, 4u);
    shim_set_ticks(10000u);
    shim_run_init();
    shim_set_board_ready(true);
    shim_run_task();
    assert(km_clock_get_state() == KM_CLOCK_STATE_READY);
}

/* Prepare + activate one slot with the default config and hand back its local
 * stepper (and optionally the published driver). */
static struct stepper *active_local_stepper(uint32_t slot, uint32_t step_pin,
                                            uint32_t dir_pin, uint32_t enable_pin,
                                            km_driver_t **out_driver)
{
    km_step_dir_config_t cfg = default_stepdir_config();
    void *safe_context = NULL;
    assert(km_stepdir_gpio_prepare(slot, step_pin, dir_pin, enable_pin, &cfg,
                                    dummy_ready_true, NULL, NULL,
                                    &safe_context) == KM_OK);
    assert(km_stepdir_gpio_activate(slot) == KM_OK);
    km_driver_t *d = km_stepdir_gpio_get_driver(slot);
    struct stepper *s = km_stepdir_gpio_get_stepper_for_driver(d);
    assert(d && s);
    if (out_driver)
        *out_driver = d;
    return s;
}

/* 1. No boot GPIO access */
static void test_no_boot_gpio(void)
{
    shim_reset(520000000u, 1u, 1u, 4u);
    shim_run_init();
    assert(shim_gpio_events_count == 0u);
    for (uint32_t i = 0u; i < SHIM_MAX_PINS; ++i) {
        assert(shim_gpio_setup_count[i] == 0u);
        assert(shim_gpio_write_count[i] == 0u);
    }
}

/* 2. Pre-ready clock rejection (boundary: clock must be READY) */
static void test_pre_ready_clock_rejection_boundary(void)
{
    shim_reset(520000000u, 1u, 1u, 4u);
    shim_run_init();
    assert(km_clock_get_state() == KM_CLOCK_STATE_UNAVAILABLE);

    km_step_dir_config_t cfg = default_stepdir_config();
    km_result_t res = km_stepdir_gpio_setup(0u, 10u, 11u, 12u, &cfg, dummy_ready_true, NULL, NULL);
    assert(res == KM_ERR_UNAVAILABLE);
    assert(shim_gpio_events_count == 0u);
    assert(km_stepdir_gpio_get_driver(0u) == NULL);
}

/* 3. IRQ disabled rejection (boundary: IRQs must be enabled) */
static void test_irq_disabled_rejection_boundary(void)
{
    init_and_ready_clock(520000000u);

    irq_disable();
    assert(test_shim_irq_is_disabled());

    km_step_dir_config_t cfg = default_stepdir_config();
    km_result_t res = km_stepdir_gpio_setup(0u, 10u, 11u, 12u, &cfg, dummy_ready_true, NULL, NULL);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(shim_gpio_events_count == 0u);
    assert(km_stepdir_gpio_get_driver(0u) == NULL);

    irq_enable();
}

/* 4. Invalid config rejection (boundary parameters) */
static void test_invalid_config_rejection_boundary(void)
{
    init_and_ready_clock(520000000u);

    km_step_dir_config_t cfg = default_stepdir_config();

    /* NULL config */
    km_result_t res = km_stepdir_gpio_setup(0u, 10u, 11u, 12u, NULL, dummy_ready_true, NULL, NULL);
    assert(res == KM_ERR_INVALID_PARAM);

    /* NULL ready callback */
    res = km_stepdir_gpio_setup(0u, 10u, 11u, 12u, &cfg, NULL, NULL, NULL);
    assert(res == KM_ERR_INVALID_PARAM);

    /* Slot index boundary: slot == KM_MAX_LANES (8) */
    res = km_stepdir_gpio_setup(KM_MAX_LANES, 10u, 11u, 12u, &cfg, dummy_ready_true, NULL, NULL);
    assert(res == KM_ERR_INVALID_PARAM);

    /* pulse_high_us == 0 */
    cfg = default_stepdir_config();
    cfg.pulse_high_us = 0u;
    res = km_stepdir_gpio_setup(0u, 10u, 11u, 12u, &cfg, dummy_ready_true, NULL, NULL);
    assert(res == KM_ERR_INVALID_PARAM);

    /* pulse_low_us == 0 */
    cfg = default_stepdir_config();
    cfg.pulse_low_us = 0u;
    res = km_stepdir_gpio_setup(0u, 10u, 11u, 12u, &cfg, dummy_ready_true, NULL, NULL);
    assert(res == KM_ERR_INVALID_PARAM);

    /* maximum_velocity_steps_s == 0 */
    cfg = default_stepdir_config();
    cfg.maximum_velocity_steps_s = 0u;
    res = km_stepdir_gpio_setup(0u, 10u, 11u, 12u, &cfg, dummy_ready_true, NULL, NULL);
    assert(res == KM_ERR_INVALID_PARAM);

    /* maximum_velocity_steps_s > KM_MOTION_MAX_VELOCITY_STEPS_S (boundary 200000) */
    cfg = default_stepdir_config();
    cfg.maximum_velocity_steps_s = KM_MOTION_MAX_VELOCITY_STEPS_S + 1u;
    res = km_stepdir_gpio_setup(0u, 10u, 11u, 12u, &cfg, dummy_ready_true, NULL, NULL);
    assert(res == KM_ERR_INVALID_PARAM);

    /* cycle = 10 + 10 = 20, max_vel = 50001 > 1000000 / 20 (boundary) */
    cfg = default_stepdir_config();
    cfg.pulse_high_us = 10u;
    cfg.pulse_low_us = 10u;
    cfg.maximum_velocity_steps_s = 50001u;
    res = km_stepdir_gpio_setup(0u, 10u, 11u, 12u, &cfg, dummy_ready_true, NULL, NULL);
    assert(res == KM_ERR_INVALID_PARAM);

    assert(shim_gpio_events_count == 0u);
}

/* 5. Inactive levels, setup order, and all 8 polarity combinations */
static void test_inactive_levels_and_polarities(void)
{
    init_and_ready_clock(520000000u);
    for (int en_pol = 0; en_pol <= 1; ++en_pol) {
        for (int st_pol = 0; st_pol <= 1; ++st_pol) {
            for (int dir_pol = 0; dir_pol <= 1; ++dir_pol) {

                km_step_dir_config_t cfg = default_stepdir_config();
                cfg.enable_active_high = (en_pol != 0);
                cfg.step_active_high = (st_pol != 0);
                cfg.forward_high = (dir_pol != 0);

                uint32_t slot = (uint32_t)(en_pol * 4 + st_pol * 2 + dir_pol);
                uint32_t step_pin = 10u + slot * 3u;
                uint32_t dir_pin = 11u + slot * 3u;
                uint32_t enable_pin = 12u + slot * 3u;

                shim_gpio_clear_events();
                km_result_t res = km_stepdir_gpio_setup(slot, step_pin, dir_pin, enable_pin,
                                                        &cfg, dummy_ready_true, NULL, NULL);
                assert(res == KM_OK);
                assert(km_stepdir_gpio_is_slot_active(slot));

                /* Verify setup ordering:
                 * 1. ENABLE setup is first
                 * 2. STEP setup is second
                 * 3. DIR setup is third
                 */
                assert(shim_gpio_events_count >= 3u);
                assert(shim_gpio_events[0].pin == enable_pin);
                assert(shim_gpio_events[0].is_setup);
                assert(shim_gpio_events[0].val == (!cfg.enable_active_high ? 1u : 0u));

                assert(shim_gpio_events[1].pin == step_pin);
                assert(shim_gpio_events[1].is_setup);
                assert(shim_gpio_events[1].val == (!cfg.step_active_high ? 1u : 0u));

                /* DIR starts low whatever the lane's polarity: Klipper's
                 * stepper.c toggles it from there (direction bit 0). */
                assert(shim_gpio_events[2].pin == dir_pin);
                assert(shim_gpio_events[2].is_setup);
                assert(shim_gpio_events[2].val == 0u);

                /* Verify no positive enable or step transitions occurred */
                for (uint32_t e = 0u; e < shim_gpio_events_count; ++e) {
                    if (shim_gpio_events[e].pin == enable_pin) {
                        assert(shim_gpio_events[e].val == (!cfg.enable_active_high ? 1u : 0u));
                    }
                    if (shim_gpio_events[e].pin == step_pin) {
                        assert(shim_gpio_events[e].val == (!cfg.step_active_high ? 1u : 0u));
                    }
                }
            }
        }
    }
}

/* 6. Duplicate and cross-slot pin rejection (boundary and collision) */
static void test_duplicate_and_cross_slot_pin_rejection_boundary(void)
{
    init_and_ready_clock(520000000u);
    km_step_dir_config_t cfg = default_stepdir_config();

    /* Pin equality within slot */
    assert(km_stepdir_gpio_setup(0u, 10u, 10u, 12u, &cfg, dummy_ready_true, NULL, NULL) == KM_ERR_INVALID_PARAM);
    assert(km_stepdir_gpio_setup(0u, 10u, 11u, 10u, &cfg, dummy_ready_true, NULL, NULL) == KM_ERR_INVALID_PARAM);
    assert(km_stepdir_gpio_setup(0u, 10u, 11u, 11u, &cfg, dummy_ready_true, NULL, NULL) == KM_ERR_INVALID_PARAM);
    assert(shim_gpio_events_count == 0u);

    /* Valid setup on slot 0 */
    assert(km_stepdir_gpio_setup(0u, 10u, 11u, 12u, &cfg, dummy_ready_true, NULL, NULL) == KM_OK);
    uint32_t count_after_s0 = shim_gpio_events_count;

    /* Duplicate setup of slot 0 */
    assert(km_stepdir_gpio_setup(0u, 20u, 21u, 22u, &cfg, dummy_ready_true, NULL, NULL) == KM_ERR_BUSY);
    assert(shim_gpio_events_count == count_after_s0);

    /* Collisions with slot 0 pins on slot 1 */
    assert(km_stepdir_gpio_setup(1u, 10u, 21u, 22u, &cfg, dummy_ready_true, NULL, NULL) == KM_ERR_CONFLICT);
    assert(km_stepdir_gpio_setup(1u, 20u, 11u, 22u, &cfg, dummy_ready_true, NULL, NULL) == KM_ERR_CONFLICT);
    assert(km_stepdir_gpio_setup(1u, 20u, 21u, 12u, &cfg, dummy_ready_true, NULL, NULL) == KM_ERR_CONFLICT);
    assert(km_stepdir_gpio_setup(1u, 12u, 21u, 22u, &cfg, dummy_ready_true, NULL, NULL) == KM_ERR_CONFLICT);
    assert(km_stepdir_gpio_setup(1u, 20u, 10u, 22u, &cfg, dummy_ready_true, NULL, NULL) == KM_ERR_CONFLICT);
    assert(km_stepdir_gpio_setup(1u, 20u, 21u, 11u, &cfg, dummy_ready_true, NULL, NULL) == KM_ERR_CONFLICT);
    assert(shim_gpio_events_count == count_after_s0);
}

/* 7. Partial setup non-returning failure and pin immutability */
static void test_partial_setup_nonreturning_failure(void)
{
    init_and_ready_clock(520000000u);
    km_step_dir_config_t cfg = default_stepdir_config();

    /* Make DIR pin fail upstream setup */
    shim_gpio_fail_pin = 11u;
    test_shim_use_longjmp = true;

    if (setjmp(test_shim_shutdown_jump) == 0) {
        (void)km_stepdir_gpio_setup(0u, 10u, 11u, 12u, &cfg, dummy_ready_true, NULL, NULL);
        assert(false); /* Should not return normally */
    }
    test_shim_use_longjmp = false;

    assert(test_shim_is_shutdown);
    assert(km_stepdir_gpio_is_shutdown());
    assert(km_stepdir_gpio_get_driver(0u) == NULL);

    /* Touched pins 12 (ENABLE) and 10 (STEP) were set up and then driven inactive during shutdown */
    assert(shim_gpio_is_setup[12u]);
    assert(shim_gpio_is_setup[10u]);
    assert(!shim_gpio_is_setup[11u]);

    assert(shim_gpio_current_level[12u] == (!cfg.enable_active_high ? 1u : 0u));
    assert(shim_gpio_current_level[10u] == (!cfg.step_active_high ? 1u : 0u));
}

/* 9. Genuine readiness suppression and loss */
static void test_readiness_suppression_and_loss(void)
{
    init_and_ready_clock(520000000u);
    km_step_dir_config_t cfg = default_stepdir_config();

    /* When ready callback returns false, setup fails */
    assert(km_stepdir_gpio_setup(0u, 10u, 11u, 12u, &cfg, dummy_ready_false, NULL, NULL) == KM_ERR_UNAVAILABLE);
    assert(!km_stepdir_gpio_is_slot_active(0u));
    assert(km_stepdir_gpio_get_driver(0u) == NULL);

    /* Slot 0 claimed pins remain unavailable */
    assert(km_stepdir_gpio_setup(1u, 10u, 21u, 22u, &cfg, dummy_ready_true, NULL, NULL) == KM_ERR_CONFLICT);

    /* Now test on slot 1 with dynamic ready and dynamic fault */
    dynamic_ready = true;
    dynamic_fault = KM_FAULT_NONE;
    assert(km_stepdir_gpio_setup(1u, 20u, 21u, 22u, &cfg, dynamic_ready_fn, dynamic_fault_fn, NULL) == KM_OK);
    km_driver_t *d1 = km_stepdir_gpio_get_driver(1u);
    assert(d1 != NULL);

    /* Verify custom fault source propagation */
    dynamic_fault = KM_FAULT_DRIVER_OVERCURRENT;
    assert(km_driver_get_fault(d1) == KM_FAULT_DRIVER_OVERCURRENT);
    dynamic_fault = KM_FAULT_NONE;
    assert(km_driver_get_fault(d1) == KM_FAULT_NONE);

    /* Loss of readiness before enable suppresses enable */
    dynamic_ready = false;
    assert(locked_enable(d1, true) == KM_ERR_UNAVAILABLE);
    assert(d1->enabled == false);
    assert(shim_gpio_current_level[22u] == 0u); /* Inactive */

    /* Restore readiness, enable succeeds */
    dynamic_ready = true;
    assert(locked_enable(d1, true) == KM_OK);
    assert(d1->enabled == true);

    /* Loss of readiness while enabled faults on step attempt */
    dynamic_ready = false;
}

/* 17. No resume after clear_shutdown */
static void test_no_resume_after_clear_shutdown(void)
{
    init_and_ready_clock(520000000u);
    km_step_dir_config_t cfg = default_stepdir_config();

    assert(km_stepdir_gpio_setup(0u, 10u, 11u, 12u, &cfg, dummy_ready_true, NULL, NULL) == KM_OK);
    km_driver_t *d = km_stepdir_gpio_get_driver(0u);
    assert(d != NULL);

    /* Trigger emergency shutdown */
    emergency_stop("Emergency stop");
    assert(km_stepdir_gpio_is_shutdown());
    assert(km_stepdir_gpio_get_driver(0u) == NULL);

    /* Upstream clears shutdown */
    sched_clear_shutdown();
    assert(sched_is_shutdown() == 0u);

    /* Binding latch MUST remain latched in shutdown */
    assert(km_stepdir_gpio_is_shutdown() == true);
    assert(km_stepdir_gpio_get_driver(0u) == NULL);

    /* Cannot setup any slot after shutdown */
    assert(km_stepdir_gpio_setup(1u, 20u, 21u, 22u, &cfg, dummy_ready_true, NULL, NULL) == KM_ERR_FAULTED);

    /* Cannot clear fault on existing driver */
    assert(locked_clear_fault(d) == KM_ERR_UNAVAILABLE);
    assert(locked_enable(d, true) == KM_ERR_FAULTED);
}

/* 18. Pin query API */
static void test_slot_pins_query(void)
{
    init_and_ready_clock(520000000u);
    km_step_dir_config_t cfg = default_stepdir_config();

    uint32_t s_pin = 0u, d_pin = 0u, e_pin = 0u;
    /* Unclaimed slot query */
    assert(km_stepdir_gpio_get_slot_pins(0u, &s_pin, &d_pin, &e_pin) == KM_ERR_UNAVAILABLE);

    /* Claimed slot */
    assert(km_stepdir_gpio_setup(0u, 33u, 34u, 35u, &cfg, dummy_ready_true, NULL, NULL) == KM_OK);
    assert(km_stepdir_gpio_get_slot_pins(0u, &s_pin, &d_pin, &e_pin) == KM_OK);
    assert(s_pin == 33u && d_pin == 34u && e_pin == 35u);

    /* Invalid params */
    assert(km_stepdir_gpio_get_slot_pins(KM_MAX_LANES, &s_pin, &d_pin, &e_pin) == KM_ERR_INVALID_PARAM);
    assert(km_stepdir_gpio_get_slot_pins(0u, NULL, &d_pin, &e_pin) == KM_ERR_INVALID_PARAM);
}

/* 19. Output-bound accessor lifecycle, bounds, and coarse/fine coherence */
static void test_output_bound_accessor_lifecycle_and_coherence(void)
{
    /* 1. Standalone km_mcu_clock_t checks */
    km_core_timepoint_t tp = { .ms = 1234u, .us = 5678u };
    uint32_t u = 9999u;

    /* Invalid parameters */
    assert(km_mcu_clock_get_completion_time(NULL, &tp) == KM_ERR_INVALID_PARAM);
    assert(km_mcu_clock_get_completion_us(NULL, &u) == KM_ERR_INVALID_PARAM);

    km_mcu_clock_t clk;
    assert(km_mcu_clock_get_completion_time(&clk, NULL) == KM_ERR_INVALID_PARAM);
    assert(km_mcu_clock_get_completion_us(&clk, NULL) == KM_ERR_INVALID_PARAM);

    /* Uninitialized clock */
    memset(&clk, 0, sizeof(clk));
    assert(km_mcu_clock_get_completion_time(&clk, &tp) == KM_ERR_UNAVAILABLE);
    assert(tp.ms == 1234u && tp.us == 5678u); /* Preserved */
    assert(km_mcu_clock_get_completion_us(&clk, &u) == KM_ERR_UNAVAILABLE);
    assert(u == 9999u); /* Preserved */

    /* Initialize healthy clock at 12.288 MHz */
    assert(km_mcu_clock_init(&clk, 12288000u, 1000u, (km_core_timepoint_t){.ms = 1u, .us = 1999u}) == KM_OK);

    /* Advance by 1 tick -> delta = 1 -> delta_us = 0, frac_num = 1000000 > 0, sub_ms = 999 */
    assert(km_mcu_clock_observe(&clk, 1001u, &tp) == KM_OK);
    assert(tp.ms == 1u && tp.us == 1999u);

    /* Test coarse/fine coherence rollover:
     * When sub_ms == 999 and frac_num > 0, rounding up us rolls over into ms + 1.
     * Verified invariant: (comp_tp.us - comp_tp.ms * 1000) < 1000. */
    km_core_timepoint_t comp_tp = {0};
    assert(km_mcu_clock_get_completion_time(&clk, &comp_tp) == KM_OK);
    assert(comp_tp.ms == 2u);
    assert(comp_tp.us == 2000u);
    assert(comp_tp.us - comp_tp.ms * 1000u < 1000u);
    assert(comp_tp.us - comp_tp.ms * 1000u == 0u);

    assert(km_mcu_clock_get_completion_us(&clk, &u) == KM_OK);
    assert(u == 2000u);

    /* Pure floor snapshot remains unchanged */
    km_core_timepoint_t floor_tp = {0};
    assert(km_mcu_clock_get_time(&clk, &floor_tp) == KM_OK);
    assert(floor_tp.ms == 1u && floor_tp.us == 1999u);

    /* Fault latches and preserves outputs */
    assert(km_mcu_clock_observe(&clk, 1001u + 0x80000000u, &tp) == KM_ERR_FAULTED);
    tp = (km_core_timepoint_t){ .ms = 111u, .us = 222u };
    u = 333u;
    assert(km_mcu_clock_get_completion_time(&clk, &tp) == KM_ERR_FAULTED);
    assert(tp.ms == 111u && tp.us == 222u);
    assert(km_mcu_clock_get_completion_us(&clk, &u) == KM_ERR_FAULTED);
    assert(u == 333u);

    /* 2. Shared MCU adapter clock checks */
    shim_reset(12288000u, 1u, 1u, 4u);
    shim_run_init();
    assert(km_clock_get_completion_time(NULL) == KM_ERR_INVALID_PARAM);
    assert(km_clock_get_completion_us(NULL) == KM_ERR_INVALID_PARAM);

    /* Unready state */
    assert(km_clock_get_completion_time(&tp) == KM_ERR_UNAVAILABLE);
    assert(tp.ms == 111u && tp.us == 222u);
    assert(km_clock_get_completion_us(&u) == KM_ERR_UNAVAILABLE);
    assert(u == 333u);

    /* Transition to READY */
    shim_set_board_ready(true);
    shim_run_task();
    assert(km_clock_get_state() == KM_CLOCK_STATE_READY);

    /* Advance 124 ticks at 12.288 MHz (10.0911458... us) */
    shim_advance_ticks(124u);
    assert(km_clock_get_time(&floor_tp) == KM_OK);
    assert(floor_tp.us == 10u);

    assert(km_clock_get_completion_time(&comp_tp) == KM_OK);
    assert(comp_tp.us == 11u);
    assert(comp_tp.us - comp_tp.ms * 1000u < 1000u);

    assert(km_clock_get_completion_us(&u) == KM_OK);
    assert(u == 11u);
}

static void test_prepare_delayed_activation(void)
{
    init_and_ready_clock(520000000u);
    km_step_dir_config_t cfg = default_stepdir_config();
    void *safe_context = NULL;
    dynamic_ready = false;
    dynamic_fault = KM_FAULT_NONE;
    assert(km_stepdir_gpio_prepare(0u, 10u, 11u, 12u, &cfg,
                                    dynamic_ready_fn, dynamic_fault_fn, NULL,
                                    &safe_context) == KM_OK);
    assert(safe_context != NULL);
    assert(km_stepdir_gpio_get_driver(0u) == NULL);
    assert(!km_stepdir_gpio_is_slot_active(0u));
    assert(km_stepdir_gpio_activate(0u) == KM_ERR_UNAVAILABLE);
    assert(km_stepdir_gpio_get_driver(0u) == NULL);
    assert(km_stepdir_gpio_prepare(0u, 10u, 11u, 12u, &cfg,
                                    dynamic_ready_fn, dynamic_fault_fn, NULL,
                                    &safe_context) == KM_ERR_BUSY);
    assert(km_stepdir_gpio_setup(1u, 12u, 20u, 21u, &cfg,
                                  dummy_ready_true, NULL, NULL) == KM_ERR_CONFLICT);
    assert(shim_gpio_setup_count[12u] == 1u);

    dynamic_ready = true;
    dynamic_fault = KM_FAULT_DRIVER_FAULT;
    assert(km_stepdir_gpio_activate(0u) == KM_ERR_FAULTED);
    assert(km_stepdir_gpio_get_driver(0u) == NULL);
    dynamic_fault = KM_FAULT_NONE;
    assert(km_stepdir_gpio_activate(0u) == KM_OK);
    km_driver_t *d = km_stepdir_gpio_get_driver(0u);
    assert(d && d->initialized && !d->enabled);
    assert(!d->caps.supports_current_setpoint);
    assert(km_stepdir_gpio_activate(0u) == KM_ERR_BUSY);

    /* A safe-off during the final ready query must prevent publication. */
    activation_ready_calls = 0;
    activation_safe_context = NULL;
    assert(km_stepdir_gpio_prepare(1u, 20u, 21u, 22u, &cfg,
                                    ready_with_raw_off_on_publish, NULL, NULL,
                                    &activation_safe_context) == KM_OK);
    assert(km_stepdir_gpio_activate(1u) == KM_ERR_FAULTED);
    assert(km_stepdir_gpio_get_driver(1u) == NULL);
    assert(km_stepdir_gpio_activate(1u) == KM_ERR_BUSY);
}

static void test_prepare_validation_and_raw_off(void)
{
    init_and_ready_clock(520000000u);
    km_step_dir_config_t cfg = default_stepdir_config();
    void *safe_context = NULL;
    cfg.pulse_high_us = 0;
    assert(km_stepdir_gpio_prepare(0u, 10u, 11u, 12u, &cfg,
                                    dummy_ready_true, NULL, NULL,
                                    &safe_context) == KM_ERR_INVALID_PARAM);
    assert(shim_gpio_events_count == 0u);
    assert(km_stepdir_gpio_get_slot_pins(0u, &(uint32_t){0},
                                         &(uint32_t){0}, &(uint32_t){0}) == KM_ERR_UNAVAILABLE);
    cfg = default_stepdir_config();
    assert(km_stepdir_gpio_prepare(0u, 10u, 11u, 12u, &cfg,
                                    dummy_ready_true, NULL, NULL,
                                    NULL) == KM_ERR_INVALID_PARAM);
    assert(shim_gpio_events_count == 0u);
    assert(km_stepdir_gpio_prepare(0u, 10u, 11u, 12u, &cfg,
                                    dummy_ready_true, NULL, NULL,
                                    &safe_context) == KM_OK);
    assert(km_stepdir_gpio_activate(0u) == KM_OK);
    km_driver_t *d = km_stepdir_gpio_get_driver(0u);
    assert(d && locked_enable(d, true) == KM_OK);
    shim_advance_ticks(5200u);
    km_core_timepoint_t tp;
    assert(km_clock_get_time(&tp) == KM_OK);
    assert(d->enabled);

    shim_gpio_clear_events();
    km_stepdir_gpio_force_off(safe_context);
    /* Raw safe-off drives ENABLE inactive and stops the local stepper (whose
     * stop returns DIR/STEP to unstep); it must emit no rising STEP edge. */
    assert(shim_gpio_current_level[12u] == 0u);
    bool saw_enable_off = false;
    for (uint32_t e = 0u; e < shim_gpio_events_count; ++e) {
        if (shim_gpio_events[e].pin == 12u && shim_gpio_events[e].val == 0u)
            saw_enable_off = true;
    }
    assert(saw_enable_off);
    assert(km_driver_get_fault(d) == KM_FAULT_DRIVER_FAULT);
    assert(locked_enable(d, true) == KM_ERR_FAULTED);
    assert(!d->enabled);
    assert(locked_clear_fault(d) == KM_OK);

    km_stepdir_gpio_shutdown();
    test_shim_clock_reads_in_shutdown = 0u;
    km_stepdir_gpio_force_off(safe_context);
    assert(test_shim_clock_reads_in_shutdown == 0u);
    assert(km_stepdir_gpio_activate(0u) == KM_ERR_FAULTED);
    assert(km_stepdir_gpio_get_driver(0u) == NULL);
}

/* Explicit clear of a raw safe-off latch: only once outputs are stopped and
 * the driver behind the callbacks is ready and fault-free again. */
static void test_clear_fault_after_raw_off(void)
{
    init_and_ready_clock(520000000u);
    km_step_dir_config_t cfg = default_stepdir_config();
    void *safe_context = NULL;
    dynamic_ready = true;
    dynamic_fault = KM_FAULT_NONE;
    assert(km_stepdir_gpio_clear_fault(KM_MAX_LANES) == KM_ERR_INVALID_PARAM);
    assert(km_stepdir_gpio_prepare(0u, 10u, 11u, 12u, &cfg,
                                    dynamic_ready_fn, dynamic_fault_fn, NULL,
                                    &safe_context) == KM_OK);
    assert(km_stepdir_gpio_clear_fault(0u) == KM_ERR_UNAVAILABLE); /* unpublished */
    assert(km_stepdir_gpio_activate(0u) == KM_OK);
    km_driver_t *d = km_stepdir_gpio_get_driver(0u);
    assert(d && locked_enable(d, true) == KM_OK);
    shim_advance_ticks(5200u);
    km_core_timepoint_t tp;
    assert(km_clock_get_time(&tp) == KM_OK);

    /* The UART binding's safe-off: ENABLE inactive. */
    dynamic_ready = false;
    dynamic_fault = KM_FAULT_DRIVER_FAULT;
    irqstatus_t flag = irq_save();
    km_stepdir_gpio_force_off(safe_context);
    irq_restore(flag);
    assert(shim_gpio_current_level[12u] == 0u);
    shim_gpio_clear_events();
    assert(km_stepdir_gpio_clear_fault(0u) == KM_ERR_BUSY); /* enabled */
    d->enabled = false;
    assert(km_driver_get_fault(d) == KM_FAULT_DRIVER_FAULT);

    /* The controller's stop drives both outputs inactive first. */
    flag = irq_save();
    km_driver_shutdown(d);
    irq_restore(flag);
    assert(shim_gpio_current_level[12u] == 0u && shim_gpio_current_level[10u] == 0u);
    shim_gpio_clear_events();
    dynamic_fault = KM_FAULT_NONE; /* Restarting: no fault, not yet ready. */
    assert(km_stepdir_gpio_clear_fault(0u) == KM_ERR_UNAVAILABLE);
    dynamic_ready = true;
    dynamic_fault = KM_FAULT_DRIVER_OVERCURRENT;
    assert(km_stepdir_gpio_clear_fault(0u) == KM_ERR_FAULTED);
    assert(km_driver_get_fault(d) == KM_FAULT_DRIVER_FAULT);
    dynamic_fault = KM_FAULT_NONE;
    assert(km_stepdir_gpio_clear_fault(0u) == KM_OK);
    assert(shim_gpio_events_count == 0u && !d->enabled && !d->step_pin_high);
    assert(km_driver_get_fault(d) == KM_FAULT_NONE);

    /* Normal output resumes only on explicit request and honors pulse-low
     * timing from the interrupted pulse's recorded fall. */
    assert(locked_enable(d, true) == KM_OK && shim_gpio_current_level[12u] == 1u);
    shim_advance_ticks(5200u * 20u);
    assert(km_clock_get_time(&tp) == KM_OK);

    /* A recurring fault latches exactly as before. */
    flag = irq_save();
    km_stepdir_gpio_force_off(safe_context);
    irq_restore(flag);
    assert(shim_gpio_current_level[12u] == 0u);
    assert(km_driver_get_fault(d) == KM_FAULT_DRIVER_FAULT);
    assert(locked_enable(d, true) == KM_ERR_FAULTED);

    km_stepdir_gpio_shutdown();
    assert(km_stepdir_gpio_clear_fault(0u) == KM_ERR_FAULTED);
    sched_clear_shutdown();
    assert(km_stepdir_gpio_clear_fault(0u) == KM_ERR_FAULTED);
}

/* Local stepper pool: room reports 4*KM_STEP_SEGMENTS_MAX per slot, a queue
 * one move beyond the room is refused, and the refusal leaves the queued moves
 * intact (they all still step and the room returns to full). */
static void test_local_pool_room_and_refusal(void)
{
    init_and_ready_clock(520000000u);
    struct stepper *s = active_local_stepper(0u, 10u, 11u, 12u, NULL);
    const int room = 4 * KM_STEP_SEGMENTS_MAX;
    const uint32_t interval = timer_from_us(50u);
    assert(stepper_local_room(s) == room);
    shim_gpio_clear_events();

    /* The first queued move is loaded immediately and its pool node goes back,
     * so priming one move leaves the room untouched. */
    uint32_t start = test_shim_ticks + timer_from_us(200u);
    assert(stepper_local_queue(s, start, interval, 1u, 0, 0) == 0);
    assert(stepper_local_room(s) == room);
    assert(stepper_local_active(s));

    for (int i = 0; i < room; ++i)
        assert(stepper_local_queue(s, start, interval, 1u, 0, 0) == 0);
    assert(stepper_local_room(s) == 0);

    /* One beyond the room is refused without queueing, stepping or clearing
     * the motion already in flight. */
    assert(stepper_local_queue(s, start, interval, 1u, 0, 0) != 0);
    assert(stepper_local_room(s) == 0);
    assert(stepper_local_active(s));
    assert(shim_gpio_events_count == 0u);

    drain_stepper(s);
    assert(!stepper_local_active(s));
    assert(stepper_local_room(s) == room); /* every node returned */
    assert(rising_edges(10u) == (uint32_t)(room + 1));
}

/* The wired local stepper's 32-bit position is unwrapped into the slot's exact
 * 64-bit position across both directions of the 2^31 and 2^32 crossings. */
static void test_local_position_unwraps_across_32_bits(void)
{
    init_and_ready_clock(520000000u);
    km_driver_t *d = NULL;
    active_local_stepper(0u, 10u, 11u, 12u, &d);
    assert(km_stepdir_gpio_get_position_for_driver(d) == 0);

    const int32_t seq[] = {(int32_t)0x7fffffff, (int32_t)0x80000000u,
                           (int32_t)0xffffffffu, 0, (int32_t)0xffffffffu,
                           (int32_t)0x7fffffff, 0};
    const int64_t want[] = {0x7fffffffLL, 0x80000000LL, 0xffffffffLL,
                            0x100000000LL, 0xffffffffLL, 0x7fffffffLL, 0};
    fake_position_enabled = true;
    for (unsigned i = 0u; i < 7u; ++i) {
        fake_position_value = seq[i];
        assert(km_stepdir_gpio_get_position_for_driver(d) == want[i]);
    }
}

/* Stop and raw safe-off discard queued moves: the stepper goes idle, the pool
 * returns, ENABLE is inactive and no step edge follows. */
static void test_local_stop_discards_queue(void)
{
    init_and_ready_clock(520000000u);
    const int room = 4 * KM_STEP_SEGMENTS_MAX;
    const uint32_t interval = timer_from_us(50u);
    km_step_dir_config_t cfg = default_stepdir_config();
    void *safe_context = NULL;
    assert(km_stepdir_gpio_prepare(0u, 10u, 11u, 12u, &cfg, dummy_ready_true,
                                   NULL, NULL, &safe_context) == KM_OK);
    assert(km_stepdir_gpio_activate(0u) == KM_OK);
    km_driver_t *d = km_stepdir_gpio_get_driver(0u);
    struct stepper *s = km_stepdir_gpio_get_stepper_for_driver(d);
    assert(d && s);

    /* Stop path: several queued moves, run part of them, then stop. */
    uint32_t start = test_shim_ticks + timer_from_us(200u);
    for (unsigned i = 0u; i < 4u; ++i)
        assert(stepper_local_queue(s, start, interval, 4u, 0, 1u) == 0);
    assert(stepper_local_room(s) < room);
    dispatch_rising_edges(s, 10u, 2u);
    assert(stepper_local_active(s));
    km_driver_shutdown(d);
    assert(!stepper_local_active(s) && stepper_local_room(s) == room);
    assert(shim_gpio_current_level[12u] == 0u);
    shim_gpio_clear_events();
    dispatch_idle_while();
    assert(rising_edges(10u) == 0u && !stepper_local_active(s));

    /* Raw safe-off path: the same discard, without a fresh enable. */
    start = test_shim_ticks + timer_from_us(200u);
    for (unsigned i = 0u; i < 4u; ++i)
        assert(stepper_local_queue(s, start, interval, 4u, 0, 1u) == 0);
    dispatch_rising_edges(s, 10u, 1u);
    assert(stepper_local_active(s));
    km_stepdir_gpio_force_off(safe_context);
    assert(!stepper_local_active(s) && stepper_local_room(s) == room);
    assert(shim_gpio_current_level[12u] == 0u);
    shim_gpio_clear_events();
    dispatch_idle_while();
    assert(rising_edges(10u) == 0u && !stepper_local_active(s));
}

/* The DECL_SHUTDOWN hook stops every slot mid-pulse: both go idle, ENABLE is
 * driven inactive no later than STEP, and nothing steps afterwards. */
static void test_shutdown_hook_stops_every_slot(void)
{
    init_and_ready_clock(520000000u);
    km_driver_t *d0 = NULL, *d1 = NULL;
    struct stepper *s0 = active_local_stepper(0u, 10u, 11u, 12u, &d0);
    struct stepper *s1 = active_local_stepper(1u, 20u, 21u, 22u, &d1);
    const uint32_t interval = timer_from_us(50u);
    uint32_t start = test_shim_ticks + timer_from_us(200u);
    assert(stepper_local_queue(s0, start, interval, 6u, 0, 1u) == 0);
    assert(stepper_local_queue(s1, start, interval, 6u, 0, 1u) == 0);
    dispatch_rising_edges(s0, 10u, 1u);
    dispatch_rising_edges(s1, 20u, 1u);
    assert(stepper_local_active(s0) && stepper_local_active(s1));
    assert(shim_gpio_current_level[10u] == 1u && shim_gpio_current_level[20u] == 1u);

    shim_gpio_clear_events();
    km_stepdir_gpio_shutdown();
    assert(!stepper_local_active(s0) && !stepper_local_active(s1));
    assert(shim_gpio_current_level[12u] == 0u && shim_gpio_current_level[22u] == 0u);
    for (unsigned slot = 0u; slot < 2u; ++slot) {
        uint32_t step_pin = slot ? 20u : 10u, enable_pin = slot ? 22u : 12u;
        uint32_t enable_off = UINT32_MAX, step_off = UINT32_MAX;
        for (uint32_t e = 0u; e < shim_gpio_events_count; ++e) {
            if (shim_gpio_events[e].pin == enable_pin && shim_gpio_events[e].val == 0u)
                enable_off = e;
            if (shim_gpio_events[e].pin == step_pin && shim_gpio_events[e].val == 0u)
                step_off = e;
        }
        assert(enable_off != UINT32_MAX && step_off != UINT32_MAX && enable_off < step_off);
    }

    shim_gpio_clear_events();
    dispatch_idle_while();
    assert(rising_edges(10u) == 0u && rising_edges(20u) == 0u);
}

/* Two slots step independently, including an inverted ENABLE on slot 1: one
 * slot's motion leaves the other slot's STEP/DIR/ENABLE pins untouched. */
static void test_local_two_slots_step_independently(void)
{
    init_and_ready_clock(520000000u);
    km_step_dir_config_t cfg0 = default_stepdir_config();
    km_step_dir_config_t cfg1 = default_stepdir_config();
    cfg1.enable_active_high = false; /* the old isolation test's inverted enable */
    void *safe0 = NULL, *safe1 = NULL;
    assert(km_stepdir_gpio_prepare(0u, 10u, 11u, 12u, &cfg0, dummy_ready_true,
                                   NULL, NULL, &safe0) == KM_OK);
    assert(km_stepdir_gpio_prepare(1u, 20u, 21u, 22u, &cfg1, dummy_ready_true,
                                   NULL, NULL, &safe1) == KM_OK);
    assert(km_stepdir_gpio_activate(0u) == KM_OK);
    assert(km_stepdir_gpio_activate(1u) == KM_OK);
    km_driver_t *d0 = km_stepdir_gpio_get_driver(0u);
    km_driver_t *d1 = km_stepdir_gpio_get_driver(1u);
    struct stepper *s0 = km_stepdir_gpio_get_stepper_for_driver(d0);
    struct stepper *s1 = km_stepdir_gpio_get_stepper_for_driver(d1);
    assert(d0 && d1 && s0 && s1);
    assert(shim_gpio_current_level[12u] == 0u && shim_gpio_current_level[22u] == 1u);

    const uint32_t interval = timer_from_us(50u);
    uint32_t start = test_shim_ticks + timer_from_us(200u);

    /* Slot 0 alone steps; slot 1's pins see no write at all. */
    uint32_t quiet0 = shim_gpio_write_count[20u] + shim_gpio_write_count[21u]
                    + shim_gpio_write_count[22u];
    assert(locked_enable(d0, true) == KM_OK);
    assert(stepper_local_queue(s0, start, interval, 5u, 0, 1u) == 0);
    drain_stepper(s0);
    assert(rising_edges(10u) == 5u);
    assert(shim_gpio_write_count[20u] + shim_gpio_write_count[21u]
           + shim_gpio_write_count[22u] == quiet0);

    /* Slot 1 alone steps (inverted ENABLE active low); slot 0 goes quiet. */
    uint32_t quiet1 = shim_gpio_write_count[10u] + shim_gpio_write_count[11u]
                    + shim_gpio_write_count[12u];
    assert(locked_enable(d1, true) == KM_OK && shim_gpio_current_level[22u] == 0u);
    start = test_shim_ticks + timer_from_us(200u);
    assert(stepper_local_queue(s1, start, interval, 4u, 0, 1u) == 0);
    drain_stepper(s1);
    assert(rising_edges(20u) == 4u);
    assert(shim_gpio_write_count[10u] + shim_gpio_write_count[11u]
           + shim_gpio_write_count[12u] == quiet1);
    assert(km_stepdir_gpio_get_position_for_driver(d0) == 5);
}

/* Each queued step yields exactly one rising STEP edge and advances the slot's
 * 64-bit position by one step, in either direction. */
static void test_local_one_rising_edge_per_step(void)
{
    init_and_ready_clock(520000000u);
    km_driver_t *d = NULL;
    struct stepper *s = active_local_stepper(0u, 10u, 11u, 12u, &d);
    const uint32_t interval = timer_from_us(50u);
    const int16_t add = (int16_t)timer_from_us(2u);
    const uint32_t n = 6u;

    shim_gpio_clear_events();
    uint32_t start = test_shim_ticks + timer_from_us(200u);
    assert(stepper_local_queue(s, start, interval, (uint16_t)n, add, 1u) == 0);
    drain_stepper(s);
    assert(rising_edges(10u) == n);
    assert(km_stepdir_gpio_get_position_for_driver(d) == (int64_t)n);

    shim_gpio_clear_events();
    start = test_shim_ticks + timer_from_us(200u);
    assert(stepper_local_queue(s, start, interval, (uint16_t)n, add, 0u) == 0);
    drain_stepper(s);
    assert(rising_edges(10u) == n);
    assert(km_stepdir_gpio_get_position_for_driver(d) == 0);
}

/* DIR polarity: a lane's forward maps through the slot's forward_high, a
 * forward move holds DIR at the lane's forward level for the whole move, and
 * the slot counts forward steps positive. Only the forward-low lane (the
 * BoxTurtle/AFC preset) had coverage; forward-high is the other wiring the
 * pin config allows. */
/* An active-low STEP lane: every step is one active (falling) edge and STEP
 * rests high, including after a stop, which returns STEP to the unstep level. */
static void test_local_step_polarity_active_low(void)
{
    init_and_ready_clock(520000000u);
    const uint32_t step_pin = 10u, dir_pin = 11u, enable_pin = 12u;
    km_step_dir_config_t cfg = default_stepdir_config();
    cfg.step_active_high = false;
    void *safe_context = NULL;
    assert(km_stepdir_gpio_prepare(0u, step_pin, dir_pin, enable_pin, &cfg,
                                   dummy_ready_true, NULL, NULL, &safe_context) == KM_OK);
    assert(km_stepdir_gpio_activate(0u) == KM_OK);
    km_driver_t *d = km_stepdir_gpio_get_driver(0u);
    struct stepper *s = km_stepdir_gpio_get_stepper_for_driver(d);
    assert(d && s && shim_gpio_current_level[step_pin] == 1u);
    for (unsigned round = 0u; round < 2u; ++round) {
        shim_gpio_clear_events();
        uint32_t start = test_shim_ticks + timer_from_us(200u);
        assert(km_stepdir_gpio_queue(d, start, timer_from_us(50u), 3u, 0, 1u) == 0);
        drain_stepper(s);
        uint32_t falls = 0u;
        for (uint32_t e = 0u; e < shim_gpio_events_count; ++e)
            if (shim_gpio_events[e].pin == step_pin && shim_gpio_events[e].val == 0u)
                falls++;
        assert(falls == 3u && rising_edges(step_pin) == 3u);
        assert(shim_gpio_current_level[step_pin] == 1u);
        stepper_local_stop(s);
        assert(shim_gpio_current_level[step_pin] == 1u);
    }
}

static void test_local_dir_polarity_both_lanes(void)
{
    init_and_ready_clock(520000000u);
    const uint32_t interval = timer_from_us(50u);
    const uint32_t n = 6u;

    for (unsigned lane = 0u; lane < 2u; ++lane) {
        bool forward_high = (lane == 0u);
        uint32_t step_pin = 10u + lane * 10u;
        uint32_t dir_pin = 11u + lane * 10u;
        uint32_t enable_pin = 12u + lane * 10u;
        uint32_t forward_level = forward_high ? 1u : 0u;
        uint32_t reverse_level = forward_high ? 0u : 1u;

        km_step_dir_config_t cfg = default_stepdir_config();
        cfg.forward_high = forward_high;
        void *safe_context = NULL;
        assert(km_stepdir_gpio_prepare(lane, step_pin, dir_pin, enable_pin, &cfg,
                                       dummy_ready_true, NULL, NULL,
                                       &safe_context) == KM_OK);
        assert(km_stepdir_gpio_activate(lane) == KM_OK);
        km_driver_t *d = km_stepdir_gpio_get_driver(lane);
        struct stepper *s = km_stepdir_gpio_get_stepper_for_driver(d);
        assert(d && s);
        assert(km_stepdir_gpio_get_position_for_driver(d) == 0);

        /* Klipper's local stepper assumes DIR starts low and only toggles it;
         * the first forward move must already be forward on both lanes. */
        assert(shim_gpio_current_level[dir_pin] == 0u);

        /* Forward: DIR carries the lane's forward level, every step is one
         * rising STEP edge, and the position advances by n. */
        shim_gpio_clear_events();
        uint32_t start = test_shim_ticks + timer_from_us(200u);
        assert(km_stepdir_gpio_queue(d, start, interval, (uint16_t)n, 0, 1u) == 0);
        dispatch_rising_edges(s, step_pin, 1u);
        assert(shim_gpio_current_level[dir_pin] == forward_level);
        drain_stepper(s);
        assert(rising_edges(step_pin) == n);
        assert(km_stepdir_gpio_get_position_for_driver(d) == (int64_t)n);

        /* Reverse: the opposite DIR level, n rising edges, position back to 0. */
        shim_gpio_clear_events();
        start = test_shim_ticks + timer_from_us(200u);
        assert(km_stepdir_gpio_queue(d, start, interval, (uint16_t)n, 0, 0u) == 0);
        dispatch_rising_edges(s, step_pin, 1u);
        assert(shim_gpio_current_level[dir_pin] == reverse_level);
        drain_stepper(s);
        assert(rising_edges(step_pin) == n);
        assert(km_stepdir_gpio_get_position_for_driver(d) == 0);
    }

    /* A driver stop returns DIR low and clears Klipper's direction state: the
     * forward-low lane must still drive DIR low for its next forward move. */
    km_driver_t *d = km_stepdir_gpio_get_driver(1u);
    struct stepper *s = km_stepdir_gpio_get_stepper_for_driver(d);
    assert(d && s);
    km_driver_shutdown(d);
    assert(!stepper_local_active(s));
    assert(shim_gpio_current_level[21u] == 0u);

    shim_gpio_clear_events();
    uint32_t start = test_shim_ticks + timer_from_us(200u);
    assert(km_stepdir_gpio_queue(d, start, interval, (uint16_t)n, 0, 1u) == 0);
    dispatch_rising_edges(s, 20u, 1u);
    assert(shim_gpio_current_level[21u] == 0u);
    drain_stepper(s);
    assert(rising_edges(20u) == n);
    assert(km_stepdir_gpio_get_position_for_driver(d) == (int64_t)n);
}

/* Saved BoxTurtle H723 image: four lanes through production GPIO binding. */
#ifdef KM_H723_IMAGE_AUTOCONF
static void test_saved_boxturtle_h723_four_lane_gpio_edges(void)
{
    init_and_ready_clock(520000000u);
    uint32_t step[4], dir[4], enable[4];
    km_step_dir_config_t cfg[4];
    void *force_off[4] = {0};
    for (unsigned lane = 0u; lane < 4u; ++lane) {
        step[lane] = saved_pin_at(CONFIG_KLIPPER_MMU_PINS_STEP, lane);
        dir[lane] = saved_pin_at(CONFIG_KLIPPER_MMU_PINS_DIR, lane);
        enable[lane] = saved_pin_at(CONFIG_KLIPPER_MMU_PINS_ENABLE, lane);
        cfg[lane] = default_stepdir_config();
        cfg[lane].enable_active_high = saved_level_at(CONFIG_KLIPPER_MMU_FEEDER_ENABLE_HIGH, lane);
        cfg[lane].step_active_high = saved_level_at(CONFIG_KLIPPER_MMU_FEEDER_STEP_HIGH, lane);
        cfg[lane].forward_high = saved_level_at(CONFIG_KLIPPER_MMU_FEEDER_FORWARD_HIGH, lane);
        assert(km_stepdir_gpio_prepare(lane, step[lane], dir[lane], enable[lane],
                                       &cfg[lane], dynamic_ready_fn, NULL, NULL,
                                       &force_off[lane]) == KM_OK);
        assert(force_off[lane] != NULL);
        assert(shim_gpio_current_level[enable[lane]] == (cfg[lane].enable_active_high ? 0u : 1u));
        assert(shim_gpio_current_level[step[lane]] == (cfg[lane].step_active_high ? 0u : 1u));
        assert(shim_gpio_current_level[dir[lane]] == (cfg[lane].forward_high ? 1u : 0u));
    }

    dynamic_ready = false;
    assert(km_stepdir_gpio_activate(0u) == KM_ERR_UNAVAILABLE);
    dynamic_ready = true;
    for (unsigned lane = 0u; lane < 4u; ++lane)
        assert(km_stepdir_gpio_activate(lane) == KM_OK);

    for (unsigned lane = 0u; lane < 4u; ++lane) {
        km_driver_t *d = km_stepdir_gpio_get_driver(lane);
        struct stepper *s = km_stepdir_gpio_get_stepper_for_driver(d);
        assert(d != NULL && s != NULL);
        const uint32_t forward_level = cfg[lane].forward_high ? 1u : 0u;
        const uint32_t step_active = cfg[lane].step_active_high ? 1u : 0u;
        const uint32_t step_inactive = cfg[lane].step_active_high ? 0u : 1u;
        const uint32_t enable_active = cfg[lane].enable_active_high ? 1u : 0u;
        const uint32_t enable_inactive = cfg[lane].enable_active_high ? 0u : 1u;

        /* One forward step through the local stepper replaces the removed
         * per-edge path: enable, queue the step, run through its trailing
         * unstep, then disable. */
        shim_gpio_clear_events();
        assert(locked_enable(d, true) == KM_OK);
        uint32_t start = test_shim_ticks + timer_from_us(200u);
        assert(km_stepdir_gpio_queue(d, start, timer_from_us(50u), 1u, 0, 1u) == 0);
        dispatch_rising_edges(s, step[lane], 1u);
        /* DIR holds the lane's forward level for the step; a DIR write only
         * happens when that differs from DIR's low setup level. */
        assert(shim_gpio_current_level[dir[lane]] == forward_level);
        drain_stepper(s);
        assert(locked_enable(d, false) == KM_OK);

        bool touched_enable = false, touched_step = false, touched_dir = false;
        for (uint32_t e = 0u; e < shim_gpio_events_count; ++e) {
            shim_gpio_event_t event = shim_gpio_events[e];
            assert(!event.is_setup);
            assert(event.pin == enable[lane] || event.pin == step[lane] || event.pin == dir[lane]);
            if (event.pin == enable[lane]) touched_enable = true;
            if (event.pin == step[lane]) touched_step = true;
            if (event.pin == dir[lane]) touched_dir = true;
        }
        assert(touched_enable && touched_step);
        assert(touched_dir == (forward_level != 0u));
        uint32_t enable_on = UINT32_MAX, direction = UINT32_MAX;
        uint32_t step_rise = UINT32_MAX, step_fall = UINT32_MAX, enable_off = UINT32_MAX;
        for (uint32_t e = 0u; e < shim_gpio_events_count; ++e) {
            shim_gpio_event_t event = shim_gpio_events[e];
            if (event.pin == enable[lane] && event.val == enable_active
                && enable_on == UINT32_MAX) enable_on = e;
            if (event.pin == dir[lane] && event.val == forward_level
                && direction == UINT32_MAX) direction = e;
            if (event.pin == step[lane] && event.val == step_active
                && step_rise == UINT32_MAX) step_rise = e;
            if (event.pin == step[lane] && event.val == step_inactive
                && step_rise != UINT32_MAX && e > step_rise && step_fall == UINT32_MAX)
                step_fall = e;
            if (event.pin == enable[lane] && event.val == enable_inactive
                && enable_on != UINT32_MAX && e > enable_on && enable_off == UINT32_MAX)
                enable_off = e;
        }
        assert(enable_on != UINT32_MAX && step_rise != UINT32_MAX);
        assert(enable_on < step_rise && step_rise < step_fall && step_fall < enable_off);
        if (touched_dir)
            assert(enable_on < direction && direction < step_rise);
        assert(shim_gpio_current_level[enable[lane]] == enable_inactive);
        assert(shim_gpio_current_level[step[lane]] == step_inactive);
        assert(shim_gpio_current_level[dir[lane]] == forward_level);
    }

    /* Force-off mid-step on lane 0. The stop discards the queued moves and
     * returns STEP to its inactive level, replacing the old "service the
     * interrupted pulse through the normal path" assertion. */
    km_driver_t *d0 = km_stepdir_gpio_get_driver(0u);
    struct stepper *s0 = km_stepdir_gpio_get_stepper_for_driver(d0);
    assert(d0 != NULL && s0 != NULL);
    const uint32_t step_active0 = cfg[0].step_active_high ? 1u : 0u;
    const uint32_t step_inactive0 = cfg[0].step_active_high ? 0u : 1u;
    assert(locked_enable(d0, true) == KM_OK);
    uint32_t start = test_shim_ticks + timer_from_us(200u);
    assert(km_stepdir_gpio_queue(d0, start, timer_from_us(50u), 4u, 0, 1u) == 0);
    dispatch_rising_edges(s0, step[0], 1u);
    assert(shim_gpio_current_level[step[0]] == step_active0);

    uint32_t quiet = 0u;
    for (unsigned lane = 1u; lane < 4u; ++lane)
        quiet += shim_gpio_write_count[step[lane]] + shim_gpio_write_count[dir[lane]]
               + shim_gpio_write_count[enable[lane]];
    shim_gpio_clear_events();
    km_stepdir_gpio_force_off(force_off[0]);
    assert(shim_gpio_current_level[enable[0]] == (cfg[0].enable_active_high ? 0u : 1u));
    assert(shim_gpio_current_level[step[0]] == step_inactive0);
    assert(!stepper_local_active(s0));
    for (uint32_t e = 0u; e < shim_gpio_events_count; ++e) {
        assert(!shim_gpio_events[e].is_setup);
        assert(shim_gpio_events[e].pin == enable[0] || shim_gpio_events[e].pin == step[0]
               || shim_gpio_events[e].pin == dir[0]);
        assert(shim_gpio_events[e].pin != step[0] || shim_gpio_events[e].val != step_active0);
    }
    uint32_t step_writes = shim_gpio_write_count[step[0]];
    shim_gpio_clear_events();
    dispatch_idle_while();
    assert(rising_edges(step[0]) == 0u);
    assert(shim_gpio_write_count[step[0]] == step_writes);
    assert(!stepper_local_active(s0));
    uint32_t quiet_after = 0u;
    for (unsigned lane = 1u; lane < 4u; ++lane)
        quiet_after += shim_gpio_write_count[step[lane]] + shim_gpio_write_count[dir[lane]]
                     + shim_gpio_write_count[enable[lane]];
    assert(quiet_after == quiet);

    km_stepdir_gpio_shutdown();
    for (unsigned lane = 0u; lane < 4u; ++lane) {
        assert(shim_gpio_current_level[enable[lane]] == (cfg[lane].enable_active_high ? 0u : 1u));
        assert(shim_gpio_current_level[step[lane]] == (cfg[lane].step_active_high ? 0u : 1u));
    }
}
#endif

static void run_isolated(void (*test_fn)(void), const char *name)
{
    printf("Running %-56s ... ", name);
    fflush(stdout);

    pid_t pid = fork();
    if (pid < 0) {
        perror("fork");
        exit(1);
    }
    if (pid == 0) {
        alarm(15);
        test_fn();
        exit(0);
    }

    int status = 0;
    waitpid(pid, &status, 0);
    if (WIFEXITED(status) && WEXITSTATUS(status) == 0) {
        printf("PASS\n");
    } else if (WIFSIGNALED(status)) {
        printf("FAIL (signal %d)\n", WTERMSIG(status));
        exit(1);
    } else {
        printf("FAIL (exit code %d)\n", WEXITSTATUS(status));
        exit(1);
    }
}

int main(void)
{
    printf("============================================================\n");
    printf("  Klipper MMU STEP/DIR GPIO Binding Integration Tests       \n");
    printf("============================================================\n");

    run_isolated(test_no_boot_gpio, "test_no_boot_gpio");
    run_isolated(test_pre_ready_clock_rejection_boundary, "test_pre_ready_clock_rejection_boundary");
    run_isolated(test_irq_disabled_rejection_boundary, "test_irq_disabled_rejection_boundary");
    run_isolated(test_invalid_config_rejection_boundary, "test_invalid_config_rejection_boundary");
    run_isolated(test_inactive_levels_and_polarities, "test_inactive_levels_and_polarities");
    run_isolated(test_duplicate_and_cross_slot_pin_rejection_boundary, "test_duplicate_and_cross_slot_pin_rejection_boundary");
    run_isolated(test_partial_setup_nonreturning_failure, "test_partial_setup_nonreturning_failure");
    run_isolated(test_readiness_suppression_and_loss, "test_readiness_suppression_and_loss");
    run_isolated(test_no_resume_after_clear_shutdown, "test_no_resume_after_clear_shutdown");
    run_isolated(test_slot_pins_query, "test_slot_pins_query");
    run_isolated(test_output_bound_accessor_lifecycle_and_coherence, "test_output_bound_accessor_lifecycle_and_coherence");
    run_isolated(test_prepare_delayed_activation, "test_prepare_delayed_activation");
    run_isolated(test_prepare_validation_and_raw_off, "test_prepare_validation_and_raw_off");
    run_isolated(test_clear_fault_after_raw_off, "test_clear_fault_after_raw_off");
    run_isolated(test_local_pool_room_and_refusal, "test_local_pool_room_and_refusal");
    run_isolated(test_local_position_unwraps_across_32_bits, "test_local_position_unwraps_across_32_bits");
    run_isolated(test_local_stop_discards_queue, "test_local_stop_discards_queue");
    run_isolated(test_shutdown_hook_stops_every_slot, "test_shutdown_hook_stops_every_slot");
    run_isolated(test_local_two_slots_step_independently, "test_local_two_slots_step_independently");
    run_isolated(test_local_one_rising_edge_per_step, "test_local_one_rising_edge_per_step");
    run_isolated(test_local_dir_polarity_both_lanes, "test_local_dir_polarity_both_lanes");
    run_isolated(test_local_step_polarity_active_low, "test_local_step_polarity_active_low");
#ifdef KM_H723_IMAGE_AUTOCONF
    run_isolated(test_saved_boxturtle_h723_four_lane_gpio_edges, "test_saved_boxturtle_h723_four_lane_gpio_edges");
#endif

    printf("============================================================\n");
#ifdef KM_H723_IMAGE_AUTOCONF
    printf("  All 39 scenarios passed successfully!\n");
#else
    printf("  All 38 scenarios passed successfully!\n");
#endif
    printf("============================================================\n");
    return 0;
}
