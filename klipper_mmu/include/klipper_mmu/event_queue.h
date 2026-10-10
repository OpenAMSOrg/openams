// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_EVENT_QUEUE_H
#define KLIPPER_MMU_EVENT_QUEUE_H

#include "klipper_mmu/types.h"

#ifdef __cplusplus
extern "C" {
#endif

#define KM_EVENT_FLAG_NONE       0x00u
#define KM_EVENT_FLAG_OVERFLOW   0x01u
/* Only on PTFE_CAL_COMPLETE: value is completed feeder microsteps from HUB to
 * qualified FPS arrival, a step estimate. Without it the value is encoder clicks. */
#define KM_EVENT_FLAG_STEP_ESTIMATE 0x02u

typedef enum {
    KM_EVENT_NONE               = 0,
    KM_EVENT_LOAD_COMPLETE      = 1,
    KM_EVENT_LOAD_CANCELLED     = 2,
    KM_EVENT_LOAD_FAILED        = 3,
    KM_EVENT_UNLOAD_COMPLETE    = 4,
    KM_EVENT_UNLOAD_CANCELLED   = 5,
    KM_EVENT_UNLOAD_FAILED      = 6,
    KM_EVENT_FOLLOW_FAULT       = 7,
    KM_EVENT_PTFE_CAL_COMPLETE  = 8,
    KM_EVENT_PTFE_CAL_CANCELLED = 9,
    KM_EVENT_PTFE_CAL_FAILED    = 10,
    KM_EVENT_STOPPED            = 11,
    KM_EVENT_FAULT_ENTERED      = 12,
    KM_EVENT_PRELOAD_COMPLETE   = 13,
    KM_EVENT_PRELOAD_CANCELLED  = 14,
    KM_EVENT_PRELOAD_FAILED     = 15,
    KM_EVENT_EJECT_COMPLETE     = 16,
    KM_EVENT_EJECT_CANCELLED    = 17,
    KM_EVENT_EJECT_FAILED       = 18,
    KM_EVENT_HOME_COMPLETE      = 19,
    KM_EVENT_HOME_FAILED        = 20
} km_event_type_t;

/**
 * Terminal operation event record.
 */
typedef struct {
    uint32_t seq;
    uint32_t timestamp_ms;
    km_client_id_t client_id;
    uint32_t session_id;
    uint32_t req_id;
    uint8_t lane_idx;
    km_operation_type_t op_type;
    km_event_type_t type;
    uint8_t result_code;
    uint32_t value;
    uint8_t flags;
} km_event_t;

/**
 * Ring buffer queue for terminal events.
 * Lossless for admitted operations with backpressure support.
 */
typedef struct {
    km_event_t events[KM_EVENT_QUEUE_CAPACITY];
    uint8_t head;
    uint8_t tail;
    uint8_t count;
    uint8_t reserved;
    uint32_t next_seq;
    uint32_t overflow_count;
} km_event_queue_t;

void km_event_queue_init(km_event_queue_t *q);
/* Reserve before accepting work. Only the owner may consume/release its slots. */
km_result_t km_event_queue_reserve(km_event_queue_t *q, uint8_t count);
void km_event_queue_release(km_event_queue_t *q, uint8_t count);
km_result_t km_event_queue_push_reserved(km_event_queue_t *q, const km_event_t *event);
/* Transports peek/retransmit until this exact sequence is acknowledged. */
km_result_t km_event_queue_ack(km_event_queue_t *q, uint32_t seq);

/**
 * Push an event into the queue.
 * Returns KM_ERR_QUEUE_FULL if full (does not silently drop events).
 */
km_result_t km_event_queue_push(km_event_queue_t *q, const km_event_t *event);

/**
 * Compatibility push: same backpressure as push; never evicts a terminal event.
 */
km_result_t km_event_queue_push_forced(km_event_queue_t *q, const km_event_t *event);

km_result_t km_event_queue_pop(km_event_queue_t *q, km_event_t *event_out);

km_result_t km_event_queue_peek(const km_event_queue_t *q, km_event_t *event_out);

bool km_event_queue_is_empty(const km_event_queue_t *q);

bool km_event_queue_is_full(const km_event_queue_t *q);

uint32_t km_event_queue_count(const km_event_queue_t *q);

uint32_t km_event_queue_overflow_count(const km_event_queue_t *q);

#ifdef __cplusplus
}
#endif

#endif /* KLIPPER_MMU_EVENT_QUEUE_H */
