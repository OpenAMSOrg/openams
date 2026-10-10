# F405/F407 exact-image hosted checks

`run_runtime.py` verifies the saved image manifest, hashes, Kconfig choices and
Klipper command dictionary before compiling the common native startup fixture
with that image's `autoconf.h`.

The safe image must reach production diagnostic `READY`, deny `km_start`, and
leave its mock feeder idle. The separate actuation image exercises the shared
controller through synthetic BAY/HUB/LOAD/toolhead switches and checks
LOAD/FOLLOW/UNLOAD plus terminal event acknowledgements. Drivers and GPIO are
hosted mocks; these checks do not qualify pin wiring, electrical behavior,
physical driver timing, or hardware safety.
