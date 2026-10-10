// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_TEST_STUB_STEPPER_API_H
#define KLIPPER_MMU_TEST_STUB_STEPPER_API_H
/* Test-facing view of the stub local stepper (stepper_local_ref.c). Klipper's
 * real stepper.c has no such entry points; they exist so a suite can ask the
 * same object the controller drives through
 * km_stepdir_gpio_get_stepper_for_driver() how far it actually moved, since
 * the bound mock driver's own set_step_edge() is never called any more. */
#include <stdint.h>
#include "klipper_mmu/driver.h"

struct stepper;

/* The stub stepper bound to d, created on first use. NULL for NULL and for a
 * driver the small static table has no room for. */
struct stepper *stub_stepper_for_driver(const km_driver_t *d);

/* Completed signed steps, accumulated from int32 deltas exactly as
 * mcu/stepdir_gpio.c does for a real stepper_local_position(). */
int64_t stub_stepper_position_for_driver(const km_driver_t *d);

/* Steps the reference executor has run so far for d's stepper. */
uint32_t stub_stepper_steps(const km_driver_t *d);

/* Of those, the steps that ran while d->enabled was false. The local stepper
 * is never told about ENABLE, so a disabled driver with a live queue means
 * the controller kept stepping a de-energized motor; suites assert this is 0. */
uint32_t stub_stepper_disabled_steps(const km_driver_t *d);

#endif
