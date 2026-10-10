// SPDX-License-Identifier: GPL-3.0-or-later
#include <assert.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "diagnostic_startup.h"
#include "board_startup.h"
#include "boot_identity.h"
#include "clock.h"
#include "commands.h"
#include "controller.h"
#include "board/irq.h"
#include "board/misc.h"
#include "command.h"
#include "sched.h"
#include "autoconf.h"
#ifdef CONFIG_KLIPPER_MMU_DIFF_BUFFER
#include "calibration.h"
#endif

#define ID_OK 0x51A7C0DEull
/* pattern: 0 idle 1 ready 2 pending 3 prereq 4 timeout 5 pre 6 owned
 * 7 foreign wait 8 foreign begin 9 owned end 10 shutdown before trigger */
struct opt {
    const char *name;
    uint8_t pattern, ident, preset, begin_ready, svc, null_core, burn, shut, wrap, attaches;
    enum km_clock_state clock0;
    km_result_t begin_err, svc_err, attach_err, expect_res;
};

static uint32_t irq_disabled, ticks, time_reads, clock_reads, id_reads, board_obs;
static uint32_t begin_calls, service_calls, attach_calls, board_shutdowns;
static uint32_t enable_calls, revoke_calls;
static int start_permitted;
static int in_isr, saw_const, saw_start, saw_query, allow_false;
static uint8_t is_shutdown;
static struct timer *armed;
static struct task_wake *model_wake;
static void (*reg_task)(void);
static void (*reg_shut)(void);
static const char *start_fmt, *query_fmt;
static uint32_t start_flags, query_flags, sent_ch, sent_state, origin;
static int sent_result;
static uint32_t sent_step, sent_lane, detail_flags;
static int sent_cause;
static const char *detail_fmt;
static enum km_clock_state clock_state;
static enum km_board_startup_state board_state;
static km_result_t board_result, begin_err, svc_err, attach_err;
static int begin_ready, svc_plan, null_core, burn, shut, ident_mode;
static km_operation_core_t core_obj;
#ifdef CONFIG_KLIPPER_MMU_DIFF_BUFFER
static km_diff_adc_config_t calibration_obj;
static int calibration_present = 1;
static uint32_t calibration_reads;
const km_diff_adc_config_t *km_diff_calibration_get(void) {
    assert(!irq_disabled && !in_isr);
    calibration_reads++;
    return calibration_present ? &calibration_obj : NULL;
}
#endif

void fixture_register_task(void (*fn)(void)) { assert(!reg_task && fn); reg_task = fn; }
void fixture_register_shutdown(void (*fn)(void)) { assert(!reg_shut && fn); reg_shut = fn; }
#ifdef CONFIG_KLIPPER_MMU_BOOT_IDENTITY_HOST
#define START_FN "command_km_diagnostic_start_host"
#define START_FMT "km_diagnostic_start_host epoch_hi=%u epoch_lo=%u"
static int saw_host_const;
#else
#define START_FN "command_km_diagnostic_start"
#define START_FMT "km_diagnostic_start"
#endif
void fixture_register_constant(const char *name, int value) {
    assert(name && value == 1);
#ifdef CONFIG_KLIPPER_MMU_BOOT_IDENTITY_HOST
    if (strcmp(name, "KLIPPER_MMU_BOOT_IDENTITY_HOST") == 0) { saw_host_const = 1; return; }
#endif
    assert(strcmp(name, "KLIPPER_MMU_DIAGNOSTIC_STARTUP") == 0);
    saw_const = 1;
}
void fixture_register_command(const char *fn, uint32_t flags, const char *fmt) {
    assert(fn && fmt);
    /* Only one start command may register; host builds lack the no-epoch one. */
    if (strcmp(fn, START_FN) == 0) {
        assert(!saw_start);
        start_fmt = fmt; start_flags = flags; saw_start = 1;
    } else if (strcmp(fn, "command_km_diagnostic_query") == 0) {
        query_fmt = fmt; query_flags = flags; saw_query = 1;
    } else if (strcmp(fn, "command_km_diagnostic_detail_query") == 0) {
        detail_fmt = fmt; detail_flags = flags;
    } else assert(0);
}
static void global_shutdown(void) {
    armed = NULL; /* Upstream clears user timers, then the hook runs. */
    is_shutdown = 1;
    assert(reg_shut);
    reg_shut();
}
irqstatus_t irq_save(void) { uint32_t old = irq_disabled; irq_disabled = 1u; return old; }
void irq_restore(irqstatus_t old) { irq_disabled = old; }
uint32_t timer_read_time(void) { time_reads++; return ticks; }
uint32_t timer_from_us(uint32_t us) { return us * (uint32_t)(CONFIG_CLOCK_FREQ / 1000000u); }
uint8_t sched_is_shutdown(void) { return is_shutdown; }
void sched_add_timer(struct timer *t) { assert(irq_disabled && !in_isr && t && !armed); armed = t; }
void sched_del_timer(struct timer *t) {
    assert(irq_disabled);
    if (!armed) return;
    assert(t == armed);
    armed = NULL;
}
void sched_wake_task(struct task_wake *w) { assert(w); model_wake = w; w->wake = 1u; }
uint8_t sched_check_wake(struct task_wake *w) {
    assert(w); uint8_t ready = w->wake; w->wake = 0u; return ready;
}
void sendf(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    if (fmt && strncmp(fmt, "km_diagnostic_detail ", 21) == 0) {
        sent_ch = va_arg(ap, uint32_t);
        sent_step = va_arg(ap, uint32_t);
        sent_lane = va_arg(ap, uint32_t);
        sent_cause = va_arg(ap, int);
        va_end(ap);
        return;
    }
    assert(fmt && strcmp(fmt, "km_diagnostic_state challenge=%u state=%c result=%i") == 0);
    sent_ch = va_arg(ap, uint32_t);
    sent_state = va_arg(ap, uint32_t);
    sent_result = va_arg(ap, int);
    va_end(ap);
}
enum km_clock_state km_clock_get_state(void) {
    assert(!irq_disabled && !in_isr); clock_reads++; return clock_state;
}
#ifndef CONFIG_KLIPPER_MMU_BOOT_IDENTITY_HOST
/* Host-epoch builds define no provider: linking proves no fallback call. */
km_result_t km_boot_identity_get(uint64_t *id) {
    assert(!irq_disabled && !in_isr && id); id_reads++;
    if (ident_mode == 1) return KM_ERR_UNAVAILABLE;
    if (ident_mode == 2) return KM_ERR_BUSY;
    *id = ident_mode == 3 ? 0u : (uint64_t)ID_OK;
    return KM_OK;
}
#endif
static uint32_t limit_ticks(void) {
    return (uint32_t)CONFIG_KLIPPER_MMU_DIAGNOSTIC_TIMEOUT_US * (uint32_t)(CONFIG_CLOCK_FREQ / 1000000u);
}
static void side(int phase) {
    if (shut == phase) global_shutdown();
    if (burn == phase) ticks += limit_ticks();
}
km_result_t km_board_startup_begin(const km_resource_claim_t *claims, size_t n,
                                  uint32_t timeout, const km_diff_adc_config_t *cal) {
    assert(!irq_disabled && !in_isr && !claims && n == 0u);
#ifdef CONFIG_KLIPPER_MMU_DIFF_BUFFER
    assert(cal == &calibration_obj);
#else
    assert(!cal);
#endif
    assert(timeout > 0u && timeout < (1u << 31));
    assert(timeout <= (uint32_t)CONFIG_KLIPPER_MMU_DIAGNOSTIC_TIMEOUT_US);
    assert(timeout == (limit_ticks() - (ticks - origin)) / (uint32_t)(CONFIG_CLOCK_FREQ / 1000000u));
    begin_calls++; board_obs++;
    if (begin_err != KM_OK) return begin_err;
    side(1);
    if (begin_ready) { board_state = KM_BOARD_STARTUP_READY; board_result = KM_OK; }
    else { board_state = KM_BOARD_STARTUP_WAITING; board_result = KM_ERR_UNAVAILABLE; }
    return KM_OK;
}
km_result_t km_board_startup_service(void) {
    assert(!irq_disabled && !in_isr); service_calls++; board_obs++; side(2);
    if (svc_plan == 0 || (svc_plan == 1 && service_calls == 1u)) return KM_ERR_UNAVAILABLE;
    if (svc_plan == 2) return svc_err;
    board_state = KM_BOARD_STARTUP_READY; board_result = KM_OK; return KM_OK;
}
enum km_board_startup_state km_board_startup_get_state(void) { board_obs++; return board_state; }
km_result_t km_board_startup_get_result(void) { board_obs++; return board_result; }
void km_board_startup_get_detail(km_board_startup_detail_t *out) {
    *out = (km_board_startup_detail_t){.lane = KM_STARTUP_NO_LANE,
                                       .pin = {KM_STARTUP_NO_PIN, KM_STARTUP_NO_PIN}};
}
km_operation_core_t *km_board_startup_get_core(void) {
    assert(!in_isr); board_obs++;
    if (null_core || board_state != KM_BOARD_STARTUP_READY) return NULL;
    return &core_obj;
}
void km_board_startup_shutdown(void) {
    assert(!in_isr); board_shutdowns++; board_obs++;
    board_state = KM_BOARD_STARTUP_FAULTED; board_result = KM_ERR_FAULTED;
}
void km_controller_changed(void) { assert(0); }
km_result_t km_commands_attach(km_operation_core_t *core, uint64_t id, void (*changed)(void), bool allow) {
    assert(!irq_disabled && !in_isr && core == &core_obj);
    assert(id == (uint64_t)ID_OK && changed == km_controller_changed && allow == false);
    allow_false = 1; attach_calls++; side(3); return attach_err;
}
km_result_t km_commands_enable_start(km_operation_core_t *core, uint64_t id,
                                    bool (*ready)(void)) {
    assert(!irq_disabled && !in_isr && core == &core_obj && id == (uint64_t)ID_OK);
    assert(km_diagnostic_get_state() == KM_DIAGNOSTIC_READY && !is_shutdown);
    assert(board_state == KM_BOARD_STARTUP_READY && board_result == KM_OK);
    assert(attach_calls == 1u && attach_err == KM_OK && !start_permitted);
    assert(ready && ready());
    enable_calls++; start_permitted = 1; return KM_OK;
}
void km_commands_revoke_start(km_operation_core_t *core, uint64_t id) {
    assert(core == &core_obj && id == (uint64_t)ID_OK);
    revoke_calls++; start_permitted = 0;
}
static void expect(enum km_diagnostic_state st, km_result_t result) {
    assert(km_diagnostic_get_state() == st && km_diagnostic_get_result() == result);
}
static void query_check(uint32_t ch, enum km_diagnostic_state st, km_result_t result) {
    uint32_t t = time_reads, c = clock_reads, i = id_reads, b = board_obs;
    uint32_t g = begin_calls, s = service_calls, a = attach_calls, args[1] = {ch};
    command_km_diagnostic_query(args);
    assert(time_reads == t && clock_reads == c && id_reads == i && board_obs == b);
    assert(begin_calls == g && service_calls == s && attach_calls == a);
    assert(sent_ch == ch && sent_state == (uint32_t)st && sent_result == (int32_t)result);
}
#ifdef CONFIG_KLIPPER_MMU_BOOT_IDENTITY_HOST
static void send_epoch(uint64_t epoch) {
    uint32_t args[2] = {(uint32_t)(epoch >> 32), (uint32_t)epoch};
    command_km_diagnostic_start_host(args);
}
static void start(void) { send_epoch((uint64_t)ID_OK); }
#else
static void start(void) { command_km_diagnostic_start(NULL); }
#endif
static void task(void) { km_diagnostic_task(); }
static void pump(void) {
    assert(armed && armed->func);
    /* Due only. Early time advances to the armed wake; a late observation stays. */
    if ((int32_t)(ticks - armed->waketime) < 0)
        ticks = armed->waketime;
    uint32_t observed = ticks;
    in_isr = 1; irqstatus_t old = irq_save(); uint_fast8_t rc = armed->func(armed); irq_restore(old); in_isr = 0;
    if (rc == SF_DONE) armed = NULL;
    else {
        assert(rc == SF_RESCHEDULE && armed);
        assert(armed->waketime == observed + timer_from_us(1000));
    }
    task();
}
static void setup(const struct opt *o) {
    clock_state = o->clock0; ident_mode = (int)o->ident;
    board_state = o->preset ? KM_BOARD_STARTUP_WAITING : KM_BOARD_STARTUP_UNINITIALIZED;
    board_result = KM_ERR_UNAVAILABLE; begin_err = o->begin_err; begin_ready = (int)o->begin_ready;
    svc_plan = (int)o->svc; svc_err = o->svc_err; null_core = (int)o->null_core;
    attach_err = o->attach_err; burn = (int)o->burn; shut = (int)o->shut;
    if (o->wrap) ticks = 0xFFFFFFF0u;
    origin = ticks;
}
static void run_timeout(void) {
    start(); assert(armed && armed->waketime == origin + timer_from_us(1000)); task();
    expect(KM_DIAGNOSTIC_WAITING, KM_ERR_UNAVAILABLE);
    ticks = origin + limit_ticks() - 1u; pump();
    expect(KM_DIAGNOSTIC_WAITING, KM_ERR_UNAVAILABLE);
    assert(begin_calls == 0u && board_shutdowns == 0u && armed);
    uint32_t w = armed->waketime, tr = time_reads; start();
    assert(time_reads == tr && armed && armed->waketime == w);
    ticks = origin + limit_ticks();
    /* Extra model wake at this raw tick. Not a hardware ISR and not an early timer. */
    assert(model_wake);
    model_wake->wake = 1u;
    {
        uint32_t held = ticks;
        task();
        assert(ticks == held);
    }
    expect(KM_DIAGNOSTIC_FAULTED, KM_ERR_TIMEOUT);
    assert(begin_calls == 0u && attach_calls == 0u && board_shutdowns == 0u && !armed);
    global_shutdown(); expect(KM_DIAGNOSTIC_FAULTED, KM_ERR_TIMEOUT);
}
static void run_end(const struct opt *o) {
    start(); task(); expect(KM_DIAGNOSTIC_FAULTED, o->expect_res);
    assert(begin_calls == 1u && attach_calls == o->attaches && board_shutdowns == 1u && !armed);
    assert(km_diagnostic_get_state() != KM_DIAGNOSTIC_READY);
    if (o->attaches) assert(allow_false == 1);
    global_shutdown(); expect(KM_DIAGNOSTIC_FAULTED, o->expect_res);
    is_shutdown = 0u; start(); task();
    assert(begin_calls == 1u); expect(KM_DIAGNOSTIC_FAULTED, o->expect_res);
}
static void dispatch(const struct opt *o) {
#ifdef CONFIG_KLIPPER_MMU_DIFF_BUFFER
    if (strcmp(o->name, "missing_calibration") == 0) {
        calibration_present = 0;
        start(); task();
        expect(KM_DIAGNOSTIC_FAULTED, KM_ERR_FAULTED);
        assert(calibration_reads == 1u && begin_calls == 0u);
        assert(board_state == KM_BOARD_STARTUP_UNINITIALIZED);
        assert(board_shutdowns == 0u && attach_calls == 0u && !armed);
        is_shutdown = 0u; start(); task();
        assert(calibration_reads == 1u && begin_calls == 0u);
        return;
    }
#endif
#ifdef CONFIG_KLIPPER_MMU_BOOT_IDENTITY_HOST
    if (o->pattern == 11) {
        /* Zero never binds or starts; the latch stays closed for this lifetime. */
        send_epoch(0u); expect(KM_DIAGNOSTIC_FAULTED, KM_ERR_INVALID_PARAM);
        assert(!armed && !time_reads && !begin_calls);
        start(); task(); expect(KM_DIAGNOSTIC_FAULTED, KM_ERR_INVALID_PARAM);
        assert(!armed && !time_reads && !begin_calls && !attach_calls);
        query_check(3u, KM_DIAGNOSTIC_FAULTED, KM_ERR_INVALID_PARAM);
        assert(!start_permitted && enable_calls == 0u);
        return;
    }
    if (o->pattern == 12) {
        /* Later epochs in WAITING, READY, and after shutdown never rebind.
         * Attach and revoke assert the first epoch (ID_OK). */
        start(); assert(armed); uint32_t w = armed->waketime, tr = time_reads;
        send_epoch(UINT64_C(0x0123456789abcdef)); send_epoch(0u);
        assert(time_reads == tr && armed && armed->waketime == w);
        expect(KM_DIAGNOSTIC_WAITING, KM_ERR_UNAVAILABLE);
        task(); expect(KM_DIAGNOSTIC_WAITING, KM_ERR_UNAVAILABLE);
        pump(); expect(KM_DIAGNOSTIC_READY, KM_OK);
        assert(begin_calls == 1u && attach_calls == 1u && !armed);
        tr = time_reads;
        send_epoch(UINT64_C(0x0123456789abcdef)); send_epoch(0u);
        expect(KM_DIAGNOSTIC_READY, KM_OK); assert(time_reads == tr && !armed);
        query_check(4u, KM_DIAGNOSTIC_READY, KM_OK);
        global_shutdown(); expect(KM_DIAGNOSTIC_FAULTED, KM_ERR_FAULTED);
        assert(revoke_calls >= 1u && !start_permitted);
        is_shutdown = 0u; send_epoch(UINT64_C(0xfedcba9876543210)); task();
        expect(KM_DIAGNOSTIC_FAULTED, KM_ERR_FAULTED);
        assert(begin_calls == 1u && attach_calls == 1u && !armed && !start_permitted);
#if defined(CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN) && CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN
        assert(enable_calls == 1u);
#else
        assert(enable_calls == 0u);
#endif
        return;
    }
#endif
    if (o->pattern == 0) {
        task(); expect(KM_DIAGNOSTIC_IDLE, KM_ERR_UNAVAILABLE);
        assert(!begin_calls && !clock_reads && !time_reads && !id_reads && !board_obs && !armed);
        query_check(0u, KM_DIAGNOSTIC_IDLE, KM_ERR_UNAVAILABLE);
        query_check(42u, KM_DIAGNOSTIC_IDLE, KM_ERR_UNAVAILABLE);
        irqstatus_t old = irq_save(); sched_del_timer(NULL); irq_restore(old); assert(!armed);
    } else if (o->pattern == 1) {
        start(); task(); expect(KM_DIAGNOSTIC_READY, KM_OK);
        assert(begin_calls == 1u && !service_calls && attach_calls == 1u && allow_false && !board_shutdowns && !armed);
        query_check(7u, KM_DIAGNOSTIC_READY, KM_OK);
        uint32_t tr = time_reads; start(); query_check(8u, KM_DIAGNOSTIC_READY, KM_OK);
        assert(time_reads == tr && begin_calls == 1u && attach_calls == 1u && !armed);
    } else if (o->pattern == 2) {
        start(); assert(armed && armed->waketime == origin + timer_from_us(1000));
        uint32_t w = armed->waketime; task(); expect(KM_DIAGNOSTIC_WAITING, KM_ERR_UNAVAILABLE);
        assert(begin_calls == 1u && service_calls == 1u && !attach_calls && armed && armed->waketime == w);
        uint32_t tr = time_reads; start();
        assert(time_reads == tr && begin_calls == 1u && armed && armed->waketime == w);
        pump(); expect(KM_DIAGNOSTIC_READY, KM_OK);
        assert(service_calls == 2u && attach_calls == 1u && allow_false && !board_shutdowns && !armed);
        query_check(9u, KM_DIAGNOSTIC_READY, KM_OK);
    } else if (o->pattern == 3) {
        start(); task(); expect(KM_DIAGNOSTIC_WAITING, KM_ERR_UNAVAILABLE);
        assert(!begin_calls && !id_reads && clock_reads && armed);
        clock_state = KM_CLOCK_STATE_READY; ident_mode = 1; pump();
        assert(!begin_calls && id_reads == 1u); expect(KM_DIAGNOSTIC_WAITING, KM_ERR_UNAVAILABLE);
        ident_mode = 0; pump(); expect(KM_DIAGNOSTIC_READY, KM_OK);
        assert(begin_calls == 1u && attach_calls == 1u && !board_shutdowns && !armed);
        assert((uint32_t)(ticks - origin) >= timer_from_us(2000));
        assert((uint32_t)(ticks - origin) < limit_ticks());
    } else if (o->pattern == 4) run_timeout();
    else if (o->pattern == 5 || o->pattern == 7) {
        start(); task(); expect(KM_DIAGNOSTIC_FAULTED, o->expect_res);
        assert(!begin_calls && !attach_calls && !board_shutdowns && !armed);
        assert(board_state == KM_BOARD_STARTUP_WAITING);
        if (clock_state == KM_CLOCK_STATE_FAULTED) {
            /* The detail names the step before the board began. */
            uint32_t args[1] = {77u};
            assert(detail_fmt && strcmp(detail_fmt, "km_diagnostic_detail_query challenge=%u") == 0);
            command_km_diagnostic_detail_query(args);
            assert(sent_ch == 77u && sent_step == KM_STARTUP_STEP_CLOCK);
            assert(sent_lane == KM_STARTUP_NO_LANE && sent_cause == KM_ERR_FAULTED);
        }
    } else if (o->pattern == 6) {
        start(); task(); expect(KM_DIAGNOSTIC_WAITING, KM_ERR_UNAVAILABLE);
        assert(begin_calls == 1u && service_calls == 1u && !board_shutdowns && board_state == KM_BOARD_STARTUP_WAITING && armed);
        clock_state = KM_CLOCK_STATE_FAULTED; pump(); expect(KM_DIAGNOSTIC_FAULTED, KM_ERR_FAULTED);
        assert(board_shutdowns == 1u && board_state == KM_BOARD_STARTUP_FAULTED && !attach_calls && !armed);
    } else if (o->pattern == 8) {
        start(); task(); expect(KM_DIAGNOSTIC_FAULTED, o->expect_res);
        assert(begin_calls == 1u && !attach_calls && !board_shutdowns && !armed);
        assert(board_state == KM_BOARD_STARTUP_UNINITIALIZED); task(); assert(begin_calls == 1u);
    } else if (o->pattern == 9) run_end(o);
    else if (o->pattern == 10) {
        global_shutdown(); expect(KM_DIAGNOSTIC_FAULTED, KM_ERR_FAULTED);
        assert(!begin_calls && !board_shutdowns && !time_reads && !armed);
        start(); task(); assert(!begin_calls && !time_reads);
        is_shutdown = 0u; start(); task(); assert(!begin_calls);
        expect(KM_DIAGNOSTIC_FAULTED, KM_ERR_FAULTED);
        query_check(5u, KM_DIAGNOSTIC_FAULTED, KM_ERR_FAULTED);
    } else assert(0);
    if (km_diagnostic_get_state() == KM_DIAGNOSTIC_READY) {
#if defined(CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN) && CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN
        assert(start_permitted && enable_calls == 1u && revoke_calls == 0u);
#else
        assert(!start_permitted && enable_calls == 0u);
#endif
        if (strcmp(o->name, "ready") == 0) {
            global_shutdown();
            assert(!start_permitted && km_diagnostic_get_state() == KM_DIAGNOSTIC_FAULTED);
        }
    } else {
        assert(!start_permitted && enable_calls == 0u);
    }
}

static const struct opt OPTS[] = {
#ifdef CONFIG_KLIPPER_MMU_DIFF_BUFFER
    {"missing_calibration", 1, 0, 0, 1, 3, 0, 0, 0, 0, 0, KM_CLOCK_STATE_READY, KM_OK, KM_OK, KM_OK, KM_ERR_FAULTED},
#endif
#ifdef CONFIG_KLIPPER_MMU_BOOT_IDENTITY_HOST
    {"host_zero", 11, 0, 0, 0, 0, 0, 0, 0, 0, 0, KM_CLOCK_STATE_READY, KM_OK, KM_OK, KM_OK, KM_ERR_INVALID_PARAM},
    {"host_rebind", 12, 0, 0, 0, 1, 0, 0, 0, 0, 1, KM_CLOCK_STATE_READY, KM_OK, KM_OK, KM_OK, KM_OK},
#endif
    {"idle", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, KM_CLOCK_STATE_READY, KM_OK, KM_OK, KM_OK, KM_OK},
    {"ready", 1, 0, 0, 1, 3, 0, 0, 0, 0, 1, KM_CLOCK_STATE_READY, KM_OK, KM_OK, KM_OK, KM_OK},
    {"pending", 2, 0, 0, 0, 1, 0, 0, 0, 0, 1, KM_CLOCK_STATE_READY, KM_OK, KM_OK, KM_OK, KM_OK},
    {"prereq", 3, 0, 0, 1, 3, 0, 0, 0, 0, 1, KM_CLOCK_STATE_UNAVAILABLE, KM_OK, KM_OK, KM_OK, KM_OK},
    {"timeout_clock", 4, 0, 0, 0, 0, 0, 0, 0, 0, 0, KM_CLOCK_STATE_UNAVAILABLE, KM_OK, KM_OK, KM_OK, KM_ERR_TIMEOUT},
    {"timeout_identity", 4, 1, 0, 0, 0, 0, 0, 0, 0, 0, KM_CLOCK_STATE_READY, KM_OK, KM_OK, KM_OK, KM_ERR_TIMEOUT},
    {"timeout_wrap", 4, 0, 0, 0, 0, 0, 0, 0, 1, 0, KM_CLOCK_STATE_UNAVAILABLE, KM_OK, KM_OK, KM_OK, KM_ERR_TIMEOUT},
    {"pre_clock", 5, 0, 1, 0, 0, 0, 0, 0, 0, 0, KM_CLOCK_STATE_FAULTED, KM_OK, KM_OK, KM_OK, KM_ERR_FAULTED},
    {"pre_ident", 5, 2, 1, 0, 0, 0, 0, 0, 0, 0, KM_CLOCK_STATE_READY, KM_OK, KM_OK, KM_OK, KM_ERR_BUSY},
    {"pre_zero", 5, 3, 1, 0, 0, 0, 0, 0, 0, 0, KM_CLOCK_STATE_READY, KM_OK, KM_OK, KM_OK, KM_ERR_FAULTED},
    {"clock_owned", 6, 0, 0, 0, 0, 0, 0, 0, 0, 0, KM_CLOCK_STATE_READY, KM_OK, KM_OK, KM_OK, KM_ERR_FAULTED},
    {"foreign_wait", 7, 0, 1, 0, 3, 0, 0, 0, 0, 0, KM_CLOCK_STATE_READY, KM_OK, KM_OK, KM_OK, KM_ERR_BUSY},
    {"foreign_busy", 8, 0, 0, 0, 3, 0, 0, 0, 0, 0, KM_CLOCK_STATE_READY, KM_ERR_BUSY, KM_OK, KM_OK, KM_ERR_BUSY},
    {"foreign_err", 8, 0, 0, 0, 3, 0, 0, 0, 0, 0, KM_CLOCK_STATE_READY, KM_ERR_FAULTED, KM_OK, KM_OK, KM_ERR_FAULTED},
    {"fail_service", 9, 0, 0, 0, 2, 0, 0, 0, 0, 0, KM_CLOCK_STATE_READY, KM_OK, KM_ERR_BUSY, KM_OK, KM_ERR_BUSY},
    {"fail_core", 9, 0, 0, 1, 3, 1, 0, 0, 0, 0, KM_CLOCK_STATE_READY, KM_OK, KM_OK, KM_OK, KM_ERR_FAULTED},
    {"fail_attach", 9, 0, 0, 1, 3, 0, 0, 0, 0, 1, KM_CLOCK_STATE_READY, KM_OK, KM_OK, KM_ERR_BUSY, KM_ERR_BUSY},
    {"cost_begin", 9, 0, 0, 0, 0, 0, 1, 0, 0, 0, KM_CLOCK_STATE_READY, KM_OK, KM_OK, KM_OK, KM_ERR_TIMEOUT},
    {"cost_service", 9, 0, 0, 0, 3, 0, 2, 0, 0, 0, KM_CLOCK_STATE_READY, KM_OK, KM_OK, KM_OK, KM_ERR_TIMEOUT},
    {"cost_attach", 9, 0, 0, 1, 3, 0, 3, 0, 0, 1, KM_CLOCK_STATE_READY, KM_OK, KM_OK, KM_OK, KM_ERR_TIMEOUT},
    {"shut_before", 10, 0, 0, 0, 0, 0, 0, 0, 0, 0, KM_CLOCK_STATE_READY, KM_OK, KM_OK, KM_OK, KM_ERR_FAULTED},
    {"shut_begin", 9, 0, 0, 1, 3, 0, 0, 1, 0, 0, KM_CLOCK_STATE_READY, KM_OK, KM_OK, KM_OK, KM_ERR_FAULTED},
    {"shut_service", 9, 0, 0, 0, 3, 0, 0, 2, 0, 0, KM_CLOCK_STATE_READY, KM_OK, KM_OK, KM_OK, KM_ERR_FAULTED},
    {"shut_attach", 9, 0, 0, 1, 3, 0, 0, 3, 0, 1, KM_CLOCK_STATE_READY, KM_OK, KM_OK, KM_OK, KM_ERR_FAULTED},
};

int main(int argc, char **argv) {
    assert(argc == 2);
    assert((int)KM_OK == 0 && (int)KM_ERR_UNAVAILABLE == -3);
    assert(saw_const && saw_start && saw_query && reg_task == km_diagnostic_task && reg_shut == km_diagnostic_shutdown);
    assert(start_flags == 0u && start_fmt && strcmp(start_fmt, START_FMT) == 0);
#ifdef CONFIG_KLIPPER_MMU_BOOT_IDENTITY_HOST
    assert(saw_host_const);
#endif
    assert(query_flags == (uint32_t)HF_IN_SHUTDOWN && query_fmt && strcmp(query_fmt, "km_diagnostic_query challenge=%u") == 0);
    for (size_t i = 0; i < sizeof OPTS / sizeof OPTS[0]; i++) {
        if (strcmp(OPTS[i].name, argv[1]) != 0) continue;
        setup(&OPTS[i]); dispatch(&OPTS[i]); puts("ok"); return 0;
    }
    return 2;
}
