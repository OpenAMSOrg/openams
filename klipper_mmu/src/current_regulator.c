// SPDX-License-Identifier: GPL-3.0-or-later
#include "klipper_mmu/current_regulator.h"
#include <string.h>

km_result_t km_current_regulator_init(km_current_regulator_t *r, uint16_t target,
                                      uint16_t limit, uint16_t base, uint32_t age)
{
    if (!r || !target || target >= limit || base > 1000 || !age || age > 60000)
        return KM_ERR_INVALID_PARAM;
    memset(r, 0, sizeof(*r));
    r->enabled = true; r->target_current_ma = target; r->current_limit_ma = limit;
    r->base_duty_permille = r->current_duty_permille = base;
    r->max_duty_permille = 1000; r->max_sample_age_ms = age;
    return KM_OK;
}

static km_result_t trip(km_current_regulator_t *r, km_fault_t fault, uint16_t *out)
{
    r->fault = fault; r->current_duty_permille = 0; *out = 0;
    return KM_ERR_FAULTED;
}

km_result_t km_current_regulator_update_sample(km_current_regulator_t *r, uint16_t measured,
                                               bool valid, uint32_t sample, uint32_t now, uint16_t *out)
{
    if (!r || !out) return KM_ERR_INVALID_PARAM;
    *out = 0;
    if (r->fault != KM_FAULT_NONE) return trip(r, r->fault, out);
    if (!r->enabled) return KM_OK;
    if (!valid || km_time_before(now, sample) || km_time_elapsed(now, sample) > r->max_sample_age_ms
        || (r->sample_seen && km_time_before(sample, r->last_sample_time_ms)))
        return trip(r, KM_FAULT_SENSOR_FAILURE, out);
    /* Protect even at zero commanded duty. A severe first sample trips now. */
    if ((uint32_t)measured * 2u > (uint32_t)r->current_limit_ma * 3u)
        return trip(r, KM_FAULT_DRIVER_OVERCURRENT, out);
    if (measured > r->current_limit_ma) {
        if (!r->overcurrent) { r->overcurrent = true; r->overcurrent_start_ms = now; }
        else if (km_time_elapsed(now, r->overcurrent_start_ms) >= 100u)
            return trip(r, KM_FAULT_DRIVER_OVERCURRENT, out);
        r->current_duty_permille = 0; /* Cut output during qualification, too. */
        return KM_OK;
    }
    r->overcurrent = false;
    if (r->sample_seen && sample == r->last_sample_time_ms) {
        *out = r->current_duty_permille;
        return KM_OK; /* One sample never integrates repeatedly. */
    }
    uint32_t dt = r->sample_seen ? km_time_elapsed(sample, r->last_sample_time_ms) : 0;
    if (dt > r->max_sample_age_ms) dt = r->max_sample_age_ms;
    r->sample_seen = true; r->last_sample_time_ms = sample;
    if (!r->base_duty_permille) { r->current_duty_permille = 0; r->integral_term = 0; return KM_OK; }
    int32_t error = (int32_t)r->target_current_ma - measured;
    /* Kp=0.2 permille/mA; Ki=0.05 permille/(mA*s). Integral stores
     * milli-permille, so behavior is independent of control callback frequency. */
    int64_t integral = (int64_t)r->integral_term + (int64_t)error * dt * 50 / 1000;
    if (integral > 300000) integral = 300000;
    if (integral < -300000) integral = -300000;
    int32_t duty = (int32_t)r->base_duty_permille + error * 200 / 1000 + (int32_t)(integral / 1000);
    if (duty > r->max_duty_permille) duty = r->max_duty_permille;
    else if (duty < r->min_duty_permille) duty = r->min_duty_permille;
    else r->integral_term = (int32_t)integral; /* Do not wind up against output saturation. */
    r->current_duty_permille = (uint16_t)duty; *out = (uint16_t)duty;
    return KM_OK;
}

km_result_t km_current_regulator_update(km_current_regulator_t *r, uint16_t ma,
                                        bool valid, uint32_t now, uint16_t *out)
{
    /* Synchronous fresh-acquisition convenience. Cached samples use _sample. */
    return km_current_regulator_update_sample(r, ma, valid, now, now, out);
}

void km_current_regulator_reset(km_current_regulator_t *r)
{
    if (!r) return;
    r->integral_term = 0; r->overcurrent = false; r->sample_seen = false;
    r->overcurrent_start_ms = 0; r->last_sample_time_ms = 0;
    r->current_duty_permille = 0; r->fault = KM_FAULT_NONE;
}
