# Host calibration and pinned Klipper configuration integration

Run from the repository root:

```sh
timeout 15s python3 klipper-mmu/tests/klipper_host_calibration_integration/run_tests.py
```

The suite requires the pinned source files in `build/klipper-menuconfig/klippy`
and a generated diagnostic Diff firmware dictionary at
`build/calibration-build-matrix/diagnostic_diff/out/klipper.dict`. It checks
the pinned `mcu.py` and `msgproto.py` hashes before importing them. To use a
fresh compatible dictionary, set `KLIPPER_MMU_TEST_DICTIONARY` to its absolute
path before running the command. `KLIPPER_MMU_TEST_SOURCE` may select the
matching pinned Klipper source checkout. Set
`KLIPPER_MMU_TEST_EXPECT_PROBE=1` with a receive-only image dictionary to
check that a raw Diff probe registers one init command, no calibration object,
and reissues only that init command after a CRC-matched reconnect.

The calibration records in this suite are synthetic; no hardware is accessed.
