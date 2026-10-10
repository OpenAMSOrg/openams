// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_BUFFER_SENSOR_H
#define KLIPPER_MMU_BUFFER_SENSOR_H

#include "klipper_mmu/types.h"
#include "klipper_mmu/adc_pair.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    KM_BUFFER_SENSOR_NONE       = 0,
    KM_BUFFER_SENSOR_SWITCHED   = 1,
    KM_BUFFER_SENSOR_ANALOG_DIFF= 2
} km_buffer_sensor_type_t;

typedef enum {
    KM_DIFF_CH_A_LOW_B_HIGH = 0,
    KM_DIFF_CH_B_LOW_A_HIGH = 1
} km_diff_channel_map_t;

/**
 * Explicit calibration point for analog DiffA/DiffB frontend.
 * Maps raw ADC counts or millivolt readings to a normalized travel position.
 */
typedef struct {
    uint16_t ch_a_raw;
    uint16_t ch_b_raw;
    int32_t  position_permille; /* 0..1000 */
} km_diff_cal_point_t;

/**
 * Configuration for DiffA/DiffB ADC frontend.
 */
typedef struct {
    km_diff_channel_map_t channel_map;
    uint16_t vref_mv;               /* ADC reference voltage in mV (e.g. 3300) */
    uint16_t adc_max_counts;        /* Max ADC counts (e.g. 4095 for 12-bit) */
    uint16_t min_valid_mv;          /* Minimum valid electrical voltage (0 = disabled) */
    uint16_t max_valid_mv;          /* Maximum valid electrical voltage (0 = disabled) */
    uint16_t path_tolerance_counts; /* Maximum allowable deviation from calibrated path in counts (must be nonzero) */
    uint8_t  point_count;
    km_diff_cal_point_t points[KM_MAX_CAL_POINTS];
} km_diff_adc_config_t;

/* Pure shared calibration validation; no sensor state or acquisition required. */
KM_API km_result_t km_diff_adc_config_validate(const km_diff_adc_config_t *config);

/**
 * Normalized buffer sensor state.
 */
typedef struct {
    km_buffer_sensor_type_t type;
    int32_t  position_permille;      /* 0..1000 */
    bool     is_valid;
    km_fault_t fault;
    uint32_t last_sample_time_ms;
    uint32_t max_sample_age_ms;

    /* Configuration for switched mode */
    int32_t  switch_low_permille;    /* e.g. 250 (compressed) */
    int32_t  switch_high_permille;   /* e.g. 750 (extended) */
    int32_t  switch_neutral_permille;/* e.g. 500 (slack) */

    /* Configuration for analog diff mode */
    km_diff_adc_config_t diff_config;
} km_buffer_sensor_t;

/**
 * Initialize buffer sensor as switched feedback.
 */
km_result_t km_buffer_init_switches(km_buffer_sensor_t *buf,
                                    int32_t low_permille,
                                    int32_t high_permille,
                                    int32_t neutral_permille,
                                    uint32_t max_sample_age_ms);

/**
 * Initialize buffer sensor as calibrated DiffA/DiffB analog frontend.
 * Validates table ordering, zero-length segments, counts bounds, and path limits.
 */
km_result_t km_buffer_init_diff_adc(km_buffer_sensor_t *buf,
                                    const km_diff_adc_config_t *config,
                                    uint32_t max_sample_age_ms);

/**
 * Update switched buffer input.
 *
 * A tension-only buffer with no physical second (compression) switch
 * (docs/devices/qidi_box.md) passes switch_high = false always: this is the
 * true state of a switch that cannot be triggered, not a fabricated one.
 * The buffer then only ever reports switch_low_permille (switch_low active)
 * or switch_neutral_permille (inactive) -- switch_high_permille can never be
 * reached, matching Happy Hare's own degraded single-switch sync-feedback
 * mapping when only tension_pin is wired (extras/mmu/unit/mmu_buffer.py
 * sync_tension_callback, compression_enabled=False branch: active means
 * tension/-1, inactive means neutral/0; the compressed/+1 state is unreachable).
 *
 * @param switch_low Low/compressed switch state (true = active)
 * @param switch_high High/extended switch state (true = active)
 * @param now_ms Timestamp in milliseconds
 */
km_result_t km_buffer_update_switches(km_buffer_sensor_t *buf,
                                      bool switch_low,
                                      bool switch_high,
                                      uint32_t now_ms);

/**
 * Update analog DiffA/DiffB ADC frontend with raw samples.
 * Validates channel mapping, path tolerance, paired consistency, and endpoints.
 * @param raw_a Raw ADC counts for Channel A (e.g. PC4)
 * @param raw_b Raw ADC counts for Channel B (e.g. PC5)
 * @param now_ms Timestamp in milliseconds
 */
km_result_t km_buffer_update_diff_adc(km_buffer_sensor_t *buf,
                                      uint16_t raw_a,
                                      uint16_t raw_b,
                                      uint32_t now_ms);

/* Publish a queried ADC pair using its original first-A attempt time. now_ms/us
 * must be one coherent observation, including across their independent wraps.
 * Missing or failed acquisitions invalidate without refreshing the timestamp.
 * The ADC owner must have checked is_valid at this observation. This checks
 * record completeness, calibrated decoding and the buffer's separate age limit. */
KM_API km_result_t km_buffer_update_adc_pair(km_buffer_sensor_t *buf,
                                            const km_adc_pair_sample_t *sample,
                                            uint32_t now_ms, uint32_t now_us);

/**
 * Check sample freshness against max_sample_age_ms.
 */
bool km_buffer_check_freshness(km_buffer_sensor_t *buf, uint32_t now_ms);

static inline bool km_buffer_is_valid(const km_buffer_sensor_t *buf)
{
    return buf ? buf->is_valid : false;
}

static inline int32_t km_buffer_get_position(const km_buffer_sensor_t *buf)
{
    return buf ? buf->position_permille : KM_PERMILLE_CENTER;
}

#ifdef __cplusplus
}
#endif

#endif /* KLIPPER_MMU_BUFFER_SENSOR_H */
