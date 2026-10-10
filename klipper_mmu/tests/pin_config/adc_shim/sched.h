/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef PIN_ADC_SCHED_H
#define PIN_ADC_SCHED_H
#include <stdint.h>
struct timer {
    struct timer *next;
    uint_fast8_t (*func)(struct timer *);
    uint32_t waketime;
};
enum { SF_DONE = 0, SF_RESCHEDULE = 1 };
uint8_t sched_is_shutdown(void);
void sched_add_timer(struct timer *timer);
void sched_del_timer(struct timer *timer);
void register_shutdown(void (*fn)(void));
#define DECL_SHUTDOWN(fn) static void __attribute__((constructor)) register_##fn(void) { register_shutdown(fn); } _Static_assert(1, "register")
#endif
