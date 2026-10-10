// SPDX-License-Identifier: GPL-3.0-or-later
#include "switch_gpio.h"
#include "controller.h"
#include "clock.h"
#include "autoconf.h"
#if CONFIG_WANT_GPIO_LOCAL
#include "stm32/gpio_local.h"
#endif
#include "board/gpio.h"
#include "board/irq.h"
#include "command.h"
#include "sched.h"

#ifndef CONFIG_KLIPPER_MMU_SELECTOR_KIND_INDEXED
#define CONFIG_KLIPPER_MMU_SELECTOR_KIND_INDEXED 0
#endif
/* BAY and LOAD by lane, HUB, toolhead, selector endstop, buffer low/high,
 * and the INDEXED selector's gate switches by lane only in an INDEXED image
 * (32 KB parts have no room for them otherwise). */
#define KM_SWITCH_INPUTS_MAX \
    (2u * KM_MAX_LANES + 5u + (CONFIG_KLIPPER_MMU_SELECTOR_KIND_INDEXED ? KM_MAX_LANES : 0u))

static km_operation_core_t *bound_core;
static km_switch_input_t inputs[KM_SWITCH_INPUTS_MAX];
static struct gpio_in pins[KM_SWITCH_INPUTS_MAX];
typedef struct {
    uint32_t last_sample_us, candidate_since_us;
    bool observed, stable_valid, stable_level, candidate_level, pending;
} km_switch_debounce_t;
static km_switch_debounce_t debounce[KM_SWITCH_INPUTS_MAX];
/* Diagnostic only (docs/diagnostics.md): a candidate raw level change
 * rejected before KM_SWITCH_DEBOUNCE_US qualified it, per configured input.
 * Saturating; never read by debounce or control logic. */
static uint16_t bounce[KM_SWITCH_INPUTS_MAX];
static unsigned lane_count, input_count;
static bool switched, load_sensors, bay_sensors, virtual_hub, toolhead_sensor, ready, stopped;
static bool has_selector, has_selector_gate;
/* Whether a switched buffer's second (compression) physical switch exists.
 * False for a tension-only buffer (docs/devices/qidi_box.md): the missing
 * switch is then reported inactive, never fabricated (km_switch_gpio_sample
 * below). */
static bool buffer_has_high;
static const bool no_bay[KM_MAX_LANES];

/* Setup order: [bays], [HUB], [toolhead], [selector endstop], [buffer low,
 * high], [LOAD by lane], [selector gate by lane]. BAY occupies no slots at
 * all when the profile has no per-lane BAY switch, and selector gate none at
 * all outside the INDEXED kind (klipper_mmu/selector.h). A tension-only
 * buffer (buffer_has_high false) occupies only the low slot; there is no
 * high slot to skip past. Shared by the sample loop and the bounce-counter
 * accessors. */
static void layout(unsigned *hub_index, unsigned *toolhead_index,
                   unsigned *selector_index, unsigned *buffer_index,
                   unsigned *load_index, unsigned *gate_index)
{
    unsigned bay_count = bay_sensors ? lane_count : 0u;
    *hub_index = bay_count;
    *toolhead_index = bay_count + (virtual_hub ? 0u : 1u);
    unsigned presence_count = *toolhead_index + (toolhead_sensor ? 1u : 0u);
    *selector_index = presence_count;
    *buffer_index = *selector_index + (has_selector ? 1u : 0u);
    *load_index = *buffer_index + (switched ? (buffer_has_high ? 2u : 1u) : 0u);
    *gate_index = *load_index + (load_sensors ? lane_count : 0u);
}

static void invalidate(void)
{
    if (!bound_core) return;
    for (unsigned i = 0; i < lane_count; ++i) bound_core->bay_valid[i] = false;
    if (load_sensors)
        for (unsigned i = 0; i < lane_count; ++i) bound_core->load_valid[i] = false;
    bound_core->bay_sensors_valid = false;
    bound_core->hub_sensor_valid = false;
    bound_core->toolhead_sensor_valid = false;
    if (switched) {
        bound_core->buffer_sensor.is_valid = false;
        bound_core->buffer_sensor.fault = KM_FAULT_BUFFER_SIGNAL;
    }
}

void km_switch_gpio_shutdown(void)
{
    irqstatus_t flag = irq_save();
    stopped = true;
    ready = false;
    invalidate();
    irq_restore(flag);
}
DECL_SHUTDOWN(km_switch_gpio_shutdown);

/* LINEAR and ROTARY selectors home on their endstop; SERVO has no axis and
 * INDEXED homes on gate 0's own index switch (klipper_mmu/selector.h). */
static bool selector_endstop_used(const km_operation_core_t *core)
{
    return core->profile.has_selector && (core->profile.selector.kind == KM_SELECTOR_LINEAR
                                          || core->profile.selector.kind == KM_SELECTOR_ROTARY);
}

km_result_t km_switch_gpio_setup(km_operation_core_t *core,
                                 const km_switch_gpio_config_t *config)
{
    if (stopped || sched_is_shutdown()) return KM_ERR_FAULTED;
    irqstatus_t flag = irq_save();
    irq_restore(flag);
    if (flag || !core || !config || km_profile_validate(&core->profile) != KM_OK)
        return KM_ERR_INVALID_PARAM;
    if (bound_core) return KM_ERR_BUSY;
    if (core->op_state != KM_OP_STATE_IDLE || core->stage != KM_STAGE_STOPPED
        || core->shared_path_owned || km_trajectory_is_moving(&core->trajectory))
        return KM_ERR_BUSY;
    if (km_clock_get_state() != KM_CLOCK_STATE_READY) return KM_ERR_UNAVAILABLE;
    bool use_switches = core->buffer_sensor.type == KM_BUFFER_SENSOR_SWITCHED;
    if (core->buffer_sensor.type != (core->profile.has_proportional_buffer
            ? KM_BUFFER_SENSOR_ANALOG_DIFF : KM_BUFFER_SENSOR_SWITCHED))
        return KM_ERR_INVALID_PARAM;
    unsigned lanes = core->profile.lane_count;
    bool use_virtual_hub = core->profile.load_is_virtual_hub;
    if (use_virtual_hub && config->hub.pin != KM_SWITCH_PIN_UNUSED)
        return KM_ERR_INVALID_PARAM;
    /* Only FPS arrival may omit the toolhead switch, which then stays unassigned. */
    bool use_toolhead = core->profile.has_toolhead_sensor;
    if (!use_toolhead && (!core->profile.fps_arrival
                          || config->toolhead.pin != KM_SWITCH_PIN_UNUSED))
        return KM_ERR_INVALID_PARAM;
    bool use_bay = core->profile.has_bay_sensors;
    bool use_selector = selector_endstop_used(core);
    bool use_gate = core->profile.has_selector
        && core->profile.selector.kind == KM_SELECTOR_INDEXED;
    if (use_gate && !CONFIG_KLIPPER_MMU_SELECTOR_KIND_INDEXED)
        return KM_ERR_UNAVAILABLE; /* This image has no room for gate switches. */
    km_switch_input_t candidate[KM_SWITCH_INPUTS_MAX];
    unsigned count = 0;
    if (use_bay)
        for (unsigned i = 0; i < lanes; ++i) candidate[count++] = config->bay[i];
    if (!use_virtual_hub) candidate[count++] = config->hub;
    if (use_toolhead) candidate[count++] = config->toolhead;
    if (use_selector) candidate[count++] = config->selector_endstop;
    /* A tension-only buffer (docs/devices/qidi_box.md) has no physical
     * compression switch: buffer_high stays unclaimed, and its state is
     * never sampled or fabricated below. */
    bool use_buffer_high = use_switches && config->buffer_high.pin != KM_SWITCH_PIN_UNUSED;
    if (use_switches) {
        candidate[count++] = config->buffer_low;
        if (use_buffer_high) candidate[count++] = config->buffer_high;
    }
    bool use_load = core->profile.has_load_sensors;
    if (use_load)
        for (unsigned i = 0; i < lanes; ++i) candidate[count++] = config->load[i];
    if (use_gate)
        for (unsigned i = 0; i < lanes; ++i) candidate[count++] = config->selector_gate[i];
    for (unsigned i = 0; i < count; ++i) {
        if (candidate[i].pin >= 128u || candidate[i].pull < -1 || candidate[i].pull > 1)
            return KM_ERR_INVALID_PARAM;
        for (unsigned j = 0; j < i; ++j)
            if (candidate[i].pin == candidate[j].pin) return KM_ERR_CONFLICT;
    }
#if CONFIG_WANT_GPIO_LOCAL
    /* Every pin this mode uses must already be local. No self-reservation. */
    for (unsigned i = 0; i < count; ++i)
        if (!gpio_pin_is_local(candidate[i].pin))
            return KM_ERR_CONFLICT;
#endif
    /* Publish the claim before upstream setup, which may invoke shutdown. */
    flag = irq_save();
    if (stopped || sched_is_shutdown()) {
        irq_restore(flag);
        return KM_ERR_FAULTED;
    }
    bound_core = core;
    lane_count = lanes;
    input_count = count;
    switched = use_switches;
    load_sensors = use_load;
    bay_sensors = use_bay;
    virtual_hub = use_virtual_hub;
    toolhead_sensor = use_toolhead;
    has_selector = use_selector;
    has_selector_gate = use_gate;
    buffer_has_high = use_buffer_high;
    for (unsigned i = 0; i < count; ++i) inputs[i] = candidate[i];
    for (unsigned i = 0; i < count; ++i) debounce[i] = (km_switch_debounce_t){0};
    for (unsigned i = 0; i < count; ++i) bounce[i] = 0;
    invalidate();
    irq_restore(flag);
    for (unsigned i = 0; i < count; ++i) {
        if (stopped || sched_is_shutdown()) return KM_ERR_FAULTED;
#if CONFIG_WANT_GPIO_LOCAL
        pins[i] = gpio_in_setup_local(inputs[i].pin, inputs[i].pull);
#else
        pins[i] = gpio_in_setup(inputs[i].pin, inputs[i].pull);
#endif
    }
    flag = irq_save();
    km_result_t result = KM_ERR_FAULTED;
    if (!stopped && !sched_is_shutdown()) {
        ready = true;
        result = KM_OK;
    }
    irq_restore(flag);
    return result;
}

/* Control task context: no interrupt touches the debounce state or the core. */
void km_switch_gpio_sample(km_operation_core_t *core, void *context)
{
    if (!core || core != bound_core || context)
        shutdown("klipper_mmu: invalid switch binding");
    if (!ready || stopped || sched_is_shutdown()) {
        invalidate();
        return;
    }
    if (core->profile.lane_count != lane_count
        || core->profile.has_load_sensors != load_sensors
        || core->profile.has_bay_sensors != bay_sensors
        || core->profile.load_is_virtual_hub != virtual_hub
        || core->profile.has_toolhead_sensor != toolhead_sensor
        || selector_endstop_used(core) != has_selector
        || (core->profile.has_selector && core->profile.selector.kind == KM_SELECTOR_INDEXED) != has_selector_gate
        || core->buffer_sensor.type != (switched ? KM_BUFFER_SENSOR_SWITCHED : KM_BUFFER_SENSOR_ANALOG_DIFF))
        shutdown("klipper_mmu: changed switch binding");
    km_core_timepoint_t acquisition;
    if (km_clock_get_time(&acquisition) != KM_OK)
        shutdown("klipper_mmu: switch clock unavailable");
    bool levels[KM_SWITCH_INPUTS_MAX];
    bool valid[KM_SWITCH_INPUTS_MAX];
    bool all_valid = true;
    unsigned hub_index, toolhead_index, selector_index, buffer_index, load_index, gate_index;
    layout(&hub_index, &toolhead_index, &selector_index, &buffer_index, &load_index, &gate_index);
    unsigned presence_count = toolhead_index + (toolhead_sensor ? 1u : 0u);
    bool presence_valid = true;
    for (unsigned i = 0; i < input_count; ++i) {
        km_switch_debounce_t *d = &debounce[i];
        bool raw = (gpio_in_read(pins[i]) != 0) == inputs[i].active_high;
        uint32_t now = acquisition.us;
        /* A homing switch, not a presence switch (mcu/switch_gpio.h). */
        uint32_t input_debounce_us = (has_selector && i == selector_index)
            ? KM_SWITCH_SELECTOR_ENDSTOP_DEBOUNCE_US : KM_SWITCH_DEBOUNCE_US;
        if (!d->observed
            || (uint32_t)(now - d->last_sample_us) > KM_SWITCH_MAX_SAMPLE_GAP_US) {
            d->stable_valid = false;
            d->pending = true;
            d->candidate_level = raw;
            d->candidate_since_us = now;
            d->observed = true;
        } else if (d->pending) {
            if (d->stable_valid && raw == d->stable_level) {
                /* A brief excursion returned to the qualified state: the
                 * candidate is rejected. */
                d->pending = false;
                if (bounce[i] < UINT16_MAX) ++bounce[i];
            } else if (raw != d->candidate_level) {
                /* The candidate itself is rejected for a newer one. */
                d->candidate_level = raw;
                d->candidate_since_us = now;
                if (bounce[i] < UINT16_MAX) ++bounce[i];
            } else if ((uint32_t)(now - d->candidate_since_us)
                       >= input_debounce_us) {
                d->stable_level = raw;
                d->stable_valid = true;
                d->pending = false;
            }
        } else if (raw != d->stable_level) {
            d->candidate_level = raw;
            d->candidate_since_us = now;
            d->pending = true;
        }
        d->last_sample_us = now;
        levels[i] = d->stable_level;
        valid[i] = d->stable_valid;
        if (!valid[i]) all_valid = false;
        if (i < presence_count && !valid[i]) presence_valid = false;
    }
    if (stopped || sched_is_shutdown()) {
        invalidate();
    } else {
        /* An absent toolhead switch never reports presence. */
        km_result_t published = km_core_set_presence_at(core, bay_sensors ? levels : no_bay,
            virtual_hub ? false : levels[hub_index],
            toolhead_sensor && levels[toolhead_index], presence_valid, acquisition.ms);
        if (has_selector)
            km_core_set_selector_endstop_at(core, levels[selector_index],
                                            valid[selector_index], acquisition.us);
        if (switched) {
            bool high_ready = !buffer_has_high || valid[buffer_index + 1u];
            if (valid[buffer_index] && high_ready) {
                /* No physical compression switch (buffer_has_high false):
                 * report it always inactive, matching Happy Hare's own
                 * degraded single-switch sync-feedback mapping
                 * (extras/mmu/unit/mmu_buffer.py sync_tension_callback,
                 * compression_enabled=False branch) instead of fabricating
                 * a reading for hardware that is not wired. */
                bool switch_high = buffer_has_high && levels[buffer_index + 1u];
                if (km_buffer_update_switches(&core->buffer_sensor,
                    levels[buffer_index], switch_high,
                    acquisition.ms) != KM_OK)
                    published = KM_ERR_FAULTED;
            } else {
                core->buffer_sensor.is_valid = false;
                core->buffer_sensor.fault = KM_FAULT_BUFFER_SIGNAL;
            }
        }
        if (load_sensors)
            for (unsigned i = 0; i < lane_count; ++i)
                km_core_set_load_sensor_at(core, (uint8_t)i, levels[load_index + i],
                                                valid[load_index + i], acquisition.ms);
        if (has_selector_gate)
            for (unsigned i = 0; i < lane_count; ++i)
                km_core_set_selector_gate_switch_at(core, (uint8_t)i, levels[gate_index + i],
                                                    valid[gate_index + i], acquisition.us);
        if (published == KM_OK && all_valid && !stopped && !sched_is_shutdown())
            km_controller_note_switch_sample(core, acquisition.us);
    }
}

/* Task-context read-only diagnostic query (docs/diagnostics.md): per-switch
 * rejected-candidate ("bounce") counts since the current binding's setup,
 * saturating. Zero for every input when unbound/not ready, and for an input
 * this binding does not use. bay/load hold KM_MAX_LANES slots; only the
 * first lane_count are meaningful. */
void km_switch_gpio_get_bounce(uint16_t bay[KM_MAX_LANES], uint16_t load[KM_MAX_LANES],
                               uint16_t *hub, uint16_t *toolhead)
{
    for (unsigned i = 0; i < KM_MAX_LANES; ++i) bay[i] = load[i] = 0;
    *hub = *toolhead = 0;
    if (!bound_core || !ready) return;
    if (bay_sensors)
        for (unsigned i = 0; i < lane_count; ++i) bay[i] = bounce[i];
    unsigned hub_index, toolhead_index, selector_index, buffer_index, load_index, gate_index;
    layout(&hub_index, &toolhead_index, &selector_index, &buffer_index, &load_index, &gate_index);
    (void)buffer_index; (void)gate_index;
    if (!virtual_hub) *hub = bounce[hub_index];
    if (toolhead_sensor) *toolhead = bounce[toolhead_index];
    if (load_sensors)
        for (unsigned i = 0; i < lane_count; ++i) load[i] = bounce[load_index + i];
}

/* Diagnostic only (docs/diagnostics.md): the selector home endstop's own
 * bounce count. Zero when unbound, not ready, or the profile has no
 * selector. A separate accessor so the fixed km_switch_gpio_get_bounce
 * signature above stays unchanged for its existing callers. */
void km_switch_gpio_get_selector_bounce(uint16_t *out)
{
    if (!out) return;
    *out = 0;
    if (!bound_core || !ready || !has_selector) return;
    unsigned hub_index, toolhead_index, selector_index, buffer_index, load_index, gate_index;
    layout(&hub_index, &toolhead_index, &selector_index, &buffer_index, &load_index, &gate_index);
    (void)hub_index; (void)toolhead_index; (void)buffer_index; (void)load_index; (void)gate_index;
    *out = bounce[selector_index];
}

/* Diagnostic only (docs/diagnostics.md): each INDEXED gate index switch's
 * own bounce count. Zero for every lane when unbound, not ready, or the
 * profile's selector kind is not INDEXED. */
void km_switch_gpio_get_selector_gate_bounce(uint16_t out[KM_MAX_LANES])
{
    for (unsigned i = 0; i < KM_MAX_LANES; ++i) out[i] = 0;
    if (!bound_core || !ready || !has_selector_gate) return;
    unsigned hub_index, toolhead_index, selector_index, buffer_index, load_index, gate_index;
    layout(&hub_index, &toolhead_index, &selector_index, &buffer_index, &load_index, &gate_index);
    (void)hub_index; (void)toolhead_index; (void)selector_index; (void)buffer_index; (void)load_index;
    for (unsigned i = 0; i < lane_count; ++i) out[i] = bounce[gate_index + i];
}
