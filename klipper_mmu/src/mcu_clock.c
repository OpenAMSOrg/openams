// SPDX-License-Identifier: GPL-3.0-or-later
#include "klipper_mmu/mcu_clock.h"
#include <string.h>
#include <stdint.h>
#include <stdbool.h>

KM_API km_result_t km_mcu_clock_init(km_mcu_clock_t *clock,
                                     uint32_t clock_hz,
                                     uint32_t initial_ticks,
                                     km_core_timepoint_t origin)
{
    if (!clock) {
        return KM_ERR_INVALID_PARAM;
    }

    /* Coherence check: unsigned (origin.us - origin.ms * 1000) < 1000.
     * Evaluated using 32-bit modular unsigned arithmetic matching operation_core.c. */
    uint32_t phase = origin.us - origin.ms * 1000u;
    if (clock_hz < 1000000u || phase >= 1000u) {
        /* Make clock inert and uninitialized; do not read uninitialized prior storage. */
        memset(clock, 0, sizeof(*clock));
        return KM_ERR_INVALID_PARAM;
    }

    clock->clock_hz = clock_hz;
    clock->last_ticks = initial_ticks;
    clock->current_time = origin;
    clock->frac_num = 0u;
    clock->sub_ms = phase;
    clock->initialized = true;
    clock->faulted = false;

    return KM_OK;
}

KM_API km_result_t km_mcu_clock_observe(km_mcu_clock_t *clock,
                                        uint32_t ticks,
                                        km_core_timepoint_t *out)
{
    /* Result precedence 1: Invalid parameters (leaves clock healthy and unadvanced) */
    if (!clock || !out) {
        return KM_ERR_INVALID_PARAM;
    }

    /* Result precedence 2: Uninitialized state */
    if (!clock->initialized) {
        return KM_ERR_UNAVAILABLE;
    }

    /* Result precedence 3: Latched clock fault */
    if (clock->faulted) {
        return KM_ERR_FAULTED;
    }

    /* Result precedence 4: Backward or half-range-ambiguous sample latches fault */
    uint32_t delta = ticks - clock->last_ticks;
    if (delta >= 0x80000000u) {
        clock->faulted = true;
        return KM_ERR_FAULTED;
    }

    /* Result precedence 5: Valid observation */
    if (delta > 0u) {
        uint32_t delta_us;
        if (clock->clock_hz % 1000000u == 0u) {
            /* Whole-MHz timers keep frac_num a multiple of 10^6: the same
             * result in 32-bit arithmetic, since ticks_total < per_us + 2^31. */
            uint32_t per_us = clock->clock_hz / 1000000u;
            uint32_t ticks_total = clock->frac_num / 1000000u + delta;
            delta_us = ticks_total / per_us;
            clock->frac_num = ticks_total % per_us * 1000000u;
        } else {
            /* Bounded wide intermediate arithmetic:
             * delta < 2^31, clock_hz >= 10^6, frac_num < clock_hz <= 2^32 - 1.
             * numerator = frac_num + delta * 1000000ULL
             *           <= (2^32 - 1) + (2^31 - 1) * 10^6
             *           <= 4.295e9 + 2.147483647e15 <= 2.147487942e15 < 2^64 - 1.
             * Fits in uint64_t with no silent overflow.
             */
            uint64_t numerator = (uint64_t)clock->frac_num + (uint64_t)delta * 1000000ULL;
            delta_us = (uint32_t)(numerator / (uint64_t)clock->clock_hz);
            clock->frac_num = (uint32_t)(numerator % (uint64_t)clock->clock_hz);
        }

        /* Coarse milliseconds advance from the same conceptual epoch.
         * delta_us <= delta < 2^31.
         * sub_ms < 1000.
         * total_sub < 1000 + 2^31 < 2^32.
         * delta_ms < 2^31 / 1000.
         */
        uint32_t total_sub = clock->sub_ms + delta_us;
        clock->current_time.ms += total_sub / 1000u;
        clock->sub_ms = total_sub % 1000u;
        clock->current_time.us += delta_us;
        clock->last_ticks = ticks;
    }

    *out = clock->current_time;
    return KM_OK;
}

KM_API km_result_t km_mcu_clock_get_time(const km_mcu_clock_t *clock,
                                         km_core_timepoint_t *out)
{
    /* Result precedence 1: Invalid parameters */
    if (!clock || !out) {
        return KM_ERR_INVALID_PARAM;
    }

    /* Result precedence 2: Uninitialized state */
    if (!clock->initialized) {
        return KM_ERR_UNAVAILABLE;
    }

    /* Result precedence 3: Latched clock fault */
    if (clock->faulted) {
        return KM_ERR_FAULTED;
    }

    /* Result precedence 4: Pure coherent snapshot */
    *out = clock->current_time;
    return KM_OK;
}

KM_API km_result_t km_mcu_clock_get_completion_time(const km_mcu_clock_t *clock,
                                                    km_core_timepoint_t *out)
{
    km_result_t result = km_mcu_clock_get_time(clock, out);
    if (result != KM_OK) return result;
    /* Upper bound, not observed time. Preserve the clock's ordinary floor
     * snapshot and advance the copied coarse epoch only at a millisecond edge. */
    if (clock->frac_num) {
        ++out->us;
        if (clock->sub_ms == 999u) ++out->ms;
    }
    return KM_OK;
}

KM_API km_result_t km_mcu_clock_get_completion_us(const km_mcu_clock_t *clock,
                                                  uint32_t *out_us)
{
    if (!out_us) return KM_ERR_INVALID_PARAM;
    km_core_timepoint_t completion;
    km_result_t result = km_mcu_clock_get_completion_time(clock, &completion);
    if (result == KM_OK) *out_us = completion.us;
    return result;
}

KM_API km_result_t km_mcu_clock_deadline_ticks(const km_mcu_clock_t *clock,
                                               uint32_t deadline_us,
                                               uint32_t *out_ticks,
                                               bool *out_due)
{
    /* Result precedence 1: Invalid parameters (output pointers must both be valid) */
    if (!clock || !out_ticks || !out_due) {
        return KM_ERR_INVALID_PARAM;
    }

    /* Result precedence 2: Uninitialized state */
    if (!clock->initialized) {
        return KM_ERR_UNAVAILABLE;
    }

    /* Result precedence 3: Latched clock fault */
    if (clock->faulted) {
        return KM_ERR_FAULTED;
    }

    uint32_t diff_us = deadline_us - clock->current_time.us;

    /* Exactly half range difference is ambiguous */
    if (diff_us == 0x80000000u) {
        return KM_ERR_INVALID_PARAM;
    }

    /* Deadlines equal to or earlier than current us are due.
     * diff_us == 0 is equal; diff_us > 0x80000000u represents a past deadline.
     */
    if (diff_us == 0u || diff_us > 0x80000000u) {
        *out_due = true;
        *out_ticks = clock->last_ticks;
        return KM_OK;
    }

    /* Future deadline: 0 < diff_us < 0x80000000u.
     * required_ticks = ceil(((deadline_us - now.us) * clock_hz - frac_num) / 1000000).
     *
     * Intermediate bounds:
     * diff_us <= 2^31 - 1
     * clock_hz <= 2^32 - 1
     * diff_us * clock_hz <= (2^31 - 1) * (2^32 - 1) = 9223372030412324865 < 2^63.
     * frac_num < clock_hz <= diff_us * clock_hz (since diff_us >= 1).
     * numerator = diff_us * clock_hz - frac_num >= 1.
     * numerator + 999999ULL <= 9223372030413324864 < UINT64_MAX.
     * Fits in uint64_t without overflow.
     */
    uint64_t required_ticks;
    if (clock->clock_hz % 1000000u == 0u) /* frac_num is a multiple of 10^6: exact */
        required_ticks = (uint64_t)diff_us * (clock->clock_hz / 1000000u) - clock->frac_num / 1000000u;
    else
        required_ticks = ((uint64_t)diff_us * clock->clock_hz - clock->frac_num + 999999ULL) / 1000000ULL;

    /* Range check: required ticks must be strictly below 2^31 */
    if (required_ticks >= 0x80000000ULL) {
        return KM_ERR_INVALID_PARAM;
    }

    *out_due = false;
    *out_ticks = clock->last_ticks + (uint32_t)required_ticks;
    return KM_OK;
}
