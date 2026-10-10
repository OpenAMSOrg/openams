# Shared TMC feeder native test

The default test builds the production feeder/UART/STEP-DIR sources against
the virtual TMC shim and runs the existing lifecycle and fault-isolation cases
from a clean checkout. One case also links the production command handlers and
operation core to drive `km_driver_restart` and `km_path_reconcile` after a
lane fault:

```sh
make -f tests/klipper_tmc_feeder/Makefile test
```

To add the saved-configuration integration case, pass the `autoconf.h` from
one exact, clean four-lane BoxTurtle STM32H723 actuation image:

```sh
make -f tests/klipper_tmc_feeder/Makefile test \
  IMAGE_AUTOCONF=/absolute/path/to/boxturtle-h723-actuation/autoconf.h
```

That case runs the production pin, feeder timing, and TMC configuration
factories, then passes their four STEP/DIR/ENABLE/UART pin assignments and
per-lane TMC address/current settings into `km_tmc_feeder_setup`. The virtual
chips must confirm the configured currents before any driver can be activated;
the test then checks fault safe-off on one lane while another remains enabled.
The optional image path is intentionally external, so the normal test does not
depend on an ignored release-matrix build directory.

This is a native software test. The virtual UART/TMC chips and GPIO shim do not
measure physical pin routing, bus signal quality, motor current, STEP timing,
or mechanical behavior, and the result does not qualify an image for hardware
motion.
