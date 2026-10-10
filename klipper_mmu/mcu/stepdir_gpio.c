// SPDX-License-Identifier: GPL-3.0-or-later
#include "stepdir_gpio.h"
#include "clock.h"
#include "autoconf.h"
#if CONFIG_WANT_GPIO_LOCAL
#include "stm32/gpio_local.h"
#endif
#include "board/irq.h"
#include "board/misc.h"
#include "board/gpio.h"
#include "command.h"
#include "sched.h"
#include "stepper.h"
#include "klipper_mmu/step_segments.h"

typedef struct {
    bool claimed;
    bool enable_setup_done;
    bool step_setup_done;
    bool dir_setup_done;
    bool prepared;
    bool activation_attempted;
    bool raw_off_during_activation;
    bool is_published;

    uint32_t step_pin;
    uint32_t dir_pin;
    uint32_t enable_pin;

    struct gpio_out step_gpio;
    struct gpio_out dir_gpio;
    struct gpio_out enable_gpio;

    km_step_dir_config_t config;
    bool (*user_ready)(void *context);
    km_fault_t (*user_fault)(void *context);
    void *user_context;

    km_driver_t driver;
    struct stepper *stepper;
    int32_t last_position32;
    int64_t position;
    km_fault_t latched_fault;
} km_stepdir_slot_t;

static km_stepdir_slot_t slots[KM_MAX_LANES];
static bool global_shutdown = false;


static km_result_t locked_enable(km_driver_t *d, bool enable)
{
    km_stepdir_slot_t *s = (km_stepdir_slot_t *)d->user_data;
    if (!s || !s->enable_setup_done) return KM_ERR_INVALID_PARAM;
    /* A latched shutdown must not be resumable through a driver pointer held
     * from before it: the old portable driver's io.ready() refused here. */
    if (enable && (global_shutdown || sched_is_shutdown()))
        return KM_ERR_FAULTED;
    /* Loss of readiness suppresses a new enable, matching the previous
     * portable-driver behavior (src/step_dir_driver.c checked the same
     * io.ready() callback before allowing a real enable). */
    if (enable && !(s->user_ready && s->user_ready(s->user_context)))
        return KM_ERR_UNAVAILABLE;
    irqstatus_t flag = irq_save();
    gpio_out_write(s->enable_gpio, (enable ^ !s->config.enable_active_high) ? 1u : 0u);
    irq_restore(flag);
    return KM_OK;
}

static void locked_stop(km_driver_t *d)
{
    km_stepdir_slot_t *s = (km_stepdir_slot_t *)d->user_data;
    if (!s) return;
    irqstatus_t flag = irq_save();
    if (s->enable_setup_done) {
        gpio_out_write(s->enable_gpio, !s->config.enable_active_high ? 1u : 0u);
    }
    if (s->stepper) {
        stepper_local_stop(s->stepper);
    }
    irq_restore(flag);
}

static km_fault_t local_get_fault(const km_driver_t *d)
{
    km_stepdir_slot_t *s = (km_stepdir_slot_t *)d->user_data;
    if (!s) return KM_FAULT_NONE;
    if (s->latched_fault != KM_FAULT_NONE) return s->latched_fault;
    if (s->user_fault) {
        km_fault_t f = s->user_fault(s->user_context);
        if (f != KM_FAULT_NONE) return f;
    }
    return KM_FAULT_NONE;
}

static km_result_t local_clear_fault(km_driver_t *d)
{
    km_stepdir_slot_t *s = (km_stepdir_slot_t *)d->user_data;
    if (!s) return KM_ERR_INVALID_PARAM;
    /* Clearing the latch after shutdown would let a held pointer re-enable
     * the outputs; the old portable driver's io.ready() refused here too. */
    if (global_shutdown || sched_is_shutdown()) return KM_ERR_UNAVAILABLE;
    /* The portable driver refused to clear a fault while an output was
     * active, while the driver was unready, or while its fault source still
     * reported one. Without these gates a caller could resume onto a
     * still-moving or still-faulted driver. */
    if (d->enabled || d->step_pin_high) return KM_ERR_BUSY;
    if (s->user_ready && !s->user_ready(s->user_context)) return KM_ERR_UNAVAILABLE;
    if (s->user_fault && s->user_fault(s->user_context) != KM_FAULT_NONE)
        return KM_ERR_FAULTED;
    s->latched_fault = KM_FAULT_NONE;
    if (s->stepper) {
        s->last_position32 = stepper_local_position(s->stepper);
    }
    return KM_OK;
}


/* STEP/DIR is a local stepper now (stepper_local_*, driven only from the
 * controller's rate tick): these ops exist so the driver interface stays
 * complete for callers that check for their presence, and so that a caller
 * still going through the generic km_driver_t API (not stepper_local) keeps
 * the same readiness-loss-while-enabled safety property step_dir_driver.c
 * had: it faults and disables
 * pre-call fault check on the next call. Actual stepper_local motion never
 * reaches these. */
static km_result_t dummy_set_direction(km_driver_t *d, km_direction_t dir, uint32_t now_us)
{
    (void)d; (void)dir; (void)now_us;
    return KM_OK;
}

static const km_driver_ops_t local_ops = {
    .set_enable = locked_enable,
    .stop_outputs = locked_stop,
    .get_fault = local_get_fault,
    .clear_fault = local_clear_fault,
    .set_direction = dummy_set_direction
};


KM_API void km_stepdir_gpio_force_off(void *context)
{
    km_stepdir_slot_t *slot = (km_stepdir_slot_t *)context;
    if (!slot || !slot->claimed || !slot->enable_setup_done)
        return;
    /* Raw ENABLE only: an active high STEP remains logically high/uncertain.
     * The next normal driver service observes the latched fault and stops it. */
    gpio_out_write(slot->enable_gpio, !slot->config.enable_active_high ? 1u : 0u);
    if (slot->stepper) {
        stepper_local_stop(slot->stepper);
    }
    if (slot->activation_attempted && !slot->is_published)
        slot->raw_off_during_activation = true;
    if (slot->is_published && slot->latched_fault == KM_FAULT_NONE)
        slot->latched_fault = KM_FAULT_DRIVER_FAULT;
}

void km_stepdir_gpio_shutdown(void)
{
    irqstatus_t flag = irq_save();
    global_shutdown = true;
    for (uint32_t i = 0u; i < KM_MAX_LANES; ++i) {
        km_stepdir_slot_t *slot = &slots[i];
        if (!slot->claimed) continue;
        if (slot->enable_setup_done) {
            gpio_out_write(slot->enable_gpio, !slot->config.enable_active_high ? 1u : 0u);
        }
        if (slot->stepper) stepper_local_stop(slot->stepper);
        if (slot->step_setup_done) {
            gpio_out_write(slot->step_gpio, !slot->config.step_active_high ? 1u : 0u);
        }
        slot->driver.enabled = false;
        slot->driver.step_pin_high = false;
        slot->driver.pwm_duty_permille = 0u;
        if (slot->latched_fault == KM_FAULT_NONE) {
            slot->latched_fault = KM_FAULT_DRIVER_FAULT;
        }
    }
    irq_restore(flag);
}
DECL_SHUTDOWN(km_stepdir_gpio_shutdown);

km_result_t km_stepdir_gpio_prepare(uint32_t slot,
                                         uint32_t step_pin,
                                         uint32_t dir_pin,
                                         uint32_t enable_pin,
                                         const km_step_dir_config_t *config,
                                         bool (*ready)(void *context),
                                         km_fault_t (*fault)(void *context),
                                         void *context,
                                         void **out_force_off_context)
{
    /* 1. Global shutdown / sched shutdown check */
    if (global_shutdown || sched_is_shutdown()) {
        return KM_ERR_FAULTED;
    }

    /* 2. IRQ check: must be called with interrupts enabled from ready task context */
    irqstatus_t flag = irq_save();
    irq_restore(flag);
    if (flag != 0ul) {
        return KM_ERR_INVALID_PARAM;
    }

    /* 3. Clock check: scheduler clock must be READY */
    enum km_clock_state clk_st = km_clock_get_state();
    if (clk_st == KM_CLOCK_STATE_UNAVAILABLE) {
        return KM_ERR_UNAVAILABLE;
    }
    if (clk_st != KM_CLOCK_STATE_READY) {
        return KM_ERR_FAULTED;
    }

    /* 4. Slot index and pointer validation */
    if (slot >= KM_MAX_LANES || !config || !ready || !out_force_off_context) {
        return KM_ERR_INVALID_PARAM;
    }

    /* 5. Pin equality check within the slot */
    if (step_pin == dir_pin || step_pin == enable_pin || dir_pin == enable_pin) {
        return KM_ERR_INVALID_PARAM;
    }

    /* 6. Portable config validation before claiming slot or touching any GPIO */
    if (km_step_dir_config_validate(config) != KM_OK) {
        return KM_ERR_INVALID_PARAM;
    }

    /* 7. Check if this slot is already claimed */
    if (slots[slot].claimed) {
        return KM_ERR_BUSY;
    }

    /* 8. Check pin collisions with all claimed slots (including partial/failed setups) */
    for (uint32_t i = 0u; i < KM_MAX_LANES; ++i) {
        if (!slots[i].claimed)
            continue;
        if (step_pin == slots[i].step_pin || step_pin == slots[i].dir_pin || step_pin == slots[i].enable_pin ||
            dir_pin == slots[i].step_pin || dir_pin == slots[i].dir_pin || dir_pin == slots[i].enable_pin ||
            enable_pin == slots[i].step_pin || enable_pin == slots[i].dir_pin || enable_pin == slots[i].enable_pin) {
            return KM_ERR_CONFLICT;
        }
    }

#if CONFIG_WANT_GPIO_LOCAL
    /* Prior local claims only. A miss leaves the slot and output context unused. */
    if (!gpio_pin_is_local(step_pin) || !gpio_pin_is_local(dir_pin)
        || !gpio_pin_is_local(enable_pin))
        return KM_ERR_CONFLICT;
#endif

    /* 9. Claim slot and pins BEFORE touching GPIO: immutable once setup begins */
    km_stepdir_slot_t *s = &slots[slot];
    s->claimed = true;
    s->step_pin = step_pin;
    s->dir_pin = dir_pin;
    s->enable_pin = enable_pin;
    s->config = *config;
    s->user_ready = ready;
    s->user_fault = fault;
    s->user_context = context;
    s->enable_setup_done = false;
    s->step_setup_done = false;
    s->dir_setup_done = false;
    s->prepared = false;
    s->activation_attempted = false;
    s->raw_off_during_activation = false;
    s->is_published = false;
    s->latched_fault = KM_FAULT_NONE;
    s->driver.ops = &local_ops;
    s->driver.user_data = s;
    s->driver.caps.supports_step_dir = true;
    s->driver.initialized = true;
    s->driver.caps.max_velocity_steps_s = config->maximum_velocity_steps_s;
    s->driver.caps.min_pulse_width_us = config->pulse_high_us;
    s->driver.caps.min_step_interval_us = config->pulse_high_us + config->pulse_low_us;

    /* 10. Physical setup in strict required order:
     *     a. Inactive ENABLE first
     *     b. Inactive STEP next
     *     c. Initial direction
     *     Track partial setup after each call so failure leaves touched pins disabled.
     */
    uint32_t val_enable = !s->config.enable_active_high ? 1u : 0u;
#if CONFIG_WANT_GPIO_LOCAL
    s->enable_gpio = gpio_out_setup_local(enable_pin, val_enable);
#else
    s->enable_gpio = gpio_out_setup(enable_pin, val_enable);
#endif
    s->enable_setup_done = true;
    if (global_shutdown || sched_is_shutdown()) {
        return KM_ERR_FAULTED;
    }

    uint32_t val_step = !s->config.step_active_high ? 1u : 0u;
#if CONFIG_WANT_GPIO_LOCAL
    s->step_gpio = gpio_out_setup_local(step_pin, val_step);
#else
    s->step_gpio = gpio_out_setup(step_pin, val_step);
#endif
    s->step_setup_done = true;
    if (global_shutdown || sched_is_shutdown()) {
        return KM_ERR_FAULTED;
    }

    /* Low: Klipper's stepper.c assumes DIR starts low (direction bit 0) and
     * only toggles it on a direction change; km_stepdir_gpio_queue maps the
     * lane's forward onto that bit. */
    uint32_t val_dir = 0u;
#if CONFIG_WANT_GPIO_LOCAL
    s->dir_gpio = gpio_out_setup_local(dir_pin, val_dir);
#else
    s->dir_gpio = gpio_out_setup(dir_pin, val_dir);
#endif
    s->dir_setup_done = true;
    if (global_shutdown || sched_is_shutdown()) {
        return KM_ERR_FAULTED;
    }

    /* Preparation stops here: no ready callback, driver publication or enable. */
    s->prepared = true;
    *out_force_off_context = s;
    return KM_OK;
}

#if CONFIG_STEPPER_LOCAL_COUNT < CONFIG_KLIPPER_MMU_LANE_COUNT
#error "one local stepper per lane: raise CONFIG_STEPPER_LOCAL_COUNT"
#endif
_Static_assert(CONFIG_STEPPER_LOCAL_MOVES >= 4 * KM_STEP_SEGMENTS_MAX,
               "two windows in flight plus two queued, as the controller assumes");

KM_API km_result_t km_stepdir_gpio_activate(uint32_t slot)
{
    if (global_shutdown || sched_is_shutdown()) return KM_ERR_FAULTED;
    irqstatus_t flag = irq_save();
    irq_restore(flag);
    if (flag != 0ul) return KM_ERR_INVALID_PARAM;
    if (km_clock_get_state() != KM_CLOCK_STATE_READY) return KM_ERR_UNAVAILABLE;
    if (slot >= KM_MAX_LANES) return KM_ERR_INVALID_PARAM;
    km_stepdir_slot_t *s = &slots[slot];
    if (!s->claimed || !s->prepared) return KM_ERR_UNAVAILABLE;
    if (s->is_published || s->activation_attempted) return KM_ERR_BUSY;
    if (s->user_fault && s->user_fault(s->user_context) != KM_FAULT_NONE)
        return KM_ERR_FAULTED;
    if (!s->user_ready(s->user_context)) return KM_ERR_UNAVAILABLE;


    s->activation_attempted = true;
    if (!s->stepper) {
        uint32_t ticks = timer_from_us(s->config.pulse_high_us);
        /* invert_step > 0 makes high the unstep level, which stepper.c also
         * restores on a stop; an active-low STEP lane needs it. */
        s->stepper = stepper_local_setup(s->step_gpio, s->dir_gpio,
                                         s->config.step_active_high ? 0 : 1, ticks,
                                         4 * KM_STEP_SEGMENTS_MAX);
        s->last_position32 = stepper_local_position(s->stepper);
        s->position = 0;
    }

    /* Publish under the same IRQ serialization as UART callbacks: a safe-off
     * cannot fall between the final readiness check and publication. */
    flag = irq_save();
    bool publish = !s->raw_off_during_activation && !global_shutdown && !sched_is_shutdown()
        && (!s->user_fault || s->user_fault(s->user_context) == KM_FAULT_NONE)
        && s->user_ready(s->user_context);
    if (s->raw_off_during_activation) publish = false;
    if (publish) s->is_published = true;
    irq_restore(flag);
    if (!publish) {
        km_stepdir_gpio_force_off(s);
        return KM_ERR_FAULTED;
    }
    return KM_OK;
}

KM_API km_result_t km_stepdir_gpio_setup(uint32_t slot,
                                         uint32_t step_pin,
                                         uint32_t dir_pin,
                                         uint32_t enable_pin,
                                         const km_step_dir_config_t *config,
                                         bool (*ready)(void *context),
                                         km_fault_t (*fault)(void *context),
                                         void *context)
{
    void *force_off_context = NULL;
    km_result_t res = km_stepdir_gpio_prepare(slot, step_pin, dir_pin, enable_pin,
                                               config, ready, fault, context,
                                               &force_off_context);
    if (res != KM_OK) return res;
    return km_stepdir_gpio_activate(slot);
}

KM_API km_result_t km_stepdir_gpio_clear_fault(uint32_t slot)
{
    if (slot >= KM_MAX_LANES) return KM_ERR_INVALID_PARAM;
    /* Same serialization as raw safe-off: a UART timer latch lands either
     * before the portable readiness/fault checks or after the clear. */
    irqstatus_t flag = irq_save();
    km_result_t res = global_shutdown || sched_is_shutdown() ? KM_ERR_FAULTED
        : !slots[slot].is_published ? KM_ERR_UNAVAILABLE
        : local_clear_fault(&slots[slot].driver);
    irq_restore(flag);
    return res;
}

KM_API km_driver_t *km_stepdir_gpio_get_driver(uint32_t slot)
{
    if (slot >= KM_MAX_LANES)
        return NULL;
    if (global_shutdown || sched_is_shutdown())
        return NULL;
    if (!slots[slot].claimed || !slots[slot].is_published)
        return NULL;
    return &slots[slot].driver;
}

KM_API bool km_stepdir_gpio_is_slot_active(uint32_t slot)
{
    if (slot >= KM_MAX_LANES)
        return false;
    if (global_shutdown || sched_is_shutdown())
        return false;
    return slots[slot].claimed && slots[slot].is_published;
}

KM_API bool km_stepdir_gpio_is_shutdown(void)
{
    return global_shutdown || sched_is_shutdown();
}

KM_API km_result_t km_stepdir_gpio_get_slot_pins(uint32_t slot,
                                                 uint32_t *out_step_pin,
                                                 uint32_t *out_dir_pin,
                                                 uint32_t *out_enable_pin)
{
    if (slot >= KM_MAX_LANES || !out_step_pin || !out_dir_pin || !out_enable_pin)
        return KM_ERR_INVALID_PARAM;
    if (!slots[slot].claimed)
        return KM_ERR_UNAVAILABLE;
    *out_step_pin = slots[slot].step_pin;
    *out_dir_pin = slots[slot].dir_pin;
    *out_enable_pin = slots[slot].enable_pin;
    return KM_OK;
}

KM_API struct stepper *km_stepdir_gpio_get_stepper_for_driver(const km_driver_t *d)
{
    if (!d || d->ops != &local_ops) return NULL;
    km_stepdir_slot_t *s = (km_stepdir_slot_t *)d->user_data;
    if (!s || !s->claimed || !s->is_published) return NULL;
    return s->stepper;
}

/* Klipper's stepper.c drives DIR high for dir 1 and returns it low on a stop;
 * like Klipper's host, the add-on applies the lane's polarity, so 'forward'
 * here is the lane's forward and positions count forward steps. */
KM_API int km_stepdir_gpio_queue(const km_driver_t *d, uint32_t start_clock,
                                 uint32_t interval, uint16_t count, int16_t add,
                                 uint8_t forward)
{
    struct stepper *st = km_stepdir_gpio_get_stepper_for_driver(d);
    if (!st) return -1;
    km_stepdir_slot_t *s = (km_stepdir_slot_t *)d->user_data;
    return stepper_local_queue(st, start_clock, interval, count, add,
                               (uint8_t)(!forward == !s->config.forward_high));
}
KM_API int64_t km_stepdir_gpio_get_position_for_driver(const km_driver_t *d)
{
    if (!d || d->ops != &local_ops) return 0;
    km_stepdir_slot_t *s = (km_stepdir_slot_t *)d->user_data;
    if (!s || !s->claimed || !s->stepper) return 0;
    irqstatus_t flag = irq_save();
    int32_t pos32 = stepper_local_position(s->stepper);
    /* Unsigned difference: the 32-bit count wraps, and a signed subtraction
     * across the wrap overflows. */
    int32_t delta = (int32_t)((uint32_t)pos32 - (uint32_t)s->last_position32);
    s->position += s->config.forward_high ? delta : -delta;
    s->last_position32 = pos32;
    int64_t res = s->position;
    irq_restore(flag);
    return res;
}
