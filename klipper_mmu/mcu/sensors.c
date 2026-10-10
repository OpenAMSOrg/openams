// SPDX-License-Identifier: GPL-3.0-or-later
#include "sensors.h"
#include "switch_gpio.h"
#include "adc.h"
#include "autoconf.h"
#include "command.h"
#include "sched.h"

/* WANT_ADC alone defaults on for every non-limited-code-size STM32 part
 * (upstream src/Kconfig); it does not mean this image can ever produce an
 * ANALOG_DIFF buffer sample (only WANT_ADC_LOCAL's H723 diff-buffer path
 * links mcu/adc.c/src/adc_pair.c -- mcu/Makefile). Set 0 there so this file
 * stops calling into them on an image that never links them. */
#ifndef KM_DIFF_BUFFER_BUILD
#define KM_DIFF_BUFFER_BUILD 1
#endif

static km_operation_core_t *bound_core;
static bool stopped;

static void invalidate(km_buffer_sensor_t *buffer)
{
    buffer->is_valid = false;
    buffer->fault = KM_FAULT_BUFFER_SIGNAL;
}

void km_sensors_shutdown(void)
{
    stopped = true;
    if (bound_core) invalidate(&bound_core->buffer_sensor);
}
DECL_SHUTDOWN(km_sensors_shutdown);

void km_sensors_sample(km_operation_core_t *core, void *context)
{
    /* Control task context: only the ADC timer runs meanwhile, and its
     * accessor copies one complete pair under its own lock. */
    km_switch_gpio_sample(core, context);
    bound_core = core;
    km_buffer_sensor_t *buffer = &core->buffer_sensor;
    if (stopped || sched_is_shutdown()) {
        invalidate(buffer);
    } else if (buffer->type == KM_BUFFER_SENSOR_ANALOG_DIFF) {
        invalidate(buffer);
#if KM_DIFF_BUFFER_BUILD && CONFIG_WANT_ADC
        /* A pair completed during a decode supersedes it: decode that once. */
        for (unsigned decode = 0u; decode < 2u; ++decode) {
            km_adc_pair_sample_t sample;
            km_core_timepoint_t query_time;
            if (buffer->diff_config.adc_max_counts != KM_ADC_MAX_RAW
                || km_adc_get_sample_at(&sample, &query_time) != KM_OK
                || km_buffer_update_adc_pair(buffer, &sample, query_time.ms, query_time.us) != KM_OK)
                break;
            uint32_t attempt_us = sample.a_attempt_us;
            /* Decoding costs time. Recheck the ADC's finer age limit after it,
             * without refreshing the acquisition timestamp. */
            bool queried = km_adc_get_sample_at(&sample, &query_time) == KM_OK;
            bool same = queried && sample.a_attempt_us == attempt_us;
            if (!same || !sample.is_valid || sample.result != KM_OK
                || sample.present_mask != 3u)
                invalidate(buffer);
            else
                (void)km_buffer_check_freshness(buffer, query_time.ms);
            if (same || !queried)
                break;
        }
#endif
    }
}
