// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef DIAG_FIXTURE_SCHED_H
#define DIAG_FIXTURE_SCHED_H
#include <stdint.h>
struct timer {
    struct timer *next;
    uint_fast8_t (*func)(struct timer *);
    uint32_t waketime;
};
struct task_wake { uint8_t wake; };
enum { SF_DONE = 0, SF_RESCHEDULE = 1 };
void sched_add_timer(struct timer *t);
void sched_del_timer(struct timer *t);
void sched_wake_task(struct task_wake *w);
uint8_t sched_check_wake(struct task_wake *w);
uint8_t sched_is_shutdown(void);
void fixture_register_task(void (*fn)(void));
void fixture_register_shutdown(void (*fn)(void));
#define DECL_TASK(fn) \
    static void fixture_ctor_task_##fn(void) __attribute__((constructor, used)); \
    static void fixture_ctor_task_##fn(void) { fixture_register_task(fn); }
#define DECL_SHUTDOWN(fn) \
    static void fixture_ctor_shut_##fn(void) __attribute__((constructor, used)); \
    static void fixture_ctor_shut_##fn(void) { fixture_register_shutdown(fn); }
#endif
