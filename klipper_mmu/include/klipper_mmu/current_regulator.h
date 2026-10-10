// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_CURRENT_REGULATOR_H
#define KLIPPER_MMU_CURRENT_REGULATOR_H

#include "klipper_mmu/types.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Measured-current regulator for powered rewind assist.
 * Implements anti-windup PI control, sensor freshness checks, bounds enforcement,
 * and overcurrent fault tripping.
 */
typedef struct {
    bool enabled;
    bool sample_seen;
    bool overcurrent;
    uint16_t target_current_ma;
    uint16_t current_limit_ma;
    uint16_t base_duty_permille;
    uint16_t current_duty_permille;
    uint16_t min_duty_permille;
    uint16_t max_duty_permille;
    int32_t integral_term;
    uint32_t last_sample_time_ms;
    uint32_t max_sample_age_ms;
    uint32_t overcurrent_start_ms;
    km_fault_t fault;
} km_current_regulator_t;

/**
 * Initialize current regulator.
 */
km_result_t km_current_regulator_init(km_current_regulator_t *reg,
                                      uint16_t target_current_ma,
                                      uint16_t current_limit_ma,
                                      uint16_t base_duty_permille,
                                      uint32_t max_sample_age_ms);

/**
 * Update closed loop with measured current reading.
 * @param measured_current_ma Measured hardware current
 * @param sensor_valid Validity flag of the measurement
 * @param now_ms Monotonic millisecond timestamp
 * @param duty_out Pointer to receive calculated PWM duty (0..1000)
 */
km_result_t km_current_regulator_update(km_current_regulator_t *reg,
                                        uint16_t measured_current_ma,
                                        bool sensor_valid,
                                        uint32_t now_ms,
                                        uint16_t *duty_out);

/* Explicit acquisition timestamp; repeated cached readings do not refresh age. */
km_result_t km_current_regulator_update_sample(km_current_regulator_t *reg,
                                               uint16_t measured_ma, bool valid,
                                               uint32_t sample_ms, uint32_t now_ms,
                                               uint16_t *duty_out);

/**
 * Reset regulator state, clearing integrator and overcurrent timers.
 */
void km_current_regulator_reset(km_current_regulator_t *reg);

#ifdef __cplusplus
}
#endif

#endif /* KLIPPER_MMU_CURRENT_REGULATOR_H */
