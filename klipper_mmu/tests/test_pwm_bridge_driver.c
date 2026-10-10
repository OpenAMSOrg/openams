// SPDX-License-Identifier: GPL-3.0-or-later
#include "klipper_mmu/pwm_bridge_driver.h"
#include "klipper_mmu/operation_core.h"
#include "mock_driver.h"
#include "fake_clock.h"
#include <assert.h>
#include <string.h>
#include <stdio.h>

/* Chronological HAL calls are recorded independently of driver state. */
typedef struct { char kind; km_direction_t channel; uint16_t value; uint32_t time; } output_t;
typedef struct {
    output_t history[256];
    unsigned count;
    uint32_t now;
    bool ready;
    km_fault_t injected_fault;
    bool force_disabled_called;
    bool enable_state;
    uint16_t duty_fwd;
    uint16_t duty_rev;

    km_result_t inject_write_enable_err;
    km_result_t inject_write_pwm_err;
    km_result_t inject_read_current_err;

    bool has_current;
    uint16_t read_current_ma;

    bool ever_both_energized;
    bool ever_nonzero_while_disabled;
} fake_io_ctx_t;

static void record(fake_io_ctx_t *ctx, char kind, km_direction_t ch, uint16_t value) {
    assert(ctx->count < 256);
    ctx->history[ctx->count++] = (output_t){kind, ch, value, ctx->now};
}

static void fake_force_disable(void *context) {
    fake_io_ctx_t *ctx = context;
    record(ctx, 'F', KM_DIR_FORWARD, 0);
    ctx->force_disabled_called = true;
    ctx->enable_state = false;
}

static km_result_t fake_write_enable(void *context, bool enabled) {
    fake_io_ctx_t *ctx = context;
    if (ctx->inject_write_enable_err != KM_OK) return ctx->inject_write_enable_err;
    if (enabled) assert(ctx->duty_fwd == 0 && ctx->duty_rev == 0);
    record(ctx, 'E', KM_DIR_FORWARD, enabled ? 1 : 0);
    ctx->enable_state = enabled;
    return KM_OK;
}

static km_result_t fake_write_pwm(void *context, km_direction_t channel, uint16_t duty) {
    fake_io_ctx_t *ctx = context;
    if (ctx->inject_write_pwm_err != KM_OK) return ctx->inject_write_pwm_err;

    assert(channel == KM_DIR_FORWARD || channel == KM_DIR_REVERSE);
    if (duty) {
        assert(ctx->enable_state);
        assert((channel == KM_DIR_FORWARD ? ctx->duty_rev : ctx->duty_fwd) == 0);
    }
    record(ctx, 'P', channel, duty);
    if (channel == KM_DIR_FORWARD) ctx->duty_fwd = duty;
    else ctx->duty_rev = duty;

    if (ctx->duty_fwd > 0 && ctx->duty_rev > 0) ctx->ever_both_energized = true;
    if (!ctx->enable_state && (ctx->duty_fwd > 0 || ctx->duty_rev > 0)) ctx->ever_nonzero_while_disabled = true;

    return KM_OK;
}

static uint32_t fake_now_us(void *context) {
    return ((fake_io_ctx_t *)context)->now;
}

static bool fake_ready(void *context) {
    return ((fake_io_ctx_t *)context)->ready;
}

static km_fault_t fake_fault(void *context) {
    return ((fake_io_ctx_t *)context)->injected_fault;
}

static km_result_t fake_read_current(void *context, uint16_t *ma) {
    fake_io_ctx_t *ctx = context;
    if (!ctx->has_current) return KM_ERR_NOT_SUPPORTED;
    if (ctx->inject_read_current_err != KM_OK) return ctx->inject_read_current_err;
    *ma = ctx->read_current_ma;
    return KM_OK;
}

static const km_pwm_bridge_io_t fake_io = {
    .force_disable = fake_force_disable,
    .write_enable = fake_write_enable,
    .write_pwm = fake_write_pwm,
    .now_us = fake_now_us,
    .ready = fake_ready,
    .fault = fake_fault,
    .read_current_ma = fake_read_current
};

static void test_standalone(void) {
    km_pwm_bridge_driver_t driver;
    fake_io_ctx_t ctx;
    memset(&ctx, 0, sizeof(ctx));
    ctx.ready = true;

    km_pwm_bridge_config_t cfg = {
        .maximum_duty_permille = 1000,
        .reversal_deadtime_us = 5000,
        .maximum_call_lateness_us = 100
    };

    // null/alias init
    assert(km_pwm_bridge_init(NULL, &cfg, &fake_io, &ctx) != KM_OK);
    assert(km_pwm_bridge_init(&driver, NULL, &fake_io, &ctx) != KM_OK);
    assert(km_pwm_bridge_init(&driver, &cfg, NULL, &ctx) != KM_OK);

    assert(km_pwm_bridge_init(&driver, &cfg, &fake_io, &ctx) == KM_OK);
    assert(ctx.force_disabled_called);
    assert(driver.base.caps.supports_pwm);
    assert(driver.base.caps.supports_current_measurement);
    assert(!driver.base.caps.supports_step_dir);

    // write enable/duty
    ctx.now += 100;
    assert(km_driver_set_enable(&driver.base, true) == KM_OK);
    assert(ctx.enable_state);
    assert(ctx.history[ctx.count-3].kind == 'P' && ctx.history[ctx.count-3].channel == KM_DIR_FORWARD && ctx.history[ctx.count-3].value == 0);
    assert(ctx.history[ctx.count-2].kind == 'P' && ctx.history[ctx.count-2].channel == KM_DIR_REVERSE && ctx.history[ctx.count-2].value == 0);
    assert(ctx.history[ctx.count-1].kind == 'E' && ctx.history[ctx.count-1].value == 1);
    assert(ctx.duty_fwd == 0 && ctx.duty_rev == 0);

    ctx.now += 100;
    assert(km_driver_set_direction(&driver.base, KM_DIR_FORWARD, ctx.now) == KM_OK);
    ctx.now += 100;
    assert(km_driver_set_pwm_duty(&driver.base, 500) == KM_OK);
    assert(ctx.duty_fwd == 500 && ctx.duty_rev == 0);
    assert(!ctx.ever_both_energized);
    assert(!ctx.ever_nonzero_while_disabled);

    // reject direction change while energized
    ctx.now += 100;
    assert(driver.base.ops->set_direction(&driver.base, KM_DIR_REVERSE, ctx.now) == KM_ERR_BUSY);
    assert(km_driver_get_fault(&driver.base) == KM_FAULT_NONE && ctx.duty_fwd == 500);
    assert(km_driver_set_enable(&driver.base, false) == KM_OK);

    // re-enable
    ctx.now += 100;
    assert(km_driver_set_enable(&driver.base, true) == KM_OK);
    assert(km_driver_set_pwm_duty(&driver.base, 0) == KM_OK);
    assert(km_driver_set_direction(&driver.base, KM_DIR_REVERSE, ctx.now) == KM_OK);

    // test deadtime
    ctx.now += 5000;
    assert(km_driver_set_pwm_duty(&driver.base, 500) == KM_OK); // REVERSE on
    assert(ctx.duty_rev == 500);
    ctx.now += 100;
    assert(km_driver_set_pwm_duty(&driver.base, 0) == KM_OK); // off

    ctx.now += 100;
    assert(km_driver_set_direction(&driver.base, KM_DIR_FORWARD, ctx.now) == KM_OK);
    ctx.now += 100;
    // too early for opposite direction
    assert(km_driver_set_pwm_duty(&driver.base, 500) == KM_ERR_BUSY);

    // wait deadtime
    ctx.now += 5000;
    assert(km_driver_set_enable(&driver.base, true) == KM_OK);
    assert(km_driver_set_pwm_duty(&driver.base, 500) == KM_OK);

    // Start a fresh instance at an arbitrary epoch; never alter backend clocks.
    km_driver_shutdown(&driver.base);
    ctx.now = UINT32_MAX - 20u;
    assert(km_pwm_bridge_init(&driver, &cfg, &fake_io, &ctx) == KM_OK);
    km_pwm_bridge_service(&driver);
    ctx.now = 10;
    km_pwm_bridge_service(&driver);
    assert(km_driver_get_fault(&driver.base) == KM_FAULT_NONE);
    assert(km_driver_set_direction(&driver.base, KM_DIR_FORWARD, ctx.now - 101u) == KM_ERR_FAULTED);
    assert(!ctx.enable_state);
    assert(km_driver_clear_fault(&driver.base) == KM_OK);

    // external fault
    ctx.now += 100;
    assert(km_driver_set_enable(&driver.base, true) == KM_OK);
    ctx.injected_fault = KM_FAULT_DRIVER_FAULT;
    km_pwm_bridge_service(&driver);
    assert(km_driver_get_fault(&driver.base) == KM_FAULT_DRIVER_FAULT);
    ctx.injected_fault = KM_FAULT_NONE;
    assert(km_driver_set_enable(&driver.base, false) == KM_OK);
    assert(km_driver_clear_fault(&driver.base) == KM_OK);
}

static void test_core_unload(void) {
    km_operation_core_t core;
    km_device_profile_t profile;
    km_profile_init_boxturtle(&profile);
    profile.lane_count = 1;
    profile.has_powered_rewind = true;
    profile.has_current_sensing = false;
    profile.rewind_pwm_duty_permille = 300;

    assert(km_core_init(&core, &profile) == KM_OK);

    mock_driver_t mock_feeder;
    memset(&mock_feeder, 0, sizeof(mock_feeder));
    mock_driver_init(&mock_feeder, 2, 10, false);

    km_pwm_bridge_driver_t rewinder;
    fake_io_ctx_t ctx;
    memset(&ctx, 0, sizeof(ctx));
    ctx.ready = true;
    ctx.has_current = false;
    km_pwm_bridge_config_t cfg = {1000, 5000, 100};
    assert(km_pwm_bridge_init(&rewinder, &cfg, &fake_io, &ctx) == KM_OK);

    assert(km_core_bind_lane_driver(&core, 0, &mock_feeder.base, &rewinder.base) == KM_OK);

    km_core_set_sensors_at(&core, 0, false, true, false, true, 1);
    km_core_host_comm_ready(&core, 1);

    // Command and physical HAL clocks share one epoch.
    ctx.now = 1000;
    km_admission_result_t ar = km_core_request_unload(&core, KM_CLIENT_HOST, 1, 1, (km_core_timepoint_t){1, 1000});
    assert(ar == KM_REQ_ACCEPTED);
    assert(core.op_state == KM_OP_STATE_UNLOADING);

    km_fake_clock_t clock;
    km_fake_clock_init(&clock, ctx.now);
    ctx.now = 2000;
    assert(km_fake_clock_run_trajectory(&clock, &core.trajectory, ctx.now) == KM_OK);
    km_pwm_bridge_service(&rewinder);
    km_core_update(&core, 2, ctx.now);
    assert(rewinder.base.enabled);
    assert(rewinder.base.direction == KM_DIR_REVERSE);
    assert(rewinder.base.pwm_duty_permille == 300);
    assert(ctx.duty_rev == 300);

    // Cancel immediately removes rewind drive; feeder keeps its physical ramp.
    ctx.now = 3000;
    assert(km_fake_clock_run_trajectory(&clock, &core.trajectory, ctx.now) == KM_OK);
    assert(km_core_request_cancel5(&core, KM_CLIENT_HOST, 1, 1, (km_core_timepoint_t){3, 3000}) == KM_REQ_ACCEPTED);
    km_pwm_bridge_service(&rewinder);
    km_core_update(&core, 3, 3000);

    assert(!rewinder.base.enabled);
    assert(ctx.enable_state == false);
    assert(ctx.duty_rev == 0);
    for (unsigned i = 0; i < 100 && core.shared_path_owned; ++i) {
        ctx.now += 1000;
        assert(km_fake_clock_run_trajectory(&clock, &core.trajectory, ctx.now) == KM_OK);
        km_pwm_bridge_service(&rewinder);
        km_core_update(&core, ctx.now / 1000u, ctx.now);
    }
    assert(!core.shared_path_owned && !mock_feeder.base.enabled);
    km_event_t event;
    assert(km_event_queue_pop(&core.event_queue, &event) == KM_OK);
    assert(event.type == KM_EVENT_UNLOAD_CANCELLED && event.req_id == 1 && event.session_id == 1);
}

int main(void) {
    test_standalone();
    test_core_unload();
    printf("OK\n");
    return 0;
}
