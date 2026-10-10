// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_SHIM_STM32_INTERNAL_H
#define TEST_SHIM_STM32_INTERNAL_H
/* H723 exposes GPIOA through GPIOI. Values are unused; pin_config.c only
 * tests whether the port macro exists. */
#define GPIOA 1
#define GPIOB 1
#define GPIOC 1
#define GPIOD 1
#define GPIOE 1
#define GPIOF 1
#define GPIOG 1
#define GPIOH 1
#define GPIOI 1
/* Distinct timer identities. gpio_pwm_get_route returns these addresses, and
 * km_h723_pwm_route compares them with the same macros. */
typedef struct { uint8_t index; } TIM_TypeDef;
extern TIM_TypeDef km_test_tim1, km_test_tim2, km_test_tim3, km_test_tim4;
extern TIM_TypeDef km_test_tim5, km_test_tim8, km_test_tim12, km_test_tim13;
extern TIM_TypeDef km_test_tim14, km_test_tim15, km_test_tim16, km_test_tim17;
#define TIM1 (&km_test_tim1)
#define TIM2 (&km_test_tim2)
#define TIM3 (&km_test_tim3)
#define TIM4 (&km_test_tim4)
#define TIM5 (&km_test_tim5)
#define TIM8 (&km_test_tim8)
#define TIM12 (&km_test_tim12)
#define TIM13 (&km_test_tim13)
#define TIM14 (&km_test_tim14)
#define TIM15 (&km_test_tim15)
#define TIM16 (&km_test_tim16)
#define TIM17 (&km_test_tim17)
#endif

