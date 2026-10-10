// SPDX-License-Identifier: GPL-3.0-or-later
#include "commands.h"
#include "autoconf.h"
#include "clock.h"
#include "messages.h"
#include "controller.h"
#include "board/irq.h"
#include "command.h"
#include "sched.h"

static km_operation_core_t *controller;
static uint64_t instance;
static void (*controller_changed)(void);
static bool start_permitted;
static bool start_revoked;
static bool (*start_ready)(void);
static bool shutdown_latched;
static uint32_t last_query;
static uint32_t last_reconcile_query;
static km_status_snapshot_t retained_snapshot;
static km_sensor_snapshot_t retained_sensors;
static km_diag_snapshot_t retained_diag;
/* Wire capability only: this does not enable actuators or physical inputs. */
DECL_CONSTANT("KLIPPER_MMU_SENSOR_LOADS", 1);
static km_core_timepoint_t acquired;

/* mcu/switch_gpio.c (bounce counters) links only in this same build
 * condition as mcu/Makefile's switch_gpio.c/sensors.c/board_startup.c line.
 * Without it, nothing samples a physical switch, so bounce/idle-edge
 * counters (docs/diagnostics.md) are both meaningless; KLIPPER_MMU_DIAG_COUNTERS
 * says so. KLIPPER_MMU_TMC_DIAG separately says whether the TMC parts below
 * are this build's actual gstat/drv_status. */
#if (defined(CONFIG_MACH_STM32H723) && CONFIG_MACH_STM32H723) \
    || (defined(CONFIG_KLIPPER_MMU_F207_BOOT_GATES) && CONFIG_KLIPPER_MMU_F207_BOOT_GATES) \
    || (defined(CONFIG_KLIPPER_MMU_F4X5_BOOT_GATES) && CONFIG_KLIPPER_MMU_F4X5_BOOT_GATES) \
    || (defined(CONFIG_KLIPPER_MMU_F765_BOOT_GATES) && CONFIG_KLIPPER_MMU_F765_BOOT_GATES) \
    || (defined(CONFIG_KLIPPER_MMU_G4_BOOT_GATES) && CONFIG_KLIPPER_MMU_G4_BOOT_GATES) \
    || (defined(CONFIG_KLIPPER_MMU_H743_H750_BOOT_GATES) && CONFIG_KLIPPER_MMU_H743_H750_BOOT_GATES) \
    || (defined(CONFIG_KLIPPER_MMU_G0B_BOOT_GATES) && CONFIG_KLIPPER_MMU_G0B_BOOT_GATES)
#include "switch_gpio.h"
DECL_CONSTANT("KLIPPER_MMU_DIAG_COUNTERS", 1);
static void diag_bounce(uint16_t bay[KM_MAX_LANES], uint16_t load[KM_MAX_LANES],
                        uint16_t *hub, uint16_t *toolhead, uint16_t *selector)
{
    km_switch_gpio_get_bounce(bay, load, hub, toolhead);
    km_switch_gpio_get_selector_bounce(selector);
}
#else
DECL_CONSTANT("KLIPPER_MMU_DIAG_COUNTERS", 0);
static void diag_bounce(uint16_t bay[KM_MAX_LANES], uint16_t load[KM_MAX_LANES],
                        uint16_t *hub, uint16_t *toolhead, uint16_t *selector)
{
    for (unsigned i = 0; i < KM_MAX_LANES; ++i) bay[i] = load[i] = 0;
    *hub = *toolhead = 0;
    *selector = 0;
}
#endif

#if defined(CONFIG_WANT_TMCUART_LOCAL) && CONFIG_WANT_TMCUART_LOCAL && \
    defined(CONFIG_KLIPPER_MMU_TMC_UART_SHARED) && CONFIG_KLIPPER_MMU_TMC_UART_SHARED
#include "tmc_uart_gpio.h"
DECL_CONSTANT("KLIPPER_MMU_TMC_DIAG", 1);
/* The feeder backend uses lane index == TMC UART GPIO slot (mcu/tmc_feeder.c). */
static void tmc_status(uint8_t lane, uint8_t *gstat, uint32_t *drv_status)
{
    km_tmc_uart_status_t status;
    if (controller && lane < controller->profile.lane_count
        && km_tmc_uart_gpio_get_status(lane, &status) == KM_OK) {
        *gstat = (uint8_t)(status.gstat & 0xffu);
        *drv_status = status.drv_status;
    } else {
        *gstat = 0;
        *drv_status = 0;
    }
}
#else
DECL_CONSTANT("KLIPPER_MMU_TMC_DIAG", 0);
static void tmc_status(uint8_t lane, uint8_t *gstat, uint32_t *drv_status)
{
    (void)lane;
    *gstat = 0;
    *drv_status = 0;
}
#endif

void km_commands_shutdown(void);
void command_km_transport_query(uint32_t *args);
void command_km_start(uint32_t *args);
void command_km_cancel(uint32_t *args);
void command_km_heartbeat(uint32_t *args);
void command_km_stop(uint32_t *args);
void command_km_event_peek(uint32_t *args);
void command_km_event_ack(uint32_t *args);
void command_km_terminal_query(uint32_t *args);
void command_km_load_attempts_query(uint32_t *args);
void command_km_snapshot_part(uint32_t *args);
void command_km_sensor_part(uint32_t *args);
void command_km_diag_part(uint32_t *args);
void command_km_switch_delay_query(uint32_t *args);
void command_km_path_reconcile(uint32_t *args);

km_result_t km_commands_attach(km_operation_core_t *core, uint64_t identity,
                               void (*changed)(void), bool allow_start)
{
    irqstatus_t flag = irq_save();
    km_result_t result = KM_OK;
    if (flag || !core || !identity || !changed) result = KM_ERR_INVALID_PARAM;
    else if (shutdown_latched || sched_is_shutdown()) result = KM_ERR_FAULTED;
    else if (controller) result = KM_ERR_BUSY;
#if !defined(CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN) || !CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN
    else if (allow_start) result = KM_ERR_UNAVAILABLE;
#endif
    else if (km_clock_get_state() != KM_CLOCK_STATE_READY) result = KM_ERR_UNAVAILABLE;
    else {
        controller = core;
        instance = identity;
        controller_changed = changed;
        start_permitted = allow_start;
    }
    irq_restore(flag);
    return result;
}

km_result_t km_commands_enable_start(km_operation_core_t *core, uint64_t identity,
                                    bool (*ready)(void))
{
    irqstatus_t flag = irq_save();
    km_result_t result = KM_OK;
    if (flag || !core || !identity || !ready) result = KM_ERR_INVALID_PARAM;
#if !defined(CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN) || !CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN
    else result = KM_ERR_UNAVAILABLE;
#else
    else if (shutdown_latched || sched_is_shutdown() || start_revoked)
        result = KM_ERR_FAULTED;
    else if (controller != core || instance != identity) result = KM_ERR_BUSY;
    else if (km_clock_get_state() != KM_CLOCK_STATE_READY) result = KM_ERR_UNAVAILABLE;
    else if (!ready()) result = KM_ERR_UNAVAILABLE;
    else {
        start_ready = ready;
        start_permitted = true;
    }
#endif
    irq_restore(flag);
    return result;
}

void km_commands_revoke_start(km_operation_core_t *core, uint64_t identity)
{
    if (controller == core && instance == identity) {
        start_permitted = false;
        start_revoked = true;
        start_ready = NULL;
    }
}

void km_commands_shutdown(void)
{
    /* Peripheral hooks own physical safe-off; do not recurse through driver clocks. */
    shutdown_latched = true;
    start_permitted = false;
    start_revoked = true;
    start_ready = NULL;
}
DECL_SHUTDOWN(km_commands_shutdown);

static uint64_t request_instance(const uint32_t *args)
{
    return ((uint64_t)args[0] << 32) | args[1];
}

/* Handlers and the control task run in task context and never preempt each
 * other, so the core needs no lock here. Only the edge interrupt runs
 * meanwhile: controller_changed() exchanges the trajectory with it before a
 * handler plans or captures and after it changes the core. */
static enum km_command_result guard(const uint32_t *args, bool reading_retained)
{
    if (!controller) return KM_COMMAND_UNBOUND;
    if (request_instance(args) != instance) return KM_COMMAND_INSTANCE_MISMATCH;
    if (!reading_retained && (shutdown_latched || sched_is_shutdown()))
        return KM_COMMAND_SHUTDOWN;
    return KM_COMMAND_OK;
}

static void result(enum km_command_kind kind, uint32_t query, enum km_command_result code)
{
    /* Task context with IRQs enabled. */
    sendf("km_command_result instance_hi=%u instance_lo=%u command=%c query=%u result=%c",
          (uint32_t)(instance >> 32), (uint32_t)instance, (uint32_t)kind,
          query, (uint32_t)code);
}

static bool identity(const uint32_t *args, km_request_identity_t *id)
{
    /* Upstream parse_int does not enforce the declared %c range. Check BEFORE casts. */
    if (args[2] > UINT8_MAX || args[5] > UINT8_MAX || args[6] > UINT8_MAX)
        return false;
    *id = (km_request_identity_t){(km_client_id_t)args[2], args[3], args[4],
                                  (km_operation_type_t)args[5], (uint8_t)args[6]};
    return true;
}

void command_km_transport_query(uint32_t *args)
{
    enum km_binding_state state = shutdown_latched || sched_is_shutdown()
        ? KM_BINDING_SHUTDOWN : !controller ? KM_BINDING_UNBOUND
        : km_clock_get_state() == KM_CLOCK_STATE_READY ? KM_BINDING_ATTACHED
        : KM_BINDING_CLOCK_UNAVAILABLE;
    uint32_t query = last_query;
    uint64_t current = instance;
    sendf("km_transport_state challenge=%u instance_hi=%u instance_lo=%u last_query=%u state=%c",
          args[0], (uint32_t)(current >> 32), (uint32_t)current, query,
          (uint32_t)state);
}
DECL_COMMAND_FLAGS(command_km_transport_query, HF_IN_SHUTDOWN,
                   "km_transport_query challenge=%u");

void command_km_switch_delay_query(uint32_t *args)
{
    uint32_t challenge = args[0];
    uint64_t requested = ((uint64_t)args[1] << 32) | args[2];
    uint32_t delay = 0;
    uint32_t count = 0, max_us = 0;
    uint64_t current = instance;
    bool valid = controller && requested == current
        && km_controller_get_switch_sample_delay(&delay);
    if (controller && requested == current)
        km_controller_get_switch_sample_stats(&count, &max_us);
    sendf("km_switch_delay_state challenge=%u instance_hi=%u instance_lo=%u valid=%c delay_us=%u count=%u max_us=%u",
          challenge, (uint32_t)(current >> 32), (uint32_t)current,
          valid ? 1u : 0u, valid ? delay : 0u, count, max_us);
}
DECL_COMMAND_FLAGS(command_km_switch_delay_query, HF_IN_SHUTDOWN,
                   "km_switch_delay_query challenge=%u instance_hi=%u instance_lo=%u");

static void operation(uint32_t *args, bool cancel)
{
    km_request_identity_t id;
    enum km_command_kind kind = cancel ? KM_COMMAND_CANCEL : KM_COMMAND_START;
    enum km_command_result code = guard(args, false);
    if (code == KM_COMMAND_OK && (!identity(args, &id) || (!cancel && args[7] > 1)))
        code = KM_COMMAND_INVALID;
    if (code == KM_COMMAND_OK && !cancel) {
        if (start_revoked || !start_permitted)
            code = KM_COMMAND_UNAVAILABLE;
        else if (start_ready && !start_ready()) {
            start_permitted = false;
            start_revoked = true;
            start_ready = NULL;
            code = KM_COMMAND_UNAVAILABLE;
        }
    }
    km_admission_result_t admission = KM_REQ_UNAVAILABLE;
    km_fault_t fault = KM_FAULT_NONE;
    if (code == KM_COMMAND_OK) {
        /* Host requests cannot impersonate LOCAL_UI and bypass host-liveness checks. */
        if (id.client != KM_CLIENT_HOST) admission = KM_REQ_REJECTED_CONFLICT;
        else {
            km_core_timepoint_t now;
            controller_changed();
            if (km_clock_get_time(&now) != KM_OK) code = KM_COMMAND_UNAVAILABLE;
            else {
                admission = cancel ? km_core_request_cancel_target(controller, &id, now)
                    : km_core_request_start(controller, &id, (km_direction_t)args[7], now);
                controller_changed();
                fault = controller->system_fault;
            }
        }
    }
    if (code == KM_COMMAND_OK) (void)km_send_admission(instance, &id, admission, fault);
    else result(kind, args[4], code);
}

void command_km_start(uint32_t *args) { operation(args, false); }
DECL_COMMAND(command_km_start,
    "km_start instance_hi=%u instance_lo=%u client=%c session=%u request=%u operation=%c lane=%c direction=%c");
void command_km_cancel(uint32_t *args) { operation(args, true); }
DECL_COMMAND(command_km_cancel,
    "km_cancel instance_hi=%u instance_lo=%u client=%c session=%u request=%u operation=%c lane=%c");

void command_km_heartbeat(uint32_t *args)
{
    enum km_command_result code = guard(args, false);
    if (code == KM_COMMAND_OK) {
        uint32_t session = args[2];
        if (!session || session < controller->client_session[KM_CLIENT_HOST]
            || (controller->shared_path_owned
                && (controller->active_client != KM_CLIENT_HOST
                    || controller->active_session_id != session))) code = KM_COMMAND_STALE;
        else {
            km_core_timepoint_t now;
            if (km_clock_get_time(&now) != KM_OK) code = KM_COMMAND_UNAVAILABLE;
            else if (controller->shared_path_owned
                     && controller->op_state != KM_OP_STATE_FAULT
                     && km_time_elapsed(now.ms, controller->last_host_heartbeat_ms)
                        >= controller->profile.host_watchdog_ms) {
                /* A late heartbeat must not hide expiry between periodic checks. */
                km_core_host_comm_lost(controller, now.ms);
                controller_changed();
                code = KM_COMMAND_STALE;
            }
            else km_core_host_comm_ready(controller, now.ms);
        }
    }
    result(KM_COMMAND_HEARTBEAT, args[2], code);
}
DECL_COMMAND(command_km_heartbeat, "km_heartbeat instance_hi=%u instance_lo=%u session=%u");

void command_km_stop(uint32_t *args)
{
    enum km_command_result code = guard(args, false);
    if (code == KM_COMMAND_OK) {
        km_core_timepoint_t now;
        if (km_clock_get_time(&now) != KM_OK) code = KM_COMMAND_UNAVAILABLE;
        else {
            km_core_explicit_stop(controller, KM_FAULT_EMERGENCY_STOP, now.ms);
            controller_changed();
        }
    }
    result(KM_COMMAND_STOP, 0, code);
}
DECL_COMMAND(command_km_stop, "km_stop instance_hi=%u instance_lo=%u");

/* Explicit operator path reconciliation; never moves or enables outputs.
 * status is the negated core result and is meaningful only when result=0. */
void command_km_path_reconcile(uint32_t *args)
{
    uint32_t query = args[2];
    km_result_t status = KM_OK;
    enum km_command_result code = guard(args, false);
    if (code == KM_COMMAND_OK && (!query || args[3] > 1 || args[4] > UINT8_MAX))
        code = KM_COMMAND_INVALID;
    if (code == KM_COMMAND_OK && (query != last_query || query <= last_reconcile_query))
        code = KM_COMMAND_STALE;
    if (code == KM_COMMAND_OK) {
        /* One attempt per fresh retained capture, including a refused attempt.
         * A delayed duplicate cannot reconcile a later occupancy episode. */
        last_reconcile_query = query;
        km_core_timepoint_t now;
        if (km_clock_get_time(&now) != KM_OK) code = KM_COMMAND_UNAVAILABLE;
        else if ((uint32_t)(now.ms - acquired.ms) > controller->profile.max_sensor_age_ms)
            code = KM_COMMAND_STALE;
        else {
            status = km_core_reconcile_path(controller, args[3] != 0, (uint8_t)args[4], now);
            controller_changed();
        }
    }
    sendf("km_path_reconcile_result instance_hi=%u instance_lo=%u query=%u result=%c status=%c",
          (uint32_t)(instance >> 32), (uint32_t)instance, query, (uint32_t)code,
          (uint32_t)(-(int)status));
}
DECL_COMMAND(command_km_path_reconcile,
    "km_path_reconcile instance_hi=%u instance_lo=%u query=%u occupied=%c lane=%c");

void command_km_event_peek(uint32_t *args)
{
    km_event_t event;
    enum km_command_result code = guard(args, true);
    if (code == KM_COMMAND_OK
        && km_event_queue_peek(&controller->event_queue, &event) != KM_OK) code = KM_COMMAND_EMPTY;
    if (code == KM_COMMAND_OK) (void)km_send_event(instance, &event);
    else result(KM_COMMAND_EVENT_PEEK, 0, code);
}
DECL_COMMAND_FLAGS(command_km_event_peek, HF_IN_SHUTDOWN,
                   "km_event_peek instance_hi=%u instance_lo=%u");

void command_km_event_ack(uint32_t *args)
{
    km_request_identity_t id;
    km_event_t head;
    enum km_command_result code = guard(args, true);
    if (code == KM_COMMAND_OK && !identity(args, &id)) code = KM_COMMAND_INVALID;
    if (code == KM_COMMAND_OK) {
        if (km_event_queue_peek(&controller->event_queue, &head) != KM_OK
            || head.seq != args[7] || head.client_id != id.client
            || head.session_id != id.session_id || head.req_id != id.req_id
            || head.op_type != id.op_type || head.lane_idx != id.lane_idx) code = KM_COMMAND_STALE;
        else (void)km_event_queue_ack(&controller->event_queue, head.seq);
    }
    result(KM_COMMAND_EVENT_ACK, args[7], code);
}
DECL_COMMAND_FLAGS(command_km_event_ack, HF_IN_SHUTDOWN,
    "km_event_ack instance_hi=%u instance_lo=%u client=%c session=%u request=%u operation=%c lane=%c seq=%u");

void command_km_terminal_query(uint32_t *args)
{
    km_request_identity_t id;
    km_event_t event;
    enum km_command_result code = guard(args, true);
    if (code == KM_COMMAND_OK && !identity(args, &id)) code = KM_COMMAND_INVALID;
    if (code == KM_COMMAND_OK && km_core_find_terminal(controller, &id, &event) != KM_OK)
        code = KM_COMMAND_STALE;
    if (code == KM_COMMAND_OK) (void)km_send_event(instance, &event);
    else result(KM_COMMAND_TERMINAL, args[4], code);
}
DECL_COMMAND_FLAGS(command_km_terminal_query, HF_IN_SHUTDOWN,
    "km_terminal_query instance_hi=%u instance_lo=%u client=%c session=%u request=%u operation=%c lane=%c");

/* Where and why each attempt of the latest LOAD ended, for the host's retry
 * report. Another identity, or none yet, reports count=0. */
void command_km_load_attempts_query(uint32_t *args)
{
    km_request_identity_t id;
    _Static_assert(KM_LOAD_ATTEMPTS == 3, "Update the km_load_attempts reply");
    km_load_attempt_t a[KM_LOAD_ATTEMPTS] = {{0}};
    uint8_t count = 0;
    if (guard(args, true) == KM_COMMAND_OK && identity(args, &id))
        count = km_core_get_load_attempts(controller, &id, a);
    uint64_t current = instance;
    sendf("km_load_attempts instance_hi=%u instance_lo=%u session=%u request=%u count=%c stage0=%c fault0=%c travel0=%u stage1=%c fault1=%c travel1=%u stage2=%c fault2=%c travel2=%u",
          (uint32_t)(current >> 32), (uint32_t)current, args[3], args[4], (uint32_t)count,
          (uint32_t)a[0].stage, (uint32_t)a[0].fault, a[0].travel_steps,
          (uint32_t)a[1].stage, (uint32_t)a[1].fault, a[1].travel_steps,
          (uint32_t)a[2].stage, (uint32_t)a[2].fault, a[2].travel_steps);
}
DECL_COMMAND_FLAGS(command_km_load_attempts_query, HF_IN_SHUTDOWN,
    "km_load_attempts_query instance_hi=%u instance_lo=%u client=%c session=%u request=%u operation=%c lane=%c");

/* Both views share one acquisition instant, after collecting the edge output. */
static enum km_command_result capture_snapshot(uint32_t query)
{
    km_core_timepoint_t now;
    km_sensor_snapshot_t sensors;
    controller_changed();
    if (km_clock_get_time(&now) != KM_OK
        || km_core_get_sensor_snapshot(controller, now.ms, &sensors) != KM_OK)
        return KM_COMMAND_UNAVAILABLE;
    retained_snapshot = km_core_get_snapshot(controller);
    retained_sensors = sensors;
    diag_bounce(retained_diag.bay_bounce, retained_diag.load_bounce,
               &retained_diag.hub_bounce, &retained_diag.toolhead_bounce,
               &retained_diag.selector_bounce);
    for (unsigned i = 0; i < KM_MAX_LANES; ++i) {
        retained_diag.bay_idle_edges[i] = controller->bay_idle_edges[i];
        retained_diag.load_idle_edges[i] = controller->load_idle_edges[i];
        tmc_status((uint8_t)i, &retained_diag.gstat[i], &retained_diag.drv_status[i]);
    }
    retained_diag.hub_idle_edges = controller->hub_idle_edges;
    acquired = now;
    last_query = query;
    return KM_COMMAND_OK;
}

void command_km_snapshot_part(uint32_t *args)
{
    uint32_t query = args[2], part = args[3];
    enum km_command_result code = guard(args, true);
    if (code == KM_COMMAND_OK && (!query || part > KM_STATUS_SELECTOR)) code = KM_COMMAND_INVALID;
    if (code == KM_COMMAND_OK && query < last_query) code = KM_COMMAND_STALE;
    if (code == KM_COMMAND_OK && query > last_query) {
        code = guard(args, false);
        if (code == KM_COMMAND_OK) code = capture_snapshot(query);
    }
    /* Commands run serially in task context; the private capture is never
     * changed by timer ISRs. */
    if (code == KM_COMMAND_OK)
        (void)km_send_snapshot_part(instance, query, &retained_snapshot,
                                   acquired.ms, acquired.us, (enum km_status_part)part);
    else result(KM_COMMAND_SNAPSHOT, query, code);
}
DECL_COMMAND_FLAGS(command_km_snapshot_part, HF_IN_SHUTDOWN,
    "km_snapshot_part instance_hi=%u instance_lo=%u query=%u part=%c");

void command_km_sensor_part(uint32_t *args)
{
    uint32_t query = args[2], part = args[3];
    enum km_command_result code = guard(args, true);
    if (code == KM_COMMAND_OK && (!query || part > KM_SENSOR_LOADS2)) code = KM_COMMAND_INVALID;
    if (code == KM_COMMAND_OK && query < last_query) code = KM_COMMAND_STALE;
    if (code == KM_COMMAND_OK && query > last_query) {
        code = guard(args, false);
        if (code == KM_COMMAND_OK) code = capture_snapshot(query);
    }
    if (code == KM_COMMAND_OK)
        (void)km_send_sensor_part(instance, query, &retained_sensors, (enum km_sensor_part)part);
    else result(KM_COMMAND_SNAPSHOT, query, code);
}
DECL_COMMAND_FLAGS(command_km_sensor_part, HF_IN_SHUTDOWN,
    "km_sensor_part instance_hi=%u instance_lo=%u query=%u part=%c");

/* Switch bounce/idle-edge and TMC gstat/drv_status counters (docs/diagnostics.md),
 * captured with the same query high-water as the status/sensor snapshot above. */
void command_km_diag_part(uint32_t *args)
{
    uint32_t query = args[2], part = args[3];
    enum km_command_result code = guard(args, true);
    if (code == KM_COMMAND_OK && (!query || part > KM_DIAG_TMC1)) code = KM_COMMAND_INVALID;
    if (code == KM_COMMAND_OK && query < last_query) code = KM_COMMAND_STALE;
    if (code == KM_COMMAND_OK && query > last_query) {
        code = guard(args, false);
        if (code == KM_COMMAND_OK) code = capture_snapshot(query);
    }
    if (code == KM_COMMAND_OK)
        (void)km_send_diag_part(instance, query, &retained_diag, (enum km_diag_part)part);
    else result(KM_COMMAND_SNAPSHOT, query, code);
}
DECL_COMMAND_FLAGS(command_km_diag_part, HF_IN_SHUTDOWN,
    "km_diag_part instance_hi=%u instance_lo=%u query=%u part=%c");

#if defined(CONFIG_WANT_TMCUART_LOCAL) && CONFIG_WANT_TMCUART_LOCAL && \
    defined(CONFIG_KLIPPER_MMU_TMC_UART_SHARED) && CONFIG_KLIPPER_MMU_TMC_UART_SHARED
#include "tmc_feeder.h"

static uint32_t last_restart_query;
void command_km_driver_restart(uint32_t *args);

/* Reserved lane value naming the selector's own TMC2209 instead of a real
 * lane (docs/selectors.md; the selector always lives at physical slot 1,
 * mcu/board_startup.c setup_selector()). Distinct from every real lane
 * (KM_MAX_LANES <= 12) and from the unrelated "no lane" sentinel 255 used
 * elsewhere (km_path_reconcile, km_diagnostic_detail): this one names a real
 * target rather than the absence of one. */
#define KM_DRIVER_RESTART_SELECTOR 254u

/* One step of an explicit restart of a stopped lane's TMC feeder driver, or
 * of the selector's own TMC2209 (lane=KM_DRIVER_RESTART_SELECTOR), with the
 * path reconciliation guard: attached instance, latest fresh capture, one
 * attempt per capture. status is the negated km_result_t and is meaningful
 * only when result=0: 0 ready with the STEP/DIR latch cleared, UNAVAILABLE
 * restarting (ask again), BUSY not stopped, FAULTED failed, NOT_SUPPORTED
 * the selector has no TMC2209 on this image (no selector, or STEP/DIR only). */
void command_km_driver_restart(uint32_t *args)
{
    uint32_t query = args[2], lane = args[3];
    bool selector = lane == KM_DRIVER_RESTART_SELECTOR;
    uint32_t slot = selector ? 1u : lane;
    km_result_t status = KM_OK;
    enum km_command_result code = guard(args, false);
    if (code == KM_COMMAND_OK && (!query || lane > UINT8_MAX)) code = KM_COMMAND_INVALID;
    if (code == KM_COMMAND_OK && (query != last_query || query <= last_restart_query))
        code = KM_COMMAND_STALE;
    if (code == KM_COMMAND_OK) {
        last_restart_query = query;
        const km_driver_t *d = selector ? controller->selector_driver
            : lane < controller->profile.lane_count ? controller->feeder_drivers[lane] : NULL;
        km_core_timepoint_t now;
        if (km_clock_get_time(&now) != KM_OK) code = KM_COMMAND_UNAVAILABLE;
        else if ((uint32_t)(now.ms - acquired.ms) > controller->profile.max_sensor_age_ms)
            code = KM_COMMAND_STALE;
        else if (!selector && lane >= controller->profile.lane_count)
            status = KM_ERR_INVALID_PARAM;
        else if (!d || d != km_tmc_feeder_get_driver(slot)) status = KM_ERR_NOT_SUPPORTED;
        else if (controller->stage != KM_STAGE_STOPPED || controller->terminal_pending
                 || controller->rewind_pending || d->enabled || d->step_pin_high
                 || km_trajectory_is_moving(&controller->trajectory))
            status = KM_ERR_BUSY;
    }
    /* Commands run serially with the control task, and a stopped core starts
     * no motion; the restart's safe-off then latches the lane's (or the
     * selector's) fault, which refuses START (or a selection) until cleared. */
    if (code == KM_COMMAND_OK && status == KM_OK) status = km_tmc_feeder_restart(slot);
    sendf("km_driver_restart_result instance_hi=%u instance_lo=%u query=%u result=%c status=%c",
          (uint32_t)(instance >> 32), (uint32_t)instance, query, (uint32_t)code,
          (uint32_t)(-(int)status));
}
DECL_COMMAND(command_km_driver_restart,
    "km_driver_restart instance_hi=%u instance_lo=%u query=%u lane=%c");
#endif

#if CONFIG_KLIPPER_MMU_SELECTOR_STEPPER || CONFIG_KLIPPER_MMU_SERVO
#include "selector_mechanics.h"

/* Runtime, idle-only calibration write for one gate (operation_core.h
 * km_core_set_selector_gate; docs/g-code-commands.md KLIPPER_MMU_CALIBRATE_SELECTOR
 * / KLIPPER_MMU_SERVO). value is selector microsteps for LINEAR/ROTARY, or
 * degrees for SERVO, -1 clearing the gate for either kind -- converted here with KM_SELECTOR_SERVO_US and bounded
 * by this image's own servo angle range before reaching the core, which
 * bounds a stepper value by the homing max travel. status is the negated
 * km_result_t and is meaningful only when result=0. The existing config-time
 * config_km_selector_gate is unaffected; this is for after start. */
void command_km_selector_gate_set(uint32_t *args)
{
    unsigned gate = args[2];
    int32_t raw = (int32_t)args[3], value = raw;
    km_result_t status = KM_OK;
    enum km_command_result code = guard(args, false);
    if (code == KM_COMMAND_OK && gate > UINT8_MAX) code = KM_COMMAND_INVALID;
#if CONFIG_KLIPPER_MMU_SERVO
    if (code == KM_COMMAND_OK && controller->profile.selector.kind == KM_SELECTOR_SERVO) {
        /* -1 clears the gate (uncalibrated, like the stepper kinds); 0 degrees
         * is a real angle (MMX's own table has one). */
        if (raw != -1 && (raw < 0 || raw > CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MAX_ANGLE_DEG))
            status = KM_ERR_INVALID_PARAM;
        else
            value = raw == -1 ? 0 : (int32_t)KM_SELECTOR_SERVO_US(raw);
    }
#endif
    if (code == KM_COMMAND_OK && status == KM_OK) {
        status = km_core_set_selector_gate(controller, (uint8_t)gate, value);
        if (status == KM_OK) controller_changed();
    }
    /* No gate echo (the host already knows; this reply is synchronous, same
     * convention as km_driver_restart_result/km_path_reconcile_result not
     * echoing their own out-of-range-checked argument). */
    sendf("km_selector_gate_set_result instance_hi=%u instance_lo=%u result=%c status=%c",
          (uint32_t)(instance >> 32), (uint32_t)instance, (uint32_t)code,
          (uint32_t)(-(int)status));
}
DECL_COMMAND(command_km_selector_gate_set,
    "km_selector_gate_set instance_hi=%u instance_lo=%u gate=%c value=%i");

/* Happy Hare's MMU_MOTORS_OFF equivalent (operation_core.h
 * km_core_selector_motors_off): idle-only, releases the selector and its
 * shared gear so the operator can move the selector by hand before
 * KLIPPER_MMU_CALIBRATE_SELECTOR. status is the negated km_result_t and is
 * meaningful only when result=0. */
void command_km_selector_motors_off(uint32_t *args)
{
    km_result_t status = KM_OK;
    enum km_command_result code = guard(args, false);
    if (code == KM_COMMAND_OK) {
        status = km_core_selector_motors_off(controller);
        if (status == KM_OK) controller_changed();
    }
    sendf("km_selector_motors_off_result instance_hi=%u instance_lo=%u result=%c status=%c",
          (uint32_t)(instance >> 32), (uint32_t)instance, (uint32_t)code,
          (uint32_t)(-(int)status));
}
DECL_COMMAND(command_km_selector_motors_off,
    "km_selector_motors_off instance_hi=%u instance_lo=%u");
#endif

#if CONFIG_KLIPPER_MMU_SERVO
/* Runtime, idle-only SERVO test move (operation_core.h km_core_selector_servo_test):
 * degrees, bounded by this image's own servo angle range and converted with
 * KM_SELECTOR_SERVO_US, so the host can jog the servo to find an angle before
 * saving it with km_selector_gate_set. Never touches the gate table or
 * selector_engaged/selector_gate. */
void command_km_selector_servo_test(uint32_t *args)
{
    int32_t degrees = (int32_t)args[2];
    km_result_t status = KM_OK;
    enum km_command_result code = guard(args, false);
    if (code == KM_COMMAND_OK
        && (degrees < 0 || degrees > CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MAX_ANGLE_DEG))
        code = KM_COMMAND_INVALID;
    if (code == KM_COMMAND_OK) {
        status = km_core_selector_servo_test(controller, KM_SELECTOR_SERVO_US(degrees));
        if (status == KM_OK) controller_changed();
    }
    sendf("km_selector_servo_test_result instance_hi=%u instance_lo=%u result=%c status=%c",
          (uint32_t)(instance >> 32), (uint32_t)instance, (uint32_t)code,
          (uint32_t)(-(int)status));
}
DECL_COMMAND(command_km_selector_servo_test,
    "km_selector_servo_test instance_hi=%u instance_lo=%u degrees=%i");
#endif

#if CONFIG_KLIPPER_MMU_FPS_ARRIVAL
/* Runtime restore of the calibrated HUB-to-arrival distance (operation_core.h
 * km_core_set_calibrated_arrival_steps; docs/g-code-commands.md CALIBRATE
 * SAVE=0 puts back what was there before a measurement it chose not to
 * keep). status is the negated km_result_t and is meaningful only when
 * result=0. */
void command_km_calibrated_arrival_set(uint32_t *args)
{
    km_result_t status = KM_OK;
    enum km_command_result code = guard(args, false);
    if (code == KM_COMMAND_OK) {
        status = km_core_set_calibrated_arrival_steps(controller, args[2]);
        if (status == KM_OK) controller_changed();
    }
    sendf("km_calibrated_arrival_set_result instance_hi=%u instance_lo=%u result=%c status=%c",
          (uint32_t)(instance >> 32), (uint32_t)instance, (uint32_t)code,
          (uint32_t)(-(int)status));
}
DECL_COMMAND(command_km_calibrated_arrival_set,
    "km_calibrated_arrival_set instance_hi=%u instance_lo=%u steps=%u");
#endif
