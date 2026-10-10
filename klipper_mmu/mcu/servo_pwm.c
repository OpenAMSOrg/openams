// SPDX-License-Identifier: GPL-3.0-or-later
// Servo output: Klipper's scheduled software PWM on one GPIO, the way
// src/gpiocmds.c's digital_out toggles a pin from a timer -- not a copy of
// it, since a servo only ever holds one duty setting (no move queue).
#include "servo_pwm.h"
#include "autoconf.h"
#if CONFIG_WANT_GPIO_LOCAL
#include "stm32/gpio_local.h"
#endif
#include "board/gpio.h"
#include "board/irq.h"
#include "board/misc.h"
#include "sched.h"
#include <string.h>

typedef struct {
    bool claimed;
    bool queued;           /* Timer is in the scheduler until an event returns SF_DONE. */
    uint32_t pulse_ticks;  /* 0 == stopped. Written only under irq_save(). */
    uint32_t period_ticks;
    struct gpio_out pin;
    struct timer timer;
    km_servo_t api;
} km_servo_slot_t;

/* KM_SERVO_MAX_SLOTS is 1: one servo per board. */
static km_servo_slot_t servo_slot;
static bool global_shutdown_latched;

static uint_fast8_t servo_off_event(struct timer *timer);

static uint_fast8_t servo_on_event(struct timer *timer)
{
    (void)timer;
    km_servo_slot_t *s = &servo_slot;
    if (!s->pulse_ticks || global_shutdown_latched || sched_is_shutdown()) {
        gpio_out_write(s->pin, 0);
        s->queued = false;
        return SF_DONE;
    }
    gpio_out_write(s->pin, 1);
    s->timer.waketime += s->pulse_ticks;
    s->timer.func = servo_off_event;
    return SF_RESCHEDULE;
}

static uint_fast8_t servo_off_event(struct timer *timer)
{
    (void)timer;
    km_servo_slot_t *s = &servo_slot;
    gpio_out_write(s->pin, 0);
    if (!s->pulse_ticks || global_shutdown_latched || sched_is_shutdown()) {
        s->queued = false;
        return SF_DONE;
    }
    s->timer.waketime += s->period_ticks - s->pulse_ticks;
    s->timer.func = servo_on_event;
    return SF_RESCHEDULE;
}

static km_result_t servo_set_pulse_us(km_servo_t *self, uint16_t pulse_us)
{
    if (!self || self->user_data != &servo_slot) return KM_ERR_INVALID_PARAM;
    km_servo_slot_t *s = &servo_slot;
    if (!s->claimed) return KM_ERR_UNAVAILABLE;
    if (pulse_us >= KM_SERVO_PERIOD_US) return KM_ERR_INVALID_PARAM;
    uint32_t ticks = pulse_us ? timer_from_us(pulse_us) : 0u;
    irqstatus_t flag = irq_save();
    if (global_shutdown_latched || sched_is_shutdown()) {
        irq_restore(flag);
        return KM_ERR_FAULTED;
    }
    s->pulse_ticks = ticks;
    /* A stop followed by a restart before the pending event runs must not
     * add the still-queued timer a second time. */
    if (ticks && !s->queued) {
        s->queued = true;
        s->timer.func = servo_on_event;
        s->timer.waketime = timer_read_time() + timer_from_us(1u);
        sched_add_timer(&s->timer);
    }
    /* A stop never drives the pin low here: a pulse in progress would be cut
     * into a runt. The pending event ends it at full width and retires. */
    irq_restore(flag);
    return KM_OK;
}

KM_API km_result_t km_servo_pwm_setup(uint32_t pin, km_servo_t **out)
{
    if (!out || pin >= 128u) return KM_ERR_INVALID_PARAM;
    if (global_shutdown_latched || sched_is_shutdown()) return KM_ERR_FAULTED;
    km_servo_slot_t *s = &servo_slot;
    if (s->claimed) return KM_ERR_BUSY;
#if CONFIG_WANT_GPIO_LOCAL
    if (!gpio_pin_is_local(pin)) return KM_ERR_CONFLICT;
#endif
    memset(s, 0, sizeof(*s));
    s->period_ticks = timer_from_us(KM_SERVO_PERIOD_US);
#if CONFIG_WANT_GPIO_LOCAL
    s->pin = gpio_out_setup_local(pin, 0);
#else
    s->pin = gpio_out_setup(pin, 0);
#endif
    s->claimed = true;
    s->api.set_pulse_us = servo_set_pulse_us;
    s->api.user_data = s;
    *out = &s->api;
    return KM_OK;
}

KM_API bool km_servo_pwm_is_slot_active(uint32_t slot)
{
    return slot < KM_SERVO_MAX_SLOTS && servo_slot.claimed;
}

void km_servo_pwm_shutdown(void)
{
    irqstatus_t flag = irq_save();
    global_shutdown_latched = true;
    if (servo_slot.claimed)
        gpio_out_write(servo_slot.pin, 0);
    irq_restore(flag);
}
DECL_SHUTDOWN(km_servo_pwm_shutdown);
