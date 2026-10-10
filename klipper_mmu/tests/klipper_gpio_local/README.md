# STM32H723 and STM32F407 GPIO local host regression

Runs the reviewed native reservation cases against unchanged sources.

```
python3 -B tests/klipper_gpio_local/run_tests.py \
  --upstream /path/to/extended/klipper \
  --results /path/to/new-results-dir

python3 -B tests/klipper_gpio_local/run_tests.py \
  --family f4 \
  --archive /path/to/klipper-ce7002bedf37e938bb483572949f3703ac6476cb.tar.gz \
  --upstream /path/to/extended/klipper \
  --results /path/to/another-new-results-dir
```

`--upstream` and `--results` are required. `--family` defaults to `h7`.
F407 requires the pinned upstream archive; the runner checks its SHA-256 before
extracting the three pristine GPIO sources. Optional `--package` is the package
root; the default is two directories above this file. `--results` must be a new
directory. A path that already exists is refused and left untouched.

Dependencies are Python 3 and gcc with AddressSanitizer and UndefinedBehaviorSanitizer. The run needs no board, serial port, or extra privileges.

Installed H723 `stm32h7_gpio.c` or F407 `gpio.c`, together with
`gpioperiph.c`, `internal.h`, and `gpio_local.h`, are checked against `after`
in `upstream/source-hashes.json` before compile. The H723 pristine trio comes
from `tests/fixtures/shared_tmcuart/src/stm32`; the F407 trio comes from the
pinned Klipper archive. Both use the fixture's pristine `gpio.h` public
structure. The baseline tree has no local header; the hashed installed
`gpio_local.h` is copied in with the feature macro off. Sources are ordinary
file copies. Nothing is rewritten.

Three separate compiles produce ordinary traces: pristine baseline, installed
feature off, and installed feature on. Those traces must match byte for byte.
Feature-off preprocessed `stm32h7_gpio.c` or `gpio.c` and `gpioperiph.c` must
match the baseline with whitespace ignored. Feature on then runs the 24 native
cases. `gpioperiph.c` is compiled again with `-Wmissing-prototypes -Werror` so
the public include stays visible. The linked build uses `-Wall -Wextra` with
`-Wno-pointer-to-int-cast` and `-Wno-unused-function` and is not `-Werror`.

Shift instrumentation is disabled in every mode (`-fno-sanitize=shift`). Unchanged upstream code evaluates signed `0x0f << af_shift`, which overflows when `af_shift` is 28. AddressSanitizer and the other UndefinedBehaviorSanitizer checks stay enabled. That is not full sanitizer coverage.

Registers, IRQ, and shutdown are in-process mocks. The test does not touch MMIO, does not qualify hardware, and does not start Klipper, firmware, or motion. Compile timeout is 30 seconds. Each trace, preprocess, and case timeout is 10 seconds. Any compile, case, equivalence, prototype, hash, or missing-case failure exits non-zero. `summary.json` plus build, trace, and case logs are written under `--results`. Cases that never run stay `ran: false` and `passed: false`.
