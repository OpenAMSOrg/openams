# Source and licensing boundary

This optional package follows the GPL-3.0-or-later architecture agreed for
Klipper-MMU; see COPYING.

The portable trajectory, event reservation and current-control implementations
were developed here. Follower behavior and operation lifecycles follow the
OpenAMS operation model (`docs/contracts` in the openams repository). Happy
Hare and AFC have been inventoried
and analyzed; their full feature implementations have not yet been copied or
ported into this package.

Upstream Klipper integration experiments use its published command/timer/task
registration interfaces. The integration scripts and probe sources are marked
GPL-3.0-or-later. Preserve original notices and record exact source revisions
when adapting further upstream code. See docs/plan/source-inventory.json for pinned
Happy Hare, AFC and Klipper references.

`mcu/h723_resources.c` adapts PWM route data from Klipper's
`src/stm32/hard_pwm.c` at `ce7002bedf37e938bb483572949f3703ac6476cb`,
Copyright (C) 2021 Michael Kurz <michi.kurz@gmail.com>. That file retains its
GNU GPL version 3 licensing boundary and original attribution.

`mcu/tmc_uart_gpio.c` adapts the GPIO/timer integration approach from Klipper's
`src/tmcuart.c` at `ce7002bedf37e938bb483572949f3703ac6476cb`,
Copyright (C) 2018 Kevin O'Connor <kevin@koconnor.net>. That file retains its
GNU GPL version 3 licensing boundary and original attribution.
