// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef AUTOCONF_H
#define AUTOCONF_H

#include <stdint.h>

// The main test build uses the extension; the equivalence pass overrides this
// with -DCONFIG_WANT_STEPPER_LOCAL=0.
#ifndef CONFIG_WANT_STEPPER_LOCAL
#define CONFIG_WANT_STEPPER_LOCAL 1
#endif
#define CONFIG_STEPPER_LOCAL_COUNT 16
#define CONFIG_STEPPER_LOCAL_MOVES 224
#define CONFIG_CLOCK_FREQ 520000000
#define CONFIG_INLINE_STEPPER_HACK 0
#define CONFIG_WANT_STEPPER_OPTIMIZED_BOTH_EDGE 0
#define CONFIG_MACH_AVR 0

#endif
