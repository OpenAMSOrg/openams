// SPDX-License-Identifier: GPL-3.0-or-later
/* Stand-ins for mcu/stepdir_gpio.c's driver-to-stepper accessors, for suites
 * that link mcu/controller.c without linking mcu/stepdir_gpio.c itself (a
 * mock or other test-double driver is bound instead). They delegate to the
 * stub local stepper table (see stub_stepper.h and stepper_local_ref.c), so
 * the controller sees a real, stepping local stepper for any non-NULL bound
 * driver, exactly as a WANT_GPIO_LOCAL slot would. Do not link this alongside
 * the real mcu/stepdir_gpio.c: it already defines both functions. */
#include "stepper.h"
#include "stub_stepper.h"
#include "stepdir_gpio.h"

struct stepper *km_stepdir_gpio_get_stepper_for_driver(const km_driver_t *d)
{
    return stub_stepper_for_driver(d);
}

int64_t km_stepdir_gpio_get_position_for_driver(const km_driver_t *d)
{
    return stub_stepper_position_for_driver(d);
}

/* Mock drivers have no DIR polarity: forward is dir 1. */
int km_stepdir_gpio_queue(const km_driver_t *d, uint32_t start_clock,
                          uint32_t interval, uint16_t count, int16_t add,
                          uint8_t forward)
{
    struct stepper *st = km_stepdir_gpio_get_stepper_for_driver(d);
    return st ? stepper_local_queue(st, start_clock, interval, count, add, forward) : -1;
}
