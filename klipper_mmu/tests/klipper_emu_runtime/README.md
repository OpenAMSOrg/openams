# Saved-image EMU runtime fixture

Run the native runtime proof against a retained five-lane EMU actuation image:

```sh
python3 -B tests/klipper_emu_runtime/run_runtime.py \
  --autoconf "$PWD/build/release-matrix/artifacts/emu-h723-usb/autoconf.h" \
  --dictionary "$PWD/build/release-matrix/artifacts/emu-h723-usb/klipper.dict" \
  --build-dir "$PWD/build/emu-runtime"
```

The runner checks the saved image manifest hashes and required Klipper command
signatures before compiling the production board startup, switch sampling,
diagnostic startup, command, message, and operation code with that image's
configuration. A native pin shim supplies deterministic sensor levels. It
starts through `km_diagnostic_start`, verifies the physical lane-0 LOAD input
is projected as the virtual HUB while the lane owns the path, then dispatches
production LOAD/FOLLOW/UNLOAD commands and exercises retained terminal query
and exact event ACK behavior. Other lanes must stay idle.

This checks software composition and state transitions only. The test's pin,
timer, clock, and motor services are simulated; it does not measure GPIO
timing, current, wiring, motion, or mechanical filament movement.
