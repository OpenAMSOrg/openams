// SPDX-License-Identifier: GPL-3.0-or-later
#include "stepper_reference.h"

void km_ref_init(km_ref_stepper_t *s, uint8_t pool, uint32_t min_lead_ticks) {
    *s = (km_ref_stepper_t){0};
    s->pool = (uint8_t)(pool > KM_REF_MAX_MOVES ? KM_REF_MAX_MOVES : pool);
    s->min_lead_ticks = min_lead_ticks;
}

int km_ref_room(const km_ref_stepper_t *s) {
    return (int)(s->pool - s->nqueued);
}

int km_ref_queue(km_ref_stepper_t *s, uint32_t now, uint32_t start_clock,
                 uint32_t interval, uint16_t count, int16_t add, uint8_t dir) {
    if (count == 0) {
        return -1;
    }
    if (s->pool == 0 || s->nqueued >= s->pool) {
        return -1;
    }
    if (s->nqueued == 0 &&
        (int32_t)(start_clock - now) < (int32_t)s->min_lead_ticks) {
        return -1;
    }
    uint8_t tail = (uint8_t)((s->head + s->nqueued) % s->pool);
    km_ref_move_t *m = &s->moves[tail];
    m->interval = interval;
    m->count = count;
    m->add = add;
    m->dir = dir;
    m->started = 0;
    if (s->nqueued == 0) {
        s->anchor = start_clock;
        s->last_step_time_set = 0;
    }
    s->nqueued = (uint8_t)(s->nqueued + 1);
    return 0;
}

void km_ref_advance(km_ref_stepper_t *s, uint32_t t, km_ref_step_t *out,
                    int max_out, int *n_out) {
    int n = 0;
    while (s->nqueued > 0 && n < max_out) {
        km_ref_move_t *m = &s->moves[s->head];
        if (!m->started) {
            uint32_t base =
                s->last_step_time_set ? s->last_step_time : s->anchor;
            s->next_step_time = base + m->interval;
            m->started = 1;
        }
        if ((int32_t)(t - s->next_step_time) < 0) {
            break;
        }
        out[n].time = s->next_step_time;
        out[n].dir = m->dir;
        n++;
        s->position += m->dir ? 1 : -1;
        s->last_step_time = s->next_step_time;
        s->last_step_time_set = 1;
        /* stepper.c: after each step the interval grows by add, then the
         * next step is that interval later (steps at I, I+(I+A), ...). */
        uint32_t add_delta = (uint32_t)m->add;
        m->interval = m->interval + add_delta;
        s->next_step_time += m->interval;
        m->count--;
        if (m->count == 0) {
            s->head = (uint8_t)((s->head + 1) % s->pool);
            s->nqueued = (uint8_t)(s->nqueued - 1);
        }
    }
    *n_out = n;
}

int64_t km_ref_position(const km_ref_stepper_t *s) {
    return s->position;
}

int km_ref_active(const km_ref_stepper_t *s) {
    return s->nqueued > 0;
}

void km_ref_stop(km_ref_stepper_t *s) {
    s->nqueued = 0;
    s->head = 0;
    s->last_step_time_set = 0;
}
