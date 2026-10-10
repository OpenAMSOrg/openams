// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_RESOURCE_PLAN_H
#define KLIPPER_MMU_RESOURCE_PLAN_H

#include "klipper_mmu/types.h"
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/** Maximum allowed claims in one plan.
 * mcu/h723_resources.c's own capacity assert needs
 * KM_PIN_CONFIG_MAX_CLAIMS + 2*KM_MAX_LANES + 3 <= KM_RESOURCE_MAX_CLAIMS.
 * mcu/pin_config.h's KM_PIN_CONFIG_MAX_CLAIMS is 11 per lane on an INDEXED
 * image (the selector's per-gate index switch, klipper_mmu/selector.h, adds
 * a lane role alongside BAY/LOAD/REWIND*3/CURRENT) or 10 per lane otherwise,
 * plus 12 shared and selector roles: 12 INDEXED lanes need 144 + 24 + 3 =
 * 171 of 172, 8 INDEXED lanes 100 + 16 + 3 = 119 of 136, 8 non-INDEXED lanes
 * 92 + 16 + 3 = 111 of 128, 12 non-INDEXED lanes 132 + 24 + 3 = 159 of 160.
 * mcu/Makefile passes both this and KM_PIN_CONFIG_MAX_CLAIMS per image (an
 * image that never selects CONFIG_KLIPPER_MMU_SELECTOR_KIND_INDEXED does not
 * carry that row's headroom); a build that includes this header without
 * going through mcu/Makefile -- every native and test build -- gets the
 * full (INDEXED-sized) capacity below unchanged. */
#ifndef KM_RESOURCE_MAX_CLAIMS
#define KM_RESOURCE_MAX_CLAIMS (9u * KM_MAX_LANES + 64u)
#endif

/** Sentinel index for diagnostic entries without an associated claim. */
#define KM_RESOURCE_NO_INDEX   UINT16_MAX

/**
 * Kind of hardware resource represented by a claim.
 * Assigned explicit integer values starting at 1. Zero and negative values are invalid.
 */
typedef enum {
    KM_RESOURCE_PIN             = 1,
    KM_RESOURCE_TIMER_EXCLUSIVE = 2,
    KM_RESOURCE_TIMER_PWM_BASE  = 3,
    KM_RESOURCE_TIMER_CHANNEL   = 4,
    KM_RESOURCE_ADC_UNIT        = 5,
    KM_RESOURCE_ADC_CHANNEL     = 6
} km_resource_kind_t;

/**
 * Diagnostic reason codes explaining plan validation outcome.
 */
typedef enum {
    KM_RESOURCE_DIAG_NONE                = 0,
    KM_RESOURCE_DIAG_BAD_ARGUMENT        = 1,
    KM_RESOURCE_DIAG_BAD_CLAIM           = 2,
    KM_RESOURCE_DIAG_DUPLICATE_RESOURCE  = 3,
    KM_RESOURCE_DIAG_TIMER_CONFIGURATION = 4,
    KM_RESOURCE_DIAG_MISSING_TIMER_BASE  = 5,
    KM_RESOURCE_DIAG_MISSING_ADC_UNIT    = 6
} km_resource_diag_reason_t;

/**
 * One canonical hardware claim for a single MCU namespace.
 *
 * All identity is numeric and canonical: no strings or dynamic handles.
 * - kind: Hardware resource kind (1..6).
 * - instance: Canonical physical pin ID, timer unit ID, or ADC unit ID (0..UINT16_MAX-1).
 * - subresource: Channel or sub-function index.
 *   * PIN, TIMER_EXCLUSIVE, TIMER_PWM_BASE, ADC_UNIT: must be 0.
 *   * TIMER_CHANNEL: must be 1..UINT16_MAX-1.
 *   * ADC_CHANNEL: must be 0..UINT16_MAX-1 (channel 0 is valid).
 * - owner: Identifier of configuring component (1..UINT16_MAX-1). Owner 0 and UINT16_MAX invalid.
 * - TIMER_PWM_BASE sharing is only for board-selected edge-aligned up-counting
 *   PWM with matching effective rate and period. Other timer modes require an
 *   exclusive claim; physical mode/route validation belongs to the board mapper.
 * - period_ticks: For TIMER_PWM_BASE, counter period in ticks (> 0). Must be 0 for all other kinds.
 * - tick_hz: For TIMER_PWM_BASE, timer counter frequency in Hz (> 0). Must be 0 for all other kinds.
 */
typedef struct {
    km_resource_kind_t kind;
    uint16_t instance;
    uint16_t subresource;
    uint16_t owner;
    uint32_t period_ticks;
    uint32_t tick_hz;
} km_resource_claim_t;

/**
 * Diagnostic details returned on plan validation.
 *
 * Caller must provide independent storage for this structure; it must not be aliased
 * into the input claims array.
 */
typedef struct {
    km_resource_diag_reason_t reason;
    uint16_t first_index;
    uint16_t second_index;
} km_resource_diagnostic_t;

/**
 * Validate a complete canonical hardware resource plan for one MCU namespace.
 *
 * This pure, allocation-free function performs setup-time validation of a complete
 * immutable hardware claim set before GPIO, timer, or ADC configuration. It evaluates
 * mutual exclusion, timer sharing rules, ADC serialized-unit ownership, and companion
 * dependencies.
 *
 * Scope and limitations:
 * - This function is NOT a runtime ownership registry and does NOT claim, reserve,
 *   or manipulate physical hardware.
 * - It does NOT resolve aliases: caller (board/profile/config adapter) must resolve
 *   all connector aliases to canonical physical IDs before invoking validation.
 * - It does NOT verify electrical, pin-multiplexing, or voltage compatibility.
 * - It cannot detect omitted claims, concurrent plan mutation, or out-of-band hardware
 *   access bypassing the adapter.
 * - Validation is bounded O(count^2); it must be invoked at setup/configuration time
 *   and NEVER inside timing-critical real-time control loops or ISRs.
 * - Any runtime hardware reconfiguration requires stopping all outputs and full revalidation.
 *
 * Diagnostic precedence:
 * 1. Plan-level arguments: count > KM_RESOURCE_MAX_CLAIMS or (count > 0 and claims == NULL)
 *    -> KM_ERR_INVALID_PARAM, BAD_ARGUMENT, both indices NO_INDEX.
 *    (Empty plan: count == 0 is valid and returns KM_OK with NONE and both indices NO_INDEX).
 * 2. Individual records: first record in ascending index violating field rules
 *    -> KM_ERR_INVALID_PARAM, BAD_CLAIM, first_index = i, second_index = NO_INDEX.
 * 3. Pairwise incompatibilities: first incompatible pair in lexicographic (i, j), i < j
 *    -> KM_ERR_CONFLICT, first_index = i, second_index = j, reason = DUPLICATE_RESOURCE
 *       (except mismatched TIMER_PWM_BASE timing returns TIMER_CONFIGURATION).
 * 4. Missing required companion: first channel in ascending index missing its required base/unit
 *    -> KM_ERR_INVALID_PARAM, MISSING_TIMER_BASE or MISSING_ADC_UNIT,
 *       first_index = channel index, second_index = NO_INDEX.
 * 5. Success -> KM_OK, NONE, both indices NO_INDEX.
 *
 * @param claims Pointer to array of claims.
 * @param count Number of claims in array (must be <= KM_RESOURCE_MAX_CLAIMS).
 * @param diagnostic Optional pointer to diagnostic structure (NULL to ignore).
 *                   Must be independent caller storage, not aliased into claims.
 * @return KM_OK on success, KM_ERR_INVALID_PARAM or KM_ERR_CONFLICT on error.
 */
KM_API km_result_t km_resource_plan_validate(const km_resource_claim_t *claims,
                                             size_t count,
                                             km_resource_diagnostic_t *diagnostic);

#ifdef __cplusplus
}
#endif

#endif /* KLIPPER_MMU_RESOURCE_PLAN_H */
