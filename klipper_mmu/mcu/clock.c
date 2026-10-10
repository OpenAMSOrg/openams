// SPDX-License-Identifier: GPL-3.0-or-later
#include "clock.h"
#include "autoconf.h"
#include "board/irq.h"
#include "board/misc.h"
#include "command.h"
#include "sched.h"

void km_clock_init(void);
void km_clock_task(void);
void km_clock_shutdown(void);
void command_km_clock_query(uint32_t *args);

static km_mcu_clock_t km_clock;
static enum km_clock_state adapter_state = KM_CLOCK_STATE_UNAVAILABLE;
static struct task_wake km_clock_wake;
static struct timer km_maintenance_timer;

/* Call with interrupts disabled. A newly detected fault enters Klipper's
 * non-returning shutdown path; later queries return KM_ERR_FAULTED. */
static km_result_t
observe_locked(km_core_timepoint_t *out, uint32_t *observed_ticks)
{
    if (sched_is_shutdown())
        return KM_ERR_FAULTED;
    if (adapter_state == KM_CLOCK_STATE_UNAVAILABLE)
        return KM_ERR_UNAVAILABLE;
    if (adapter_state != KM_CLOCK_STATE_READY)
        return KM_ERR_FAULTED;
    uint32_t ticks = timer_read_time();
    if (km_mcu_clock_observe(&km_clock, ticks, out) != KM_OK) {
        adapter_state = KM_CLOCK_STATE_FAULTED;
        try_shutdown("klipper_mmu: clock fault");
        return KM_ERR_FAULTED;
    }
    if (observed_ticks)
        *observed_ticks = ticks;
    return KM_OK;
}

static uint_fast8_t
km_clock_timer_event(struct timer *t)
{
    km_core_timepoint_t now;
    uint32_t ticks;
    if (observe_locked(&now, &ticks) != KM_OK)
        return SF_DONE;
    /* One future wake, never a loop replaying missed maintenance intervals. */
    t->waketime = ticks + timer_from_us(1000u);
    return SF_RESCHEDULE;
}

void km_clock_init(void)
{
    /* Board timer initialization may follow this init hook. */
    sched_wake_task(&km_clock_wake);
}
DECL_INIT(km_clock_init);

void km_clock_task(void)
{
    if (!sched_check_wake(&km_clock_wake))
        return;
    irqstatus_t flag = irq_save();
    if (adapter_state == KM_CLOCK_STATE_UNAVAILABLE && !sched_is_shutdown()) {
        uint32_t ticks = timer_read_time();
        if (km_mcu_clock_init(&km_clock, CONFIG_CLOCK_FREQ, ticks,
                              (km_core_timepoint_t){0, 0}) != KM_OK) {
            adapter_state = KM_CLOCK_STATE_FAULTED;
            shutdown("klipper_mmu: clock init failed");
        }
        adapter_state = KM_CLOCK_STATE_READY;
        km_maintenance_timer.func = km_clock_timer_event;
        km_maintenance_timer.waketime = ticks + timer_from_us(1000u);
        sched_add_timer(&km_maintenance_timer);
    }
    irq_restore(flag);
}
DECL_TASK(km_clock_task);

void km_clock_shutdown(void)
{
    /* Klipper has already reset user timers. Latch even before first task. */
    irqstatus_t flag = irq_save();
    if (adapter_state != KM_CLOCK_STATE_FAULTED)
        adapter_state = KM_CLOCK_STATE_SHUTDOWN;
    irq_restore(flag);
}
DECL_SHUTDOWN(km_clock_shutdown);

enum km_clock_state km_clock_get_state(void)
{
    irqstatus_t flag = irq_save();
    enum km_clock_state state = adapter_state;
    if (sched_is_shutdown() && state != KM_CLOCK_STATE_FAULTED)
        state = KM_CLOCK_STATE_SHUTDOWN;
    irq_restore(flag);
    return state;
}

KM_API km_result_t km_clock_get_time(km_core_timepoint_t *out)
{
    if (!out)
        return KM_ERR_INVALID_PARAM;
    irqstatus_t flag = irq_save();
    km_result_t result = observe_locked(out, NULL);
    irq_restore(flag);
    return result;
}

KM_API km_result_t km_clock_get_completion_time(km_core_timepoint_t *out)
{
    if (!out)
        return KM_ERR_INVALID_PARAM;
    irqstatus_t flag = irq_save();
    km_core_timepoint_t now;
    km_result_t result = observe_locked(&now, NULL);
    if (result == KM_OK)
        result = km_mcu_clock_get_completion_time(&km_clock, out);
    irq_restore(flag);
    return result;
}

KM_API km_result_t km_clock_get_completion_us(uint32_t *out_us)
{
    if (!out_us)
        return KM_ERR_INVALID_PARAM;
    km_core_timepoint_t completion;
    km_result_t result = km_clock_get_completion_time(&completion);
    if (result == KM_OK) *out_us = completion.us;
    return result;
}

KM_API km_result_t km_clock_deadline_ticks(uint32_t deadline_us,
                                           uint32_t *out_ticks, bool *out_due)
{
    if (!out_ticks || !out_due)
        return KM_ERR_INVALID_PARAM;
    irqstatus_t flag = irq_save();
    km_core_timepoint_t now;
    km_result_t result = observe_locked(&now, NULL);
    if (result == KM_OK)
        result = km_mcu_clock_deadline_ticks(&km_clock, deadline_us, out_ticks, out_due);
    irq_restore(flag);
    return result;
}

void command_km_clock_query(uint32_t *args)
{
    (void)args;
    irqstatus_t flag = irq_save();
    km_core_timepoint_t now = {0, 0};
    (void)observe_locked(&now, NULL);
    enum km_clock_state state = km_clock_get_state();
    irq_restore(flag);
    sendf("km_clock_state state=%u ms=%u us=%u device_id=%u board_id=%u lane_count=%u",
          (uint32_t)state, now.ms, now.us, (uint32_t)CONFIG_KLIPPER_MMU_DEVICE_ID,
          (uint32_t)CONFIG_KLIPPER_MMU_BOARD_ID, (uint32_t)CONFIG_KLIPPER_MMU_LANE_COUNT);
}
DECL_COMMAND_FLAGS(command_km_clock_query, HF_IN_SHUTDOWN, "km_clock_query");

DECL_CONSTANT("KLIPPER_MMU_PROTOCOL_VERSION", CONFIG_KLIPPER_MMU_PROTOCOL_VERSION);
DECL_CONSTANT("KLIPPER_MMU_INTEGRATION_VERSION", CONFIG_KLIPPER_MMU_INTEGRATION_VERSION);
#if defined(CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN) && CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN
DECL_CONSTANT("KLIPPER_MMU_ACTUATION", 1);
#else
DECL_CONSTANT("KLIPPER_MMU_ACTUATION", 0);
#endif
DECL_CONSTANT("KLIPPER_MMU_DEVICE_ID", CONFIG_KLIPPER_MMU_DEVICE_ID);
DECL_CONSTANT("KLIPPER_MMU_BOARD_ID", CONFIG_KLIPPER_MMU_BOARD_ID);
DECL_CONSTANT("KLIPPER_MMU_LANE_COUNT", CONFIG_KLIPPER_MMU_LANE_COUNT);
DECL_CONSTANT("KLIPPER_MMU_CLOCK_STATE_UNAVAILABLE", KM_CLOCK_STATE_UNAVAILABLE);
DECL_CONSTANT("KLIPPER_MMU_CLOCK_STATE_READY", KM_CLOCK_STATE_READY);
DECL_CONSTANT("KLIPPER_MMU_CLOCK_STATE_FAULTED", KM_CLOCK_STATE_FAULTED);
DECL_CONSTANT("KLIPPER_MMU_CLOCK_STATE_SHUTDOWN", KM_CLOCK_STATE_SHUTDOWN);
