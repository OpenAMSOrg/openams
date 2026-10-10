// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_SHIM_SCHED_H
#define TEST_SHIM_SCHED_H

#include <stdint.h>

uint8_t sched_is_shutdown(void);
void sched_clear_shutdown(void);
void shutdown(const char *msg);

typedef void (*shutdown_func_t)(void);
void register_shutdown(shutdown_func_t f);

#define _TEST_PASTE_SCHED(a, b) a##b
#define TEST_PASTE_SCHED(a, b) _TEST_PASTE_SCHED(a, b)
#define DECL_SHUTDOWN(fn) \
    static void __attribute__((constructor)) TEST_PASTE_SCHED(_reg_sd_, __LINE__)(void) { \
        register_shutdown(fn); \
    } _Static_assert(1, "registration")

struct task_wake {
    uint8_t wake;
};
struct timer {
    struct timer *next;
    uint_fast8_t (*func)(struct timer *);
    uint32_t waketime;
};
enum { SF_DONE = 0, SF_RESCHEDULE = 1 };

void sched_wake_task(struct task_wake *wake);
uint8_t sched_check_wake(struct task_wake *wake);
void sched_add_timer(struct timer *timer);
void sched_del_timer(struct timer *timer);
#define DECL_INIT(fn) extern void fn(void)
#define DECL_TASK(fn) extern void fn(void)

#endif
