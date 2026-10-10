// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef COMPOSITION_SCHED_H
#define COMPOSITION_SCHED_H
#include <stdint.h>
struct timer { struct timer *next; uint_fast8_t (*func)(struct timer *); uint32_t waketime; };
struct task_wake { uint8_t wake; };
enum { SF_DONE = 0, SF_RESCHEDULE = 1 };
typedef void (*shutdown_func_t)(void);
void register_shutdown(shutdown_func_t f);
#define DECL_SHUTDOWN(fn) static void __attribute__((constructor)) reg_##fn(void) { register_shutdown(fn); }
#define DECL_INIT(fn) extern void fn(void)
#define DECL_TASK(fn)
void sched_add_timer(struct timer *t);
void sched_del_timer(struct timer *t);
void sched_wake_task(struct task_wake *w);
uint8_t sched_check_wake(struct task_wake *w);
uint8_t sched_is_shutdown(void);
void sched_clear_shutdown(void);
void shutdown(const char *msg);
#endif
