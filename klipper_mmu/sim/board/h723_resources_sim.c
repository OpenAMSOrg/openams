// SPDX-License-Identifier: GPL-3.0-or-later
#include "klipper_mmu/mcu/h723_resources.h"
#include <string.h>

km_result_t km_h723_pwm_route(uint32_t pin, km_h723_pwm_route_t *out)
{
    if (!out) return KM_ERR_INVALID_PARAM;
    out->timer = 1 + (pin & 0x7);
    out->channel = 1 + ((pin >> 3) & 0x3);
    return KM_OK;
}

km_result_t km_config_h723_resources(km_h723_resource_plan_t *out, km_resource_diagnostic_t *diagnostic)
{
    if (diagnostic) *diagnostic = (km_resource_diagnostic_t){KM_RESOURCE_DIAG_NONE, KM_RESOURCE_NO_INDEX, KM_RESOURCE_NO_INDEX};
    if (!out) return KM_ERR_INVALID_PARAM;
    out->count = 0;
    memset(out->rewind, 0, sizeof(out->rewind));
    for (int lane = 0; lane < 4; lane++) {
        for (int dir = 0; dir < 2; dir++) {
            out->rewind[lane][dir].timer = 1 + lane;
            out->rewind[lane][dir].channel = 1 + dir;
        }
    }
    return KM_OK;
}
