# TMC UART host regression

These runners compile host programs. They do not flash a board, talk to a printer, attach the MMU host, or authorize motion. Nothing here qualifies real hardware, and the GPIO model does not imply ownership of pins outside the cases below.

The current package pins the reviewed driver. A later test run alone does not accept any changed driver. A passing ownership run is regression coverage of those pinned sources.

Paths below are relative to the package root.

## Shared driver

`tests/klipper_tmc_uart/run_shared.py` is the existing shared TMC UART runner. It builds the shared driver with the board headers in `tests/klipper_tmc_uart/shim_include/` and checks the shared UART behavior that runner already owns. The ownership runner does not replace it.

From the package root, choose an unused results path:

```
python3 -B tests/klipper_tmc_uart/run_shared.py /tmp/mmu-shared-results
```

## Pin ownership

`tests/klipper_tmc_uart/run_ownership.py` compiles `tests/klipper_tmc_uart/ownership.c` with the shared driver from an installed tree.

From the package root, choose an unused results path:

```
python3 -B tests/klipper_tmc_uart/run_ownership.py --upstream /path/to/extended-klipper --results /tmp/mmu-ownership-results
```

`--upstream` is required. It is an installed extended Klipper checkout. Before either compile, the runner hashes `src/tmcuart.c` and `src/tmcuart.h` and compares them with the `after` sha256 values in `upstream/source-hashes.json`. The exact hashes are stored in `results.json`. A mismatch stops the run before compilation. The same checkout supplies `src/basecmd.h`, which is compiled unchanged. There is no alternate driver and no baseline mode that treats missing coverage as success.

`--results` is required and must be a path that does not exist yet. The runner creates it. Each configuration directory copies `tmcuart.c`, `tmcuart.h`, and `basecmd.h`, writes the small generated shims `autoconf.h`, `command.h`, and `sched.h`, and stores the compiled native binary plus `build.log` and one log per case. `results.json` records the run. Existing package shims are read-only.

`--package` defaults to this package root, taken from the runner file.

## F407 GPIO and TMC UART ownership

`run_f407.py` compiles the installed `src/tmcuart.c` with the installed
generic STM32 `src/stm32/gpio.c` and `gpioperiph.c` for F407. It copies those
source files unchanged into a temporary compile tree, verifies patched-file
hashes against the package manifest, and records hashes for every source. It
supplies register storage plus scheduler and IRQ shims. The GPIO setup and
peripheral functions are the actual pinned upstream implementations; no GPIO
function is stubbed. Run it with an unused
results directory:

```
python3 -B tests/klipper_tmc_uart/run_f407.py \
  --package /path/to/klipper-mmu \
  --upstream /path/to/installed-pinned-klipper \
  --results /tmp/mmu-f407-ownership-results
```

The four cases check ordinary-first and local-first TMC UART conflicts,
rejection of an unreserved local setup before GPIO register or clock changes,
and successful local setup after an explicit batch reservation. The runner
stores source hashes, compile output, per-case output, and results JSON. This
is a native register-model regression; it does not establish F407 electrical
or physical support.

Both builds use capacity 8. One sets `CONFIG_WANT_TMCUART_LOCAL` to 0 and the other sets it to 1. The off build runs 1 case (`ordinary-sharing`). The on build runs 21 cases, including `ordinary-sharing` again. Together those are 22 result keys. Every case must exit 0. A compile error, a timeout, a failed case, or any other set of keys leaves `status` as `failed` and the process exits nonzero.

`ordinary-sharing` configures `ordinary(0, 20, 21)` and `ordinary(1, 20, 21)`: two ordinary clients on the same distinct RX/TX pair. The separate `single-wire` case checks `local(20, 20)` and then rejects an overlapping local client and an overlapping ordinary client. A local channel that reuses either wire of an ordinary config, or either wire of another local channel, is rejected before GPIO setup and before another allocation. That rejection still holds after cancel, while a transfer is active, while a completion is pending, and when setup is interrupted by shutdown. Shutdown does not clear a claim that was already taken. The checks observe the driver's public calls and the GPIO, timer, and OID activity those calls perform. They do not measure a machine and they do not infer a general GPIO ownership scheme.
