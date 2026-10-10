// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef SHIM_TMC_H
#define SHIM_TMC_H

#include <stdint.h>
#include <stdbool.h>
#include <setjmp.h>
#include "sched.h"
#include "command.h"
#include "board/gpio.h"

#ifdef __cplusplus
extern "C" {
#endif

#define SHIM_MAX_PINS 128

typedef struct {
    bool attached;
    uint8_t address;
    uint32_t registers[128];
    bool master_driving;
    bool master_level;
    bool chip_receiving;
    unsigned chip_bit;
    uint8_t chip_byte;
    uint32_t chip_sample;
    uint8_t frame[8];
    unsigned byte_count;
    unsigned expected_bytes;
    unsigned valid_frames;
    bool reply_pending;
    uint8_t reply[8];
    uint32_t reply_start;
    uint32_t reply_release;
    bool corrupt_reply_crc;
    bool no_reply;
    unsigned corrupt_reply_count; /* Corrupt only the next N replies. */
    unsigned no_reply_count;      /* Ignore only the next N read requests. */
    bool stuck_low;
    uint32_t bit_ticks;
    uint32_t byte_started;
    uint32_t edge_history[64];
    bool edge_levels[64];
    unsigned edge_count;
} virtual_tmc_t;

/* Configurable simulated symbols */
extern uint32_t test_clock_freq;

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

/* Virtual TMC management */
void shim_attach_virtual_tmc(uint32_t pin, uint8_t address, uint32_t clock_hz);
virtual_tmc_t *shim_get_virtual_tmc(uint32_t pin);

/* Lifecycle controls */
void shim_reset(uint32_t clock_hz);
void shim_set_board_ready(bool ready);
void shim_set_ticks(uint32_t ticks);
void shim_advance_ticks(uint32_t delta);

/* Scheduler interface */
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

#endif /* SHIM_TMC_H */
