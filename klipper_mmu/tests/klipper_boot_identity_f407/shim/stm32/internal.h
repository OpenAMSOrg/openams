// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <stdint.h>
#include "autoconf.h"

struct km_test_rng_regs { volatile uint32_t CR, SR, DR; };
struct km_test_rcc_regs {
    volatile uint32_t CR, PLLCFGR, CFGR, AHB2ENR, PLLSAICFGR, DCKCFGR2,
        CRRCR, CCIPR;
};
extern struct km_test_rng_regs km_test_rng;
extern struct km_test_rcc_regs km_test_rcc;
#define RNG (&km_test_rng)
#define RCC (&km_test_rcc)

#if CONFIG_MACH_STM32G431 || CONFIG_MACH_STM32G474
#define RCC_CR_HSIRDY (1u << 10)
#else
#define RCC_CR_HSIRDY (1u << 1)
#endif
#define RCC_CR_HSERDY (1u << 17)
#define RCC_CR_PLLON (1u << 24)
#define RCC_CR_PLLRDY (1u << 25)
#define RCC_CR_PLLSAIRDY (1u << 29)
#if CONFIG_MACH_STM32G431 || CONFIG_MACH_STM32G474
#define RCC_PLLCFGR_PLLM_Pos 4u
#define RCC_PLLCFGR_PLLM_Msk (0xfu << RCC_PLLCFGR_PLLM_Pos)
#define RCC_PLLCFGR_PLLN_Pos 8u
#define RCC_PLLCFGR_PLLN_Msk (0x7fu << RCC_PLLCFGR_PLLN_Pos)
#define RCC_PLLCFGR_PLLP_Msk 0u
#define RCC_PLLCFGR_PLLSRC_Msk 3u
#define RCC_PLLCFGR_PLLSRC_HSE 3u
#define RCC_PLLCFGR_PLLSRC_HSI (1u << 1)
#define RCC_PLLCFGR_PLLR_Pos 25u
#define RCC_PLLCFGR_PLLR_Msk (3u << RCC_PLLCFGR_PLLR_Pos)
#define RCC_PLLCFGR_PLLREN (1u << 24)
#define RCC_CRRCR_HSI48ON 1u
#define RCC_CRRCR_HSI48RDY (1u << 1)
#define RCC_CCIPR_CLK48SEL_Msk (3u << 26)
#define RCC_AHB2ENR_RNGEN (1u << 26)
#else
#define RCC_PLLCFGR_PLLM_Pos 0u
#define RCC_PLLCFGR_PLLM_Msk (0x3fu << RCC_PLLCFGR_PLLM_Pos)
#define RCC_PLLCFGR_PLLN_Pos 6u
#define RCC_PLLCFGR_PLLN_Msk (0x1ffu << RCC_PLLCFGR_PLLN_Pos)
#define RCC_PLLCFGR_PLLP_Msk (3u << 16)
#define RCC_PLLCFGR_PLLSRC_Msk (1u << 22)
#define RCC_PLLCFGR_PLLSRC_HSE (1u << 22)
#define RCC_PLLCFGR_PLLSRC_HSI 0u
#define RCC_PLLCFGR_PLLQ_Pos 24u
#define RCC_PLLCFGR_PLLQ_Msk (0xfu << RCC_PLLCFGR_PLLQ_Pos)
#define RCC_DCKCFGR2_CK48MSEL (1u << 27)
#define RCC_PLLSAICFGR_PLLSAIN_Pos 6u
#define RCC_PLLSAICFGR_PLLSAIN_Msk (0x1ffu << RCC_PLLSAICFGR_PLLSAIN_Pos)
#define RCC_PLLSAICFGR_PLLSAIP_Pos 16u
#define RCC_PLLSAICFGR_PLLSAIP_Msk (3u << RCC_PLLSAICFGR_PLLSAIP_Pos)
#define RCC_PLLSAICFGR_PLLSAIQ_Pos 24u
#define RCC_PLLSAICFGR_PLLSAIQ_Msk (0xfu << RCC_PLLSAICFGR_PLLSAIQ_Pos)
#endif
#define RCC_CFGR_SWS_Msk (3u << 2)
#if CONFIG_MACH_STM32G431 || CONFIG_MACH_STM32G474
#define RCC_CFGR_SWS_PLL (3u << 2)
#else
#define RCC_CFGR_SWS_PLL (2u << 2)
#define RCC_AHB2ENR_RNGEN (1u << 6)
#endif
#define RNG_CR_RNGEN (1u << 2)
#define RNG_SR_DRDY (1u << 0)
#define RNG_SR_CECS (1u << 1)
#define RNG_SR_SECS (1u << 2)
#define RNG_SR_CEIS (1u << 5)
#define RNG_SR_SEIS (1u << 6)
