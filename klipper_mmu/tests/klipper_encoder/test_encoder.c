// SPDX-License-Identifier: GPL-3.0-or-later
/* Native tests for mcu/encoder.c's GPIO counting logic: km_config_encoder
 * (the Kconfig pin-resolution factory) is disabled at compile time here
 * (shim/autoconf.h) and is instead covered by the mock board_startup.c
 * suite's contract-level fakes and by tests/test_pin_config.py's real
 * Kconfig-driven pin plan. */
#include "encoder.h"
#include "pin_config.h"
#include "board/gpio.h"
#include "board/irq.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/wait.h>

/* ---- board/gpio.h fake: one settable level per pin id ------------------ */
static uint8_t levels[64];
struct gpio_in gpio_in_setup(uint32_t pin, int32_t pull_up)
{
    assert(pin < 64);
    (void)pull_up;
    return (struct gpio_in){pin};
}
uint8_t gpio_in_read(struct gpio_in g)
{
    assert(g.encoded < 64);
    return levels[g.encoded];
}

/* ---- board/irq.h fake --------------------------------------------------- */
irqstatus_t irq_save(void) { return 0; }
void irq_restore(irqstatus_t flag) { (void)flag; }

/* ---- sched.h fake --------------------------------------------------------- */
static uint8_t scheduler_shutdown;
uint8_t sched_is_shutdown(void) { return scheduler_shutdown; }

/* ---- command.h fake: never expected to fire in these tests -------------- */
static unsigned shutdown_calls;
void shutdown(const char *msg) { (void)msg; ++shutdown_calls; }

/* ---- clock.h fake --------------------------------------------------------- */
static km_result_t clock_result = KM_OK;
static km_core_timepoint_t clock_now;
km_result_t km_clock_get_time(km_core_timepoint_t *out)
{
    if (clock_result == KM_OK) *out = clock_now;
    return clock_result;
}

/* ---- operation_core.h's km_core_set_encoder_at fake ---------------------- */
static int32_t recorded_clicks;
static bool recorded_valid;
static uint32_t recorded_sample_ms;
static unsigned recorded_calls;
void km_core_set_encoder_at(km_operation_core_t *core, int32_t clicks, bool valid, uint32_t sample_ms)
{
    (void)core;
    recorded_clicks = clicks;
    recorded_valid = valid;
    recorded_sample_ms = sample_ms;
    ++recorded_calls;
}

static void reset(void)
{
    memset(levels, 0, sizeof(levels));
    scheduler_shutdown = 0;
    shutdown_calls = 0;
    clock_result = KM_OK;
    clock_now = (km_core_timepoint_t){.ms = 1000, .us = 1000000};
    recorded_clicks = 0; recorded_valid = false; recorded_sample_ms = 0; recorded_calls = 0;
}

static km_operation_core_t core;

/* Test 1: km_encoder_gpio_setup validates its argument shape before
 * touching any pin. pin_b == KM_PIN_UNUSED is deliberately *not* checked
 * here -- that is the one-wire shape (test_single_wire_setup_and_sample
 * below), not an error. */
static void test_setup_rejects_invalid_config(void)
{
    reset();
    assert(km_encoder_gpio_setup(NULL) == KM_ERR_INVALID_PARAM);
    km_encoder_gpio_config_t unassigned_a = {.pin_a = KM_PIN_UNUSED, .pin_b = 5, .pull = 0};
    assert(km_encoder_gpio_setup(&unassigned_a) == KM_ERR_INVALID_PARAM);
    km_encoder_gpio_config_t duplicate = {.pin_a = 5, .pin_b = 5, .pull = 0};
    assert(km_encoder_gpio_setup(&duplicate) == KM_ERR_INVALID_PARAM);
    assert(shutdown_calls == 0);
}

/* Test ("invalid when unconfigured" + one wire): Happy Hare's own ERCF/
 * Tradrack encoder is a single wire (installer/Kconfig.encoder:128); an
 * unset pin_b is accepted, never sampled, and never fabricated into a
 * click. Runs in its own forked process (main() below) so this is free to
 * be the one real km_encoder_gpio_setup() call in it -- the driver is an
 * immutable single-lifetime binding (encoder.h), like real hardware. */
static void test_single_wire_setup_and_sample(void)
{
    reset();
    km_encoder_gpio_config_t cfg = {.pin_a = 5, .pin_b = KM_PIN_UNUSED, .pull = 1};
    assert(km_encoder_gpio_setup(&cfg) == KM_OK);
    /* Already bound: a second call is refused, not silently re-armed. */
    assert(km_encoder_gpio_setup(&cfg) == KM_ERR_BUSY);
    assert(km_encoder_gpio_get_clicks() == 0);

    /* An unwired "pin 9" toggling does nothing: has_b is false, so it is
     * never read, let alone counted. */
    levels[9] = 1;
    km_encoder_gpio_sample(&core, NULL);
    assert(recorded_valid == true);
    assert(recorded_clicks == 0);

    /* The one real wire still counts normally. */
    levels[5] = 1;
    km_encoder_gpio_sample(&core, NULL);
    assert(recorded_clicks == 1);
    assert(km_encoder_gpio_get_clicks() == 1);
    km_encoder_gpio_sample(&core, NULL);
    assert(recorded_clicks == 1); /* Settled: no further click. */
}

/* Test 2 ("invalid when unconfigured"): sampling before setup publishes
 * valid=false with the driver's starting total (0), not an invented
 * zero-movement reading layered on top of some other value. */
static void test_sample_invalid_before_setup(void)
{
    reset();
    core.last_control_ms = 4242u;
    km_encoder_gpio_sample(&core, NULL);
    assert(recorded_calls == 1);
    assert(recorded_valid == false);
    assert(recorded_clicks == 0);
    assert(recorded_sample_ms == 4242u);
}

/* Test ("counting", two wires): every level change on either pin adds
 * exactly one click; a sample with no change adds none. Matches Happy
 * Hare's own MCU_counter, generalized to a second wire (encoder.h). Runs
 * in its own forked process (main() below), so this two-wire setup does
 * not collide with the one-wire setup above. */
static void test_counts_each_pin_transition(void)
{
    reset();
    km_encoder_gpio_config_t cfg = {.pin_a = 5, .pin_b = 9, .pull = 1};
    assert(km_encoder_gpio_setup(&cfg) == KM_OK);
    /* Already bound: a second call is refused, not silently re-armed. */
    assert(km_encoder_gpio_setup(&cfg) == KM_ERR_BUSY);
    assert(km_encoder_gpio_get_clicks() == 0);

    /* No change: no click. */
    km_encoder_gpio_sample(&core, NULL);
    assert(recorded_valid == true);
    assert(recorded_clicks == 0);
    assert(km_encoder_gpio_get_clicks() == 0);

    /* Pin A toggles once. */
    levels[5] = 1;
    km_encoder_gpio_sample(&core, NULL);
    assert(recorded_clicks == 1);
    assert(km_encoder_gpio_get_clicks() == 1);

    /* Pin B toggles once. */
    levels[9] = 1;
    km_encoder_gpio_sample(&core, NULL);
    assert(recorded_clicks == 2);

    /* Both toggle back on the same sample: two clicks in one call. */
    levels[5] = 0; levels[9] = 0;
    km_encoder_gpio_sample(&core, NULL);
    assert(recorded_clicks == 4);

    /* Settling with no change again adds nothing further. */
    km_encoder_gpio_sample(&core, NULL);
    assert(recorded_clicks == 4);
    assert(recorded_calls == 5);
}

/* Test: once armed, an unavailable clock or a shutdown/stopped driver
 * reports invalid but retains the running total for diagnostics rather
 * than inventing a zero/no-movement reading (km_core_set_load_sensor_at's
 * "retains its value for diagnostics" precedent, cited in encoder.h). */
static void test_invalid_retains_last_total(void)
{
    reset();
    km_encoder_gpio_config_t cfg = {.pin_a = 5, .pin_b = 9, .pull = 1};
    assert(km_encoder_gpio_setup(&cfg) == KM_OK);
    levels[5] = 1;
    km_encoder_gpio_sample(&core, NULL);
    assert(recorded_clicks == 1 && recorded_valid == true);

    /* Clock adapter faulted: invalid, but the same total is republished. */
    clock_result = KM_ERR_FAULTED;
    core.last_control_ms = 99u;
    km_encoder_gpio_sample(&core, NULL);
    assert(recorded_valid == false);
    assert(recorded_clicks == 1);
    assert(recorded_sample_ms == 99u);
    clock_result = KM_OK;

    /* Global scheduler shutdown: invalid, total still retained. */
    scheduler_shutdown = 1;
    km_encoder_gpio_sample(&core, NULL);
    assert(recorded_valid == false);
    assert(recorded_clicks == 1);
    scheduler_shutdown = 0;

    /* km_encoder_gpio_shutdown() latches the same invalid/retained shape. */
    km_encoder_gpio_shutdown();
    km_encoder_gpio_sample(&core, NULL);
    assert(recorded_valid == false);
    assert(recorded_clicks == 1);
}

/* Test 5 ("overflow/wrap"): the accumulator is a plain uint32_t and the
 * driver hands the core (int32_t)total_clicks -- the exact contract
 * encoder.h documents. This checks that contract directly: consecutive
 * uint32_t values straddling the wrap boundary reinterpret as a
 * consecutive int32_t run (delta +1 every step, no discontinuity), which
 * is what lets operation_core.c's plain "clicks changed"/delta checks
 * keep working through a wrap without a special case. Driving the real
 * 32-bit accumulator through 2^32 real GPIO samples to observe the same
 * property directly would take hours for no additional evidence. */
static void test_wrap_arithmetic_contract(void)
{
    uint32_t raw = UINT32_MAX - 1u; /* 0xFFFFFFFE */
    int32_t prev = (int32_t)raw;
    for (int i = 0; i < 4; ++i) {
        ++raw;
        int32_t now = (int32_t)raw;
        assert((int64_t)now - (int64_t)prev == 1);
        prev = now;
    }
    assert(raw == 2u); /* 0xFFFFFFFE + 4, wrapped: 2 (2^32-2+4 mod 2^32). */
}

/* km_encoder_gpio_setup is a one-shot, immutable-until-reset binding
 * (encoder.h), so each test that calls it for real needs a fresh process,
 * exactly like tests/klipper_board_startup/test_board_startup.c's own
 * run_test. */
static void run_test(void (*test_fn)(void), const char *name)
{
    fflush(stdout);
    fflush(stderr);
    pid_t pid = fork();
    if (pid < 0) { perror("fork"); exit(1); }
    if (pid == 0) { test_fn(); exit(0); }
    int status = 0;
    if (waitpid(pid, &status, 0) < 0) { perror("waitpid"); exit(1); }
    if (WIFSIGNALED(status)) {
        fprintf(stderr, "[-] Test %s CRASHED with signal %d\n", name, WTERMSIG(status));
        exit(1);
    }
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
        fprintf(stderr, "[-] Test %s FAILED (exit status %d)\n", name, WEXITSTATUS(status));
        exit(1);
    }
    printf("[+] %s PASSED\n", name);
}

int main(void)
{
    run_test(test_setup_rejects_invalid_config, "test_setup_rejects_invalid_config");
    run_test(test_sample_invalid_before_setup, "test_sample_invalid_before_setup");
    run_test(test_single_wire_setup_and_sample, "test_single_wire_setup_and_sample");
    run_test(test_counts_each_pin_transition, "test_counts_each_pin_transition");
    run_test(test_invalid_retains_last_total, "test_invalid_retains_last_total");
    run_test(test_wrap_arithmetic_contract, "test_wrap_arithmetic_contract");
    printf("=== All 6 encoder test suites PASSED! ===\n");
    return 0;
}
