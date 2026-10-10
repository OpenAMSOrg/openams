// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef BASECMD_H
#define BASECMD_H

#include <stddef.h>
#include <stdint.h>

typedef void (*oid_type)(uint32_t *);

struct move_node {
    struct move_node *next;
};
struct move_queue_head {
    struct move_node *head;
    struct move_node *tail;
};

void move_queue_setup(struct move_queue_head *mq, int move_size);
void move_queue_push(struct move_node *m, struct move_queue_head *mq);
struct move_node *move_queue_pop(struct move_queue_head *mq);
int move_queue_empty(struct move_queue_head *mq);
void move_queue_clear(struct move_queue_head *mq);
void move_free(void *m);
void *move_alloc(void);

void *oid_alloc(uint8_t oid, oid_type type, size_t size);
void *oid_lookup(uint8_t oid, oid_type type);
void *test_oid_foreach(uint8_t index, oid_type type);
void *alloc_chunk(size_t size);

#define foreach_oid(oid, ptr, type) \
    for ((oid) = 0; (oid) < 8; ++(oid)) \
        if (((ptr) = test_oid_foreach((oid), (type))) != NULL)

#endif
