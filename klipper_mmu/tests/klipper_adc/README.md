# SPDX-License-Identifier: GPL-3.0-or-later

# ADC reader and shared-driver composition

Permanent regressions compile the copied STM32H7 ADC driver with this package's MCU reader and portable pair core. Success requires all 15 actual shared-driver/reader model-composition cases. Those 15 cases are separate from the 55 permanent driver setup-deadline executions below. Coverage is software/register-model only: CMSIS `ADC_TypeDef` / `ADC_Common_TypeDef` and `ADC_` macros extracted from the upstream header, a fake clock, and explicit conversion-complete plus DR read-clear register writes. No analog calibration or electrical model.

The klipper_adc Makefile suite is 34 mock-ADC timing cases, including fractional-clock rejection, 12 MHz / 520 MHz retry coverage, and the completed-pair sampling delay. Those mocks cannot establish actual shared-unit ownership or no-rebind; the 15-case composition runner can. Neither suite measures physical timing, current, or synchronized sampling. Acceptance of iterations 139–143 is recorded in `../../docs/plan/status-history.md`.

## Prerequisites

- gcc with `-fsanitize=address,undefined`
- `--upstream`: installed Klipper checkout, used read-only. Needs `src/stm32/stm32h7_adc.c`, `src/stm32/adc_local.h`, `src/stm32/gpio.h`, and `lib/stm32h7/include/stm32h723xx.h`.
- Package `upstream/source-hashes.json`: `['src/stm32/stm32h7_adc.c']['after']` must equal the installed driver SHA-256.
- Do not install into or edit `--upstream`.

## Command

Subprocesses time out at 30 seconds. `--results` must be a directory that does not exist yet. `--package` defaults to the directory two levels above this script (the package root).

```
python3 tests/klipper_adc/run_composed.py --upstream /path/to/installed/klipper --results /tmp/klipper-adc-composed-results
```

The process exits 0 only if all 15 cases pass. `results/results.json` stores the actual source hashes, case exit codes, `coverage` of `software/register-model`, and `hardware_access: false`. Per-unit compile logs and per-case logs are kept. Sanitizer options are `halt_on_error=1` (leak detection off).

## Cases

Local API disabled (`0/off`): consumer tree has no `stm32/adc_local.h`. The shared driver still includes its own `adc_local.h`.

Local API enabled: `control`, `sample`, `ordinary-owned`, `local-owned`, `restart`, `rebind`, `adc2`, `adc3`, `invalid`, `shutdown-first`, `shutdown-second`, `clock-loss`, `active-stop`, `active-shutdown`.

## Fixture limits

ADCs start with `ADEN` set. Clock helpers are deterministic stubs. The harness writes `EOC` and data-register values and clears `ISR` to model DR read-clear. Copied driver, `adc_local.h`, and `gpio.h` bytes are checked against the upstream files. No printer, flash, or hardware access.

## Setup deadlines

`run_setup_deadlines.py` and `check_setup_deadlines.c` compile the unchanged upstream `stm32h7_adc.c` and exercise `gpio_adc_setup_local_pair_timeout`. These are the 55 permanent driver setup-deadline executions, not the 15 composition cases and not the 34 mock-ADC timing cases. Eighteen cases run at 1000000, 12000000, and 520000000 Hz, including the previous seventeen plus the largest valid timeout. One case runs at 12000001 Hz, where the new API is invalid before pin, unit, or peripheral changes and the older setup calls remain usable on an already initialized ADC.

```
python3 tests/klipper_adc/run_setup_deadlines.py --upstream /path/to/installed/klipper --results /tmp/klipper-adc-deadline-results
```

`--results` must not already exist; an existing path is refused without changes. Before compilation, `upstream/source-hashes.json` `after` hashes for `src/stm32/stm32h7_adc.c` and `src/stm32/adc_local.h` must match the installed files. A mismatch exits non-zero and does not compile. The build limit is 20 seconds and each case limit is 3 seconds, with no retries. `results/results.json` stores source, header, and fixture hashes, commands, return codes, timeouts, compile failures, cases not run, `hardware_access` false, and the model limits. This fixture leaves `CONFIG_WANT_GPIO_LOCAL` off. Calibration and ready bits move only on explicit timer observations, and W1C is applied on the next observation. It does not model enable-bit write failures, analog hardware, or shared-GPIO composition.
