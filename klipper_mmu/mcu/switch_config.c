// SPDX-License-Identifier: GPL-3.0-or-later
#include "switch_config.h"
#include "pin_config.h"
#include "autoconf.h"

/* Saved headers from before FPS arrival always have a toolhead switch. */
#ifndef CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR
#define CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR 1
#endif
/* Saved headers from before the selector add-on have neither. */
#ifndef CONFIG_KLIPPER_MMU_SELECTOR_STEPPER
#define CONFIG_KLIPPER_MMU_SELECTOR_STEPPER 0
#endif
#ifndef CONFIG_KLIPPER_MMU_SELECTOR_ENDSTOP_ACTIVE_HIGH
#define CONFIG_KLIPPER_MMU_SELECTOR_ENDSTOP_ACTIVE_HIGH (-1)
#endif
#ifndef CONFIG_KLIPPER_MMU_SELECTOR_ENDSTOP_PULL
#define CONFIG_KLIPPER_MMU_SELECTOR_ENDSTOP_PULL (-2)
#endif
/* Saved headers from before a tension-only buffer (single switch) existed
 * always had a second (compression) buffer pin. */
#ifndef CONFIG_KLIPPER_MMU_BUFFER_SINGLE_SWITCH
#define CONFIG_KLIPPER_MMU_BUFFER_SINGLE_SWITCH 0
#endif

#if CONFIG_KLIPPER_MMU_PIN_CONFIG
/* Exact canonical list item; signed values are used only for pull-down (-1). */
static bool item(const char **cursor, bool allow_negative, bool last, int8_t *out)
{
    const char *s = *cursor;
    int8_t value;
    if (allow_negative && s[0] == '-' && s[1] == '1') {
        value = -1;
        s += 2;
    } else if (s[0] == '0' || s[0] == '1') {
        value = (int8_t)(*s++ - '0');
    } else {
        return false;
    }
    if (last ? *s != '\0' : *s != ',') return false;
    *cursor = last ? s : s + 1;
    *out = value;
    return true;
}

static km_result_t group(km_switch_input_t *out, const uint16_t *pins,
                          unsigned count, const char *levels, const char *pulls)
{
    if (!*levels || !*pulls) return KM_ERR_UNAVAILABLE;
    for (unsigned i = 0; i < count; ++i) {
        int8_t high, pull;
        if (!item(&levels, false, i + 1u == count, &high)
            || !item(&pulls, true, i + 1u == count, &pull))
            return KM_ERR_INVALID_PARAM;
        out[i] = (km_switch_input_t){pins[i], pull, high != 0};
    }
    return KM_OK;
}
#endif

km_result_t km_config_switches(km_switch_gpio_config_t *out)
{
    if (!out) return KM_ERR_INVALID_PARAM;
#if !CONFIG_KLIPPER_MMU_PIN_CONFIG
    return KM_ERR_UNAVAILABLE;
#else
    const km_pin_config_t *pins;
    km_result_t result = km_config_pins_view(&pins, NULL);
    if (result != KM_OK) return result;
#if CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR
    if (CONFIG_KLIPPER_MMU_TOOLHEAD_ACTIVE_HIGH < 0 || CONFIG_KLIPPER_MMU_TOOLHEAD_PULL < -1)
        return KM_ERR_UNAVAILABLE;
#endif
#if !CONFIG_KLIPPER_MMU_VIRTUAL_HUB
    if (CONFIG_KLIPPER_MMU_HUB_ACTIVE_HIGH < 0 || CONFIG_KLIPPER_MMU_HUB_PULL < -1)
        return KM_ERR_UNAVAILABLE;
#endif
#if CONFIG_KLIPPER_MMU_SELECTOR_HAS_ENDSTOP
    if (CONFIG_KLIPPER_MMU_SELECTOR_ENDSTOP_ACTIVE_HIGH < 0
        || CONFIG_KLIPPER_MMU_SELECTOR_ENDSTOP_PULL < -1)
        return KM_ERR_UNAVAILABLE;
#endif
    km_switch_gpio_config_t candidate = {0};
    for (unsigned i = 0; i < KM_MAX_LANES; ++i) candidate.bay[i].pin = KM_PIN_UNUSED;
    for (unsigned i = 0; i < KM_MAX_LANES; ++i) candidate.load[i].pin = KM_PIN_UNUSED;
    for (unsigned i = 0; i < KM_MAX_LANES; ++i) candidate.selector_gate[i].pin = KM_PIN_UNUSED;
    candidate.buffer_low.pin = candidate.buffer_high.pin = KM_PIN_UNUSED;
#if CONFIG_KLIPPER_MMU_BAY_SENSORS
    result = group(candidate.bay, pins->pin[KM_PIN_BAY], CONFIG_KLIPPER_MMU_LANE_COUNT,
                   CONFIG_KLIPPER_MMU_BAY_ACTIVE_HIGH, CONFIG_KLIPPER_MMU_BAY_PULL);
    if (result != KM_OK) return result;
#endif
#if CONFIG_KLIPPER_MMU_SELECTOR_KIND_INDEXED
    result = group(candidate.selector_gate, pins->pin[KM_PIN_SELECTOR_GATE],
                   CONFIG_KLIPPER_MMU_LANE_COUNT, CONFIG_KLIPPER_MMU_SELECTOR_GATE_ACTIVE_HIGH,
                   CONFIG_KLIPPER_MMU_SELECTOR_GATE_PULL);
    if (result != KM_OK) return result;
#endif
#if CONFIG_KLIPPER_MMU_LOAD_SENSORS || CONFIG_KLIPPER_MMU_DEVICE_EMU
    result = group(candidate.load, pins->pin[KM_PIN_LOAD], CONFIG_KLIPPER_MMU_LANE_COUNT,
                   CONFIG_KLIPPER_MMU_LOAD_ACTIVE_HIGH, CONFIG_KLIPPER_MMU_LOAD_PULL);
    if (result != KM_OK) return result;
#endif
#if CONFIG_KLIPPER_MMU_VIRTUAL_HUB
    candidate.hub.pin = KM_SWITCH_PIN_UNUSED;
#else
    candidate.hub = (km_switch_input_t){pins->pin[KM_PIN_HUB][0],
        CONFIG_KLIPPER_MMU_HUB_PULL, CONFIG_KLIPPER_MMU_HUB_ACTIVE_HIGH != 0};
#endif
#if CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR
    candidate.toolhead = (km_switch_input_t){pins->pin[KM_PIN_TOOLHEAD][0],
        CONFIG_KLIPPER_MMU_TOOLHEAD_PULL, CONFIG_KLIPPER_MMU_TOOLHEAD_ACTIVE_HIGH != 0};
#else
    candidate.toolhead.pin = KM_SWITCH_PIN_UNUSED;
#endif
#if CONFIG_KLIPPER_MMU_SELECTOR_HAS_ENDSTOP
    candidate.selector_endstop = (km_switch_input_t){pins->pin[KM_PIN_SELECTOR_ENDSTOP][0],
        CONFIG_KLIPPER_MMU_SELECTOR_ENDSTOP_PULL,
        CONFIG_KLIPPER_MMU_SELECTOR_ENDSTOP_ACTIVE_HIGH != 0};
#else
    candidate.selector_endstop.pin = KM_SWITCH_PIN_UNUSED;
#endif
#if !CONFIG_KLIPPER_MMU_DIFF_BUFFER
#if CONFIG_KLIPPER_MMU_BUFFER_SINGLE_SWITCH
    /* Tension-only buffer (docs/devices/qidi_box.md): the sole physical
     * switch is always the low/tension side (KLIPPER_MMU_BUFFER_LOW_A/B has
     * no meaning with only one pin). buffer_high stays KM_SWITCH_PIN_UNUSED;
     * mcu/switch_gpio.c never claims or samples it, and src/buffer_sensor.c
     * is told its state is always inactive rather than guessing one. */
    uint16_t single_pin[] = {pins->pin[KM_PIN_BUFFER_A][0]};
    km_switch_input_t single[1];
    result = group(single, single_pin, 1, CONFIG_KLIPPER_MMU_BUFFER_ACTIVE_HIGH,
                   CONFIG_KLIPPER_MMU_BUFFER_PULL);
    if (result != KM_OK) return result;
    candidate.buffer_low = single[0];
    candidate.buffer_high.pin = KM_SWITCH_PIN_UNUSED;
#else
#if !CONFIG_KLIPPER_MMU_BUFFER_LOW_A && !CONFIG_KLIPPER_MMU_BUFFER_LOW_B
    return KM_ERR_UNAVAILABLE;
#endif
    uint16_t buffer_pins[] = {pins->pin[KM_PIN_BUFFER_A][0], pins->pin[KM_PIN_BUFFER_B][0]};
    km_switch_input_t buffer[2];
    result = group(buffer, buffer_pins, 2, CONFIG_KLIPPER_MMU_BUFFER_ACTIVE_HIGH,
                   CONFIG_KLIPPER_MMU_BUFFER_PULL);
    if (result != KM_OK) return result;
#if CONFIG_KLIPPER_MMU_BUFFER_LOW_A
    candidate.buffer_low = buffer[0]; candidate.buffer_high = buffer[1];
#else
    candidate.buffer_low = buffer[1]; candidate.buffer_high = buffer[0];
#endif
#endif
#endif
    *out = candidate;
    return KM_OK;
#endif
}
