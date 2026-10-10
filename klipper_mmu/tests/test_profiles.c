// SPDX-License-Identifier: GPL-3.0-or-later
#include <stdio.h>
#include <assert.h>
#include "klipper_mmu/operation_core.h"
#include "mock_driver.h"

int main(void)
{
    km_device_profile_t p;
    km_operation_core_t c;
    km_profile_init_boxturtle(&p);
    assert(p.has_powered_rewind && !p.has_current_sensing && !p.has_encoder && !p.has_proportional_buffer);
    assert(km_core_init(&c, &p) == KM_OK);
    km_profile_init_nightowl(&p);
    assert(!p.has_powered_rewind && !p.has_current_sensing && !p.has_proportional_buffer);
    assert(!p.load_is_virtual_hub);
    assert(km_core_init(&c, &p) == KM_OK);
    km_profile_init_emu(&p);
    assert(p.lane_count == 5 && p.has_load_sensors && p.load_is_virtual_hub);
    assert(!p.has_selector && !p.has_powered_rewind);
    assert(km_core_init(&c, &p) == KM_OK);
    p.lane_count = KM_MAX_LANES;
    assert(km_profile_validate(&p) == KM_OK);
    p.lane_count = 1;
    p.has_load_sensors = false;
    assert(km_profile_validate(&p) == KM_ERR_INVALID_PARAM);
    km_profile_init_boxturtle_enhanced(&p);
    assert(!p.load_is_virtual_hub);
    /* Optional capabilities do not fabricate a measured calibration. */
    assert(km_core_init(&c, &p) == KM_ERR_INVALID_PARAM);
    p.has_diff_calibration = true;
    p.diff_calibration = (km_diff_adc_config_t){
        .channel_map = KM_DIFF_CH_A_LOW_B_HIGH, .vref_mv = 3300, .adc_max_counts = 4095,
        .path_tolerance_counts = 50, .point_count = 3,
        .points = {{0, 2048, 0}, {2048, 2048, 500}, {2048, 4095, 1000}}
    }; /* Synthetic test calibration, never a board default. */
    assert(km_core_init(&c, &p) == KM_OK);
    assert(km_core_init(&c, &c.profile) == KM_OK);
    assert(km_buffer_init_diff_adc(&c.buffer_sensor, &c.buffer_sensor.diff_config, 200) == KM_OK);
    assert(km_buffer_update_diff_adc(&c.buffer_sensor, 0, 2048, 0) == KM_OK);
    assert(c.buffer_sensor.position_permille == 0);
    assert(km_buffer_update_diff_adc(&c.buffer_sensor, 2048, 4095, 1) == KM_OK);
    assert(c.buffer_sensor.position_permille == 1000);
    assert(km_buffer_update_diff_adc(&c.buffer_sensor, 0, 4095, 2) == KM_ERR_FAULTED);
    mock_driver_t feeder, rewind;
    mock_driver_init(&feeder, 2, 10, false); mock_driver_init(&rewind, 2, 10, false);
    assert(km_core_bind_lane_driver(&c, 0, &feeder.base, &rewind.base) == KM_ERR_UNAVAILABLE);
    mock_driver_init(&rewind, 2, 10, true);
    assert(km_core_bind_lane_driver(&c, 0, &feeder.base, &rewind.base) == KM_OK);
    assert(!km_core_get_snapshot(&c).measured_current_available);
    km_core_set_rewind_current(&c, 321, true, 0);
    assert(km_core_get_snapshot(&c).measured_current_ma == 321);
    p.default_feed_velocity_steps_s = UINT32_MAX;
    assert(km_profile_validate(&p) == KM_ERR_INVALID_PARAM);
    puts("Baseline, passive, explicitly calibrated and measured-current composition tests passed.");
    return 0;
}
