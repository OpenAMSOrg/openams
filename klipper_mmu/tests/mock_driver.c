// SPDX-License-Identifier: GPL-3.0-or-later
#include "mock_driver.h"

static km_result_t mock_init(km_driver_t *self, const void *config)
{
    (void)config;
    if (!self) {
        return KM_ERR_INVALID_PARAM;
    }
    mock_driver_t *m = (mock_driver_t *)self;
    m->initialized = true;
    m->step_pin_high = false;
    m->last_service_time_us = 0;
    m->injected_step_edge_result = KM_OK;
    self->initialized = true;
    self->enabled = false;
    self->step_pin_high = false;
    return KM_OK;
}

static km_result_t mock_set_enable(km_driver_t *self, bool enable)
{
    if (!self) {
        return KM_ERR_INVALID_PARAM;
    }
    self->enabled = enable;
    return KM_OK;
}

static km_result_t mock_set_direction(km_driver_t *self, km_direction_t dir, uint32_t now_us)
{
    if (!self) {
        return KM_ERR_INVALID_PARAM;
    }
    mock_driver_t *m = (mock_driver_t *)self;
    if (m->injected_fault != KM_FAULT_NONE) {
        return KM_ERR_FAULTED;
    }

    if (m->last_service_time_us > 0 && km_time_before(now_us, m->last_service_time_us)) {
        return KM_ERR_INVALID_PARAM;
    }
    m->last_service_time_us = now_us;


    self->direction = dir;
    m->last_dir_change_us = now_us;
    return KM_OK;
}


static km_result_t mock_set_pwm_duty(km_driver_t *self, uint16_t duty_permille)
{
    if (!self) {
        return KM_ERR_INVALID_PARAM;
    }
    self->pwm_duty_permille = duty_permille;
    return KM_OK;
}

static km_result_t mock_set_current_ma(km_driver_t *self, uint16_t run_ma, uint16_t hold_ma)
{
    if (!self) {
        return KM_ERR_INVALID_PARAM;
    }
    self->run_current_ma = run_ma;
    self->hold_current_ma = hold_ma;
    return KM_OK;
}

static bool mock_has_measured_current(const km_driver_t *self)
{
    if (!self) {
        return false;
    }
    const mock_driver_t *m = (const mock_driver_t *)self;
    return m->has_measured_current_hw;
}

static km_result_t mock_get_measured_current_ma(const km_driver_t *self, uint16_t *measured_ma)
{
    if (!self || !measured_ma) {
        return KM_ERR_INVALID_PARAM;
    }
    const mock_driver_t *m = (const mock_driver_t *)self;
    if (!m->has_measured_current_hw) {
        return KM_ERR_NOT_SUPPORTED;
    }
    *measured_ma = m->simulated_measured_current_ma;
    return KM_OK;
}

static km_fault_t mock_get_fault(const km_driver_t *self)
{
    if (!self) {
        return KM_FAULT_NONE;
    }
    const mock_driver_t *m = (const mock_driver_t *)self;
    return m->injected_fault;
}

static km_result_t mock_clear_fault(km_driver_t *self)
{
    if (!self) {
        return KM_ERR_INVALID_PARAM;
    }
    mock_driver_t *m = (mock_driver_t *)self;
    m->injected_fault = KM_FAULT_NONE;
    return KM_OK;
}

static void mock_shutdown(km_driver_t *self)
{
    if (!self) {
        return;
    }
    mock_driver_t *m = (mock_driver_t *)self;
    m->shutdown_called = true;
    m->step_pin_high = false;
    self->enabled = false;
    self->step_pin_high = false;
    self->pwm_duty_permille = 0;
}

static const km_driver_ops_t mock_ops = {
    .init = mock_init,
    .set_enable = mock_set_enable,
    .set_direction = mock_set_direction,
    .set_pwm_duty = mock_set_pwm_duty,
    .set_current_ma = mock_set_current_ma,
    .has_measured_current = mock_has_measured_current,
    .get_measured_current_ma = mock_get_measured_current_ma,
    .get_fault = mock_get_fault,
    .clear_fault = mock_clear_fault,
    .stop_outputs = mock_shutdown,
};

void mock_driver_init(mock_driver_t *mock,
                      uint16_t min_pulse_width_us,
                      uint32_t min_step_interval_us,
                      bool has_measured_current_hw)
{
    if (!mock) {
        return;
    }
    mock->base.ops = &mock_ops;
    mock->base.caps.supports_step_dir = true;
    mock->base.caps.supports_pwm = true;
    mock->base.caps.supports_current_setpoint = true;
    mock->base.caps.supports_current_measurement = has_measured_current_hw;
    mock->base.caps.min_pulse_width_us = min_pulse_width_us > 0 ? min_pulse_width_us : 2u;
    mock->base.caps.min_step_interval_us = min_step_interval_us > 0 ? min_step_interval_us : 10u;
    mock->base.caps.max_velocity_steps_s = 20000;
    mock->base.initialized = true;
    mock->base.enabled = false;
    mock->base.direction = KM_DIR_FORWARD;
    mock->base.step_pin_high = false;
    mock->base.run_current_ma = 0;
    mock->base.hold_current_ma = 0;
    mock->base.pwm_duty_permille = 0;
    mock->base.user_data = NULL;

    mock->min_pulse_width_us = mock->base.caps.min_pulse_width_us;
    mock->min_step_interval_us = mock->base.caps.min_step_interval_us;

    mock->step_count = 0;
    mock->rising_edge_count = 0;
    mock->falling_edge_count = 0;
    mock->last_step_time_us = 0;
    mock->last_rising_edge_us = 0;
    mock->last_falling_edge_us = 0;
    mock->last_dir_change_us = 0;
    mock->last_service_time_us = 0;
    mock->min_observed_interval_us = UINT32_MAX;
    mock->min_observed_pulse_width_us = UINT32_MAX;
    mock->step_interval_violations = 0;
    mock->pulse_width_violations = 0;
    mock->disabled_step_attempts = 0;
    mock->step_pin_high = false;

    mock->has_measured_current_hw = has_measured_current_hw;
    mock->simulated_measured_current_ma = 0;
    mock->initialized = true;
    mock->shutdown_called = false;
    mock->injected_fault = KM_FAULT_NONE;
    mock->injected_step_edge_result = KM_OK;
}

void mock_driver_reset_stats(mock_driver_t *mock)
{
    if (!mock) {
        return;
    }
    mock->step_count = 0;
    mock->rising_edge_count = 0;
    mock->falling_edge_count = 0;
    mock->last_step_time_us = 0;
    mock->last_rising_edge_us = 0;
    mock->last_falling_edge_us = 0;
    mock->last_dir_change_us = 0;
    mock->last_service_time_us = 0;
    mock->min_observed_interval_us = UINT32_MAX;
    mock->min_observed_pulse_width_us = UINT32_MAX;
    mock->step_interval_violations = 0;
    mock->pulse_width_violations = 0;
    mock->disabled_step_attempts = 0;
    mock->shutdown_called = false;
    mock->step_pin_high = false;
    mock->injected_step_edge_result = KM_OK;
}
