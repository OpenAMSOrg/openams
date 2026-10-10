// SPDX-License-Identifier: GPL-3.0-or-later
/* Test-only shim (Milestone 1, HH-1): the real klipper-mmu core (src),
 * unmodified, one mock_driver.c feeder+rewinder per lane, and the reference
 * stepper executor (tests/stepper_reference.c), built as a shared library so
 * Happy Hare's own Python test harness (test/hh) can drive PRELOAD and UNLOAD
 * through the operation-level seam described in build/b2/review_hh_harness_review.md.
 *
 * No new core code. The control/motion tick body is lifted from
 * sim/test_loop.c (setup_with_spool, run_motion_tick, run_for), with the
 * causal plant (sim/plant.h) removed: there is no plant here, because Happy
 * Hare's own FilamentPath (test/hh/filament.py) plays that role on the Python
 * side. Each km_hh_run() call reports the net completed steps since it was
 * called; Python converts that to mm with the caller-supplied steps/mm and
 * applies it to HH's model with FilamentPath.advance(), then reads the
 * model's sensors back in with km_hh_sensors() before the next call. Sensor
 * acquisition time is the shim's own tick clock (ms); no extra transport
 * latency is modelled here (HH's own switch drive already imposes distance,
 * not delay).
 */
#include "klipper_mmu/operation_core.h"
#include "mock_driver.h"
#include "stepper_reference.h"
#include <string.h>

#if defined(_WIN32)
#define HH_API __declspec(dllexport)
#else
#define HH_API __attribute__((visibility("default")))
#endif

#define HH_LANES 4u

typedef struct {
    km_operation_core_t core;
    mock_driver_t feeder[HH_LANES];
    mock_driver_t rewinder[HH_LANES];
    km_ref_stepper_t ref;
    uint64_t now_us;
    uint64_t next_heartbeat_us;
    uint32_t req_id;
    bool bay_state[HH_LANES];
} hh_fixture_t;

static hh_fixture_t g_hh;

static uint32_t hh_ms(void) { return (uint32_t)(g_hh.now_us / 1000u); }
static km_core_timepoint_t hh_now(void)
{
    km_core_timepoint_t t = {hh_ms(), (uint32_t)g_hh.now_us};
    return t;
}

HH_API int km_hh_init(double steps_per_mm)
{
    (void)steps_per_mm; /* Kept in the signature for the caller's own record;
                        * the shim reports steps only, Python owns the mm
                        * conversion (and so must pass the same value into
                        * every FilamentPath.advance() it makes from those
                        * steps). See hh_probe.py steps_per_mm(). */
    memset(&g_hh, 0, sizeof(g_hh));
    km_device_profile_t profile;
    km_profile_init_boxturtle(&profile);
    profile.lane_count = (uint8_t)HH_LANES;
    /* HH's shipped boxturtle wires a per-gate mmu_exit switch as well as the
     * shared unit0:mmu_shared_exit hub (test/hh/filament.py layout); the
     * board-startup pin map gives a real BoxTurtle build the same
     * has_load_sensors=true (tests/klipper_board_startup/test_boxturtle_boot.c).
     * The Baseline profile from km_profile_init_boxturtle() leaves it false
     * (no fabricated optional sensor), so it is set explicitly here. */
    profile.has_load_sensors = true;
    /* HH's boxturtle bowden (mmu_entry -150mm to extruder +700mm, plus the
     * +28mm nozzle offset: 878mm end to end) is longer than the Baseline
     * profile's stage deadlines assume, and UNLOAD_RETRACT_TO_HUB runs at
     * default_rewind_velocity_steps_s (operation_core.c:715), about
     * 21 mm/s here - a full-length unload wants ~35 s, well past the 8 s
     * default unload_retract_timeout_ms. Sized to this fixture's geometry,
     * the same way sim/test_loop.c:402 shortens hub_feed_timeout_ms for its
     * fixture; not a change to what the timeout means. */
    profile.unload_retract_timeout_ms = 300000;
    profile.unload_rewind_timeout_ms = 300000;
    if (km_core_init(&g_hh.core, &profile) != KM_OK) return -1;
    for (unsigned i = 0; i < HH_LANES; i++) {
        mock_driver_init(&g_hh.feeder[i], 4, 8, false);
        mock_driver_init(&g_hh.rewinder[i], 4, 8, false);
        if (km_core_bind_lane_driver(&g_hh.core, (uint8_t)i,
                &g_hh.feeder[i].base, &g_hh.rewinder[i].base) != KM_OK)
            return -2;
    }
    km_ref_init(&g_hh.ref, KM_REF_MAX_MOVES, 0);
    km_core_set_motor_ready(&g_hh.core, true);
    km_core_host_comm_ready(&g_hh.core, 0);
    km_core_update(&g_hh.core, 0, 0);
    g_hh.next_heartbeat_us = 100000;
    return 0;
}

HH_API void km_hh_sensors(int lane, int bay, int load, int hub, int toolhead,
                          int low, int high)
{
    (void)low; (void)high; /* Switched buffer low/high: not exercised by PRELOAD/UNLOAD. */
    if (lane < 0 || (unsigned)lane >= HH_LANES) return;
    g_hh.bay_state[(unsigned)lane] = bay != 0;
    uint32_t ms = hh_ms();
    bool bays[HH_LANES];
    for (unsigned i = 0; i < HH_LANES; i++) bays[i] = g_hh.bay_state[i];
    km_core_set_presence_at(&g_hh.core, bays, hub != 0, toolhead != 0, true, ms);
    km_core_set_load_sensor_at(&g_hh.core, (uint8_t)lane, load != 0, true, ms);
}

HH_API int km_hh_request(int op, int lane)
{
    km_core_timepoint_t t = hh_now();
    g_hh.req_id++;
    km_admission_result_t r;
    if (op == 0)
        r = km_core_request_preload(&g_hh.core, KM_CLIENT_HOST, 1, g_hh.req_id, (uint8_t)lane, t);
    else if (op == 1)
        r = km_core_request_unload(&g_hh.core, KM_CLIENT_HOST, 1, g_hh.req_id, t);
    else
        return -1;
    return (int)r;
}

HH_API int km_hh_adopt(int lane)
{
    return (int)km_core_reconcile_path(&g_hh.core, true, (uint8_t)lane, hh_now());
}

/* Debug/report only: not part of the seam, just visibility for the report's
 * "what surprised you" and for local debugging (tools/gdb_probe.py covers
 * anything deeper). */
HH_API void km_hh_debug(int *stage, int *system_fault, int *motion_fault, int *op_state)
{
    *stage = (int)g_hh.core.stage;
    *system_fault = (int)g_hh.core.system_fault;
    *motion_fault = (int)g_hh.core.motion_fault;
    *op_state = (int)g_hh.core.op_state;
}

/* Runs up to `ticks` 1 ms control+motion ticks (the cadence in
 * sim/test_loop.c: control and motion together every 1 ms), stopping early on
 * the first queued core event or once the net completed steps reach
 * max_delta_steps in magnitude. Returns the number of ticks actually run;
 * *delta_steps is the signed net step count over those ticks (Python divides
 * by km_hh_steps_per_mm() and applies it to HH's FilamentPath); *event is the
 * km_event_type_t of the terminal/fault event, or KM_EVENT_NONE. */
HH_API int km_hh_run(int ticks, int32_t max_delta_steps, int32_t *delta_steps, int *event)
{
    int64_t start_pos = km_ref_position(&g_hh.ref);
    *event = (int)KM_EVENT_NONE;
    int ran = 0;
    for (; ran < ticks; ran++) {
        g_hh.now_us += 1000u;
        uint32_t ms = hh_ms();
        if (g_hh.now_us >= g_hh.next_heartbeat_us) {
            km_core_host_comm_ready(&g_hh.core, ms);
            g_hh.next_heartbeat_us += 100000u;
        }
        km_core_update(&g_hh.core, ms, (uint32_t)g_hh.now_us);

        bool active = km_ref_active(&g_hh.ref) != 0;
        uint32_t min_int = 1;
        if (g_hh.core.active_feeder && g_hh.core.active_feeder->caps.min_step_interval_us)
            min_int = g_hh.core.active_feeder->caps.min_step_interval_us;
        km_trajectory_window_t w;
        km_result_t res = km_trajectory_window(&g_hh.core.trajectory, 1000u, 1u,
                                               min_int, !active, &w);
        if (res != KM_OK) {
            km_trajectory_emergency_stop(&g_hh.core.trajectory);
        } else {
            uint32_t start_clock = (uint32_t)(g_hh.now_us - 1000u);
            if (w.count > 0 && km_ref_room(&g_hh.ref) >= (int)w.count) {
                for (int i = 0; i < w.count; i++)
                    (void)km_ref_queue(&g_hh.ref, start_clock, start_clock,
                                       w.segments[i].interval, w.segments[i].count,
                                       w.segments[i].add, w.segments[i].dir);
            }
            km_trajectory_commit(&g_hh.core.trajectory, &w);
            km_ref_step_t steps[256];
            int n_out = 0;
            km_ref_advance(&g_hh.ref, (uint32_t)g_hh.now_us, steps, 256, &n_out);
            km_trajectory_observe(&g_hh.core.trajectory, km_ref_position(&g_hh.ref),
                                  km_ref_active(&g_hh.ref) != 0);
        }

        km_event_t evt;
        if (km_event_queue_pop(&g_hh.core.event_queue, &evt) == KM_OK)
            *event = (int)evt.type;

        int64_t moved = km_ref_position(&g_hh.ref) - start_pos;
        if (moved < 0) moved = -moved;
        if (*event != (int)KM_EVENT_NONE || moved >= max_delta_steps) {
            ran++;
            break;
        }
    }
    *delta_steps = (int32_t)(km_ref_position(&g_hh.ref) - start_pos);
    return ran;
}
