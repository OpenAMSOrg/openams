// SPDX-License-Identifier: GPL-3.0-or-later
// STM32F207/F405/F407/F429/F765/G431/G474/H743/H750 boot identity on USB 48 MHz
// clock routes. RNG register names and layout come from the pinned CMSIS headers.
// Clock and health semantics follow RM0033 (F2), RM0090 (F4), RM0410 (F7) and
// RM0433 (H743/H750 classic RNG: CR/SR/DR only, no conditioning or HTCR; RM0440 G4). This
// owns the RNG only when its AHB2 clock is initially disabled; it never uses
// Klipper's AHB1-only generic peripheral clock helper for this AHB2 peripheral.
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <stdint.h>
#include "autoconf.h"

#if !defined(CONFIG_USBSERIAL) || !CONFIG_USBSERIAL \
    || (defined(CONFIG_STM32_USB_PB14_PB15) \
        && CONFIG_STM32_USB_PB14_PB15)
#error "STM32 RNG identity requires USB on PA11/PA12"
#endif
#if (defined(CONFIG_MACH_STM32H743) && CONFIG_MACH_STM32H743) \
    || (defined(CONFIG_MACH_STM32H750) && CONFIG_MACH_STM32H750)
#define KM_RNG_H7 1
#if !defined(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H743_H750) \
    || !CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H743_H750 \
    || !defined(CONFIG_STM32_USB_PA11_PA12) \
    || !CONFIG_STM32_USB_PA11_PA12 \
    || (defined(CONFIG_STM32_CLOCK_REF_INTERNAL) \
        && CONFIG_STM32_CLOCK_REF_INTERNAL) \
    || !((defined(CONFIG_MACH_STM32H743) && CONFIG_MACH_STM32H743 \
          && CONFIG_CLOCK_FREQ == 400000000u) \
         || (defined(CONFIG_MACH_STM32H750) && CONFIG_MACH_STM32H750 \
             && CONFIG_CLOCK_FREQ == 480000000u))
#error "STM32H743/H750 RNG identity requires its crystal USB gate at 400/480 MHz"
#endif
#elif (defined(CONFIG_MACH_STM32G431) && CONFIG_MACH_STM32G431) \
    || (defined(CONFIG_MACH_STM32G474) && CONFIG_MACH_STM32G474)
#if !defined(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_G4) \
    || !CONFIG_KLIPPER_MMU_BOOT_IDENTITY_G4 \
    || !defined(CONFIG_STM32_USB_PA11_PA12) \
    || !CONFIG_STM32_USB_PA11_PA12 \
    || CONFIG_CLOCK_FREQ != 170000000u \
    || (defined(CONFIG_STM32_CLOCK_REF_25M) && CONFIG_STM32_CLOCK_REF_25M)
#error "STM32G4 RNG identity requires USB PA11/PA12 at 170 MHz with supported reference clock"
#endif
#elif defined(CONFIG_MACH_STM32F765) && CONFIG_MACH_STM32F765
#if !defined(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F765) \
    || !CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F765 \
    || !defined(CONFIG_STM32_USB_PA11_PA12) \
    || !CONFIG_STM32_USB_PA11_PA12 \
    || CONFIG_CLOCK_FREQ != 216000000u
#error "STM32F765 RNG identity requires its 216 MHz USB identity gate"
#endif
#elif defined(CONFIG_MACH_STM32F207) && CONFIG_MACH_STM32F207
#if !defined(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F207) \
    || !CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F207 \
    || !defined(CONFIG_STM32_USB_PA11_PA12) \
    || !CONFIG_STM32_USB_PA11_PA12 \
    || CONFIG_CLOCK_FREQ != 120000000u
#error "STM32F207 RNG identity requires its 120 MHz USB identity gate"
#endif
#elif !(((defined(CONFIG_MACH_STM32F405) && CONFIG_MACH_STM32F405) \
         || (defined(CONFIG_MACH_STM32F407) && CONFIG_MACH_STM32F407) \
         || (defined(CONFIG_MACH_STM32F429) && CONFIG_MACH_STM32F429)) \
        && defined(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F4X5) \
        && CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F4X5 \
        && defined(CONFIG_STM32_USB_PA11_PA12) \
        && CONFIG_STM32_USB_PA11_PA12 \
        && CONFIG_CLOCK_FREQ == 168000000u)
#error "STM32 RNG identity requires F207 USB at 120 MHz, F405/F407/F429 USB at 168 MHz, G431/G474 USB at 170 MHz, or F765 USB at 216 MHz"
#endif

#if !defined(CONFIG_KLIPPER_MMU_LANE_COUNT) \
    || !(((defined(CONFIG_KLIPPER_MMU_DEVICE_CUSTOM) \
           && CONFIG_KLIPPER_MMU_DEVICE_CUSTOM \
           && CONFIG_KLIPPER_MMU_LANE_COUNT == 1) \
          || (defined(CONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL) \
              && CONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL \
              && CONFIG_KLIPPER_MMU_LANE_COUNT == 4)) \
         && (!defined(CONFIG_KLIPPER_MMU_POWERED_REWIND) \
             || !CONFIG_KLIPPER_MMU_POWERED_REWIND) \
         && (!defined(CONFIG_KLIPPER_MMU_DIFF_BUFFER) \
             || !CONFIG_KLIPPER_MMU_DIFF_BUFFER))
#error "STM32 RNG startup requires Custom/1 or NightOwl/4 passive switched-buffer configuration"
#endif

#include "board/irq.h"
#include "board/misc.h"
#include "sched.h"
#include "stm32/internal.h"
#include "klipper_mmu/types.h"
#include "boot_identity.h"

#define KM_RNG_ERRORS (RNG_SR_CECS | RNG_SR_SECS | RNG_SR_CEIS | RNG_SR_SEIS)
#define KM_RNG_POLL_US 100u
#define KM_RNG_TIMEOUT_US 10000u

#ifndef KM_RNG_H7
#define KM_RNG_H7 0
#endif

#if KM_RNG_H7
// Pinned stm32h7.c: HSE/DIVM1 gives a 4 MHz PLL1 input (5 MHz for 25 MHz
// crystals), the VCO runs at twice CONFIG_CLOCK_FREQ with P1=2, D1CPRE=1,
// HPRE=2, and USB selects HSI48. RNGSEL keeps its reset value, so the RNG
// kernel clock is that same HSI48. Nothing here writes a clock selector.
#if CONFIG_STM32_CLOCK_REF_25M
#define KM_RNG_PLL_BASE_HZ 5000000u
#else
#define KM_RNG_PLL_BASE_HZ 4000000u
#endif
#define KM_RNG_PLL_VCO_HZ (CONFIG_CLOCK_FREQ * 2u)
#define KM_RNG_PLL_M (CONFIG_CLOCK_REF_FREQ / KM_RNG_PLL_BASE_HZ)
#define KM_RNG_PLL_N (KM_RNG_PLL_VCO_HZ / KM_RNG_PLL_BASE_HZ)
#define KM_RNG_PLL_READY RCC_CR_PLL1RDY
#define KM_RNG_SWS_PLL RCC_CFGR_SWS_PLL1
#define KM_RNG_CKSEL_FIELDS (RCC_PLLCKSELR_PLLSRC_Msk | RCC_PLLCKSELR_DIVM1_Msk)
#define KM_RNG_CKSEL_EXPECTED \
    (RCC_PLLCKSELR_PLLSRC_HSE | (KM_RNG_PLL_M << RCC_PLLCKSELR_DIVM1_Pos))
#define KM_RNG_PLLCFG_FIELDS \
    (RCC_PLLCFGR_PLL1RGE_Msk | RCC_PLLCFGR_PLL1VCOSEL \
     | RCC_PLLCFGR_PLL1FRACEN | RCC_PLLCFGR_DIVP1EN)
#define KM_RNG_PLLCFG_EXPECTED \
    ((2u << RCC_PLLCFGR_PLL1RGE_Pos) | RCC_PLLCFGR_DIVP1EN)
#define KM_RNG_DIVR_FIELDS (RCC_PLL1DIVR_N1_Msk | RCC_PLL1DIVR_P1_Msk)
#define KM_RNG_DIVR_EXPECTED \
    (((KM_RNG_PLL_N - 1u) << RCC_PLL1DIVR_N1_Pos) \
     | (1u << RCC_PLL1DIVR_P1_Pos))
#define KM_RNG_D1_FIELDS (RCC_D1CFGR_D1CPRE_Msk | RCC_D1CFGR_HPRE_Msk)
#define KM_RNG_CCIP_FIELDS (RCC_D2CCIP2R_RNGSEL_Msk | RCC_D2CCIP2R_USBSEL_Msk)

_Static_assert(CONFIG_CLOCK_REF_FREQ % KM_RNG_PLL_BASE_HZ == 0u
               && KM_RNG_PLL_M >= 1u && KM_RNG_PLL_M <= 63u,
               "STM32H7 RNG PLL1 input divider is not exact");
_Static_assert(KM_RNG_PLL_VCO_HZ % KM_RNG_PLL_BASE_HZ == 0u
               && KM_RNG_PLL_N >= 4u && KM_RNG_PLL_N <= 512u,
               "STM32H7 RNG PLL1 multiplier is not exact");
#else
#define KM_RNG_PLL_READY RCC_CR_PLLRDY
#define KM_RNG_SWS_PLL RCC_CFGR_SWS_PLL

#if CONFIG_STM32_CLOCK_REF_INTERNAL
#define KM_RNG_PLL_REF_HZ 16000000u
#define KM_RNG_PLL_SOURCE RCC_PLLCFGR_PLLSRC_HSI
#define KM_RNG_SOURCE_READY RCC_CR_HSIRDY
#else
#define KM_RNG_PLL_REF_HZ CONFIG_CLOCK_REF_FREQ
#define KM_RNG_PLL_SOURCE RCC_PLLCFGR_PLLSRC_HSE
#define KM_RNG_SOURCE_READY RCC_CR_HSERDY
#endif

#if CONFIG_MACH_STM32G431 || CONFIG_MACH_STM32G474
#define KM_RNG_PLL_M ((KM_RNG_PLL_REF_HZ / 4000000u) - 1u)
#define KM_RNG_PLL_N ((CONFIG_CLOCK_FREQ * 2u) / 4000000u)
#define KM_RNG_PLL_FIELDS (RCC_PLLCFGR_PLLSRC_Msk | RCC_PLLCFGR_PLLM_Msk \
                           | RCC_PLLCFGR_PLLN_Msk | RCC_PLLCFGR_PLLR_Msk \
                           | RCC_PLLCFGR_PLLREN)
#define KM_RNG_PLL_EXPECTED (KM_RNG_PLL_SOURCE \
    | (KM_RNG_PLL_M << RCC_PLLCFGR_PLLM_Pos) \
    | (KM_RNG_PLL_N << RCC_PLLCFGR_PLLN_Pos) | RCC_PLLCFGR_PLLREN)
_Static_assert(KM_RNG_PLL_REF_HZ % 4000000u == 0u,
               "STM32G4 PLL input divider is not exact");
_Static_assert(KM_RNG_PLL_M <= 15u && KM_RNG_PLL_N >= 24u
               && KM_RNG_PLL_N <= 127u,
               "STM32G4 PLL M/N field outside range");
#else
#if CONFIG_MACH_STM32F207 || (CONFIG_STM32_CLOCK_REF_25M && !CONFIG_MACH_STM32F765)
#define KM_RNG_PLL_BASE_HZ 1000000u
#else
#define KM_RNG_PLL_BASE_HZ 2000000u
#endif

#define KM_RNG_PLL_VCO_HZ (CONFIG_CLOCK_FREQ * 2u)
#define KM_RNG_PLL_M (KM_RNG_PLL_REF_HZ / KM_RNG_PLL_BASE_HZ)
#define KM_RNG_PLL_N (KM_RNG_PLL_VCO_HZ / KM_RNG_PLL_BASE_HZ)
#define KM_RNG_PLL_Q (KM_RNG_PLL_VCO_HZ / 48000000u)
#if CONFIG_MACH_STM32F765
#define KM_RNG_PLL_Q_EXPECTED 9u
#elif CONFIG_MACH_STM32F207
#define KM_RNG_PLL_Q_EXPECTED 5u
#else
#define KM_RNG_PLL_Q_EXPECTED 7u
#endif
#define KM_RNG_PLL_FIELDS \
    (RCC_PLLCFGR_PLLM_Msk | RCC_PLLCFGR_PLLN_Msk | RCC_PLLCFGR_PLLP_Msk \
     | RCC_PLLCFGR_PLLSRC_Msk | RCC_PLLCFGR_PLLQ_Msk)
#define KM_RNG_PLL_EXPECTED \
    ((KM_RNG_PLL_M << RCC_PLLCFGR_PLLM_Pos) \
     | (KM_RNG_PLL_N << RCC_PLLCFGR_PLLN_Pos) \
     | KM_RNG_PLL_SOURCE \
     | (KM_RNG_PLL_Q << RCC_PLLCFGR_PLLQ_Pos))

_Static_assert(KM_RNG_PLL_REF_HZ % KM_RNG_PLL_BASE_HZ == 0u,
               "STM32 RNG PLL input divider is not exact");
_Static_assert(KM_RNG_PLL_REF_HZ / KM_RNG_PLL_BASE_HZ >= 2u
               && KM_RNG_PLL_REF_HZ / KM_RNG_PLL_BASE_HZ <= 63u,
               "STM32 RNG PLL M field outside range");
_Static_assert(KM_RNG_PLL_VCO_HZ % KM_RNG_PLL_BASE_HZ == 0u,
               "STM32 RNG PLL N divider is not exact");
_Static_assert(KM_RNG_PLL_VCO_HZ % 48000000u == 0u
               && KM_RNG_PLL_Q == KM_RNG_PLL_Q_EXPECTED,
               "STM32 RNG identity requires an exact 48 MHz PLLQ");
_Static_assert(KM_RNG_PLL_VCO_HZ >= 100000000u
               && KM_RNG_PLL_VCO_HZ <= 432000000u,
               "STM32 RNG PLL VCO outside supported range");

#if CONFIG_MACH_STM32F765
// Pinned stm32f7.c selects PLLSAIP for the shared USB/SDMMC/RNG clock.
// Its 2 MHz PLLSAI input, N=96 and P=4 produce exactly 48 MHz.
#define KM_RNG_SAI_FIELDS \
    (RCC_PLLSAICFGR_PLLSAIN_Msk | RCC_PLLSAICFGR_PLLSAIP_Msk \
     | RCC_PLLSAICFGR_PLLSAIQ_Msk)
#define KM_RNG_SAI_EXPECTED \
    ((96u << RCC_PLLSAICFGR_PLLSAIN_Pos) \
     | (1u << RCC_PLLSAICFGR_PLLSAIP_Pos) \
     | (4u << RCC_PLLSAICFGR_PLLSAIQ_Pos))
#endif
#endif
#endif

enum km_rng_phase {
    KM_RNG_WAIT_CLOCK = 0,
    KM_RNG_WAIT_FIRST,
    KM_RNG_WAIT_SECOND,
    KM_RNG_WAIT_THIRD,
    KM_RNG_READY,
    KM_RNG_FAILED,
};

struct km_rng_identity {
    uint64_t identity;
    uint32_t deadline;
    uint32_t previous;
    uint32_t high;
    km_result_t error;
    enum km_rng_phase phase;
    uint8_t clock_owned;
    uint8_t published;
    uint8_t shutdown;
    uint8_t timer_on;
    uint8_t deadline_set;
};

static struct km_rng_identity identity = { .error = KM_OK };
static struct task_wake identity_wake;
static struct timer identity_timer;

void km_bi_rng_init(void);
void km_bi_rng_task(void);
void km_bi_rng_shutdown(void);

DECL_INIT(km_bi_rng_init);
DECL_TASK(km_bi_rng_task);
DECL_SHUTDOWN(km_bi_rng_shutdown);

static uint_fast8_t
km_bi_rng_on_timer(struct timer *timer)
{
    (void)timer;
    identity.timer_on = 0;
    sched_wake_task(&identity_wake);
    return SF_DONE;
}

static void
km_bi_rng_disarm(void)
{
    if (!identity.timer_on)
        return;
    identity.timer_on = 0;
    sched_del_timer(&identity_timer);
}

static void
km_bi_rng_arm(void)
{
    uint32_t now;
    uint32_t when;

    if (identity.timer_on || identity.phase == KM_RNG_READY
        || identity.phase == KM_RNG_FAILED)
        return;
    now = timer_read_time();
    when = now + timer_from_us(KM_RNG_POLL_US);
    if (!timer_is_before(now, when))
        when = now + 1u;
    identity_timer.waketime = when;
    identity.timer_on = 1;
    sched_add_timer(&identity_timer);
}

static void
km_bi_rng_release_clock(void)
{
    irqstatus_t flags;

    if (!identity.clock_owned)
        return;
    flags = irq_save();
    RNG->CR = 0u;
    RCC->AHB2ENR &= ~RCC_AHB2ENR_RNGEN;
    (void)RCC->AHB2ENR;
    identity.clock_owned = 0;
    irq_restore(flags);
}

static void
km_bi_rng_fail(km_result_t error)
{
    if (identity.phase == KM_RNG_FAILED || identity.phase == KM_RNG_READY)
        return;
    km_bi_rng_release_clock();
    if (identity.error == KM_OK)
        identity.error = error;
    identity.phase = KM_RNG_FAILED;
    km_bi_rng_disarm();
}

static uint8_t
km_bi_rng_expired(void)
{
    return !timer_is_before(timer_read_time(), identity.deadline);
}

/* Check Klipper's pinned PLL setup and the selected 48 MHz source before
 * connecting the RNG clock. F2/F4 use main PLLQ; F765 selects PLLSAIP;
 * H743/H750 share USB's HSI48 through RNGSEL=0. */
#if KM_RNG_H7
static uint8_t
km_bi_rng_pll48_ready(void)
{
    uint32_t cr = RCC->CR;

    if (!(cr & RCC_CR_PLL1ON) || !(cr & RCC_CR_PLL1RDY)
        || !(cr & RCC_CR_HSERDY)
        || !(cr & RCC_CR_HSI48ON) || !(cr & RCC_CR_HSI48RDY)
        || (RCC->CFGR & RCC_CFGR_SWS_Msk) != RCC_CFGR_SWS_PLL1)
        return 0;
    return (RCC->PLLCKSELR & KM_RNG_CKSEL_FIELDS) == KM_RNG_CKSEL_EXPECTED
        && (RCC->PLLCFGR & KM_RNG_PLLCFG_FIELDS) == KM_RNG_PLLCFG_EXPECTED
        && (RCC->PLL1DIVR & KM_RNG_DIVR_FIELDS) == KM_RNG_DIVR_EXPECTED
        && (RCC->D1CFGR & KM_RNG_D1_FIELDS) == RCC_D1CFGR_HPRE_DIV2
        && (RCC->D2CCIP2R & KM_RNG_CCIP_FIELDS) == RCC_D2CCIP2R_USBSEL;
}
#else
static uint8_t
km_bi_rng_pll48_ready(void)
{
    uint32_t cr = RCC->CR;
    uint32_t cfgr = RCC->CFGR;
    uint32_t pll = RCC->PLLCFGR;

    if (!(cr & RCC_CR_PLLON) || !(cr & RCC_CR_PLLRDY)
        || !(cr & KM_RNG_SOURCE_READY)
        || (cfgr & RCC_CFGR_SWS_Msk) != RCC_CFGR_SWS_PLL)
        return 0;
    if ((pll & KM_RNG_PLL_FIELDS) != KM_RNG_PLL_EXPECTED)
        return 0;
#if CONFIG_MACH_STM32G431 || CONFIG_MACH_STM32G474
    if (!(RCC->CRRCR & RCC_CRRCR_HSI48ON)
        || !(RCC->CRRCR & RCC_CRRCR_HSI48RDY)
        || (RCC->CCIPR & RCC_CCIPR_CLK48SEL_Msk) != 0u)
        return 0;
    return 1;
#elif CONFIG_MACH_STM32F765
    if (!(cr & RCC_CR_PLLSAIRDY)
        || !(RCC->DCKCFGR2 & RCC_DCKCFGR2_CK48MSEL))
        return 0;
    return (RCC->PLLSAICFGR & KM_RNG_SAI_FIELDS) == KM_RNG_SAI_EXPECTED;
#else
    return 1;
#endif
}
#endif

static void
km_bi_rng_enable_rng(void)
{
    irqstatus_t flags;

    if (km_bi_rng_expired()) {
        km_bi_rng_fail(KM_ERR_TIMEOUT);
        return;
    }
    if (!km_bi_rng_pll48_ready()) {
        /* Once Klipper has switched to PLL, a mismatched divider is terminal;
         * before that switch, allow its normal clock setup to finish. */
        if ((RCC->CR & KM_RNG_PLL_READY)
            && (RCC->CFGR & RCC_CFGR_SWS_Msk) == KM_RNG_SWS_PLL) {
            km_bi_rng_fail(KM_ERR_CONFLICT);
            return;
        }
        km_bi_rng_arm();
        return;
    }
    if (km_bi_rng_expired()) {
        km_bi_rng_fail(KM_ERR_TIMEOUT);
        return;
    }
    flags = irq_save();
    if (RCC->AHB2ENR & RCC_AHB2ENR_RNGEN) {
        irq_restore(flags);
        km_bi_rng_fail(KM_ERR_CONFLICT);
        return;
    }
    RCC->AHB2ENR |= RCC_AHB2ENR_RNGEN;
    (void)RCC->AHB2ENR;
    identity.clock_owned = 1;
    RNG->CR = RNG_CR_RNGEN;
    irq_restore(flags);
    identity.phase = KM_RNG_WAIT_FIRST;
    km_bi_rng_arm();
}

static int
km_bi_rng_read(uint32_t *word)
{
    uint32_t status;

    if (km_bi_rng_expired()) {
        km_bi_rng_fail(KM_ERR_TIMEOUT);
        return -1;
    }
    status = RNG->SR;
    if (status & KM_RNG_ERRORS) {
        km_bi_rng_fail(KM_ERR_FAULTED);
        return -1;
    }
    if (!(status & RNG_SR_DRDY)) {
        km_bi_rng_arm();
        return 0;
    }
    if (km_bi_rng_expired()) {
        km_bi_rng_fail(KM_ERR_TIMEOUT);
        return -1;
    }
    status = RNG->SR;
    if (status & KM_RNG_ERRORS) {
        km_bi_rng_fail(KM_ERR_FAULTED);
        return -1;
    }
    if (!(status & RNG_SR_DRDY)) {
        km_bi_rng_arm();
        return 0;
    }
    *word = RNG->DR;
    status = RNG->SR;
    if (status & KM_RNG_ERRORS) {
        km_bi_rng_fail(KM_ERR_FAULTED);
        return -1;
    }
    if (km_bi_rng_expired()) {
        km_bi_rng_fail(KM_ERR_TIMEOUT);
        return -1;
    }
    return 1;
}

void
km_bi_rng_init(void)
{
    identity.phase = KM_RNG_WAIT_CLOCK;
    identity_timer.func = km_bi_rng_on_timer;
    sched_wake_task(&identity_wake);
}

void
km_bi_rng_task(void)
{
    uint32_t word;
    int got;
    irqstatus_t flags;

    if (!sched_check_wake(&identity_wake))
        return;
    if (identity.phase == KM_RNG_READY || identity.phase == KM_RNG_FAILED)
        return;
    if (identity.shutdown || sched_is_shutdown()) {
        km_bi_rng_fail(KM_ERR_FAULTED);
        return;
    }
    if (!identity.deadline_set) {
        identity.deadline = timer_read_time() + timer_from_us(KM_RNG_TIMEOUT_US);
        identity.deadline_set = 1;
    }
    if (identity.phase == KM_RNG_WAIT_CLOCK) {
        km_bi_rng_enable_rng();
        return;
    }
    got = km_bi_rng_read(&word);
    if (got != 1)
        return;
    if (identity.phase == KM_RNG_WAIT_FIRST) {
        /* Startup discard and continuous random-number test: retain the
         * first word only to compare with the next word. */
        identity.previous = word;
        identity.phase = KM_RNG_WAIT_SECOND;
        km_bi_rng_arm();
        return;
    }
    if (word == identity.previous) {
        km_bi_rng_fail(KM_ERR_FAULTED);
        return;
    }
    if (identity.phase == KM_RNG_WAIT_SECOND) {
        identity.high = word;
        identity.previous = word;
        identity.phase = KM_RNG_WAIT_THIRD;
        km_bi_rng_arm();
        return;
    }
    if (identity.phase != KM_RNG_WAIT_THIRD) {
        km_bi_rng_fail(KM_ERR_FAULTED);
        return;
    }
    flags = irq_save();
    if (identity.shutdown || sched_is_shutdown() || km_bi_rng_expired()
        || (RNG->SR & KM_RNG_ERRORS)) {
        irq_restore(flags);
        km_bi_rng_fail(identity.shutdown || sched_is_shutdown()
                        ? KM_ERR_FAULTED
                        : (km_bi_rng_expired() ? KM_ERR_TIMEOUT : KM_ERR_FAULTED));
        return;
    }
    identity.identity = ((uint64_t)identity.high << 32) | (uint64_t)word;
    if (identity.identity == 0u) {
        irq_restore(flags);
        km_bi_rng_fail(KM_ERR_FAULTED);
        return;
    }
    identity.published = 1;
    identity.phase = KM_RNG_READY;
    irq_restore(flags);
    km_bi_rng_release_clock();
    km_bi_rng_disarm();
}

void
km_bi_rng_shutdown(void)
{
    identity.shutdown = 1;
    if (identity.phase != KM_RNG_READY) {
        if (identity.error == KM_OK)
            identity.error = KM_ERR_FAULTED;
        identity.phase = KM_RNG_FAILED;
    }
    km_bi_rng_release_clock();
    km_bi_rng_disarm();
}

KM_API km_result_t
km_boot_identity_get(uint64_t *out)
{
    irqstatus_t flags;
    km_result_t result;
    uint64_t value;

    if (!out)
        return KM_ERR_INVALID_PARAM;
    flags = irq_save();
    result = identity.error;
    if (identity.shutdown || sched_is_shutdown()) {
        if (result == KM_OK)
            result = KM_ERR_FAULTED;
    } else if (result == KM_OK && identity.published
               && identity.phase == KM_RNG_READY) {
        value = identity.identity;
        irq_restore(flags);
        *out = value;
        return KM_OK;
    } else if (result == KM_OK) {
        result = KM_ERR_UNAVAILABLE;
    }
    irq_restore(flags);
    return result;
}
