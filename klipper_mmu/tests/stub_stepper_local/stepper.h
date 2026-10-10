// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_TEST_STUB_STEPPER_H
#define KLIPPER_MMU_TEST_STUB_STEPPER_H
/* Stand-in for Klipper's WANT_STEPPER_LOCAL stepper.h declarations
 * (upstream/drivers-local.patch), for suites that link mcu/controller.c
 * against test-double drivers instead of mcu/stepdir_gpio.c and Klipper's
 * real stepper.c. The trivial bodies in stepper_local_stub.c are weak, so a
 * suite may instead link stepper_local_ref.c's strong, reference-backed
 * versions and have the local stepper really step; the
 * tests/klipper_stepdir_gpio_local runner compiles only the trivial file and
 * therefore keeps the "not a local stepper" behaviour. Suites that exercise
 * the real patched stepper.c (tests/klipper_controller, tests/klipper_stepdir)
 * must not see this header. */
#include <stdint.h>

/* As in the patch: an idle local stepper refuses a start clock closer than
 * this, which the controller treats as an underrun. */
#define STEPPER_LOCAL_MIN_LEAD_US 100

struct stepper;

int stepper_local_queue(struct stepper *s, uint32_t start_clock, uint32_t interval,
                        uint16_t count, int16_t add, uint8_t dir);
int stepper_local_room(struct stepper *s);
int32_t stepper_local_position(struct stepper *s);
int stepper_local_active(struct stepper *s);
void stepper_local_stop(struct stepper *s);

#endif
