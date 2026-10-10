// SPDX-License-Identifier: GPL-3.0-or-later
// Minimal shim of Klipper's src/sched.h for a standalone build of
// sim/board/tmc2209.c: only the pieces that file actually uses.
#ifndef SHIM_SCHED_H
#define SHIM_SCHED_H
#include <stdint.h>

#define DECL_INIT(FUNC)

struct timer {
    struct timer *next;
    uint_fast8_t (*func)(struct timer *);
    uint32_t waketime;
};

enum { SF_DONE = 0, SF_RESCHEDULE = 1 };

void sched_add_timer(struct timer *t);

#endif
