// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef PAIR_TEST_INTERNAL_H
#define PAIR_TEST_INTERNAL_H
#include <stdint.h>
#include <stddef.h>
typedef struct {
    volatile uint32_t CR1, CR2, SMCR, DIER, SR, EGR, CCMR1, CCMR2, CCER;
    volatile uint32_t CNT, PSC, ARR, RCR, CCR1, CCR2, CCR3, CCR4, BDTR;
} TIM_TypeDef;
extern TIM_TypeDef pair_test_timers[18];
#define TIM1 (&pair_test_timers[1])
#define TIM2 (&pair_test_timers[2])
#define TIM3 (&pair_test_timers[3])
#define TIM4 (&pair_test_timers[4])
#define TIM5 (&pair_test_timers[5])
#define TIM8 (&pair_test_timers[8])
#define TIM12 (&pair_test_timers[12])
#define TIM13 (&pair_test_timers[13])
#define TIM14 (&pair_test_timers[14])
#define TIM15 (&pair_test_timers[15])
#define TIM16 (&pair_test_timers[16])
#define TIM17 (&pair_test_timers[17])
#define GPIO(port, num) (((port) - 'A') * 16 + (num))
#define GPIO_FUNCTION(fn) (2 | ((fn) << 4))
#define TIM_CR1_CEN (1U << 0)
#define TIM_EGR_UG (1U << 0)
#define TIM_SR_UIF (1U << 0)
#define TIM_CCER_CC1E (1U << 0)
#define TIM_CCER_CC2E (1U << 4)
#define TIM_CCER_CC3E (1U << 8)
#define TIM_CCER_CC4E (1U << 12)
#define TIM_CCER_CC1P (1U << 1)
#define TIM_CCER_CC2P (1U << 5)
#define TIM_CCER_CC3P (1U << 9)
#define TIM_CCER_CC4P (1U << 13)
#define TIM_CCMR1_CC1S (3U << 0)
#define TIM_CCMR1_CC2S (3U << 8)
#define TIM_CCMR2_CC3S (3U << 0)
#define TIM_CCMR2_CC4S (3U << 8)
#define TIM_CCMR1_OC1M (0x10070U)
#define TIM_CCMR1_OC1M_1 (2U << 4)
#define TIM_CCMR1_OC1M_2 (4U << 4)
#define TIM_CCMR1_OC1PE (1U << 3)
#define TIM_CCMR1_OC2M (0x10070U << 8)
#define TIM_CCMR1_OC2M_1 (2U << 12)
#define TIM_CCMR1_OC2M_2 (4U << 12)
#define TIM_CCMR1_OC2PE (1U << 11)
#define TIM_CCMR2_OC3M (0x10070U)
#define TIM_CCMR2_OC3M_1 (2U << 4)
#define TIM_CCMR2_OC3M_2 (4U << 4)
#define TIM_CCMR2_OC3PE (1U << 3)
#define TIM_CCMR2_OC4M (0x10070U << 8)
#define TIM_CCMR2_OC4M_1 (2U << 12)
#define TIM_CCMR2_OC4M_2 (4U << 12)
#define TIM_CCMR2_OC4PE (1U << 11)
#define TIM_CCMR1_OC1FE (1U << 2)
#define TIM_CCMR1_OC2FE (1U << 10)
#define TIM_CCMR2_OC3FE (1U << 2)
#define TIM_CCMR2_OC4FE (1U << 10)
#define TIM_BDTR_MOE (1U << 15)
uint32_t get_pclock_frequency(uint32_t base);
int is_enabled_pclock(uint32_t base);
void enable_pclock(uint32_t base);
#endif
