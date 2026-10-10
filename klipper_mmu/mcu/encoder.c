// SPDX-License-Identifier: GPL-3.0-or-later
#include "encoder.h"
#include "pin_config.h"
#include "clock.h"
#include "autoconf.h"
#if CONFIG_WANT_GPIO_LOCAL
#include "stm32/gpio_local.h"
#endif
#include "board/gpio.h"
#include "board/irq.h"
#include "command.h"
#include "sched.h"

/* Every static byte below is carried by every image that links this file
 * (mcu/Makefile builds it unconditionally, like switch_gpio.c/sensors.c),
 * regardless of whether this particular build enables the encoder. Some
 * images have very little RAM to spare (docs/selectors.md's G431 NightOwl
 * note), so the disabled build below declares none of it: a config/GPIO
 * state machine only exists when CONFIG_KLIPPER_MMU_ENCODER actually turns
 * it on, the same disabled-build-equivalence shape as every other optional
 * capability here. */
#if CONFIG_KLIPPER_MMU_ENCODER

km_result_t km_config_encoder(km_encoder_gpio_config_t *out)
{
    if (!out) return KM_ERR_INVALID_PARAM;
#if !CONFIG_KLIPPER_MMU_PIN_CONFIG
    return KM_ERR_UNAVAILABLE;
#else
    if (CONFIG_KLIPPER_MMU_ENCODER_PULL < -1) return KM_ERR_UNAVAILABLE;
    const km_pin_config_t *pins;
    km_result_t result = km_config_pins_view(&pins, NULL);
    if (result != KM_OK) return result;
    uint16_t pin_a = pins->pin[KM_PIN_ENCODER_A][0];
    if (pin_a == KM_PIN_UNUSED) return KM_ERR_UNAVAILABLE;
    out->pin_a = pin_a;
    /* KM_PIN_UNUSED when KLIPPER_MMU_ENCODER_DUAL_PIN is off (the plain,
     * one-wire shape every cited real ERCF/Tradrack encoder uses); setup
     * and sample below simply do not touch a pin that is not there. */
    out->pin_b = pins->pin[KM_PIN_ENCODER_B][0];
    out->pull = (int8_t)CONFIG_KLIPPER_MMU_ENCODER_PULL;
    return KM_OK;
#endif
}

static struct gpio_in gpio_a, gpio_b;
static uint8_t last_a, last_b;
static uint32_t total_clicks; /* Wraps like Klipper's own MCU counter; see encoder.h. */
static bool ready, stopped, has_b;

km_result_t km_encoder_gpio_setup(const km_encoder_gpio_config_t *config)
{
    if (!config || config->pin_a == KM_PIN_UNUSED
        || (config->pin_b != KM_PIN_UNUSED && config->pin_a == config->pin_b))
        return KM_ERR_INVALID_PARAM;
    if (ready) return KM_ERR_BUSY;
    has_b = config->pin_b != KM_PIN_UNUSED;
#if CONFIG_WANT_GPIO_LOCAL
    gpio_a = gpio_in_setup_local(config->pin_a, config->pull);
    if (has_b) gpio_b = gpio_in_setup_local(config->pin_b, config->pull);
#else
    gpio_a = gpio_in_setup(config->pin_a, config->pull);
    if (has_b) gpio_b = gpio_in_setup(config->pin_b, config->pull);
#endif
    last_a = (uint8_t)(gpio_in_read(gpio_a) != 0);
    last_b = has_b ? (uint8_t)(gpio_in_read(gpio_b) != 0) : 0;
    total_clicks = 0;
    stopped = false;
    ready = true;
    return KM_OK;
}

/* Control task context: no interrupt touches gpio_a/gpio_b or total_clicks. */
void km_encoder_gpio_sample(km_operation_core_t *core, void *context)
{
    if (!core || context)
        shutdown("klipper_mmu: invalid encoder binding");
    km_core_timepoint_t acquisition;
    bool active = ready && !stopped && !sched_is_shutdown()
                 && km_clock_get_time(&acquisition) == KM_OK;
    if (active) {
        uint8_t a = (uint8_t)(gpio_in_read(gpio_a) != 0);
        if (a != last_a) { ++total_clicks; last_a = a; }
        if (has_b) {
            uint8_t b = (uint8_t)(gpio_in_read(gpio_b) != 0);
            if (b != last_b) { ++total_clicks; last_b = b; }
        }
        km_core_set_encoder_at(core, (int32_t)total_clicks, true, acquisition.ms);
    } else {
        /* Retain the last total instead of inventing a zero/no-movement
         * reading; see km_core_set_load_sensor_at's "retains its value for
         * diagnostics but cannot satisfy admission" precedent. */
        km_core_set_encoder_at(core, (int32_t)total_clicks, false, core->last_control_ms);
    }
}

void km_encoder_gpio_shutdown(void)
{
    stopped = true;
}
DECL_SHUTDOWN(km_encoder_gpio_shutdown);

int32_t km_encoder_gpio_get_clicks(void)
{
    irqstatus_t flag = irq_save();
    uint32_t clicks = total_clicks;
    irq_restore(flag);
    return (int32_t)clicks;
}

#else /* !CONFIG_KLIPPER_MMU_ENCODER: no state, no pins, no RAM. */

km_result_t km_config_encoder(km_encoder_gpio_config_t *out)
{
    (void)out;
    return KM_ERR_UNAVAILABLE;
}

km_result_t km_encoder_gpio_setup(const km_encoder_gpio_config_t *config)
{
    (void)config;
    return KM_ERR_UNAVAILABLE;
}

void km_encoder_gpio_sample(km_operation_core_t *core, void *context)
{
    (void)context;
    if (core) km_core_set_encoder_at(core, 0, false, core->last_control_ms);
}

void km_encoder_gpio_shutdown(void)
{
}

int32_t km_encoder_gpio_get_clicks(void)
{
    return 0;
}

#endif /* CONFIG_KLIPPER_MMU_ENCODER */
