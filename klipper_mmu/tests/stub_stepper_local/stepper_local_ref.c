// SPDX-License-Identifier: GPL-3.0-or-later
/* Strong, reference-backed stand-ins for stepper_local_* (see stepper.h in
 * this directory), for suites that link mcu/controller.c and want the local
 * stepper to really step. This file is deliberately separate from
 * stepper_local_stub.c: the CONFIG_WANT_GPIO_LOCAL claim/polarity runner
 * (tests/klipper_stepdir_gpio_local/run_tests.py) compiles that file alone,
 * without tests/stepper_reference.c, so the trivial bodies there stay
 * km_ref-free and weak; where both files are linked, these strong
 * definitions win.
 *
 * The controller only ever holds a struct stepper * obtained from
 * km_stepdir_gpio_get_stepper_for_driver(), and the suites bind mock drivers,
 * so a small static table maps each bound km_driver_t * to one reference
 * stepper. There is no timer of our own: the reference advances lazily to
 * timer_read_time() whenever the controller (or a test accessor) reads the
 * position or activity, exactly like the real local stepper is advanced by
 * Klipper's own step timer. */
#include "stepper.h"
#include "stub_stepper.h"
#include "../stepper_reference.h"
#include "board/misc.h"

/* One entry per driver the suites bind (they use one or two); a full table
 * means no stepper, which the controller treats as "not local". */
#define STUB_STEPPER_TABLE 4

struct stepper {
    const km_driver_t *driver;
    km_ref_stepper_t ref;
    int32_t last_position32;   /* mcu/stepdir_gpio.c delta accounting */
    int64_t position;
    uint32_t steps;            /* steps the reference executor has run */
    uint32_t disabled_steps;   /* ... while the driver was not enabled */
};

static struct stepper stubs[STUB_STEPPER_TABLE];

/* Run every step the reference has due by now, recording the ones that run
 * while the driver is disabled: the local stepper never sees ENABLE, so those
 * are the steps a real motor would take de-energized. */
static void advance_to_now(struct stepper *e)
{
    km_ref_step_t due[8];
    const int max_out = (int)(sizeof due / sizeof due[0]);
    uint32_t now = timer_read_time();
    int n = 0;
    do {
        km_ref_advance(&e->ref, now, due, max_out, &n);
        for (int i = 0; i < n; ++i) {
            e->steps++;
            if (!e->driver->enabled)
                e->disabled_steps++;
        }
    } while (n == max_out);
}

static struct stepper *find_or_create(const km_driver_t *d)
{
    if (!d)
        return NULL;
    for (unsigned i = 0; i < STUB_STEPPER_TABLE; ++i)
        if (stubs[i].driver == d)
            return &stubs[i];
    for (unsigned i = 0; i < STUB_STEPPER_TABLE; ++i) {
        if (!stubs[i].driver) {
            stubs[i] = (struct stepper){0};
            stubs[i].driver = d;
            /* (LEAD / tick + 2) x KM_STEP_SEGMENTS_MAX = 4 x 56 = 224 moves
             * would be the real pool, but the reference executor caps its
             * pool at KM_REF_MAX_MOVES, so the pool is KM_REF_MAX_MOVES (64)
             * moves: enough for a window, and a full pool is a refusal. */
            km_ref_init(&stubs[i].ref, KM_REF_MAX_MOVES,
                        timer_from_us(STEPPER_LOCAL_MIN_LEAD_US));
            return &stubs[i];
        }
    }
    return NULL;
}

struct stepper *stub_stepper_for_driver(const km_driver_t *d)
{
    return find_or_create(d);
}

int64_t stub_stepper_position_for_driver(const km_driver_t *d)
{
    struct stepper *e = find_or_create(d);
    if (!e)
        return 0;
    advance_to_now(e);
    int32_t pos32 = (int32_t)e->ref.position;
    e->position += (int32_t)(pos32 - e->last_position32);
    e->last_position32 = pos32;
    return e->position;
}

uint32_t stub_stepper_steps(const km_driver_t *d)
{
    struct stepper *e = find_or_create(d);
    if (!e)
        return 0;
    advance_to_now(e);
    return e->steps;
}

uint32_t stub_stepper_disabled_steps(const km_driver_t *d)
{
    struct stepper *e = find_or_create(d);
    if (!e)
        return 0;
    advance_to_now(e);
    return e->disabled_steps;
}

int stepper_local_queue(struct stepper *s, uint32_t start_clock, uint32_t interval,
                        uint16_t count, int16_t add, uint8_t dir)
{
    if (!s)
        return -1;
    advance_to_now(s);
    return km_ref_queue(&s->ref, timer_read_time(), start_clock, interval, count, add, dir);
}

int stepper_local_room(struct stepper *s)
{
    return s ? km_ref_room(&s->ref) : 0;
}

int stepper_local_active(struct stepper *s)
{
    if (!s)
        return 0;
    advance_to_now(s);
    return km_ref_active(&s->ref);
}

int32_t stepper_local_position(struct stepper *s)
{
    if (!s)
        return 0;
    advance_to_now(s);
    return (int32_t)s->ref.position;
}

void stepper_local_stop(struct stepper *s)
{
    if (s)
        km_ref_stop(&s->ref);
}
