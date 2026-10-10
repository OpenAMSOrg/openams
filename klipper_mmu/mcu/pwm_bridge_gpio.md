# H723 rewind binding to Klipper's paired PWM driver

`km_pwm_bridge_gpio_setup()` connects the portable rewind controller to the
explicit shared-driver extension in Klipper's `src/stm32/hard_pwm.c`. This file
contains MMU policy and the separate motor ENABLE GPIO; timer routing, period
conversion, compare updates, output polarity and timer ownership stay in Klipper.

Build the H723 add-on with `WANT_HARD_PWM_LOCAL_PAIR` to include this binding.
Powered-rewind planning also requires `WANT_HARD_PWM_QUERY`. Configure rewind
pins, electrical active levels, requested period, duty ceiling and reversal
interval through the ordinary Klipper menu. An unset setting is unavailable;
selecting a board does not invent motor limits or output polarity.

Setup runs in task context with interrupts enabled. The ready callback is required and the fault callback is optional. With `CONFIG_WANT_GPIO_LOCAL`, `NULL` and count 0 is configured admission: saved factories still run, including `km_config_h723_resources`, and only external-list validation and inclusion are skipped. A supplied array keeps `km_resource_plan_validate` and fieldwise inclusion under both configurations. A non-NULL pointer with count 0, `NULL` with a nonzero count, or a count above `KM_RESOURCE_MAX_CLAIMS` is `KM_ERR_INVALID_PARAM`. With GPIO local off, that empty pair stays invalid and the caller supplies and enforces complete external ownership. ENABLE is made inactive first. The portable driver is published only after shared pair setup and portable initialization. Pair `BUSY` is `KM_ERR_CONFLICT` after safe-off. A partial slot stays claimed until reset. This binding reserves no pins. With `CONFIG_WANT_GPIO_LOCAL`, forward, reverse, and ENABLE must already be local before ENABLE is touched (`gpio_out_setup_local`); otherwise ordinary GPIO setup is used. Forward and reverse go through the shared pair driver and its timer exclusion. [`km_board_startup_begin`](board_startup.md) forwards the same pointer and count, including `NULL`, 0, and reserves exactly the expected-plan PIN entries once before this setup when GPIO local is enabled. Startup does not run automatically or attach commands. `ACTUATION` stays 0 as metadata; outputs start inactive. Klipper's pin resolver checks `RESERVE_PINS`, aliases, and `BUS_PINS` before deployment; these functions do not.

Runtime calls and service must be serialized by the MCU controller. Each requested
directional update carries the other direction's last successful request. Both
nonzero requests are rejected. Requests commit only after the actual driver
acknowledges the pair, its confirmed counts satisfy zero/opposing-output checks,
and a fresh local completion time is available. Tiny positive duties may round
down to zero. Current settings and duty requests are not current measurements.

Routine stop first removes ENABLE, then confirms both outputs zero, and can be
re-enabled later. The portable controller enforces the continuous zero interval
before reversal. Emergency off permanently latches the slot, removes ENABLE
before the pair's raw shutdown, and never claims a fresh completion time. A lost
clock in the scalar time callback forces physical off and invokes Klipper's
non-returning shutdown rather than inventing a timestamp or allowing false fault
recovery. Upstream shutdown also latches every claimed slot.

This replaces the unused MMU timer converter; actual driver timing is the only
source of applied timer settings. Native tests cover the portable/binding/driver
composition and fault injection, with real H723 and simulator build checks.
[`km_board_startup_begin`](board_startup.md) invokes this binding for configured powered rewind and attaches the controller only after readiness. Electrical and timing qualification remain absent.
