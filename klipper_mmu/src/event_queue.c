// SPDX-License-Identifier: GPL-3.0-or-later
#include "klipper_mmu/event_queue.h"
#include <string.h>

void km_event_queue_init(km_event_queue_t *q)
{
    if (!q) return;
    memset(q, 0, sizeof(*q));
    q->next_seq = 1;
}

km_result_t km_event_queue_reserve(km_event_queue_t *q, uint8_t count)
{
    if (!q || !count) return KM_ERR_INVALID_PARAM;
    if ((unsigned)q->count + q->reserved + count > KM_EVENT_QUEUE_CAPACITY)
        return KM_ERR_QUEUE_FULL;
    q->reserved = (uint8_t)(q->reserved + count);
    return KM_OK;
}

void km_event_queue_release(km_event_queue_t *q, uint8_t count)
{
    if (q && count <= q->reserved) q->reserved = (uint8_t)(q->reserved - count);
}

static km_result_t push(km_event_queue_t *q, const km_event_t *event, bool reserved)
{
    if (!q || !event) return KM_ERR_INVALID_PARAM;
    if (reserved && !q->reserved) return KM_ERR_INVALID_PARAM;
    if (q->count >= KM_EVENT_QUEUE_CAPACITY || (!reserved && km_event_queue_is_full(q)))
        return KM_ERR_QUEUE_FULL;
    if (reserved) --q->reserved;
    q->events[q->tail] = *event;
    q->events[q->tail].seq = q->next_seq++;
    if (!q->next_seq) q->next_seq = 1; /* Zero is never an acknowledgement identity. */
    q->tail = (uint8_t)((q->tail + 1u) % KM_EVENT_QUEUE_CAPACITY);
    ++q->count;
    return KM_OK;
}

km_result_t km_event_queue_push(km_event_queue_t *q, const km_event_t *event)
{
    return push(q, event, false);
}

km_result_t km_event_queue_push_reserved(km_event_queue_t *q, const km_event_t *event)
{
    return push(q, event, true);
}

km_result_t km_event_queue_push_forced(km_event_queue_t *q, const km_event_t *event)
{
    /* Compatibility entry point. Urgency NEVER permits evicting a terminal. */
    return push(q, event, false);
}

km_result_t km_event_queue_peek(const km_event_queue_t *q, km_event_t *out)
{
    if (!q || !out) return KM_ERR_INVALID_PARAM;
    if (!q->count) return KM_ERR_QUEUE_EMPTY;
    *out = q->events[q->head];
    return KM_OK;
}

km_result_t km_event_queue_ack(km_event_queue_t *q, uint32_t seq)
{
    if (!q || !seq) return KM_ERR_INVALID_PARAM;
    if (!q->count) return KM_ERR_QUEUE_EMPTY;
    if (q->events[q->head].seq != seq) return KM_ERR_STALE;
    q->head = (uint8_t)((q->head + 1u) % KM_EVENT_QUEUE_CAPACITY);
    --q->count;
    return KM_OK;
}

km_result_t km_event_queue_pop(km_event_queue_t *q, km_event_t *out)
{
    km_result_t result = km_event_queue_peek(q, out);
    return result == KM_OK ? km_event_queue_ack(q, out->seq) : result;
}

bool km_event_queue_is_empty(const km_event_queue_t *q) { return !q || !q->count; }
bool km_event_queue_is_full(const km_event_queue_t *q)
{
    return q && (unsigned)q->count + q->reserved >= KM_EVENT_QUEUE_CAPACITY;
}
uint32_t km_event_queue_count(const km_event_queue_t *q) { return q ? q->count : 0; }
uint32_t km_event_queue_overflow_count(const km_event_queue_t *q) { return q ? q->overflow_count : 0; }
