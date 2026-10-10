// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef MOCK_DRIVER_H
#define MOCK_DRIVER_H

#include "klipper_mmu/driver.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    km_driver_t base;

    /* Timing constraints */
    uint16_t min_pulse_width_us;
    uint32_t min_step_interval_us;

    /* Recorded telemetry */
    uint32_t step_count;
    uint32_t rising_edge_count;
    uint32_t falling_edge_count;
    uint32_t last_step_time_us;
    uint32_t last_rising_edge_us;
    uint32_t last_falling_edge_us;
    uint32_t last_dir_change_us;
    uint32_t last_service_time_us;
    uint32_t min_observed_interval_us;
    uint32_t min_observed_pulse_width_us;
    uint32_t step_interval_violations;
    uint32_t pulse_width_violations;
    uint32_t disabled_step_attempts;
    bool step_pin_high;

    /* Current sensing simulation */
    bool has_measured_current_hw;
    uint16_t simulated_measured_current_ma;

    /* State trackers */
    bool initialized;
    bool shutdown_called;
    km_fault_t injected_fault;
    km_result_t injected_step_edge_result;
} mock_driver_t;

void mock_driver_init(mock_driver_t *mock,
                      uint16_t min_pulse_width_us,
                      uint32_t min_step_interval_us,
                      bool has_measured_current_hw);

void mock_driver_reset_stats(mock_driver_t *mock);

#ifdef __cplusplus
}
#endif

#endif /* MOCK_DRIVER_H */
