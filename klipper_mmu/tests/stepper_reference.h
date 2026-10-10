// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_TEST_STEPPER_REFERENCE_H
#define KLIPPER_MMU_TEST_STEPPER_REFERENCE_H

#include <stdint.h>

/* Portable model of Klipper's stepper.c semantics for one local stepper: a
 * private move pool, moves appended while active, per step
 * next_step_time += interval then interval += add, and a position counting
 * completed signed steps. Times are uint32 timer ticks that wrap; ordering is
 * decided with signed differences. No allocation: one struct per stepper. */

#define KM_REF_MAX_MOVES 64

/* One step that ran: its absolute (wrapped) time and its move's direction
 * (1 increasing phase, 0 decreasing). */
typedef struct {
    uint32_t time;
    uint8_t dir;
} km_ref_step_t;

typedef struct {
    uint32_t interval;      /* interval for the next step of this move */
    uint32_t count;         /* steps still to run in this move */
    int16_t add;            /* added to interval after every step */
    uint8_t dir;            /* 1 or 0; never changes inside a move */
    uint8_t started;        /* first step time has been computed */
} km_ref_move_t;

typedef struct km_ref_stepper {
    uint32_t min_lead_ticks;  /* idle start clock must lead now by this much */
    uint32_t anchor;          /* start clock of the first move after idle */
    uint32_t next_step_time;  /* time of the next step to run */
    uint32_t last_step_time;  /* time of the last step emitted */
    int64_t position;         /* completed signed steps */
    uint8_t pool;             /* move pool capacity, <= KM_REF_MAX_MOVES */
    uint8_t nqueued;          /* moves queued and not yet exhausted */
    uint8_t head;             /* index of the active move in moves[] */
    uint8_t last_step_time_set;
    km_ref_move_t moves[KM_REF_MAX_MOVES];
} km_ref_stepper_t;

void km_ref_init(km_ref_stepper_t *s, uint8_t pool, uint32_t min_lead_ticks);

/* Free moves in the private pool. */
int km_ref_room(const km_ref_stepper_t *s);

/* Append a move. Returns 0 on success, -1 (queueing nothing, never a fault)
 * when count is 0, the pool is full, or the stepper is idle and start_clock
 * is less than min_lead_ticks after now. start_clock is used only when idle. */
int km_ref_queue(km_ref_stepper_t *s, uint32_t now, uint32_t start_clock,
                 uint32_t interval, uint16_t count, int16_t add, uint8_t dir);

/* Run every step due no later than t, recording its time and direction in out.
 * Writes at most max_out steps and sets *n_out to the number recorded. */
void km_ref_advance(km_ref_stepper_t *s, uint32_t t, km_ref_step_t *out,
                    int max_out, int *n_out);

/* Completed signed steps. */
int64_t km_ref_position(const km_ref_stepper_t *s);

/* 1 until the last queued step has run. */
int km_ref_active(const km_ref_stepper_t *s);

/* Drop the queue, return the pool to full, become idle, and require a fresh
 * start clock. Completed position is kept. */
void km_ref_stop(km_ref_stepper_t *s);

#endif
