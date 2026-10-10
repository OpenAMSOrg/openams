// SPDX-License-Identifier: GPL-3.0-or-later
#include "clock.h"
#include "shim_klipper.h"
#include "board/misc.h"
#include "board/irq.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <assert.h>
#include <unistd.h>
#include <sys/wait.h>

#define ASSERT_TRUE(cond) do { \
    if (!(cond)) { \
        fprintf(stderr, "Assertion failed at %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        abort(); \
    } \
} while (0)

#define ASSERT_EQ(a, b) do { \
    if ((a) != (b)) { \
        fprintf(stderr, "Assertion failed at %s:%d: %s (%llu) == %s (%llu)\n", \
                __FILE__, __LINE__, #a, (unsigned long long)(a), #b, (unsigned long long)(b)); \
        abort(); \
    } \
} while (0)

/**
 * 1. Pre-ready calls:
 * - Before task runs, adapter is UNAVAILABLE.
 * - km_clock_get_time returns KM_ERR_UNAVAILABLE and preserves *out.
 * - km_clock_deadline_ticks returns KM_ERR_UNAVAILABLE and preserves outputs.
 * - Command reports state=0 (UNAVAILABLE), ms=0, us=0.
 */
static void test_pre_ready_calls(void)
{
    shim_reset(520000000u, 1u, 1u, 4u);
    ASSERT_EQ(km_clock_get_state(), KM_CLOCK_STATE_UNAVAILABLE);

    km_core_timepoint_t out = { .ms = 0xDEADBEEFu, .us = 0xCAFEBABEu };
    km_result_t res = km_clock_get_time(&out);
    ASSERT_EQ(res, KM_ERR_UNAVAILABLE);
    ASSERT_EQ(out.ms, 0xDEADBEEFu);
    ASSERT_EQ(out.us, 0xCAFEBABEu);

    uint32_t ticks = 0x12345678u;
    bool due = true;
    res = km_clock_deadline_ticks(1000u, &ticks, &due);
    ASSERT_EQ(res, KM_ERR_UNAVAILABLE);
    ASSERT_EQ(ticks, 0x12345678u);
    ASSERT_TRUE(due == true);

    /* Diagnostic command before task runs */
    command_func_t cmd = shim_find_command("km_clock_query");
    ASSERT_TRUE(cmd != NULL);
    cmd(NULL);
    ASSERT_TRUE(test_shim_sendf_count > 0);
    ASSERT_TRUE(!test_shim_sendf_called_in_lock);

    /* Parse diagnostic reply */
    uint32_t state = 99u, ms = 99u, us = 99u, dev = 0u, board = 0u, lanes = 0u;
    int parsed = sscanf(test_shim_last_sendf,
                        "km_clock_state state=%u ms=%u us=%u device_id=%u board_id=%u lane_count=%u",
                        &state, &ms, &us, &dev, &board, &lanes);
    ASSERT_EQ(parsed, 6);
    ASSERT_EQ(state, (uint32_t)KM_CLOCK_STATE_UNAVAILABLE);
    ASSERT_EQ(ms, 0u);
    ASSERT_EQ(us, 0u);
    ASSERT_EQ(dev, 1u);
    ASSERT_EQ(board, 1u);
    ASSERT_EQ(lanes, 4u);
}

/**
 * 2. Delayed task startup & board readiness:
 * - DECL_INIT runs before board timer hardware is initialized.
 * - Verify timer is NEVER armed before board readiness.
 * - Once board is ready, task runs, establishes origin {0,0} at current ticks,
 *   and arms the 1ms maintenance timer.
 */
static void test_delayed_task_startup(void)
{
    shim_reset(520000000u, 1u, 1u, 4u);
    shim_set_ticks(100000u);

    /* DECL_INIT runs early during boot */
    shim_run_init();

    /* Board timer hardware is NOT ready yet */
    ASSERT_EQ(test_shim_timer_add_count, 0);
    ASSERT_TRUE(!test_shim_timer_armed_before_ready);
    ASSERT_EQ(km_clock_get_state(), KM_CLOCK_STATE_UNAVAILABLE);

    /* Hardware timer counter advances while firmware boots */
    shim_advance_ticks(500000u); /* now at 600,000 ticks */

    /* Board is now ready, task dispatch starts */
    shim_set_board_ready(true);
    shim_run_task();

    /* Timer armed once board is ready */
    ASSERT_EQ(test_shim_timer_add_count, 1);
    ASSERT_TRUE(!test_shim_timer_armed_before_ready);
    ASSERT_EQ(km_clock_get_state(), KM_CLOCK_STATE_READY);
    ASSERT_TRUE(test_shim_active_timer != NULL);

    /* Waketime should be initial_ticks + timer_from_us(1000) */
    uint32_t expected_wake = 600000u + timer_from_us(1000u);
    ASSERT_EQ(test_shim_active_timer->waketime, expected_wake);

    /* Clock should be at origin {0,0} */
    km_core_timepoint_t out = { 99u, 99u };
    km_result_t res = km_clock_get_time(&out);
    ASSERT_EQ(res, KM_OK);
    ASSERT_EQ(out.ms, 0u);
    ASSERT_EQ(out.us, 0u);
}

/**
 * 3. Duplicate task wake:
 * - Repeated task calls must not reset or duplicate the maintenance timer.
 */
static void test_duplicate_task_wake(void)
{
    shim_reset(520000000u, 1u, 1u, 4u);
    shim_set_board_ready(true);
    shim_run_init();
    shim_run_task();

    ASSERT_EQ(test_shim_timer_add_count, 1);
    ASSERT_EQ(km_clock_get_state(), KM_CLOCK_STATE_READY);

    /* Advance time and call task again */
    shim_advance_ticks(2000000u);
    shim_run_task();
    ASSERT_EQ(test_shim_timer_add_count, 1); /* Timer add count MUST remain 1 */

    shim_advance_ticks(5000000u);
    shim_run_task();
    ASSERT_EQ(test_shim_timer_add_count, 1); /* Still 1 */
}

/**
 * 4. Periodic idle service & raw 32-bit wrap at 520 MHz:
 * - Start raw ticks just before 2^32 wrap (e.g. 0xFFFE0000).
 * - Step maintenance timer in 1ms increments across wrap.
 * - Verify monotonic ms and us advance and no clock fault occurs.
 */
static void test_periodic_service_wrap_520mhz(void)
{
    uint32_t freq = 520000000u;
    shim_reset(freq, 1u, 1u, 4u);
    shim_set_board_ready(true);

    /* Start raw tick counter 10ms before wrap */
    uint32_t step_ticks = timer_from_us(1000u); /* 520,000 ticks per 1ms */
    uint32_t start_ticks = 0xFFFFFFFFu - (step_ticks * 5u);
    shim_set_ticks(start_ticks);

    shim_run_init();
    shim_run_task();
    ASSERT_EQ(km_clock_get_state(), KM_CLOCK_STATE_READY);

    km_core_timepoint_t prev = { .ms = 0u, .us = 0u };
    km_result_t res = km_clock_get_time(&prev);
    ASSERT_EQ(res, KM_OK);
    ASSERT_EQ(prev.ms, 0u);
    ASSERT_EQ(prev.us, 0u);

    /* Run 20 iterations of 1ms maintenance timer dispatch across wrap */
    for (int i = 1; i <= 20; i++) {
        shim_advance_ticks(step_ticks);
        uint_fast8_t sres = shim_dispatch_timer();
        ASSERT_EQ(sres, (uint_fast8_t)SF_RESCHEDULE);
        ASSERT_EQ(km_clock_get_state(), KM_CLOCK_STATE_READY);

        km_core_timepoint_t cur;
        res = km_clock_get_time(&cur);
        ASSERT_EQ(res, KM_OK);

        /* Milliseconds and microseconds must advance monotonically */
        ASSERT_TRUE(cur.ms >= prev.ms);
        ASSERT_TRUE(cur.us > prev.us);
        ASSERT_EQ(cur.ms, (uint32_t)i);
        ASSERT_EQ(cur.us, (uint32_t)(i * 1000));

        /* Verify active timer waketime was advanced to now_ticks + 1ms without backlog */
        uint32_t expected_wake = test_shim_ticks + step_ticks;
        ASSERT_EQ(test_shim_active_timer->waketime, expected_wake);

        prev = cur;
    }
}

/**
 * 5. Periodic idle service & raw 32-bit wrap at fractional frequency:
 * - Timer frequency with fractional MHz: 12,345,678 Hz.
 * - Test 1ms steps over raw wrap to verify fractional phase residue.
 */
static void test_periodic_service_wrap_fractional(void)
{
    uint32_t freq = 12345678u; /* Fractional rate: 12345678 % 1000000 != 0 */
    shim_reset(freq, 2u, 1u, 4u);
    shim_set_board_ready(true);

    uint32_t step_ticks = timer_from_us(1000u); /* ceil(1000 * 12345678 / 1000000) = 12346 */
    uint32_t start_ticks = 0xFFFFFFFFu - (step_ticks * 5u);
    shim_set_ticks(start_ticks);

    shim_run_init();
    shim_run_task();
    ASSERT_EQ(km_clock_get_state(), KM_CLOCK_STATE_READY);

    for (int i = 1; i <= 20; i++) {
        shim_advance_ticks(step_ticks);
        uint_fast8_t sres = shim_dispatch_timer();
        ASSERT_EQ(sres, (uint_fast8_t)SF_RESCHEDULE);
        ASSERT_EQ(km_clock_get_state(), KM_CLOCK_STATE_READY);

        km_core_timepoint_t cur;
        km_result_t res = km_clock_get_time(&cur);
        ASSERT_EQ(res, KM_OK);
        ASSERT_TRUE(cur.us > 0u);
    }
}

/**
 * 6. Command timestamp coherence & sendf outside critical section:
 * - Check that command snapshots coherent ms/us under critical section.
 * - Check that sendf occurs strictly outside the lock.
 * - Verify command output fields match configured constants.
 */
static void test_command_coherence_and_lock(void)
{
    shim_reset(520000000u, 1u, 1u, 4u);
    shim_set_board_ready(true);
    shim_run_init();
    shim_run_task();

    /* Advance time by 5ms */
    shim_advance_ticks(timer_from_us(5000u));
    shim_dispatch_timer();

    km_core_timepoint_t cur;
    km_result_t res = km_clock_get_time(&cur);
    ASSERT_EQ(res, KM_OK);

    command_func_t cmd = shim_find_command("km_clock_query");
    ASSERT_TRUE(cmd != NULL);

    test_shim_sendf_called_in_lock = false;
    cmd(NULL);

    ASSERT_TRUE(!test_shim_sendf_called_in_lock); /* MUST be sent outside the lock */

    uint32_t state = 99u, ms = 99u, us = 99u, dev = 0u, board = 0u, lanes = 0u;
    int parsed = sscanf(test_shim_last_sendf,
                        "km_clock_state state=%u ms=%u us=%u device_id=%u board_id=%u lane_count=%u",
                        &state, &ms, &us, &dev, &board, &lanes);
    ASSERT_EQ(parsed, 6);
    ASSERT_EQ(state, (uint32_t)KM_CLOCK_STATE_READY);
    ASSERT_EQ(ms, cur.ms);
    ASSERT_EQ(us, cur.us);
    ASSERT_EQ(dev, 1u);
    ASSERT_EQ(board, 1u);
    ASSERT_EQ(lanes, 4u);
}

/**
 * 7. Deadline rounding and due behavior:
 * - Past or due deadline returns out_due = true, out_ticks = current ticks.
 * - Future deadline returns out_due = false, out_ticks = target ticks (rounded up).
 * - Ambiguous (diff == 2^31) returns KM_ERR_INVALID_PARAM and preserves outputs.
 * - Far-future (required_ticks >= 2^31) returns KM_ERR_INVALID_PARAM and preserves outputs.
 */
static void test_deadline_rounding_and_due(void)
{
    shim_reset(520000000u, 1u, 1u, 4u);
    shim_set_board_ready(true);
    shim_run_init();
    shim_run_task();

    /* Advance by 10ms */
    shim_advance_ticks(timer_from_us(10000u));
    shim_dispatch_timer();

    km_core_timepoint_t now;
    km_result_t res = km_clock_get_time(&now);
    ASSERT_EQ(res, KM_OK);
    ASSERT_EQ(now.us, 10000u);

    /* 1. Exactly due deadline */
    uint32_t target_ticks = 0u;
    bool is_due = false;
    res = km_clock_deadline_ticks(now.us, &target_ticks, &is_due);
    ASSERT_EQ(res, KM_OK);
    ASSERT_TRUE(is_due == true);
    ASSERT_EQ(target_ticks, test_shim_ticks);

    /* 2. Past deadline (100 us ago) */
    res = km_clock_deadline_ticks(now.us - 100u, &target_ticks, &is_due);
    ASSERT_EQ(res, KM_OK);
    ASSERT_TRUE(is_due == true);
    ASSERT_EQ(target_ticks, test_shim_ticks);

    /* 3. Future deadline (50 us in future) */
    res = km_clock_deadline_ticks(now.us + 50u, &target_ticks, &is_due);
    ASSERT_EQ(res, KM_OK);
    ASSERT_TRUE(is_due == false);
    /* At 520MHz, 50us is exactly 50 * 520 = 26,000 ticks */
    uint32_t expected_ticks = test_shim_ticks + 26000u;
    ASSERT_EQ(target_ticks, expected_ticks);

    /* 4. Ambiguous deadline (diff == 0x80000000) */
    uint32_t sent_ticks = 0x55AA55AAu;
    bool sent_due = true;
    res = km_clock_deadline_ticks(now.us + 0x80000000u, &sent_ticks, &sent_due);
    ASSERT_EQ(res, KM_ERR_INVALID_PARAM);
    ASSERT_EQ(sent_ticks, 0x55AA55AAu); /* Preserved */
    ASSERT_TRUE(sent_due == true);      /* Preserved */

    /* 5. Range limit (diff requiring >= 2^31 ticks) */
    /* At 520MHz, 5,000,000 us requires 2.6 * 10^9 ticks >= 2^31 */
    uint32_t far_future_us = now.us + 5000000u;
    res = km_clock_deadline_ticks(far_future_us, &sent_ticks, &sent_due);
    ASSERT_EQ(res, KM_ERR_INVALID_PARAM);
    ASSERT_EQ(sent_ticks, 0x55AA55AAu); /* Preserved */
    ASSERT_TRUE(sent_due == true);      /* Preserved */

    /* 6. NULL pointer checks */
    res = km_clock_deadline_ticks(now.us + 10u, NULL, &sent_due);
    ASSERT_EQ(res, KM_ERR_INVALID_PARAM);
    res = km_clock_deadline_ticks(now.us + 10u, &sent_ticks, NULL);
    ASSERT_EQ(res, KM_ERR_INVALID_PARAM);
}

/**
 * 8. Invalid observation latching:
 * - Ambiguous or backward tick delta (>= 2^31) latches fault.
 * - Fatal fault triggers try_shutdown.
 * - Subsequent observations and queries stay permanently faulted.
 */
static void test_invalid_observation_latching(void)
{
    shim_reset(520000000u, 1u, 1u, 4u);
    shim_set_board_ready(true);
    shim_run_init();
    shim_run_task();

    /* Valid advance */
    shim_advance_ticks(timer_from_us(1000u));
    shim_dispatch_timer();
    ASSERT_EQ(km_clock_get_state(), KM_CLOCK_STATE_READY);
    ASSERT_TRUE(!test_shim_is_shutdown);

    /* Inject backward tick (delta = 0x80000000 >= 2^31) */
    shim_advance_ticks(0x80000000u);
    uint_fast8_t sres = shim_dispatch_timer();

    /* Timer callback must abort rescheduling */
    ASSERT_EQ(sres, (uint_fast8_t)SF_DONE);
    ASSERT_TRUE(test_shim_is_shutdown);
    ASSERT_EQ(test_shim_shutdown_count, 1);
    ASSERT_EQ(km_clock_get_state(), KM_CLOCK_STATE_FAULTED);

    /* Subsequent queries must return KM_ERR_FAULTED and preserve outputs */
    km_core_timepoint_t out = { 0x11u, 0x22u };
    km_result_t res = km_clock_get_time(&out);
    ASSERT_EQ(res, KM_ERR_FAULTED);
    ASSERT_EQ(out.ms, 0x11u);
    ASSERT_EQ(out.us, 0x22u);

    uint32_t ticks = 0x33u;
    bool due = false;
    res = km_clock_deadline_ticks(5000u, &ticks, &due);
    ASSERT_EQ(res, KM_ERR_FAULTED);
    ASSERT_EQ(ticks, 0x33u);
    ASSERT_TRUE(due == false);

    /* Even if raw ticks advance forward legitimately now, fault stays latched */
    shim_advance_ticks(1000u);
    res = km_clock_get_time(&out);
    ASSERT_EQ(res, KM_ERR_FAULTED);

    /* Diagnostic command reports faulted state with ms=0, us=0 */
    command_func_t cmd = shim_find_command("km_clock_query");
    ASSERT_TRUE(cmd != NULL);
    cmd(NULL);
    uint32_t state = 99u, ms = 99u, us = 99u, dev = 0u, board = 0u, lanes = 0u;
    int parsed = sscanf(test_shim_last_sendf,
                        "km_clock_state state=%u ms=%u us=%u device_id=%u board_id=%u lane_count=%u",
                        &state, &ms, &us, &dev, &board, &lanes);
    ASSERT_EQ(parsed, 6);
    ASSERT_EQ(state, (uint32_t)KM_CLOCK_STATE_FAULTED);
    ASSERT_EQ(ms, 0u);
    ASSERT_EQ(us, 0u);
}

/**
 * A delayed maintenance callback remains usable for the largest legal raw
 * interval, including raw-counter wrap. The next diagnostic and deadline
 * commands must see the advanced epoch. An exactly half-range interval is
 * ambiguous and must instead shut the MCU clock down permanently.
 */
static void test_long_idle_boundary_and_followup_commands(void)
{
    const uint32_t freq = 520000000u;
    const uint32_t legal_delta = 0x7fffffffu;
    shim_reset(freq, 1u, 1u, 4u);
    shim_set_board_ready(true);
    shim_set_ticks(0xfff00000u);
    shim_run_init();
    shim_run_task();
    ASSERT_EQ(km_clock_get_state(), KM_CLOCK_STATE_READY);

    /* A single delayed service crosses raw wrap but stays below half range. */
    shim_advance_ticks(legal_delta);
    ASSERT_EQ(shim_dispatch_timer(), (uint_fast8_t)SF_RESCHEDULE);
    ASSERT_EQ(km_clock_get_state(), KM_CLOCK_STATE_READY);
    ASSERT_EQ(test_shim_active_timer->waketime,
              test_shim_ticks + timer_from_us(1000u));

    km_core_timepoint_t now;
    ASSERT_EQ(km_clock_get_time(&now), KM_OK);
    ASSERT_EQ(now.us, legal_delta / 520u);
    ASSERT_EQ(now.ms, now.us / 1000u);

    command_func_t query = shim_find_command("km_clock_query");
    ASSERT_TRUE(query != NULL);
    query(NULL);
    uint32_t state = 99u, ms = 99u, us = 99u, device = 0u, board = 0u, lanes = 0u;
    int parsed = sscanf(test_shim_last_sendf,
                        "km_clock_state state=%u ms=%u us=%u device_id=%u board_id=%u lane_count=%u",
                        &state, &ms, &us, &device, &board, &lanes);
    ASSERT_EQ(parsed, 6);
    ASSERT_EQ(state, (uint32_t)KM_CLOCK_STATE_READY);
    ASSERT_EQ(ms, now.ms);
    ASSERT_EQ(us, now.us);

    uint32_t target = 0u;
    bool due = true;
    ASSERT_EQ(km_clock_deadline_ticks(now.us + 2000u, &target, &due), KM_OK);
    ASSERT_TRUE(!due);
    /* The legal-delta remainder is 127,000,000 / 520,000,000 us. */
    ASSERT_EQ(target, test_shim_ticks + timer_from_us(2000u) - 127u);

    /* No legal interpretation exists for this subsequent half-range gap. */
    shim_advance_ticks(0x80000000u);
    ASSERT_EQ(shim_dispatch_timer(), (uint_fast8_t)SF_DONE);
    ASSERT_TRUE(test_shim_is_shutdown);
    ASSERT_EQ(km_clock_get_state(), KM_CLOCK_STATE_FAULTED);
    target = 0x12345678u;
    due = true;
    ASSERT_EQ(km_clock_deadline_ticks(now.us + 3000u, &target, &due), KM_ERR_FAULTED);
    ASSERT_EQ(target, 0x12345678u);
    ASSERT_TRUE(due);
}

/**
 * 9. Shutdown & no automatic resume:
 * - Upstream shutdown stops rescheduling.
 * - Repeated task calls do NOT re-arm timer or restart clock.
 * - State stays in shutdown / faulted.
 */
static void test_shutdown_no_automatic_resume(void)
{
    shim_reset(520000000u, 1u, 1u, 4u);
    shim_set_board_ready(true);
    shim_run_init();
    shim_run_task();

    /* Trigger upstream shutdown */
    shutdown("test emergency stop");
    ASSERT_TRUE(test_shim_is_shutdown);

    /* State must reflect shutdown */
    enum km_clock_state st = km_clock_get_state();
    ASSERT_TRUE(st == KM_CLOCK_STATE_SHUTDOWN || st == KM_CLOCK_STATE_FAULTED);

    /* Calls fail with KM_ERR_FAULTED */
    km_core_timepoint_t out = { 1u, 2u };
    km_result_t res = km_clock_get_time(&out);
    ASSERT_EQ(res, KM_ERR_FAULTED);
    ASSERT_EQ(out.ms, 1u);
    ASSERT_EQ(out.us, 2u);

    /* Repeated task call must NEVER reinitialize or arm timer */
    int prior_timer_adds = test_shim_timer_add_count;
    shim_advance_ticks(1000000u);
    shim_run_task();
    ASSERT_EQ(test_shim_timer_add_count, prior_timer_adds);
    ASSERT_TRUE(test_shim_active_timer == NULL);
}

/**
 * 10. Calls from already-disabled IRQ context:
 * - Calling km_clock_get_time or km_clock_deadline_ticks while IRQ is disabled
 *   must preserve the disabled state upon exit (no accidental re-enabling).
 */
static void test_already_disabled_irq_context(void)
{
    shim_reset(520000000u, 1u, 1u, 4u);
    shim_set_board_ready(true);
    shim_run_init();
    shim_run_task();

    /* Disable IRQs */
    irq_disable();
    ASSERT_TRUE(test_shim_irq_is_disabled());

    km_core_timepoint_t out;
    km_result_t res = km_clock_get_time(&out);
    ASSERT_EQ(res, KM_OK);

    /* IRQs must STILL be disabled */
    ASSERT_TRUE(test_shim_irq_is_disabled());

    uint32_t ticks;
    bool due;
    res = km_clock_deadline_ticks(out.us + 100u, &ticks, &due);
    ASSERT_EQ(res, KM_OK);

    /* IRQs must STILL be disabled */
    ASSERT_TRUE(test_shim_irq_is_disabled());

    irq_enable();
    ASSERT_TRUE(!test_shim_irq_is_disabled());
}

static void test_shutdown_before_first_task(void)
{
    shim_reset(520000000u, 1u, 1u, 4u);
    shim_run_init();
    shutdown("early shutdown");
    ASSERT_EQ(km_clock_get_state(), KM_CLOCK_STATE_SHUTDOWN);
    test_shim_is_shutdown = false; /* Models upstream sched_clear_shutdown. */
    shim_set_board_ready(true);
    shim_run_task();
    ASSERT_EQ(km_clock_get_state(), KM_CLOCK_STATE_SHUTDOWN);
    ASSERT_EQ(test_shim_timer_add_count, 0);
    km_core_timepoint_t out = {1u, 2u};
    ASSERT_EQ(km_clock_get_time(&out), KM_ERR_FAULTED);
    ASSERT_EQ(out.ms, 1u);
    ASSERT_EQ(out.us, 2u);
}

/* Isolated test runner using fork() to guarantee pristine address space */
static void run_isolated(void (*test_fn)(void), const char *name)
{
    printf("Running %-52s ... ", name);
    fflush(stdout);

    pid_t pid = fork();
    if (pid < 0) {
        perror("fork");
        exit(1);
    }
    if (pid == 0) {
        test_fn();
        exit(0);
    }

    int status = 0;
    waitpid(pid, &status, 0);
    if (WIFEXITED(status) && WEXITSTATUS(status) == 0) {
        printf("PASS\n");
    } else if (WIFSIGNALED(status)) {
        printf("FAIL (signal %d)\n", WTERMSIG(status));
        exit(1);
    } else {
        printf("FAIL (exit code %d)\n", WEXITSTATUS(status));
        exit(1);
    }
}

int main(void)
{
    printf("============================================================\n");
    printf("  Klipper MMU Scheduler Clock Adapter Integration Tests    \n");
    printf("============================================================\n");

    run_isolated(test_shutdown_before_first_task, "shutdown_before_first_task");
    run_isolated(test_pre_ready_calls, "pre_ready_calls");
    run_isolated(test_delayed_task_startup, "delayed_task_startup");
    run_isolated(test_duplicate_task_wake, "duplicate_task_wake");
    run_isolated(test_periodic_service_wrap_520mhz, "periodic_service_wrap_520mhz");
    run_isolated(test_periodic_service_wrap_fractional, "periodic_service_wrap_fractional");
    run_isolated(test_command_coherence_and_lock, "command_coherence_and_lock");
    run_isolated(test_deadline_rounding_and_due, "deadline_rounding_and_due");
    run_isolated(test_invalid_observation_latching, "invalid_observation_latching");
    run_isolated(test_long_idle_boundary_and_followup_commands,
                 "long_idle_boundary_and_followup_commands");
    run_isolated(test_shutdown_no_automatic_resume, "shutdown_no_automatic_resume");
    run_isolated(test_already_disabled_irq_context, "already_disabled_irq_context");

    printf("============================================================\n");
    printf("  All clock adapter scenarios passed successfully!\n");
    printf("============================================================\n");
    return 0;
}
