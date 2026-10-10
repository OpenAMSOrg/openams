// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef FAKE_CLOCK_H
#define FAKE_CLOCK_H

#include <stdint.h>
#include <stdbool.h>
#include <assert.h>
#include "klipper_mmu/trajectory.h"
#include "stepper_reference.h"

/* Timer ticks per microsecond. A real MCU rate (the slowest STM32 timers run
 * at 48-64 MHz, the H723 at 520): at 1 tick/us a stepper.c segment, with its
 * whole-tick interval and add, cannot follow a fractional step interval
 * within the error bound. Reference clocks are now_us * this, mod 2^32. */
#define KM_FAKE_CLOCK_TICKS_PER_US 64u

/* Drives a km_trajectory_t through the B2 tick API (km_trajectory_window /
 * commit / observe), the same loop shape the MCU adapter uses, standing in
 * for Klipper's stepper timer. The reference executor (tests/stepper_reference)
 * is owned here, by the clock, not by the driver: a driver-bound trajectory's
 * driver is consulted only for enable/fault/readiness (its caps set the
 * minimum step interval, and a fault or a disabled driver stops the move),
 * never cast to a concrete driver type. A driverless trajectory keeps using
 * km_trajectory_update, unaffected.
 *
 * km_ref_position always counts from 0 at km_ref_init, like a real driver's
 * own hardware step counter (mcu/stepdir_gpio.c's s->position) counts from
 * 0 at first activation: a net accumulator since then, never itself rebased.
 * km_trajectory_recover/resync declaring a new absolute baseline on the
 * trajectory is exactly what km_trajectory_t's own position_offset now
 * exists to reconcile against that raw count (src/trajectory.c), so
 * km_trajectory_observe is given km_ref_position raw, unfolded -- the same
 * thing a real MCU adapter passes from its own driver. */
typedef struct {
    uint32_t now_us;
    km_ref_stepper_t ref;
} km_fake_clock_t;

static inline void km_fake_clock_init(km_fake_clock_t *clock, uint32_t start_us)
{
    if (!clock) {
        return;
    }
    clock->now_us = start_us;
    /* This loop is not simulating dispatcher jitter (that's what the
     * contract's two-tick LEAD in a real MCU adapter guards against): it
     * generates and executes each window synchronously in the same tick, so
     * a restart's start clock (now, not now+LEAD) never needs a lead. */
    km_ref_init(&clock->ref, KM_REF_MAX_MOVES, 0);
}

static inline void km_fake_clock_advance(km_fake_clock_t *clock, uint32_t delta_us)
{
    if (clock) {
        clock->now_us += delta_us;
    }
}

/**
 * Execute all scheduled trajectory motion chronologically up to target_us.
 * Bounded event loop; stops exactly at requested time; a driver fault or a
 * disabled driver, noticed between ticks, stops the trajectory the same way
 * a generator refusal does. Returns the trajectory's own fault, if any.
 */
static inline km_result_t km_fake_clock_run_trajectory(km_fake_clock_t *clock, km_trajectory_t *traj, uint32_t target_us)
{
    if (!clock || !traj) return KM_ERR_INVALID_PARAM;

    uint32_t tick_us = 1000;

    while (km_time_before(clock->now_us, target_us) && km_trajectory_is_moving(traj)) {
        /* A driver fault stops the move, the same way a step_dir_driver edge
         * call used to fail and propagate through km_trajectory_service. A
         * merely disabled driver is not itself a fault (a move settling to
         * arrival can outlive the driver's own enable window by a tick). */
        if (traj->driver && km_driver_get_fault(traj->driver) != KM_FAULT_NONE) {
            km_trajectory_emergency_stop(traj);
            break;
        }

        uint32_t next_tick = clock->now_us + tick_us;
        if (km_time_after(next_tick, target_us)) {
            next_tick = target_us;
        }
        uint32_t step_us = next_tick - clock->now_us;
        if (step_us == 0) {
            break;
        }

        bool active = km_ref_active(&clock->ref) != 0;
        uint32_t min_int = 1;   /* ticks */
        if (traj->driver && traj->driver->caps.min_step_interval_us) {
            min_int = traj->driver->caps.min_step_interval_us * KM_FAKE_CLOCK_TICKS_PER_US;
        }

        km_trajectory_window_t w;
        km_result_t res = km_trajectory_window(traj, step_us, KM_FAKE_CLOCK_TICKS_PER_US,
                                               min_int, !active, &w);
        if (res != KM_OK) {
            km_trajectory_emergency_stop(traj);
            clock->now_us = target_us;
            km_trajectory_update(traj, clock->now_us);
            return res;
        }

        if (traj->driver) {
            /* All or nothing, like the adapter: a window that does not fit
             * must fail the test, never be committed without its steps. */
            assert(km_ref_room(&clock->ref) >= (int)w.count);
            for (int i = 0; i < w.count; i++) {
                uint32_t now_ticks = clock->now_us * KM_FAKE_CLOCK_TICKS_PER_US;
                int q = km_ref_queue(&clock->ref, now_ticks, now_ticks,
                                     w.segments[i].interval, w.segments[i].count,
                                     w.segments[i].add, w.segments[i].dir);
                assert(q == 0);
            }
            km_trajectory_commit(traj, &w);
            km_ref_step_t steps[128];
            int n_out = 0;
            do {   /* a tick can hold more steps than the buffer */
                km_ref_advance(&clock->ref, next_tick * KM_FAKE_CLOCK_TICKS_PER_US,
                               steps, 128, &n_out);
            } while (n_out == 128);
            km_trajectory_observe(traj, km_ref_position(&clock->ref),
                                  km_ref_active(&clock->ref) != 0);
        } else {
            km_trajectory_commit(traj, &w);
            km_trajectory_observe(traj, traj->model_position_steps, false);
        }
        clock->now_us = next_tick;
    }

    clock->now_us = target_us;
    km_trajectory_update(traj, clock->now_us);
    return KM_OK;
}

#endif /* FAKE_CLOCK_H */
