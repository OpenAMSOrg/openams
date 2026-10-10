test:
	timeout 60s python3 tests/linux_mcu/tmc2209_model_test/run.py
	timeout 60s python3 tests/linux_mcu/plant_model_test/run.py
	timeout 1500s python3 tests/linux_mcu/build.py
	timeout 120s python3 tests/linux_mcu/boot_check.py
	timeout 180s python3 tests/linux_mcu/motion_boot_check.py
	timeout 240s python3 tests/linux_mcu/preload_check.py
	timeout 1500s python3 tests/linux_mcu/fault_check.py
	timeout 300s python3 tests/linux_mcu/control_smoke.py
	timeout 600s python3 tests/linux_mcu/load_check.py
	timeout 1200s python3 tests/linux_mcu/load_scenarios.py

# Separate from `test`/check-integration (kept BoxTurtle-only, per
# tests/linux_mcu/README.md and devices.py): NightOwl and EMU each build
# and run as their own sim-only actuation-permitted image, proven against
# their Happy Hare dossier (docs/devices/{nightowl,emu}.json). Not wired
# into the default `test` target so BoxTurtle's own timing budget is
# unaffected; run explicitly with `make -f tests/linux_mcu/Makefile
# test-devices`.
test-devices:
	timeout 900s python3 tests/linux_mcu/build.py nightowl
	timeout 600s python3 tests/linux_mcu/device_scenarios.py nightowl
	timeout 900s python3 tests/linux_mcu/build.py ercf_2_0
	timeout 600s python3 tests/linux_mcu/device_scenarios.py ercf_2_0
	timeout 900s python3 tests/linux_mcu/build.py chameleon_3d
	timeout 600s python3 tests/linux_mcu/device_scenarios.py chameleon_3d
	timeout 900s python3 tests/linux_mcu/build.py mmx
	timeout 600s python3 tests/linux_mcu/device_scenarios.py mmx
	timeout 900s python3 tests/linux_mcu/build.py vivid
	timeout 600s python3 tests/linux_mcu/device_scenarios.py vivid
	timeout 900s python3 tests/linux_mcu/build.py claymore
	timeout 600s python3 tests/linux_mcu/device_scenarios.py claymore
	timeout 900s python3 tests/linux_mcu/build.py emu
	timeout 600s python3 tests/linux_mcu/device_scenarios.py emu
	timeout 900s python3 tests/linux_mcu/build.py qidi_box
	timeout 600s python3 tests/linux_mcu/device_scenarios.py qidi_box
	timeout 900s python3 tests/linux_mcu/build.py kms
	timeout 600s python3 tests/linux_mcu/device_scenarios.py kms
	timeout 900s python3 tests/linux_mcu/build.py angry_beaver
	timeout 600s python3 tests/linux_mcu/device_scenarios.py angry_beaver
	timeout 900s python3 tests/linux_mcu/build.py 3ms
	timeout 600s python3 tests/linux_mcu/device_scenarios.py 3ms
	timeout 900s python3 tests/linux_mcu/build.py quattrobox_2
	timeout 600s python3 tests/linux_mcu/device_scenarios.py quattrobox_2
	timeout 900s python3 tests/linux_mcu/build.py ercf_1_1
	timeout 600s python3 tests/linux_mcu/device_scenarios.py ercf_1_1
	timeout 900s python3 tests/linux_mcu/build.py ercf_3_0
	timeout 600s python3 tests/linux_mcu/device_scenarios.py ercf_3_0
	timeout 900s python3 tests/linux_mcu/build.py jack_rabbit
	timeout 600s python3 tests/linux_mcu/device_scenarios.py jack_rabbit
	timeout 900s python3 tests/linux_mcu/build.py tradrack
	timeout 600s python3 tests/linux_mcu/device_scenarios.py tradrack
	timeout 900s python3 tests/linux_mcu/build.py htlf
	timeout 600s python3 tests/linux_mcu/device_scenarios.py htlf
	timeout 900s python3 tests/linux_mcu/build.py mmx6
	timeout 600s python3 tests/linux_mcu/device_scenarios.py mmx6
	timeout 900s python3 tests/linux_mcu/build.py low_rider
	timeout 600s python3 tests/linux_mcu/device_scenarios.py low_rider
	timeout 900s python3 tests/linux_mcu/build.py pico_mmu
	timeout 600s python3 tests/linux_mcu/device_scenarios.py pico_mmu
