// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_MCU_PWM_BRIDGE_GPIO_H
#define KLIPPER_MMU_MCU_PWM_BRIDGE_GPIO_H

#include "klipper_mmu/pwm_bridge_driver.h"
#include "klipper_mmu/resource_plan.h"
#include <stddef.h>

#define KM_PWM_BRIDGE_MAX_SLOTS KM_MAX_LANES

/* Task-context setup. Ready is required; fault is optional. With
 * CONFIG_WANT_GPIO_LOCAL, complete_claims NULL and claim_count 0 is configured
 * admission: saved factories still run, including km_config_h723_resources, and
 * only the external list is skipped. A non-NULL pointer with count 0, NULL with
 * a nonzero count, or a count above KM_RESOURCE_MAX_CLAIMS is KM_ERR_INVALID_PARAM.
 * A supplied array keeps plan validation and fieldwise inclusion under both
 * configurations. With GPIO local off, NULL and 0 stay invalid and the caller
 * supplies and enforces complete external ownership. This adapter does not reserve
 * pins. Under CONFIG_WANT_GPIO_LOCAL, forward, reverse, and ENABLE must already be
 * local before ENABLE changes; a miss returns KM_ERR_CONFLICT with no slot
 * consumption and no callbacks. The first physical action remains inactive ENABLE.
 * Pair BUSY maps to KM_ERR_CONFLICT after safe-off. Claims stay consumed after
 * partial setup. board_startup forwards the same pointer and count, including
 * NULL, 0, and reserves exactly the expected MMU PIN entries once before component
 * setup. Startup does not attach commands or run automatically. ACTUATION stays 0
 * as metadata; outputs are initialized inactive regardless. No hardware
 * qualification is claimed.
 * Returns KM_ERR_UNAVAILABLE when the saved option or clock is unavailable,
 * KM_ERR_INVALID_PARAM for invalid input/configuration, KM_ERR_CONFLICT for a
 * missing/conflicting full plan or missing prior local GPIO claim,
 * KM_ERR_BUSY for a claimed slot, KM_ERR_NOT_SUPPORTED for unsupported PWM
 * routing, or KM_ERR_FAULTED for setup/clock/shutdown faults. A false ready
 * callback does not prevent setup; it prevents positive ENABLE until readiness
 * is established. */
KM_API km_result_t km_pwm_bridge_gpio_setup(uint32_t slot,
    const km_resource_claim_t *complete_claims, size_t claim_count,
    bool (*ready)(void *context), km_fault_t (*fault)(void *context),
    void *context);

/* Published only after complete pair and portable initialization. Runtime
 * operations and service must be serialized by the caller/MCU controller. */
KM_API km_driver_t *km_pwm_bridge_gpio_get_driver(uint32_t slot);
KM_API void km_pwm_bridge_gpio_service(uint32_t slot);
KM_API bool km_pwm_bridge_gpio_is_slot_active(uint32_t slot);
KM_API bool km_pwm_bridge_gpio_is_shutdown(void);
KM_API km_result_t km_pwm_bridge_gpio_get_slot_pins(uint32_t slot,
    uint32_t *forward, uint32_t *reverse, uint32_t *enable);

/* Emergency off permanently latches a slot. Ordinary portable disable uses
 * the separate ENABLE-off callback and confirmed zero PWM; it remains
 * reversible. These functions never report a new completion timestamp. */
KM_API void km_pwm_bridge_gpio_force_off(uint32_t slot);
void km_pwm_bridge_gpio_shutdown(void);

#endif
