// SPDX-License-Identifier: GPL-3.0-or-later
#include "feedback.h"
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>

static const mmu_sim_parameters_t parameters = {
    .step_mm = 0.125, .forward_transfer = 1, .reverse_transfer = 1,
    .hub_mm = 10, .arrival_mm = 30, .buffer_capacity_mm = 20,
    .switch_hysteresis_mm = 0.25, .rewind_max_mm_s = 10, .rewind_time_constant_s = 1
};
/* Explicit synthetic piecewise curve around half of a 3.3-V ADC reference. */
static const km_diff_adc_config_t curve = {
    .channel_map = KM_DIFF_CH_A_LOW_B_HIGH, .vref_mv = 3300, .adc_max_counts = 4095,
    .path_tolerance_counts = 2, .point_count = 3,
    .points = {{0, 2048, 0}, {2048, 2048, 500}, {2048, 4095, 1000}}
};

static void known_readings(void)
{
    const uint16_t expected[][2] = {{0,2048}, {1024,2048}, {2048,2048}, {2048,3072}, {2048,4095}};
    for (unsigned swapped = 0; swapped < 2; ++swapped) {
        km_diff_adc_config_t calibration = curve;
        if (swapped) calibration.channel_map = KM_DIFF_CH_B_LOW_A_HIGH;
        for (unsigned quarter = 0; quarter < 5; ++quarter) {
            mmu_sim_plant_t plant;
            assert(mmu_sim_init(&plant, &parameters, 30 + quarter * 5, 0));
            uint16_t a = 65535, b = 65535;
            assert(mmu_sim_diff_sample(&plant, &calibration, &a, &b));
            assert(a == expected[quarter][swapped ? 1 : 0]);
            assert(b == expected[quarter][swapped ? 0 : 1]);
            km_buffer_sensor_t decoder;
            assert(km_buffer_init_diff_adc(&decoder, &calibration, 200) == KM_OK);
            assert(km_buffer_update_diff_adc(&decoder, a, b, 123) == KM_OK);
            assert(abs(decoder.position_permille - (int)quarter * 250) <= 1);
            assert(decoder.last_sample_time_ms == 123);
        }
    }
}

static void moving_buffer(void)
{
    for (unsigned nonlinear = 0; nonlinear < 2; ++nonlinear) {
        km_diff_adc_config_t calibration = curve;
        if (nonlinear) {
            calibration.points[1] = (km_diff_cal_point_t){1000, 2400, 250};
        }
        mmu_sim_plant_t plant;
        assert(mmu_sim_init(&plant, &parameters, 30, 0));
        km_buffer_sensor_t decoder;
        assert(km_buffer_init_diff_adc(&decoder, &calibration, 200) == KM_OK);
        for (unsigned step = 0; step <= 160; ++step) {
            uint16_t a, b;
            assert(mmu_sim_diff_sample(&plant, &calibration, &a, &b));
            assert(km_buffer_update_diff_adc(&decoder, a, b, step) == KM_OK);
            double expected = step * 6.25;
            assert(fabs(decoder.position_permille - expected) <= 1.5);
            if (step != 160) assert(mmu_sim_step(&plant, 1));
        }
        assert(mmu_sim_advance(&plant, 1000000, 10));
        uint16_t a, b;
        assert(mmu_sim_diff_sample(&plant, &calibration, &a, &b));
        assert(km_buffer_update_diff_adc(&decoder, a, b, 1160) == KM_OK);
        assert(abs(decoder.position_permille - 500) <= 1);
        /* The decoder, not the generator, judges corrupted/out-of-path readings. */
        assert(km_buffer_update_diff_adc(&decoder, 4095, 0, 1161) == KM_ERR_FAULTED);
        assert(!decoder.is_valid && decoder.fault == KM_FAULT_BUFFER_SIGNAL);
    }
}

static void invalid_samples(void)
{
    mmu_sim_plant_t plant; assert(mmu_sim_init(&plant, &parameters, 40, 0));
    uint16_t a = 123, b = 456;
    assert(!mmu_sim_diff_sample(&plant, NULL, &a, &b));
    assert(!mmu_sim_diff_sample(NULL, &curve, &a, &b));
    assert(!mmu_sim_diff_sample(&plant, &curve, &a, &a));
    km_diff_adc_config_t bad = curve; bad.points[1].position_permille = 0;
    assert(!mmu_sim_diff_sample(&plant, &bad, &a, &b));
    bad = curve; bad.point_count = 0;
    assert(!mmu_sim_diff_sample(&plant, &bad, &a, &b));
    plant.material_mm = NAN;
    assert(!mmu_sim_diff_sample(&plant, &curve, &a, &b));
    assert(a == 123 && b == 456);
}

int main(void)
{
    known_readings(); moving_buffer(); invalid_samples();
    puts("feedback: explicit curves, channel mapping, moving buffer and production decoder passed");
    return 0;
}
