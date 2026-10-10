// SPDX-License-Identifier: GPL-3.0-or-later
// STM32H723 one-shot boot identity. Default off. Does not admit motion.
//
// RNG_CR 0x00F00D00 and RNG_HTCR 0x0000AA74 are the complete H72x/H73x 48 MHz
// recipe in ST AN4230 Rev 14 (July 2026) section 5.1.3 Table 3. Conditioning
// bypass (CR bit 6) stays clear. The pinned HAL 3.2 HTCR 0x7274 is a different
// source value and is not used; why the two differ is not established.
// Disable, conditioning reset, immediate HTCR unlock/write, and before/after
// data-read status checks follow the ST HAL register sequence as reference
// only. No HAL library is linked. ST license: licenses/ST-BSD-3-Clause.txt.
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <stdint.h>
#include "autoconf.h"

#if !defined(CONFIG_MACH_STM32H723) || !CONFIG_MACH_STM32H723 \
    || !defined(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H723) \
    || !CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H723
#error "boot_identity_h723.c requires CONFIG_MACH_STM32H723 and CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H723"
#endif

#include "board/irq.h"
#include "board/misc.h"
#include "sched.h"
#include "stm32/internal.h"
#include "klipper_mmu/types.h"
#include "boot_identity.h"

/* AN4230 Table 3 configuration. RNGEN, IE, CED, and conditioning bypass are 0. */
#define KM_BI_CR_CFG       0x00F00D00u
#define KM_BI_HTCR_UNLOCK  0x17590ABCu
#define KM_BI_HTCR_VALUE   0x0000AA74u
#define KM_BI_SR_FAIL      (RNG_SR_CECS | RNG_SR_CEIS | RNG_SR_SECS | RNG_SR_SEIS)

enum {
    KM_BI_PH_START = 0,
    KM_BI_PH_HSI48,
    KM_BI_PH_CONDRST,
    KM_BI_PH_WORD0,
    KM_BI_PH_WORD1,
    KM_BI_PH_READY,
    KM_BI_PH_FAILED
};

struct km_bi_state {
    uint64_t identity;
    uint32_t deadline;
    uint32_t hi;
    km_result_t error;
    uint8_t phase;
    uint8_t owned;
    uint8_t published;
    uint8_t shutdown;
    uint8_t timer_on;
};

void km_bi_init(void);
void km_bi_task(void);
void km_bi_shutdown(void);
static uint_fast8_t km_bi_on_timer(struct timer *t);
static void km_bi_disarm(void);
static void km_bi_arm_poll(void);
static void km_bi_arm_wait(uint8_t phase);
static void km_bi_fail(km_result_t err);
static uint8_t km_bi_expired(void);
static void km_bi_start(void);
static void km_bi_poll_hsi48(void);
static void km_bi_poll_condrst(void);
static int km_bi_take_word(uint32_t *word);
static void km_bi_poll_word0(void);
static void km_bi_poll_word1(void);

DECL_INIT(km_bi_init);
DECL_TASK(km_bi_task);
DECL_SHUTDOWN(km_bi_shutdown);

static struct km_bi_state bi;
static struct task_wake bi_wake;
static struct timer bi_timer = { .func = km_bi_on_timer };

static uint_fast8_t
km_bi_on_timer(struct timer *t)
{
    (void)t;
    bi.timer_on = 0;
    sched_wake_task(&bi_wake);
    return SF_DONE;
}

static void
km_bi_disarm(void)
{
    if (!bi.timer_on)
        return;
    bi.timer_on = 0;
    sched_del_timer(&bi_timer);
}

static void
km_bi_arm_poll(void)
{
    uint32_t now;
    uint32_t when;

    if (bi.timer_on || bi.phase == KM_BI_PH_READY || bi.phase == KM_BI_PH_FAILED)
        return;
    now = timer_read_time();
    when = now + timer_from_us(100u);
    if (!timer_is_before(now, when))
        when = now + 1u;
    bi_timer.waketime = when;
    bi.timer_on = 1;
    sched_add_timer(&bi_timer);
}

static void
km_bi_arm_wait(uint8_t phase)
{
    uint32_t now = timer_read_time();
    bi.deadline = now + timer_from_us(2000u);
    bi.phase = phase;
    km_bi_arm_poll();
}

static void
km_bi_fail(km_result_t err)
{
    if (bi.phase == KM_BI_PH_FAILED || bi.phase == KM_BI_PH_READY)
        return;
    if (bi.owned && !bi.shutdown && !sched_is_shutdown())
        RNG->CR &= ~RNG_CR_RNGEN;
    if (bi.error == KM_OK)
        bi.error = err;
    bi.phase = KM_BI_PH_FAILED;
    km_bi_disarm();
}

static uint8_t
km_bi_expired(void)
{
    return !timer_is_before(timer_read_time(), bi.deadline);
}

static void
km_bi_start(void)
{
    irqstatus_t flags;

    if (bi.shutdown || sched_is_shutdown()) {
        if (bi.error == KM_OK)
            bi.error = KM_ERR_FAULTED;
        bi.phase = KM_BI_PH_FAILED;
        return;
    }
    flags = irq_save();
    if (is_enabled_pclock((uint32_t)RNG)) {
        irq_restore(flags);
        km_bi_fail(KM_ERR_CONFLICT);
        return;
    }
    bi.owned = 1;
    enable_pclock((uint32_t)RNG);
    RCC->CR |= RCC_CR_HSI48ON;
    irq_restore(flags);
    km_bi_arm_wait(KM_BI_PH_HSI48);
}

static void
km_bi_poll_hsi48(void)
{
    irqstatus_t flags;

    if (km_bi_expired()) {
        km_bi_fail(KM_ERR_TIMEOUT);
        return;
    }
    if ((RCC->CR & RCC_CR_HSI48RDY) == 0) {
        km_bi_arm_poll();
        return;
    }
    if (km_bi_expired()) {
        km_bi_fail(KM_ERR_TIMEOUT);
        return;
    }
    flags = irq_save();
    RCC->D2CCIP2R &= ~RCC_D2CCIP2R_RNGSEL;
    irq_restore(flags);
    RNG->CR = 0;
    RNG->CR = KM_BI_CR_CFG | RNG_CR_CONDRST;
    flags = irq_save();
    RNG->HTCR = KM_BI_HTCR_UNLOCK;
    RNG->HTCR = KM_BI_HTCR_VALUE;
    irq_restore(flags);
    RNG->CR = KM_BI_CR_CFG;
    km_bi_arm_wait(KM_BI_PH_CONDRST);
}

static void
km_bi_poll_condrst(void)
{
    uint32_t sr;

    if (km_bi_expired()) {
        km_bi_fail(KM_ERR_TIMEOUT);
        return;
    }
    sr = RNG->SR;
    if (sr & KM_BI_SR_FAIL) {
        km_bi_fail(KM_ERR_FAULTED);
        return;
    }
    if (RNG->CR & RNG_CR_CONDRST) {
        km_bi_arm_poll();
        return;
    }
    if (km_bi_expired()) {
        km_bi_fail(KM_ERR_TIMEOUT);
        return;
    }
    sr = RNG->SR;
    if (sr & KM_BI_SR_FAIL) {
        km_bi_fail(KM_ERR_FAULTED);
        return;
    }
    if (RNG->CR & RNG_CR_CONDRST) {
        km_bi_arm_poll();
        return;
    }
    RNG->CR = KM_BI_CR_CFG | RNG_CR_RNGEN;
    km_bi_arm_wait(KM_BI_PH_WORD0);
}

static int
km_bi_take_word(uint32_t *word)
{
    uint32_t sr;

    if (km_bi_expired()) {
        km_bi_fail(KM_ERR_TIMEOUT);
        return -1;
    }
    sr = RNG->SR;
    if (sr & KM_BI_SR_FAIL) {
        km_bi_fail(KM_ERR_FAULTED);
        return -1;
    }
    if ((sr & RNG_SR_DRDY) == 0) {
        km_bi_arm_poll();
        return 0;
    }
    if (km_bi_expired()) {
        km_bi_fail(KM_ERR_TIMEOUT);
        return -1;
    }
    sr = RNG->SR;
    if (sr & KM_BI_SR_FAIL) {
        km_bi_fail(KM_ERR_FAULTED);
        return -1;
    }
    if ((sr & RNG_SR_DRDY) == 0) {
        km_bi_arm_poll();
        return 0;
    }
    if (km_bi_expired()) {
        km_bi_fail(KM_ERR_TIMEOUT);
        return -1;
    }
    *word = RNG->DR;
    sr = RNG->SR;
    if (sr & KM_BI_SR_FAIL) {
        km_bi_fail(KM_ERR_FAULTED);
        return -1;
    }
    if (km_bi_expired()) {
        km_bi_fail(KM_ERR_TIMEOUT);
        return -1;
    }
    return 1;
}

static void
km_bi_poll_word0(void)
{
    uint32_t word;
    int got = km_bi_take_word(&word);

    if (got != 1)
        return;
    if (km_bi_expired()) {
        km_bi_fail(KM_ERR_TIMEOUT);
        return;
    }
    bi.hi = word;
    km_bi_arm_wait(KM_BI_PH_WORD1);
}

static void
km_bi_poll_word1(void)
{
    uint32_t word;
    uint32_t sr;
    uint64_t id;
    irqstatus_t flags;
    int got = km_bi_take_word(&word);

    if (got != 1)
        return;
    id = ((uint64_t)bi.hi << 32) | (uint64_t)word;
    if (id == 0u) {
        km_bi_fail(KM_ERR_FAULTED);
        return;
    }
    sr = RNG->SR;
    if (sr & KM_BI_SR_FAIL) {
        km_bi_fail(KM_ERR_FAULTED);
        return;
    }
    if (km_bi_expired()) {
        km_bi_fail(KM_ERR_TIMEOUT);
        return;
    }
    RNG->CR = KM_BI_CR_CFG;
    flags = irq_save();
    sr = RNG->SR;
    if (sr & KM_BI_SR_FAIL) {
        irq_restore(flags);
        km_bi_fail(KM_ERR_FAULTED);
        return;
    }
    if (km_bi_expired()) {
        irq_restore(flags);
        km_bi_fail(KM_ERR_TIMEOUT);
        return;
    }
    if (bi.shutdown || sched_is_shutdown() || bi.published) {
        if (bi.error == KM_OK)
            bi.error = KM_ERR_FAULTED;
        if (!bi.published)
            bi.phase = KM_BI_PH_FAILED;
        irq_restore(flags);
        km_bi_disarm();
        return;
    }
    bi.identity = id;
    bi.published = 1;
    bi.phase = KM_BI_PH_READY;
    irq_restore(flags);
    km_bi_disarm();
}

void
km_bi_init(void)
{
    sched_wake_task(&bi_wake);
}

void
km_bi_task(void)
{
    if (!sched_check_wake(&bi_wake))
        return;
    if (bi.phase == KM_BI_PH_READY || bi.phase == KM_BI_PH_FAILED)
        return;
    if (bi.shutdown || sched_is_shutdown()) {
        if (bi.error == KM_OK)
            bi.error = KM_ERR_FAULTED;
        bi.phase = KM_BI_PH_FAILED;
        km_bi_disarm();
        return;
    }
    switch (bi.phase) {
    case KM_BI_PH_START:
        km_bi_start();
        break;
    case KM_BI_PH_HSI48:
        km_bi_poll_hsi48();
        break;
    case KM_BI_PH_CONDRST:
        km_bi_poll_condrst();
        break;
    case KM_BI_PH_WORD0:
        km_bi_poll_word0();
        break;
    case KM_BI_PH_WORD1:
        km_bi_poll_word1();
        break;
    default:
        km_bi_fail(KM_ERR_FAULTED);
        break;
    }
}

void
km_bi_shutdown(void)
{
    bi.shutdown = 1;
    if (bi.error == KM_OK)
        bi.error = KM_ERR_FAULTED;
    if (bi.owned)
        RNG->CR &= ~RNG_CR_RNGEN;
    if (bi.phase != KM_BI_PH_READY)
        bi.phase = KM_BI_PH_FAILED;
    km_bi_disarm();
}

KM_API km_result_t
km_boot_identity_get(uint64_t *out)
{
    irqstatus_t flags;
    uint64_t id;
    km_result_t err;

    if (out == NULL)
        return KM_ERR_INVALID_PARAM;
    flags = irq_save();
    err = bi.error;
    if (bi.shutdown || sched_is_shutdown()) {
        if (err == KM_OK)
            err = KM_ERR_FAULTED;
    } else if (err == KM_OK && bi.published && bi.phase == KM_BI_PH_READY) {
        id = bi.identity;
        irq_restore(flags);
        *out = id;
        return KM_OK;
    } else if (err == KM_OK) {
        err = KM_ERR_UNAVAILABLE;
    }
    irq_restore(flags);
    return err;
}
