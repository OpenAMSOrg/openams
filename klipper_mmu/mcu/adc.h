// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_MCU_ADC_H_INCLUDED
#define KLIPPER_MMU_MCU_ADC_H_INCLUDED

#include "klipper_mmu/types.h"
#include "klipper_mmu/adc_pair.h"
#include "klipper_mmu/mcu_clock.h"
#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Optional AFC Lite presets for one sequential STM32H723 ADC pair.
 *
 * DiffA: PC4 = GPIO('C', 4) = 36, ADC1 channel 4.
 * DiffB: PC5 = GPIO('C', 5) = 37, ADC1 channel 8.
 * Maximum raw conversion output is 4095 (12-bit). These constants are optional
 * presets only. km_adc_start accepts any external distinct same-unit,
 * different-channel pair on ADC1, ADC2, or ADC3 that local-pair setup accepts,
 * including a reversed valid pair on the first successful setup.
 *
 * The caller supplies a prevalidated complete resource plan that includes other
 * MCU users. The winning local owner holds that whole ADC unit until MCU
 * restart. Ordinary clients and a local pair never share any channel of that
 * unit. This reader does not release ownership.
 */
#define KM_ADC_PIN_DIFF_A 36u
#define KM_ADC_PIN_DIFF_B 37u
#define KM_ADC_MAX_RAW    4095u

/**
 * Paired ADC reader lifecycle states.
 */
enum km_adc_state {
    KM_ADC_STATE_STOPPED  = 0,
    KM_ADC_STATE_RUNNING  = 1,
    KM_ADC_STATE_FAULTED  = 2,
    KM_ADC_STATE_SHUTDOWN = 3,
};

/**
 * Query the current paired ADC reader lifecycle state.
 *
 * Thread-safe and IRQ-safe.
 *
 * @return Current lifecycle state (STOPPED, RUNNING, FAULTED, SHUTDOWN).
 */
enum km_adc_state km_adc_get_state(void);

/**
 * Explicitly configure and start the paired ADC acquisition reader.
 *
 * Requirements and Preconditions:
 * - Task context with interrupts enabled. Never call from a timer or init hook.
 * - Clock adapter is KM_CLOCK_STATE_READY and the scheduler is not shut down.
 *   Readiness and portable config validation run before any pin setup.
 * - The caller has a prevalidated complete resource plan, including other MCU
 *   users. Setup is one gpio_adc_setup_local_pair_timeout call, available only when
 *   the STM32H723 local ADC API is compiled in. Any external distinct
 *   same-unit, different-channel pair on ADC1, ADC2, or ADC3 that the driver
 *   accepts may be used, including an initially reversed valid pair.
 *   KM_ADC_PIN_DIFF_A and KM_ADC_PIN_DIFF_B are optional presets. The driver
 *   rejects invalid, truncated, sentinel, same-channel, and cross-unit pins.
 *   Ordinary gpio_adc_setup is never a fallback.
 * - The driver initializes with IRQs enabled, cancels a partial attempt
 *   itself, and publishes both handles only on success. The reader arms no
 *   timer after a failed setup. This reader calls only
 *   gpio_adc_setup_local_pair_timeout. timeout_us is an upper bound for the
 *   whole synchronous start, not a minimum wait: positive, strictly below
 *   2^31 microseconds, and rejected before hardware. The budget is one
 *   observed scheduler-clock interval covering driver setup and publication.
 *   The driver receives the positive remaining time, capped to the largest
 *   microsecond count whose integral-MHz tick length is strictly below 2^31.
 *   It is not given the original budget again. A non-integral or sub-MHz
 *   clock is KM_ERR_INVALID_PARAM before setup. Exhausted remaining time is
 *   KM_ERR_TIMEOUT and does not call the driver. The publication critical
 *   section rejects the start when completion time, rounded up by a
 *   fractional microsecond, meets the deadline, and then no timer is armed.
 *   A driver timeout latches FAULTED: the unit stays unpublished until MCU
 *   reset, and stop does not make it restartable. Shutdown still latches
 *   SHUTDOWN and returns KM_ERR_FAULTED. After a successful setup, ordered
 *   identity is kept if a later clock or deadline failure blocks RUNNING.
 *   Samples stay sequential. They are not synchronized samples and they are
 *   not current or BLDC control.
 * - The first successful local setup fixes this reader's ordered pin identity
 *   until MCU reset. Stop then start of that same pair reuses the driver
 *   handles and invalidates old samples. A different pair, a reversal, or a
 *   different ADC unit returns KM_ERR_BUSY before local setup or any other
 *   hardware access, leaves the reader STOPPED with no timer armed, and leaves
 *   the original pair restartable. Distinct units may each hold a local pair
 *   in the driver; this one reader does not take a second unit. Whole-unit
 *   ownership lasts until MCU reset.
 * - A running reader rejects a duplicate start with KM_ERR_BUSY and stays
 *   RUNNING.
 * - FAULTED and SHUTDOWN stay latched until MCU restart. A normal stop permits
 *   a later start of the same ordered pair.
 * - Config must be non-NULL and in range. The validated copy is stable across
 *   setup. The acquisition timer is armed only after setup, shutdown, and
 *   clock checks succeed.
 *
 * @param pin_a Physical pin ID for channel A. Order is part of the latched identity.
 * @param pin_b Physical pin ID for channel B. Must be the pair mate of pin_a.
 * @param config Pointer to timing and range configuration.
 * @param timeout_us Upper bound for this start, in microseconds. Must be
 *        non-zero and strictly below 2^31. Checked before hardware.
 * @return KM_OK on success.
 *         KM_ERR_INVALID_PARAM if pins, config, timeout, or timing are invalid, or if called with IRQs disabled.
 *         KM_ERR_UNAVAILABLE if the scheduler clock is unavailable, or if the
 *         STM32H723 local pair API is not compiled in (no pin I/O and no
 *         ordinary setup fallback).
 *         KM_ERR_BUSY if the reader is already running (state stays RUNNING),
 *         the shared unit is owned, or this reader already set up a different
 *         ordered pair (reader stays STOPPED, no timer is armed, and the
 *         original pair is still restartable).
 *         KM_ERR_TIMEOUT if the remaining budget is exhausted before setup, or
 *         the driver reports timeout (reader latched FAULTED), or completion
 *         time reaches the deadline before the acquisition timer is armed
 *         (ordered identity kept when setup already succeeded; no RUNNING and
 *         no valid sample).
 *         KM_ERR_FAULTED if the reader, clock, or MCU is faulted or shut down.
 *         Local setup shutdown latches KM_ADC_STATE_SHUTDOWN.
 */
KM_API km_result_t km_adc_start(uint32_t pin_a, uint32_t pin_b,
                                const km_adc_pair_config_t *config,
                                uint32_t timeout_us);

/**
 * Explicitly stop paired ADC acquisition.
 *
 * Cancels any active hardware conversion for the active channel, removes the scheduled
 * timer, invalidates pending/cached results, and leaves no scheduled work.
 *
 * If currently KM_ADC_STATE_RUNNING, transitions to KM_ADC_STATE_STOPPED and
 * invalidates cached samples. A later start of the same ordered pair reuses
 * the driver handles. km_adc_start rejects a different ordered pair with
 * KM_ERR_BUSY and does not touch hardware. Ordered pin identity from the first
 * successful local setup remains until MCU reset.
 * If currently KM_ADC_STATE_FAULTED or KM_ADC_STATE_SHUTDOWN, cleans up the
 * timer and active conversion but leaves that state latched until MCU restart.
 *
 * @return KM_OK.
 */
KM_API km_result_t km_adc_stop(void);

/**
 * Query the latest completed paired ADC sample record.
 *
 * An ordinary query preserves the paired ADC acquisition state; clock-fault
 * shutdown cancels acquisition and invalidates cached data.
 * Observes and advances the shared scheduler clock (via km_clock_get_time);
 * not pure with respect to clock adapter state and may trigger Klipper emergency
 * shutdown if a clock jump or hardware counter fault is detected.
 * Evaluates sample staleness against the coherent scheduler clock: if the sample's
 * observation age exceeds max_sample_age_us, out_sample->is_valid is set to false.
 * Original service-entry attempt/readiness observations and accepted partial raw
 * data are retained. Completion clock checks do not rewrite those timestamps.
 *
 * @param out_sample Pointer to caller-allocated structure to receive the sample copy.
 * @return KM_OK if a sample record was copied (caller must inspect out_sample->is_valid).
 *         KM_ERR_INVALID_PARAM if out_sample is NULL.
 *         KM_ERR_UNAVAILABLE if no sample has completed yet or reader is stopped.
 *         KM_ERR_FAULTED if reader, clock, or MCU is in fault or shutdown.
 */
KM_API km_result_t km_adc_get_sample(km_adc_pair_sample_t *out_sample);

/* Same query, also returns the coherent time used to assess the record's age.
 * query_time is required and written only when a record was copied (KM_OK).
 * An expired/failed record may still be copied; inspect is_valid and result. */
KM_API km_result_t km_adc_get_sample_at(km_adc_pair_sample_t *out_sample,
                                        km_core_timepoint_t *query_time);

/**
 * Firmware emergency shutdown hook for paired ADC reader.
 *
 * Removes scheduler timer, cancels any active conversion without calling clock APIs,
 * and latches KM_ADC_STATE_SHUTDOWN.
 */
void km_adc_shutdown(void);

#ifdef __cplusplus
}
#endif

#endif /* KLIPPER_MMU_MCU_ADC_H_INCLUDED */
