// SPDX-License-Identifier: GPL-3.0-or-later
#include <stdio.h>
#include <assert.h>
#include <string.h>
#include "klipper_mmu/buffer_sensor.h"

static void test_switched_buffer_logic(void)
{
    km_buffer_sensor_t buf;
    km_result_t res = km_buffer_init_switches(&buf, 250, 750, 500, 200);
    assert(res == KM_OK);
    assert(!km_buffer_is_valid(&buf));

    /* Neutral state */
    res = km_buffer_update_switches(&buf, false, false, 1000);
    assert(res == KM_OK);
    assert(km_buffer_is_valid(&buf));
    assert(km_buffer_get_position(&buf) == 500);

    /* Low switch active (demand forward feed) */
    res = km_buffer_update_switches(&buf, true, false, 1010);
    assert(res == KM_OK);
    assert(km_buffer_is_valid(&buf));
    assert(km_buffer_get_position(&buf) == 250);

    /* High switch active (slack, stop feed) */
    res = km_buffer_update_switches(&buf, false, true, 1020);
    assert(res == KM_OK);
    assert(km_buffer_is_valid(&buf));
    assert(km_buffer_get_position(&buf) == 750);

    /* Contradictory state: both switches closed indicates mechanical/electrical failure */
    res = km_buffer_update_switches(&buf, true, true, 1030);
    assert(res == KM_ERR_FAULTED);
    assert(!km_buffer_is_valid(&buf));
    assert(buf.fault == KM_FAULT_BUFFER_SIGNAL);

    printf("PASS: test_switched_buffer_logic\n");
}

/* A tension-only buffer (docs/devices/qidi_box.md) has no physical
 * compression switch: the caller must pass switch_high = false always. This
 * matches Happy Hare's own degraded single-switch sync-feedback mapping
 * (extras/mmu/unit/mmu_buffer.py sync_tension_callback,
 * compression_enabled=False branch): the tension switch active means
 * tension/-1 (switch_low here), inactive means neutral/0, and the
 * compressed/+1 state (switch_high_permille) is never reported. */
static void test_single_switch_buffer_logic(void)
{
    km_buffer_sensor_t buf;
    km_result_t res = km_buffer_init_switches(&buf, 250, 750, 500, 200);
    assert(res == KM_OK);

    /* No second switch exists: switch_high is always false, never fabricated. */
    res = km_buffer_update_switches(&buf, false, false, 1000);
    assert(res == KM_OK && km_buffer_is_valid(&buf));
    assert(km_buffer_get_position(&buf) == 500); /* Neutral: tension switch inactive. */

    res = km_buffer_update_switches(&buf, true, false, 1010);
    assert(res == KM_OK && km_buffer_is_valid(&buf));
    assert(km_buffer_get_position(&buf) == 250); /* Tension active: demand forward feed. */

    /* Releasing the tension switch returns to neutral, never to the
     * unreachable compressed/750 state -- there is no switch to report it. */
    res = km_buffer_update_switches(&buf, false, false, 1020);
    assert(res == KM_OK && km_buffer_is_valid(&buf));
    assert(km_buffer_get_position(&buf) == 500);

    printf("PASS: test_single_switch_buffer_logic\n");
}

static void test_diff_adc_calibration_and_interpolation(void)
{
    km_buffer_sensor_t buf;
    km_diff_adc_config_t cfg = {0};
    cfg.path_tolerance_counts = 200;
    cfg.channel_map = KM_DIFF_CH_A_LOW_B_HIGH;
    cfg.vref_mv = 3300;
    cfg.adc_max_counts = 4095;
    cfg.min_valid_mv = 50;
    cfg.max_valid_mv = 3250;
    cfg.point_count = 3;

    /* Point 0: 0% travel (min voltage ~161mV, well above 50mV min) */
    cfg.points[0].ch_a_raw = 200;
    cfg.points[0].ch_b_raw = 2048;
    cfg.points[0].position_permille = 0;

    /* Point 1: 50% travel (1.65V center) */
    cfg.points[1].ch_a_raw = 2048;
    cfg.points[1].ch_b_raw = 2048;
    cfg.points[1].position_permille = 500;

    /* Point 2: 100% travel (max voltage ~3138mV, well below 3250mV max) */
    cfg.points[2].ch_a_raw = 2048;
    cfg.points[2].ch_b_raw = 3895;
    cfg.points[2].position_permille = 1000;

    km_result_t res = km_buffer_init_diff_adc(&buf, &cfg, 200);
    assert(res == KM_OK);

    /* Test exact midpoint */
    res = km_buffer_update_diff_adc(&buf, 2048, 2048, 1000);
    assert(res == KM_OK);
    assert(km_buffer_is_valid(&buf));
    assert(km_buffer_get_position(&buf) == 500);

    /* Test lower half quarter point: A is halfway between 200 and 2048 -> ~1124 */
    res = km_buffer_update_diff_adc(&buf, 1124, 2048, 1010);
    assert(res == KM_OK);
    int32_t pos = km_buffer_get_position(&buf);
    assert(pos >= 245 && pos <= 255);

    /* Test upper half quarter point: B is halfway between 2048 and 3895 -> ~2971 */
    res = km_buffer_update_diff_adc(&buf, 2048, 2971, 1020);
    assert(res == KM_OK);
    pos = km_buffer_get_position(&buf);
    assert(pos >= 745 && pos <= 755);

    /* Test clamping below endpoint */
    res = km_buffer_update_diff_adc(&buf, 100, 2048, 1030);
    assert(res == KM_OK);
    assert(km_buffer_get_position(&buf) == 0);

    /* Test clamping above endpoint */
    res = km_buffer_update_diff_adc(&buf, 2048, 4000, 1040);
    assert(res == KM_OK);
    assert(km_buffer_get_position(&buf) == 1000);

    printf("PASS: test_diff_adc_calibration_and_interpolation\n");
}

static void test_diff_adc_electrical_faults_and_freshness(void)
{
    km_buffer_sensor_t buf;
    km_diff_adc_config_t cfg = {0};
    cfg.path_tolerance_counts = 200;
    cfg.channel_map = KM_DIFF_CH_A_LOW_B_HIGH;
    cfg.vref_mv = 3300;
    cfg.adc_max_counts = 4095;
    cfg.min_valid_mv = 50;  /* 50 mV */
    cfg.max_valid_mv = 3250;/* 3250 mV */
    cfg.point_count = 3;
    cfg.points[0].ch_a_raw = 200;  cfg.points[0].ch_b_raw = 2048; cfg.points[0].position_permille = 0;
    cfg.points[1].ch_a_raw = 2048; cfg.points[1].ch_b_raw = 2048; cfg.points[1].position_permille = 500;
    cfg.points[2].ch_a_raw = 2048; cfg.points[2].ch_b_raw = 3895; cfg.points[2].position_permille = 1000;

    km_buffer_init_diff_adc(&buf, &cfg, 100); /* max age 100ms */

    /* Normal reading */
    km_result_t res = km_buffer_update_diff_adc(&buf, 2048, 2048, 1000);
    assert(res == KM_OK);
    assert(km_buffer_is_valid(&buf));

    /* Freshness check at 1050ms (age 50ms <= 100ms) */
    assert(km_buffer_check_freshness(&buf, 1050));
    assert(km_buffer_is_valid(&buf));

    /* Freshness check at 1150ms (age 150ms > 100ms) */
    assert(!km_buffer_check_freshness(&buf, 1150));
    assert(!km_buffer_is_valid(&buf));
    assert(buf.fault == KM_FAULT_BUFFER_SIGNAL);

    /* Reading outside this calibration's configured electrical range. */
    res = km_buffer_update_diff_adc(&buf, 10, 2048, 1200);
    assert(res == KM_ERR_FAULTED);
    assert(!km_buffer_is_valid(&buf));
    assert(buf.fault == KM_FAULT_BUFFER_SIGNAL);

    /* High reading outside this calibration's range; not proof of a short. */
    res = km_buffer_update_diff_adc(&buf, 2048, 4090, 1300);
    assert(res == KM_ERR_FAULTED);
    assert(!km_buffer_is_valid(&buf));
    assert(buf.fault == KM_FAULT_BUFFER_SIGNAL);

    printf("PASS: test_diff_adc_electrical_faults_and_freshness\n");
}

static void test_paired_acquisition_time(void)
{
    const km_diff_adc_config_t cfg = {.vref_mv=3300, .adc_max_counts=4095,
        .path_tolerance_counts=32, .point_count=3,
        .points={{0,2048,0},{2048,2048,500},{2048,4095,1000}}};
    km_buffer_sensor_t buf;
    assert(km_buffer_init_diff_adc(&buf, &cfg, 200) == KM_OK);
    km_adc_pair_sample_t sample = {.is_valid=true, .result=KM_OK,
        .present_mask=3, .raw_a=1024, .raw_b=2048};
    const uint64_t epochs[] = {0,999,1000,UINT32_MAX-15ULL,UINT32_MAX+17ULL,
        UINT64_C(4294967295)*1000u+999u};
    const uint32_t ages[] = {0,1,999,1000,1001,199999,200000,200001};
    for (unsigned i=0; i<sizeof(epochs)/sizeof(epochs[0]); ++i) {
        sample.a_attempt_us=(uint32_t)epochs[i];
        for (unsigned j=0; j<sizeof(ages)/sizeof(ages[0]); ++j) {
            uint64_t now=epochs[i]+ages[j];
            bool fresh=now/1000u-epochs[i]/1000u <= 200;
            assert(km_buffer_update_adc_pair(&buf,&sample,(uint32_t)(now/1000u),(uint32_t)now)
                   == (fresh ? KM_OK : KM_ERR_FAULTED));
            assert(buf.last_sample_time_ms == (uint32_t)(epochs[i]/1000u));
            assert(buf.is_valid == fresh && buf.position_permille == 250);
        }
    }
    /* Missing, partial, failed and impossible-time records cannot refresh data. */
    sample.a_attempt_us=1000;
    assert(km_buffer_update_adc_pair(&buf,&sample,1,1000) == KM_OK);
    for (unsigned kind=0; kind<7; ++kind) {
        km_adc_pair_sample_t bad=sample;
        if (kind==1) bad.present_mask=1;
        if (kind==2) bad.is_valid=false;
        if (kind==3) bad.result=KM_ERR_TIMEOUT;
        if (kind==4) bad.a_attempt_us=2001;
        if (kind==5) bad.a_attempt_us=2000u-0x80000000u;
        assert(km_buffer_update_adc_pair(&buf,kind ? &bad : NULL,kind==6 ? 1 : 2,2000)!=KM_OK);
        assert(!buf.is_valid && buf.fault==KM_FAULT_BUFFER_SIGNAL && buf.last_sample_time_ms==1);
    }
    assert(km_buffer_update_adc_pair(&buf,&sample,2,2000)==KM_OK);
    assert(buf.last_sample_time_ms==1); /* Cached reading is not dated at delivery. */
    assert(km_buffer_update_adc_pair(NULL,&sample,2,2000)==KM_ERR_INVALID_PARAM);
    assert(km_buffer_init_switches(&buf,250,750,500,200)==KM_OK);
    km_buffer_sensor_t saved=buf;
    assert(km_buffer_update_adc_pair(&buf,&sample,2,2000)==KM_ERR_INVALID_PARAM);
    assert(!memcmp(&buf,&saved,sizeof(buf)));
    puts("PASS: test_paired_acquisition_time");
}

int main(void)
{
    printf("--- Running Buffer Sensor Tests ---\n");
    test_switched_buffer_logic();
    test_single_switch_buffer_logic();
    test_diff_adc_calibration_and_interpolation();
    test_diff_adc_electrical_faults_and_freshness();
    test_paired_acquisition_time();
    printf("All Buffer Sensor Tests Passed.\n\n");
    return 0;
}
