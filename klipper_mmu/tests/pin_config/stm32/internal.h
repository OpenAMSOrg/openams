// SPDX-License-Identifier: GPL-3.0-or-later
/* Target metadata for parser validation only, not registers or hardware
 * support. Native tests compile the real pin parser with the same
 * GPIO-presence conditions as MCU builds. GPIOI is intentionally absent.
 * Only native metadata is combined: angle brackets pull the PWM fixture
 * timer metadata (TIM_TypeDef, TIM2, and the rest) already on the test
 * include path, skipping this directory so this file is not included
 * again. Timer declarations and driver implementation stay there.
 * Production MCU builds never see this native header.
 */
#ifndef KLIPPER_MMU_PIN_CONFIG_STM32_INTERNAL_H
#define KLIPPER_MMU_PIN_CONFIG_STM32_INTERNAL_H

#include <internal.h>

#define GPIOA 1
#define GPIOB 1
#define GPIOC 1
#define GPIOD 1
#define GPIOE 1
#define GPIOF 1
#define GPIOG 1
#define GPIOH 1

#endif
