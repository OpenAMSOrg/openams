// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_SCHED_H
#define TEST_SCHED_H

#include <stdint.h>

struct timer {
    struct timer *next;
    uint_fast8_t (*func)(struct timer*);
    uint32_t waketime;
};

enum { SF_DONE = 0, SF_RESCHEDULE = 1 };

struct task_wake {
    uint8_t wake;
};

void sched_add_timer(struct timer *t);
void sched_del_timer(struct timer *del);
void sched_wake_task(struct task_wake *w);
uint8_t sched_check_wake(struct task_wake *w);
uint8_t sched_is_shutdown(void);
void shutdown(const char *msg);
void try_shutdown(const char *msg);

typedef void (*shim_func_void_t)(void);

void shim_register_init(shim_func_void_t f);
void shim_register_task(shim_func_void_t f);
void shim_register_shutdown(shim_func_void_t f);

#define _TEST_PASTE_SCHED(a, b) a##b
#define TEST_PASTE_SCHED(a, b) _TEST_PASTE_SCHED(a, b)

#define DECL_INIT(FUNC) \
    static void __attribute__((constructor)) TEST_PASTE_SCHED(_reg_init_, __LINE__)(void) { \
        shim_register_init(FUNC); \
    } _Static_assert(1, "registration")

#define DECL_TASK(FUNC) \
    static void __attribute__((constructor)) TEST_PASTE_SCHED(_reg_task_, __LINE__)(void) { \
        shim_register_task(FUNC); \
    } _Static_assert(1, "registration")

#define DECL_SHUTDOWN(FUNC) \
    static void __attribute__((constructor)) TEST_PASTE_SCHED(_reg_shutdown_, __LINE__)(void) { \
        shim_register_shutdown(FUNC); \
    } _Static_assert(1, "registration")

#endif
