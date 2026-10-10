// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_DRIVER_H
#define KLIPPER_MMU_DRIVER_H

#include "klipper_mmu/types.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct km_driver km_driver_t;

/**
 * Driver hardware capabilities.
 */
typedef struct {
    bool supports_step_dir;
    bool supports_pwm;
    bool supports_current_setpoint;
    bool supports_current_measurement;
    uint16_t min_pulse_width_us;      /* Minimum step pulse high time */
    uint32_t min_step_interval_us;    /* Minimum step cycle time (high + low) */
    uint32_t max_velocity_steps_s;
} km_driver_caps_t;

/**
 * Abstract driver operations table.
 * Supports step edge timing, direction setup/hold, PWM, current regulation,
 * and fail-closed readiness.
 */
typedef struct {
    km_result_t (*init)(km_driver_t *self, const void *config);
    km_result_t (*set_enable)(km_driver_t *self, bool enable);
    km_result_t (*set_direction)(km_driver_t *self, km_direction_t dir, uint32_t now_us);
    km_result_t (*set_pwm_duty)(km_driver_t *self, uint16_t duty_permille);
    km_result_t (*set_current_ma)(km_driver_t *self, uint16_t run_ma, uint16_t hold_ma);
    bool (*has_measured_current)(const km_driver_t *self);
    km_result_t (*get_measured_current_ma)(const km_driver_t *self, uint16_t *measured_ma);
    km_fault_t (*get_fault)(const km_driver_t *self);
    km_result_t (*clear_fault)(km_driver_t *self);
    void (*stop_outputs)(km_driver_t *self);
} km_driver_ops_t;

/**
 * Base driver instance structure.
 */
struct km_driver {
    const km_driver_ops_t *ops;
    km_driver_caps_t caps;
    bool initialized;
    bool enabled;
    km_direction_t direction;
    bool step_pin_high;
    uint16_t run_current_ma;
    uint16_t hold_current_ma;
    uint16_t pwm_duty_permille;
    void *user_data;
};

/* Convenience inline wrappers */
static inline km_fault_t km_driver_get_fault(const km_driver_t *d);
static inline void km_driver_shutdown(km_driver_t *d);

static inline km_result_t km_driver_init(km_driver_t *d, const void *cfg)
{
    if (!d || !d->ops || !d->ops->init) {
        return KM_ERR_INVALID_PARAM;
    }
    if (d->enabled) return KM_ERR_BUSY;
    d->initialized = false;
    km_result_t res = d->ops->init(d, cfg);
    if (res == KM_OK) {
        d->initialized = true;
    } else {
        km_driver_shutdown(d);
    }
    return res;
}

static inline km_result_t km_driver_set_enable(km_driver_t *d, bool enable)
{
    if (!d || !d->ops || !d->ops->set_enable) {
        return KM_ERR_INVALID_PARAM;
    }
    if (enable && (!d->initialized || !d->ops->stop_outputs)) return KM_ERR_UNAVAILABLE;
    if (enable && km_driver_get_fault(d) != KM_FAULT_NONE) {
        km_driver_shutdown(d);
        return KM_ERR_FAULTED;
    }
    km_result_t res = d->ops->set_enable(d, enable);
    if (res == KM_OK) {
        d->enabled = enable;
    } else {
        km_driver_shutdown(d);
    }
    return res;
}

static inline km_result_t km_driver_set_direction(km_driver_t *d, km_direction_t dir, uint32_t now_us)
{
    if (!d || !d->ops) {
        return KM_ERR_INVALID_PARAM;
    }
    if (!d->ops->set_direction) {
        return KM_ERR_NOT_SUPPORTED;
    }
    if (dir != KM_DIR_FORWARD && dir != KM_DIR_REVERSE) return KM_ERR_INVALID_PARAM;
    if (d->step_pin_high || d->pwm_duty_permille) return KM_ERR_BUSY;
    if (km_driver_get_fault(d) != KM_FAULT_NONE) {
        km_driver_shutdown(d);
        return KM_ERR_FAULTED;
    }
    if (!d->initialized) {
        return KM_ERR_UNAVAILABLE;
    }
    km_result_t res = d->ops->set_direction(d, dir, now_us);
    if (res == KM_OK) {
        d->direction = dir;
    }
    return res;
}

static inline km_result_t km_driver_set_pwm_duty(km_driver_t *d, uint16_t duty)
{
    if (!d || !d->ops || !d->ops->set_pwm_duty) {
        return KM_ERR_INVALID_PARAM;
    }
    if (!d->caps.supports_pwm) return KM_ERR_NOT_SUPPORTED;
    if (duty > KM_PERMILLE_MAX) return KM_ERR_INVALID_PARAM;
    if (duty && (!d->initialized || !d->enabled)) return KM_ERR_UNAVAILABLE;
    if (duty && km_driver_get_fault(d) != KM_FAULT_NONE) {
        km_driver_shutdown(d);
        return KM_ERR_FAULTED;
    }
    km_result_t res = d->ops->set_pwm_duty(d, duty);
    if (res == KM_OK) {
        d->pwm_duty_permille = duty;
    } else {
        km_driver_shutdown(d);
    }
    return res;
}

static inline km_result_t km_driver_set_current_ma(km_driver_t *d, uint16_t run_ma, uint16_t hold_ma)
{
    if (!d || !d->ops || !d->ops->set_current_ma) {
        return KM_ERR_INVALID_PARAM;
    }
    if (!d->caps.supports_current_setpoint) return KM_ERR_NOT_SUPPORTED;
    if (!d->initialized) return KM_ERR_UNAVAILABLE;
    if (hold_ma > run_ma) return KM_ERR_INVALID_PARAM;
    if (km_driver_get_fault(d) != KM_FAULT_NONE) {
        km_driver_shutdown(d);
        return KM_ERR_FAULTED;
    }
    km_result_t res = d->ops->set_current_ma(d, run_ma, hold_ma);
    if (res == KM_OK) {
        d->run_current_ma = run_ma;
        d->hold_current_ma = hold_ma;
    }
    return res;
}

static inline bool km_driver_has_measured_current(const km_driver_t *d)
{
    return (d && d->initialized && d->caps.supports_current_measurement
            && d->ops && d->ops->has_measured_current)
        ? d->ops->has_measured_current(d) : false;
}

static inline km_result_t km_driver_get_measured_current_ma(const km_driver_t *d, uint16_t *measured_ma)
{
    if (!measured_ma) return KM_ERR_INVALID_PARAM;
    return (km_driver_has_measured_current(d) && d->ops->get_measured_current_ma)
        ? d->ops->get_measured_current_ma(d, measured_ma) : KM_ERR_NOT_SUPPORTED;
}

static inline km_fault_t km_driver_get_fault(const km_driver_t *d)
{
    return (d && d->ops && d->ops->get_fault) ? d->ops->get_fault(d) : KM_FAULT_NONE;
}

static inline km_result_t km_driver_clear_fault(km_driver_t *d)
{
    return (d && d->ops && d->ops->clear_fault) ? d->ops->clear_fault(d) : KM_ERR_INVALID_PARAM;
}

static inline void km_driver_shutdown(km_driver_t *d)
{
    if (d) {
        if (d->ops && d->ops->stop_outputs) {
            d->ops->stop_outputs(d);
        }
        d->enabled = false;
        d->step_pin_high = false;
        d->pwm_duty_permille = 0;
    }
}

#ifdef __cplusplus
}
#endif

#endif /* KLIPPER_MMU_DRIVER_H */
