// SPDX-License-Identifier: GPL-3.0-or-later
/* Native ownership integration: real pinned tmcuart + STM32 GPIO code. */
#include "autoconf.h"
#include "tmcuart.h"
#include "stm32/gpio_local.h"
#include "stm32/internal.h"
#include "board/irq.h"
#include "board/misc.h"
#include "basecmd.h"
#include "sched.h"
#include <assert.h>
#include <setjmp.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

void command_config_tmcuart(uint32_t *args);

GPIO_TypeDef ports[9];

static unsigned gpio_clock_calls, allocations, timers;
static unsigned irq_disabled, scheduler_shutdown;
static jmp_buf shutdown_target;
static int expect_shutdown;
static const char *shutdown_reason;
static uint32_t now_ticks = 1000;
static void *objects[4], *object_types[4];

irqstatus_t irq_save(void) { irqstatus_t old = irq_disabled; irq_disabled = 1; return old; }
void irq_restore(irqstatus_t old) { irq_disabled = old; }
void irq_disable(void) { irq_disabled = 1; }
void irq_enable(void) { irq_disabled = 0; }
uint32_t timer_read_time(void) { return now_ticks; }
uint32_t timer_from_us(uint32_t us) { return us; }
uint8_t sched_is_shutdown(void) { return scheduler_shutdown; }
void sched_wake_task(struct task_wake *task) { task->wake = 1; }
uint8_t sched_check_wake(struct task_wake *task) { uint8_t wake = task->wake; task->wake = 0; return wake; }
void sched_add_timer(struct timer *timer) { (void)timer; ++timers; }
void sched_del_timer(struct timer *timer) { (void)timer; ++timers; }
void gpio_clock_enable(GPIO_TypeDef *regs) { (void)regs; ++gpio_clock_calls; }

_Noreturn void shutdown(const char *reason)
{
    shutdown_reason = reason;
    if (expect_shutdown)
        longjmp(shutdown_target, 1);
    fprintf(stderr, "unexpected shutdown: %s\n", reason);
    abort();
}

void *oid_alloc(uint8_t oid, void *type, uint16_t size)
{
    assert(oid < 4 && !objects[oid] && size <= 512);
    objects[oid] = calloc(1, size);
    assert(objects[oid]);
    object_types[oid] = type;
    ++allocations;
    return objects[oid];
}
void *oid_lookup(uint8_t oid, void *type)
{
    assert(oid < 4 && object_types[oid] == type);
    return objects[oid];
}
void *oid_next(uint8_t *position, void *type)
{
    unsigned first = *position == UINT8_MAX ? 0 : (unsigned)*position + 1;
    for (unsigned i = first; i < 4; ++i) {
        if (object_types[i] == type) { *position = (uint8_t)i; return objects[i]; }
    }
    return NULL;
}
void *command_decode_ptr(uint32_t value) { return (void *)(uintptr_t)value; }
void review_sendf(const char *format, ...) { (void)format; abort(); }

static void completed(void *context, uint32_t token, int status,
                      const uint8_t *data, uint8_t length, uint32_t tick)
{
    (void)context; (void)token; (void)status; (void)data; (void)length; (void)tick;
    abort();
}

static void ordinary(uint8_t oid, uint32_t rx, uint32_t tx)
{
    uint32_t args[] = { oid, rx, 1, tx, 25 };
    command_config_tmcuart(args);
    assert(!irq_disabled);
}

static int reserve_pair(uint32_t rx, uint32_t tx)
{
    uint32_t pins[] = { rx, tx };
    return gpio_local_reserve(pins, 2);
}

static void assert_ports_equal(const GPIO_TypeDef *before)
{
    assert(!memcmp(before, ports, sizeof(ports)));
}

int main(int argc, char **argv)
{
    assert(argc == 2);
    const uint32_t rx = GPIO('C', 4), tx = GPIO('C', 5);
    if (!strcmp(argv[1], "ordinary-first")) {
        ordinary(0, rx, tx);
        GPIO_TypeDef before[9]; memcpy(before, ports, sizeof(ports));
        unsigned clocks = gpio_clock_calls, allocs = allocations;
        assert(reserve_pair(rx, tx) == GPIO_LOCAL_ERR_BUSY);
        assert(!tmcuart_local_setup(rx, tx, 1, 25, completed, NULL));
        assert(gpio_clock_calls == clocks && allocations == allocs);
        assert_ports_equal(before);
    } else if (!strcmp(argv[1], "local-first")) {
        assert(reserve_pair(rx, tx) == GPIO_LOCAL_OK);
        assert(gpio_pin_is_local(rx) && gpio_pin_is_local(tx));
        assert(tmcuart_local_setup(rx, tx, 1, 25, completed, NULL));
        GPIO_TypeDef before[9]; memcpy(before, ports, sizeof(ports));
        unsigned clocks = gpio_clock_calls, allocs = allocations;
        expect_shutdown = 1;
        if (!setjmp(shutdown_target)) {
            ordinary(0, rx, tx);
            assert(!"ordinary TMC UART must reject a local pin");
        }
        expect_shutdown = 0;
        assert(!strcmp(shutdown_reason, "tmcuart pin conflict"));
        assert(gpio_clock_calls == clocks && allocations == allocs && !irq_disabled);
        assert_ports_equal(before);
    } else if (!strcmp(argv[1], "unreserved-local")) {
        GPIO_TypeDef before[9]; memcpy(before, ports, sizeof(ports));
        assert(!gpio_pin_is_local(rx) && !gpio_pin_is_local(tx));
        assert(!tmcuart_local_setup(rx, tx, 1, 25, completed, NULL));
        assert(!gpio_clock_calls && !allocations && !timers && !irq_disabled);
        assert_ports_equal(before);
    } else if (!strcmp(argv[1], "reserved-local")) {
        assert(reserve_pair(rx, tx) == GPIO_LOCAL_OK);
        assert(tmcuart_local_setup(rx, tx, 1, 25, completed, NULL));
        assert(gpio_pin_is_local(rx) && gpio_pin_is_local(tx));
        assert(gpio_clock_calls && !irq_disabled);
        assert(((ports[2].MODER >> (4 * 2)) & 3) == GPIO_INPUT);
        assert(((ports[2].PUPDR >> (4 * 2)) & 3) == 1);
        assert(((ports[2].MODER >> (5 * 2)) & 3) == GPIO_OUTPUT);
        assert(ports[2].BSRR == (1u << 5));
    } else {
        fprintf(stderr, "unknown case: %s\n", argv[1]);
        return 2;
    }
    puts(argv[1]);
    return 0;
}
