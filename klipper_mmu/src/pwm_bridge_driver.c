// SPDX-License-Identifier: GPL-3.0-or-later
#include "klipper_mmu/pwm_bridge_driver.h"
#include <string.h>

static void latch(km_pwm_bridge_driver_t *d, km_fault_t fault)
{
    if (d->latched_fault == KM_FAULT_NONE)
        d->latched_fault = fault == KM_FAULT_NONE ? KM_FAULT_DRIVER_FAULT : fault;
}

/* No I/O or recursive shutdown here. Off age saturates before raw timer wrap. */
static bool observe(km_pwm_bridge_driver_t *d)
{
    if (!d->io.now_us) return false;
    uint32_t now = d->io.now_us(d->context);
    if (d->clock_seen) {
        uint32_t elapsed = now - d->last_call_us;
        if (elapsed >= UINT32_C(2147483648)) {
            latch(d, KM_FAULT_DRIVER_FAULT);
            d->off_tracking = false;
            d->off_age_us = 0;
            return false;
        }
        if (d->off_tracking) {
            uint64_t age = (uint64_t)d->off_age_us + elapsed;
            d->off_age_us = age < d->config.reversal_deadtime_us
                ? (uint32_t)age : d->config.reversal_deadtime_us;
        }
    }
    d->last_call_us = now;
    d->clock_seen = true;
    return true;
}

/* Called only after an actual disable or confirmed zero outputs, never before. */
static bool observed_off(km_pwm_bridge_driver_t *d)
{
    if (!observe(d)) return false;
    if (!d->off_tracking) {
        d->off_age_us = 0;
        d->off_tracking = true;
    }
    d->last_output_us = d->last_call_us;
    return true;
}

static bool safe_stop(km_pwm_bridge_driver_t *d)
{
    bool io_ok = true;
    bool time_ok = true;
    /* The enable interlock is removed before touching potentially failing PWM. */
    if (d->io.force_disable) {
        d->io.force_disable(d->context);
        time_ok = observed_off(d);
    } else {
        io_ok = false;
    }
    d->base.enabled = false;
    d->base.pwm_duty_permille = 0;
    d->base.step_pin_high = false;
    if (d->io.write_pwm) {
        if (d->io.write_pwm(d->context, KM_DIR_FORWARD, 0) != KM_OK) {
            latch(d, KM_FAULT_DRIVER_FAULT); io_ok = false;
        }
        if (!observe(d)) time_ok = false;
        if (d->io.write_pwm(d->context, KM_DIR_REVERSE, 0) != KM_OK) {
            latch(d, KM_FAULT_DRIVER_FAULT); io_ok = false;
        }
        if (!observe(d)) time_ok = false;
    } else {
        io_ok = false;
    }
    if (time_ok) d->last_output_us = d->last_call_us;
    if (!io_ok) latch(d, KM_FAULT_DRIVER_FAULT);
    return io_ok && time_ok;
}

void km_pwm_bridge_trip(km_pwm_bridge_driver_t *d, km_fault_t fault)
{
    if (!d) return;
    latch(d, fault);
    (void)safe_stop(d);
}

/* Physical status is ingested here. get_fault itself is a cached, pure query. */
static km_result_t health(km_pwm_bridge_driver_t *d)
{
    km_fault_t physical = d->io.fault ? d->io.fault(d->context) : KM_FAULT_NONE;
    if (physical != KM_FAULT_NONE) latch(d, physical);
    bool ready = d->io.ready(d->context);
    if (!ready && d->base.enabled) latch(d, KM_FAULT_DRIVER_FAULT);
    if (d->latched_fault != KM_FAULT_NONE) {
        (void)safe_stop(d);
        return KM_ERR_FAULTED;
    }
    return ready ? KM_OK : KM_ERR_UNAVAILABLE;
}

static km_result_t prepare(km_pwm_bridge_driver_t *d, bool require_ready)
{
    if (!d->base.initialized) { (void)safe_stop(d); return KM_ERR_UNAVAILABLE; }
    if (!observe(d)) { (void)safe_stop(d); return KM_ERR_FAULTED; }
    km_result_t result = health(d);
    if (result == KM_ERR_FAULTED) return result;
    if (require_ready && result != KM_OK)
        return safe_stop(d) ? result : KM_ERR_FAULTED;
    return KM_OK;
}

void km_pwm_bridge_service(km_pwm_bridge_driver_t *d)
{
    if (d && d->base.initialized) (void)prepare(d, false);
}

/* Stop at the first failure so the caller force-disables before further writes. */
static bool zero_pair(km_pwm_bridge_driver_t *d)
{
    if (d->io.write_pwm(d->context, KM_DIR_FORWARD, 0) != KM_OK) return false;
    if (!observe(d)) return false;
    if (d->io.write_pwm(d->context, KM_DIR_REVERSE, 0) != KM_OK) return false;
    if (!observed_off(d)) return false;
    d->base.pwm_duty_permille = 0;
    return true;
}

static km_result_t write_failure(km_pwm_bridge_driver_t *d)
{
    km_pwm_bridge_trip(d, KM_FAULT_DRIVER_FAULT);
    return KM_ERR_FAULTED;
}

static km_result_t set_enable(km_driver_t *base, bool enabled)
{
    km_pwm_bridge_driver_t *d = (km_pwm_bridge_driver_t *)base;
    if (!enabled) return safe_stop(d) ? KM_OK : KM_ERR_FAULTED;
    km_result_t result = prepare(d, true);
    if (result != KM_OK) return result;
    if (base->enabled) return KM_OK;
    if (!zero_pair(d)) return write_failure(d);
    if ((result = health(d)) != KM_OK)
        return safe_stop(d) ? result : KM_ERR_FAULTED;
    if (d->io.write_enable(d->context, true) != KM_OK) return write_failure(d);
    base->enabled = true;
    if (!observe(d)) return write_failure(d);
    d->last_output_us = d->last_call_us;
    result = health(d);
    return result;
}

static km_result_t set_direction(km_driver_t *base, km_direction_t dir, uint32_t supplied)
{
    km_pwm_bridge_driver_t *d = (km_pwm_bridge_driver_t *)base;
    if (dir != KM_DIR_FORWARD && dir != KM_DIR_REVERSE) return KM_ERR_INVALID_PARAM;
    if (base->pwm_duty_permille) return KM_ERR_BUSY;
    km_result_t result = prepare(d, true);
    if (result != KM_OK) return result;
    /* Cached-status callbacks can take time too; measure at the selection. */
    if (!observe(d) || d->last_call_us - supplied > d->config.maximum_call_lateness_us)
        return write_failure(d);
    base->direction = dir;
    d->last_output_us = d->last_call_us;
    return KM_OK;
}

static km_result_t set_pwm(km_driver_t *base, uint16_t duty)
{
    km_pwm_bridge_driver_t *d = (km_pwm_bridge_driver_t *)base;
    km_result_t result = prepare(d, duty != 0);
    if (result != KM_OK) return result;
    if (duty > d->config.maximum_duty_permille) {
        return safe_stop(d) ? KM_ERR_INVALID_PARAM : KM_ERR_FAULTED;
    }
    if (!duty) {
        if (!zero_pair(d)) return write_failure(d);
        result = health(d);
        return result == KM_ERR_UNAVAILABLE ? KM_OK : result;
    }
    if (!base->enabled) return safe_stop(d) ? KM_ERR_UNAVAILABLE : KM_ERR_FAULTED;
    if (base->direction != KM_DIR_FORWARD && base->direction != KM_DIR_REVERSE)
        return write_failure(d);
    if (d->has_been_energized && d->last_energized_direction != base->direction
        && (!d->off_tracking || d->off_age_us < d->config.reversal_deadtime_us)) {
        return safe_stop(d) ? KM_ERR_BUSY : KM_ERR_FAULTED;
    }
    km_direction_t opposite = base->direction == KM_DIR_FORWARD ? KM_DIR_REVERSE : KM_DIR_FORWARD;
    if (d->io.write_pwm(d->context, opposite, 0) != KM_OK) return write_failure(d);
    if (!observe(d)) return write_failure(d);
    if ((result = health(d)) != KM_OK)
        return safe_stop(d) ? result : KM_ERR_FAULTED;
    /* A failed HAL write may have reached hardware. Retain conservative history
     * before issuing it so any later reversal still requires a full off interval. */
    d->has_been_energized = true;
    d->last_energized_direction = base->direction;
    d->off_tracking = false;
    d->off_age_us = 0;
    if (d->io.write_pwm(d->context, base->direction, duty) != KM_OK) return write_failure(d);
    base->pwm_duty_permille = duty;
    if (!observe(d)) return write_failure(d);
    d->last_output_us = d->last_call_us;
    return health(d);
}

static bool has_current(const km_driver_t *base)
{
    return ((const km_pwm_bridge_driver_t *)base)->io.read_current_ma != NULL;
}
static km_result_t get_current(const km_driver_t *base, uint16_t *out)
{
    const km_pwm_bridge_driver_t *d = (const km_pwm_bridge_driver_t *)base;
    if (!out) return KM_ERR_INVALID_PARAM;
    if (!base->initialized) return KM_ERR_UNAVAILABLE;
    if (!d->io.read_current_ma) return KM_ERR_NOT_SUPPORTED;
    uint16_t measured = 0;
    km_result_t result = d->io.read_current_ma(d->context, &measured);
    if (result == KM_OK) *out = measured;
    return result;
}
static km_fault_t get_fault(const km_driver_t *base)
{
    return ((const km_pwm_bridge_driver_t *)base)->latched_fault;
}
static km_result_t clear_fault(km_driver_t *base)
{
    km_pwm_bridge_driver_t *d = (km_pwm_bridge_driver_t *)base;
    if (!base->initialized) return KM_ERR_UNAVAILABLE;
    if (base->enabled || base->pwm_duty_permille) return KM_ERR_BUSY;
    /* Recovery deliberately reseeds time, but only after confirmed safe I/O. */
    d->io.force_disable(d->context);
    km_result_t a = d->io.write_pwm(d->context, KM_DIR_FORWARD, 0);
    km_result_t b = d->io.write_pwm(d->context, KM_DIR_REVERSE, 0);
    if (a != KM_OK || b != KM_OK) { latch(d, KM_FAULT_DRIVER_FAULT); return KM_ERR_FAULTED; }
    km_fault_t physical = d->io.fault ? d->io.fault(d->context) : KM_FAULT_NONE;
    if (physical != KM_FAULT_NONE) { latch(d, physical); return KM_ERR_FAULTED; }
    if (!d->io.ready(d->context)) return KM_ERR_UNAVAILABLE;
    d->last_call_us = d->io.now_us(d->context);
    d->last_output_us = d->last_call_us;
    d->clock_seen = d->off_tracking = true;
    d->off_age_us = 0; /* Never invent pre-recovery dead time. */
    d->latched_fault = KM_FAULT_NONE;
    return KM_OK;
}
static void stop_outputs(km_driver_t *base)
{
    (void)safe_stop((km_pwm_bridge_driver_t *)base);
}
static const km_driver_ops_t ops = {
    .set_enable=set_enable, .set_direction=set_direction, .set_pwm_duty=set_pwm,
    .has_measured_current=has_current, .get_measured_current_ma=get_current,
    .get_fault=get_fault, .clear_fault=clear_fault, .stop_outputs=stop_outputs
};

km_result_t km_pwm_bridge_config_validate(const km_pwm_bridge_config_t *config)
{
    return config && config->maximum_duty_permille && config->maximum_duty_permille <= 1000
        && config->reversal_deadtime_us && config->reversal_deadtime_us < UINT32_C(2147483648)
        && config->maximum_call_lateness_us && config->maximum_call_lateness_us <= 1000
        ? KM_OK : KM_ERR_INVALID_PARAM;
}

km_result_t km_pwm_bridge_init(km_pwm_bridge_driver_t *d,
    const km_pwm_bridge_config_t *config, const km_pwm_bridge_io_t *io, void *context)
{
    km_pwm_bridge_config_t saved = {0}; km_pwm_bridge_io_t saved_io = {0};
    if (config) saved = *config;
    if (io) saved_io = *io;
    if (!d) {
        if (saved_io.force_disable) saved_io.force_disable(context);
        if (saved_io.write_pwm) {
            (void)saved_io.write_pwm(context, KM_DIR_FORWARD, 0);
            (void)saved_io.write_pwm(context, KM_DIR_REVERSE, 0);
        }
        return KM_ERR_INVALID_PARAM;
    }
    memset(d, 0, sizeof(*d));
    d->config = saved; d->io = saved_io; d->context = context; d->base.ops = &ops;
    d->base.direction = KM_DIR_FORWARD;
    bool valid = config && io && saved_io.force_disable && saved_io.write_enable
        && saved_io.write_pwm && saved_io.now_us && saved_io.ready
        && km_pwm_bridge_config_validate(&saved) == KM_OK;
    if (!valid) latch(d, KM_FAULT_DRIVER_FAULT);
    bool stopped = safe_stop(d);
    if (!valid) return KM_ERR_INVALID_PARAM;
    if (!stopped) return KM_ERR_FAULTED;
    d->base.caps.supports_pwm = true;
    d->base.caps.supports_current_measurement = saved_io.read_current_ma != NULL;
    d->base.initialized = true;
    /* Configuration may be valid while physical readiness is still pending. */
    (void)health(d);
    return KM_OK;
}
