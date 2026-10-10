// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef REVIEW_PWM_INTERNAL_H
#define REVIEW_PWM_INTERNAL_H
#include <stdint.h>

/* Register storage only. This fixture does NOT model physical shadow loading. */
typedef struct {
    volatile uint32_t CR1, CR2, SMCR, DIER, SR, EGR, CCMR1, CCMR2, CCER;
    volatile uint32_t CNT, PSC, ARR, RCR, CCR1, CCR2, CCR3, CCR4, BDTR;
} TIM_TypeDef;
extern TIM_TypeDef review_timers[18];
#define TIM1 (&review_timers[1])
#define TIM2 (&review_timers[2])
#define TIM3 (&review_timers[3])
#define TIM4 (&review_timers[4])
#define TIM5 (&review_timers[5])
#define TIM8 (&review_timers[8])
#define TIM12 (&review_timers[12])
#define TIM13 (&review_timers[13])
#define TIM14 (&review_timers[14])
#define TIM15 (&review_timers[15])
#define TIM16 (&review_timers[16])
#define TIM17 (&review_timers[17])
#define GPIO(port, num) (((port)-'A') * 16 + (num))
#define GPIO_FUNCTION(fn) (2 | ((fn) << 4))
#define TIM_CR1_CEN (1U << 0)
#define TIM_EGR_UG (1U << 0)
#define TIM_CCER_CC1E (1U << 0)
#define TIM_CCER_CC2E (1U << 4)
#define TIM_CCER_CC3E (1U << 8)
#define TIM_CCER_CC4E (1U << 12)
#define TIM_CCMR1_CC1S (3U << 0)
#define TIM_CCMR1_CC2S (3U << 8)
#define TIM_CCMR2_CC3S (3U << 0)
#define TIM_CCMR2_CC4S (3U << 8)
#define TIM_CCMR1_OC1M (7U << 4)
#define TIM_CCMR1_OC1M_1 (2U << 4)
#define TIM_CCMR1_OC1M_2 (4U << 4)
#define TIM_CCMR1_OC1PE (1U << 3)
#define TIM_CCMR1_OC1FE (1U << 2)
#define TIM_CCMR1_OC2M (7U << 12)
#define TIM_CCMR1_OC2M_1 (2U << 12)
#define TIM_CCMR1_OC2M_2 (4U << 12)
#define TIM_CCMR1_OC2PE (1U << 11)
#define TIM_CCMR1_OC2FE (1U << 10)
#define TIM_CCMR2_OC3M (7U << 4)
#define TIM_CCMR2_OC3M_1 (2U << 4)
#define TIM_CCMR2_OC3M_2 (4U << 4)
#define TIM_CCMR2_OC3PE (1U << 3)
#define TIM_CCMR2_OC3FE (1U << 2)
#define TIM_CCMR2_OC4M (7U << 12)
#define TIM_CCMR2_OC4M_1 (2U << 12)
#define TIM_CCMR2_OC4M_2 (4U << 12)
#define TIM_CCMR2_OC4PE (1U << 11)
#define TIM_CCMR2_OC4FE (1U << 10)
#define TIM_BDTR_MOE (1U << 15)
uint32_t get_pclock_frequency(uint32_t base);
int is_enabled_pclock(uint32_t base);
void enable_pclock(uint32_t base);
void gpio_peripheral(uint32_t pin, uint32_t function, int pull_up);
#endif
