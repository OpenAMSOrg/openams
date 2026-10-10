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

static struct rng_regs rng_regs;
static struct rcc_regs rcc_regs;
static uint32_t ticks, words[2], error_after_read, drift_on_read, drift_on_publish, error_on_publish;
static unsigned reads, clock_claimed, clock_enables, irq_mask, global_shutdown;
static unsigned schedules, timer_reads, status_reads;
static struct timer *pending;
static uint32_t review_sr(void);
static uint32_t review_dr(void);
#include "candidate.c"

irqstatus_t irq_save(void)
{
    unsigned prior=irq_mask;
    /* Model preemption immediately before interrupts are masked. */
    if (!prior && drift_on_publish && reads==2) {
        ticks += drift_on_publish;
        drift_on_publish=0;
    }
    if (!prior && error_on_publish && reads==2) {
        rng_regs.SR |= error_on_publish;error_on_publish=0;
    }
    irq_mask=1;
    return prior;
}
void irq_restore(irqstatus_t prior) { irq_mask=prior; }
uint32_t timer_read_time(void) { timer_reads++; return ticks; }
uint32_t timer_from_us(uint32_t us) { return us; }
uint8_t timer_is_before(uint32_t a,uint32_t b) { return (int32_t)(a-b)<0; }
uint32_t is_enabled_pclock(uint32_t peripheral) { (void)peripheral; assert(irq_mask); return clock_claimed; }
void enable_pclock(uint32_t peripheral) { (void)peripheral; assert(irq_mask && !clock_claimed); clock_claimed=1; clock_enables++; memset(&rng_regs,0,sizeof(rng_regs)); }
void sched_add_timer(struct timer *t) { assert(!pending); assert(timer_is_before(ticks,t->waketime)); pending=t; schedules++; }
void sched_del_timer(struct timer *t) { if (pending==t) pending=NULL; }
void sched_wake_task(struct task_wake *t) { t->wake=1; }
uint8_t sched_check_wake(struct task_wake *t) { unsigned v=t->wake; t->wake=0; return v; }
uint8_t sched_is_shutdown(void) { return global_shutdown; }
static uint32_t review_sr(void) { status_reads++; return rng_regs.SR; }
static uint32_t review_dr(void) {
    assert(reads<2); uint32_t value=words[reads++];
    if (error_after_read) rng_regs.SR|=error_after_read;
    ticks+=drift_on_read; drift_on_read=0;
    return value;
}
static km_result_t get(void) {
    uint64_t out=UINT64_C(0xDEADDEADDEADDEAD);
    km_result_t r=km_boot_identity_get(&out);
    if (r!=KM_OK) assert(out==UINT64_C(0xDEADDEADDEADDEAD));
    return r;
}
static void wake(void) {
    assert(pending); struct timer *t=pending; pending=NULL;
    ticks=t->waketime; unsigned before=reads;
    assert(t->func(t)==SF_DONE); assert(reads==before);
    km_bi_task(); assert(reads-before<=1); assert(!irq_mask);
}
static void start(void) {
    assert(get()==KM_ERR_UNAVAILABLE);
    km_bi_init(); assert(!timer_reads && !clock_enables && !pending);
    km_bi_task(); assert(clock_enables==1 && pending);
    assert((rcc_regs.CR & RCC_CR_HSI48ON)!=0);
    assert(get()==KM_ERR_UNAVAILABLE);
}
static void word_phase(void) {
    start(); rcc_regs.CR |= RCC_CR_HSI48RDY; wake();
    assert(rng_regs.CR==KM_BI_CR_CFG && rng_regs.HTCR==KM_BI_HTCR_VALUE);
    assert((rcc_regs.D2CCIP2R & RCC_D2CCIP2R_RNGSEL)==0);
    wake(); assert(rng_regs.CR==(KM_BI_CR_CFG|RNG_CR_RNGEN));
}
static void expect_terminal(km_result_t err) {
    assert(get()==err); assert(!pending); assert(!(rng_regs.CR&RNG_CR_RNGEN));
    unsigned n=reads, e=clock_enables, s=schedules;
    global_shutdown=0;
    for (int i=0;i<5;i++) { km_bi_init();km_bi_task();assert(get()==err); }
    assert(reads==n && clock_enables==e && schedules==s);
}
int main(int argc,char **argv) {
    assert(argc>=2); const char *name=argv[1];
    words[0]=0x81234567;words[1]=0xfedcba98;
    rcc_regs.CR=0x80000000; rcc_regs.D2CCIP2R=0xa5a503ff;
    if (!strcmp(name,"success") || !strcmp(name,"wrap") || !strcmp(name,"usb")) {
        if (!strcmp(name,"wrap")) ticks=UINT32_MAX-150;
        if (!strcmp(name,"usb")) rcc_regs.CR |= RCC_CR_HSI48ON|RCC_CR_HSI48RDY;
        word_phase();rng_regs.SR=RNG_SR_DRDY;wake();assert(get()==KM_ERR_UNAVAILABLE);wake();
        assert(reads==2);assert(get()==KM_OK);assert(!pending);assert(!(rng_regs.CR&RNG_CR_RNGEN));
        uint64_t out=0;assert(km_boot_identity_get(&out)==KM_OK && out==UINT64_C(0x81234567fedcba98));
        assert((rcc_regs.CR & 0x80000000)!=0 && rcc_regs.D2CCIP2R==0xa5a500ff);
        unsigned count=schedules;km_bi_init();km_bi_task();assert(schedules==count && reads==2);
        km_bi_shutdown();expect_terminal(KM_ERR_FAULTED);
    } else if (!strcmp(name,"foreign")) {
        clock_claimed=1; rng_regs.CR=0xabcdef01;rng_regs.HTCR=123;
        km_bi_init();km_bi_task();assert(get()==KM_ERR_CONFLICT);assert(!clock_enables && !pending);
        assert(rng_regs.CR==0xabcdef01 && rng_regs.HTCR==123 && rcc_regs.CR==0x80000000 && rcc_regs.D2CCIP2R==0xa5a503ff);
        km_bi_shutdown();assert(rng_regs.CR==0xabcdef01);
    } else if (!strcmp(name,"shutdown")) {
        unsigned phase=(unsigned)atoi(argv[2]);
        if (phase) start();
        if (phase>1) { rcc_regs.CR|=RCC_CR_HSI48RDY;wake(); }
        if (phase>2) wake();
        if (phase>3) { rng_regs.SR=RNG_SR_DRDY;wake(); }
        global_shutdown=1;km_bi_shutdown();expect_terminal(KM_ERR_FAULTED);
        if (!phase) assert(!clock_enables && !timer_reads);
    } else if (!strcmp(name,"pre_shutdown")) {
        global_shutdown=1;km_bi_init();km_bi_task();assert(!clock_enables && !timer_reads);expect_terminal(KM_ERR_FAULTED);
    } else if (!strcmp(name,"timeout")) {
        unsigned phase=(unsigned)atoi(argv[2]);start();
        if (phase>0) { rcc_regs.CR|=RCC_CR_HSI48RDY;wake(); }
        if (phase>1) wake();
        if (phase>2) { rng_regs.SR=RNG_SR_DRDY;wake(); }
        if (phase==1) rng_regs.CR|=RNG_CR_CONDRST;
        if (phase>1) rng_regs.SR=0;
        unsigned loops=0;while(pending) { assert(++loops<=20);wake(); }
        expect_terminal(KM_ERR_TIMEOUT);
    } else if (!strcmp(name,"late_ready")) {
        unsigned phase=(unsigned)atoi(argv[2]);start();
        if (phase>0) { rcc_regs.CR|=RCC_CR_HSI48RDY;wake(); }
        if (phase>1) wake();
        if (phase>2) { rng_regs.SR=RNG_SR_DRDY;wake(); }
        rcc_regs.CR|=RCC_CR_HSI48RDY;rng_regs.CR &= ~RNG_CR_CONDRST;rng_regs.SR=RNG_SR_DRDY;
        pending->waketime=bi.deadline;wake();expect_terminal(KM_ERR_TIMEOUT);
    } else if (!strcmp(name,"error")) {
        unsigned word=(unsigned)atoi(argv[2]); unsigned after=(unsigned)atoi(argv[3]); uint32_t flag=(uint32_t)strtoul(argv[4],NULL,0);
        word_phase();rng_regs.SR=RNG_SR_DRDY;
        if(word) wake();
        if(after) error_after_read=flag; else rng_regs.SR|=flag;
        wake();expect_terminal(KM_ERR_FAULTED);assert(reads==word+after);
    } else if (!strcmp(name,"zero")) {
        words[0]=0;words[1]=0;word_phase();rng_regs.SR=RNG_SR_DRDY;wake();wake();expect_terminal(KM_ERR_FAULTED);
    } else if (!strcmp(name,"read_crosses_deadline")) {
        word_phase();rng_regs.SR=RNG_SR_DRDY;wake();drift_on_read=2000;wake();expect_terminal(KM_ERR_TIMEOUT);
    } else if (!strcmp(name,"commit_error")) {
        word_phase();rng_regs.SR=RNG_SR_DRDY;wake();
        error_on_publish=(uint32_t)strtoul(argv[2],NULL,0);wake();
        expect_terminal(KM_ERR_FAULTED);
    } else if (!strcmp(name,"commit_crosses_deadline")) {
        word_phase();rng_regs.SR=RNG_SR_DRDY;wake();
        pending->waketime=bi.deadline-1;drift_on_publish=1;wake();
        printf("publication at tick %u deadline %u returned %d (timeout expected %d)\n",ticks,bi.deadline,get(),KM_ERR_TIMEOUT);fflush(stdout);
        expect_terminal(KM_ERR_TIMEOUT);
    } else { assert(0); }
    assert(km_boot_identity_get(NULL)==KM_ERR_INVALID_PARAM);
    puts("PASS");return 0;
}
