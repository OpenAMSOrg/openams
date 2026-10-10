// SPDX-License-Identifier: GPL-3.0-or-later
// One-shot DiffA/DiffB calibration object. No ADC or motion startup.
#include "autoconf.h"

#if !defined(CONFIG_MACH_STM32H723) || !CONFIG_MACH_STM32H723 \
    || !defined(CONFIG_KLIPPER_MMU_DIFF_BUFFER) || !CONFIG_KLIPPER_MMU_DIFF_BUFFER \
    || !defined(CONFIG_WANT_ADC) || !CONFIG_WANT_ADC \
    || !defined(CONFIG_WANT_ADC_LOCAL) || !CONFIG_WANT_ADC_LOCAL
#error "calibration.c requires H723, DIFF_BUFFER, WANT_ADC, and WANT_ADC_LOCAL"
#endif

#include <stdint.h>
#include <string.h>
#include "basecmd.h"
#include "command.h"
#include "sched.h"
#include "adc.h"
#include "klipper_mmu/buffer_sensor.h"
#include "calibration.h"

DECL_CONSTANT("KLIPPER_MMU_DIFF_CALIBRATION", 1);

static const km_diff_adc_config_t *published;
static uint8_t denied;

static uint16_t
km_cal_u16(const uint8_t *p)
{
    return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}

void
km_cal_shutdown(void)
{
    denied = 1;
    published = 0;
}
DECL_SHUTDOWN(km_cal_shutdown);

const km_diff_adc_config_t *
km_diff_calibration_get(void)
{
    if (denied || sched_is_shutdown() || !published)
        return 0;
    return published;
}

void
command_config_km_diff_calibration(uint32_t *args)
{
    uint32_t oid;
    uint32_t length;
    uint32_t count;
    uint32_t expect;
    const uint8_t *data;
    const uint8_t *p;
    km_diff_adc_config_t cfg;
    km_diff_adc_config_t *obj;
    uint8_t i;

    if (sched_is_shutdown())
        return;
    if (denied)
        shutdown("Diff calibration denied after shutdown");
    if (published)
        shutdown("Diff calibration already configured");

    oid = args[0];
    length = args[1];
    if (oid > 255u)
        shutdown("Invalid diff calibration oid");
    if (length < 18u || length > 54u)
        shutdown("Invalid diff calibration data");

    data = command_decode_ptr(args[2]);
    if (data[0] > 1u)
        shutdown("Invalid diff calibration data");
    count = data[1];
    if (count < 2u || count > 8u || count > KM_MAX_CAL_POINTS)
        shutdown("Invalid diff calibration data");
    expect = 6u * count + 6u;
    if (length != expect)
        shutdown("Invalid diff calibration data");

    memset(&cfg, 0, sizeof(cfg));
    cfg.channel_map = (km_diff_channel_map_t)data[0];
    cfg.vref_mv = km_cal_u16(data + 2);
    cfg.min_valid_mv = km_cal_u16(data + 4);
    cfg.max_valid_mv = km_cal_u16(data + 6);
    cfg.path_tolerance_counts = km_cal_u16(data + 8);
    cfg.adc_max_counts = KM_ADC_MAX_RAW;
    cfg.point_count = (uint8_t)count;

    p = data + 10;
    for (i = 0; i < (uint8_t)count; i++) {
        cfg.points[i].ch_a_raw = km_cal_u16(p);
        p += 2;
        cfg.points[i].ch_b_raw = km_cal_u16(p);
        p += 2;
        if (i == 0)
            cfg.points[i].position_permille = 0;
        else if ((uint32_t)i + 1u == count)
            cfg.points[i].position_permille = 1000;
        else {
            cfg.points[i].position_permille = (int32_t)km_cal_u16(p);
            p += 2;
        }
    }
    if ((uint32_t)(p - data) != length)
        shutdown("Invalid diff calibration data");
    if (km_diff_adc_config_validate(&cfg) != KM_OK)
        shutdown("Invalid diff calibration data");
    if (sched_is_shutdown() || denied)
        return;

    obj = oid_alloc((uint8_t)oid, command_config_km_diff_calibration,
                    sizeof(*obj));
    if (sched_is_shutdown() || denied || !obj)
        return;
    memcpy(obj, &cfg, sizeof(*obj));
    if (sched_is_shutdown() || denied)
        return;
    published = obj;
}
DECL_COMMAND(command_config_km_diff_calibration,
             "config_km_diff_calibration oid=%c data=%*s");
