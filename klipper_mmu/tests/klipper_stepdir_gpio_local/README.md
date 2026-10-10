# Local STEP/DIR GPIO claim tests

Host adapter-contract tests for `mcu/stepdir_gpio.c` with `CONFIG_WANT_GPIO_LOCAL=1`. The real adapter sources are compiled unchanged and linked with the existing `tests/klipper_stepdir/shim_klipper.c` (not a copy). Each named case is a new process.

```bash
python3 tests/klipper_stepdir_gpio_local/run_tests.py --results /tmp/stepdir-gpio-local-results
```

`--package` defaults to the klipper-mmu root that contains this directory and locates portable core sources plus `tests/klipper_stepdir` fixtures. `--source` defaults to `--package` and supplies `mcu/stepdir_gpio.c` and `mcu/stepdir_gpio.h`. `--results` is required and must be a path that does not already exist.

The suite does not touch hardware and does not replace upstream Klipper GPIO driver tests. Clock, scheduler, shutdown, and GPIO setup/write behavior come only from the existing shim. `gpio_pin_is_local` reads an explicit unclaimed / ordinary / local map in the test and does not reserve pins. Local setup calls `__real_gpio_out_setup`; an ordinary `gpio_out_setup` from the adapter aborts. A correct short-circuit of the claim check is accepted. Shutdown during setup is observed with `setjmp`/`longjmp`, and the test does not read ordinary automatic variables written in that window. A later prepare on that process must return `KM_ERR_FAULTED` without reinitializing GPIO.
