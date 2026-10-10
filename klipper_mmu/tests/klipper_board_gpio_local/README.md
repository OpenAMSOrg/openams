# GPIO-local board startup batch

Host composition tests for candidate `mcu/board_startup.c` compiled with
`-DCONFIG_WANT_GPIO_LOCAL=1`. Each case is a new process. The runner links
that candidate to the package portable core and includes
`tests/klipper_board_startup/test_board_startup.c` (its `main` is renamed).
`src/mcu_clock.c` is not linked; the fixture supplies the clock. The package
tree is not modified.

## Invocation

From the Klipper-MMU package directory:

```
python3 tests/klipper_board_gpio_local/run_tests.py --results /path/to/new-directory
```

`--package` defaults to `HERE.parents[1]`. `--source` defaults to `--package`.
The candidate is `<source>/mcu/board_startup.c` and `board_startup.h`.
Portable sources, startup shims, `mock_driver.c`, and the startup fixture
always come from `--package`.

`--results` is required and must be a new directory. If that path exists, the
runner exits 2 and writes nothing. The generated `stm32/gpio_local.h`, the
binary, logs, and `summary.json` stay under `--results`.

`summary.json` records `hardware_access` false, each command argv, sha256
hashes of the candidate, this suite, the fixture, and the linked sources, and
every case as `passed`, `failed`, or `not_run`. Compile and case logs are the
raw streams. A failure record stores the return code, timeout, and whether
each stream arrived as bytes, text, or `None`. The runner exits nonzero when
the build or any case fails, including when the compiler errors or times out.
No retries and no polls.

## Limits

One compile command, at most 60 seconds. One case, at most 10 seconds.
Flags are C11, `-Wall -Wextra -Werror -pedantic`, conversion warnings,
non-PIE, AddressSanitizer, and UndefinedBehaviorSanitizer.
`ASAN_OPTIONS=detect_leaks=0:halt_on_error=1` and
`UBSAN_OPTIONS=halt_on_error=1:abort_on_error=1`.

The public prototype in the generated header is
`int gpio_local_reserve(const uint32_t *pins, uint32_t count)` with
`GPIO_LOCAL_OK` 0, `GPIO_LOCAL_ERR_INVALID` -1, `GPIO_LOCAL_ERR_BUSY` -2,
and `GPIO_LOCAL_ERR_SHUTDOWN` -3.

## What is real, and what is a model

`mcu/board_startup.c` and the linked core are the product under test.
`gpio_local_reserve` in `test_batch.c` is only the stand-in those calls hit.
It stores full-width ids, commits a batch all at once, and has no release.
A passing case shows how this startup treats that model. It does not show
that the shared GPIO registry behaves the same way. There is no separate
model self-test in this runner.

The reserve list must be the expected plan's `KM_RESOURCE_PIN` ids in plan
order, including pin 257, and must omit ADC, timer, and the extra caller pin
planted beside the plan. The fixture's switch setup returns `KM_ERR_BUSY`
before it increments `switch_setup_count` or `gpio_ops_count`. After a
successful reserve, that busy result latches with those counters still zero;
clearing scheduler shutdown does not make startup retryable.

This runner does not open hardware. An extra pin in the claim list is only
there to show that startup must not reserve it. That does not admit an
external owner or scan the machine for other GPIO users. Startup that never
reserves fails the order case and the later cases that require the batch to
remain held. The GPIO-off suite under `tests/klipper_board_startup` is
separate; its switch-BUSY path stays retryable because that build never
reserves.

## Configured admission

`configured`, `configured_ordinary`, `configured_local`, and
`configured_deadline` call `km_board_startup_begin(NULL, 0, ...)`. They do
not pass `sim.expected_plan` as a caller list. Saved factories still produce
the MMU plan, and this model must observe exactly those expected pin ids
once. `configured` stays WAITING with no published core, then both lanes
become ready and service reaches READY. A second begin is BUSY and does not
reserve again.

`configured_ordinary` and `configured_local` preclaim one expected pin in
this model. NULL, 0 returns `KM_ERR_CONFLICT`, state stays UNINITIALIZED,
component counters stay zero, and the existing model claim is unchanged.
That is a model boundary only. Both-order registry tests elsewhere remain
authoritative for the shared GPIO registry.

`configured_deadline` moves this model's clock forward by the exact timeout
after the batch is stored. Begin returns `KM_ERR_TIMEOUT` in FAULTED, the
batch stays reserved, the core is NULL, and there is no component activity.
`sched_clear_shutdown` does not allow another start or another reserve.

## ADC budget remainder

`adc_budget` 0..4 are five more fresh processes. They use the same reserve
model, a synthetic 3-point differential calibration, and proportional buffer
with powered rewind left false. The table is test input only. It is not a
hardware curve and these cases do not prove the ADC peripheral.

A 100 us original budget loses 7 us in this model's post-reserve clock step
and 11 us in switch setup. The reader must be started with the remaining
82 us. Case 1 starts the same sequence at `UINT32_MAX - 10` and still
requires 82 us, so elapsed time is unsigned modulo from the original start.
Begin stays WAITING, not READY, and the pin batch stays reserved.

Case 2 spends 7 us plus 93 us in switch setup, so the original deadline is
exhausted before the reader exists. There is no ADC start and no ADC
cleanup. Switch shutdown runs once. Feeders, PWM, and the core are not
published. The board latches `KM_ERR_TIMEOUT` and a later begin stays
faulted with the same GPIO batch.

Case 3 captures 82 us, then the reader returns `KM_ERR_TIMEOUT` after
spending that long. Case 4 spends exactly 82 us and returns `KM_OK`; the
following check of the original deadline expires. Both shut down the owned
reader and the switch, leave the reader in `KM_ADC_STATE_SHUTDOWN`, and do
not publish feeders, PWM, or READY. Clearing scheduler shutdown does not
release the pins or allow another start.
