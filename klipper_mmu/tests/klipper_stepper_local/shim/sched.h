// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_SCHED_H
#define TEST_SCHED_H

#include <stdint.h>

#define SF_DONE 0
#define SF_RESCHEDULE 1

struct timer {
    struct timer *next;
    uint_fast8_t (*func)(struct timer *);
    uint32_t waketime;
};

struct move_node {
    struct move_node *next;
};

struct move_queue_head {
    struct move_node *head;
    struct move_node *tail;
};

void sched_add_timer(struct timer *t);
void sched_del_timer(struct timer *del);
_Noreturn void shutdown(const char *msg);
void try_shutdown(const char *msg);

#define DECL_SHUTDOWN(FUNC) extern int qc_unused_shutdown_decl

#define move_queue_empty(mq) ((mq)->head == NULL)
#define move_queue_clear(mq) \
    do { (mq)->head = (mq)->tail = NULL; } while (0)

void move_queue_setup(struct move_queue_head *mq, uint8_t size);
void move_queue_push(struct move_node *m, struct move_queue_head *mq);
struct move_node *move_queue_pop(struct move_queue_head *mq);
void *move_alloc(void);
void move_free(void *m);

#endif
