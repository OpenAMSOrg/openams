// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_MCU_DIAGNOSTIC_STARTUP_H
#define KLIPPER_MMU_MCU_DIAGNOSTIC_STARTUP_H

#include "klipper_mmu/types.h"
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Hosted diagnostic startup. Compiled only with KLIPPER_MMU_DIAGNOSTIC_STARTUP.
 * Software gate only: hardware qualification stays pending, outputs stay inactive,
 * and km_start stays rejected. DiffA/DiffB additionally requires the
 * validated calibration object before board startup. The boot-identity
 * provider has its own startup hook.
 *
 * Trigger is the ordinary command km_diagnostic_start, not DECL_INIT. A trusted
 * host sends it via add_config_cmd(..., is_init=True) after its normal
 * RESERVE_PINS, alias, and BUS_PINS checks. This module does not census the MCU
 * and does not grant a new authorization. There is no host allow_start.
 *
 * KLIPPER_MMU_BOOT_IDENTITY_HOST builds register only
 * km_diagnostic_start_host epoch_hi=%u epoch_lo=%u, sent after Klipper's MCU
 * config and post-init. The first nonzero epoch binds for the MCU lifetime;
 * it is a lifetime discriminator, not MCU entropy or authentication.
 */
enum km_diagnostic_state {
    KM_DIAGNOSTIC_IDLE    = 0,
    KM_DIAGNOSTIC_WAITING = 1,
    KM_DIAGNOSTIC_READY   = 2,
    KM_DIAGNOSTIC_FAULTED = 3,
};

/* State only. Observing global shutdown as FAULTED is intentional. */
enum km_diagnostic_state km_diagnostic_get_state(void);
km_result_t km_diagnostic_get_result(void);

void km_diagnostic_task(void);
void km_diagnostic_shutdown(void);
/* Exactly one start command is defined and registered per build. */
void command_km_diagnostic_start(uint32_t *args);
void command_km_diagnostic_start_host(uint32_t *args);
void command_km_diagnostic_query(uint32_t *args);
void command_km_diagnostic_detail_query(uint32_t *args);

#ifdef __cplusplus
}
#endif

#endif /* KLIPPER_MMU_MCU_DIAGNOSTIC_STARTUP_H */
