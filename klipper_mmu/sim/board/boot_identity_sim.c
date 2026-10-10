// SPDX-License-Identifier: GPL-3.0-or-later
#include "board/misc.h"
#include "sched.h"
#include "klipper_mmu/types.h"
#include "klipper_mmu/mcu/boot_identity.h"

void km_bi_init(void) {}
void km_bi_task(void) {}
void km_bi_shutdown(void) {}

DECL_INIT(km_bi_init);
DECL_TASK(km_bi_task);
DECL_SHUTDOWN(km_bi_shutdown);

KM_API km_result_t km_boot_identity_get(uint64_t *out)
{
    if (!out) return KM_ERR_INVALID_PARAM;
    *out = 0x1234567890abcdefULL;
    return KM_OK;
}
