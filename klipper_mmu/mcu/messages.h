// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_MESSAGES_H
#define KLIPPER_MMU_MESSAGES_H

#include "klipper_mmu/operation_core.h"

/* Task-context response emitters. They do not admit operations, acknowledge
 * events, acquire the clock, or touch outputs. The caller supplies a nonzero
 * controller-instance discriminator established by the configuration lifecycle.
 * These functions do not establish that discriminator themselves. KM_OK means
 * the send was attempted, not delivered: Klipper sendf can drop a busy/full-buffer
 * response. Retain events/snapshots for host-driven retry until acknowledged. */
km_result_t km_send_admission(uint64_t instance, const km_request_identity_t *id,
                              km_admission_result_t result, km_fault_t fault);
km_result_t km_send_event(uint64_t instance, const km_event_t *event);

enum km_status_part {
    KM_STATUS_IDENTITY, KM_STATUS_MOTION, KM_STATUS_TIME,
    KM_STATUS_SESSIONS, KM_STATUS_REQUESTS,
    /* Selector machines only (klipper_mmu/selector.h); homed/gate are zero
     * and meaningless on a build without a selector. */
    KM_STATUS_SELECTOR
};

/* Capture the snapshot and acquisition time once under controller serialization.
 * Serve one part per host request from that private copy with interrupts enabled.
 * Do not pass live core state or burst all parts into Klipper's small TX buffer.
 * Each response carries the same nonzero query ID. */
km_result_t km_send_snapshot_part(uint64_t instance, uint32_t query,
                                  const km_status_snapshot_t *snapshot,
                                  uint32_t acquired_ms, uint32_t acquired_us,
                                  enum km_status_part part);

/* Sensor observations share the status query/capture. Presence is meaningful
 * only with configured, valid and fresh bits set for that sensor.
 * KM_SENSOR_BAYS2/KM_SENSOR_LOADS2 carry lanes 8-11 and are sent only when
 * the profile's lane_count exceeds 8. They are appended after the original
 * five parts so an 8-lane image's part numbers (and wire bytes) are unchanged
 * from before this pair existed. */
enum km_sensor_part {
    KM_SENSOR_FLAGS, KM_SENSOR_BAYS0, KM_SENSOR_BAYS1,
    KM_SENSOR_LOADS0, KM_SENSOR_LOADS1, KM_SENSOR_BAYS2, KM_SENSOR_LOADS2
};
km_result_t km_send_sensor_part(uint64_t instance, uint32_t query,
                                const km_sensor_snapshot_t *snapshot,
                                enum km_sensor_part part);

/* Diagnostic-only counters (docs/diagnostics.md), captured with the same
 * query as the status/sensor snapshot above. Bounce is a rejected-candidate
 * count from mcu/switch_gpio.c's debounce; idle edges are accepted BAY/LOAD/
 * HUB level changes observed while that lane (or, for HUB, any lane) was not
 * being driven by this MMU. gstat/drv_status are the TMC2209 feeder's own
 * last-read registers (0 when a lane has none, or the build has no shared
 * TMC UART), never polled or fault-decoded here. All saturating/raw; never
 * used by control logic. */
typedef struct {
    uint16_t bay_bounce[KM_MAX_LANES];
    uint16_t load_bounce[KM_MAX_LANES];
    uint16_t bay_idle_edges[KM_MAX_LANES];
    uint16_t load_idle_edges[KM_MAX_LANES];
    uint16_t hub_bounce;
    uint16_t hub_idle_edges;
    uint16_t toolhead_bounce;
    uint16_t selector_bounce; /* Selector home endstop; zero without a selector. */
    uint8_t gstat[KM_MAX_LANES];
    uint32_t drv_status[KM_MAX_LANES];
} km_diag_snapshot_t;

enum km_diag_part {
    KM_DIAG_BAY_BOUNCE, KM_DIAG_BAY_IDLE, KM_DIAG_LOAD_BOUNCE, KM_DIAG_LOAD_IDLE,
    KM_DIAG_TMC0, KM_DIAG_TMC1
};
km_result_t km_send_diag_part(uint64_t instance, uint32_t query,
                              const km_diag_snapshot_t *snapshot,
                              enum km_diag_part part);

#endif
