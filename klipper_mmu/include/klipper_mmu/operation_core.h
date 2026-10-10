// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_OPERATION_CORE_H
#define KLIPPER_MMU_OPERATION_CORE_H

#include "klipper_mmu/types.h"
#include "klipper_mmu/driver.h"
#include "klipper_mmu/trajectory.h"
#include "klipper_mmu/buffer_sensor.h"
#include "klipper_mmu/follower.h"
#include "klipper_mmu/current_regulator.h"
#include "klipper_mmu/event_queue.h"
#include "klipper_mmu/device_profile.h"
#include "klipper_mmu/protocol_wire.h"

#ifdef __cplusplus
extern "C" {
#endif

/* LOAD feeds at most this many times; see docs/fps-arrival.md, LOAD retries. */
#define KM_LOAD_ATTEMPTS 3u
/* A LOAD with its retries ends within this, below OpenAMS's 300 s disconnect
 * backstop. */
#define KM_LOAD_BUDGET_MS 240000u

/* Where and why one LOAD attempt ended: a failed attempt, a failed retract
 * (stage LOAD_RETRY_RETRACT) or arrival (fault NONE). */
typedef struct {
    uint8_t stage;         /* km_motion_stage_t */
    uint8_t fault;         /* km_fault_t */
    uint32_t travel_steps; /* Completed feeder microsteps since the feed began,
                            * or since HUB in LOAD_FEED_TO_BUFFER. */
} km_load_attempt_t;

/**
 * Shared operation core managing admission, path arbitration,
 * full request identity, lifecycle state machines, and local protection.
 */
typedef struct {
    km_device_profile_t profile;
    km_trajectory_t trajectory;
    km_follower_t follower;
    km_buffer_sensor_t buffer_sensor;
    km_current_regulator_t current_regulator;
    km_event_queue_t event_queue;

    /* Per-lane driver actuator bindings */
    km_driver_t *feeder_drivers[KM_MAX_LANES];
    km_driver_t *rewinder_drivers[KM_MAX_LANES];
    km_driver_t *selector_driver;
    km_servo_t *selector_servo;
    km_driver_t *active_feeder;
    km_driver_t *active_rewinder;

    /* Shared-drive selector state (profile.has_selector; klipper_mmu/selector.h).
     * selector_gate/selector_engaged are meaningful only while selector_engaged
     * is true; homed stepper kinds also need selector_homed. */
    km_selector_home_t selector_home;
    bool selector_homed;
    bool selector_engaged;
    uint8_t selector_gate;
    bool selector_endstop_asserted, selector_endstop_valid;
    uint32_t selector_endstop_sample_us;
    /* INDEXED only: one index switch per gate, sampled like selector_endstop_*
     * above but per lane (km_core_set_selector_gate_switch_at). */
    bool selector_gate_asserted[KM_MAX_LANES], selector_gate_valid[KM_MAX_LANES];
    uint32_t selector_gate_sample_us[KM_MAX_LANES];
    bool select_pending;      /* Selection is running; the op's own motion is deferred. */
    bool select_releasing;    /* SELECT_MOVE is a ROTARY post-op release, not a pre-op selection. */
    bool select_backoff;      /* SELECT_MOVE clears a pressed endstop before homing. */
    bool select_centering;    /* INDEXED: SELECT_MOVE is the post-find centering nudge. */
    uint8_t select_seek_gate; /* INDEXED: the gate this SELECT_HOME/SELECT_MOVE is seeking. */
    bool select_flag_a;       /* LOAD: from_preload. UNLOAD: local_preload. CALIBRATE: encoder. */
    bool select_flag_b;       /* UNLOAD: virtual_preload_clear. */
    int64_t select_target_steps; /* SELECT_MOVE: final target position. */
    /* The selector axis's own last-known absolute position (0 at its home),
     * restored onto the single shared trajectory every time it is rebound to
     * the selector stepper: gear motion in between (LOAD/UNLOAD, relative to
     * its own origins) otherwise leaves the trajectory's position wherever
     * feeding/retracting stopped, meaningless as a selector target delta. */
    int64_t selector_position_steps;
    /* selector_position_steps at the moment the standalone HOME op (not a
     * lane selection's own auto-home) was requested, before any pressed-switch
     * backoff: HOME_COMPLETE's value is the net travel from here to the
     * switch, so a selector already at home (backs off, then finds the
     * switch again) reports ~0 instead of the backoff-and-return distance. */
    int64_t home_start_steps;
    /* The gear's own motion limits, restored after the selector's home/move
     * phase retargets the single shared trajectory to its own limits. */
    uint32_t base_max_velocity_steps_s, base_acceleration_steps_s2, base_deceleration_steps_s2;

    /* Arbiter and full request identity */
    km_client_id_t active_client;
    uint32_t active_session_id;
    uint32_t active_req_id;
    km_operation_type_t active_op;
    uint8_t active_lane;

    /* Logical path ownership and physical occupancy */
    bool shared_path_owned;      /* True while an active operation owns the path */
    uint8_t path_owner_lane;
    bool path_occupied;          /* Retained lane reservation, including lane-local PRELOAD. */
    uint8_t path_occupant_lane;

    /* Lifecycle state */
    km_op_state_t op_state;
    km_motion_stage_t stage;
    km_fault_t system_fault;
    km_fault_t motion_fault;
    bool host_connected;
    bool motor_ready;

    /* Completed terminal operation record for idempotent duplicate replay */
    bool has_last_terminal_event;
    km_event_t last_terminal_event;
    km_event_t terminal_history[8];
    uint8_t terminal_history_count;
    uint8_t terminal_history_next;
    uint32_t client_session[KM_MAX_CLIENTS];
    uint32_t client_request[KM_MAX_CLIENTS];
    uint8_t event_reservations;
    bool terminal_pending;
    km_event_type_t settling_event;
    bool pending_fault_event;
    km_event_t fault_event;
    bool bay_valid[KM_MAX_LANES];
    uint32_t bay_sample_ms[KM_MAX_LANES];
    bool load_valid[KM_MAX_LANES];
    uint32_t load_sample_ms[KM_MAX_LANES];
    uint32_t path_sample_ms;
    uint32_t encoder_sample_ms;
    uint16_t measured_rewind_ma;
    uint32_t rewind_sample_ms;
    bool rewind_sample_valid;
    bool rewind_pending;
    bool unload_from_load_preload; /* Retract to lane LOAD switch, not hub. */
    uint32_t last_control_ms;
    uint32_t last_host_heartbeat_ms;
    uint32_t last_control_us;
    bool control_clock_valid; /* Independent of trajectory initialization/faults. */

    /* Sensor presence inputs */
    bool bay_sensors_valid;
    bool hub_sensor_valid;
    /* Virtual hub has no independent sensor; this cache projects only the
     * retained or active lane. Other LOAD readings never overwrite it. */
    bool toolhead_sensor_valid;
    bool filament_present_bay[KM_MAX_LANES];
    bool filament_present_load[KM_MAX_LANES];
    bool filament_present_hub;
    bool filament_present_toolhead;
    int32_t encoder_clicks;
    bool encoder_valid;

    /* Diagnostic only (docs/diagnostics.md): accepted BAY/LOAD/HUB level
     * changes observed while that lane (or, for HUB, any lane) was not being
     * driven by this MMU. Saturating; never read by control logic. */
    uint16_t bay_idle_edges[KM_MAX_LANES];
    uint16_t load_idle_edges[KM_MAX_LANES];
    uint16_t hub_idle_edges;

    /* Deadlines & watchdog tracking */
    uint32_t stage_entered_time_ms;
    uint32_t last_progress_time_ms;
    int32_t last_progress_clicks;

    /* FPS arrival evidence; reset whenever LOAD or calibration enters FEED_TO_BUFFER. */
    int64_t fps_hub_steps;   /* Completed feeder position at observed HUB. */
    uint32_t fps_min_ms;     /* First control observation at minimum travel. */
    bool fps_min_reached;
    uint32_t fps_sample_ms;  /* Last consumed buffer acquisition time. */
    uint32_t fps_start_ms;   /* Acquisition time of the candidate's first sample. */
    uint8_t fps_count;       /* Distinct samples in the current candidate. */
    bool fps_armed;          /* Uncompressed sample seen after minimum travel. */

    /* The latest admitted LOAD and its attempt records, kept for the host's
     * report until the next LOAD admission. */
    km_request_identity_t load_identity;
    km_load_attempt_t load_attempts[KM_LOAD_ATTEMPTS];
    uint8_t load_attempt_count;
    uint32_t load_started_ms; /* Admission, for KM_LOAD_BUDGET_MS. */

    /* PTFE Calibration data. Without an encoder and toolhead switch, a profile
     * with FPS arrival estimates HUB-to-FPS travel in completed microsteps. */
    int32_t cal_start_clicks;
    bool cal_step_estimate;
    uint32_t calibrated_ptfe_value; /* Encoder clicks, or microsteps if estimated. */

    /* Calibrated LOAD (FPS arrival devices). HUB-to-arrival travel in
     * completed microsteps: the profile's stored value, or the one this
     * session's CALIBRATE measured. Zero means unknown and LOAD is refused. */
    uint32_t calibrated_arrival_steps;
    bool load_crawl; /* LOAD has retargeted to the crawl and stays there. */
} km_operation_core_t;

/**
 * Coherent local monotonic timepoint.
 * Represents the adapter's local command dispatch observation.
 * Both values represent the same sampled local instant derived from a conceptual
 * unwrapped total microsecond counter:
 * ms = floor(total_microseconds / 1000) modulo 2^32
 * us = total_microseconds modulo 2^32
 * These are MCU-local dispatch times, not host-supplied scheduling timestamps.
 * Calls into the core and trajectory must be serialized. Service the core with
 * coherent intervals < 2^31 us even while idle/faulted. A first coherent sample
 * may have any epoch. Commands must not precede the last validated core time or
 * a newer non-faulted trajectory service. See docs/operation-time.md.
 */
typedef struct {
    uint32_t ms;
    uint32_t us;
} km_core_timepoint_t;

/* A presence reading is usable only when configured, valid and fresh.
 * `present` is retained for diagnostics even when a configured reading has
 * expired; consumers must never interpret an invalid reading as absence. */
typedef struct {
    bool configured;
    bool present;
    bool valid;
    bool fresh;
    uint32_t sample_ms;
} km_sensor_sample_t;

/* The km_sensor_* wire parts always carry 12 lanes (mcu/messages.c); lanes
 * past the build's KM_MAX_LANES stay zero. */
#define KM_SENSOR_WIRE_LANES 12u

typedef struct {
    uint8_t lane_count;
    uint32_t observed_ms;
    km_sensor_sample_t bays[KM_SENSOR_WIRE_LANES];
    km_sensor_sample_t loads[KM_SENSOR_WIRE_LANES];
    km_sensor_sample_t hub;
    km_sensor_sample_t toolhead;
} km_sensor_snapshot_t;

/**
 * Initialize operation core with a device profile.
 */
KM_API km_result_t km_core_init(km_operation_core_t *core, const km_device_profile_t *profile);

/**
 * Bind drivers for a specific lane.
 * Distinct lanes must route to distinct configured feeder/rewinder drivers.
 */
KM_API km_result_t km_core_bind_lane_driver(km_operation_core_t *core,
                                     uint8_t lane_idx,
                                     km_driver_t *feeder,
                                     km_driver_t *rewinder);

/**
 * Selector machines (profile.has_selector; klipper_mmu/selector.h).
 * stepper: the selector axis driver (NULL for KM_SELECTOR_SERVO).
 * servo: the grip or selection servo (NULL when profile.selector.has_servo
 * is false). The gear is bound separately, to every lane, through
 * km_core_bind_lane_driver.
 */
KM_API km_result_t km_core_bind_selector(km_operation_core_t *core,
                                         km_driver_t *stepper, km_servo_t *servo);

/* The selector home endstop, sampled like any other switch. */
KM_API void km_core_set_selector_endstop_at(km_operation_core_t *core, bool asserted,
                                            bool valid, uint32_t sample_us);

/* INDEXED only: gate `gate`'s own index switch, sampled like the endstop
 * above. Every other kind ignores this. */
KM_API void km_core_set_selector_gate_switch_at(km_operation_core_t *core, uint8_t gate,
                                                bool asserted, bool valid, uint32_t sample_us);

/* Host-calibrated gate positions (selector microsteps from home), replacing
 * the Kconfig defaults. Refused while an operation runs. -1 marks a gate
 * deliberately uncalibrated (Happy Hare's own sentinel); selecting it is then
 * refused at admission until a real position is set. */
KM_API km_result_t km_core_set_selector_gate_steps(km_operation_core_t *core,
                                                   const int32_t *steps, uint8_t count);

/* Runtime, idle-only calibration writer for one gate: refused unless the core
 * is fully idle (KM_STAGE_STOPPED, no active op, not faulted) -- stricter than
 * km_core_set_selector_gate_steps above, which a FOLLOW in progress still
 * permits. `value` is selector microsteps for LINEAR/ROTARY (-1 uncalibrated,
 * or [0, home.max_travel_steps)); for SERVO it is a servo pulse width in
 * microseconds (0 uncalibrated, or [500, 2500]) -- the MCU command converts
 * the host's degrees with KM_SELECTOR_SERVO_US before calling this. */
KM_API km_result_t km_core_set_selector_gate(km_operation_core_t *core,
                                             uint8_t gate, int32_t value);

/* Runtime, idle-only servo test move for the SERVO selector kind: pulses the
 * servo directly (0 stops the pulse train) without touching any gate's
 * calibrated angle or selector_engaged/selector_gate, so the host can jog the
 * servo to find an angle before saving it with km_core_set_selector_gate.
 * Refused unless the core is fully idle, same as km_core_set_selector_gate. */
KM_API km_result_t km_core_selector_servo_test(km_operation_core_t *core, uint16_t pulse_us);

/* Happy Hare's MMU_MOTORS_OFF equivalent: idle-only (same check as the two
 * functions above), shuts the selector driver and the shared gear down
 * without faulting, and clears selector_homed/selector_engaged so the next
 * selection re-homes -- lets the operator move the selector by hand to
 * align it at a gate before KLIPPER_MMU_CALIBRATE_SELECTOR. */
KM_API km_result_t km_core_selector_motors_off(km_operation_core_t *core);

/* Runtime restore of the calibrated HUB-to-arrival distance, in reference
 * feeder microsteps (0 clears it, refusing LOAD until a fresh CALIBRATE).
 * Used by KLIPPER_MMU_CALIBRATE SAVE=0 to put back what was there before a
 * step-estimated measurement it chose not to keep. Idle-only, the same
 * guard as km_core_set_selector_gate; refused outright without
 * profile.fps_arrival, or for a nonzero value outside
 * [fps_arrival_min_steps, fps_arrival_max_steps]. */
KM_API km_result_t km_core_set_calibrated_arrival_steps(km_operation_core_t *core, uint32_t steps);

/**
 * Convenience binding wrapper for single-lane setups or lane 0.
 */
KM_API void km_core_bind_drivers(km_operation_core_t *core,
                          km_driver_t *feeder,
                          km_driver_t *rewinder,
                          km_driver_t *selector);

/**
 * Set sensor presence inputs.
 * Note: Convenience setters use the core's retained coarse control time.
 * Physical frontends must use explicit timestamped variants like km_core_set_sensors_at
 * which use millisecond acquisition timestamps (no fabricated exact hardware microsecond accuracy).
 */
KM_API void km_core_set_sensors(km_operation_core_t *core,
                         uint8_t lane_idx,
                         bool bay_present,
                         bool hub_present,
                         bool toolhead_present);

/**
 * Update encoder feedback clicks and validity.
 * Note: Uses the core's retained coarse control time.
 */
KM_API void km_core_set_encoder(km_operation_core_t *core, int32_t clicks, bool valid);
/* Physical frontends must publish acquisition time, including explicit invalid samples.
 * This single-lane compatibility setter also supplies a source-lane hint for
 * newly observed path occupancy. Use the batch setter below for an unassociated
 * scan of physical switches. */
KM_API void km_core_set_sensors_at(km_operation_core_t *core, uint8_t lane,
                           bool bay, bool hub, bool toolhead, bool valid, uint32_t sample_ms);
/* Publish a raw lane-local LOAD switch with its actual acquisition time.
 * An invalid sample retains its value for diagnostics but cannot satisfy admission. */
KM_API void km_core_set_load_sensor_at(km_operation_core_t *core, uint8_t lane,
                                      bool present, bool valid, uint32_t sample_ms);
/* Physical multi-lane input frontend: one acquisition batch, one shared path.
 * bays contains at least profile.lane_count values. Call under the same lock as
 * core control. First observed path occupancy without an operation owner has
 * KM_LANE_UNKNOWN, regardless of which bay switches are active. Absence and
 * invalid observations never erase retained occupancy; explicit recovery must
 * reconcile it. Invalid arguments leave the core unchanged. */
KM_API km_result_t km_core_set_presence_at(km_operation_core_t *core,
                           const bool *bays, bool hub, bool toolhead,
                           bool valid, uint32_t sample_ms);
/* Read the physical presence inputs at a caller-observed MCU time. Call under
 * the same serialization as sensor updates. This never alters path ownership
 * or substitutes a logical occupancy inference for a physical reading. */
KM_API km_result_t km_core_get_sensor_snapshot(const km_operation_core_t *core,
                                               uint32_t observed_ms,
                                               km_sensor_snapshot_t *snapshot);
KM_API void km_core_set_encoder_at(km_operation_core_t *core, int32_t clicks, bool valid, uint32_t sample_ms);
KM_API void km_core_set_rewind_current(km_operation_core_t *core, uint16_t ma, bool valid, uint32_t sample_ms);
KM_API km_result_t km_core_find_terminal(const km_operation_core_t *core,
                                 const km_request_identity_t *request, km_event_t *event);
/* Copy the attempt records of LOAD `request` into `out` and return their
 * count; 0 when it is not the latest admitted LOAD. Read-only. */
KM_API uint8_t km_core_get_load_attempts(const km_operation_core_t *core,
                                         const km_request_identity_t *request,
                                         km_load_attempt_t out[KM_LOAD_ATTEMPTS]);
/* Explicit operator reconciliation of an idle path occupied by an unknown lane,
 * e.g. filament observed at MCU boot, or of a stopped operation fault. Never
 * moves or enables outputs, and leaves events, history and session/request
 * high-water marks unchanged. From FAULT it also needs the fault event and
 * every other event acknowledged, then clears the latched motion fault,
 * position uncertainty and owner identity, keeping the confirmed step count;
 * it never clears a driver's own fault, which still reports FAULTED.
 * occupied=false (CLEAR, lane must be KM_LANE_UNKNOWN) needs every configured
 * shared-path sensor, and on a virtual hub every LOAD switch, fresh and clear.
 * occupied=true (ADOPT) asserts that filament in the path is continuous from
 * `lane`: HUB (or that lane's virtual-hub LOAD) must be fresh and present, the
 * lane's BAY/LOAD fresh and present, other virtual-hub LOADs fresh and clear,
 * and a configured TOOLHEAD fresh. BAY presence alone never selects a lane.
 * The state becomes LOADED so that lane can UNLOAD. Rejections change nothing:
 * INVALID_PARAM lane/time, STALE nothing to reconcile, BUSY owner/motion/events,
 * FAULTED unqueued fault, sensor or driver fault or active output,
 * NOT_SUPPORTED no required sensor,
 * UNAVAILABLE stale/invalid sensor, CONFLICT fresh contradicting sensor. */
KM_API km_result_t km_core_reconcile_path(km_operation_core_t *core, bool occupied,
                                          uint8_t lane, km_core_timepoint_t time);
/* Explicit recovery after physical position/path reconciliation; never enables outputs. */
KM_API km_result_t km_core_recover(km_operation_core_t *core, int64_t known_position,
                            bool path_occupied, uint8_t lane, km_core_timepoint_t time);

/**
 * Mark motor power/readiness explicitly.
 * Note: Uses the core's retained coarse control time for faults.
 */
KM_API void km_core_set_motor_ready(km_operation_core_t *core, bool ready);

/* Operation Requests with Full Request Identity */

/* Preferred frontend entry points: preserve the explicit operation and lane.
 * direction is used only for starting FOLLOW. CANCEL is not a new operation:
 * cancel_target takes the complete existing owner identity, including after a
 * successful LOAD has entered automatic following. Neither function establishes
 * a transport/controller instance or acknowledges terminal delivery. Call under
 * the same serialization and MCU-local time contract as the periodic update. */
KM_API km_admission_result_t km_core_request_start(km_operation_core_t *core,
                                            const km_request_identity_t *request,
                                            km_direction_t direction,
                                            km_core_timepoint_t time);
KM_API km_admission_result_t km_core_request_cancel_target(km_operation_core_t *core,
                                            const km_request_identity_t *target,
                                            km_core_timepoint_t time);

/* Compatibility entry points below. Unload/follow infer the active lane and
 * cancel5 does not carry operation/lane; new frontends must use the full-identity
 * entry points above rather than dropping fields from a received request. */
KM_API km_admission_result_t km_core_request_load(km_operation_core_t *core,
                                           km_client_id_t client,
                                           uint32_t session_id,
                                           uint32_t req_id,
                                           uint8_t lane_idx,
                                           km_core_timepoint_t time);

/* With a configured LOAD switch, feed only to that switch and retain the lane
 * reservation. Without it, feed to the hub and retain occupied-path ownership.
 * The full-identity start entry point is preferred by new frontends. */
KM_API km_admission_result_t km_core_request_preload(km_operation_core_t *core,
                                           km_client_id_t client,
                                           uint32_t session_id,
                                           uint32_t req_id,
                                           uint8_t lane_idx,
                                           km_core_timepoint_t time);

/* Reverse the selected lane from BAY to clear, after the shared path is clear.
 * Requires a fresh asserted BAY and fresh clear path/LOAD sensors. */
KM_API km_admission_result_t km_core_request_eject(km_operation_core_t *core,
                                           km_client_id_t client,
                                           uint32_t session_id,
                                           uint32_t req_id,
                                           uint8_t lane_idx,
                                           km_core_timepoint_t time);

KM_API km_admission_result_t km_core_request_unload(km_operation_core_t *core,
                                             km_client_id_t client,
                                             uint32_t session_id,
                                             uint32_t req_id,
                                             km_core_timepoint_t time);

KM_API km_admission_result_t km_core_request_follow(km_operation_core_t *core,
                                             km_client_id_t client,
                                             uint32_t session_id,
                                             uint32_t req_id,
                                             bool enable,
                                             km_direction_t dir,
                                             km_core_timepoint_t time);

KM_API km_admission_result_t km_core_request_ptfe_cal(km_operation_core_t *core,
                                               km_client_id_t client,
                                               uint32_t session_id,
                                               uint32_t req_id,
                                               uint8_t lane_idx,
                                               km_core_timepoint_t time);

KM_API km_admission_result_t km_core_request_cancel5(km_operation_core_t *core,
                                              km_client_id_t client,
                                              uint32_t session_id,
                                              uint32_t req_id,
                                              km_core_timepoint_t time);

KM_API km_admission_result_t km_core_request_cancel4(km_operation_core_t *core,
                                              km_client_id_t client,
                                              uint32_t req_id,
                                              km_core_timepoint_t time);

#define KM_GET_CANCEL_MACRO(_1, _2, _3, _4, _5, NAME, ...) NAME
#define km_core_request_cancel(...) KM_GET_CANCEL_MACRO(__VA_ARGS__, km_core_request_cancel5, km_core_request_cancel4, 0)(__VA_ARGS__)

/**
 * Immediate stop (e.g. limit trip, emergency stop, fatal fault).
 * Usable under any state including active fault.
 */
KM_API void km_core_explicit_stop(km_operation_core_t *core, km_fault_t reason, uint32_t now_ms);

/**
 * Query legacy clearance eligibility. Position uncertainty requires explicit
 * km_core_recover after physical reconciliation; this never resumes motion.
 */
KM_API km_result_t km_core_clear_fault(km_operation_core_t *core);

KM_API void km_core_host_comm_lost(km_operation_core_t *core, uint32_t now_ms);

KM_API void km_core_host_comm_ready(km_operation_core_t *core, uint32_t now_ms);

/**
 * Periodic update loop.
 * Safety and sensor evaluation occurs BEFORE actuator outputs.
 * @param now_ms Coherent MCU-local coarse timestamp (same epoch as now_us).
 * @param now_us Coherent MCU-local fine timestamp (same sample as now_ms).
 * Invalid/backward updates fault before actuator work, using the last validated
 * coarse event time (zero when no coherent observation has yet been accepted).
 */
KM_API void km_core_update(km_operation_core_t *core, uint32_t now_ms, uint32_t now_us);

/**
 * Get internal full status snapshot.
 */
KM_API km_status_snapshot_t km_core_get_snapshot(const km_operation_core_t *core);

#ifdef __cplusplus
}
#endif

#endif /* KLIPPER_MMU_OPERATION_CORE_H */
