# Software checks

Run the portable suite from this package's root with `make check`. It runs the
root native ASan/UBSan tests, the registered dependency-free native Makefile
suites, the registered host-compiled adapter fixtures, and every registered
portable `test_*.py` module. Each Python test module runs in its own process so
duplicate module names cannot collide. A discovered test module, runner, or
Makefile without an explicit registry entry fails the aggregate instead of
being assumed covered.

This package is the `klipper_mmu/` directory of the openams repository. It is a
self-contained unit: its tests, tools and Makefiles resolve everything relative
to this package, so `make check` gives the same result from here, from a
worktree, and from a standalone `klipper_mmu` checkout. From the repository
root, `make test` runs this package's `make check` together with the openams
unit tests, and `.github/workflows/tests.yml` runs the same two as the
`klipper-mmu` and `unittest` jobs.

## Build inputs

Inputs that are built or downloaded rather than committed all live under one
build root, `build/` inside this package. `KLIPPER_MMU_BUILD_ROOT` overrides
it. The pinned Klipper tarball and the integration klippy virtualenv are read
from there, as are the extracted Happy Hare and AFC trees. A check that needs
one of these but does not have it says what to provide and skips; it does not
fail, and the skip is not counted as a pass.

Every executed entry in `build/check/run-*/summary.json` has a `result_ref` to
its actual bounded command record and log files. The Makefile passes its
`BUILD_DIR` through as `CHECK_BUILD_DIR`; all aggregate logs and outputs remain
under that build directory. The root test target receives both `BUILD_DIR` and
`TEST_BUILD_DIR` overrides. Commands run in their own process groups; timeouts
send TERM, then KILL after a short grace period. The aggregate never accesses
hardware or edits installed Klipper/OpenAMS sources or saved user
configuration.

The default check is a portable regression run, not a release qualification.
It intentionally omits external-source and retained-image checks, with each
omission recorded by entrypoint and reason. The optional command groups are:

```sh
KLIPPER_SOURCE=/path/to/klipper make check-upstream
make check-openams
OPENAMS_SOURCE=/path/to/other/openams make check-openams
KLIPPER_SOURCE=/path/to/klipper make check-slow
make check-release
```

Equivalent direct invocations are `python3 -B tools/check.py --upstream`,
`--openams`, `--slow`, or `--release`, with the same environment variables.
`--upstream` runs the actual Klipper command/config/message/pin tests, EMU
planned-profile checks, the config-isolation runner, and real F4/H7 CMSIS
identity compiles when their registered entrypoints and toolchain are present.
Other installed-source checks (local GPIO, ADC setup/composition, PWM driver,
and similar fixtures) remain individually listed as outside that selected
subset. `--openams` runs only modules whose test code imports the provided
`OPENAMS_SOURCE` and exercises its real runtime, manager and FPS code without
source hashes or patches. `klipper_mmu/` sits inside the openams repository,
so `OPENAMS_SOURCE` defaults to the repository root and `make check-openams`
needs no environment variable; set it only to test another checkout. Other
host protocol/adapter unit tests run in the portable default. `--slow` adds the calibration receiver and diagnostic actuation
fixtures. `--release` runs the pinned release matrix; it does not run the
separate STM32 target matrix or saved-image runtime suites.

Saved-image and dictionary checks are listed individually with the artifact
they require. Their absence does not count as a pass. If an opt-in source or
toolchain is requested but unavailable, or a suite discovers zero tests, the
aggregate fails and records that result. The openams repository's GitHub
Actions workflow runs the same `make check` command with GCC, Make, and Python
installed, as the `klipper-mmu` job.
