// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <stdint.h>
#define DECL_INIT(fn)
#define DECL_TASK(fn)
#define DECL_SHUTDOWN(fn)
struct timer { struct timer *next; uint_fast8_t (*func)(struct timer *); uint32_t waketime; };
struct task_wake { uint8_t wake; };
enum { SF_DONE = 0, SF_RESCHEDULE = 1 };
void sched_add_timer(struct timer *t);
void sched_del_timer(struct timer *t);
void sched_wake_task(struct task_wake *w);
uint8_t sched_check_wake(struct task_wake *w);
uint8_t sched_is_shutdown(void);
