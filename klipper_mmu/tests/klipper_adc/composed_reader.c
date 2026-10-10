// SPDX-License-Identifier: GPL-3.0-or-later
/* Composes the actual shared ADC driver, MCU reader and portable pair core.
 * ADCs start enabled. Analog calibration and read-clear circuitry are not
 * emulated; the test supplies completion register states explicitly. */
#include "autoconf.h"
#include "internal.h"
#include "board/irq.h"
#include "sched.h"
#include "adc.h"
#include "clock.h"
#if CONFIG_WANT_ADC_LOCAL
#include "stm32/adc_local.h"
#endif
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

ADC_TypeDef test_adcs[3];
ADC_Common_TypeDef test_common[2];
static unsigned irq_state, io_calls, mux_calls, inject, shutdown_state;
static uint32_t ticks = 10000;
static enum km_clock_state clock_state = KM_CLOCK_STATE_READY;
static struct timer *active_timer;
static void (*shutdown_hooks[4])(void);
static unsigned hook_count;
void register_shutdown(void (*fn)(void)) {
    assert(hook_count < 4); shutdown_hooks[hook_count++] = fn;
}
irqstatus_t irq_save(void) { unsigned p=irq_state; irq_state=1; return p; }
void irq_restore(irqstatus_t p) { irq_state=p; }
uint8_t sched_is_shutdown(void) { return (uint8_t)shutdown_state; }
uint32_t timer_read_time(void) { return ticks; }
uint32_t timer_from_us(uint32_t us) { return us * 520u; }
void sched_add_timer(struct timer *t) {
    assert(irq_state && !shutdown_state && !active_timer); active_timer=t;
}
void sched_del_timer(struct timer *t) { if (active_timer==t) active_timer=NULL; }
int is_enabled_pclock(uint32_t base) { (void)base; ++io_calls; return 1; }
void enable_pclock(uint32_t base) { (void)base; ++io_calls; }
static void run_shutdown(void) {
    unsigned prev=irq_save(); shutdown_state=1;
    for (unsigned i=0;i<hook_count;++i) shutdown_hooks[i]();
    irq_restore(prev);
}
_Noreturn void shutdown(const char *message) {
    fprintf(stderr,"Unexpected upstream shutdown: %s\n",message); abort();
}
void gpio_peripheral(uint32_t pin, uint32_t fn, int32_t pull) {
    (void)pin; (void)fn; (void)pull;
    assert(!irq_state); ++io_calls; ++mux_calls;
    if (inject==1 && mux_calls==1) run_shutdown();
    if (inject==2 && mux_calls==2) {
        ADC1->SQR1=4u<<ADC_SQR1_SQ1_Pos;
        ADC1->CR|=ADC_CR_ADSTART;
        run_shutdown();
    }
    if (inject==3 && mux_calls==1) clock_state=KM_CLOCK_STATE_FAULTED;
}
enum km_clock_state km_clock_get_state(void) { return clock_state; }
km_result_t km_clock_get_time(km_core_timepoint_t *out) {
    if (clock_state!=KM_CLOCK_STATE_READY) return KM_ERR_FAULTED;
    *out=(km_core_timepoint_t){.ms=ticks/520000u,.us=ticks/520u}; return KM_OK;
}
km_result_t km_clock_deadline_ticks(uint32_t us,uint32_t *out,bool *due) {
    if (clock_state!=KM_CLOCK_STATE_READY) return KM_ERR_FAULTED;
    *out=us*520u; *due=(int32_t)(*out-ticks)<=0; return KM_OK;
}
/* Truthful ceil(ticks/520) of the current fixture clock. Not an integer-floor completion. */
km_result_t km_clock_get_completion_us(uint32_t *out_us) {
    if (!out_us) return KM_ERR_INVALID_PARAM;
    if (clock_state!=KM_CLOCK_STATE_READY) return KM_ERR_FAULTED;
    *out_us=ticks/520u+(ticks%520u?1u:0u); return KM_OK;
}
static void dispatch(void) {
    assert(active_timer); struct timer *t=active_timer; ticks=t->waketime;
    unsigned prev=irq_save(); unsigned r=t->func(t); irq_restore(prev);
    if (r==SF_DONE) active_timer=NULL;
}
static const km_adc_pair_config_t config = {
    .max_raw=4095,.conversion_timeout_us=1000,
    .max_pair_span_us=2500,.max_sample_age_us=5000,
};
static void expect_start(uint32_t a,uint32_t b,km_result_t expected) {
    km_result_t r=km_adc_start(a,b,&config,100000u);
    if (r!=expected) fprintf(stderr,"start(%u,%u): %d, expected %d\n",a,b,r,expected);
    assert(r==expected); assert(!irq_state);
}
static void no_io_start(uint32_t a,uint32_t b,km_result_t expected) {
    unsigned before=io_calls; expect_start(a,b,expected);
    assert(io_calls==before && !active_timer);
}
static void sample_and_stop(void) {
    expect_start(36,37,KM_OK);
    dispatch(); assert(ADC1->CR & ADC_CR_ADSTART);
    ADC1->CR=ADC_CR_ADEN; ADC1->ISR=ADC_ISR_EOC; ADC1->DR=1234;
    dispatch(); /* Consume A. */
    ADC1->ISR=0; /* Model the hardware's DR read-clear effect. */
    dispatch(); assert(ADC1->CR & ADC_CR_ADSTART);
    ADC1->CR=ADC_CR_ADEN; ADC1->ISR=ADC_ISR_EOC; ADC1->DR=2345;
    dispatch(); ADC1->ISR=0;
    km_adc_pair_sample_t s;
    assert(km_adc_get_sample(&s)==KM_OK && s.is_valid);
    assert(s.raw_a==1234 && s.raw_b==2345 && s.present_mask==3);
    assert(km_adc_stop()==KM_OK && !active_timer);
    assert(km_adc_get_sample(&s)==KM_ERR_UNAVAILABLE && !s.is_valid);
}
int main(int argc,char **argv) {
    assert(argc==2);
    for (unsigned i=0;i<3;++i) test_adcs[i].CR=ADC_CR_ADEN;
    const char *test=argv[1];
    if (!strcmp(test,"control")) {
        expect_start(36,37,KM_OK); assert(active_timer && mux_calls==2);
        assert(km_adc_stop()==KM_OK && !active_timer);
    } else if (!strcmp(test,"off")) {
        no_io_start(36,37,KM_ERR_UNAVAILABLE);
        assert(km_adc_get_state()==KM_ADC_STATE_STOPPED);
    } else if (!strcmp(test,"sample")) sample_and_stop();
#if CONFIG_WANT_ADC_LOCAL
    else if (!strcmp(test,"ordinary-owned")) {
        (void)gpio_adc_setup(91);
        no_io_start(36,37,KM_ERR_BUSY);
        assert(km_adc_get_state()==KM_ADC_STATE_STOPPED);
    } else if (!strcmp(test,"local-owned")) {
        struct gpio_adc a,b; assert(gpio_adc_setup_local_pair(36,37,&a,&b)==0);
        unsigned before=io_calls; expect_start(36,37,KM_OK);
        assert(io_calls==before && active_timer);
    } else if (!strcmp(test,"restart")) {
        expect_start(36,37,KM_OK); km_adc_stop(); unsigned before=io_calls;
        expect_start(36,37,KM_OK); assert(active_timer && io_calls==before);
    } else if (!strcmp(test,"rebind")) {
        expect_start(36,37,KM_OK); km_adc_stop();
        no_io_start(37,36,KM_ERR_BUSY);
        no_io_start(91,36,KM_ERR_BUSY);
        no_io_start(93,94,KM_ERR_BUSY);
        unsigned before=io_calls; expect_start(36,37,KM_OK);
        assert(io_calls==before && active_timer);
    } else if (!strcmp(test,"adc2")) expect_start(93,94,KM_OK);
    else if (!strcmp(test,"adc3")) expect_start(83,84,KM_OK);
    else if (!strcmp(test,"invalid")) {
        const uint32_t bad[][2]={{36,36},{36,93},{255,36},{36,UINT32_MAX},
                                {254,89},{0x10024,37},{36,0x10025}};
        for (unsigned i=0;i<sizeof(bad)/sizeof(bad[0]);++i)
            no_io_start(bad[i][0],bad[i][1],KM_ERR_INVALID_PARAM);
        expect_start(36,37,KM_OK);
    } else if (!strcmp(test,"shutdown-first") || !strcmp(test,"shutdown-second")) {
        inject=!strcmp(test,"shutdown-first")?1:2;
        expect_start(36,37,KM_ERR_FAULTED);
        assert(!active_timer && km_adc_get_state()==KM_ADC_STATE_SHUTDOWN);
        if (inject==2) assert(!(ADC1->CR & ADC_CR_ADSTART) && (ADC1->CR & ADC_CR_ADSTP));
        shutdown_state=0; no_io_start(36,37,KM_ERR_FAULTED);
    } else if (!strcmp(test,"clock-loss")) {
        inject=3; expect_start(36,37,KM_ERR_FAULTED); assert(!active_timer);
    } else if (!strcmp(test,"active-stop") || !strcmp(test,"active-shutdown")) {
        expect_start(36,37,KM_OK); dispatch(); assert(ADC1->CR & ADC_CR_ADSTART);
        if (!strcmp(test,"active-stop")) km_adc_stop(); else run_shutdown();
        assert(!active_timer && !(ADC1->CR & ADC_CR_ADSTART) && (ADC1->CR & ADC_CR_ADSTP));
    }
#endif
    else { fprintf(stderr,"Unknown test: %s\n",test); return 2; }
    puts(test); return 0;
}
