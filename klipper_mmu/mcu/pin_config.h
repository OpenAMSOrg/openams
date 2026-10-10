// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_PIN_CONFIG_H
#define KLIPPER_MMU_PIN_CONFIG_H

#include "klipper_mmu/resource_plan.h"

/* Role values are also the owner IDs in the returned pin claims. */
enum km_pin_role {
    KM_PIN_STEP = 1, KM_PIN_DIR, KM_PIN_ENABLE, KM_PIN_UART, KM_PIN_BAY,
    KM_PIN_HUB, KM_PIN_TOOLHEAD, KM_PIN_BUFFER_A, KM_PIN_BUFFER_B,
    KM_PIN_REWIND_FORWARD, KM_PIN_REWIND_REVERSE, KM_PIN_REWIND_ENABLE,
    KM_PIN_ENCODER_A, KM_PIN_ENCODER_B, KM_PIN_CURRENT, KM_PIN_LOAD,
    /* Shared-drive selector machines only (klipper_mmu/selector.h). */
    KM_PIN_SELECTOR_STEP, KM_PIN_SELECTOR_DIR, KM_PIN_SELECTOR_ENABLE,
    KM_PIN_SELECTOR_UART, KM_PIN_SELECTOR_ENDSTOP, KM_PIN_SERVO,
    /* INDEXED only: one index switch per gate, in place of a gate position
     * table (klipper_mmu/selector.h). */
    KM_PIN_SELECTOR_GATE,
    KM_PIN_ROLE_COUNT
};
#define KM_PIN_UNUSED UINT16_MAX

/* A selector machine drives every lane through one gear driver, so the gear's
 * pin, level and TMC lists hold one entry; otherwise one per lane. A C
 * expression, so it reads the options wherever autoconf.h is included. */
#define KM_GEAR_DRIVERS ((CONFIG_KLIPPER_MMU_SELECTOR_STEPPER || CONFIG_KLIPPER_MMU_SERVO) \
                         ? 1u : (unsigned)CONFIG_KLIPPER_MMU_LANE_COUNT)
/* +6 shared selector/servo pins on top of the previous shared-role headroom;
 * 11 per lane on an INDEXED image, where the per-gate index switch
 * (KM_PIN_SELECTOR_GATE) joins BAY/LOAD/REWIND*3/CURRENT as a
 * tenth-and-eleventh per-lane role, or 10 per lane otherwise -- an image
 * that never selects CONFIG_KLIPPER_MMU_SELECTOR_KIND_INDEXED (BTT ViViD,
 * Claymore) makes no KM_PIN_SELECTOR_GATE claims and does not need the row.
 * mcu/Makefile passes the per-image value; a build that includes this
 * header without going through it -- every native and test build -- gets
 * the full (INDEXED-sized) capacity below unchanged. */
#ifndef KM_PIN_CONFIG_MAX_CLAIMS
#define KM_PIN_CONFIG_MAX_CLAIMS (11u * KM_MAX_LANES + 12u)
#endif

typedef struct {
    /* Lane roles use 0..lane_count-1; shared roles use index zero only.
     * Unconfigured entries, including the unused role zero, are KM_PIN_UNUSED. */
    uint16_t pin[KM_PIN_ROLE_COUNT][KM_MAX_LANES];
    km_resource_claim_t claims[KM_PIN_CONFIG_MAX_CLAIMS];
    size_t count;
} km_pin_config_t;

/* Saved-configuration factory with no GPIO setup or global ownership mutation.
 * Serialized setup calls only: fixed scratch storage avoids a large MCU stack
 * temporary. Caller output should also use persistent storage on the MCU.
 * Validate before any board initialization. Output is unchanged on failure.
 * Diagnostic storage must not alias out. Indices refer to the role-ordered
 * pin claims; malformed/missing
 * list elements report the next expected claim index. Append these pin claims
 * to the COMPLETE board/peripheral/external plan before final resource validation.
 * This function validates pins only, not PWM routing, ADC units or electrical
 * suitability. Disabled configuration returns KM_ERR_UNAVAILABLE. */
KM_API km_result_t km_config_pins(km_pin_config_t *out,
                                 km_resource_diagnostic_t *diagnostic);

/* Setup-only read-only view of the immutable saved pin map. The single
 * validated view has static lifetime; callers must serialize setup and must
 * not modify it. On failure, *out is unchanged. */
KM_API km_result_t km_config_pins_view(const km_pin_config_t **out,
                                      km_resource_diagnostic_t *diagnostic);

#endif
