// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_SHIM_KLIPPER_H
#define TEST_SHIM_KLIPPER_H

#include <stdint.h>
#include <stdbool.h>
#include <setjmp.h>
#include "sched.h"
#include "command.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Configurable simulated symbols */
extern uint32_t test_clock_freq;
extern uint32_t test_device_id;
extern uint32_t test_board_id;
extern uint32_t test_lane_count;
extern uint32_t test_protocol_version;

/* Observable shim diagnostics */
extern uint32_t test_shim_ticks;
extern bool test_shim_board_ready;
extern bool test_shim_timer_armed_before_ready;
extern bool test_shim_sendf_called_in_lock;
extern bool test_shim_is_shutdown;
extern int test_shim_shutdown_count;
extern char test_shim_shutdown_msg[128];
extern char test_shim_last_sendf[256];
extern int test_shim_sendf_count;
extern int test_shim_timer_add_count;
extern struct timer *test_shim_active_timer;
extern bool test_shim_use_longjmp;
extern jmp_buf test_shim_shutdown_jump;

/* Shim lifecycle controls */
void shim_reset(uint32_t clock_hz, uint32_t device_id, uint32_t board_id, uint32_t lane_count);
void shim_set_board_ready(bool ready);
void shim_set_ticks(uint32_t ticks);
void shim_advance_ticks(uint32_t delta);
uint_fast8_t shim_dispatch_timer(void);

void shim_run_init(void);
void shim_run_task(void);
void shim_run_shutdown(void);

command_func_t shim_find_command(const char *name);

#ifdef __cplusplus
}
#endif

#endif /* TEST_SHIM_KLIPPER_H */
