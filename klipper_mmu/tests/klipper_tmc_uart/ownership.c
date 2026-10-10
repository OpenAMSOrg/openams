// SPDX-License-Identifier: GPL-3.0-or-later
/* Public driver calls, real object enumeration shape, and observable GPIO/timers. */
#include "autoconf.h"
#include "tmcuart.h"
#include "board/gpio.h"
#include "board/irq.h"
#include "board/misc.h"
#include "basecmd.h"
#include "sched.h"
#include <assert.h>
#include <setjmp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

void command_config_tmcuart(uint32_t *);
void tmcuart_shutdown(void);
static union { max_align_t alignment; unsigned char bytes[1024]; } objects[8];
static void *types[8];
static unsigned allocations, setups, io_calls, timer_calls, callbacks, irq_off, stopped;
static unsigned expect_stop, inject_stop;
static jmp_buf stop_target;
static struct timer *active;
static uint32_t now=1000;
irqstatus_t irq_save(void) { unsigned p=irq_off;irq_off=1;return p; }
void irq_restore(irqstatus_t p) { irq_off=p; }
void irq_disable(void) { irq_off=1; }
void irq_enable(void) { irq_off=0; }
uint32_t timer_read_time(void) { return now; }
uint32_t timer_from_us(uint32_t us) { return us; }
uint8_t sched_is_shutdown(void) { return (uint8_t)stopped; }
void sched_wake_task(struct task_wake *w) { w->wake=1; }
uint8_t sched_check_wake(struct task_wake *w) { uint8_t p=w->wake;w->wake=0;return p; }
void sched_add_timer(struct timer *t) { assert(irq_off && !active);active=t;++timer_calls; }
void sched_del_timer(struct timer *t) { assert(irq_off);if(active==t)active=NULL;++timer_calls; }
static void stop_hooks(void) {
    irqstatus_t old=irq_save();stopped=1;active=NULL;tmcuart_shutdown();irq_restore(old);
}
_Noreturn void shutdown(const char *reason) {
    assert(expect_stop);assert(reason);stop_hooks();irq_off=0;longjmp(stop_target,1);
}
void *oid_alloc(uint8_t oid,void *type,uint16_t size) {
    assert(oid<8 && !types[oid] && size<=sizeof(objects[oid]));
    ++allocations;types[oid]=type;memset(&objects[oid],0,sizeof(objects[oid]));return objects[oid].bytes;
}
void *oid_lookup(uint8_t oid,void *type) {
    assert(oid<8 && types[oid]==type);return objects[oid].bytes;
}
void *oid_next(uint8_t *pos,void *type) {
    unsigned first=*pos==UINT8_MAX?0:(unsigned)*pos+1;
    for(unsigned i=first;i<8;++i)if(types[i]==type){*pos=(uint8_t)i;return objects[i].bytes;}
    return NULL;
}
void *command_decode_ptr(uint32_t value) { return (void *)(uintptr_t)value; }
void review_sendf(const char *format,...) { (void)format;abort(); }
struct gpio_in gpio_in_setup(uint32_t pin,int32_t pull) {
    (void)pull;assert(!irq_off);++setups;++io_calls;
    if(inject_stop){inject_stop=0;stop_hooks();}
    return (struct gpio_in){pin};
}
struct gpio_out gpio_out_setup(uint32_t pin,uint32_t value) {
    (void)value;assert(!irq_off);++setups;++io_calls;return (struct gpio_out){pin};
}
void gpio_in_reset(struct gpio_in pin,int32_t pull) { (void)pin;(void)pull;++io_calls; }
uint8_t gpio_in_read(struct gpio_in pin) { (void)pin;++io_calls;return 1; }
void gpio_out_reset(struct gpio_out pin,uint32_t value) { (void)pin;(void)value;++io_calls; }
void gpio_out_write(struct gpio_out pin,uint32_t value) { (void)pin;(void)value;++io_calls; }
void gpio_out_toggle_noirq(struct gpio_out pin) { (void)pin;++io_calls; }
static void ordinary(uint8_t oid,uint32_t rx,uint32_t tx) {
    uint32_t args[]={oid,rx,1,tx,25};command_config_tmcuart(args);assert(!irq_off);
}
#if CONFIG_WANT_TMCUART_LOCAL
static void completion(void *context,uint32_t token,int status,const uint8_t *data,uint8_t len,uint32_t tick) {
    (void)context;(void)token;(void)status;(void)data;(void)len;(void)tick;++callbacks;
}
static struct tmcuart_local *local(uint32_t rx,uint32_t tx) {
    struct tmcuart_local *p=tmcuart_local_setup(rx,tx,0,30,completion,NULL);assert(!irq_off);return p;
}
static void rejected_local(uint32_t rx,uint32_t tx) {
    unsigned io=io_calls,tm=timer_calls,al=allocations,cb=callbacks;
    assert(!local(rx,tx));assert(io==io_calls && tm==timer_calls && al==allocations && cb==callbacks);
}
static void fill_slots(unsigned count) {
    for(unsigned i=0;i<count;++i)assert(local(100+2*i,101+2*i));
    rejected_local(500,501);
}
static void rejected_ordinary(uint32_t rx,uint32_t tx) {
    unsigned count=setups,al=allocations;expect_stop=1;
    if(!setjmp(stop_target)){ordinary(7,rx,tx);assert(!"ordinary conflict must shutdown");}
    expect_stop=0;assert(setups==count && allocations==al && !irq_off && stopped);
}
#endif
int main(int argc,char **argv) {
    assert(argc==2);const char *test=argv[1];
    if(!strcmp(test,"ordinary-sharing")) {
        ordinary(0,20,21);ordinary(1,20,21);assert(setups==4 && allocations==2);
    }
#if CONFIG_WANT_TMCUART_LOCAL
    else if(!strncmp(test,"ordinary-local-",15)) {
        unsigned i=(unsigned)atoi(test+15);assert(i<4);ordinary(0,20,21);
        const uint32_t pairs[4][2]={{20,40},{40,20},{21,40},{40,21}};
        rejected_local(pairs[i][0],pairs[i][1]);fill_slots(CONFIG_TMCUART_LOCAL_MAX_CHANNELS);
    } else if(!strncmp(test,"local-local-",12)) {
        unsigned i=(unsigned)atoi(test+12);assert(i<4);assert(local(20,21));
        const uint32_t pairs[4][2]={{20,40},{40,20},{21,40},{40,21}};
        rejected_local(pairs[i][0],pairs[i][1]);fill_slots(CONFIG_TMCUART_LOCAL_MAX_CHANNELS-1);
    } else if(!strncmp(test,"local-ordinary-",15)) {
        unsigned i=(unsigned)atoi(test+15);assert(i<4);assert(local(20,21));
        const uint32_t pairs[4][2]={{20,40},{40,20},{21,40},{40,21}};
        rejected_ordinary(pairs[i][0],pairs[i][1]);
    } else if(!strcmp(test,"distinct")) {
        assert(local(20,21));ordinary(0,30,31);assert(local(40,41));
        assert(setups==6 && allocations==1);fill_slots(CONFIG_TMCUART_LOCAL_MAX_CHANNELS-2);
    } else if(!strcmp(test,"single-wire")) {
        assert(local(20,20));rejected_local(20,20);rejected_local(20,21);rejected_local(21,20);
        rejected_ordinary(20,20);
    } else if(!strcmp(test,"full-width")) {
        assert(local(20,21));assert(local(0x10014,0x10015));
        rejected_local(0x10014,1000);rejected_ordinary(1000,0x10015);
    } else if(!strcmp(test,"cancelled")) {
        struct tmcuart_local *p=local(20,21);assert(p);
        assert(tmcuart_local_cancel(p)==TMCUART_OK);rejected_local(20,21);rejected_ordinary(21,20);
    } else if(!strcmp(test,"active")) {
        struct tmcuart_local *p=local(20,21);assert(p);uint8_t write=0xff;
        assert(tmcuart_local_submit(p,123,&write,1,0)==TMCUART_OK);
        assert(active && tmcuart_local_is_busy(p));rejected_local(21,20);
        assert(active && tmcuart_local_is_busy(p));rejected_ordinary(20,21);
    } else if(!strcmp(test,"pending")) {
        struct tmcuart_local *p=local(20,21);assert(p);uint8_t write=0xff;
        assert(tmcuart_local_submit(p,123,&write,1,0)==TMCUART_OK);
        for(unsigned i=0;active;++i) {
            assert(i<64);struct timer *t=active;active=NULL;now=t->waketime;
            irqstatus_t prev=irq_save();uint_fast8_t r=t->func(t);
            if(r==SF_RESCHEDULE)active=t;else assert(r==SF_DONE);
            irq_restore(prev);
        }
        assert(tmcuart_local_is_busy(p) && !callbacks);
        rejected_local(21,20);assert(tmcuart_local_is_busy(p));rejected_ordinary(20,21);
    } else if(!strcmp(test,"partial-shutdown")) {
        inject_stop=1;assert(!local(20,21));assert(stopped && !active);
        stopped=0;rejected_local(30,31);rejected_ordinary(21,20);
    } else if(!strcmp(test,"invalid-unclaimed")) {
        unsigned io=io_calls;assert(!tmcuart_local_setup(20,21,0,25,NULL,NULL));
        assert(!tmcuart_local_setup(20,21,0,15,completion,NULL));assert(io==io_calls);
        assert(local(20,21));fill_slots(CONFIG_TMCUART_LOCAL_MAX_CHANNELS-1);
    }
#endif
    else { fprintf(stderr,"Unknown scenario %s\n",test);return 2; }
    assert(!callbacks);puts(test);return 0;
}
