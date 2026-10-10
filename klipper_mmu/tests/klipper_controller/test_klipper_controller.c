// SPDX-License-Identifier: GPL-3.0-or-later
#include "controller.h"
#include "servo_pwm.h"
#include "stepdir_gpio.h"
#include "clock.h"
#include "commands.h"
#include "shim_klipper.h"
#include "board/irq.h"
#include "board/misc.h"
#include "board/gpio.h"
#include "stepper.h"
#include "klipper_mmu/operation_core.h"

#include "klipper_mmu/trajectory.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <assert.h>
#include <unistd.h>
#include <sys/wait.h>
#include <setjmp.h>
#include <stdarg.h>

/* mcu/switch_gpio.c is not linked here: this test drives the controller and
 * command layers directly. commands.c's diagnostic capture (docs/diagnostics.md)
 * still calls this accessor unconditionally, so stand in with no switch bound. */
void km_switch_gpio_get_bounce(uint16_t bay[KM_MAX_LANES], uint16_t load[KM_MAX_LANES],
                               uint16_t *hub, uint16_t *toolhead)
{
    for (unsigned i = 0; i < KM_MAX_LANES; ++i) bay[i] = load[i] = 0;
    *hub = *toolhead = 0;
}
void km_switch_gpio_get_selector_bounce(uint16_t *selector) { *selector = 0; }

static bool switch_reply_seen;
static uint32_t switch_reply[7];
void __wrap_command_sendf(const char *fmt, ...)
{
    if (strcmp(fmt, "km_switch_delay_state challenge=%u instance_hi=%u instance_lo=%u valid=%c delay_us=%u count=%u max_us=%u"))
        return;
    va_list args;
    va_start(args, fmt);
    for (unsigned i = 0; i < 7; ++i) switch_reply[i] = va_arg(args, uint32_t);
    va_end(args);
    switch_reply_seen = true;
}

/* =========================================================================
 * Small strict test-local scheduler adapter/wrapper
 *
 * Catches duplicate timer insertions, self-removal/insertion of executing
 * timer callbacks, past scheduling, and unlocked timer mutations.
 * ========================================================================= */
#define STRICT_MAX_TIMERS 16
static struct timer *strict_active_timers[STRICT_MAX_TIMERS];
static int strict_num_active_timers = 0;
static struct timer *strict_executing_timer = NULL;

extern void __real_sched_add_timer(struct timer *t);
extern void __real_sched_del_timer(struct timer *t);

void __wrap_sched_add_timer(struct timer *t)
{
    assert(test_shim_irq_is_disabled());
    assert(t != strict_executing_timer);
    assert(!timer_is_before(t->waketime, timer_read_time()));
    for (int i = 0; i < strict_num_active_timers; i++) {
        assert(strict_active_timers[i] != t);
    }
    assert(strict_num_active_timers < STRICT_MAX_TIMERS);
    strict_active_timers[strict_num_active_timers++] = t;
    __real_sched_add_timer(t);
}

void __wrap_sched_del_timer(struct timer *del)
{
    assert(test_shim_irq_is_disabled());
    assert(del != strict_executing_timer);
    for (int i = 0; i < strict_num_active_timers; i++) {
        if (strict_active_timers[i] == del) {
            for (int j = i; j < strict_num_active_timers - 1; j++)
                strict_active_timers[j] = strict_active_timers[j + 1];
            strict_num_active_timers--;
            break;
        }
    }
    __real_sched_del_timer(del);
}

static void strict_timer_reset(void)
{
    strict_num_active_timers = 0;
    strict_executing_timer = NULL;
}
DECL_SHUTDOWN(strict_timer_reset);

/* =========================================================================
 * Test fixtures and helpers
 * ========================================================================= */
static void emergency_stop(const char *reason)
{
    test_shim_use_longjmp = true;
    if (setjmp(test_shim_shutdown_jump) == 0)
        shutdown(reason);
    test_shim_use_longjmp = false;
}

static bool dummy_ready_true(void *ctx)
{
    (void)ctx;
    return true;
}

static km_fault_t dyn_driver_fault = KM_FAULT_NONE;
static km_fault_t dyn_fault_query(void *ctx)
{
    (void)ctx;
    return dyn_driver_fault;
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

static void init_and_ready_clock(uint32_t freq_hz)
{
    shim_reset(freq_hz, 1u, 1u, 4u);
    strict_timer_reset();
    shim_set_ticks(10000u);
    shim_run_init();
    shim_set_board_ready(true);
    shim_run_task();
    assert(km_clock_get_state() == KM_CLOCK_STATE_READY);
}

/* One timer interrupt, then the main loop's tasks, as Klipper runs them. */
static uint_fast8_t dispatch_timer_only(void)
{
    struct timer *t = shim_find_earliest_timer();
    if (!t)
        return SF_DONE;
    if (timer_is_before(test_shim_ticks, t->waketime))
        test_shim_ticks = t->waketime;
    irq_disable();
    assert(strict_executing_timer == NULL);
    strict_executing_timer = t;
    uint_fast8_t res = shim_dispatch_timer(t);
    strict_executing_timer = NULL;
    if (res == SF_DONE) {
        for (int i = 0; i < strict_num_active_timers; i++) {
            if (strict_active_timers[i] == t) {
                for (int j = i; j < strict_num_active_timers - 1; j++)
                    strict_active_timers[j] = strict_active_timers[j + 1];
                strict_num_active_timers--;
                break;
            }
        }
    }
    irq_enable();
    return res;
}

static uint_fast8_t dispatch_earliest_timer_irq(void)
{
    uint_fast8_t res = dispatch_timer_only();
    shim_run_task();
    return res;
}

/* Dispatch until the control task completes a cycle (km_core_update). */
static void run_control_cycle(km_operation_core_t *c)
{
    uint32_t before = c->last_control_us;
    bool valid = c->control_clock_valid;
    /* A control interval can span more STEP deadline dispatches than the old
     * planner produced, so allow a few more than 8 before declaring starvation. */
    for (unsigned i = 0; i < 32u && c->control_clock_valid == valid
         && c->last_control_us == before; ++i)
        dispatch_earliest_timer_irq();
    assert(c->control_clock_valid && c->last_control_us != before);
}

static void provide_baseline_sensors(km_operation_core_t *c, uint32_t ms)
{
    km_core_set_sensors_at(c, 0u, true, false, false, true, ms);
    (void)km_buffer_update_switches(&c->buffer_sensor, false, false, ms);
}

/* Fresh sensor callback: always samples fresh acquisition timestamp from clock */
static void default_sensor_sample_cb(km_operation_core_t *c, void *ctx)
{
    (void)ctx;
    km_core_timepoint_t acquired;
    assert(km_clock_get_time(&acquired) == KM_OK);
    provide_baseline_sensors(c, acquired.ms);
}

/* =========================================================================
 * 1. No boot output or attachment
 * ========================================================================= */
static void test_no_boot_output_or_attachment(void)
{
    shim_reset(520000000u, 1u, 1u, 4u);
    strict_timer_reset();
    shim_run_init();

    assert(!km_controller_is_attached());
    assert(!km_controller_is_shutdown());
    assert(shim_active_timer_count() == 0);
    assert(shim_gpio_events_count == 0u);
}

/* =========================================================================
 * 2. Pre-ready clock rejection
 * ========================================================================= */
static void test_pre_ready_clock_rejection(void)
{
    shim_reset(520000000u, 1u, 1u, 4u);
    strict_timer_reset();
    shim_run_init();
    assert(km_clock_get_state() == KM_CLOCK_STATE_UNAVAILABLE);

    km_device_profile_t p;
    km_profile_init_nightowl(&p);
    km_operation_core_t core;
    assert(km_core_init(&core, &p) == KM_OK);

    km_result_t res = km_controller_attach(&core, NULL, NULL);
    assert(res == KM_ERR_UNAVAILABLE);
    assert(!km_controller_is_attached());
    assert(shim_active_timer_count() == 0);
    assert(shim_gpio_events_count == 0u);
}

/* =========================================================================
 * 3. Rejection of invalid profile before taking ownership or timer
 *    (Deliberate invalid-input fixture)
 * ========================================================================= */
static void test_attach_reject_invalid_profile(void)
{
    init_and_ready_clock(520000000u);
    km_operation_core_t core = {0};
    core.profile.lane_count = 4u;
    assert(km_profile_validate(&core.profile) == KM_ERR_INVALID_PARAM);

    int timers_before = shim_active_timer_count();
    uint32_t events_before = shim_gpio_events_count;

    km_result_t result = km_controller_attach(&core, NULL, NULL);
    assert(result == KM_ERR_INVALID_PARAM);
    assert(!km_controller_is_attached());
    assert(shim_active_timer_count() == timers_before);
    assert(shim_gpio_events_count == events_before);
}

/* =========================================================================
 * 4. Rejection of dirty or faulted core before taking ownership
 *    (Deliberate invalid-input fixture)
 * ========================================================================= */
static void test_attach_reject_dirty_or_faulted_core(void)
{
    init_and_ready_clock(520000000u);

    km_device_profile_t p;
    km_profile_init_nightowl(&p);
    km_operation_core_t core;
    assert(km_core_init(&core, &p) == KM_OK);

    /* Case A: Core recovered to LOADED (reproduces iteration-30 review defect) */
    irqstatus_t lock = irq_save();
    km_core_timepoint_t now;
    assert(km_clock_get_time(&now) == KM_OK);
    km_core_host_comm_ready(&core, now.ms);
    assert(km_core_recover(&core, 0, true, 0u, now) == KM_OK);
    irq_restore(lock);
    assert(core.op_state == KM_OP_STATE_LOADED);

    km_result_t result = km_controller_attach(&core, NULL, NULL);
    assert(result == KM_ERR_INVALID_PARAM);
    assert(!km_controller_is_attached());

    /* Case B: Re-init clean core, test other dirty/faulted conditions */
    assert(km_core_init(&core, &p) == KM_OK);

    /* NULL core */
    assert(km_controller_attach(NULL, NULL, NULL) == KM_ERR_INVALID_PARAM);

    /* IRQs disabled */
    irq_disable();
    assert(km_controller_attach(&core, NULL, NULL) == KM_ERR_INVALID_PARAM);
    irq_enable();

    /* Core in fault */
    core.op_state = KM_OP_STATE_FAULT;
    assert(km_controller_attach(&core, NULL, NULL) == KM_ERR_INVALID_PARAM);
    core.op_state = KM_OP_STATE_IDLE;

    /* Core system fault */
    core.system_fault = KM_FAULT_DRIVER_FAULT;
    assert(km_controller_attach(&core, NULL, NULL) == KM_ERR_INVALID_PARAM);
    core.system_fault = KM_FAULT_NONE;

    /* Core motion fault */
    core.motion_fault = KM_FAULT_DRIVER_FAULT;
    assert(km_controller_attach(&core, NULL, NULL) == KM_ERR_INVALID_PARAM);
    core.motion_fault = KM_FAULT_NONE;

    /* Core stage not stopped */
    core.stage = KM_STAGE_LOAD_FEED_TO_HUB;
    assert(km_controller_attach(&core, NULL, NULL) == KM_ERR_INVALID_PARAM);
    core.stage = KM_STAGE_STOPPED;

    /* Core shared path owned */
    core.shared_path_owned = true;
    assert(km_controller_attach(&core, NULL, NULL) == KM_ERR_INVALID_PARAM);
    core.shared_path_owned = false;

    /* Terminal pending */
    core.terminal_pending = true;
    assert(km_controller_attach(&core, NULL, NULL) == KM_ERR_INVALID_PARAM);
    core.terminal_pending = false;

    /* Trajectory active */
    core.trajectory.is_active = true;
    assert(km_controller_attach(&core, NULL, NULL) == KM_ERR_INVALID_PARAM);
    core.trajectory.is_active = false;

    /* Trajectory faulted */
    core.trajectory.faulted = true;
    assert(km_controller_attach(&core, NULL, NULL) == KM_ERR_INVALID_PARAM);
    core.trajectory.faulted = false;

    /* Trajectory position uncertain */
    core.trajectory.position_uncertain = true;
    assert(km_controller_attach(&core, NULL, NULL) == KM_ERR_INVALID_PARAM);
    core.trajectory.position_uncertain = false;


    assert(!km_controller_is_attached());
    assert(shim_active_timer_count() == 1); /* clock maintenance timer only */
}

/* =========================================================================
 * 5. Rejection of bound driver enabled, step high, PWM, or faulted
 *    (Deliberate invalid-input fixture)
 * ========================================================================= */
static void test_attach_reject_dirty_bound_driver(void)
{
    init_and_ready_clock(520000000u);

    km_device_profile_t p;
    km_profile_init_nightowl(&p);
    km_operation_core_t core;
    assert(km_core_init(&core, &p) == KM_OK);

    km_step_dir_config_t cfg = default_stepdir_config();
    assert(km_stepdir_gpio_setup(0u, 10u, 11u, 12u, &cfg,
                                dummy_ready_true, dyn_fault_query, NULL) == KM_OK);
    km_driver_t *driver = km_stepdir_gpio_get_driver(0u);
    assert(driver != NULL);
    
km_result_t res_bind = km_core_bind_lane_driver(&core, 0u, driver, NULL);
if (res_bind != KM_OK) {
    printf("DEBUG_BIND: %d\n", res_bind);
}
assert(res_bind == KM_OK);


    /* Case A: Driver enabled before attachment (reproduces iteration-30 review defect) */
    irqstatus_t lock = irq_save();
    assert(km_driver_set_enable(driver, true) == KM_OK);
    irq_restore(lock);
    assert(driver->enabled && shim_gpio_current_level[12u] == 1u);

    km_result_t res = km_controller_attach(&core, NULL, NULL);
    assert(res == KM_ERR_INVALID_PARAM);
    /* Output MUST NOT be altered by rejected attachment */
    assert(driver->enabled && shim_gpio_current_level[12u] == 1u);
    assert(!km_controller_is_attached());
    assert(shim_active_timer_count() == 1);

    /* Clean up driver enable */
    lock = irq_save();
    assert(km_driver_set_enable(driver, false) == KM_OK);
    irq_restore(lock);
    assert(!driver->enabled && shim_gpio_current_level[12u] == 0u);

    /* Case B: Driver step pin high */
    driver->step_pin_high = true;
    assert(km_controller_attach(&core, NULL, NULL) == KM_ERR_INVALID_PARAM);
    driver->step_pin_high = false;

    /* Case C: Driver PWM active */
    driver->pwm_duty_permille = 500u;
    assert(km_controller_attach(&core, NULL, NULL) == KM_ERR_INVALID_PARAM);
    driver->pwm_duty_permille = 0u;

    /* Case D: Driver reporting hardware fault */
    dyn_driver_fault = KM_FAULT_DRIVER_FAULT;
    assert(km_controller_attach(&core, NULL, NULL) == KM_ERR_INVALID_PARAM);
    dyn_driver_fault = KM_FAULT_NONE;

    /* Clean idle driver now attaches and works */
    assert(km_controller_attach(&core, NULL, NULL) == KM_OK);
    assert(km_controller_is_attached());
    assert(shim_active_timer_count() == 2);
}

/* =========================================================================
 * 6. Repeated attachment rejection
 * ========================================================================= */
static void test_repeated_attachment_rejection(void)
{
    init_and_ready_clock(520000000u);

    km_device_profile_t p;
    km_profile_init_nightowl(&p);
    km_operation_core_t core1, core2;
    assert(km_core_init(&core1, &p) == KM_OK);
    assert(km_core_init(&core2, &p) == KM_OK);

    /* First attachment succeeds */
    assert(km_controller_attach(&core1, NULL, NULL) == KM_OK);
    assert(km_controller_is_attached());
    assert(shim_active_timer_count() == 2);

    /* Repeated attachment returns KM_ERR_BUSY and does not change attachment */
    assert(km_controller_attach(&core2, NULL, NULL) == KM_ERR_BUSY);
    assert(km_controller_attach(&core1, NULL, NULL) == KM_ERR_BUSY);
    assert(km_controller_is_attached());
    assert(shim_active_timer_count() == 2);
}

/* =========================================================================
 * 7. Independent local periodic service
 * ========================================================================= */
static uint32_t sensor_sample_calls = 0u;
static void sensor_sample_cb(km_operation_core_t *c, void *ctx)
{
    (void)ctx;
    ++sensor_sample_calls;
    km_core_timepoint_t acquired;
    assert(km_clock_get_time(&acquired) == KM_OK);
    provide_baseline_sensors(c, acquired.ms);
}

static void test_independent_local_periodic_service(void)
{
    sensor_sample_calls = 0u;
    init_and_ready_clock(520000000u);

    km_device_profile_t p;
    km_profile_init_nightowl(&p);
    km_operation_core_t core;
    assert(km_core_init(&core, &p) == KM_OK);

    assert(km_controller_attach(&core, sensor_sample_cb, NULL) == KM_OK);

    for (int i = 0; i < 5; ++i) {
        dispatch_earliest_timer_irq();
        dispatch_earliest_timer_irq();
    }

    assert(sensor_sample_calls >= 5u);
    assert(core.last_control_ms >= 5u);
    assert(km_controller_is_attached());
    assert(!km_controller_is_shutdown());
    assert(core.op_state == KM_OP_STATE_IDLE);
}

/* =========================================================================
 * 8. Real positive motion from a core load request
 * ========================================================================= */
static void test_real_positive_motion_core_load(void)
{
    init_and_ready_clock(520000000u);

    km_device_profile_t p;
    km_profile_init_nightowl(&p);
    km_operation_core_t core;
    assert(km_core_init(&core, &p) == KM_OK);

    km_step_dir_config_t cfg = default_stepdir_config();
    assert(km_stepdir_gpio_setup(0u, 10u, 11u, 12u, &cfg, dummy_ready_true, NULL, NULL) == KM_OK);
    km_driver_t *feeder = km_stepdir_gpio_get_driver(0u);
    assert(feeder != NULL);

    assert(km_core_bind_lane_driver(&core, 0u, feeder, NULL) == KM_OK);
    assert(km_controller_attach(&core, default_sensor_sample_cb, NULL) == KM_OK);
    run_control_cycle(&core); /* A completed control cycle leases motion. */
    assert(km_commands_attach(&core, 0x1234ULL, km_controller_changed, true) == KM_OK);

    km_controller_changed(); /* Task context: collect the edge output first. */
    km_core_timepoint_t now;
    assert(km_clock_get_time(&now) == KM_OK);
    km_core_host_comm_ready(&core, now.ms);
    provide_baseline_sensors(&core, now.ms);

    km_request_identity_t req = {
        .client = KM_CLIENT_HOST,
        .session_id = 1u,
        .req_id = 100u,
        .op_type = KM_OP_TYPE_LOAD,
        .lane_idx = 0u
    };
    km_admission_result_t adm = km_core_request_start(&core, &req, KM_DIR_FORWARD, now);
    assert(adm == KM_REQ_ACCEPTED);
    km_controller_changed();

    assert(core.stage == KM_STAGE_LOAD_FEED_TO_HUB);
    assert(feeder->enabled);

    shim_gpio_clear_events();

    /* Real stepper.c fires a separate timer event per edge (rise and fall),
     * plus the controller's own rate tick and the clock's maintenance timer,
     * so reaching 10 steps now takes many more dispatches than one edge per
     * step did. */
    for (int i = 0; i < 4000; ++i) {
        dispatch_earliest_timer_irq();
        if (core.trajectory.current_position_steps >= 10)
            break;
    }

    assert(core.trajectory.current_position_steps >= 10);
    assert(shim_gpio_events_count > 0u);
    assert(shim_gpio_write_count[10u] >= 20u);
}

/* A selector machine: the shared trajectory switches from the gear (slot 0)
 * to the selector stepper (slot 1) for the selection phase, and the real
 * controller must step the selector's own STEP pin. The endstop is pressed at
 * power-up, so PRELOAD first backs the selector off it. */
static bool selector_endstop_pressed = true;
static void selector_sensor_cb(km_operation_core_t *c, void *ctx)
{
    (void)ctx;
    km_core_timepoint_t acquired;
    assert(km_clock_get_time(&acquired) == KM_OK);
    provide_baseline_sensors(c, acquired.ms);
    /* The carriage leaves the switch after its first selector steps. */
    if (shim_gpio_write_count[20u] >= 4u) selector_endstop_pressed = false;
    km_core_set_selector_endstop_at(c, selector_endstop_pressed, true, acquired.us);
}

static void test_selector_move_steps_the_selector_axis(void)
{
    init_and_ready_clock(520000000u);
    km_device_profile_t p;
    km_profile_init_nightowl(&p);
    p.lane_count = 2u;
    p.has_selector = true;
    p.selector = (km_selector_profile_t){
        .kind = KM_SELECTOR_LINEAR,
        /* ERCF 2.0's selector: 80 steps/mm, 60/200 mm/s, 1200 mm/s^2. */
        .home = {.speed_steps_s = 4800u, .acceleration_steps_s2 = 96000u,
                 .deceleration_steps_s2 = 96000u, .max_travel_steps = 15000u,
                 .deadline_us = 5000000u, .max_switch_age_us = 20000u,
                 .home_direction = KM_DIR_REVERSE},
        .gate_steps = {320, 2160},
        .velocity_steps_s = 16000u, .acceleration_steps_s2 = 96000u,
        .has_servo = true, .servo_engage_us = 1556u, .servo_release_us = 1167u,
        .servo_move_us = 1278u, .servo_dwell_ms = 500u,
    };
    km_operation_core_t core;
    assert(km_core_init(&core, &p) == KM_OK);

    km_step_dir_config_t cfg = default_stepdir_config();
    cfg.pulse_high_us = cfg.pulse_low_us = 3u;
    cfg.maximum_velocity_steps_s = 72727u;
    assert(km_stepdir_gpio_setup(0u, 10u, 11u, 12u, &cfg, dummy_ready_true, NULL, NULL) == KM_OK);
    cfg.maximum_velocity_steps_s = 16000u;
    assert(km_stepdir_gpio_setup(1u, 20u, 21u, 22u, &cfg, dummy_ready_true, NULL, NULL) == KM_OK);
    km_driver_t *gear = km_stepdir_gpio_get_driver(0u);
    km_driver_t *selector = km_stepdir_gpio_get_driver(1u);
    assert(gear && selector);
    assert(km_core_bind_lane_driver(&core, 0u, gear, NULL) == KM_OK);
    assert(km_core_bind_lane_driver(&core, 1u, gear, NULL) == KM_OK);
    km_servo_t *servo = NULL;
    assert(km_servo_pwm_setup(30u, &servo) == KM_OK);
    assert(km_core_bind_selector(&core, selector, servo) == KM_OK);
    assert(km_controller_attach(&core, selector_sensor_cb, NULL) == KM_OK);
    run_control_cycle(&core);
    assert(km_commands_attach(&core, 0x1234ULL, km_controller_changed, true) == KM_OK);
    km_controller_changed();
    km_core_timepoint_t now;
    assert(km_clock_get_time(&now) == KM_OK);
    km_core_host_comm_ready(&core, now.ms);
    shim_gpio_clear_events();

    km_request_identity_t req = {KM_CLIENT_HOST, 1u, 100u, KM_OP_TYPE_PRELOAD, 0u};
    assert(km_core_request_start(&core, &req, KM_DIR_FORWARD, now) == KM_REQ_ACCEPTED);
    km_controller_changed();
    assert(core.stage == KM_STAGE_SELECT_MOVE && core.select_backoff);
    for (int i = 0; i < 20000 && core.op_state != KM_OP_STATE_FAULT
         && core.stage != KM_STAGE_SELECT_HOME; ++i)
        dispatch_earliest_timer_irq();
    assert(core.op_state != KM_OP_STATE_FAULT);
    assert(shim_gpio_write_count[20u] >= 4u); /* The selector's STEP pin moved. */
    assert(shim_gpio_write_count[10u] == 0u); /* The gear's did not. */
}

/* =========================================================================
 * 9. Load-to-follow progression
 * ========================================================================= */
static uint8_t sim_sensor_stage = 0u;
static void follow_progression_sensor_cb(km_operation_core_t *c, void *ctx)
{
    (void)ctx;
    km_core_timepoint_t acquired;
    assert(km_clock_get_time(&acquired) == KM_OK);
    if (sim_sensor_stage == 0) {
        km_core_set_sensors_at(c, 0u, true, false, false, true, acquired.ms);
        (void)km_buffer_update_switches(&c->buffer_sensor, false, false, acquired.ms);
        if (c->trajectory.current_position_steps >= 5) {
            sim_sensor_stage = 1;
        }
    } else if (sim_sensor_stage == 1) {
        km_core_set_sensors_at(c, 0u, true, true, false, true, acquired.ms);
        (void)km_buffer_update_switches(&c->buffer_sensor, false, false, acquired.ms);
        if (c->stage == KM_STAGE_LOAD_FEED_TO_BUFFER && c->trajectory.current_position_steps >= 10) {
            sim_sensor_stage = 2;
        }
    } else if (sim_sensor_stage == 2) {
        km_core_set_sensors_at(c, 0u, true, true, true, true, acquired.ms);
        (void)km_buffer_update_switches(&c->buffer_sensor, false, false, acquired.ms);
    }
}

static void test_load_to_follow_progression(void)
{
    sim_sensor_stage = 0u;
    init_and_ready_clock(520000000u);

    km_device_profile_t p;
    km_profile_init_nightowl(&p);
    km_operation_core_t core;
    assert(km_core_init(&core, &p) == KM_OK);

    km_step_dir_config_t cfg = default_stepdir_config();
    assert(km_stepdir_gpio_setup(0u, 10u, 11u, 12u, &cfg, dummy_ready_true, NULL, NULL) == KM_OK);
    km_driver_t *feeder = km_stepdir_gpio_get_driver(0u);
    assert(km_core_bind_lane_driver(&core, 0u, feeder, NULL) == KM_OK);
    assert(km_controller_attach(&core, follow_progression_sensor_cb, NULL) == KM_OK);
    run_control_cycle(&core); /* A completed control cycle leases motion. */
    assert(km_commands_attach(&core, 0x1234ULL, km_controller_changed, true) == KM_OK);

    km_controller_changed(); /* Task context: collect the edge output first. */
    km_core_timepoint_t now;
    assert(km_clock_get_time(&now) == KM_OK);
    km_core_host_comm_ready(&core, now.ms);
    provide_baseline_sensors(&core, now.ms);

    km_request_identity_t req = {
        .client = KM_CLIENT_HOST, .session_id = 1u, .req_id = 101u,
        .op_type = KM_OP_TYPE_LOAD, .lane_idx = 0u
    };
    assert(km_core_request_start(&core, &req, KM_DIR_FORWARD, now) == KM_REQ_ACCEPTED);
    km_controller_changed();

    for (int i = 0; i < 400; ++i) {
        dispatch_earliest_timer_irq();
        if (core.stage == KM_STAGE_FOLLOW)
            break;
    }

    assert(core.stage == KM_STAGE_FOLLOW);
    assert(core.op_state == KM_OP_STATE_FOLLOWING);

    km_event_t head;
    assert(km_event_queue_peek(&core.event_queue, &head) == KM_OK);
    assert(head.type == KM_EVENT_LOAD_COMPLETE);
    assert(head.result_code == KM_WIRE_OP_CODE_SUCCESS);
}

/* =========================================================================
 * 10. Actual velocity retarget through accepted public API
 * ========================================================================= */
static void test_velocity_retarget_and_cancellation(void)
{
    init_and_ready_clock(520000000u);

    km_device_profile_t p;
    km_profile_init_nightowl(&p);
    km_operation_core_t core;
    assert(km_core_init(&core, &p) == KM_OK);

    km_step_dir_config_t cfg = default_stepdir_config();
    assert(km_stepdir_gpio_setup(0u, 10u, 11u, 12u, &cfg, dummy_ready_true, NULL, NULL) == KM_OK);
    km_driver_t *feeder = km_stepdir_gpio_get_driver(0u);
    assert(km_core_bind_lane_driver(&core, 0u, feeder, NULL) == KM_OK);
    assert(km_controller_attach(&core, default_sensor_sample_cb, NULL) == KM_OK);
    run_control_cycle(&core); /* A completed control cycle leases motion. */
    assert(km_commands_attach(&core, 0x1234ULL, km_controller_changed, true) == KM_OK);

    km_controller_changed(); /* Task context: collect the edge output first. */
    km_core_timepoint_t now;
    assert(km_clock_get_time(&now) == KM_OK);
    km_core_host_comm_ready(&core, now.ms);
    provide_baseline_sensors(&core, now.ms);

    km_request_identity_t req = {
        .client = KM_CLIENT_HOST, .session_id = 1u, .req_id = 102u,
        .op_type = KM_OP_TYPE_LOAD, .lane_idx = 0u
    };
    assert(km_core_request_start(&core, &req, KM_DIR_FORWARD, now) == KM_REQ_ACCEPTED);
    km_controller_changed();

    assert(shim_active_timer_count() == 2);

    for (int i = 0; i < 200; ++i) {
        dispatch_earliest_timer_irq();
        if (core.trajectory.current_position_steps >= 5) break;
    }
    assert(core.trajectory.current_position_steps >= 5);

    /* Retarget velocity to 2500 steps/s using accepted public trajectory API under IRQ lock */
    km_controller_changed(); /* Task context: collect the edge output first. */
    assert(km_clock_get_time(&now) == KM_OK);
    assert(km_trajectory_set_target_velocity(&core.trajectory, 2500, now.us) == KM_OK);
    km_controller_changed();

    /* Motion is already under way (the position >= 5 loop above), so the
     * real stepper's own timer may or may not be registered at this exact
     * instant (it drains between microsteps at low speed); just rule out a
     * timer leaked by the retarget itself. */
    assert(shim_active_timer_count() >= 2 && shim_active_timer_count() <= 3);

    for (int i = 0; i < 200; ++i) {
        dispatch_earliest_timer_irq();
        if (core.trajectory.current_position_steps >= 12) break;
    }
    assert(core.trajectory.current_position_steps >= 12);

    /* Cancel target atomically under IRQ lock */
    km_controller_changed(); /* Task context: collect the edge output first. */
    assert(km_clock_get_time(&now) == KM_OK);
    assert(km_core_request_cancel_target(&core, &req, now) == KM_REQ_ACCEPTED);
    km_controller_changed();

    /* Stop from 2500 steps/s at D=12000, J=160000 takes v/D + D/J = 0.28 s and
     * ~520 jerk-limited braking steps, so allow far more timer dispatches. */
    for (int i = 0; i < 8000; ++i) {
        dispatch_earliest_timer_irq();
        if (core.stage == KM_STAGE_STOPPED) break;
    }

    assert(core.stage == KM_STAGE_STOPPED);
    assert(core.op_state == KM_OP_STATE_IDLE);
    assert(!km_trajectory_is_moving(&core.trajectory));
    assert(!feeder->enabled);

    km_event_t head;
    assert(km_event_queue_peek(&core.event_queue, &head) == KM_OK);
    assert(head.type == KM_EVENT_LOAD_CANCELLED);
}

/* =========================================================================
 * 11. Cancellation during active high STEP through stopped settlement
 *     with exact raw rising-edge accounting and one terminal event
 * ========================================================================= */
static void test_cancellation_during_active_high_step(void)
{
    init_and_ready_clock(520000000u);

    km_device_profile_t p;
    km_profile_init_nightowl(&p);
    km_operation_core_t core;
    assert(km_core_init(&core, &p) == KM_OK);

    km_step_dir_config_t cfg = default_stepdir_config();
    cfg.step_active_high = true;
    cfg.enable_active_high = true;
    assert(km_stepdir_gpio_setup(0u, 10u, 11u, 12u, &cfg, dummy_ready_true, NULL, NULL) == KM_OK);
    km_driver_t *driver = km_stepdir_gpio_get_driver(0u);
    
km_result_t res_bind = km_core_bind_lane_driver(&core, 0u, driver, NULL);
if (res_bind != KM_OK) {
    printf("DEBUG_BIND: %d\n", res_bind);
}
assert(res_bind == KM_OK);

    assert(km_controller_attach(&core, default_sensor_sample_cb, NULL) == KM_OK);
    run_control_cycle(&core); /* A completed control cycle leases motion. */
    assert(km_commands_attach(&core, 0x1234ULL, km_controller_changed, true) == KM_OK);

    km_controller_changed(); /* Task context: collect the edge output first. */
    km_core_timepoint_t now;
    assert(km_clock_get_time(&now) == KM_OK);
    km_core_host_comm_ready(&core, now.ms);
    provide_baseline_sensors(&core, now.ms);

    km_request_identity_t req = {
        .client = KM_CLIENT_HOST, .session_id = 1u, .req_id = 103u,
        .op_type = KM_OP_TYPE_LOAD, .lane_idx = 0u
    };
    assert(km_core_request_start(&core, &req, KM_DIR_FORWARD, now) == KM_REQ_ACCEPTED);
    km_controller_changed();

    /* Run until during a physical high pulse. Real motion steps through
     * stepper_local, which never calls the driver ops ;
     * driver->step_pin_high only reflects that generic wrapper's bookkeeping,
     * so the mid-pulse pin state is now the simulated GPIO level itself. */
    unsigned calls = 0u;
    while (core.trajectory.current_position_steps < 10 || shim_gpio_current_level[10u] != 1u) {
        assert(++calls < 10000u);
        dispatch_earliest_timer_irq();
        assert(core.op_state != KM_OP_STATE_FAULT);
    }
    assert(shim_gpio_current_level[10u] == 1u);

    /* Cancel during a physical high pulse, atomically under IRQ lock with rescheduling */
    km_controller_changed(); /* Task context: collect the edge output first. */
    assert(km_clock_get_time(&now) == KM_OK);
    assert(km_core_request_cancel_target(&core, &req, now) == KM_REQ_ACCEPTED);
    km_controller_changed();
    km_controller_changed(); /* duplicate call test */

    while (core.stage != KM_STAGE_STOPPED) {
        assert(++calls < 20000u);
        dispatch_earliest_timer_irq();
        assert(core.op_state != KM_OP_STATE_FAULT);
    }

    assert(!driver->enabled && !driver->step_pin_high && shim_gpio_current_level[10u] == 0u);
    assert(shim_active_timer_count() == 2);

    /* Verify physical high/low minima and exact raw rising-edge accounting */
    uint32_t previous = 0u, previous_level = 0u, rising = 0u;
    bool have_edge = false;
    for (uint32_t i = 0u; i < shim_gpio_events_count; ++i) {
        const shim_gpio_event_t *e = &shim_gpio_events[i];
        if (e->pin != 10u) continue;
        if (e->val != previous_level) {
            if (have_edge) assert(e->tick - previous >= timer_from_us(10u));
            if (e->val) ++rising;
            previous = e->tick;
            previous_level = e->val;
            have_edge = true;
        }
    }
    assert(rising >= 10u);
    assert(core.trajectory.current_position_steps == (int64_t)rising);

    /* Verify exactly one terminal event */
    km_event_t event;
    assert(km_event_queue_peek(&core.event_queue, &event) == KM_OK);
    assert(event.type == KM_EVENT_LOAD_CANCELLED);
    assert(core.event_queue.count == 1u);
}

/* =========================================================================
 * 12. Local host lease expiry without any later host call
 * ========================================================================= */
static void test_local_host_lease_expiry_without_host_call(void)
{
    init_and_ready_clock(520000000u);

    km_device_profile_t p;
    km_profile_init_nightowl(&p);
    p.host_watchdog_ms = 50u;
    km_operation_core_t core;
    assert(km_core_init(&core, &p) == KM_OK);

    km_step_dir_config_t cfg = default_stepdir_config();
    assert(km_stepdir_gpio_setup(0u, 10u, 11u, 12u, &cfg, dummy_ready_true, NULL, NULL) == KM_OK);
    km_driver_t *feeder = km_stepdir_gpio_get_driver(0u);
    assert(km_core_bind_lane_driver(&core, 0u, feeder, NULL) == KM_OK);
    assert(km_controller_attach(&core, default_sensor_sample_cb, NULL) == KM_OK);
    run_control_cycle(&core); /* A completed control cycle leases motion. */
    assert(km_commands_attach(&core, 0x1234ULL, km_controller_changed, true) == KM_OK);

    km_controller_changed(); /* Task context: collect the edge output first. */
    km_core_timepoint_t now;
    assert(km_clock_get_time(&now) == KM_OK);
    km_core_host_comm_ready(&core, now.ms);
    provide_baseline_sensors(&core, now.ms);

    km_request_identity_t req = {
        .client = KM_CLIENT_HOST, .session_id = 1u, .req_id = 104u,
        .op_type = KM_OP_TYPE_LOAD, .lane_idx = 0u
    };
    assert(km_core_request_start(&core, &req, KM_DIR_FORWARD, now) == KM_REQ_ACCEPTED);
    km_controller_changed();

    for (int i = 0; i < 150; ++i) {
        dispatch_earliest_timer_irq();
        if (core.system_fault == KM_FAULT_HOST_COMM_LOST) break;
    }

    assert(core.system_fault == KM_FAULT_HOST_COMM_LOST);
    assert(core.op_state == KM_OP_STATE_FAULT);
    assert(!feeder->enabled);
}

/* =========================================================================
 * 13. Stale or invalid sensor failure
 *     (Deliberate invalid-input fixture: retains old frozen timestamp)
 * ========================================================================= */
static bool stale_flag = false;
static uint32_t stale_frozen_ms = 0u;
static void stale_sensor_sample_cb(km_operation_core_t *c, void *ctx)
{
    (void)ctx;
    if (!stale_flag) {
        km_core_timepoint_t acquired;
        assert(km_clock_get_time(&acquired) == KM_OK);
        stale_frozen_ms = acquired.ms;
        provide_baseline_sensors(c, acquired.ms);
    } else {
        /* Deliberately retain the old acquisition timestamp */
        provide_baseline_sensors(c, stale_frozen_ms);
    }
}

static void test_stale_or_invalid_sensor_failure(void)
{
    stale_flag = false;
    init_and_ready_clock(520000000u);

    km_device_profile_t p;
    km_profile_init_nightowl(&p);
    p.max_sensor_age_ms = 30u;
    km_operation_core_t core;
    assert(km_core_init(&core, &p) == KM_OK);

    km_step_dir_config_t cfg = default_stepdir_config();
    assert(km_stepdir_gpio_setup(0u, 10u, 11u, 12u, &cfg, dummy_ready_true, NULL, NULL) == KM_OK);
    km_driver_t *feeder = km_stepdir_gpio_get_driver(0u);
    assert(km_core_bind_lane_driver(&core, 0u, feeder, NULL) == KM_OK);
    assert(km_controller_attach(&core, stale_sensor_sample_cb, NULL) == KM_OK);
    run_control_cycle(&core); /* A completed control cycle leases motion. */
    assert(km_commands_attach(&core, 0x1234ULL, km_controller_changed, true) == KM_OK);

    km_controller_changed(); /* Task context: collect the edge output first. */
    km_core_timepoint_t now;
    assert(km_clock_get_time(&now) == KM_OK);
    km_core_host_comm_ready(&core, now.ms);
    provide_baseline_sensors(&core, now.ms);

    km_request_identity_t req = {
        .client = KM_CLIENT_HOST, .session_id = 1u, .req_id = 105u,
        .op_type = KM_OP_TYPE_LOAD, .lane_idx = 0u
    };
    assert(km_core_request_start(&core, &req, KM_DIR_FORWARD, now) == KM_REQ_ACCEPTED);
    km_controller_changed();

    for (int i = 0; i < 10; ++i) {
        dispatch_earliest_timer_irq();
    }
    assert(core.op_state != KM_OP_STATE_FAULT);

    /* Cease fresh samples, retain old acquisition timestamp */
    stale_flag = true;

    for (int i = 0; i < 150; ++i) {
        dispatch_earliest_timer_irq();
        if (core.system_fault == KM_FAULT_SENSOR_FAILURE) break;
    }

    assert(core.system_fault == KM_FAULT_SENSOR_FAILURE);
    assert(core.op_state == KM_OP_STATE_FAULT);
    assert(!feeder->enabled);
}

/* =========================================================================
 * 14. Overdue pulse service fault
 * ========================================================================= */
/* =========================================================================
 * 15. Ordinary driver fault propagation
 * ========================================================================= */
static void test_ordinary_driver_fault_propagation(void)
{
    dyn_driver_fault = KM_FAULT_NONE;
    init_and_ready_clock(520000000u);

    km_device_profile_t p;
    km_profile_init_nightowl(&p);
    km_operation_core_t core;
    assert(km_core_init(&core, &p) == KM_OK);

    km_step_dir_config_t cfg = default_stepdir_config();
    assert(km_stepdir_gpio_setup(0u, 10u, 11u, 12u, &cfg, dummy_ready_true, dyn_fault_query, NULL) == KM_OK);
    km_driver_t *feeder = km_stepdir_gpio_get_driver(0u);
    assert(km_core_bind_lane_driver(&core, 0u, feeder, NULL) == KM_OK);
    assert(km_controller_attach(&core, default_sensor_sample_cb, NULL) == KM_OK);
    run_control_cycle(&core); /* A completed control cycle leases motion. */
    assert(km_commands_attach(&core, 0x1234ULL, km_controller_changed, true) == KM_OK);

    km_controller_changed(); /* Task context: collect the edge output first. */
    km_core_timepoint_t now;
    assert(km_clock_get_time(&now) == KM_OK);
    km_core_host_comm_ready(&core, now.ms);
    provide_baseline_sensors(&core, now.ms);

    km_request_identity_t req = {
        .client = KM_CLIENT_HOST, .session_id = 1u, .req_id = 107u,
        .op_type = KM_OP_TYPE_LOAD, .lane_idx = 0u
    };
    assert(km_core_request_start(&core, &req, KM_DIR_FORWARD, now) == KM_REQ_ACCEPTED);
    km_controller_changed();

    for (int i = 0; i < 5; ++i) {
        dispatch_earliest_timer_irq();
    }
    assert(core.op_state != KM_OP_STATE_FAULT);

    dyn_driver_fault = KM_FAULT_DRIVER_FAULT;

    for (int i = 0; i < 10; ++i) {
        dispatch_earliest_timer_irq();
        if (core.system_fault == KM_FAULT_DRIVER_FAULT) break;
    }

    assert(core.system_fault == KM_FAULT_DRIVER_FAULT);
    assert(core.op_state == KM_OP_STATE_FAULT);
    assert(!feeder->enabled);
}

/* =========================================================================
 * 16. Shutdown BEFORE attachment and no resume after clear_shutdown
 * ========================================================================= */
static void test_shutdown_before_attachment_and_no_resume(void)
{
    init_and_ready_clock(520000000u);

    km_device_profile_t p;
    km_profile_init_nightowl(&p);
    km_operation_core_t core;
    assert(km_core_init(&core, &p) == KM_OK);

    emergency_stop("shutdown_before_attachment");
    assert(km_controller_is_shutdown());

    /* Attachment rejected due to shutdown */
    assert(km_controller_attach(&core, NULL, NULL) == KM_ERR_FAULTED);
    assert(!km_controller_is_attached());

    /* Clear scheduler shutdown: controller shutdown remains permanently latched */
    sched_clear_shutdown();
    assert(km_controller_is_shutdown());
    assert(km_controller_attach(&core, NULL, NULL) == KM_ERR_FAULTED);

    km_controller_changed();
    assert(shim_active_timer_count() == 0);
}

/* =========================================================================
 * 17. Shutdown AFTER attachment and no resume after clear_shutdown
 * ========================================================================= */
static void test_shutdown_after_attachment_and_no_resume(void)
{
    init_and_ready_clock(520000000u);

    km_device_profile_t p;
    km_profile_init_nightowl(&p);
    km_operation_core_t core;
    assert(km_core_init(&core, &p) == KM_OK);

    assert(km_controller_attach(&core, NULL, NULL) == KM_OK);
    assert(km_controller_is_attached());
    assert(shim_active_timer_count() == 2);

    emergency_stop("shutdown_after_attachment");
    assert(km_controller_is_shutdown());

    sched_clear_shutdown();
    assert(km_controller_is_shutdown());

    km_controller_changed();
    assert(shim_active_timer_count() == 0);
}

/* =========================================================================
 * 18. Active motion combined execution and sampling costs at
 *     1 MHz, 12.288 MHz, and 520 MHz
 * ========================================================================= */
static uint32_t active_cost_sample_us = 0u;
static void cost_matrix_sample_cb(km_operation_core_t *c, void *ctx)
{
    (void)ctx;
    km_core_timepoint_t acquired;
    assert(km_clock_get_time(&acquired) == KM_OK);
    provide_baseline_sensors(c, acquired.ms);
    if (active_cost_sample_us > 0u) {
        shim_advance_ticks(timer_from_us(active_cost_sample_us));
    }
}

static void run_motion_cost_case(uint32_t freq, uint32_t gpio_ticks, uint32_t sample_us)
{
    init_and_ready_clock(freq);
    active_cost_sample_us = sample_us;
    shim_gpio_write_delay_ticks = gpio_ticks;

    km_device_profile_t p;
    km_profile_init_nightowl(&p);
    km_operation_core_t core;
    assert(km_core_init(&core, &p) == KM_OK);

    km_step_dir_config_t cfg = default_stepdir_config();
    assert(km_stepdir_gpio_setup(0u, 10u, 11u, 12u, &cfg, dummy_ready_true, NULL, NULL) == KM_OK);
    km_driver_t *driver = km_stepdir_gpio_get_driver(0u);
    
km_result_t res_bind = km_core_bind_lane_driver(&core, 0u, driver, NULL);
if (res_bind != KM_OK) {
    printf("DEBUG_BIND: %d\n", res_bind);
}
assert(res_bind == KM_OK);

    assert(km_controller_attach(&core, cost_matrix_sample_cb, NULL) == KM_OK);
    run_control_cycle(&core); /* A completed control cycle leases motion. */

    km_controller_changed(); /* Task context: collect the edge output first. */
    km_core_timepoint_t now;
    assert(km_clock_get_time(&now) == KM_OK);
    km_core_host_comm_ready(&core, now.ms);
    provide_baseline_sensors(&core, now.ms);

    km_request_identity_t req = {
        .client = KM_CLIENT_HOST, .session_id = 1u, .req_id = 1u,
        .op_type = KM_OP_TYPE_LOAD, .lane_idx = 0u
    };
    assert(km_core_request_start(&core, &req, KM_DIR_FORWARD, now) == KM_REQ_ACCEPTED);
    km_controller_changed();

    unsigned calls = 0u;
    while (core.trajectory.current_position_steps < 10 || shim_gpio_current_level[10u] != 1u) {
        assert(++calls < 10000u);
        dispatch_earliest_timer_irq();
        assert(core.op_state != KM_OP_STATE_FAULT);
    }

    /* Cancel during active high STEP */
    km_controller_changed(); /* Task context: collect the edge output first. */
    assert(km_clock_get_time(&now) == KM_OK);
    assert(km_core_request_cancel_target(&core, &req, now) == KM_REQ_ACCEPTED);
    km_controller_changed();

    while (core.stage != KM_STAGE_STOPPED) {
        assert(++calls < 20000u);
        dispatch_earliest_timer_irq();
        assert(core.op_state != KM_OP_STATE_FAULT);
    }

    assert(!driver->enabled && !driver->step_pin_high && shim_gpio_current_level[10u] == 0u);
    assert(shim_active_timer_count() == 2);

    uint32_t previous = 0u, previous_level = 0u, rising = 0u;
    bool have_edge = false;
    for (uint32_t i = 0u; i < shim_gpio_events_count; ++i) {
        const shim_gpio_event_t *e = &shim_gpio_events[i];
        if (e->pin != 10u) continue;
        if (e->val != previous_level) {
            if (have_edge) assert(e->tick - previous >= timer_from_us(10u));
            if (e->val) ++rising;
            previous = e->tick;
            previous_level = e->val;
            have_edge = true;
        }
    }
    assert(rising >= 10u);
    assert(core.trajectory.current_position_steps == (int64_t)rising);

    km_event_t event;
    assert(km_event_queue_peek(&core.event_queue, &event) == KM_OK);
    assert(event.type == KM_EVENT_LOAD_CANCELLED);
}

static void test_active_motion_costs_1mhz_12mhz_520mhz(void)
{
    const uint32_t freqs[] = {1000000u, 12288000u, 520000000u};
    for (unsigned f = 0u; f < 3u; ++f) {
        for (unsigned g = 0u; g < 2u; ++g) {
            for (unsigned s = 0u; s < 2u; ++s) {
                fflush(stdout);
                pid_t pid = fork();
                assert(pid >= 0);
                if (pid == 0) {
                    alarm(15);
                    run_motion_cost_case(freqs[f], g ? 7u : 1u, s ? 25u : 0u);
                    exit(0);
                }
                int status = 0;
                assert(waitpid(pid, &status, 0) == pid);
                assert(WIFEXITED(status) && WEXITSTATUS(status) == 0);
            }
        }
    }
}

/* =========================================================================
 * 19. Actual fine-microsecond wrap through public clock observations
 * ========================================================================= */
static void test_fine_microsecond_clock_wrap(void)
{
    /* Clock at 1 MHz: 1 tick = 1 us */
    init_and_ready_clock(1000000u);

    km_core_timepoint_t now;
    assert(km_clock_get_time(&now) == KM_OK);

    /* Advance in chunks < 2^31 ticks to advance time near 2^32 us (4294967295 us) */
    for (int i = 0; i < 4; ++i) {
        shim_advance_ticks(1070000000u);
        assert(km_clock_get_time(&now) == KM_OK);
    }

    /* Advance close to UINT32_MAX: UINT32_MAX - 4280000000 = 14967295 */
    shim_advance_ticks(14964000u);
    assert(km_clock_get_time(&now) == KM_OK);
    assert(now.us > 4294960000u); /* Within ~7 ms of 32-bit microsecond wraparound */

    km_device_profile_t p;
    km_profile_init_nightowl(&p);
    km_operation_core_t core;
    assert(km_core_init(&core, &p) == KM_OK);

    km_step_dir_config_t cfg = default_stepdir_config();
    assert(km_stepdir_gpio_setup(0u, 10u, 11u, 12u, &cfg, dummy_ready_true, NULL, NULL) == KM_OK);
    km_driver_t *feeder = km_stepdir_gpio_get_driver(0u);
    assert(km_core_bind_lane_driver(&core, 0u, feeder, NULL) == KM_OK);
    assert(km_controller_attach(&core, default_sensor_sample_cb, NULL) == KM_OK);
    run_control_cycle(&core); /* A completed control cycle leases motion. */
    assert(km_commands_attach(&core, 0x1234ULL, km_controller_changed, true) == KM_OK);

    km_controller_changed(); /* Task context: collect the edge output first. */
    assert(km_clock_get_time(&now) == KM_OK);
    km_core_host_comm_ready(&core, now.ms);
    provide_baseline_sensors(&core, now.ms);

    km_request_identity_t req = {
        .client = KM_CLIENT_HOST, .session_id = 1u, .req_id = 109u,
        .op_type = KM_OP_TYPE_LOAD, .lane_idx = 0u
    };
    assert(km_core_request_start(&core, &req, KM_DIR_FORWARD, now) == KM_REQ_ACCEPTED);
    km_controller_changed();

    uint32_t us_before = now.us;
    bool saw_wrap = false;

    for (int i = 0; i < 200; ++i) {
        dispatch_earliest_timer_irq();
        assert(km_clock_get_time(&now) == KM_OK);
        if (now.us < us_before) {
            saw_wrap = true;
        }
        if (core.trajectory.current_position_steps >= 10 && saw_wrap)
            break;
    }

    assert(saw_wrap); /* Fine microsecond wrap occurred */
    assert(core.op_state != KM_OP_STATE_FAULT);
    assert(core.trajectory.current_position_steps >= 10);

    /* Cancel and settle */
    km_controller_changed(); /* Task context: collect the edge output first. */
    assert(km_clock_get_time(&now) == KM_OK);
    assert(km_core_request_cancel_target(&core, &req, now) == KM_REQ_ACCEPTED);
    km_controller_changed();

    /* Stop from ~500 steps/s at D=12000, J=160000 takes v/D + D/J = 0.12 s, and
     * a dispatch may advance well under a millisecond, so allow many more. */
    for (int i = 0; i < 5000; ++i) {
        dispatch_earliest_timer_irq();
        if (core.stage == KM_STAGE_STOPPED) break;
    }

    assert(core.stage == KM_STAGE_STOPPED);
    assert(core.op_state == KM_OP_STATE_IDLE);
}

/* =========================================================================
 * Isolated runner harness
 * ========================================================================= */
/* Compose actual task-context command handlers with local timer service. */
void command_km_start(uint32_t *args);
void command_km_cancel(uint32_t *args);
void command_km_heartbeat(uint32_t *args);
void command_km_stop(uint32_t *args);

static void command_motion_end(unsigned how)
{
    init_and_ready_clock(520000000u);
    km_device_profile_t profile;
    km_profile_init_nightowl(&profile);
    km_operation_core_t core;
    assert(km_core_init(&core, &profile) == KM_OK);
    km_step_dir_config_t cfg = default_stepdir_config();
    assert(km_stepdir_gpio_setup(0u, 10u, 11u, 12u, &cfg,
                               dummy_ready_true, NULL, NULL) == KM_OK);
    km_driver_t *driver = km_stepdir_gpio_get_driver(0u);
    
km_result_t res_bind = km_core_bind_lane_driver(&core, 0u, driver, NULL);
if (res_bind != KM_OK) {
    printf("DEBUG_BIND: %d\n", res_bind);
}
assert(res_bind == KM_OK);

    assert(km_controller_attach(&core, default_sensor_sample_cb, NULL) == KM_OK);
    assert(km_commands_attach(&core, UINT64_C(0xFEDCBA9876543210),
                              km_controller_changed, true) == KM_OK);
    // Let the local sampler acquire initial inputs before a host intent.
    for (unsigned i = 0; i < 4u; ++i) dispatch_earliest_timer_irq();
    uint32_t args[] = {0xFEDCBA98u, 0x76543210u, KM_CLIENT_HOST, 1u,
                       0x80000001u, KM_OP_TYPE_LOAD, 0u, KM_DIR_FORWARD};
    uint32_t heartbeat[] = {args[0], args[1], 1u};
    command_km_heartbeat(heartbeat);
    command_km_start(args);
    assert(!test_shim_irq_is_disabled());
    assert(core.stage == KM_STAGE_LOAD_FEED_TO_HUB && driver->enabled);
    unsigned calls = 0u;
    while (core.trajectory.current_position_steps < 10 || shim_gpio_current_level[10u] != 1u) {
        assert(++calls < 10000u);
        dispatch_earliest_timer_irq();
        assert(core.op_state != KM_OP_STATE_FAULT);
    }
    assert(shim_gpio_current_level[10u] == 1u);
    // A mismatched instance must not cancel or reschedule the active operation.
    args[0] ^= 1u;
    command_km_cancel(args);
    assert(core.stage == KM_STAGE_LOAD_FEED_TO_HUB);
    args[0] ^= 1u;
    if (how == 0u) {
        command_km_cancel(args);
        while (core.stage != KM_STAGE_STOPPED) {
            assert(++calls < 20000u);
            dispatch_earliest_timer_irq();
            assert(core.op_state != KM_OP_STATE_FAULT);
        }
        km_event_t event;
        assert(km_event_queue_peek(&core.event_queue, &event) == KM_OK);
        assert(event.type == KM_EVENT_LOAD_CANCELLED && core.event_queue.count == 1u);
        assert(event.req_id == args[4]);
    } else if (how == 1u) {
        command_km_stop(args);
        assert(core.op_state == KM_OP_STATE_FAULT);
        assert(core.system_fault == KM_FAULT_EMERGENCY_STOP);
        for (unsigned i = 0; i < 8u; ++i) dispatch_earliest_timer_irq();
        assert(shim_active_timer_count() == 2);
    } else {
        emergency_stop("controller_high_step_shutdown");
        assert(km_controller_is_shutdown());
        assert(test_shim_clock_reads_in_shutdown == 0u);
        sched_clear_shutdown();
        command_km_start(args);
        km_controller_changed();
        assert(shim_active_timer_count() == 0);
    }
    assert(!test_shim_irq_is_disabled());
    assert(shim_gpio_current_level[10u] == 0u);
    assert(shim_gpio_current_level[12u] == 0u);
}

static void test_command_cancel_high_step(void) { command_motion_end(0u); }
static void test_command_stop_high_step(void) { command_motion_end(1u); }
static void test_global_shutdown_high_step(void) { command_motion_end(2u); }

static bool wire_sample_enabled = true;
static uint32_t wire_sample_cost_us;
static void wire_switch_sample(km_operation_core_t *core, void *context)
{
    (void)context;
    if (!wire_sample_enabled) return;
    km_core_timepoint_t acquired;
    assert(km_clock_get_time(&acquired) == KM_OK);
    km_controller_note_switch_sample(core, acquired.us);
    shim_advance_ticks(timer_from_us(wire_sample_cost_us));
}

static void test_switch_delay_wire_query(void)
{
    init_and_ready_clock(1000000u);
    km_device_profile_t profile;
    km_profile_init_nightowl(&profile);
    km_operation_core_t core;
    assert(km_core_init(&core, &profile) == KM_OK);
    wire_sample_enabled = true;
    wire_sample_cost_us = 5u;
    assert(km_controller_attach(&core, wire_switch_sample, NULL) == KM_OK);
    assert(km_commands_attach(&core, UINT64_C(0xFEDCBA9876543210),
                              km_controller_changed, false) == KM_OK);
    uint32_t query[] = {91u, 0xFEDCBA98u, 0x76543210u};
    command_km_switch_delay_query(query);
    assert(switch_reply_seen && switch_reply[0] == 91u
           && switch_reply[1] == query[1] && switch_reply[2] == query[2]
           && switch_reply[3] == 0u && switch_reply[4] == 0u
           && switch_reply[5] == 0u && switch_reply[6] == 0u);
    uint32_t observed_delay = 0;
    for (unsigned i = 0; i < 3 && !km_controller_get_switch_sample_delay(&observed_delay); ++i)
        dispatch_earliest_timer_irq();
    assert(km_controller_get_switch_sample_delay(&observed_delay));
    switch_reply_seen = false;
    command_km_switch_delay_query(query);
    assert(switch_reply_seen && switch_reply[3] == 1u
           && switch_reply[4] < KM_TIME_WRAP_HORIZON_US
           && switch_reply[5] >= 1u && switch_reply[6] >= switch_reply[4]);
    uint32_t first_count = switch_reply[5], first_max = switch_reply[6];
    wire_sample_cost_us = 40u;
    uint32_t count = first_count, max_us = first_max;
    for (unsigned i = 0; i < 3 && count == first_count; ++i) {
        dispatch_earliest_timer_irq();
        km_controller_get_switch_sample_stats(&count, &max_us);
    }
    assert(count > first_count && max_us > first_max && max_us >= 40u);
    command_km_switch_delay_query(query);
    assert(switch_reply[5] == count && switch_reply[6] == max_us);
    uint32_t historical_count = switch_reply[5], historical_max = switch_reply[6];
    wire_sample_enabled = false;
    for (unsigned i = 0; i < 3 && km_controller_get_switch_sample_delay(&observed_delay); ++i)
        dispatch_earliest_timer_irq();
    assert(!km_controller_get_switch_sample_delay(&observed_delay));
    command_km_switch_delay_query(query);
    assert(switch_reply[3] == 0u && switch_reply[4] == 0u
           && switch_reply[5] == historical_count && switch_reply[6] == historical_max);
    km_controller_shutdown();
    command_km_switch_delay_query(query);
    assert(switch_reply[3] == 0u && switch_reply[4] == 0u
           && switch_reply[5] == historical_count && switch_reply[6] == historical_max);
    query[2] = 0u;
    command_km_switch_delay_query(query);
    assert(switch_reply[2] == 0x76543210u && switch_reply[3] == 0u
           && switch_reply[5] == 0u && switch_reply[6] == 0u);
}

/* =========================================================================
 * Task/interrupt split: the timer interrupt services STEP/DIR edges only;
 * the control task samples, updates the core and plans. These scenarios put
 * interrupts and commands between and inside task cycles.
 * ========================================================================= */
static km_operation_core_t split_core;
static km_driver_t *split_driver;
static const km_request_identity_t split_req = {
    .client = KM_CLIENT_HOST, .session_id = 1u, .req_id = 900u,
    .op_type = KM_OP_TYPE_LOAD, .lane_idx = 0u
};
static void (*split_hook)(km_operation_core_t *c);

/* Fresh sensors, then a one-shot hook inside the task cycle before the core. */
static void split_sample(km_operation_core_t *c, void *ctx)
{
    default_sensor_sample_cb(c, ctx);
    void (*hook)(km_operation_core_t *) = split_hook;
    split_hook = NULL;
    if (hook) hook(c);
}

/* A pending interrupt fires when task code unmasks: after the Nth
 * irq_restore() to enabled, run the action as the timer interrupt would. */
static unsigned preempt_countdown;
static void (*preempt_action)(void);
extern void __real_irq_restore(irqstatus_t flag);
void __wrap_irq_restore(irqstatus_t flag)
{
    __real_irq_restore(flag);
    if (flag || !preempt_countdown || --preempt_countdown) return;
    void (*action)(void) = preempt_action;
    preempt_action = NULL;
    action();
}

static void split_start(uint32_t accel)
{
    dyn_driver_fault = KM_FAULT_NONE;
    init_and_ready_clock(520000000u);
    km_device_profile_t p;
    km_profile_init_nightowl(&p);
    p.default_acceleration_steps_s2 = p.default_deceleration_steps_s2 = accel;
    assert(km_core_init(&split_core, &p) == KM_OK);
    km_step_dir_config_t cfg = default_stepdir_config();
    assert(km_stepdir_gpio_setup(0u, 10u, 11u, 12u, &cfg, dummy_ready_true,
                                 dyn_fault_query, NULL) == KM_OK);
    split_driver = km_stepdir_gpio_get_driver(0u);
    assert(km_core_bind_lane_driver(&split_core, 0u, split_driver, NULL) == KM_OK);
    assert(km_controller_attach(&split_core, split_sample, NULL) == KM_OK);
    assert(km_commands_attach(&split_core, 0x1234ULL, km_controller_changed, true) == KM_OK);
    run_control_cycle(&split_core);
    km_controller_changed();
    km_core_timepoint_t now;
    assert(km_clock_get_time(&now) == KM_OK);
    km_core_host_comm_ready(&split_core, now.ms);
    provide_baseline_sensors(&split_core, now.ms);
    km_request_identity_t req = split_req;
    assert(km_core_request_start(&split_core, &req, KM_DIR_FORWARD, now) == KM_REQ_ACCEPTED);
    km_controller_changed();
    assert(split_driver->enabled);
}

/* Run until the position, then stop just after a completed control cycle,
 * with STEP low and the task copy collected, so an event window starts with
 * exact accounting and a fresh lease. */
static void split_run_to(int64_t position)
{
    for (unsigned i = 0u; split_core.trajectory.current_position_steps < position; ++i) {
        assert(i < 20000u && split_core.op_state != KM_OP_STATE_FAULT);
        dispatch_earliest_timer_irq();
    }
    for (unsigned i = 0u; i < 50u; ++i) {
        run_control_cycle(&split_core);
        if (shim_gpio_current_level[10u] == 0u) break;
    }
    assert(shim_gpio_current_level[10u] == 0u);
    km_controller_changed();
}

/* Rising STEP edges in the event log; asserts both minimum levels. */
static uint32_t step_rises(void)
{
    uint32_t previous = 0u, level = 0u, rising = 0u;
    bool have_edge = false;
    assert(shim_gpio_events_count < SHIM_GPIO_MAX_EVENTS);
    for (uint32_t i = 0u; i < shim_gpio_events_count; ++i) {
        const shim_gpio_event_t *e = &shim_gpio_events[i];
        if (e->pin != 10u || e->val == level) continue;
        if (have_edge) assert(e->tick - previous >= timer_from_us(10u));
        if (e->val) ++rising;
        previous = e->tick;
        level = e->val;
        have_edge = true;
    }
    return rising;
}

static km_core_timepoint_t split_now(void)
{
    km_core_timepoint_t now;
    assert(km_clock_get_time(&now) == KM_OK);
    return now;
}

/* The control task observes a failed sensor read while the local stepper
 * still has a STEP queued. The core's stop disables the driver and clears
 * the queued edge at once, so nothing rises after the fault and the
 * completed steps match the edges emitted since the start. */
static struct stepper *split_stepper;
static void coincident_fault_hook(km_operation_core_t *c)
{
    if (!stepper_local_active(split_stepper)) { /* Wait for a step in flight. */
        split_hook = coincident_fault_hook;
        return;
    }
    km_core_timepoint_t now = split_now();
    km_core_set_sensors_at(c, 0u, false, false, false, false, now.ms);
}

static void test_coincident_sensor_fault_stops_stepping(void)
{
    split_start(8000u);
    split_stepper = km_stepdir_gpio_get_stepper_for_driver(split_driver);
    assert(split_stepper);
    split_run_to(5);
    split_hook = coincident_fault_hook;
    for (unsigned i = 0u; split_core.op_state != KM_OP_STATE_FAULT; ++i) {
        assert(i < 200u);
        dispatch_earliest_timer_irq();
    }
    assert(split_core.system_fault == KM_FAULT_SENSOR_FAILURE);
    uint32_t rises_at_fault = step_rises();
    for (unsigned i = 0u; i < 8u; ++i) dispatch_earliest_timer_irq();
    assert(step_rises() == rises_at_fault); /* Nothing rises at or after the fault. */
    assert(!split_driver->enabled && !split_driver->step_pin_high);
    assert(shim_gpio_current_level[10u] == 0u && shim_gpio_current_level[12u] == 0u);
    assert(!stepper_local_active(split_stepper)); /* The queued edges are gone. */
    /* Completed steps account exactly for the edges emitted since the start. */
    assert(split_core.trajectory.current_position_steps == (int64_t)rises_at_fault);
}

/* Starve the control task: timers keep firing, the task never runs. No STEP
 * rises at or after the lease; the first wake there stops the driver. */
static void test_task_starvation_underrun_fails_closed(void)
{
    split_start(100000u);
    split_run_to(80);
    uint32_t lease_end = split_core.last_control_us + KM_CONTROLLER_LEASE_US;
    shim_gpio_clear_events();
    uint32_t rises_in_lease = 0u, stopped_at = 0u;
    /* Real stepper.c fires a separate timer event per edge, so this now
     * takes many more single-timer dispatches to reach the driver's stop
     * than one edge per step did. */
    for (unsigned i = 0u; split_driver->enabled; ++i) {
        assert(i < 20000u);
        uint32_t before = step_rises();
        dispatch_timer_only();
        km_core_timepoint_t now = split_now();
        if (step_rises() != before) {
            /* Edges only inside the lease plus the two-tick lead and one more
             * tick: windows already committed before the lease expired keep
             * draining (docs/plan/b2-stepper-segments.md, "Lease and
             * liveness"). */
            assert(km_time_before(now.us, lease_end + 3u * KM_CONTROLLER_PERIOD_US));
            ++rises_in_lease;
        }
        stopped_at = now.us;
    }
    assert(rises_in_lease > 0u); /* Motion continued on the adopted image meanwhile. */
    assert(!km_time_before(stopped_at, lease_end));
    assert(km_time_elapsed(stopped_at, lease_end) <= 3u * KM_CONTROLLER_PERIOD_US);
    assert(!split_driver->step_pin_high && shim_gpio_current_level[12u] == 0u);
    /* The core has not run yet; the task then latches the edge fault. */
    assert(split_core.op_state != KM_OP_STATE_FAULT);
    shim_run_task();
    assert(split_core.op_state == KM_OP_STATE_FAULT);
    assert(split_core.system_fault == KM_FAULT_CONTROL_TIMING);
    assert(split_core.trajectory.faulted && split_core.trajectory.position_uncertain);
    km_event_t event;
    assert(km_event_queue_peek(&split_core.event_queue, &event) == KM_OK);
    assert(event.type == KM_EVENT_LOAD_FAILED && event.value == KM_FAULT_CONTROL_TIMING);
    /* Later cycles and exchanges do not revive the stopped image. */
    for (unsigned i = 0u; i < 6u; ++i) dispatch_earliest_timer_irq();
    km_controller_changed();
    assert(!split_driver->enabled && step_rises() == rises_in_lease);
}

/* An interrupt fault lands while the task is between its planning and its
 * exchange: the task has built a newer image on a copy that does not show
 * the fault. The exchange refuses that image, the core stops with the fault
 * and no later STEP is emitted. */
static uint32_t hook_lease_end;
static void underrun_now(void)
{
    /* The interrupt is late past the lease, then services its wakes. */
    km_core_timepoint_t now = split_now();
    if (km_time_before(now.us, hook_lease_end))
        shim_advance_ticks(timer_from_us(hook_lease_end - now.us));
    /* Real stepper.c fires a separate timer event per edge, so draining
     * whatever was already queued before the underrun now takes many more
     * single-timer dispatches than the old per-edge model needed. */
    for (unsigned i = 0u; split_driver->enabled; ++i) {
        assert(i < 20000u);
        dispatch_timer_only();
    }
}

static unsigned fault_after_unmasks;
static bool hook_ran;
static void retarget_then_fault_hook(km_operation_core_t *c)
{
    hook_lease_end = c->last_control_us + KM_CONTROLLER_LEASE_US; /* Previous cycle's. */
    km_core_timepoint_t now = split_now();
    uint32_t seq = c->trajectory.command_seq;
    assert(km_trajectory_set_target_velocity(&c->trajectory, 1200, now.us) == KM_OK);
    assert(c->trajectory.command_seq == seq + 1u && !c->trajectory.faulted);
    shim_gpio_clear_events();
    /* Unmasks after the hook: the task's clock read (1), the exchange's clock
     * read (2) and, for the newer image, its collect (3). The interrupt fires
     * before the exchange collects, or while it seeks the image. */
    preempt_countdown = fault_after_unmasks;
    preempt_action = underrun_now;
    hook_ran = true;
}

static void isr_fault_mid_update(unsigned unmasks)
{
    split_start(8000u);
    split_run_to(5);
    fault_after_unmasks = unmasks;
    split_hook = retarget_then_fault_hook;
    for (unsigned i = 0u; !hook_ran || preempt_countdown; ++i) {
        assert(i < 20u);
        dispatch_earliest_timer_irq();
    }
    /* The interrupt's emergency stop turned the outputs off at once. A fault
     * before the exchange collects is refused by that exchange; one after its
     * single locked section (there is no seek window any more) is latched by
     * the next exchange, one control cycle later. */
    assert(split_core.trajectory.faulted || !split_driver->enabled);
    for (unsigned i = 0u; split_core.op_state != KM_OP_STATE_FAULT; ++i) {
        assert(i < 20u);
        dispatch_earliest_timer_irq();
    }
    assert(split_core.op_state == KM_OP_STATE_FAULT);
    assert(split_core.system_fault == KM_FAULT_CONTROL_TIMING);
    assert(split_core.trajectory.faulted && !split_driver->enabled);
    /* Windows committed before the lease expired may drain (lease + LEAD +
     * one tick); nothing may step after the fault is latched. */
    uint32_t rises_at_fault = step_rises();
    for (unsigned i = 0u; i < 8u; ++i) dispatch_earliest_timer_irq();
    assert(step_rises() == rises_at_fault && !split_driver->enabled
           && shim_gpio_current_level[12u] == 0u);
}
static void test_isr_fault_before_exchange_collect(void) { isr_fault_mid_update(2u); }
static void test_isr_fault_after_exchange(void) { isr_fault_mid_update(3u); }

/* Retarget in every task cycle while the interrupt emits edges both before
 * the exchange and inside its seek window (between the collect and the
 * adoption). Every image continues on the live output: no missed pulse, and
 * rising edges equal the position and the completed-pulse count. */
static void interrupt_edges(void)
{
    /* Timer wakes due within the next 150 us, as a burst during the task. */
    uint32_t limit = test_shim_ticks + timer_from_us(150u);
    for (struct timer *t = shim_find_earliest_timer();
         t && !timer_is_before(limit, t->waketime); t = shim_find_earliest_timer())
        (void)dispatch_timer_only();
}

static unsigned handoffs;
static void retarget_hook(km_operation_core_t *c)
{
    static const int32_t targets[] = {1500, 3500, 2000, 3000, 2500};
    km_core_timepoint_t now = split_now();
    assert(km_trajectory_set_target_velocity(&c->trajectory, targets[handoffs % 5u], now.us) == KM_OK);
    interrupt_edges(); /* Before the exchange: edges on the previous image. */
    /* Unmasks: task clock (1), exchange clock (2), its collect (3): the
     * interrupt then fires while the task seeks the new image. */
    preempt_countdown = 3u;
    preempt_action = interrupt_edges;
    if (++handoffs < 40u) split_hook = retarget_hook;
}

static void test_exact_accounting_across_handoffs(void)
{
    split_start(100000u);
    split_run_to(80);
    int64_t start = split_core.trajectory.current_position_steps;
    int64_t start_pos = split_core.trajectory.current_position_steps;
    shim_gpio_clear_events();
    handoffs = 0u;
    split_hook = retarget_hook;
    for (unsigned i = 0u; handoffs < 40u || split_hook || preempt_countdown; ++i) {
        assert(i < 20000u && split_core.op_state != KM_OP_STATE_FAULT);
        dispatch_earliest_timer_irq();
    }
    km_request_identity_t req = split_req;
    km_controller_changed();
    assert(km_core_request_cancel_target(&split_core, &req, split_now()) == KM_REQ_ACCEPTED);
    km_controller_changed();
    for (unsigned i = 0u; split_core.stage != KM_STAGE_STOPPED; ++i) {
        assert(i < 20000u && split_core.op_state != KM_OP_STATE_FAULT);
        dispatch_earliest_timer_irq();
    }
    assert(!split_core.trajectory.faulted && !split_core.trajectory.position_uncertain);
    int64_t travelled = split_core.trajectory.current_position_steps - start;
    assert(travelled > 40 && travelled == (int64_t)step_rises());
    assert(split_core.trajectory.current_position_steps - start_pos == travelled);
}

/* =========================================================================
 * B2 tick controller, slow motion: a step every four ticks (250 steps/s) is
 * well under the 1000 steps/s where an idle stepper between steps is normal.
 * Windows chain from queued_end and the stepper is queued at queued_end; an
 * on-time tick leaves the stepper idle until the next generating tick, and a
 * tick more than LEAD late leaves that start clock inside the local
 * stepper's lead, which is the one refusal: an underrun fault, not the lease.
 * ========================================================================= */
#define SLOW_VELOCITY 250u

static void split_start_slow(void)
{
    split_start(8000u);
    km_controller_changed(); /* Task context: collect the edge output first. */
    km_core_timepoint_t now;
    assert(km_clock_get_time(&now) == KM_OK);
    assert(km_trajectory_set_target_velocity(&split_core.trajectory, SLOW_VELOCITY,
                                             now.us) == KM_OK);
    km_controller_changed();
}

/* Tick of the last commanded STEP rising edge in the event log. */
static uint32_t last_step_rise_ticks(void)
{
    uint32_t tick = 0u;
    for (uint32_t i = 0u; i < shim_gpio_events_count; ++i) {
        const shim_gpio_event_t *e = &shim_gpio_events[i];
        if (e->pin == 10u && e->val)
            tick = e->tick;
    }
    return tick;
}

/* A tick that ran more than LEAD (2*TICK) late: its window would queue the
 * idle stepper's next slow step at a start clock inside the local stepper's
 * 100 us lead, so stepper_local_queue() refuses. Distinct from the lease
 * expiry test: the tick fires before the lease, the stepper is idle. */
static void test_late_tick_underrun_faults(void)
{
    split_start_slow();
    /* Reach steady slow motion with the stepper genuinely idle between steps:
     * model_position_steps only equals the completed position once a queued
     * step has executed, so the trajectory's lagging stepper_active mirror
     * alone would exit on the generating tick with a step already pending. */
    for (unsigned i = 0u; ; ++i) {
        assert(i < 20000u && split_core.op_state != KM_OP_STATE_FAULT);
        if (split_core.trajectory.current_velocity_steps_s == (int32_t)SLOW_VELOCITY
            && !split_core.trajectory.stepper_active
            && split_core.trajectory.current_position_steps
                   == split_core.trajectory.model_position_steps)
            break;
        dispatch_earliest_timer_irq();
    }
    /* The next crossing is one step interval after the last, at a fixed rate.
     * Jump just past it: the generating tick (two ticks earlier) now fires
     * ~2 ticks late, past the lead but still inside the lease. */
    uint32_t c_next = last_step_rise_ticks()
        + timer_from_us(1000000u / SLOW_VELOCITY + 1u);
    assert(timer_is_before(test_shim_ticks, c_next));
    shim_gpio_clear_events();
    shim_advance_ticks(c_next - test_shim_ticks);
    for (unsigned i = 0u; split_driver->enabled; ++i) {
        assert(i < 20000u);
        dispatch_timer_only();
    }
    assert(!split_driver->enabled);
    /* The core has not run yet; the task then latches the edge fault. */
    assert(split_core.op_state != KM_OP_STATE_FAULT);
    shim_run_task();
    assert(split_core.op_state == KM_OP_STATE_FAULT);
    assert(split_core.system_fault == KM_FAULT_CONTROL_TIMING);
    assert(split_core.trajectory.faulted && split_core.trajectory.position_uncertain);
    assert(split_core.trajectory.current_position_steps
               == split_core.trajectory.model_position_steps);
    assert(split_core.trajectory.current_velocity_steps_s == 0);
    assert(!split_driver->enabled && shim_gpio_current_level[12u] == 0u);
    assert(step_rises() == 0u); /* The refused window emitted nothing. */
    for (unsigned i = 0u; i < 6u; ++i) dispatch_earliest_timer_irq();
    assert(!split_driver->enabled && step_rises() == 0u);
}

/* Positive control: the same slow motion with every tick on time never
 * refuses, emits every step, and drains to a stepper position that equals
 * the core's whole phase (idle and unfaulted). */
static void test_slow_motion_drains_without_fault(void)
{
    split_start_slow();
    for (unsigned i = 0u; split_core.trajectory.current_position_steps < 20; ++i) {
        assert(i < 20000u && split_core.op_state != KM_OP_STATE_FAULT);
        dispatch_earliest_timer_irq();
    }
    /* Drain: wait for genuine idle, not a generating tick with a step queued. */
    for (unsigned i = 0u; split_core.trajectory.stepper_active
             || split_core.trajectory.current_position_steps
                    != split_core.trajectory.model_position_steps; ++i) {
        assert(i < 20000u && split_core.op_state != KM_OP_STATE_FAULT);
        dispatch_earliest_timer_irq();
    }
    int64_t completed = split_core.trajectory.current_position_steps;
    assert(completed >= 20);
    assert(split_core.op_state != KM_OP_STATE_FAULT);
    assert(!split_core.trajectory.faulted && !split_core.trajectory.position_uncertain);
    assert((int64_t)step_rises() == completed);
    assert(split_core.trajectory.current_position_steps
               == split_core.trajectory.model_position_steps);
}

/* A host stall far longer than one tick must not wedge the fast rate timer's
 * own reschedule. Klipper's generic Linux dispatcher (src/linux/timer.c)
 * shuts the whole MCU down if a timer it just ran is still more than 100 ms
 * behind wall time; a reschedule that advances from the timer's own stale
 * old waketime by one tick, instead of catching up to the current time, can
 * need one re-dispatch per missed tick to close a gap that size, which never
 * finishes before that shutdown fires. This is a host-load bug, not a
 * step-accuracy one: every active timer must leave its waketime at or after
 * "now" once it has been dispatched, whatever the stall was. */
static void test_huge_host_stall_resyncs_rate_timer(void)
{
    split_start_slow();
    for (unsigned i = 0u; i < 50u; ++i)
        dispatch_earliest_timer_irq();

    int active = shim_active_timer_count();
    assert(active >= 1);

    /* Comfortably past both the underrun lease and the generic dispatcher's
     * own -100 ms "still behind" threshold. */
    shim_advance_ticks(timer_from_us(500000u));

    for (int i = 0; i < active; ++i) {
        struct timer *t = shim_find_earliest_timer();
        assert(t != NULL);
        dispatch_timer_only();
        assert(!timer_is_before(t->waketime, test_shim_ticks));
    }
}

/* The other half of the same property: a tick that is late but still
 * chained a moving window must replay, not resync, or queued_end's LEAD
 * drains by one tick on every such hiccup. 1.5 periods is late enough to
 * matter but stays inside the lead (2 ticks) and the lease (5 ticks), so
 * ordinary host jitter must never fault or lose a step. */
static void test_moderate_repeated_lateness_replays_without_fault(void)
{
    split_start_slow();
    for (unsigned i = 0u; i < 50u; ++i)
        dispatch_earliest_timer_irq();

    uint32_t period = timer_from_us(CONFIG_KLIPPER_MMU_RATE_TICK_US);
    int64_t before = split_core.trajectory.current_position_steps;

    for (unsigned round = 0u; round < 20u; ++round) {
        /* Fire the rate timer 1.5 periods after its own due time, not 1.5
         * periods after the last dispatch: that is what "late" means. */
        struct timer *t = shim_find_earliest_timer();
        assert(t != NULL);
        shim_set_ticks(t->waketime + period + period / 2u);
        dispatch_timer_only();
        /* Drain anything else now due (the clock's own maintenance timer)
         * without adding further lateness of our own. */
        for (unsigned drain = 0u; drain < 4u; ++drain) {
            struct timer *t2 = shim_find_earliest_timer();
            if (!t2 || timer_is_before(test_shim_ticks, t2->waketime))
                break;
            dispatch_timer_only();
        }
        shim_run_task();
        assert(!split_core.trajectory.faulted);
        assert(split_core.op_state != KM_OP_STATE_FAULT);
        assert(split_driver->enabled);
    }

    assert(split_core.trajectory.current_position_steps > before);
    assert((int64_t)step_rises() == split_core.trajectory.current_position_steps);
}

static void run_isolated(void (*test_fn)(void), const char *name)
{
    printf("Running %-60s ... ", name);
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
    printf("  Klipper MMU Controller Scheduler Integration Tests (Iter-31)\n");
    printf("============================================================\n");

    run_isolated(test_no_boot_output_or_attachment, "test_no_boot_output_or_attachment");
    run_isolated(test_pre_ready_clock_rejection, "test_pre_ready_clock_rejection");
    run_isolated(test_attach_reject_invalid_profile, "test_attach_reject_invalid_profile");
    run_isolated(test_attach_reject_dirty_or_faulted_core, "test_attach_reject_dirty_or_faulted_core");
    run_isolated(test_attach_reject_dirty_bound_driver, "test_attach_reject_dirty_bound_driver");
    run_isolated(test_repeated_attachment_rejection, "test_repeated_attachment_rejection");
    run_isolated(test_independent_local_periodic_service, "test_independent_local_periodic_service");
    run_isolated(test_real_positive_motion_core_load, "test_real_positive_motion_core_load");
    run_isolated(test_selector_move_steps_the_selector_axis, "test_selector_move_steps_the_selector_axis");
    run_isolated(test_load_to_follow_progression, "test_load_to_follow_progression");
    run_isolated(test_velocity_retarget_and_cancellation, "test_velocity_retarget_and_cancellation");
    run_isolated(test_cancellation_during_active_high_step, "test_cancellation_during_active_high_step");
    run_isolated(test_local_host_lease_expiry_without_host_call, "test_local_host_lease_expiry_without_host_call");
    run_isolated(test_stale_or_invalid_sensor_failure, "test_stale_or_invalid_sensor_failure");
    run_isolated(test_ordinary_driver_fault_propagation, "test_ordinary_driver_fault_propagation");
    run_isolated(test_shutdown_before_attachment_and_no_resume, "test_shutdown_before_attachment_and_no_resume");
    run_isolated(test_shutdown_after_attachment_and_no_resume, "test_shutdown_after_attachment_and_no_resume");
    run_isolated(test_active_motion_costs_1mhz_12mhz_520mhz, "test_active_motion_costs_1mhz_12mhz_520mhz");
    run_isolated(test_fine_microsecond_clock_wrap, "test_fine_microsecond_clock_wrap");
    run_isolated(test_command_cancel_high_step, "test_command_cancel_high_step");
    run_isolated(test_command_stop_high_step, "test_command_stop_high_step");
    run_isolated(test_global_shutdown_high_step, "test_global_shutdown_high_step");
    run_isolated(test_switch_delay_wire_query, "test_switch_delay_wire_query");
    run_isolated(test_task_starvation_underrun_fails_closed, "test_task_starvation_underrun_fails_closed");
    run_isolated(test_coincident_sensor_fault_stops_stepping, "test_coincident_sensor_fault_stops_stepping");
    run_isolated(test_isr_fault_before_exchange_collect, "test_isr_fault_before_exchange_collect");
    run_isolated(test_isr_fault_after_exchange, "test_isr_fault_after_exchange");
    run_isolated(test_exact_accounting_across_handoffs, "test_exact_accounting_across_handoffs");
    run_isolated(test_late_tick_underrun_faults, "test_late_tick_underrun_faults");
    run_isolated(test_slow_motion_drains_without_fault, "test_slow_motion_drains_without_fault");
    run_isolated(test_huge_host_stall_resyncs_rate_timer, "test_huge_host_stall_resyncs_rate_timer");
    run_isolated(test_moderate_repeated_lateness_replays_without_fault, "test_moderate_repeated_lateness_replays_without_fault");

    printf("============================================================\n");
    printf("  All 31 scenarios passed successfully!\n");
    printf("============================================================\n");
    return 0;
}
