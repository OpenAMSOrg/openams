// SPDX-License-Identifier: GPL-3.0-or-later
// The handful of Klipper scheduler entry points sim/board/plant.c links
// against but never actually needs serviced for this test (see run.py):
// no timer this test arms is ever due, and DECL_INIT is a no-op in the
// shim, so each test drives km_sim_plant_notify_write()/_init() directly
// and owns its own fake clock.
#include <stdint.h>
#include "sched.h"

void sched_add_timer(struct timer *t) { (void)t; }
uint8_t timer_is_before(uint32_t a, uint32_t b) { return (int32_t)(a - b) < 0; }

uint32_t km_sim_fake_now;
uint32_t timer_read_time(void) { return km_sim_fake_now; }
