// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_ENCODER_H
#define KLIPPER_MMU_ENCODER_H

#include "klipper_mmu/operation_core.h"

/* Shared-path filament encoder (ERCF/Tradrack, either a TCRT5000 opto or a
 * Binky vane wheel: installer/Kconfig.encoder in the pinned Happy Hare copy).
 * Happy Hare's own encoder is one wire into one MCU pin (installer/
 * Kconfig.encoder:128 "config PIN_ENCODER", singular) -- pin_b is optional
 * (KLIPPER_MMU_ENCODER_DUAL_PIN off by default) for exactly that shape. When
 * pin_b is present it is counted as a second, independent channel -- every
 * level change on either pin adds a click -- so a one-pin build matches
 * Happy Hare's own MCU_counter (src/pulse_counter.c) exactly, and a two-pin
 * build is the same rule generalized to a second wire, one tick per
 * calibrated resolution mm either way. */
typedef struct {
    uint16_t pin_a, pin_b; /* Canonical PA0..PH15 IDs. pin_b is KM_PIN_UNUSED
                             * when only one wire is configured; pin_a is
                             * always required. */
    int8_t pull;           /* -1 pull-down, 0 unbiased, 1 pull-up. Applied to whichever pin(s) exist. */
} km_encoder_gpio_config_t;

/* Saved-configuration factory: reads CONFIG_KLIPPER_MMU_PINS_ENCODER_A/_B via
 * the shared km_config_pins_view() and CONFIG_KLIPPER_MMU_ENCODER_PULL. No
 * GPIO setup. pin_a unassigned returns KM_ERR_UNAVAILABLE; pin_b unassigned
 * (KLIPPER_MMU_ENCODER_DUAL_PIN off) is a valid one-wire result, not a
 * failure. Output is unchanged on failure. */
KM_API km_result_t km_config_encoder(km_encoder_gpio_config_t *out);

/* One immutable binding until MCU reset, like km_switch_gpio_setup. Call from
 * task context with IRQs enabled, before controller attachment. Rejects a
 * missing pin_a, or a pin_b that duplicates pin_a; a KM_PIN_UNUSED pin_b is
 * accepted (one-wire mode) and is simply never touched. Reads an initial
 * level from each configured pin so the first sample after setup cannot
 * report a spurious click. */
KM_API km_result_t km_encoder_gpio_setup(const km_encoder_gpio_config_t *config);

/* Controller sample callback (km_controller_sample_fn shape); context must be
 * NULL. Reads each configured pin once (task context; see the .c file for
 * why no interrupt lock is needed), adds one click per pin whose level
 * changed since the previous sample -- pin_b is skipped entirely in one-wire
 * mode, never fabricated -- and publishes the running total with
 * km_core_set_encoder_at. Before setup, after shutdown, or with the clock not
 * ready, publishes the last total with valid=false rather than inventing
 * zero/no movement -- see km_core_set_load_sensor_at's "retains its value for
 * diagnostics" precedent. The accumulator is a plain uint32_t: it wraps like
 * Klipper's own MCU counter (src/pulse_counter.c), which the host already
 * unwraps with 32-bit modular arithmetic (klippy/extras/pulse_counter.py:49
 * "handle 32-bit counter overflow"). */
KM_API void km_encoder_gpio_sample(km_operation_core_t *core, void *context);

void km_encoder_gpio_shutdown(void);

/* Diagnostic only (docs/diagnostics.md style): the running click total,
 * regardless of validity. Zero before setup. */
KM_API int32_t km_encoder_gpio_get_clicks(void);

#endif
