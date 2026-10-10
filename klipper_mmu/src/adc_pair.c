// SPDX-License-Identifier: GPL-3.0-or-later
#include "klipper_mmu/adc_pair.h"
#include <string.h>

KM_API km_result_t km_adc_pair_config_validate(const km_adc_pair_config_t *config) {
    if (!config || !config->max_raw ||
        !config->conversion_timeout_us || config->conversion_timeout_us >= 0x80000000u ||
        !config->max_pair_span_us || config->max_pair_span_us >= 0x80000000u ||
        !config->max_sample_age_us || config->max_sample_age_us >= 0x80000000u ||
        config->max_sample_age_us < config->max_pair_span_us)
        return KM_ERR_INVALID_PARAM;
    return KM_OK;
}

KM_API km_result_t km_adc_pair_init(km_adc_pair_t *pair, const km_adc_pair_config_t *config, const km_adc_pair_hal_t *hal, void *context, uint32_t now_us) {
    km_adc_pair_hal_t safe_hal = {0};
    km_adc_pair_config_t safe_cfg = {0};
    if (hal) safe_hal = *hal;
    if (config) safe_cfg = *config;
    
    if (!pair) {
        if (safe_hal.cancel) safe_hal.cancel(context);
        return KM_ERR_INVALID_PARAM;
    }
    
    if (!config || !hal || !hal->poll || !hal->cancel) {
        if (safe_hal.cancel) safe_hal.cancel(context);
        memset(pair, 0, sizeof(*pair));
        return KM_ERR_INVALID_PARAM;
    }
    
    if (km_adc_pair_config_validate(&safe_cfg) != KM_OK) {
        safe_hal.cancel(context);
        memset(pair, 0, sizeof(*pair));
        return KM_ERR_INVALID_PARAM;
    }
    
    memset(pair, 0, sizeof(*pair));
    pair->config = safe_cfg;
    pair->hal = safe_hal;
    pair->context = context;
    pair->last_time_us = now_us;
    pair->state = KM_ADC_PAIR_STATE_IDLE;
    return KM_OK;
}

static km_result_t check_time(km_adc_pair_t *pair, uint32_t now_us) {
    if (km_time_before(now_us, pair->last_time_us) || (now_us - pair->last_time_us) > KM_TIME_WRAP_HORIZON_US) {
        pair->state = KM_ADC_PAIR_STATE_FAULT;
        pair->current_sample.is_valid = false;
        pair->has_completed_sample = false;
        if (pair->hal.cancel) pair->hal.cancel(pair->context);
        return KM_ERR_FAULTED;
    }
    if (pair->current_sample.is_valid) {
        if (now_us - pair->current_sample.a_attempt_us > pair->config.max_sample_age_us) {
            pair->current_sample.is_valid = false;
        }
    }
    pair->last_time_us = now_us;
    return KM_OK;
}

static void fail_acquisition(km_adc_pair_t *pair, km_result_t reason) {
    pair->current_sample.is_valid = false;
    pair->current_sample.result = reason;
    pair->current_sample.raw_a = pair->in_prog_raw_a;
    pair->current_sample.raw_b = pair->in_prog_raw_b;
    pair->current_sample.present_mask = pair->in_prog_present_mask;
    
    pair->current_sample.pair_start_us = pair->in_prog_pair_start_us;
    pair->current_sample.a_attempt_us = pair->in_prog_a_attempt_us;
    pair->current_sample.a_complete_us = pair->in_prog_a_complete_us;
    pair->current_sample.b_attempt_us = pair->in_prog_b_attempt_us;
    pair->current_sample.b_complete_us = pair->in_prog_b_complete_us;
    
    pair->has_completed_sample = true;
    pair->hal.cancel(pair->context);
    pair->state = KM_ADC_PAIR_STATE_IDLE;
}

KM_API km_result_t km_adc_pair_begin(km_adc_pair_t *pair, uint32_t now_us) {
    if (!pair || pair->state == KM_ADC_PAIR_STATE_UNINIT) return KM_ERR_INVALID_PARAM;
    
    if (check_time(pair, now_us) != KM_OK) return KM_ERR_FAULTED;
    if (pair->state == KM_ADC_PAIR_STATE_FAULT) return KM_ERR_FAULTED;
    if (pair->state != KM_ADC_PAIR_STATE_IDLE) return KM_ERR_BUSY;
    
    pair->state = KM_ADC_PAIR_STATE_A_POLL;
    pair->in_prog_pair_start_us = now_us;
    pair->in_prog_a_attempt_us = 0;
    pair->in_prog_a_complete_us = 0;
    pair->in_prog_b_attempt_us = 0;
    pair->in_prog_b_complete_us = 0;
    pair->in_prog_present_mask = 0;
    pair->in_prog_raw_a = 0;
    pair->in_prog_raw_b = 0;
    pair->span_deadline_us = now_us + pair->config.max_pair_span_us;
    pair->channel_deadline_us = now_us + pair->config.conversion_timeout_us;
    
    return KM_OK;
}

KM_API km_result_t km_adc_pair_service(km_adc_pair_t *pair, uint32_t now_us) {
    if (!pair || pair->state == KM_ADC_PAIR_STATE_UNINIT) return KM_ERR_INVALID_PARAM;
    
    if (check_time(pair, now_us) != KM_OK) return KM_ERR_FAULTED;
    if (pair->state == KM_ADC_PAIR_STATE_FAULT) return KM_ERR_FAULTED;
    if (pair->state == KM_ADC_PAIR_STATE_IDLE) return KM_OK;
    
    if (km_time_after_or_eq(now_us, pair->span_deadline_us)) {
        fail_acquisition(pair, KM_ERR_TIMEOUT);
        return KM_OK;
    }
    
    if (km_time_after_or_eq(now_us, pair->channel_deadline_us)) {
        fail_acquisition(pair, KM_ERR_TIMEOUT);
        return KM_OK;
    }
    
    if (pair->state == KM_ADC_PAIR_STATE_A_RETRY || pair->state == KM_ADC_PAIR_STATE_B_RETRY) {
        if (km_time_before(now_us, pair->next_retry_us)) {
            return KM_OK;
        }
    }
    
    if (pair->state == KM_ADC_PAIR_STATE_A_POLL) {
        pair->in_prog_a_attempt_us = now_us;
    } else if (pair->state == KM_ADC_PAIR_STATE_B_POLL) {
        pair->in_prog_b_attempt_us = now_us;
    }

    uint8_t channel = (pair->state == KM_ADC_PAIR_STATE_A_POLL || pair->state == KM_ADC_PAIR_STATE_A_RETRY) ? 0 : 1;
    uint16_t raw = 0;
    uint32_t retry_us = 0;
    
    km_result_t res = pair->hal.poll(pair->context, channel, &raw, &retry_us);
    
    if (res == KM_ERR_BUSY) {
        if (retry_us == 0 || retry_us >= 0x80000000u) {
            fail_acquisition(pair, KM_ERR_INVALID_PARAM);
            return KM_OK;
        }
        pair->next_retry_us = now_us + retry_us;
        if (channel == 0) pair->state = KM_ADC_PAIR_STATE_A_RETRY;
        else pair->state = KM_ADC_PAIR_STATE_B_RETRY;
        return KM_OK;
    } else if (res == KM_OK) {
        if (channel == 0) {
            pair->in_prog_raw_a = raw;
            pair->in_prog_present_mask |= 1;
            pair->in_prog_a_complete_us = now_us;
        } else {
            pair->in_prog_raw_b = raw;
            pair->in_prog_present_mask |= 2;
            pair->in_prog_b_complete_us = now_us;
        }
        
        if (raw > pair->config.max_raw) {
            fail_acquisition(pair, KM_ERR_INVALID_PARAM);
            return KM_OK;
        }
        
        if (channel == 0) {
            pair->state = KM_ADC_PAIR_STATE_B_POLL;
            pair->channel_deadline_us = now_us + pair->config.conversion_timeout_us;
        } else {
            pair->current_sample.is_valid = true;
            pair->current_sample.result = KM_OK;
            pair->current_sample.raw_a = pair->in_prog_raw_a;
            pair->current_sample.raw_b = pair->in_prog_raw_b;
            pair->current_sample.present_mask = pair->in_prog_present_mask;
            pair->current_sample.pair_start_us = pair->in_prog_pair_start_us;
            pair->current_sample.a_attempt_us = pair->in_prog_a_attempt_us;
            pair->current_sample.a_complete_us = pair->in_prog_a_complete_us;
            pair->current_sample.b_attempt_us = pair->in_prog_b_attempt_us;
            pair->current_sample.b_complete_us = pair->in_prog_b_complete_us;
            
            pair->has_completed_sample = true;
            pair->state = KM_ADC_PAIR_STATE_IDLE;
        }
    } else {
        fail_acquisition(pair, res);
    }
    
    return KM_OK;
}

KM_API km_result_t km_adc_pair_next_deadline(km_adc_pair_t *pair, uint32_t *deadline_us) {
    if (!pair || !deadline_us || pair->state == KM_ADC_PAIR_STATE_UNINIT) return KM_ERR_INVALID_PARAM;
    if (pair->state == KM_ADC_PAIR_STATE_FAULT || pair->state == KM_ADC_PAIR_STATE_IDLE) {
        return KM_ERR_UNAVAILABLE;
    }
    
    uint32_t earliest = pair->span_deadline_us;
    
    if (km_time_before(pair->channel_deadline_us, earliest)) {
        earliest = pair->channel_deadline_us;
    }
    
    if (pair->state == KM_ADC_PAIR_STATE_A_RETRY || pair->state == KM_ADC_PAIR_STATE_B_RETRY) {
        if (km_time_before(pair->next_retry_us, earliest)) {
            earliest = pair->next_retry_us;
        }
    } else {
        if (km_time_before(pair->last_time_us, earliest)) {
            earliest = pair->last_time_us;
        }
    }
    
    if (km_time_before(earliest, pair->last_time_us)) {
        earliest = pair->last_time_us;
    }
    
    *deadline_us = earliest;
    return KM_OK;
}

KM_API km_result_t km_adc_pair_get_sample(km_adc_pair_t *pair, uint32_t now_us, km_adc_pair_sample_t *sample_out) {
    if (!pair || !sample_out) return KM_ERR_INVALID_PARAM;
    if (pair->state == KM_ADC_PAIR_STATE_UNINIT) return KM_ERR_INVALID_PARAM;
    
    *sample_out = pair->current_sample;
    
    if (!pair->has_completed_sample) {
        sample_out->is_valid = false;
        return KM_ERR_UNAVAILABLE;
    }
    
    if (km_time_before(now_us, pair->last_time_us) || (now_us - pair->last_time_us) > KM_TIME_WRAP_HORIZON_US) {
        sample_out->is_valid = false;
    } else {
        uint32_t age = now_us - sample_out->a_attempt_us;
        if (age > pair->config.max_sample_age_us) {
            sample_out->is_valid = false;
        }
    }
    
    return KM_OK;
}

KM_API km_result_t km_adc_pair_reset(km_adc_pair_t *pair, uint32_t now_us) {
    if (!pair) return KM_ERR_INVALID_PARAM;
    if (pair->state == KM_ADC_PAIR_STATE_UNINIT) return KM_ERR_INVALID_PARAM;
    
    pair->hal.cancel(pair->context);
    
    pair->current_sample.is_valid = false;
    pair->has_completed_sample = false;
    pair->state = KM_ADC_PAIR_STATE_IDLE;
    pair->last_time_us = now_us;
    
    return KM_OK;
}
