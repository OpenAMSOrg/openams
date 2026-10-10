// SPDX-License-Identifier: GPL-3.0-or-later
#include <assert.h>
#include <stdio.h>
#include "klipper_mmu/event_queue.h"

int main(void)
{
    km_event_queue_t q;
    km_event_queue_init(&q);
    km_event_t e = {0}, out;
    e.type = KM_EVENT_LOAD_COMPLETE; e.req_id = 1000; e.session_id = 700;
    assert(km_event_queue_reserve(&q, 2) == KM_OK);
    for (unsigned i = 0; i < 14; ++i) assert(km_event_queue_push(&q, &e) == KM_OK);
    assert(km_event_queue_push(&q, &e) == KM_ERR_QUEUE_FULL);
    assert(km_event_queue_push_forced(&q, &e) == KM_ERR_QUEUE_FULL);
    assert(km_event_queue_push_reserved(&q, &e) == KM_OK);
    assert(km_event_queue_push_reserved(&q, &e) == KM_OK);
    assert(km_event_queue_peek(&q, &out) == KM_OK && out.seq == 1);
    assert(km_event_queue_ack(&q, 2) == KM_ERR_STALE);
    assert(km_event_queue_peek(&q, &out) == KM_OK && out.seq == 1);
    for (uint32_t i = 1; i <= 16; ++i) {
        assert(km_event_queue_peek(&q, &out) == KM_OK);
        assert(out.seq == i && out.req_id == 1000 && out.session_id == 700);
        assert(km_event_queue_ack(&q, out.seq) == KM_OK);
    }
    assert(km_event_queue_pop(&q, &out) == KM_ERR_QUEUE_EMPTY);
    assert(km_event_queue_overflow_count(&q) == 0);
    q.next_seq = UINT32_MAX;
    assert(km_event_queue_push(&q, &e) == KM_OK);
    assert(km_event_queue_push(&q, &e) == KM_OK);
    assert(km_event_queue_pop(&q, &out) == KM_OK && out.seq == UINT32_MAX);
    assert(km_event_queue_pop(&q, &out) == KM_OK && out.seq == 1);
    puts("Event reservation, retained delivery, exact acknowledgement and wrap tests passed.");
    return 0;
}
