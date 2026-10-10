// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef PAIR_BINDING_PWM_H
#define PAIR_BINDING_PWM_H
#include <stdint.h>
struct gpio_pwm_pair;
enum { GPIO_PWM_PAIR_OK=0, GPIO_PWM_PAIR_ERR_INVALID=-1, GPIO_PWM_PAIR_ERR_BUSY=-2,
       GPIO_PWM_PAIR_ERR_NOT_SUPPORTED=-3, GPIO_PWM_PAIR_ERR_HARDWARE=-4,
       GPIO_PWM_PAIR_ERR_SHUTDOWN=-5 };
struct gpio_pwm_pair_config {
    uint32_t forward_pin, reverse_pin; uint8_t forward_active_low, reverse_active_low;
    uint32_t cycle_time; void (*raw_force_off)(void *); void *raw_force_off_context;
};
struct gpio_pwm_pair_info {
    uint32_t hwpwm_ticks, prescaler, reverse_hwpwm_ticks, reverse_prescaler;
    uint32_t confirmed_forward_val, confirmed_reverse_val;
    uint32_t confirmed_forward_counts, confirmed_reverse_counts;
    uint8_t forward_channel, reverse_channel, same_timer, is_ready;
};
int gpio_pwm_pair_setup(const struct gpio_pwm_pair_config *, struct gpio_pwm_pair **);
int gpio_pwm_pair_update(struct gpio_pwm_pair *, uint32_t, uint32_t);
void gpio_pwm_pair_raw_disable(struct gpio_pwm_pair *);
int gpio_pwm_pair_is_ready(const struct gpio_pwm_pair *);
int gpio_pwm_pair_get_info(const struct gpio_pwm_pair *, struct gpio_pwm_pair_info *);
void gpio_pwm_pair_shutdown(void);
#endif
