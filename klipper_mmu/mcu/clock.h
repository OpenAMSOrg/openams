// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_MCU_CLOCK_H_INCLUDED
#define KLIPPER_MMU_MCU_CLOCK_H_INCLUDED

#include "klipper_mmu/types.h"
#include "klipper_mmu/mcu_clock.h"
#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * MMU scheduler clock adapter lifecycle states.
 *
 * Reported in diagnostic replies and query interfaces.
 */
enum km_clock_state {
    KM_CLOCK_STATE_UNAVAILABLE = 0,
    KM_CLOCK_STATE_READY       = 1,
    KM_CLOCK_STATE_FAULTED     = 2,
    KM_CLOCK_STATE_SHUTDOWN    = 3,
};

/**
 * Query current adapter lifecycle state.
 *
 * @return Current lifecycle state (UNAVAILABLE, READY, FAULTED, SHUTDOWN).
 */
enum km_clock_state km_clock_get_state(void);

/**
 * Retrieve a fresh coherent local timepoint {ms, us} from the scheduler clock adapter.
 *
 * Reads the hardware timer tick counter inside an interrupt-disabled critical section,
 * observes the raw tick delta through the portable MCU clock, and returns the
 * updated coherent timepoint.
 *
 * Supports calls from task context or already-disabled timer IRQ context.
 * A newly detected clock fault invokes upstream's non-returning shutdown path.
 * Subsequent calls return KM_ERR_FAULTED without resuming the clock.
 *
 * Result precedence:
 * 1. KM_ERR_INVALID_PARAM: out pointer is NULL.
 * 2. KM_ERR_UNAVAILABLE: adapter is pre-ready / uninitialized (*out preserved).
 * 3. KM_ERR_FAULTED: adapter is in fault or shutdown (*out preserved).
 * 4. KM_OK: successful observation, *out updated with fresh timepoint.
 *
 * @param out Receives the fresh coherent local timepoint.
 * @return Result code following documented precedence.
 */
KM_API km_result_t km_clock_get_time(km_core_timepoint_t *out);

/**
 * Retrieve a fresh coherent local conservative physical completion timepoint from
 * the scheduler clock adapter.
 *
 * Reads the hardware timer tick counter inside an interrupt-disabled critical section,
 * observes the raw tick delta through the portable MCU clock, and returns the
 * updated conservative completion timepoint rounded toward the future (ceiling)
 * when fractional microsecond residue exists (frac_num > 0).
 *
 * Strictly preserves coarse/fine coherence: unsigned (out->us - out->ms * 1000) < 1000.
 * This is an upper completion bound, not observed time. Use km_clock_get_time()
 * for command ordering and model integration; the two can differ by one us.
 *
 * Supports calls from task context or already-disabled timer IRQ context.
 * A newly detected clock fault invokes upstream's non-returning shutdown path.
 * Subsequent calls return KM_ERR_FAULTED without resuming the clock.
 *
 * Result precedence:
 * 1. KM_ERR_INVALID_PARAM: out pointer is NULL.
 * 2. KM_ERR_UNAVAILABLE: adapter is pre-ready / uninitialized (*out preserved).
 * 3. KM_ERR_FAULTED: adapter is in fault or shutdown (*out preserved).
 * 4. KM_OK: successful observation, *out updated with fresh completion timepoint.
 *
 * @param out Receives the fresh coherent local completion timepoint.
 * @return Result code following documented precedence.
 */
KM_API km_result_t km_clock_get_completion_time(km_core_timepoint_t *out);

/**
 * Retrieve a fresh conservative physical completion microsecond timestamp from
 * the scheduler clock adapter.
 *
 * Reads the hardware timer tick counter inside an interrupt-disabled critical section,
 * observes the raw tick delta through the portable MCU clock, and returns the
 * updated conservative completion microsecond:
 * *out_us = current_time.us + (frac_num > 0 ? 1 : 0), wrapping modulo 2^32.
 *
 * Supports calls from task context or already-disabled timer IRQ context.
 * A newly detected clock fault invokes upstream's non-returning shutdown path.
 * Subsequent calls return KM_ERR_FAULTED without resuming the clock.
 *
 * Result precedence:
 * 1. KM_ERR_INVALID_PARAM: out_us pointer is NULL.
 * 2. KM_ERR_UNAVAILABLE: adapter is pre-ready / uninitialized (*out_us preserved).
 * 3. KM_ERR_FAULTED: adapter is in fault or shutdown (*out_us preserved).
 * 4. KM_OK: successful observation, *out_us updated with fresh completion microsecond.
 *
 * @param out_us Receives the fresh completion microsecond.
 * @return Result code following documented precedence.
 */
KM_API km_result_t km_clock_get_completion_us(uint32_t *out_us);


/**
 * Map a local microsecond deadline back to raw hardware timer ticks.
 *
 * Samples raw ticks inside an interrupt-disabled critical section, refreshes the
 * coherent clock observation, and calculates target ticks relative to the updated
 * snapshot without scheduling early.
 *
 * Both out_ticks and out_due must be valid pointers. Caller outputs are strictly
 * preserved on failure.
 * A newly detected clock fault invokes upstream's non-returning shutdown path;
 * subsequent calls return KM_ERR_FAULTED.
 *
 * Result precedence:
 * 1. KM_ERR_INVALID_PARAM: out_ticks or out_due is NULL.
 * 2. KM_ERR_UNAVAILABLE: adapter is pre-ready / uninitialized (outputs preserved).
 * 3. KM_ERR_FAULTED: adapter is in fault or shutdown (outputs preserved).
 * 4. KM_ERR_INVALID_PARAM: ambiguous half-range (diff == 2^31) or conversion range
 *    limit (required_ticks >= 2^31). Outputs preserved.
 * 5. KM_OK: conversion successful.
 *    If deadline is due or past: *out_due = true, *out_ticks = current ticks.
 *    If deadline is future: *out_due = false, *out_ticks = target ticks (rounded up).
 *
 * @param deadline_us Target local deadline in microseconds.
 * @param out_ticks Receives target raw hardware ticks modulo 2^32.
 * @param out_due Receives true if deadline is already due.
 * @return Result code following documented precedence.
 */
KM_API km_result_t km_clock_deadline_ticks(uint32_t deadline_us,
                                           uint32_t *out_ticks,
                                           bool *out_due);

#ifdef __cplusplus
}
#endif

#endif /* KLIPPER_MMU_MCU_CLOCK_H_INCLUDED */
