// SPDX-License-Identifier: GPL-3.0-or-later
#include <stdlib.h>
#include <stdint.h>
#include "command.h"
#include "basecmd.h"
#include "trsync.h"

// Mocks for stepper.c command handling
void *move_alloc(void) { return malloc(32); }
void move_queue_setup(struct move_queue_head *mq, int move_size) { (void)move_size; mq->head = mq->tail = NULL; }
void move_queue_push(struct move_node *m, struct move_queue_head *mq) {
    m->next = NULL;
    if (mq->tail)
        mq->tail->next = m;
    else
        mq->head = m;
    mq->tail = m;
}
struct move_node *move_queue_pop(struct move_queue_head *mq) {
    struct move_node *m = mq->head;
    if (m) {
        mq->head = m->next;
        if (!mq->head)
            mq->tail = NULL;
        m->next = NULL;
    }
    return m;
}
int move_queue_empty(struct move_queue_head *mq) {
    return mq->head == NULL;
}
void move_queue_clear(struct move_queue_head *mq) {
    mq->head = mq->tail = NULL;
}
void move_free(void *m) { free(m); }

void *test_oid_foreach(uint8_t index, oid_type type) { (void)index; (void)type; return NULL; }

/* Real Klipper's alloc_chunk() zeroes memory (comment in
 * upstream/drivers-local.patch's stepper_local_setup()); stepper_local's
 * struct stepper_local relies on that for its zero-initialized flags/count. */
void *alloc_chunk(size_t size) { return calloc(1, size); }

struct trsync *trsync_oid_lookup(uint8_t oid) { (void)oid; return NULL; }
void trsync_add_signal(struct trsync *ts, struct trsync_signal *tss,
                       trsync_signal_func func) { (void)ts; (void)tss; (void)func; }
