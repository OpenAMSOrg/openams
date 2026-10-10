// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_ADC_PAIR_H
#define KLIPPER_MMU_ADC_PAIR_H

#include "klipper_mmu/types.h"
#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    uint16_t max_raw;
    uint32_t conversion_timeout_us;
    uint32_t max_pair_span_us;
    uint32_t max_sample_age_us;
} km_adc_pair_config_t;

typedef struct {
    /* Poll one exclusively owned converter; KM_OK consumes its raw result.
     * BUSY supplies a delay in (0, 2^31). Callbacks are immediate/non-reentrant. */
    km_result_t (*poll)(void *context, uint8_t channel, uint16_t *raw, uint32_t *retry_us);
    void (*cancel)(void *context);
} km_adc_pair_hal_t;

typedef struct {
    bool is_valid;
    km_result_t result;
    uint16_t raw_a;
    uint16_t raw_b;
    uint8_t present_mask; /* bit 0 = A, bit 1 = B */
    uint32_t pair_start_us; /* Admission time: bounds queued work as well as conversion. */
    uint32_t a_attempt_us;
    uint32_t a_complete_us;
    uint32_t b_attempt_us;
    uint32_t b_complete_us;
} km_adc_pair_sample_t;

typedef enum {
    KM_ADC_PAIR_STATE_UNINIT = 0,
    KM_ADC_PAIR_STATE_IDLE,
    KM_ADC_PAIR_STATE_A_POLL,
    KM_ADC_PAIR_STATE_A_RETRY,
    KM_ADC_PAIR_STATE_B_POLL,
    KM_ADC_PAIR_STATE_B_RETRY,
    KM_ADC_PAIR_STATE_FAULT
} km_adc_pair_state_t;

typedef struct {
    km_adc_pair_state_t state;
    km_adc_pair_config_t config;
    km_adc_pair_hal_t hal;
    void *context;
    
    uint32_t last_time_us;
    uint32_t channel_deadline_us;
    uint32_t span_deadline_us;
    uint32_t next_retry_us;
    
    bool has_completed_sample;
    km_adc_pair_sample_t current_sample;
    
    uint16_t in_prog_raw_a;
    uint16_t in_prog_raw_b;
    uint8_t in_prog_present_mask;
    uint32_t in_prog_pair_start_us;
    uint32_t in_prog_a_attempt_us;
    uint32_t in_prog_a_complete_us;
    uint32_t in_prog_b_attempt_us;
    uint32_t in_prog_b_complete_us;
} km_adc_pair_t;

/* Pure check shared by saved configuration and initialization. No HAL calls. */
KM_API km_result_t km_adc_pair_config_validate(const km_adc_pair_config_t *config);

/* Fresh instance. Serialize all APIs and HAL calls; context must remain valid
 * and must not alias cleared pair storage. Time-advancing gaps are <2^31 us. */
KM_API km_result_t km_adc_pair_init(km_adc_pair_t *pair, const km_adc_pair_config_t *config, const km_adc_pair_hal_t *hal, void *context, uint32_t now_us);
KM_API km_result_t km_adc_pair_begin(km_adc_pair_t *pair, uint32_t now_us);
KM_API km_result_t km_adc_pair_service(km_adc_pair_t *pair, uint32_t now_us);
KM_API km_result_t km_adc_pair_next_deadline(km_adc_pair_t *pair, uint32_t *deadline_us);
/* Pure copy into separate caller storage. KM_OK means a record is available;
 * check is_valid and result before using it, including after service returns OK. */
KM_API km_result_t km_adc_pair_get_sample(km_adc_pair_t *pair, uint32_t now_us, km_adc_pair_sample_t *sample_out);
KM_API km_result_t km_adc_pair_reset(km_adc_pair_t *pair, uint32_t now_us);

#ifdef __cplusplus
}
#endif

#endif
