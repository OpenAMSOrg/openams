// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_MCU_SERVO_PWM_H_INCLUDED
#define KLIPPER_MMU_MCU_SERVO_PWM_H_INCLUDED

#include "klipper_mmu/selector.h"

#ifdef __cplusplus
extern "C" {
#endif

/* One servo output, driven by Klipper's own scheduled software PWM on a
 * plain GPIO: a fixed 20 ms period, the way src/gpiocmds.c's digital_out
 * toggles a pin from a timer, but with only one duty setting instead of a
 * move queue (a servo just holds a position). set_pulse_us(0) stops the
 * pulse train and leaves the pin low; hardware PWM is not used. */
#define KM_SERVO_MAX_SLOTS 1u
#define KM_SERVO_PERIOD_US 20000u

/* Claims the pin (permanently, until MCU reset) and publishes *out. Task
 * context, IRQs enabled, clock/shutdown state does not matter beyond the
 * usual "not already latched" check. With CONFIG_WANT_GPIO_LOCAL, pin must
 * already be locally reserved like every other MMU output. */
KM_API km_result_t km_servo_pwm_setup(uint32_t pin, km_servo_t **out);

KM_API bool km_servo_pwm_is_slot_active(uint32_t slot);

/* Emergency shutdown hook: drives the pin low and stops pulsing. */
void km_servo_pwm_shutdown(void);

#ifdef __cplusplus
}
#endif

#endif
