// SPDX-License-Identifier: GPL-3.0-or-later
#include <assert.h>
#include <stdio.h>
#include "stepper_reference.h"

static void check_steps(const km_ref_step_t *got, int n, const uint32_t *times,
                        const uint8_t *dirs, int want) {
    assert(n == want);
    for (int i = 0; i < want; i++) {
        assert(got[i].time == times[i]);
        assert(got[i].dir == dirs[i]);
    }
}

/* One move from idle: the first step is start_clock plus its interval, then
 * one interval each. With add 0 the intervals are constant. */
static void test_one_move(void) {
    km_ref_stepper_t s;
    km_ref_step_t out[16];
    int n = -1;
    km_ref_init(&s, 4, 100);
    assert(km_ref_queue(&s, 1000, 2000, 200, (uint16_t)4, 0, 1) == 0);
    assert(km_ref_active(&s) == 1);
    assert(km_ref_room(&s) == 3);
    km_ref_advance(&s, 10000, out, 16, &n);
    const uint32_t times[4] = {2200, 2400, 2600, 2800};
    const uint8_t dirs[4] = {1, 1, 1, 1};
    check_steps(out, n, times, dirs, 4);
    assert(km_ref_position(&s) == 4);
    assert(km_ref_active(&s) == 0);
    assert(km_ref_room(&s) == 4);
}

/* add != 0: interval grows by add after every step. */
static void test_add(void) {
    km_ref_stepper_t s;
    km_ref_step_t out[16];
    int n = -1;
    km_ref_init(&s, 4, 100);
    assert(km_ref_queue(&s, 1000, 2000, 100, (uint16_t)4, 10, 1) == 0);
    km_ref_advance(&s, 10000, out, 16, &n);
    /* stepper.c: 2100, then the interval grows before each later step:
     * +110 -> 2210, +120 -> 2330, +130 -> 2460. */
    const uint32_t times[4] = {2100, 2210, 2330, 2460};
    const uint8_t dirs[4] = {1, 1, 1, 1};
    check_steps(out, n, times, dirs, 4);
    assert(km_ref_position(&s) == 4);
}

/* A move queued while active starts at the previous step's time plus its own
 * interval; its start_clock argument is ignored. */
static void test_back_to_back(void) {
    km_ref_stepper_t s;
    km_ref_step_t out[16];
    int n = -1;
    km_ref_init(&s, 4, 100);
    assert(km_ref_queue(&s, 1000, 2000, 200, (uint16_t)2, 0, 1) == 0);
    assert(km_ref_queue(&s, 1000, 0, 300, (uint16_t)2, 0, 1) == 0);
    assert(km_ref_room(&s) == 2);
    km_ref_advance(&s, 10000, out, 16, &n);
    const uint32_t times[4] = {2200, 2400, 2700, 3000};
    const uint8_t dirs[4] = {1, 1, 1, 1};
    check_steps(out, n, times, dirs, 4);
    assert(km_ref_position(&s) == 4);
    assert(km_ref_room(&s) == 4);
}

/* A direction change between moves: every step of a move carries its dir. */
static void test_direction(void) {
    km_ref_stepper_t s;
    km_ref_step_t out[16];
    int n = -1;
    km_ref_init(&s, 4, 100);
    assert(km_ref_queue(&s, 1000, 2000, 100, (uint16_t)2, 0, 1) == 0);
    assert(km_ref_queue(&s, 1000, 0, 150, (uint16_t)2, 0, 0) == 0);
    km_ref_advance(&s, 10000, out, 16, &n);
    const uint32_t times[4] = {2100, 2200, 2350, 2500};
    const uint8_t dirs[4] = {1, 1, 0, 0};
    check_steps(out, n, times, dirs, 4);
    assert(km_ref_position(&s) == 0);
}

/* Refusals queue nothing and never fault: count 0, a full pool, and a start
 * clock inside the idle lead. */
static void test_refusals(void) {
    km_ref_stepper_t s;
    km_ref_step_t out[16];
    int n = -1;
    km_ref_init(&s, 4, 100);
    assert(km_ref_queue(&s, 1000, 2000, 200, 0, 0, 1) == -1);
    assert(km_ref_active(&s) == 0);
    assert(km_ref_room(&s) == 4);
    /* 1050 is only 50 after now, less than the 100 tick lead. */
    assert(km_ref_queue(&s, 1000, 1050, 200, (uint16_t)1, 0, 1) == -1);
    assert(km_ref_room(&s) == 4);
    /* Exactly the lead is accepted; once active the lead does not apply. */
    assert(km_ref_queue(&s, 1000, 1100, 200, (uint16_t)1, 0, 1) == 0);
    assert(km_ref_queue(&s, 1000, 0, 200, (uint16_t)1, 0, 1) == 0);
    assert(km_ref_room(&s) == 2);
    km_ref_stop(&s);

    /* A one-move pool refuses a second move until the first has run. */
    km_ref_init(&s, 1, 100);
    assert(km_ref_queue(&s, 1000, 2000, 200, (uint16_t)2, 0, 1) == 0);
    assert(km_ref_room(&s) == 0);
    assert(km_ref_queue(&s, 1000, 0, 200, (uint16_t)1, 0, 1) == -1);
    assert(km_ref_room(&s) == 0);
    km_ref_advance(&s, 10000, out, 16, &n);
    assert(n == 2);
    assert(km_ref_room(&s) == 1);
    assert(km_ref_active(&s) == 0);
}

/* Stop drops the queue and frees the pool, keeps the completed position, and
 * the next move needs (and uses) a fresh start clock. */
static void test_stop(void) {
    km_ref_stepper_t s;
    km_ref_step_t out[16];
    int n = -1;
    km_ref_init(&s, 4, 100);
    assert(km_ref_queue(&s, 1000, 2000, 100, (uint16_t)5, 0, 1) == 0);
    km_ref_advance(&s, 2200, out, 16, &n);
    assert(n == 2);
    assert(km_ref_position(&s) == 2);
    assert(km_ref_active(&s) == 1);
    km_ref_stop(&s);
    assert(km_ref_active(&s) == 0);
    assert(km_ref_room(&s) == 4);
    assert(km_ref_position(&s) == 2);
    /* Idle again: the lead is re-checked and the new clock is honoured. */
    assert(km_ref_queue(&s, 5000, 5050, 100, (uint16_t)1, 0, 0) == -1);
    assert(km_ref_queue(&s, 5000, 6000, 100, (uint16_t)1, 0, 0) == 0);
    km_ref_advance(&s, 10000, out, 16, &n);
    assert(n == 1);
    assert(out[0].time == 6100u);
    assert(out[0].dir == 0);
    assert(km_ref_position(&s) == 1);
}

/* Position is signed: forward steps add, reverse steps subtract. */
static void test_position(void) {
    km_ref_stepper_t s;
    km_ref_step_t out[32];
    int n = -1;
    km_ref_init(&s, 4, 100);
    assert(km_ref_queue(&s, 1000, 2000, 50, (uint16_t)3, 0, 1) == 0);
    assert(km_ref_queue(&s, 1000, 0, 50, (uint16_t)5, 0, 0) == 0);
    km_ref_advance(&s, 100000, out, 32, &n);
    assert(n == 8);
    for (int i = 0; i < 3; i++) {
        assert(out[i].dir == 1);
    }
    for (int i = 3; i < 8; i++) {
        assert(out[i].dir == 0);
    }
    assert(km_ref_position(&s) == -2);
}

/* Step times are uint32 ticks that wrap; the lead is a signed difference too. */
static void test_wrap(void) {
    km_ref_stepper_t s;
    km_ref_step_t out[16];
    int n = -1;
    km_ref_init(&s, 4, 100);
    /* Start just below the wrap; the first step lands past it. */
    assert(km_ref_queue(&s, 0xFFFFFE00u, 0xFFFFFF00u, 0x100u, (uint16_t)5, 0,
                        1) == 0);
    km_ref_advance(&s, 0x00000800u, out, 16, &n);
    const uint32_t times[5] = {0x00000000u, 0x00000100u, 0x00000200u,
                               0x00000300u, 0x00000400u};
    const uint8_t dirs[5] = {1, 1, 1, 1, 1};
    check_steps(out, n, times, dirs, 5);
    assert(km_ref_position(&s) == 5);

    /* A start clock on the far side of the wrap is still ahead of now. */
    km_ref_stop(&s);
    assert(km_ref_queue(&s, 0xFFFFFF00u, 0x00000020u, 0x40u, (uint16_t)1, 0,
                        1) == 0);
    km_ref_advance(&s, 0x00000060u, out, 16, &n);
    assert(n == 1);
    assert(out[0].time == 0x00000060u);
}

int main(void) {
    test_one_move();
    test_add();
    test_back_to_back();
    test_direction();
    test_refusals();
    test_stop();
    test_position();
    test_wrap();
    printf("test_stepper_reference: all checks passed\n");
    return 0;
}
