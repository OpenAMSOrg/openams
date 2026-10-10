// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_SHIM_KLIPPER_H
#define TEST_SHIM_KLIPPER_H

#include <stdint.h>
#include <stdbool.h>
#include <setjmp.h>
#include "sched.h"
#include "command.h"
#include "board/gpio.h"

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
extern bool test_shim_is_shutdown;
extern int test_shim_shutdown_count;
extern char test_shim_shutdown_msg[128];
extern bool test_shim_use_longjmp;
extern jmp_buf test_shim_shutdown_jump;

/* ADC Hardware Mock Controls and Diagnostics */
extern uint32_t shim_adc_setup_fail_pin;
extern bool shim_adc_setup_a_called;
extern bool shim_adc_setup_b_called;
extern uint32_t shim_adc_delay_ticks[2];
extern uint32_t shim_adc_sample_return_ticks[2];
extern uint16_t shim_adc_raw_value[2];
extern bool shim_adc_converting[2];
extern uint32_t shim_adc_ready_at_ticks[2];
extern int shim_adc_sample_count[2];
extern int shim_adc_read_count[2];
extern int shim_adc_cancel_count[2];
extern bool shim_adc_double_read_error;
extern bool shim_adc_read_before_ready_error;
extern bool shim_adc_b_started_before_a_consumed;
extern bool shim_adc_cancel_inactive_error;

/* Callback execution cost simulation (advances test_shim_ticks during callback) */
extern uint32_t shim_adc_sample_cost_ticks[2];
extern uint32_t shim_adc_read_cost_ticks[2];

/* Promised IRQ state tracking */
extern int shim_adc_irq_state_violations;
extern bool shim_adc_setup_irq_disabled_violation;
extern bool shim_adc_callback_irq_enabled_violation;

/* Shim lifecycle and timer controls */
void shim_reset(uint32_t clock_hz, uint32_t device_id, uint32_t board_id, uint32_t lane_count);
void shim_set_board_ready(bool ready);
void shim_set_ticks(uint32_t ticks);
void shim_advance_ticks(uint32_t delta);

/* Multi-timer scheduling interface */
struct timer *shim_find_earliest_timer(void);
uint_fast8_t shim_dispatch_timer(struct timer *t);
uint_fast8_t shim_dispatch_earliest_timer(void);
int shim_active_timer_count(void);

void shim_run_init(void);
void shim_run_task(void);
void shim_run_shutdown(void);

#ifdef __cplusplus
}
#endif

#endif /* TEST_SHIM_KLIPPER_H */
