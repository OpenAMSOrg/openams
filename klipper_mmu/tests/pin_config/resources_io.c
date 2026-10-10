// SPDX-License-Identifier: GPL-3.0-or-later
// Read-only route tests must never touch clock, registers or GPIO.
#include "stm32/internal.h"
#include "gpio.h"
#include "board/io.h"
#include "board/irq.h"
#include "sched.h"
#include <stdlib.h>
TIM_TypeDef pair_test_timers[18];
uint32_t get_pclock_frequency(uint32_t base) { (void)base; abort(); }
int is_enabled_pclock(uint32_t base) { (void)base; abort(); }
void enable_pclock(uint32_t base) { (void)base; abort(); }
void gpio_peripheral(uint32_t pin, uint32_t fn, int pull) {
    (void)pin; (void)fn; (void)pull; abort();
}
struct gpio_out gpio_out_setup(uint32_t pin, uint32_t val) {
    (void)pin; (void)val; abort();
}
void writel(void *addr, uint32_t value) { (void)addr; (void)value; abort(); }
uint32_t readl(const void *addr) { (void)addr; abort(); }
irqstatus_t irq_save(void) { abort(); }
void irq_restore(irqstatus_t flag) { (void)flag; abort(); }
uint8_t sched_is_shutdown(void) { abort(); }
_Noreturn void shutdown(const char *message) { (void)message; abort(); }
/* Startup only records the callback. Invoking it, or any clock/timer/scheduler
 * entry used by setup, is a route-test failure. */
void register_shutdown(void (*fn)(void)) { (void)fn; }
struct timer;
void sched_add_timer(struct timer *timer) { (void)timer; abort(); }
void sched_del_timer(struct timer *timer) { (void)timer; abort(); }
uint32_t timer_read_time(void) { abort(); }
uint32_t timer_from_us(uint32_t us) { (void)us; abort(); }
