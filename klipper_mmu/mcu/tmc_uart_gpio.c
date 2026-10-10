// SPDX-License-Identifier: GPL-3.0-only
// Local TMC register policy over Klipper's shared tmcuart.c bit engine.
#include "tmc_uart_gpio.h"
#include "clock.h"
#include "autoconf.h"
#include "board/irq.h"
#include "board/misc.h"
#include "sched.h"
#include "tmcuart.h"
#include "klipper_mmu/tmc_uart_codec.h"
#include "klipper_mmu/tmc_uart_config.h"
#include <limits.h>
#include <string.h>

typedef struct {
    bool claimed, active, timer_active, pending, quiet, cleanup_needed;
    bool timer_wake_phase, timer_has_safety;
    km_fault_t fault;                 // ISR may set this single-field latch.
    uint32_t pin, baud_ticks, quiet_until, pending_token, pending_start_tick;
    uint32_t safety_tick;
    km_tmc_uart_force_off_fn force_off;
    void *force_context;
    struct tmcuart_local *uart;
    struct timer timer;              // Transaction and diagnostic safety only.
    km_tmc2209_t controller;
} uart_slot_t;

static uart_slot_t slots[KM_TMC_UART_MAX_SLOTS];
static struct task_wake uart_wake;
static bool global_shutdown_latched;

static bool future(uint32_t target, uint32_t now)
{
    return (int32_t)(target - now) > 0;
}

static bool task_irqs_enabled(void)
{
    irqstatus_t state = irq_save();
    irq_restore(state);
    return state == 0ul;
}

static void raw_off(uart_slot_t *s)
{
    if (s->force_off) s->force_off(s->force_context);
}

static void latch_fault(uart_slot_t *s, km_fault_t fault)
{
    if (s->fault == KM_FAULT_NONE)
        s->fault = fault == KM_FAULT_NONE ? KM_FAULT_DRIVER_FAULT : fault;
    raw_off(s);
    s->cleanup_needed = true;
    sched_wake_task(&uart_wake);
}

static void cancel_timer(uart_slot_t *s)
{
    irqstatus_t state = irq_save();
    if (s->timer_active) sched_del_timer(&s->timer);
    s->timer_active = false;
    irq_restore(state);
}

/* Timer ISR never touches the controller or shared UART task API. A service
 * wake leaves a precomputed hard safety expiration armed during task starvation. */
static uint_fast8_t uart_timer_event(struct timer *timer)
{
    uart_slot_t *s = NULL;
    for (unsigned i = 0; i < KM_TMC_UART_MAX_SLOTS; ++i)
        if (&slots[i].timer == timer) { s = &slots[i]; break; }
    if (!s) return SF_DONE;
    if (global_shutdown_latched || sched_is_shutdown() || !s->active) {
        s->timer_active = false;
        return SF_DONE;
    }
    uint32_t now = timer_read_time();
    if (s->timer_wake_phase) {
        s->timer_wake_phase = false;
        sched_wake_task(&uart_wake);
        if (s->timer_has_safety && future(s->safety_tick, now)) {
            timer->waketime = s->safety_tick;
            return SF_RESCHEDULE;
        }
        if (!s->timer_has_safety) {
            s->timer_active = false;
            return SF_DONE;
        }
    }
    s->timer_active = false;
    latch_fault(s, KM_FAULT_DRIVER_FAULT);
    return SF_DONE;
}

static km_result_t deadline_tick(uint32_t deadline_us, uint32_t *tick)
{
    bool due = false;
    km_result_t r = km_clock_deadline_ticks(deadline_us, tick, &due);
    return r == KM_OK && !due ? KM_OK : KM_ERR_FAULTED;
}

/* Task-only. Recompute both a service wake and a hard actuator deadline. */
static km_result_t arm_slot(uart_slot_t *s)
{
    if (s->fault != KM_FAULT_NONE || s->controller.fault != KM_FAULT_NONE)
        return KM_ERR_FAULTED;
    uint32_t now = timer_read_time(), wake = 0, hard = 0;
    bool have_wake = false, have_hard = false;
    if (s->controller.ready) {
        /* A diagnostic consists of two independently timed register reads
         * (GSTAT then DRV_STATUS), each retried after a communication error.
         * Keep one fixed overall stale bound covering every permitted attempt
         * and its quiet guard; do not extend it when a later attempt begins. */
        uint32_t budget = (uint32_t)km_tmc_uart_diagnostic_budget_us(
            s->controller.config.transaction_timeout_us); /* Validated at setup. */
        if (deadline_tick(s->controller.next_diagnostic_us + budget, &hard) != KM_OK)
            return KM_ERR_FAULTED;
        have_hard = true;
    }
    if (s->controller.pending || s->controller.waiting || s->pending) {
        uint32_t transaction;
        if (deadline_tick(s->controller.started_us +
                          s->controller.config.transaction_timeout_us, &transaction) != KM_OK)
            return KM_ERR_FAULTED;
        if (!have_hard || future(hard, transaction)) hard = transaction;
        have_hard = true;
    } else if (s->controller.ready) {
        bool due = false;
        if (km_clock_deadline_ticks(s->controller.next_diagnostic_us, &wake, &due) != KM_OK)
            return KM_ERR_FAULTED;
        /* A due diagnostic needs a task wake, not a one-tick timer retry:
         * conversion, status and scheduler bookkeeping can exceed one tick.
         * Eight bit-times is 200 us at the validated exact 40000 baud and
         * remains bounded well before the independent hard stale deadline. */
        if (due) wake = now + 8u * s->baud_ticks;
        have_wake = true;
    }
    if (s->quiet) {
        if (future(s->quiet_until, now)) {
            /* A due diagnostic cannot wake again before the physical quiet
             * guard ends. Keep the independent hard deadline unchanged. */
            if (!have_wake || future(s->quiet_until, wake)) wake = s->quiet_until;
            have_wake = true;
        } else s->quiet = false;
    }
    if (s->controller.waiting && !s->pending && !s->quiet) {
        uint32_t retry = now + s->baud_ticks;
        if (!have_wake || future(wake, retry)) wake = retry;
        have_wake = true;
    }
    if (!have_wake && !have_hard) {
        irqstatus_t state = irq_save();
        if (global_shutdown_latched || sched_is_shutdown() ||
            s->fault != KM_FAULT_NONE || s->controller.fault != KM_FAULT_NONE) {
            irq_restore(state);
            return KM_ERR_FAULTED;
        }
        if (s->timer_active) sched_del_timer(&s->timer);
        s->timer_active = false;
        sched_wake_task(&uart_wake);
        irq_restore(state);
        return KM_OK;
    }
    if (have_hard && (!have_wake || !future(hard, wake))) {
        wake = hard;
        have_wake = false;
    }
    irqstatus_t state = irq_save();
    now = timer_read_time();
    if (global_shutdown_latched || sched_is_shutdown() ||
        s->fault != KM_FAULT_NONE || s->controller.fault != KM_FAULT_NONE ||
        !future(wake, now) || (have_hard && !future(hard, now))) {
        irq_restore(state);
        return KM_ERR_FAULTED;
    }
    if (s->timer_active) sched_del_timer(&s->timer);
    s->timer.func = uart_timer_event;
    s->timer.waketime = wake;
    s->timer_wake_phase = have_wake;
    s->timer_has_safety = have_hard;
    s->safety_tick = hard;
    sched_add_timer(&s->timer);
    s->timer_active = true;
    irq_restore(state);
    return KM_OK;
}

static void quiet_from_now(uart_slot_t *s, uint32_t bits)
{
    s->quiet_until = timer_read_time() + bits * s->baud_ticks;
    s->quiet = true;
}

static km_result_t transport_begin(void *context, uint32_t token,
                                   const uint8_t *raw, uint8_t tx_length,
                                   uint8_t rx_length)
{
    uart_slot_t *s = context;
    if (s->fault != KM_FAULT_NONE || global_shutdown_latched || sched_is_shutdown())
        return KM_ERR_FAULTED;
    if (s->pending) return KM_ERR_BUSY;
    uint32_t now = timer_read_time();
    if (s->quiet && future(s->quiet_until, now)) return KM_ERR_BUSY;
    s->quiet = false;
    if ((tx_length != 4u && tx_length != 8u) ||
        (rx_length != 0u && rx_length != 8u)) return KM_ERR_INVALID_PARAM;
    uint8_t packed[KM_TMC_UART_ENCODED_MAX];
    uint8_t count = km_tmc_uart_encode(raw, tx_length, packed);
    if (!count) return KM_ERR_INVALID_PARAM;
    int r = tmcuart_local_submit(s->uart, token, packed, count,
                                 rx_length ? KM_TMC_UART_ENCODED_MAX : 0u);
    if (r == TMCUART_ERR_BUSY) return KM_ERR_BUSY;
    if (r != TMCUART_OK) return KM_ERR_FAULTED;
    s->pending = true;
    s->pending_token = token;
    s->pending_start_tick = now;
    return KM_OK;
}

static void transport_abort(void *context)
{
    uart_slot_t *s = context;
    /* Only cancel the physical transfer when this slot is the one that
     * actually has it in flight. On a shared single-wire UART pin (Happy
     * Hare's EASY-BRD wiring, km_tmc_uart_gpio_setup below) another sharer
     * may legitimately have its own transaction -- or an already-completed
     * one still waiting for delivery -- outstanding on the same wire right
     * now; cancelling it would silently drop that sharer's reply, and a
     * fault on one slot must never fault the other. */
    if (s->pending && s->uart && !global_shutdown_latched && !sched_is_shutdown()) {
        if (tmcuart_local_cancel(s->uart) != TMCUART_OK)
            latch_fault(s, KM_FAULT_DRIVER_FAULT);
    }
    s->pending = false;
    quiet_from_now(s, KM_TMC_UART_ERROR_QUIET_BITS);
}

static void transport_force_off(void *context)
{
    raw_off((uart_slot_t *)context);
}

static const km_tmc2209_transport_t transport = {
    .begin = transport_begin, .abort = transport_abort,
    .force_off = transport_force_off,
};

/* A slot sharing another's UART pin (km_tmc_uart_gpio_setup below) submits
 * through whichever slot's tmcuart_local_setup call actually owns the
 * physical channel, so its completion always arrives with that owner as
 * context, even for a sharer's own transaction. At most one sharer of a
 * given physical channel is ever pending at a time (tmcuart_local_submit's
 * own busy check serializes them), so find that one instead of assuming
 * context is it; on an unshared pin this is simply the owner itself. */
static uart_slot_t *find_pending_slot(struct tmcuart_local *uart, uint32_t token)
{
    for (unsigned i = 0; i < KM_TMC_UART_MAX_SLOTS; ++i) {
        uart_slot_t *s = &slots[i];
        if (s->claimed && s->uart == uart && s->pending && s->pending_token == token)
            return s;
    }
    return NULL;
}

static void uart_completed(void *context, uint32_t token, int status,
                           const uint8_t *packed, uint8_t length,
                           uint32_t completion_tick)
{
    uart_slot_t *owner = context;
    uart_slot_t *s = find_pending_slot(owner->uart, token);
    if (!s || !s->active || s->fault != KM_FAULT_NONE || global_shutdown_latched ||
        sched_is_shutdown())
        return;
    uint32_t now_tick = timer_read_time();
    uint32_t age = now_tick - completion_tick;
    if (age >= UINT32_C(0x80000000) ||
        (completion_tick - s->pending_start_tick) >= UINT32_C(0x80000000)) {
        latch_fault(s, KM_FAULT_DRIVER_FAULT);
        return;
    }
    uint8_t raw[8];
    km_result_t result = KM_OK;
    uint8_t raw_length = s->controller.expected_rx_length;
    if (status != TMCUART_STATUS_OK ||
        (raw_length && (!packed || !km_tmc_uart_decode(packed, length, raw))) ||
        (!raw_length && length != 0u)) result = KM_ERR_FAULTED;
    uint32_t now_us;
    if (km_clock_get_completion_us(&now_us) != KM_OK) {
        latch_fault(s, KM_FAULT_DRIVER_FAULT);
        return;
    }
    s->pending = false;
    s->quiet_until = completion_tick
        + (result == KM_OK ? 6u : KM_TMC_UART_ERROR_QUIET_BITS) * s->baud_ticks;
    s->quiet = true;
    km_result_t done = km_tmc2209_complete(&s->controller, token, result,
                               result == KM_OK && raw_length ? raw : NULL,
                               result == KM_OK ? raw_length : 0u, now_us);
    if (done != KM_OK || s->controller.fault != KM_FAULT_NONE ||
        arm_slot(s) != KM_OK) latch_fault(s, s->controller.fault);
}

static km_result_t validate_setup(const km_tmc2209_config_t *config)
{
    if (!config) return KM_ERR_INVALID_PARAM;
    const km_tmc_uart_config_t uart_cfg = {
        .tmc = *config,
        .clock_hz = CONFIG_CLOCK_FREQ,
    };
    return km_tmc_uart_config_validate(&uart_cfg);
}

KM_API km_result_t km_tmc_uart_gpio_setup(uint32_t slot, uint32_t pin,
    bool pull_up, const km_tmc2209_config_t *config,
    km_tmc_uart_force_off_fn force_off, void *context)
{
    if (force_off) force_off(context);
    if (!task_irqs_enabled() || slot >= KM_TMC_UART_MAX_SLOTS || pin >= 128u ||
        !force_off || !config || validate_setup(config) != KM_OK)
        return KM_ERR_INVALID_PARAM;
    if (global_shutdown_latched || sched_is_shutdown()) return KM_ERR_FAULTED;
    enum km_clock_state state = km_clock_get_state();
    if (state == KM_CLOCK_STATE_UNAVAILABLE) return KM_ERR_UNAVAILABLE;
    if (state != KM_CLOCK_STATE_READY) return KM_ERR_FAULTED;
    if (slots[slot].claimed) return KM_ERR_BUSY;
    /* A pin already claimed by another slot is a conflict, except exactly
     * Happy Hare's classic ERCF 1.1 EASY-BRD wiring: the gear's and the
     * selector's TMC2209 sharing one single-wire UART pin at distinct
     * addresses (installer/boards/Kconfig.easy_brd_rp2040:24-25;
     * docs/selectors.md), same as Klipper's own tmc_uart.py serializing
     * several TMC2209 on one pin by address. Equal addresses on one wire
     * cannot be told apart and are refused just as clearly. A claimed slot
     * that never reached a working physical channel (uart == NULL) keeps
     * poisoning this pin for everyone, sharing included -- physical setup
     * failure is permanent until MCU reset (see this function's own doc). */
    struct tmcuart_local *shared_uart = NULL;
    for (unsigned i = 0; i < KM_TMC_UART_MAX_SLOTS; ++i) {
        if (!slots[i].claimed || slots[i].pin != pin) continue;
        if (!slots[i].uart || slots[i].controller.config.address == config->address)
            return KM_ERR_CONFLICT;
        shared_uart = slots[i].uart;
    }

    uart_slot_t *s = &slots[slot];
    s->claimed = true;              // Permanent, including partial setup.
    s->pin = pin;
    s->force_off = force_off;       // Fixed before shared setup/callback ownership.
    s->force_context = context;
    s->baud_ticks = CONFIG_CLOCK_FREQ / 40000u;
    s->fault = KM_FAULT_NONE;
    if (shared_uart) {
        /* Reuse the already-set-up physical channel; do not touch its GPIO
         * or pull-up again. tmcuart_local_submit's own busy check serializes
         * every sharer's transactions onto this one wire. */
        s->uart = shared_uart;
    } else {
        s->uart = tmcuart_local_setup(pin, pin, pull_up ? 1u : 0u,
                                     s->baud_ticks, uart_completed, s);
        if (!s->uart) { latch_fault(s, KM_FAULT_DRIVER_FAULT); return KM_ERR_FAULTED; }
    }
    if (global_shutdown_latched || sched_is_shutdown()) {
        latch_fault(s, KM_FAULT_DRIVER_FAULT); return KM_ERR_FAULTED;
    }
    if (km_tmc2209_init(&s->controller, config, &transport, s) != KM_OK) {
        latch_fault(s, KM_FAULT_DRIVER_FAULT); return KM_ERR_FAULTED;
    }
    s->active = true;
    if (arm_slot(s) != KM_OK) { latch_fault(s, KM_FAULT_DRIVER_FAULT); return KM_ERR_FAULTED; }
    return KM_OK;
}

void km_tmc_uart_gpio_task(void)
{
    if (!sched_check_wake(&uart_wake) || global_shutdown_latched || sched_is_shutdown()) return;
    for (unsigned i = 0; i < KM_TMC_UART_MAX_SLOTS; ++i) {
        uart_slot_t *s = &slots[i];
        if (!s->active) continue;
        if (s->cleanup_needed) {
            s->cleanup_needed = false;
            if (s->uart) (void)tmcuart_local_cancel(s->uart);
            s->pending = false;
            quiet_from_now(s, KM_TMC_UART_ERROR_QUIET_BITS);
            continue;
        }
        if (s->fault != KM_FAULT_NONE) continue;
        uint32_t now_tick = timer_read_time();
        if (s->quiet && future(s->quiet_until, now_tick)) {
            if (arm_slot(s) != KM_OK) latch_fault(s, KM_FAULT_DRIVER_FAULT);
            continue;
        }
        s->quiet = false;
        uint32_t now_us;
        if (km_clock_get_completion_us(&now_us) != KM_OK) {
            latch_fault(s, KM_FAULT_DRIVER_FAULT); continue;
        }
        km_result_t r = km_tmc2209_service(&s->controller, now_us);
        if (r == KM_ERR_FAULTED || s->controller.fault != KM_FAULT_NONE ||
            arm_slot(s) != KM_OK) latch_fault(s, s->controller.fault);
    }
}
DECL_TASK(km_tmc_uart_gpio_task);

static bool ready_snapshot(const uart_slot_t *s)
{
    return !global_shutdown_latched && !sched_is_shutdown() &&
           s->active && s->timer_active && s->fault == KM_FAULT_NONE &&
           s->controller.fault == KM_FAULT_NONE && s->controller.ready;
}

KM_API bool km_tmc_uart_gpio_is_ready(uint32_t slot)
{
    if (slot >= KM_TMC_UART_MAX_SLOTS) return false;
    irqstatus_t state = irq_save();
    bool ready = ready_snapshot(&slots[slot]);
    irq_restore(state);
    return ready;
}

KM_API km_fault_t km_tmc_uart_gpio_get_fault(uint32_t slot)
{
    if (slot >= KM_TMC_UART_MAX_SLOTS) return KM_FAULT_DRIVER_FAULT;
    irqstatus_t state = irq_save();
    const uart_slot_t *s = &slots[slot];
    km_fault_t fault = global_shutdown_latched || sched_is_shutdown() || !s->claimed
        ? KM_FAULT_DRIVER_FAULT
        : (s->fault != KM_FAULT_NONE ? s->fault : s->controller.fault);
    irq_restore(state);
    return fault;
}

KM_API km_result_t km_tmc_uart_gpio_get_status(uint32_t slot, km_tmc_uart_status_t *out)
{
    if (slot >= KM_TMC_UART_MAX_SLOTS || !out) return KM_ERR_INVALID_PARAM;
    irqstatus_t state = irq_save();
    if (global_shutdown_latched || sched_is_shutdown()) {
        irq_restore(state);
        return KM_ERR_FAULTED;
    }
    const uart_slot_t *s = &slots[slot];
    if (!s->active) {
        irq_restore(state);
        return KM_ERR_UNAVAILABLE;
    }
    const km_tmc2209_t *t = &s->controller;
    const km_tmc_uart_status_t value = {
        .configured = t->configured,
        .ready = ready_snapshot(s),
        .fault = s->fault != KM_FAULT_NONE ? s->fault : t->fault,
        .requested_run_ma = t->config.run_current_ma,
        .requested_hold_ma = t->config.hold_current_ma,
        .confirmed_run_ma = t->confirmed_run_ma,
        .confirmed_hold_ma = t->confirmed_hold_ma,
        .drv_status = t->drv_status, .gstat = t->gstat, .ifcnt = t->ifcnt,
    };
    *out = value;
    irq_restore(state);
    return KM_OK;
}

KM_API km_result_t km_tmc_uart_gpio_restart(uint32_t slot)
{
    if (slot >= KM_TMC_UART_MAX_SLOTS || !task_irqs_enabled()) return KM_ERR_INVALID_PARAM;
    if (global_shutdown_latched || sched_is_shutdown()) return KM_ERR_FAULTED;
    uart_slot_t *s = &slots[slot];
    if (!s->active) return KM_ERR_UNAVAILABLE;
    if (km_clock_get_state() != KM_CLOCK_STATE_READY) return KM_ERR_FAULTED;
    raw_off(s);
    cancel_timer(s);
    s->cleanup_needed = false;
    s->fault = KM_FAULT_NONE; // Explicit recovery; cancellation may relatch it.
    km_result_t r = km_tmc2209_restart(&s->controller); // Cancels shared transfer.
    if (r != KM_OK || s->fault != KM_FAULT_NONE) {
        latch_fault(s, KM_FAULT_DRIVER_FAULT);
        return r != KM_OK ? r : KM_ERR_FAULTED;
    }
    if (arm_slot(s) != KM_OK) { latch_fault(s, KM_FAULT_DRIVER_FAULT); return KM_ERR_FAULTED; }
    return KM_OK;
}

KM_API km_result_t km_tmc_uart_gpio_set_current(uint32_t slot,
                                                uint16_t run_ma, uint16_t hold_ma)
{
    if (slot >= KM_TMC_UART_MAX_SLOTS || !task_irqs_enabled()) return KM_ERR_INVALID_PARAM;
    if (global_shutdown_latched || sched_is_shutdown()) return KM_ERR_FAULTED;
    uart_slot_t *s = &slots[slot];
    if (!s->active) return KM_ERR_UNAVAILABLE;
    if (s->fault != KM_FAULT_NONE) return KM_ERR_FAULTED;
    km_result_t r = km_tmc2209_set_current(&s->controller, run_ma, hold_ma);
    if (r != KM_OK) return r; // Early rejection keeps existing monitoring armed.
    if (arm_slot(s) != KM_OK) { latch_fault(s, KM_FAULT_DRIVER_FAULT); return KM_ERR_FAULTED; }
    return KM_OK;
}

KM_API bool km_tmc_uart_gpio_is_slot_active(uint32_t slot)
{
    return slot < KM_TMC_UART_MAX_SLOTS && !global_shutdown_latched &&
           !sched_is_shutdown() && slots[slot].active;
}

KM_API km_result_t km_tmc_uart_gpio_get_pin(uint32_t slot, uint32_t *out_pin)
{
    if (slot >= KM_TMC_UART_MAX_SLOTS || !out_pin) return KM_ERR_INVALID_PARAM;
    if (!slots[slot].claimed) return KM_ERR_UNAVAILABLE;
    *out_pin = slots[slot].pin;
    return KM_OK;
}

KM_API bool km_tmc_uart_gpio_is_shutdown(void)
{
    return global_shutdown_latched || sched_is_shutdown();
}

void km_tmc_uart_gpio_shutdown(void)
{
    global_shutdown_latched = true;
    for (unsigned i = 0; i < KM_TMC_UART_MAX_SLOTS; ++i)
        if (slots[i].claimed) raw_off(&slots[i]);
}
DECL_SHUTDOWN(km_tmc_uart_gpio_shutdown);
