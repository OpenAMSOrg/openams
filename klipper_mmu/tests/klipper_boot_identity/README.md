# Boot-identity regression fixtures

Host-only regressions for `mcu/boot_identity_h723.c`. The runner compiles that provider from the selected package. It does not copy the control algorithm into the tests, does not modify the package, and does not register the provider in the firmware build. These tests do not exercise hardware and do not prove entropy, freshness, automatic startup, complete resources, or full OpenAMS. Actuation evidence is none (`ACTUATION0`).

## Invocation

```sh
python3 -B klipper-mmu/tests/klipper_boot_identity/run_tests.py --results /path/to/new-results
python3 -B klipper-mmu/tests/klipper_boot_identity/run_tests.py --results "/path/with spaces/new-results" --package "/path/with spaces/klipper-mmu"
```

`--results` must be a path that does not exist yet. An existing file or directory is refused and its contents are left untouched. The default package is two directories above this test directory (`Path(__file__).resolve().parents[2]`).

## Dependencies

Python 3 standard library and host `gcc` with AddressSanitizer and UndefinedBehaviorSanitizer. No extra Python packages, no network, and no hardware. The package must already contain the sources the binaries compile. There is no fallback to an alternate production API.

Native compile uses the provider plus package headers `klipper_mmu/types.h` and `boot_identity.h`. Replay compiles:

- `src/{rate,trajectory,buffer_sensor,follower,operation_core,event_queue,device_profile,protocol_wire,current_regulator,mcu_clock}.c`
- `mcu/{clock,messages,commands,boot_identity_h723}.c`
- `tests/mock_driver.c`
- `tests/commands/capture.c`
- `tests/klipper_clock/shim_klipper.c` and `tests/klipper_clock/shim_include/autoconf.h`

The runner writes small autoconf, IRQ, scheduler, and MMIO headers into a temporary include directory. Native and replay includes are separate, so the 1 MHz register stubs cannot leak into the replay binary. The clock shim keeps its 520 MHz conversion. `timer_from_us` in the native fixture returns the microsecond count unchanged (1 MHz model).

## What is substituted

The provider file is read from the package and written only under the temporary build directory.

- Native: `RNG->SR` becomes `review_sr()` and `RNG->DR` becomes `review_dr()`. Status and data reads can inject faults and deadline drift.
- Replay: only `RNG->DR` becomes `review_dr()`. Supplied words are simulated.

Each replacement is counted and reversed in memory. A missing token, a substitution name already present in the provider, or any other text change fails that build. The original SHA-256 is recorded. It is not a lock: a repaired provider and an older provider are both valid inputs when the named tokens still exist.

## Coverage

Forty-one native register/scheduler processes, one argv case each:

| Group | Cases |
| --- | --- |
| success, wrap, usb, foreign, pre_shutdown, zero, read_crosses_deadline, commit_crosses_deadline | 8 |
| shutdown phases 0 through 4 | 5 |
| timeout phases 0 through 3, late_ready phases 0 through 3 | 8 |
| error: word 0 or 1, flag before or after the read, flags 2, 4, 32, 64 | 16 |
| commit_error flags 2, 4, 32, 64 | 4 |

Seven replay command-composition processes: `fresh`, `reused`, `zero`, `fault`, `foreign`, `timeout`, `shutdown`.

`reused` feeds the same simulated RNG pair as the existing session and must still admit that old request. `fresh` flips a high bit of the simulated 64-bit value and must refuse admission (`KM_COMMAND_INSTANCE_MISMATCH`). That pair checks the identity boundary. It says nothing about random collision quality. No physical entropy is involved.

Replay drives the provider to a terminal state on its own timer, then calls the shim init/task and `km_commands_attach`. One active timer is enough because this fixture reaches the terminal state before production clock init. The order is fixture attachment only. It is not production bootstrap and it is not an automatic-startup caller.

## Old94

The historical old94 provider failed five native cases. The other 36 native cases and all 7 composition cases passed on that tree. The runner always launches every case that its binary successfully compiled, including cases after a failure:

- `commit_error 2` (CECS)
- `commit_error 4` (SECS)
- `commit_error 32` (CEIS)
- `commit_error 64` (SEIS)
- `commit_crosses_deadline`

Those five are publication-window races: a status fault or a one-tick deadline crossing injected as interrupts are masked. `commit_crosses_deadline` prints the tick, deadline, and return code before asserting `KM_ERR_TIMEOUT`. A repaired provider may pass all 48. The suite does not treat the old failures as an acceptable skip.

## Results

On a new results directory the runner writes:

- `summary.json`: status, package path, provider hash, exact substitution maps and counts, per-case exit codes, and failed names. Stdout of cases is not inlined.
- `native-build.log` and `replay-build.log`: compiler command lines and diagnostics.
- `logs/native/<case>.log` and `logs/replay/<case>.log`: that process's combined stdout and stderr.

A compilation failure is logged, every case of that binary is recorded with `passed: false` and `ran: false`, and the process exits nonzero. Unbuilt cases are not reported as passes. A timeout, sanitizer abort, assertion failure, or exception is a failure. The temporary build directory is removed when the run ends. Results already written stay in the new directory.

Stdout is one status line plus, when needed, one line listing failed case names.

## Limits

Each compiler invocation is limited to 45 seconds. Each case process is limited to 10 seconds. Sanitizers use `-fsanitize=address,undefined -fno-sanitize-recover=all -fno-pie -no-pie` and `ASAN_OPTIONS=detect_leaks=0:halt_on_error=1`. Native warnings are `-Wall -Wextra -Werror -Wno-pointer-to-int-cast`. Replay adds `-pedantic -Wconversion -Wsign-conversion`. Commands are argument lists; the shell is not used. There is no sleep and no poll loop.

Simulated MMIO cannot show oscillator startup, RNG health on silicon, clock-tree interaction beyond the stubbed RCC bits, interrupt timing, or a real command transport. Pending outside this fixture: hardware, automatic startup, complete resources, and full OpenAMS.
