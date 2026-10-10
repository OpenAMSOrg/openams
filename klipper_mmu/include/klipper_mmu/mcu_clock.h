// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_MCU_CLOCK_H
#define KLIPPER_MMU_MCU_CLOCK_H

#include "klipper_mmu/types.h"
#include "klipper_mmu/operation_core.h"
#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Bounded portable MCU clock bridge state.
 *
 * Converts wrapping hardware timer ticks into the common controller's coherent
 * local {ms, us} timepoint and converts microsecond deadlines back to raw ticks
 * without scheduling early.
 *
 * All structure fields are private implementation details. Callers and tests
 * must interact exclusively through the public API functions. Direct mutation
 * of private fields to fake clock advance is strictly prohibited.
 */
typedef struct {
    uint32_t clock_hz;
    uint32_t last_ticks;
    km_core_timepoint_t current_time;
    uint32_t frac_num;   /* Fractional microsecond residue: numerator in [0, clock_hz - 1] */
    uint32_t sub_ms;     /* Microsecond-within-millisecond remainder in [0, 999] */
    bool initialized;
    bool faulted;
} km_mcu_clock_t;

/**
 * Initialize the MCU clock bridge at a specified raw hardware timer tick and origin.
 *
 * @param clock Pointer to clock structure to initialize.
 * @param clock_hz Raw timer frequency in Hz. Must be in [1000000, UINT32_MAX].
 * @param initial_ticks Initial raw hardware timer sample at epoch.
 * @param origin Initial coherent local timepoint {ms, us}. Must satisfy:
 *               unsigned (origin.us - origin.ms * 1000) < 1000.
 *
 * Init establishes origin at initial_ticks with zero fractional-microsecond phase.
 * Initial ticks are not counted as elapsed time.
 *
 * Reinitialization is fresh/quiescent only and requires all actuator outputs to be
 * stopped, followed by coordinated reinitialization and reconciliation of dependent
 * consumers. It must never be used as an in-flight time-reset recovery trick.
 *
 * @return KM_OK on success.
 *         KM_ERR_INVALID_PARAM if clock is NULL, clock_hz < 1000000, or origin is incoherent.
 */
KM_API km_result_t km_mcu_clock_init(km_mcu_clock_t *clock,
                                     uint32_t clock_hz,
                                     uint32_t initial_ticks,
                                     km_core_timepoint_t origin);

/**
 * Observe one raw hardware timer tick sample.
 *
 * Calculates the modular tick difference delta = ticks - last_ticks.
 * - Repeated identical ticks (delta == 0) are permitted and leave the timepoint unchanged.
 * - Valid forward samples require delta < 2^31 (the raw tick horizon).
 * - Backward or half-range-ambiguous deltas (delta >= 2^31) latch an internal clock
 *   fault and return KM_ERR_FAULTED without advancing last valid time or modifying *out.
 *
 * Once faulted, the clock stays latched through any subsequent observations.
 * A full unseen timer cycle (2^32 ticks) is mathematically undetectable; the caller
 * must ensure the timer is sampled within the < 2^31 tick horizon in every lifecycle state.
 *
 * Result precedence:
 * 1. KM_ERR_INVALID_PARAM if clock or out is NULL (healthy clock is not faulted).
 * 2. KM_ERR_UNAVAILABLE for a zero-initialized or invalid-init clock (*out untouched).
 * 3. KM_ERR_FAULTED if clock is already faulted (*out untouched).
 * 4. KM_ERR_FAULTED if delta >= 2^31 (latches fault, *out untouched).
 * 5. KM_OK on successful observation (*out updated).
 *
 * @param clock Pointer to initialized clock.
 * @param ticks Raw hardware timer tick sample.
 * @param out Receives the updated coherent local timepoint.
 * @return Result code following documented precedence.
 */
KM_API km_result_t km_mcu_clock_observe(km_mcu_clock_t *clock,
                                        uint32_t ticks,
                                        km_core_timepoint_t *out);

/**
 * Retrieve a pure snapshot of the last accepted coherent timepoint.
 *
 * This function does not perform a hardware read, advance time, or clear faults.
 *
 * Result precedence:
 * 1. KM_ERR_INVALID_PARAM if clock or out is NULL.
 * 2. KM_ERR_UNAVAILABLE for a zero-initialized or invalid-init clock (*out untouched).
 * 3. KM_ERR_FAULTED if clock is faulted (*out untouched).
 * 4. KM_OK on success (*out receives snapshot).
 *
 * @param clock Pointer to clock.
 * @param out Receives the coherent timepoint snapshot.
 * @return Result code following documented precedence.
 */
KM_API km_result_t km_mcu_clock_get_time(const km_mcu_clock_t *clock,
                                         km_core_timepoint_t *out);

/**
 * Retrieve a pure snapshot of the conservative physical completion timepoint.
 *
 * Rounds the last accepted timepoint toward the future (ceiling) when there is
 * a nonzero fractional microsecond residue (frac_num > 0). If frac_num == 0,
 * the completion timepoint is identical to the exact coherent floor timepoint.
 * Strictly preserves coarse/fine coherence: unsigned (out->us - out->ms * 1000) < 1000.
 * This bound may be ahead of observed time. It constrains physical output
 * deadlines; it must not advance model time or replace command timestamps.
 *
 * Pure conversion at the last accepted snapshot. Does not advance the clock,
 * sample hardware ticks, or clear/latch faults. Both ms and us wrap modulo 2^32.
 *
 * Result precedence:
 * 1. KM_ERR_INVALID_PARAM if clock or out is NULL.
 * 2. KM_ERR_UNAVAILABLE for a zero-initialized or invalid-init clock (*out untouched).
 * 3. KM_ERR_FAULTED if clock is faulted (*out untouched).
 * 4. KM_OK on success (*out receives conservative completion snapshot).
 *
 * @param clock Pointer to clock.
 * @param out Receives the conservative completion timepoint snapshot.
 * @return Result code following documented precedence.
 */
KM_API km_result_t km_mcu_clock_get_completion_time(const km_mcu_clock_t *clock,
                                                    km_core_timepoint_t *out);

/**
 * Retrieve a pure snapshot of the conservative physical completion microsecond.
 *
 * Returns the ceiling of the last accepted timepoint in microseconds:
 * *out_us = current_time.us + (frac_num > 0 ? 1 : 0), wrapping modulo 2^32.
 * If frac_num == 0, *out_us is identical to current_time.us.
 *
 * Pure conversion at the last accepted snapshot. Does not advance the clock,
 * sample hardware ticks, or clear/latch faults.
 *
 * Result precedence:
 * 1. KM_ERR_INVALID_PARAM if clock or out_us is NULL.
 * 2. KM_ERR_UNAVAILABLE for a zero-initialized or invalid-init clock (*out_us untouched).
 * 3. KM_ERR_FAULTED if clock is faulted (*out_us untouched).
 * 4. KM_OK on success (*out_us receives conservative completion microsecond).
 *
 * @param clock Pointer to clock.
 * @param out_us Receives the conservative completion microsecond.
 * @return Result code following documented precedence.
 */
KM_API km_result_t km_mcu_clock_get_completion_us(const km_mcu_clock_t *clock,
                                                  uint32_t *out_us);


/**
 * Map a local microsecond deadline back to raw hardware timer ticks.
 *
 * Pure conversion at the last accepted snapshot. Does not advance the clock
 * or clear/latch faults. Both out_ticks and out_due must be valid pointers;
 * any error leaves both untouched.
 *
 * Interpretation:
 * - Evaluated relative to clock's current us using signed modular difference:
 *   diff_us = deadline_us - now.us.
 * - diff_us == 0 or (int32_t)diff_us < 0 (i.e. diff_us > 0x80000000u): deadline is
 *   equal or past (due). Returns KM_OK, *out_due = true, *out_ticks = last_ticks.
 *   Does not roll overdue edges into a future cycle; caller trajectory lateness
 *   policy evaluates actual current us.
 * - diff_us == 0x80000000u: exact half-range ambiguous difference, returns KM_ERR_INVALID_PARAM.
 * - 0 < diff_us < 0x80000000u: future deadline. Computes required ticks using:
 *   required_ticks = ceil(((deadline_us - now.us) * clock_hz - frac_num) / 1000000).
 *   Rounds toward the future (never early). Preceding tick does not reach the deadline.
 *   If required_ticks < 2^31: returns KM_OK, *out_due = false, *out_ticks = last_ticks + required_ticks.
 *   If required_ticks >= 2^31: returns KM_ERR_INVALID_PARAM (conversion range limit, not a fault).
 *
 * Result precedence:
 * 1. KM_ERR_INVALID_PARAM if clock, out_ticks, or out_due is NULL.
 * 2. KM_ERR_UNAVAILABLE for a zero-initialized or invalid-init clock (outputs untouched).
 * 3. KM_ERR_FAULTED if clock is faulted (*out_ticks, *out_due untouched).
 * 4. KM_ERR_INVALID_PARAM if diff_us == 0x80000000u or required_ticks >= 2^31.
 * 5. KM_OK on successful conversion.
 *
 * @param clock Pointer to clock.
 * @param deadline_us Target deadline in microseconds.
 * @param out_ticks Receives target raw ticks modulo 2^32.
 * @param out_due Receives true if deadline is already due.
 * @return Result code following documented precedence.
 */
KM_API km_result_t km_mcu_clock_deadline_ticks(const km_mcu_clock_t *clock,
                                               uint32_t deadline_us,
                                               uint32_t *out_ticks,
                                               bool *out_due);

#ifdef __cplusplus
}
#endif

#endif /* KLIPPER_MMU_MCU_CLOCK_H */
