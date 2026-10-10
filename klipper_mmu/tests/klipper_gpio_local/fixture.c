// SPDX-License-Identifier: GPL-3.0-or-later
#include <assert.h>
#include <setjmp.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "autoconf.h"
#if CONFIG_MACH_STM32F4
#include "stm32f4xx.h"
#else
#include "stm32h7xx.h"
#endif
#include "board/irq.h"
#include "gpio.h"
#include "internal.h"
#include "gpio_local.h"

GPIO_TypeDef review_ports[9];
static unsigned irq_mask, clock_calls, global_shutdown, shutdown_calls;
static const char *last_shutdown;
static jmp_buf escape;
irqstatus_t irq_save(void) { unsigned old=irq_mask;irq_mask=1;return old; }
void irq_restore(irqstatus_t value) { irq_mask=value; }
uint8_t sched_is_shutdown(void) { return global_shutdown; }
void gpio_clock_enable(GPIO_TypeDef *p) { assert(p>=review_ports && p<review_ports+9);clock_calls++; }
_Noreturn void review_shutdown(const char *s) { shutdown_calls++;last_shutdown=s;longjmp(escape,1); }
struct image { GPIO_TypeDef regs[9];unsigned clocks; };
static struct image snapshot(void) { struct image i;memcpy(i.regs,review_ports,sizeof(review_ports));i.clocks=clock_calls;return i; }
static void unchanged(struct image *i) { assert(!memcmp(i->regs,review_ports,sizeof(review_ports)) && i->clocks==clock_calls); }
static void ordinary(uint32_t pin,int kind) {
    if(kind==0) (void)gpio_out_setup(pin,1);
    else if(kind==1) (void)gpio_in_setup(pin,1);
    else gpio_peripheral(pin,GPIO_FUNCTION(4),-1);
}
#if CONFIG_WANT_GPIO_LOCAL
void review_run_shutdown(void);
static void local(uint32_t pin,int kind) {
    if(kind==0) (void)gpio_out_setup_local(pin,1);
    else if(kind==1) (void)gpio_in_setup_local(pin,1);
    else gpio_peripheral_local(pin,GPIO_FUNCTION(4),-1);
}
static void must_refuse(uint32_t pin,int kind,int local_call) {
    struct image before=snapshot();unsigned saved_irq=irq_mask;
    unsigned calls=shutdown_calls;
    if (!setjmp(escape)) { if(local_call) local(pin,kind);else ordinary(pin,kind);assert(0); }
    assert(shutdown_calls==calls+1 && last_shutdown);assert(irq_mask==saved_irq);unchanged(&before);
}
#endif
static void trace(void) {
    for(unsigned n=0;n<12;n++) {
        uint32_t pin=n+1,bit=1u<<pin;
        struct gpio_in in=gpio_in_setup(pin,1);struct gpio_out out=gpio_out_setup(pin,1);
        gpio_out_reset(out,0);assert(review_ports[0].BSRR==(bit<<16));
        gpio_out_toggle(out);
#if CONFIG_MACH_STM32F4
        assert(review_ports[0].ODR==bit);
#else
        assert(review_ports[0].BSRR==bit);
#endif
        gpio_in_reset(in,-1);gpio_out_reset(out,1);gpio_out_write(out,0);
        assert(review_ports[0].BSRR==(bit<<16));
        gpio_out_toggle_noirq(out);
#if CONFIG_MACH_STM32F4
        assert(review_ports[0].ODR==0);
#else
        assert(review_ports[0].BSRR==bit);
#endif
        review_ports[0].IDR=bit;assert(gpio_in_read(in));
        gpio_peripheral(pin,GPIO_FUNCTION(5)|GPIO_OPEN_DRAIN|GPIO_HIGH_SPEED,0);
        printf("%u %08x %08x %08x %08x %08x %08x %08x\n",clock_calls,review_ports[0].BSRR,review_ports[0].MODER,review_ports[0].PUPDR,review_ports[0].OTYPER,review_ports[0].OSPEEDR,review_ports[0].AFR[0],review_ports[0].AFR[1]);
    }
}
int main(int argc,char **argv) {
    assert(argc>=2);const char *which=argv[1];int kind=argc>2?atoi(argv[2]):0;
    if(!strcmp(which,"trace")) {trace();return 0;}
#if CONFIG_WANT_GPIO_LOCAL
    uint32_t pins[3]={5,18,33};struct image before=snapshot();
    if(!strcmp(which,"local_success")) {
        assert(gpio_local_reserve(pins,3)==GPIO_LOCAL_OK);unchanged(&before);
        for(int k=0;k<3;k++) local(pins[k],k);
        struct gpio_in in=gpio_in_setup_local(5,1);struct gpio_out out=gpio_out_setup_local(5,0);
        gpio_in_reset(in,-1);gpio_out_reset(out,1);gpio_out_write(out,0);gpio_out_toggle(out);
#if CONFIG_MACH_STM32F4
        assert(review_ports[0].BSRR==(1u<<21));
        assert(review_ports[0].ODR==(1u<<5));
#else
        assert(review_ports[0].BSRR==(1u<<5));
#endif
        review_run_shutdown();global_shutdown=0;
        gpio_in_reset(in,0);gpio_out_reset(out,0);gpio_out_toggle_noirq(out);gpio_peripheral_local(33,GPIO_ANALOG,0);
        before=snapshot();uint32_t fresh=6;assert(gpio_local_reserve(&fresh,1)==GPIO_LOCAL_ERR_SHUTDOWN);unchanged(&before);
        must_refuse(5,0,0);
    } else if(!strcmp(which,"ordinary_first")) {
        ordinary(18,kind);before=snapshot();assert(gpio_local_reserve(pins,3)==GPIO_LOCAL_ERR_BUSY);unchanged(&before);
        assert(gpio_local_reserve(pins,1)==GPIO_LOCAL_OK);
        assert(gpio_local_reserve(pins+2,1)==GPIO_LOCAL_OK);
    } else if(!strcmp(which,"local_first")) {
        assert(gpio_local_reserve(pins,1)==GPIO_LOCAL_OK);must_refuse(5,kind,0);
    } else if(!strcmp(which,"unreserved")) {must_refuse(5,kind,1);
    } else if(!strcmp(which,"invalid_local")) {must_refuse(UINT32_MAX,kind,1);
    } else if(!strcmp(which,"batch")) {
        if(kind==0) {pins[1]=pins[0];assert(gpio_local_reserve(pins,2)==GPIO_LOCAL_ERR_INVALID);}
        if(kind==1) {pins[1]=UINT32_MAX;assert(gpio_local_reserve(pins,2)==GPIO_LOCAL_ERR_INVALID);}
        if(kind==2) {
            pins[1]=
#if CONFIG_MACH_STM32F4
                144; /* No GPIOJ on F407. */
#else
                48; /* Absent GPIOD in H7 fixture. */
#endif
            assert(gpio_local_reserve(pins,2)==GPIO_LOCAL_ERR_INVALID);
        }
        if(kind==3) assert(gpio_local_reserve(NULL,1)==GPIO_LOCAL_ERR_INVALID);
        if(kind==4) assert(gpio_local_reserve(pins,0)==GPIO_LOCAL_ERR_INVALID);
        if(kind==5) assert(gpio_local_reserve(pins,145)==GPIO_LOCAL_ERR_INVALID);
        unchanged(&before);assert(gpio_local_reserve(pins,1)==GPIO_LOCAL_OK);
    } else if(!strcmp(which,"largest")) {
        pins[0]=143;assert(gpio_local_reserve(pins,1)==GPIO_LOCAL_OK);unchanged(&before);local(143,1);
    } else if(!strcmp(which,"repeat")) {
        assert(gpio_local_reserve(pins+1,1)==GPIO_LOCAL_OK);before=snapshot();
        assert(gpio_local_reserve(pins,3)==GPIO_LOCAL_ERR_BUSY);unchanged(&before);
        assert(gpio_local_reserve(pins,1)==GPIO_LOCAL_OK);
    } else if(!strcmp(which,"irq_restore")) {
        irq_mask=1;assert(gpio_local_reserve(pins,1)==GPIO_LOCAL_OK);assert(irq_mask==1);
        assert(gpio_local_reserve(pins,1)==GPIO_LOCAL_ERR_BUSY);assert(irq_mask==1);
        assert(gpio_local_reserve(NULL,1)==GPIO_LOCAL_ERR_INVALID);assert(irq_mask==1);
        local(5,0);assert(irq_mask==1);must_refuse(5,1,0);irq_mask=0;
    } else if(!strcmp(which,"global_shutdown")) {
        global_shutdown=1;int result=gpio_local_reserve(pins,3);
        printf("reserve while scheduler shutdown before local hook: %d (expected %d)\n",result,GPIO_LOCAL_ERR_SHUTDOWN);fflush(stdout);
        assert(result==GPIO_LOCAL_ERR_SHUTDOWN);unchanged(&before);
    } else if(!strcmp(which,"closed")) {
        review_run_shutdown();unchanged(&before);global_shutdown=0;
        assert(gpio_local_reserve(pins,1)==GPIO_LOCAL_ERR_SHUTDOWN);unchanged(&before);
    } else assert(0);
    assert(!irq_mask);puts("PASS");return 0;
#else
    (void)kind;assert(0);return 1;
#endif
}
