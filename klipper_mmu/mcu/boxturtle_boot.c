// SPDX-License-Identifier: GPL-3.0-or-later
/* BoxTurtle on the AFC Lite preset. The init hook only schedules this task.
 * The task calls the shipped board startup once the scheduler is in task
 * context. It never grants START and never requests motion. A pre-lifecycle
 * admission reject (invalid arguments or a missing backend) leaves the board
 * uninitialized. A later owned failure still latches fail-closed.
 */
#include "autoconf.h"
#include "board/irq.h"
#include "board/misc.h"
#include "board_startup.h"
#include "clock.h"
#include "sched.h"
#include <stdbool.h>

#define KM_BOOT_TIMEOUT_US 1000000u

static struct task_wake boot_wake;
static struct timer boot_timer;
static bool timer_armed;
static bool boot_finished;
static bool boot_stamped;
static uint32_t boot_start_tick;
static km_result_t (*const boot_startup)(const km_resource_claim_t *, size_t,
                                         uint32_t, const km_diff_adc_config_t *)
    = km_board_startup_begin;

static void disarm_timer_locked(void)
{
    if (!timer_armed || sched_is_shutdown()) {
        timer_armed = false;
        return;
    }
    timer_armed = false;
    sched_del_timer(&boot_timer);
}

static uint_fast8_t boot_timer_event(struct timer *t)
{
    if (boot_finished || sched_is_shutdown()) {
        timer_armed = false;
        return SF_DONE;
    }
    sched_wake_task(&boot_wake);
    t->waketime = timer_read_time() + timer_from_us(1000);
    return SF_RESCHEDULE;
}

static void arm_timer(void)
{
    irqstatus_t flag = irq_save();
    if (!timer_armed && !boot_finished && !sched_is_shutdown()) {
        boot_timer.func = boot_timer_event;
        boot_timer.waketime = timer_read_time() + timer_from_us(1000);
        timer_armed = true;
        sched_add_timer(&boot_timer);
    }
    irq_restore(flag);
}

static bool boot_deadline(void)
{
    uint32_t elapsed = timer_read_time() - boot_start_tick;
    return elapsed >= timer_from_us(KM_BOOT_TIMEOUT_US);
}

static void finish_closed(void)
{
    if (km_board_startup_get_state() != KM_BOARD_STARTUP_FAULTED)
        km_board_startup_shutdown();
    boot_finished = true;
    irqstatus_t flag = irq_save();
    disarm_timer_locked();
    irq_restore(flag);
}

__attribute__((externally_visible))
void km_boxturtle_boot_init(void)
{
    /* Keep the shipped entry on this init function under LTO. */
    __asm__ __volatile__("" : : "r"(boot_startup) : "memory");
    sched_wake_task(&boot_wake);
}
DECL_INIT(km_boxturtle_boot_init);

__attribute__((externally_visible))
void km_boxturtle_boot_task(void)
{
    if (!sched_check_wake(&boot_wake))
        return;
    if (boot_finished)
        return;
    if (sched_is_shutdown()) {
        boot_finished = true;
        return;
    }
    if (!boot_stamped) {
        boot_start_tick = timer_read_time();
        boot_stamped = true;
    }
    if (boot_deadline()) {
        finish_closed();
        return;
    }

    enum km_board_startup_state state = km_board_startup_get_state();
    if (state == KM_BOARD_STARTUP_UNINITIALIZED) {
        if (km_clock_get_state() == KM_CLOCK_STATE_UNAVAILABLE) {
            arm_timer();
            return;
        }
        km_result_t result = boot_startup(NULL, 0, KM_BOOT_TIMEOUT_US, NULL);
        state = km_board_startup_get_state();
        if (state == KM_BOARD_STARTUP_UNINITIALIZED) {
            if (result == KM_ERR_UNAVAILABLE) {
                arm_timer();
                return;
            }
            /* begin() rejected before it owned outputs. Latching FAULTED here
             * would make a later valid admission impossible. */
            if (result == KM_ERR_INVALID_PARAM || result == KM_ERR_NOT_SUPPORTED) {
                boot_finished = true;
                irqstatus_t flag = irq_save();
                disarm_timer_locked();
                irq_restore(flag);
                return;
            }
            finish_closed();
            return;
        }
    }
    if (state == KM_BOARD_STARTUP_WAITING) {
        (void)km_board_startup_service();
        state = km_board_startup_get_state();
        if (state == KM_BOARD_STARTUP_WAITING) {
            arm_timer();
            return;
        }
    }
    boot_finished = true;
    irqstatus_t flag = irq_save();
    disarm_timer_locked();
    irq_restore(flag);
}
DECL_TASK(km_boxturtle_boot_task);
