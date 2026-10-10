// SPDX-License-Identifier: GPL-3.0-or-later
#include "pin_config.h"
#include "autoconf.h"
#include "command.h"
#include <string.h>

/* Saved headers from before FPS arrival always have a toolhead switch. */
#ifndef CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR
#define CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR 1
#endif
/* Saved headers from before the selector add-on have neither. */
#ifndef CONFIG_KLIPPER_MMU_SELECTOR_STEPPER
#define CONFIG_KLIPPER_MMU_SELECTOR_STEPPER 0
#endif
#ifndef CONFIG_KLIPPER_MMU_SERVO
#define CONFIG_KLIPPER_MMU_SERVO 0
#endif
#ifndef CONFIG_KLIPPER_MMU_PINS_SELECTOR_UART
#define CONFIG_KLIPPER_MMU_PINS_SELECTOR_UART ""
#endif
#ifndef CONFIG_KLIPPER_MMU_SELECTOR_UART_ADDRESS
#define CONFIG_KLIPPER_MMU_SELECTOR_UART_ADDRESS 0
#endif
/* Saved headers from before a tension-only buffer (single switch) existed
 * always had a second (compression) buffer pin. */
#ifndef CONFIG_KLIPPER_MMU_BUFFER_SINGLE_SWITCH
#define CONFIG_KLIPPER_MMU_BUFFER_SINGLE_SWITCH 0
#endif
/* Saved headers from before a gear-without-TMC-UART device (QIDI Box) existed
 * always had a UART-reachable gear driver. */
#ifndef CONFIG_KLIPPER_MMU_GEAR_TMC_UART
#define CONFIG_KLIPPER_MMU_GEAR_TMC_UART 1
#endif

#if CONFIG_KLIPPER_MMU_PIN_CONFIG
#include "stm32/internal.h"
/* Require nonempty lists before publishing reservations. Canonical spelling,
 * exact lane counts and conflicts are additionally checked by the factory. */
#define RESERVE(role, value) \
    _Static_assert(sizeof(value) > 1, "MMU " role " pin assignment required"); \
    DECL_CONSTANT_STR("RESERVE_PINS_klipper_mmu_" role, value)
/* With a selector, the gear behind it is one driver, not one per lane
 * (enforced at runtime in km_config_pins_view, where the actual lane count
 * is known); either way at least one pin must be assigned. */
RESERVE("step", CONFIG_KLIPPER_MMU_PINS_STEP);
RESERVE("dir", CONFIG_KLIPPER_MMU_PINS_DIR);
RESERVE("enable", CONFIG_KLIPPER_MMU_PINS_ENABLE);
/* A gear with no TMC UART chip (drivers set in hardware -- Happy Hare's
 * CHOICE_GEAR_TMC_NONE, e.g. QIDI Box) never claims a UART pin: there is
 * no such signal to invent (KLIPPER_MMU_GEAR_TMC_UART, mcu/Kconfig.control). */
#if CONFIG_KLIPPER_MMU_GEAR_TMC_UART
RESERVE("uart", CONFIG_KLIPPER_MMU_PINS_UART);
#endif
#if CONFIG_KLIPPER_MMU_SELECTOR_STEPPER
RESERVE("selector_step", CONFIG_KLIPPER_MMU_PINS_SELECTOR_STEP);
RESERVE("selector_dir", CONFIG_KLIPPER_MMU_PINS_SELECTOR_DIR);
RESERVE("selector_enable", CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENABLE);
#endif
#if CONFIG_KLIPPER_MMU_SELECTOR_HAS_ENDSTOP
RESERVE("selector_endstop", CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENDSTOP);
#endif
#if CONFIG_KLIPPER_MMU_SELECTOR_KIND_INDEXED
RESERVE("selector_gate", CONFIG_KLIPPER_MMU_PINS_SELECTOR_GATE);
#endif
/* Klipper's dictionary cannot carry an empty reservation, so the optional
 * selector UART is reserved only when its driver has one. */
#if CONFIG_KLIPPER_MMU_SELECTOR_UART
RESERVE("selector_uart", CONFIG_KLIPPER_MMU_PINS_SELECTOR_UART);
#endif
#if CONFIG_KLIPPER_MMU_SERVO
RESERVE("servo", CONFIG_KLIPPER_MMU_PINS_SERVO);
#endif
#if CONFIG_KLIPPER_MMU_BAY_SENSORS
RESERVE("bay", CONFIG_KLIPPER_MMU_PINS_BAY);
#endif
#if CONFIG_KLIPPER_MMU_LOAD_SENSORS || CONFIG_KLIPPER_MMU_DEVICE_EMU
RESERVE("load", CONFIG_KLIPPER_MMU_PINS_LOAD);
#endif
#if !CONFIG_KLIPPER_MMU_VIRTUAL_HUB
RESERVE("hub", CONFIG_KLIPPER_MMU_PINS_HUB);
#endif
#if CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR
RESERVE("toolhead", CONFIG_KLIPPER_MMU_PINS_TOOLHEAD);
#endif
RESERVE("buffer_a", CONFIG_KLIPPER_MMU_PINS_BUFFER_A);
/* A tension-only buffer (KLIPPER_MMU_BUFFER_SINGLE_SWITCH) has no second,
 * compression switch to wire. */
#if !CONFIG_KLIPPER_MMU_BUFFER_SINGLE_SWITCH
RESERVE("buffer_b", CONFIG_KLIPPER_MMU_PINS_BUFFER_B);
#endif
#if CONFIG_KLIPPER_MMU_POWERED_REWIND
RESERVE("rewind_forward", CONFIG_KLIPPER_MMU_PINS_REWIND_FORWARD);
RESERVE("rewind_reverse", CONFIG_KLIPPER_MMU_PINS_REWIND_REVERSE);
RESERVE("rewind_enable", CONFIG_KLIPPER_MMU_PINS_REWIND_ENABLE);
#endif
#if CONFIG_KLIPPER_MMU_ENCODER
RESERVE("encoder_a", CONFIG_KLIPPER_MMU_PINS_ENCODER_A);
#if CONFIG_KLIPPER_MMU_ENCODER_DUAL_PIN
RESERVE("encoder_b", CONFIG_KLIPPER_MMU_PINS_ENCODER_B);
#endif
#endif
#if CONFIG_KLIPPER_MMU_MEASURED_CURRENT
RESERVE("current", CONFIG_KLIPPER_MMU_PINS_CURRENT);
#endif

/* Canonical P[A-I][0-15], same A..I order as Klipper's stm32 GPIO enumeration.
 * A port is accepted only when this target's header defines GPIOA..GPIOI.
 * Encoding remains port * 16 + bit. No J/K and no per-MCU pin tables. */
static bool stm32_port_available(unsigned port)
{
    switch (port) {
#ifdef GPIOA
    case 0:
#endif
#ifdef GPIOB
    case 1:
#endif
#ifdef GPIOC
    case 2:
#endif
#ifdef GPIOD
    case 3:
#endif
#ifdef GPIOE
    case 4:
#endif
#ifdef GPIOF
    case 5:
#endif
#ifdef GPIOG
    case 6:
#endif
#ifdef GPIOH
    case 7:
#endif
#ifdef GPIOI
    case 8:
#endif
        return true;
    default:
        return false;
    }
}

static bool parse_pin(const char **cursor, uint16_t *pin)
{
    const char *s = *cursor;
    if (s[0] != 'P' || s[1] < 'A' || s[1] > 'I') return false;
    unsigned port = (unsigned)(s[1] - 'A');
    if (!stm32_port_available(port)) return false;
    s += 2;
    if (*s < '0' || *s > '9') return false;
    unsigned bit = (unsigned)(*s++ - '0');
    if (*s >= '0' && *s <= '9') {
        if (bit != 1u || *s > '5') return false;
        bit = 10u + (unsigned)(*s++ - '0');
    }
    if (*s && *s != ',') return false;
    *pin = (uint16_t)(port * 16u + bit);
    *cursor = s;
    return true;
}
#endif

km_result_t km_config_pins_view(const km_pin_config_t **out,
                               km_resource_diagnostic_t *diagnostic)
{
    km_resource_diagnostic_t d = {KM_RESOURCE_DIAG_BAD_ARGUMENT,
                                KM_RESOURCE_NO_INDEX, KM_RESOURCE_NO_INDEX};
    if (diagnostic) *diagnostic = d;
    if (!out) return KM_ERR_INVALID_PARAM;
    d.reason = KM_RESOURCE_DIAG_NONE;
    if (diagnostic) *diagnostic = d;
#if !CONFIG_KLIPPER_MMU_PIN_CONFIG
    return KM_ERR_UNAVAILABLE;
#else
    const unsigned lanes = CONFIG_KLIPPER_MMU_LANE_COUNT;
    const unsigned gear = KM_GEAR_DRIVERS;
    struct { enum km_pin_role role; unsigned count; const char *text; } groups[] = {
        {KM_PIN_STEP, gear, CONFIG_KLIPPER_MMU_PINS_STEP},
        {KM_PIN_DIR, gear, CONFIG_KLIPPER_MMU_PINS_DIR},
        {KM_PIN_ENABLE, gear, CONFIG_KLIPPER_MMU_PINS_ENABLE},
#if CONFIG_KLIPPER_MMU_GEAR_TMC_UART
        {KM_PIN_UART, gear, CONFIG_KLIPPER_MMU_PINS_UART},
#endif
#if CONFIG_KLIPPER_MMU_BAY_SENSORS
        {KM_PIN_BAY, lanes, CONFIG_KLIPPER_MMU_PINS_BAY},
#endif
#if CONFIG_KLIPPER_MMU_LOAD_SENSORS || CONFIG_KLIPPER_MMU_DEVICE_EMU
        {KM_PIN_LOAD, lanes, CONFIG_KLIPPER_MMU_PINS_LOAD},
#endif
#if !CONFIG_KLIPPER_MMU_VIRTUAL_HUB
        {KM_PIN_HUB, 1, CONFIG_KLIPPER_MMU_PINS_HUB},
#endif
#if CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR
        {KM_PIN_TOOLHEAD, 1, CONFIG_KLIPPER_MMU_PINS_TOOLHEAD},
#endif
        {KM_PIN_BUFFER_A, 1, CONFIG_KLIPPER_MMU_PINS_BUFFER_A},
#if !CONFIG_KLIPPER_MMU_BUFFER_SINGLE_SWITCH
        {KM_PIN_BUFFER_B, 1, CONFIG_KLIPPER_MMU_PINS_BUFFER_B},
#endif
#if CONFIG_KLIPPER_MMU_POWERED_REWIND
        {KM_PIN_REWIND_FORWARD, lanes, CONFIG_KLIPPER_MMU_PINS_REWIND_FORWARD},
        {KM_PIN_REWIND_REVERSE, lanes, CONFIG_KLIPPER_MMU_PINS_REWIND_REVERSE},
        {KM_PIN_REWIND_ENABLE, lanes, CONFIG_KLIPPER_MMU_PINS_REWIND_ENABLE},
#endif
#if CONFIG_KLIPPER_MMU_ENCODER
        {KM_PIN_ENCODER_A, 1, CONFIG_KLIPPER_MMU_PINS_ENCODER_A},
#if CONFIG_KLIPPER_MMU_ENCODER_DUAL_PIN
        {KM_PIN_ENCODER_B, 1, CONFIG_KLIPPER_MMU_PINS_ENCODER_B},
#endif
#endif
#if CONFIG_KLIPPER_MMU_MEASURED_CURRENT
        {KM_PIN_CURRENT, lanes, CONFIG_KLIPPER_MMU_PINS_CURRENT},
#endif
#if CONFIG_KLIPPER_MMU_SELECTOR_STEPPER
        {KM_PIN_SELECTOR_STEP, 1, CONFIG_KLIPPER_MMU_PINS_SELECTOR_STEP},
        {KM_PIN_SELECTOR_DIR, 1, CONFIG_KLIPPER_MMU_PINS_SELECTOR_DIR},
        {KM_PIN_SELECTOR_ENABLE, 1, CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENABLE},
#endif
#if CONFIG_KLIPPER_MMU_SELECTOR_HAS_ENDSTOP
        {KM_PIN_SELECTOR_ENDSTOP, 1, CONFIG_KLIPPER_MMU_PINS_SELECTOR_ENDSTOP},
#endif
#if CONFIG_KLIPPER_MMU_SELECTOR_KIND_INDEXED
        {KM_PIN_SELECTOR_GATE, lanes, CONFIG_KLIPPER_MMU_PINS_SELECTOR_GATE},
#endif
#if CONFIG_KLIPPER_MMU_SERVO
        {KM_PIN_SERVO, 1, CONFIG_KLIPPER_MMU_PINS_SERVO},
#endif
    };
    if (!lanes || lanes > KM_MAX_LANES) {
        d.reason = KM_RESOURCE_DIAG_BAD_ARGUMENT;
        if (diagnostic) *diagnostic = d;
        return KM_ERR_INVALID_PARAM;
    }
#if CONFIG_KLIPPER_MMU_VIRTUAL_HUB
    if (CONFIG_KLIPPER_MMU_PINS_HUB[0] != '\0') {
        d.reason = KM_RESOURCE_DIAG_BAD_CLAIM;
        if (diagnostic) *diagnostic = d;
        return KM_ERR_INVALID_PARAM;
    }
#endif
    /* One immutable saved configuration view serves all setup consumers.
     * Keep this plan static: it is larger than Klipper's default MCU stack. */
    static km_pin_config_t candidate;
    static bool candidate_valid;
    if (candidate_valid) {
        *out = &candidate;
        return KM_OK;
    }
    memset(&candidate, 0, sizeof(candidate));
    memset(candidate.pin, 0xff, sizeof(candidate.pin));
    for (size_t group = 0; group < sizeof(groups) / sizeof(groups[0]); ++group) {
        const char *s = groups[group].text;
        for (unsigned lane = 0; lane < groups[group].count; ++lane) {
            uint16_t pin;
            bool last = lane + 1u == groups[group].count;
            if (!parse_pin(&s, &pin) || (last ? *s != '\0' : *s != ',')) {
                d.reason = KM_RESOURCE_DIAG_BAD_CLAIM;
                d.first_index = (uint16_t)candidate.count;
                if (diagnostic) *diagnostic = d;
                return KM_ERR_INVALID_PARAM;
            }
            candidate.pin[groups[group].role][lane] = pin;
            candidate.claims[candidate.count++] = (km_resource_claim_t){
                .kind = KM_RESOURCE_PIN, .instance = pin,
                .owner = (uint16_t)groups[group].role};
            if (!last) ++s;
        }
    }
#if CONFIG_KLIPPER_MMU_SELECTOR_STEPPER && CONFIG_KLIPPER_MMU_SELECTOR_UART
    /* KLIPPER_MMU_SELECTOR_UART on: the selector's own TMC UART pin is
     * required (an empty one is refused, not a silent plain step/dir axis).
     * Off: no pin, and candidate.pin stays UNUSED. */
    {
        const char *s = CONFIG_KLIPPER_MMU_PINS_SELECTOR_UART;
        uint16_t pin;
        if (!parse_pin(&s, &pin) || *s != '\0') {
            d.reason = KM_RESOURCE_DIAG_BAD_CLAIM;
            d.first_index = (uint16_t)candidate.count;
            if (diagnostic) *diagnostic = d;
            return KM_ERR_INVALID_PARAM;
        }
        candidate.pin[KM_PIN_SELECTOR_UART][0] = pin;
        /* Happy Hare's classic ERCF 1.1 EASY-BRD ties the gear's and the
         * selector's TMC2209 to one shared single-wire UART pin at distinct
         * addresses (installer/boards/Kconfig.easy_brd_rp2040:24-25;
         * docs/selectors.md). A PIN claim is exclusive by instance
         * regardless of owner (km_resource_plan_validate), so that one
         * legitimate sharing must not add a second claim here -- the gear's
         * own KM_PIN_UART claim above already reserves the wire for both,
         * and km_tmc_uart_gpio_setup enforces the same distinct-address
         * rule again at the transport layer. Equal addresses (or an
         * unreadable gear address) fall straight through to the ordinary
         * claim below, which the generic exclusivity check just below
         * rejects exactly like any other accidental pin reuse. */
        const char *gear_address = CONFIG_KLIPPER_MMU_TMC_ADDRESSES;
        bool shared_at_distinct_address = pin == candidate.pin[KM_PIN_UART][0]
            && gear_address[0] >= '0' && gear_address[0] <= '3'
            && (gear_address[0] - '0') != CONFIG_KLIPPER_MMU_SELECTOR_UART_ADDRESS;
        if (!shared_at_distinct_address) {
            candidate.claims[candidate.count++] = (km_resource_claim_t){
                .kind = KM_RESOURCE_PIN, .instance = pin,
                .owner = (uint16_t)KM_PIN_SELECTOR_UART};
        }
    }
#endif
    /* Non-H723 differential buffers stay unsupported. Route validity is owned
     * by the H723 resource factory, which queries the shared ADC route. */
#if CONFIG_KLIPPER_MMU_DIFF_BUFFER && !CONFIG_MACH_STM32H723
    d.reason = KM_RESOURCE_DIAG_BAD_CLAIM;
    d.first_index = (uint16_t)(5u * lanes + 2u);
    if (diagnostic) *diagnostic = d;
    return KM_ERR_NOT_SUPPORTED;
#endif
    km_result_t result = km_resource_plan_validate(candidate.claims, candidate.count, diagnostic);
    if (result == KM_OK) {
        candidate_valid = true;
        *out = &candidate;
    }
    return result;
#endif
}

km_result_t km_config_pins(km_pin_config_t *out,
                           km_resource_diagnostic_t *diagnostic)
{
    if (!out) {
        if (diagnostic) *diagnostic = (km_resource_diagnostic_t){
            KM_RESOURCE_DIAG_BAD_ARGUMENT, KM_RESOURCE_NO_INDEX, KM_RESOURCE_NO_INDEX};
        return KM_ERR_INVALID_PARAM;
    }
    const km_pin_config_t *view;
    km_result_t result = km_config_pins_view(&view, diagnostic);
    if (result == KM_OK) *out = *view;
    return result;
}
