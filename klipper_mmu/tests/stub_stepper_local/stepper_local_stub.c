// SPDX-License-Identifier: GPL-3.0-or-later
/* Trivial stand-ins for stepper_local_* (see stepper.h in this directory),
 * for a build that links mcu/stepdir_gpio.c and/or mcu/controller.c without a
 * real, patched Klipper stepper.c and without stepper_local_ref.c. Callers
 * only need these to link, not to behave: mcu/stepdir_gpio.c's own
 * stepper_local_setup() is not provided here (suites that call it need the
 * real stepper.c, see tests/klipper_stepdir and tests/klipper_controller), so
 * no caller in this configuration ever obtains a non-NULL struct stepper * to
 * pass in.
 *
 * The definitions are weak so a suite that wants real stepping can link
 * stepper_local_ref.c instead; this file stays free of km_ref_* so that
 * tests/klipper_stepdir_gpio_local/run_tests.py, which compiles it alone,
 * still links without tests/stepper_reference.c. */
#include "stepper.h"

__attribute__((weak)) int stepper_local_queue(struct stepper *s, uint32_t start_clock,
                                              uint32_t interval, uint16_t count,
                                              int16_t add, uint8_t dir)
{
    (void)s; (void)start_clock; (void)interval; (void)count; (void)add; (void)dir;
    return -1;
}

__attribute__((weak)) int stepper_local_room(struct stepper *s) { (void)s; return 0; }
__attribute__((weak)) int32_t stepper_local_position(struct stepper *s) { (void)s; return 0; }
__attribute__((weak)) int stepper_local_active(struct stepper *s) { (void)s; return 0; }
__attribute__((weak)) void stepper_local_stop(struct stepper *s) { (void)s; }
