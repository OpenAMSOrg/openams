// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <stdint.h>
#include "autoconf.h"

struct km_test_rng_regs { volatile uint32_t CR, SR, DR; };
struct km_test_rcc_regs {
    volatile uint32_t CR, PLLCFGR, CFGR, AHB2ENR, PLLSAICFGR, DCKCFGR2;
    volatile uint32_t PLLCKSELR, PLL1DIVR, D1CFGR, D2CCIP2R;
};
extern struct km_test_rng_regs km_test_rng;
extern struct km_test_rcc_regs km_test_rcc;
#define RNG (&km_test_rng)
#define RCC (&km_test_rcc)

#define RCC_CR_HSIRDY (1u << 1)
#define RCC_CR_HSERDY (1u << 17)
#define RCC_CR_PLLON (1u << 24)
#define RCC_CR_PLLRDY (1u << 25)
#define RCC_CR_PLLSAIRDY (1u << 29)
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
#if CONFIG_MACH_STM32H743 || CONFIG_MACH_STM32H750
#define RCC_CFGR_SWS_Msk (7u << 3)
#else
#define RCC_CFGR_SWS_Msk (3u << 2)
#endif
#define RCC_CFGR_SWS_PLL (2u << 2)
#define RCC_AHB2ENR_RNGEN (1u << 6)
#define RCC_DCKCFGR2_CK48MSEL (1u << 27)
#define RCC_PLLSAICFGR_PLLSAIN_Pos 6u
#define RCC_PLLSAICFGR_PLLSAIN_Msk (0x1ffu << RCC_PLLSAICFGR_PLLSAIN_Pos)
#define RCC_PLLSAICFGR_PLLSAIP_Pos 16u
#define RCC_PLLSAICFGR_PLLSAIP_Msk (3u << RCC_PLLSAICFGR_PLLSAIP_Pos)
#define RCC_PLLSAICFGR_PLLSAIQ_Pos 24u
#define RCC_PLLSAICFGR_PLLSAIQ_Msk (0xfu << RCC_PLLSAICFGR_PLLSAIQ_Pos)

/* STM32H743/H750 RCC fields, positions from the pinned stm32h743xx.h. */
#define RCC_CR_HSI48ON (1u << 12)
#define RCC_CR_HSI48RDY (1u << 13)
#define RCC_CR_PLL1ON (1u << 24)
#define RCC_CR_PLL1RDY (1u << 25)
#define RCC_CFGR_SWS_PLL1 (3u << 3)
#define RCC_PLLCKSELR_PLLSRC_Msk (3u << 0)
#define RCC_PLLCKSELR_PLLSRC_HSE (2u << 0)
#define RCC_PLLCKSELR_DIVM1_Pos 4u
#define RCC_PLLCKSELR_DIVM1_Msk (0x3fu << RCC_PLLCKSELR_DIVM1_Pos)
#define RCC_PLLCFGR_PLL1FRACEN (1u << 0)
#define RCC_PLLCFGR_PLL1VCOSEL (1u << 1)
#define RCC_PLLCFGR_PLL1RGE_Pos 2u
#define RCC_PLLCFGR_PLL1RGE_Msk (3u << RCC_PLLCFGR_PLL1RGE_Pos)
#define RCC_PLLCFGR_DIVQ1EN (1u << 17)
#define RCC_PLLCFGR_DIVP1EN (1u << 16)
#define RCC_PLL1DIVR_N1_Pos 0u
#define RCC_PLL1DIVR_N1_Msk (0x1ffu << RCC_PLL1DIVR_N1_Pos)
#define RCC_PLL1DIVR_P1_Pos 9u
#define RCC_PLL1DIVR_P1_Msk (0x7fu << RCC_PLL1DIVR_P1_Pos)
#define RCC_PLL1DIVR_Q1_Pos 16u
#define RCC_D1CFGR_HPRE_Msk (0xfu << 0)
#define RCC_D1CFGR_HPRE_DIV2 (8u << 0)
#define RCC_D1CFGR_D1PPRE_DIV2 (4u << 4)
#define RCC_D1CFGR_D1CPRE_Msk (0xfu << 8)
#define RCC_D2CCIP2R_RNGSEL_Pos 8u
#define RCC_D2CCIP2R_RNGSEL_Msk (3u << RCC_D2CCIP2R_RNGSEL_Pos)
#define RCC_D2CCIP2R_USBSEL_Msk (3u << 20)
#define RCC_D2CCIP2R_USBSEL RCC_D2CCIP2R_USBSEL_Msk

#define RNG_CR_RNGEN (1u << 2)
#define RNG_SR_DRDY (1u << 0)
#define RNG_SR_CECS (1u << 1)
#define RNG_SR_SECS (1u << 2)
#define RNG_SR_CEIS (1u << 5)
#define RNG_SR_SEIS (1u << 6)
