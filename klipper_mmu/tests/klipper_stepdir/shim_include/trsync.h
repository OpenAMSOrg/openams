// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TRSYNC_H
#define TRSYNC_H

#include <stdint.h>

struct trsync;

struct trsync_signal {
    struct trsync_signal *next;
    void (*func)(struct trsync_signal *tss, uint8_t reason);
};

typedef void (*trsync_signal_func)(struct trsync_signal *tss, uint8_t reason);

void trsync_add_signal(struct trsync *ts, struct trsync_signal *tss,
                       trsync_signal_func func);
struct trsync *trsync_oid_lookup(uint8_t oid);

#endif
