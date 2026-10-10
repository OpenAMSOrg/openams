// SPDX-License-Identifier: GPL-3.0-or-later
#include "klipper_mmu/buffer_sensor.h"
#include <string.h>

km_result_t km_buffer_init_switches(km_buffer_sensor_t *buf,
                                    int32_t low_permille,
                                    int32_t high_permille,
                                    int32_t neutral_permille,
                                    uint32_t max_sample_age_ms)
{
    if (!buf || low_permille < 0 || high_permille > 1000
        || low_permille >= neutral_permille || neutral_permille >= high_permille
        || !max_sample_age_ms || max_sample_age_ms > 60000) {
        return KM_ERR_INVALID_PARAM;
    }
    memset(buf, 0, sizeof(*buf));
    buf->type = KM_BUFFER_SENSOR_SWITCHED;
    buf->position_permille = neutral_permille;
    buf->is_valid = false;
    buf->fault = KM_FAULT_NONE;
    buf->last_sample_time_ms = 0;
    buf->max_sample_age_ms = max_sample_age_ms > 0 ? max_sample_age_ms : 200;
    buf->switch_low_permille = low_permille;
    buf->switch_high_permille = high_permille;
    buf->switch_neutral_permille = neutral_permille;
    return KM_OK;
}

km_result_t km_diff_adc_config_validate(const km_diff_adc_config_t *config)
{
    if (!config) {
        return KM_ERR_INVALID_PARAM;
    }
    if (config->point_count < 2 || config->point_count > KM_MAX_CAL_POINTS) {
        return KM_ERR_INVALID_PARAM;
    }
    if (!config->adc_max_counts || !config->vref_mv
        || (config->channel_map != KM_DIFF_CH_A_LOW_B_HIGH && config->channel_map != KM_DIFF_CH_B_LOW_A_HIGH)
        || config->min_valid_mv >= config->vref_mv
        || (config->max_valid_mv && (config->max_valid_mv <= config->min_valid_mv || config->max_valid_mv > config->vref_mv))
        || !config->path_tolerance_counts || config->path_tolerance_counts > config->adc_max_counts) {
        return KM_ERR_INVALID_PARAM;
    }

    /* Validate calibration points */
    const uint8_t n = config->point_count;
    if (config->points[0].position_permille != KM_PERMILLE_MIN ||
        config->points[n - 1].position_permille != KM_PERMILLE_MAX) {
        return KM_ERR_INVALID_PARAM;
    }

    for (uint8_t i = 0; i < n; i++) {
        if (config->points[i].ch_a_raw > config->adc_max_counts ||
            config->points[i].ch_b_raw > config->adc_max_counts) {
            return KM_ERR_INVALID_PARAM;
        }
        if (i > 0) {
            if (config->points[i].position_permille <= config->points[i - 1].position_permille) {
                return KM_ERR_INVALID_PARAM; /* Strictly monotonic */
            }
            /* Canonical paired calibration must not fold back or intersect. */
            if (config->points[i].ch_a_raw < config->points[i - 1].ch_a_raw
                || config->points[i].ch_b_raw < config->points[i - 1].ch_b_raw)
                return KM_ERR_INVALID_PARAM;
            if (config->points[i].ch_a_raw == config->points[i - 1].ch_a_raw &&
                config->points[i].ch_b_raw == config->points[i - 1].ch_b_raw) {
                return KM_ERR_INVALID_PARAM; /* Zero-length segment prohibited */
            }
        }
    }

    return KM_OK;
}

km_result_t km_buffer_init_diff_adc(km_buffer_sensor_t *buf,
                                    const km_diff_adc_config_t *config,
                                    uint32_t max_sample_age_ms)
{
    if (!buf || !max_sample_age_ms || max_sample_age_ms > 60000
        || km_diff_adc_config_validate(config) != KM_OK)
        return KM_ERR_INVALID_PARAM;
    km_diff_adc_config_t saved_config = *config; /* Permit in-place reconfiguration. */
    memset(buf, 0, sizeof(*buf));
    buf->type = KM_BUFFER_SENSOR_ANALOG_DIFF;
    buf->position_permille = KM_PERMILLE_CENTER;
    buf->is_valid = false;
    buf->fault = KM_FAULT_NONE;
    buf->last_sample_time_ms = 0;
    buf->max_sample_age_ms = max_sample_age_ms;
    buf->diff_config = saved_config;
    return KM_OK;
}

/* A caller with only a tension-style low switch (no physical compression
 * switch: docs/devices/qidi_box.md) must pass switch_high = false always --
 * the true, unfabricated state of an input that cannot be triggered. This
 * degrades exactly like Happy Hare's own sync-feedback mapping when only
 * tension_pin is wired (extras/mmu/unit/mmu_buffer.py sync_tension_callback,
 * compression_enabled=False branch: `event_value = -tension_state`, so
 * active means tension/-1 and inactive means neutral/0; compressed/+1 is
 * unreachable). Below, that is exactly switch_low->switch_low_permille and
 * both-false->switch_neutral_permille; switch_high_permille is never
 * produced because switch_high is never true. */
km_result_t km_buffer_update_switches(km_buffer_sensor_t *buf,
                                      bool switch_low,
                                      bool switch_high,
                                      uint32_t now_ms)
{
    if (!buf || buf->type != KM_BUFFER_SENSOR_SWITCHED) {
        return KM_ERR_INVALID_PARAM;
    }

    buf->last_sample_time_ms = now_ms;

    /* Both switches closed simultaneously indicates a mechanical or electrical sensor fault */
    if (switch_low && switch_high) {
        buf->is_valid = false;
        buf->fault = KM_FAULT_BUFFER_SIGNAL;
        return KM_ERR_FAULTED;
    }

    if (switch_low) {
        buf->position_permille = buf->switch_low_permille;
    } else if (switch_high) {
        buf->position_permille = buf->switch_high_permille;
    } else {
        buf->position_permille = buf->switch_neutral_permille;
    }

    buf->is_valid = true;
    buf->fault = KM_FAULT_NONE;
    return KM_OK;
}

km_result_t km_buffer_update_diff_adc(km_buffer_sensor_t *buf,
                                      uint16_t raw_a,
                                      uint16_t raw_b,
                                      uint32_t now_ms)
{
    if (!buf || buf->type != KM_BUFFER_SENSOR_ANALOG_DIFF) {
        return KM_ERR_INVALID_PARAM;
    }

    const km_diff_adc_config_t *cfg = &buf->diff_config;
    if (cfg->point_count < 2) {
        buf->is_valid = false;
        buf->fault = KM_FAULT_BUFFER_SIGNAL;
        return KM_ERR_INVALID_PARAM;
    }

    buf->last_sample_time_ms = now_ms;

    /* Channel mapping */
    uint16_t in_a = raw_a;
    uint16_t in_b = raw_b;
    if (cfg->channel_map == KM_DIFF_CH_B_LOW_A_HIGH) {
        in_a = raw_b;
        in_b = raw_a;
    }

    /* Raw count bounds */
    if (in_a > cfg->adc_max_counts || in_b > cfg->adc_max_counts) {
        buf->is_valid = false;
        buf->fault = KM_FAULT_BUFFER_SIGNAL;
        return KM_ERR_FAULTED;
    }

    /* Electrical rail limits if configured */
    if (cfg->adc_max_counts > 0 && cfg->vref_mv > 0) {
        const uint32_t mv_a = ((uint32_t)in_a * cfg->vref_mv) / cfg->adc_max_counts;
        const uint32_t mv_b = ((uint32_t)in_b * cfg->vref_mv) / cfg->adc_max_counts;

        if (cfg->min_valid_mv > 0 && (mv_a < cfg->min_valid_mv || mv_b < cfg->min_valid_mv)) {
            buf->is_valid = false;
            buf->fault = KM_FAULT_BUFFER_SIGNAL;
            return KM_ERR_FAULTED;
        }

        if (cfg->max_valid_mv > 0 && cfg->max_valid_mv < cfg->vref_mv &&
            (mv_a > cfg->max_valid_mv || mv_b > cfg->max_valid_mv)) {
            buf->is_valid = false;
            buf->fault = KM_FAULT_BUFFER_SIGNAL;
            return KM_ERR_FAULTED;
        }
    }

    /*
     * Paired measurement path validation:
     * Check if (in_a, in_b) lies along any segment of the calibrated curve
     * within path_tolerance_counts.
     */
    const uint8_t n = cfg->point_count;
    uint64_t min_dist_sq = UINT64_MAX;
    int32_t best_pos = KM_PERMILLE_CENTER;

    for (uint8_t i = 0; i < n - 1; i++) {
        const km_diff_cal_point_t *p0 = &cfg->points[i];
        const km_diff_cal_point_t *p1 = &cfg->points[i + 1];

        const int64_t da = (int64_t)p1->ch_a_raw - (int64_t)p0->ch_a_raw;
        const int64_t db = (int64_t)p1->ch_b_raw - (int64_t)p0->ch_b_raw;
        const int64_t seg_len_sq = da * da + db * db;

        if (seg_len_sq == 0) {
            continue;
        }

        const int64_t pa = (int64_t)in_a - (int64_t)p0->ch_a_raw;
        const int64_t pb = (int64_t)in_b - (int64_t)p0->ch_b_raw;

        /* Projection parameter u = dot(P - P0, P1 - P0) / seg_len_sq */
        const int64_t dot = pa * da + pb * db;

        int64_t proj_a;
        int64_t proj_b;
        int32_t seg_pos;

        if (dot <= 0) {
            proj_a = p0->ch_a_raw;
            proj_b = p0->ch_b_raw;
            seg_pos = p0->position_permille;
        } else if (dot >= seg_len_sq) {
            proj_a = p1->ch_a_raw;
            proj_b = p1->ch_b_raw;
            seg_pos = p1->position_permille;
        } else {
            proj_a = (int64_t)p0->ch_a_raw + (dot * da) / seg_len_sq;
            proj_b = (int64_t)p0->ch_b_raw + (dot * db) / seg_len_sq;
            const int64_t dp = (int64_t)p1->position_permille - (int64_t)p0->position_permille;
            seg_pos = (int32_t)(p0->position_permille + (dot * dp) / seg_len_sq);
        }

        const int64_t err_a = (int64_t)in_a - proj_a;
        const int64_t err_b = (int64_t)in_b - proj_b;
        const uint64_t dist_sq = (uint64_t)(err_a * err_a + err_b * err_b);

        if (dist_sq < min_dist_sq) {
            min_dist_sq = dist_sq;
            best_pos = seg_pos;
        }
    }

    const uint64_t tol = cfg->path_tolerance_counts;
    const uint64_t max_dist_sq = tol * tol;

    if (min_dist_sq > max_dist_sq) {
        /* Sample is far off the calibrated path: contradictory paired reading */
        buf->is_valid = false;
        buf->fault = KM_FAULT_BUFFER_SIGNAL;
        return KM_ERR_FAULTED;
    }

    /* Clamp position to 0..1000 */
    if (best_pos < KM_PERMILLE_MIN) {
        best_pos = KM_PERMILLE_MIN;
    }
    if (best_pos > KM_PERMILLE_MAX) {
        best_pos = KM_PERMILLE_MAX;
    }

    buf->position_permille = best_pos;
    buf->is_valid = true;
    buf->fault = KM_FAULT_NONE;
    return KM_OK;
}

km_result_t km_buffer_update_adc_pair(km_buffer_sensor_t *buf,
                                      const km_adc_pair_sample_t *sample,
                                      uint32_t now_ms, uint32_t now_us)
{
    if (!buf || buf->type != KM_BUFFER_SENSOR_ANALOG_DIFF)
        return KM_ERR_INVALID_PARAM;
    buf->is_valid = false;
    buf->fault = KM_FAULT_BUFFER_SIGNAL;
    if (!sample) return KM_ERR_UNAVAILABLE;
    if (!sample->is_valid || sample->result != KM_OK || sample->present_mask != 3u)
        return KM_ERR_FAULTED;
    uint32_t age_us = now_us - sample->a_attempt_us;
    uint32_t phase_us = now_us - now_ms * 1000u;
    if (age_us >= 0x80000000u || phase_us >= 1000u)
        return KM_ERR_INVALID_PARAM;
    /* Subtract elapsed milliseconds with the coherent current phase. Dividing
     * a wrapping microsecond timestamp by 1000 would lose its coarse epoch. */
    uint32_t sample_ms = now_ms - (age_us + 999u - phase_us) / 1000u;
    km_result_t result = km_buffer_update_diff_adc(buf, sample->raw_a, sample->raw_b, sample_ms);
    if (result != KM_OK) return result;
    return km_buffer_check_freshness(buf, now_ms) ? KM_OK : KM_ERR_FAULTED;
}

bool km_buffer_check_freshness(km_buffer_sensor_t *buf, uint32_t now_ms)
{
    if (!buf) {
        return false;
    }
    if (!buf->is_valid) {
        return false;
    }
    if (km_time_elapsed(now_ms, buf->last_sample_time_ms) > buf->max_sample_age_ms) {
        buf->is_valid = false;
        buf->fault = KM_FAULT_BUFFER_SIGNAL;
        return false;
    }
    return true;
}
