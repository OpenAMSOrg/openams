Host orchestration for `mcu/diagnostic_startup.c`: 24 switched cases on 3 clocks (72 fresh processes), or 25 DIFF ADC cases on 3 clocks (75 fresh processes).

From the repo root:

`python3 tests/klipper_diagnostic_startup/run_tests.py --results ../build/diag-startup-results`

`python3 tests/klipper_diagnostic_startup/run_tests.py --mode diff --results ../build/diag-startup-diff-results`

`python3 tests/klipper_diagnostic_startup/run_tests.py --mode actuation --results ../build/diag-startup-actuation-results`

`python3 tests/klipper_diagnostic_startup/run_tests.py --mode actuation_diff --results ../build/diag-startup-actuation-diff-results`

`--package` defaults to this package directory (`Path(__file__).resolve().parents[2]`).
`--source` defaults to that package and replaces only `mcu/diagnostic_startup.c` and `.h`.
Common headers still come from `package/include` (also `package/mcu` and the fixture shim).

`--results` must be a new directory. An existing one is refused and left untouched.
Clocks are 1000000, 12000000, and 520000000 with a 1000000 us timeout.
Flags are `-std=c11 -Wall -Wextra -Werror -Wconversion -Wsign-conversion` plus ASan/UBSan.
Compile limit 60s. Case limit 5s. No retries.

Board startup, boot identity, clock, the controller, and command attach/permission are modeled. The actuation mode checks that only READY can grant START, while every failed, late, and shutdown path denies or revokes it. `tests/test_commands.py` separately exercises the real command permission code in opt-in and default-off native builds, including loss of physical readiness after READY.
IRQ, the scheduler, and `sendf` are mocked.
A normal pump runs the one timer only when it is due: early time moves forward to that wake, and late time stays.
After reschedule, the next wake is based on the time that callback observed.
The exact timeout tick is a separate model task wake on the saved production wake pointer, not an early hardware ISR.
No drivers, `km_start`, or hardware. A pass does not qualify a board.
`summary.json` stores fixture and production hashes, argv, return codes, timeouts, logs, `hardware_access` false, compile failures, case failures, and cases not run.
