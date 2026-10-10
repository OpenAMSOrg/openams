// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KM_SIM_TMC2209_H
#define KM_SIM_TMC2209_H
// Test-only: a pin-level TMC2209 model. See tmc2209.c and
// docs/plan/linux-mcu-integration.md, "Time, or how to bit-bang a UART on
// an operating system that naps".
#include <stdint.h>

// Called by sim/board/gpio.c on every write to any output pin, with the
// pin's new level and the current time (virtual, inside a timer callback;
// wall-anchored otherwise, same as timer_read_time()). A cheap no-op for
// any pin without an attached chip.
void km_sim_tmc2209_notify_write(uint32_t pin, uint8_t level, uint32_t time);

// The chip's own contribution to the single-wire line: 1 (released, pulled
// up) except while it is actively driving a reply. gpio_in_read wires this
// AND the MCU's own output together. 1 for any pin without an attached chip.
uint8_t km_sim_tmc2209_chip_level(uint32_t pin);

// DECL_INIT hook: attaches the four BoxTurtle/AFC Lite lane chips. Exposed
// here (rather than only linker-registered) so a native test can call it
// directly without pulling in Klipper's init-list machinery.
void km_sim_tmc2209_init(void);

// Trouble injection for the control socket ("tmc <pin> fault on|off"): report
// DRV_STATUS.ot from every later read of register 0x6f until cleared. The
// model has a register file and reply framing but no stuck-low line driver,
// so a fault is something the chip says, not something the wire does. 0 on
// success, -1 if that pin has no chip of ours attached.
int km_sim_tmc2209_set_fault(uint32_t pin, uint8_t on);

#endif
