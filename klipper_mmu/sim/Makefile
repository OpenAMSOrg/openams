CC ?= cc
CFLAGS ?= -std=c11 -Wall -Wextra -Werror -pedantic -Wconversion -Wsign-conversion -g -fsanitize=address,undefined -fno-sanitize-recover=all
# Run from klipper_mmu/sim, so this package's build/ is one level up.
BUILD_DIR ?= ../build/simulator
FIRMWARE_DIR ?= ..
PROFILE ?= profiles/synthetic-lane.json
LOOP_SRCS = $(addprefix $(FIRMWARE_DIR)/src/, operation_core.c selector_home.c rate.c trajectory.c \
    buffer_sensor.c follower.c current_regulator.c event_queue.c device_profile.c \
    protocol_wire.c step_dir_driver.c pwm_bridge_driver.c adc_pair.c mcu_clock.c \
    step_segments.c) \
    $(FIRMWARE_DIR)/tests/stepper_reference.c

.PHONY: test test-loop test-measurements test-profile profile-header
test: $(BUILD_DIR)/test_plant $(BUILD_DIR)/test_feedback
	timeout 10s env ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1 $(BUILD_DIR)/test_plant
	timeout 10s env ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1 $(BUILD_DIR)/test_feedback

$(BUILD_DIR)/test_plant: plant.c plant.h test_plant.c
	mkdir -p $(BUILD_DIR)
	timeout 30s $(CC) $(CFLAGS) plant.c test_plant.c -lm -o $@

$(BUILD_DIR)/test_feedback: plant.c plant.h feedback.c feedback.h test_feedback.c ../src/buffer_sensor.c ../include/klipper_mmu/buffer_sensor.h ../include/klipper_mmu/types.h
	mkdir -p $(BUILD_DIR)
	timeout 30s $(CC) $(CFLAGS) -I../include plant.c feedback.c ../src/buffer_sensor.c test_feedback.c -lm -o $@

# Causal integration scenarios; separate from the small model unit suites.
test-loop: $(BUILD_DIR)/test_loop
	timeout 15s env ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1 $(BUILD_DIR)/test_loop $(BUILD_DIR)/trace.csv

$(BUILD_DIR)/test_loop: plant.c plant.h feedback.c feedback.h test_loop.c $(LOOP_SRCS) $(wildcard $(FIRMWARE_DIR)/include/klipper_mmu/*.h)
	mkdir -p $(BUILD_DIR)
	timeout 60s $(CC) $(CFLAGS) -I$(FIRMWARE_DIR)/include -I$(FIRMWARE_DIR)/tests plant.c feedback.c test_loop.c $(LOOP_SRCS) -lm -o $@

test-measurements:
	timeout 30s python3 -B -m unittest -v test_measurements.py test_normalize_travel.py

# Keep profile builds separate from the fixed synthetic regression binary.
profile-header:
	mkdir -p $(BUILD_DIR)
	timeout 10s python3 -B measurements.py export-header "$(PROFILE)" "$(BUILD_DIR)/profile.h"

$(BUILD_DIR)/test_loop_profile: profile-header plant.c plant.h feedback.c feedback.h test_loop.c $(LOOP_SRCS) $(wildcard $(FIRMWARE_DIR)/include/klipper_mmu/*.h)
	timeout 60s $(CC) $(CFLAGS) -I$(FIRMWARE_DIR)/include -I$(FIRMWARE_DIR)/tests -DMMU_SIM_PROFILE_HEADER='"$(abspath $(BUILD_DIR)/profile.h)"' plant.c feedback.c test_loop.c $(LOOP_SRCS) -lm -o $@

test-profile: $(BUILD_DIR)/test_loop_profile
	timeout 15s env ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1 $(BUILD_DIR)/test_loop_profile $(BUILD_DIR)/profile-trace.csv
