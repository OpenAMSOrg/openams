// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_SHIM_AUTOCONF_H
#define TEST_SHIM_AUTOCONF_H

#ifndef CONFIG_MACH_STM32H723
#define CONFIG_MACH_STM32H723 1
#endif
#ifndef CONFIG_WANT_ADC
#define CONFIG_WANT_ADC 1
#endif
#ifndef CONFIG_WANT_ADC_LOCAL
#define CONFIG_WANT_ADC_LOCAL 1
#endif
#ifndef CONFIG_WANT_HARD_PWM_LOCAL_PAIR
#define CONFIG_WANT_HARD_PWM_LOCAL_PAIR 1
#endif
#ifndef CONFIG_WANT_HARD_PWM_QUERY
#define CONFIG_WANT_HARD_PWM_QUERY 1
#endif
#ifndef CONFIG_WANT_TMCUART_LOCAL
#define CONFIG_WANT_TMCUART_LOCAL 1
#endif
#ifndef CONFIG_KLIPPER_MMU_TMC_UART_SHARED
#define CONFIG_KLIPPER_MMU_TMC_UART_SHARED 1
#endif
#ifndef CONFIG_WANT_GPIO_LOCAL
#define CONFIG_WANT_GPIO_LOCAL 1
#endif
/* Selector add-on (klipper_mmu/selector.h): off for the suites that include
 * this shim; the board-startup Makefile turns both on for its selector
 * scenario, which links the real mcu/servo_pwm.c. */
#ifndef CONFIG_KLIPPER_MMU_SELECTOR_STEPPER
#define CONFIG_KLIPPER_MMU_SELECTOR_STEPPER 0
#endif
#ifndef CONFIG_KLIPPER_MMU_SERVO
#define CONFIG_KLIPPER_MMU_SERVO 0
#endif

#endif
