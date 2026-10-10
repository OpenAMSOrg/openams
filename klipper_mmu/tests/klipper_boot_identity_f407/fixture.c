// SPDX-License-Identifier: GPL-3.0-or-later
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "board/irq.h"
#include "board/misc.h"
#include "sched.h"
#include "stm32/internal.h"

struct km_test_rng_regs km_test_rng;
struct km_test_rcc_regs km_test_rcc;
static uint32_t ticks, initial_ticks, inject_after_read, status_reads, data_reads;
static unsigned irq_mask, shutting_down;
static struct timer *pending_timer;
static uint32_t review_sr(void);
static uint32_t review_dr(void);
#include "candidate.c"

irqstatus_t irq_save(void)
{
    unsigned previous = irq_mask;
    irq_mask = 1;
    return previous;
}

void irq_restore(irqstatus_t flags) { irq_mask = flags; }
uint32_t timer_read_time(void) { return ticks; }
uint32_t timer_from_us(uint32_t us) { return us * (CONFIG_CLOCK_FREQ / 1000000u); }
uint8_t timer_is_before(uint32_t a, uint32_t b) { return (int32_t)(a - b) < 0; }
void sched_add_timer(struct timer *timer) { assert(!pending_timer); pending_timer = timer; }
void sched_del_timer(struct timer *timer) { if (pending_timer == timer) pending_timer = NULL; }
void sched_wake_task(struct task_wake *wake) { wake->wake = 1; }
uint8_t sched_check_wake(struct task_wake *wake) { uint8_t set = wake->wake; wake->wake = 0; return set; }
uint8_t sched_is_shutdown(void) { return (uint8_t)(shutting_down != 0u); }

static uint32_t review_sr(void)
{
    status_reads++;
    return km_test_rng.SR;
}

static uint32_t review_dr(void)
{
    uint32_t data = km_test_rng.DR;
    data_reads++;
    km_test_rng.SR &= ~RNG_SR_DRDY;
    if (inject_after_read)
        km_test_rng.SR |= inject_after_read;
    return data;
}

static km_result_t get(uint64_t *value)
{
    uint64_t before = UINT64_C(0xDEADDEADDEADDEAD);
    km_result_t result;
    *value = before;
    result = km_boot_identity_get(value);
    if (result != KM_OK)
        assert(*value == before);
    return result;
}

static void advance(void)
{
    struct timer *timer = pending_timer;
    assert(timer);
    pending_timer = NULL;
    ticks = timer->waketime;
    assert(timer->func(timer) == SF_DONE);
    km_bi_rng_task();
    assert(!irq_mask);
}

static void init_clock(uint8_t pll_ready)
{
    memset(&km_test_rng, 0, sizeof(km_test_rng));
    memset(&km_test_rcc, 0, sizeof(km_test_rcc));
    km_test_rcc.CR = RCC_CR_HSERDY | RCC_CR_PLLON;
    if (pll_ready)
        km_test_rcc.CR |= RCC_CR_PLLRDY;
#if CONFIG_MACH_STM32G431 || CONFIG_MACH_STM32G474
    km_test_rcc.PLLCFGR = RCC_PLLCFGR_PLLSRC_HSE
        | (1u << RCC_PLLCFGR_PLLM_Pos) | (85u << RCC_PLLCFGR_PLLN_Pos)
        | RCC_PLLCFGR_PLLREN;
    km_test_rcc.CRRCR = RCC_CRRCR_HSI48ON | RCC_CRRCR_HSI48RDY;
    km_test_rcc.CCIPR = 0u;
#else
    km_test_rcc.PLLCFGR = RCC_PLLCFGR_PLLSRC_HSE
        | (CONFIG_MACH_STM32F207 ? 8u : 4u)
        | ((CONFIG_MACH_STM32F207 ? 240u : CONFIG_MACH_STM32F765 ? 216u : 168u) << 6)
        | ((CONFIG_MACH_STM32F207 ? 5u : CONFIG_MACH_STM32F765 ? 9u : 7u) << 24);
#endif
    km_test_rcc.CFGR = RCC_CFGR_SWS_PLL;
#if CONFIG_MACH_STM32F765
    km_test_rcc.CR |= RCC_CR_PLLSAIRDY;
    km_test_rcc.DCKCFGR2 = RCC_DCKCFGR2_CK48MSEL;
    km_test_rcc.PLLSAICFGR = (96u << RCC_PLLSAICFGR_PLLSAIN_Pos)
        | (1u << RCC_PLLSAICFGR_PLLSAIP_Pos)
        | (4u << RCC_PLLSAICFGR_PLLSAIQ_Pos);
#endif
    ticks = initial_ticks;
    initial_ticks = 0;
    inject_after_read = status_reads = data_reads = 0;
    shutting_down = irq_mask = 0;
    pending_timer = NULL;
}

static void start_provider(void)
{
    km_bi_rng_init();
    km_bi_rng_task();
}

static void word(uint32_t value)
{
    km_test_rng.DR = value;
    km_test_rng.SR = RNG_SR_DRDY;
    advance();
}

static void ready_prefix(void)
{
    uint64_t value;
    init_clock(1);
    start_provider();
    assert(get(&value) == KM_ERR_UNAVAILABLE);
    assert(km_test_rcc.AHB2ENR & RCC_AHB2ENR_RNGEN);
    assert(km_test_rng.CR == RNG_CR_RNGEN);
    word(0x11111111u); /* RM0090 startup word: discard, retain for comparison. */
    assert(data_reads == 1u);
    assert(get(&value) == KM_ERR_UNAVAILABLE);
    word(0x22222222u);
    assert(data_reads == 2u);
    assert(get(&value) == KM_ERR_UNAVAILABLE);
}

int main(int argc, char **argv)
{
    uint64_t value;
    assert(argc >= 2);
    if (!strcmp(argv[1], "success")) {
        ready_prefix();
        word(0x33333333u);
        assert(get(&value) == KM_OK);
        assert(value == UINT64_C(0x2222222233333333));
        assert(!(km_test_rcc.AHB2ENR & RCC_AHB2ENR_RNGEN));
        assert(km_test_rng.CR == 0u && pending_timer == NULL);
#if CONFIG_MACH_STM32G431 || CONFIG_MACH_STM32G474
        assert(km_test_rcc.CRRCR == (RCC_CRRCR_HSI48ON | RCC_CRRCR_HSI48RDY));
        assert(km_test_rcc.CCIPR == 0u);
#endif
        assert(km_boot_identity_get(NULL) == KM_ERR_INVALID_PARAM);
    } else if (!strcmp(argv[1], "repeat-first")) {
        init_clock(1);
        start_provider();
        word(0x11111111u);
        word(0x11111111u);
        assert(get(&value) == KM_ERR_FAULTED);
        assert(data_reads == 2u);
    } else if (!strcmp(argv[1], "repeat-second")) {
        ready_prefix();
        word(0x22222222u);
        assert(get(&value) == KM_ERR_FAULTED);
        assert(!(km_test_rcc.AHB2ENR & RCC_AHB2ENR_RNGEN));
#if CONFIG_MACH_STM32G431 || CONFIG_MACH_STM32G474
        assert(km_test_rcc.CRRCR == (RCC_CRRCR_HSI48ON | RCC_CRRCR_HSI48RDY));
        assert(km_test_rcc.CCIPR == 0u);
#endif
    } else if (!strncmp(argv[1], "error-before-", 13)
               || !strncmp(argv[1], "error-after-", 12)) {
        uint8_t after = !strncmp(argv[1], "error-after-", 12);
        uint32_t flag = (uint32_t)strtoul(argv[1] + (after ? 12 : 13), NULL, 0);
        ready_prefix();
        if (after) {
            inject_after_read = flag;
            word(0x33333333u);
        } else {
            km_test_rng.DR = 0x33333333u;
            km_test_rng.SR = RNG_SR_DRDY | flag;
            advance();
        }
        assert(get(&value) == KM_ERR_FAULTED);
        assert(!(km_test_rcc.AHB2ENR & RCC_AHB2ENR_RNGEN));
#if CONFIG_MACH_STM32G431 || CONFIG_MACH_STM32G474
        assert(km_test_rcc.CRRCR == (RCC_CRRCR_HSI48ON | RCC_CRRCR_HSI48RDY));
        assert(km_test_rcc.CCIPR == 0u);
#endif
    } else if (!strcmp(argv[1], "clock-conflict")) {
        init_clock(1);
        km_test_rcc.AHB2ENR = RCC_AHB2ENR_RNGEN;
        km_test_rng.CR = 0xA5u;
        start_provider();
        assert(get(&value) == KM_ERR_CONFLICT);
        assert(km_test_rcc.AHB2ENR == RCC_AHB2ENR_RNGEN);
        assert(km_test_rng.CR == 0xA5u);
#if CONFIG_MACH_STM32G431 || CONFIG_MACH_STM32G474
        assert(km_test_rcc.CRRCR == (RCC_CRRCR_HSI48ON | RCC_CRRCR_HSI48RDY));
        assert(km_test_rcc.CCIPR == 0u);
#endif
    } else if (!strncmp(argv[1], "wrong-pll", 9)) {
        init_clock(1);
#if CONFIG_MACH_STM32G431 || CONFIG_MACH_STM32G474
        if (!strcmp(argv[1], "wrong-pll"))
            km_test_rcc.PLLCFGR ^= 1u << RCC_PLLCFGR_PLLN_Pos;
        else if (!strcmp(argv[1], "wrong-pll-source"))
            km_test_rcc.PLLCFGR ^= 1u;
        else if (!strcmp(argv[1], "wrong-pll-m"))
            km_test_rcc.PLLCFGR ^= 1u << RCC_PLLCFGR_PLLM_Pos;
        else if (!strcmp(argv[1], "wrong-pll-r"))
            km_test_rcc.PLLCFGR ^= 1u << RCC_PLLCFGR_PLLR_Pos;
        else
            km_test_rcc.PLLCFGR &= ~RCC_PLLCFGR_PLLREN;
#else
        km_test_rcc.PLLCFGR &= ~RCC_PLLCFGR_PLLQ_Msk;
        km_test_rcc.PLLCFGR |= 6u << RCC_PLLCFGR_PLLQ_Pos;
#endif
        start_provider();
        assert(get(&value) == KM_ERR_CONFLICT);
        assert(km_test_rcc.AHB2ENR == 0u);
#if CONFIG_MACH_STM32G431 || CONFIG_MACH_STM32G474
        assert(km_test_rcc.CRRCR == (RCC_CRRCR_HSI48ON | RCC_CRRCR_HSI48RDY));
        assert(km_test_rcc.CCIPR == 0u);
#endif
#if CONFIG_MACH_STM32G431 || CONFIG_MACH_STM32G474
    } else if (!strcmp(argv[1], "wrong-mux")) {
        init_clock(1);
        km_test_rcc.CCIPR = 2u << 26;
        start_provider();
        assert(get(&value) == KM_ERR_CONFLICT);
        assert(km_test_rcc.AHB2ENR == 0u);
        assert(km_test_rcc.CCIPR == (2u << 26));
        assert(km_test_rcc.CRRCR == (RCC_CRRCR_HSI48ON | RCC_CRRCR_HSI48RDY));
    } else if (!strcmp(argv[1], "wrong-mux-reserved-low")) {
        init_clock(1);
        km_test_rcc.CCIPR = 1u << 26;
        start_provider();
        assert(get(&value) == KM_ERR_CONFLICT);
        assert(km_test_rcc.AHB2ENR == 0u);
        assert(km_test_rcc.CCIPR == (1u << 26));
    } else if (!strcmp(argv[1], "wrong-mux-reserved-high")) {
        init_clock(1);
        km_test_rcc.CCIPR = 3u << 26;
        start_provider();
        assert(get(&value) == KM_ERR_CONFLICT);
        assert(km_test_rcc.AHB2ENR == 0u);
        assert(km_test_rcc.CCIPR == (3u << 26));
    } else if (!strcmp(argv[1], "hsi48-off")) {
        init_clock(1);
        km_test_rcc.CRRCR = RCC_CRRCR_HSI48RDY;
        start_provider();
        assert(get(&value) == KM_ERR_CONFLICT);
        assert(km_test_rcc.AHB2ENR == 0u);
        assert(km_test_rcc.CRRCR == RCC_CRRCR_HSI48RDY);
    } else if (!strcmp(argv[1], "hsi48-not-ready")) {
        init_clock(1);
        km_test_rcc.CRRCR = RCC_CRRCR_HSI48ON;
        start_provider();
        assert(get(&value) == KM_ERR_CONFLICT);
        assert(km_test_rcc.AHB2ENR == 0u);
        assert(km_test_rcc.CRRCR == RCC_CRRCR_HSI48ON);
    } else if (!strcmp(argv[1], "late-sws-timeout")) {
        init_clock(1);
        km_test_rcc.CFGR = 0u;
        start_provider();
        while (pending_timer) advance();
        assert(get(&value) == KM_ERR_TIMEOUT);
        assert(km_test_rcc.AHB2ENR == 0u);
#endif
#if CONFIG_MACH_STM32F765
    } else if (!strcmp(argv[1], "wrong-sai")) {
        init_clock(1);
        km_test_rcc.PLLSAICFGR ^= RCC_PLLSAICFGR_PLLSAIP_Msk;
        start_provider();
        assert(get(&value) == KM_ERR_CONFLICT);
        assert(km_test_rcc.AHB2ENR == 0u);
    } else if (!strcmp(argv[1], "wrong-source")) {
        init_clock(1);
        km_test_rcc.DCKCFGR2 = 0u;
        start_provider();
        assert(get(&value) == KM_ERR_CONFLICT);
        assert(km_test_rcc.AHB2ENR == 0u);
#endif
    } else if (!strcmp(argv[1], "timeout")) {
        init_clock(0);
        start_provider();
        /* Existing timer is the next bounded poll. */
        while (pending_timer) advance();
        assert(get(&value) == KM_ERR_TIMEOUT);
        assert(!(km_test_rcc.AHB2ENR & RCC_AHB2ENR_RNGEN));
    } else if (!strcmp(argv[1], "shutdown")) {
        init_clock(1);
        start_provider();
        shutting_down = 1;
        km_bi_rng_shutdown();
        assert(get(&value) == KM_ERR_FAULTED);
        assert(!(km_test_rcc.AHB2ENR & RCC_AHB2ENR_RNGEN));
#if CONFIG_MACH_STM32G431 || CONFIG_MACH_STM32G474
        assert(km_test_rcc.CRRCR == (RCC_CRRCR_HSI48ON | RCC_CRRCR_HSI48RDY));
        assert(km_test_rcc.CCIPR == 0u);
#endif
    } else if (!strcmp(argv[1], "wrap")) {
        initial_ticks = UINT32_MAX - 1000u;
        ready_prefix();
        word(0x33333333u);
        assert(get(&value) == KM_OK);
        assert(value == UINT64_C(0x2222222233333333));
    } else {
        assert(0 && "unknown fixture case");
    }
    puts("PASS");
    return 0;
}
