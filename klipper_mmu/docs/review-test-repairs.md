# Review test repairs

Rechecked against package HEAD `099970e`. These failures were fixture drift;
the production switch, GPIO ownership, and diagnostic startup behavior did not
need changes.

## Reproduced failures and causes

`make -f tests/klipper_switch/Makefile test` failed in switch scenario 13,
`virtual_hub_input`. The test initialized an EMU profile after preparing a
one-lane fixture, but `km_profile_init_emu()` resets `lane_count` to five. The
remaining bay entries were zero, so setup correctly rejected duplicate pin
assignments. The scenario is specifically about the legacy one-lane virtual
hub projection, which the core documents as unambiguous. The fixture now sets
the EMU profile's lane count to one before `km_core_init`; the existing expected
setup/read counts of five remain correct (bay, toolhead, two switched-buffer
inputs, and virtual-hub load input). Assertions were not weakened.

`python3 tests/klipper_board_gpio_local/run_tests.py --results <new-dir>` failed
at compile time because `test_batch.c` includes
`klipper_board_startup/test_board_startup.c`, which defines its own
`gpio_local_reserve`, then defines the distinct local-ownership model with the
same name. The included fixture implementation is now renamed only within that
include; the board GPIO-local product still links to and exercises the local
model.

`cd tests/klipper_startup_composition && make test` initially failed on
duplicate symbols from the included board fixture, a missing shim
`DECL_INIT`, an unguarded inline timer helper, and the expected provider-gate
`#error`. The fixture's overlapping timer/scheduler/GPIO and PWM service symbols
are now locally renamed while included. The timer helper is inside its header
guard, and the shim declares `DECL_INIT`. The fixture now supplies the explicit
H723 boot-identity config symbol and uses H723's 520 MHz clock setting, so the
real diagnostic-startup source compiles under its verified-target gate. This
does not change the tested lifecycle or claim a new hardware qualification.

## Fresh verification

All commands below were run from the repository root unless stated otherwise.

```sh
make -f tests/klipper_switch/Makefile test
```

Result: 14 switch input/controller scenarios passed, including scenario 13.

```sh
python3 tests/klipper_board_gpio_local/run_tests.py --results build/review-board-gpio-fixed
```

Result: compile passed and all 40 cases passed. Results are in
`build/review-board-gpio-fixed/summary.json`.

```sh
cd tests/klipper_startup_composition && make test
```

Result: compile passed and all nine composition cases passed (`ready`,
`missing_calibration`, `fault`, `timeout`, `powered_ready`,
`powered_timer_conflict`, `powered_conflict`, `powered_partial`,
`powered_fault`). Results are under `build/klipper_startup_composition/`.

No hardware was accessed. No production behavior assertions were relaxed.
