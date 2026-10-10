// SPDX-License-Identifier: GPL-3.0-or-later
/* Inert ADC register file. Route queries must leave it unchanged. */
#include "internal.h"
#include <string.h>
ADC_TypeDef test_adcs[3];
ADC_Common_TypeDef test_common[2];
static ADC_TypeDef snap_adcs[3];
static ADC_Common_TypeDef snap_common[2];

void adc_watch_arm(void)
{
    memset(test_adcs, 0xa5, sizeof(test_adcs));
    memset(test_common, 0x5a, sizeof(test_common));
    memcpy(snap_adcs, test_adcs, sizeof(snap_adcs));
    memcpy(snap_common, test_common, sizeof(snap_common));
}

int adc_watch_unchanged(void)
{
    if (memcmp(snap_adcs, test_adcs, sizeof(snap_adcs)) != 0)
        return 0;
    if (memcmp(snap_common, test_common, sizeof(snap_common)) != 0)
        return 0;
    return 1;
}
