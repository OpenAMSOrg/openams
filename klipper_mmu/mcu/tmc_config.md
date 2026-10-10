# Saved TMC2209 settings

The enabled H723 pin menu contains **Feeder TMC2209 register settings**. It saves
one UART address per lane plus shared sense resistance, motor/board current
ceiling, microsteps, interpolation, stealthChop, transaction timeout and diagnostic
interval. Run/hold targets use the existing motion/driver defaults menu. Pulse
rates and acceleration remain expressed in configured microsteps; changing
microsteps requires reviewing motion and travel calibration.

Addresses are comma-separated digits `0` through `3`, without spaces, one per
configured lane. Each must match that driver's address wiring. Separate UART pins
can use the same address; this configuration does not create a multidrop bus.
Four AFC Lite addresses default to zero and sense resistance to 110 milliohms,
following AFC's `templates/AFC_Turtle_1.cfg`. Custom boards need explicit values.
Saved overrides persist when selecting another board.

The current ceiling defaults to zero, meaning **unset**, except the BoxTurtle
preset, which uses 800 mA so it matches that preset's run-current default and
the register factory can validate. Replace 800 with the fitted motor and board
limit before enabling motion. The register controller caps its supported range
at 2000 mA and also checks what the selected sense resistor can represent.
Run/hold targets must fit both limits. A zero hold target is not a startup register setting in this driver;
motor disable uses ENABLE. Quantized nominal and requested current remain distinct
from real measured current, which requires a separate sensor/backend.

`km_config_tmc()` returns an array of `km_tmc_uart_config_t`.
It validates every lane before publishing the array, zeroes unused lanes, and
preserves output on failure. Disabled pin configuration or an unset resistor or
current ceiling returns `KM_ERR_UNAVAILABLE`. Invalid addresses, register choices,
current values and timing combinations return `KM_ERR_INVALID_PARAM`.
Only one candidate occupies temporary storage; all lane addresses are validated
before output is written. MCU callers should keep the output array in persistent
storage so it does not add to the nested register-validation stack requirement.

The pure `km_tmc2209_config_validate()` and
`km_tmc_uart_config_validate()` use the same checks as actual initialization.
They perform no I/O. Register preparation is shared rather than reproduced in the
menu factory. The shared transport requires an integral-MHz clock and exact 40000
baud, a transaction budget of at least 264 bit periods, and diagnostic interval
at least as long as the transaction timeout. The fixed two-read diagnostic
stale bound is the interval plus the retry budget of both reads (every permitted
attempt and its 256-bit post-error quiet guard); all converted raw
tick deadlines must fit below the signed half range. Pure validation accepts
1/2/20/520 MHz and rejects 12.288 MHz; this is not a claim that an H723 runs at
every accepted model clock. Menu configuration cannot override the contract. Timing defaults are software policy, not physical execution measurements.

This factory does not configure a pin, start a UART transaction, mark a driver
ready or enable a motor. The future board initializer must validate this result
together with pin, feeder-timing and complete resource assignments, initialize the
physical transport, and wait for actual register confirmation. GPIO pull-up and
electrical suitability belong to that physical transport. ACTUATION remains zero
and no production startup caller is supplied by this change.

The default-off **Local shared TMC UART driver binding** option requires the
explicit upstream local API. It controls compilation of the callable binding
and fixed-current feeder. The pure saved-configuration factory remains available
without that option; saved register settings do not prove a driver was initialized.

## Selector axis TMC2209

`km_config_selector_tmc()` is the same pure, no-I/O factory for the one
selector-axis TMC2209 on its own dedicated single-wire UART (`klipper_mmu/
selector.h`; `KLIPPER_MMU_SELECTOR_UART`, `mcu/Kconfig.pins`). It shares the
gear's sense resistor, current ceiling, register microsteps, interpolation,
stealthChop and UART timing (`KLIPPER_MMU_TMC_RSENSE`/`_CURRENT_CEILING`/
`_MICROSTEPS`/`_INTERPOLATE`/`_STEALTHCHOP`/`_TIMEOUT_US`/`_DIAGNOSTIC_US`):
those describe the fitted driver chip and board, not which axis it drives.
Its own settings are `KLIPPER_MMU_SELECTOR_UART_ADDRESS` (default 0, `mcu/
Kconfig.tmc`), `KLIPPER_MMU_SELECTOR_UART_PULLUP` (default off) and the
existing `KLIPPER_MMU_SELECTOR_RUN_CURRENT`/`_HOLD_CURRENT` (Happy Hare's own
per-device selector stepper values, cited in `mcu/Kconfig.tmc`'s help text).
Disabled selector stepper, or an unset shared resistor/ceiling, returns
`KM_ERR_UNAVAILABLE`; an invalid combination returns `KM_ERR_INVALID_PARAM`,
exactly like `km_config_tmc()`. As with the gear, this is a saved register
setting, not a claim that hardware exists or was initialized.

The selector can also share the gear's own physical UART wire, exactly as
Happy Hare's classic EASY-BRD board wires ERCF 1.1's selector to the *same*
pin as the gear, at a different address (`installer/boards/Kconfig.easy_brd_rp2040`
in the pinned oracle). Set `KLIPPER_MMU_PINS_SELECTOR_UART` equal to
`KLIPPER_MMU_PINS_UART` and give `KLIPPER_MMU_SELECTOR_UART_ADDRESS` a value
different from the gear's own `KLIPPER_MMU_TMC_ADDRESSES`; `mcu/pin_config.c`
and `mcu/tmc_uart_gpio.c` are the two places that accept that one sharing (and
still refuse any other pin reuse, including equal addresses on this one wire).
Build with the selector on its own separate pin instead, or leave
`KLIPPER_MMU_SELECTOR_UART` off for a plain STEP/DIR selector, when your board
does not use EASY-BRD's shared-wire wiring.
