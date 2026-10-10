# Calibration receiver native fixture

Run `python3 tests/klipper_calibration_receiver/run_tests.py` from the
`klipper-mmu` directory. The runner builds the add-on calibration receiver and
portable validator with the pinned Klipper `src/command.c` and `src/basecmd.c`
from `../build/klipper-menuconfig`. Each case runs in a fresh process with a
60-second compile timeout and 10-second case timeout. It uses a simulated
scheduler and memory arena; no board, ADC, GPIO, motor, or flashing code is
linked.

The maximum record case sends 54 calibration bytes in a 64-byte physical frame
with two-byte command and object IDs. The fixture checks wire decoding,
object allocation and identity, invalid record and OID rejection, duplicate
rejection, Klipper's finalize guard, shutdown/config_reset denial, and pointer
retention on a matching reconnect with no command replay.

These curves are synthetic parser fixtures, not measured calibration data or
evidence that diagnostic startup admits the sensor.
