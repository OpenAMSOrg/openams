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
extern bool test_shim_is_shutdown;
extern int test_shim_shutdown_count;
extern char test_shim_shutdown_msg[128];
extern bool test_shim_use_longjmp;
extern jmp_buf test_shim_shutdown_jump;
extern uint32_t test_shim_clock_reads_in_shutdown;
extern bool test_shim_in_shutdown;

/* GPIO diagnostics and controls */
#define SHIM_MAX_PINS 256
extern uint32_t shim_gpio_setup_count[SHIM_MAX_PINS];
extern uint32_t shim_gpio_write_count[SHIM_MAX_PINS];
extern uint32_t shim_gpio_current_level[SHIM_MAX_PINS];
extern uint32_t shim_gpio_last_write_tick[SHIM_MAX_PINS];
extern bool shim_gpio_is_setup[SHIM_MAX_PINS];
extern uint32_t shim_gpio_fail_pin;

/* Write execution cost / clock jump controls */
extern uint32_t shim_gpio_write_delay_ticks;
extern uint32_t shim_gpio_write_delay_pin;
extern uint32_t shim_gpio_write_clock_jump_ticks;
extern uint32_t shim_gpio_write_clock_jump_pin;

/* Transition event log */
typedef struct {
    uint32_t pin;
    uint32_t val;
    uint32_t tick;
    bool is_setup;
} shim_gpio_event_t;

#define SHIM_GPIO_MAX_EVENTS 512
extern shim_gpio_event_t shim_gpio_events[SHIM_GPIO_MAX_EVENTS];
extern uint32_t shim_gpio_events_count;
void shim_gpio_clear_events(void);

/* Lifecycle controls */
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
