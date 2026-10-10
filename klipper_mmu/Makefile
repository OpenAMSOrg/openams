CC ?= gcc
.DEFAULT_GOAL := all
CFLAGS ?= -std=c11 -Wall -Wextra -Werror -pedantic -Wconversion -Wsign-conversion -Iinclude -Itests -fsanitize=address,undefined -fno-sanitize-recover=all -g -MMD -MP
LDFLAGS ?= -fsanitize=address,undefined -fno-sanitize-recover=all -lm

BUILD_DIR ?= build
# Per-binary limit for the native tests. The sanitizer builds are several times
# slower on a shared CI runner than on a workstation.
TEST_TIMEOUT ?= 60s
TEST_BUILD_DIR = $(BUILD_DIR)/tests

CORE_SRCS = src/rate.c \
            src/trajectory.c \
            src/selector_home.c \
            src/buffer_sensor.c \
            src/follower.c \
            src/operation_core.c \
            src/event_queue.c \
            src/device_profile.c \
            src/protocol_wire.c \
            src/current_regulator.c \
            src/tmc2209.c \
            src/tmc_uart_codec.c \
            src/tmc_uart_config.c \
            src/step_dir_driver.c \
            src/step_segments.c \
            src/adc_pair.c \
            src/pwm_bridge_driver.c \
            src/mcu_clock.c \
            src/resource_plan.c

MOCK_SRCS = tests/mock_driver.c tests/stepper_reference.c

CORE_OBJS = $(patsubst src/%.c, $(BUILD_DIR)/src/%.o, $(CORE_SRCS))
MOCK_OBJS = $(patsubst tests/%.c, $(BUILD_DIR)/tests/%.o, $(MOCK_SRCS))

TESTS = test_rate \
        test_rate_crosscheck \
        test_step_segments \
        test_stepper_reference \
        test_trajectory \
        test_selector_home \
        test_buffer_sensor \
        test_follower \
        test_operation_core \
        test_selector_core \
        test_operation_time \
        test_fps_arrival \
        test_reconnect_status \
        test_request_dispatch \
        test_event_queue \
        test_profiles \
        test_safety_contracts \
        test_tmc2209 \
        test_tmc_uart_codec \
        test_tmc_uart_config \
        test_adc_pair \
        test_pwm_bridge_driver \
        test_mcu_clock \
        test_resource_plan

TEST_BINS = $(patsubst %, $(TEST_BUILD_DIR)/%, $(TESTS))
ALL_DEPS = $(CORE_OBJS:.o=.d) $(MOCK_OBJS:.o=.d) $(patsubst %, $(TEST_BUILD_DIR)/%.d, $(TESTS))

.SECONDARY: $(CORE_OBJS) $(MOCK_OBJS)

.PHONY: all test test-summary test-motion test-trajectory check check-upstream check-openams check-slow check-release check-integration clean

check:
	CHECK_BUILD_DIR="$(BUILD_DIR)/check" python3 -B tools/check.py

check-upstream:
	CHECK_BUILD_DIR="$(BUILD_DIR)/check" python3 -B tools/check.py --upstream

check-openams:
	CHECK_BUILD_DIR="$(BUILD_DIR)/check" python3 -B tools/check.py --openams

check-slow:
	CHECK_BUILD_DIR="$(BUILD_DIR)/check" python3 -B tools/check.py --slow

check-release:
	CHECK_BUILD_DIR="$(BUILD_DIR)/check" python3 -B tools/check.py --release

# The whole stack on the sim board: the real BoxTurtle image on Klipper's Linux
# runtime, klippy, OpenAMS and the plant (tests/linux_mcu/README.md). Needs
# `sudo -n setcap` for real-time priority; without it host stalls can fail it.
check-integration:
	$(MAKE) -f tests/linux_mcu/Makefile test

test-motion: $(TEST_BUILD_DIR)/test_trajectory
	@echo "Running Trajectory / Motion Foundation Test Suite..."
	timeout 10s env UBSAN_OPTIONS=halt_on_error=1:abort_on_error=1 ASAN_OPTIONS=halt_on_error=1:detect_leaks=0 LSAN_OPTIONS=detect_leaks=0 $(TEST_BUILD_DIR)/test_trajectory

test-trajectory: test-motion

test-rate: $(TEST_BUILD_DIR)/test_rate
	@echo "Running Rate Motion Core Test Suite..."
	timeout 10s env UBSAN_OPTIONS=halt_on_error=1:abort_on_error=1 ASAN_OPTIONS=halt_on_error=1:detect_leaks=0 LSAN_OPTIONS=detect_leaks=0 $(TEST_BUILD_DIR)/test_rate

test-step-segments: $(TEST_BUILD_DIR)/test_step_segments
	@if command -v arm-none-eabi-gcc >/dev/null 2>&1; then \
		echo "Running static checks for Cortex-M0+..."; \
		arm-none-eabi-gcc -Iinclude -mcpu=cortex-m0plus -mthumb -O2 -c src/step_segments.c -o $(BUILD_DIR)/step_segments_m0.o || exit 1; \
		arm-none-eabi-nm -u $(BUILD_DIR)/step_segments_m0.o > $(BUILD_DIR)/step_segments_m0.undef; \
		if grep -E "__aeabi_ldivmod|__aeabi_uldivmod|__divdi3|__udivdi3|__moddi3|__umoddi3|__aeabi_d|__aeabi_f|__aeabi_i2d|__aeabi_l2d" $(BUILD_DIR)/step_segments_m0.undef; then \
			echo "FAIL: 64-bit division or floating-point helper found in step_segments.c"; exit 1; \
		fi; \
	fi
	@echo "Running Step Segments Test Suite..."
	timeout 10s env UBSAN_OPTIONS=halt_on_error=1:abort_on_error=1 ASAN_OPTIONS=halt_on_error=1:detect_leaks=0 LSAN_OPTIONS=detect_leaks=0 $(TEST_BUILD_DIR)/test_step_segments

test-stepper-reference: $(TEST_BUILD_DIR)/test_stepper_reference
	@echo "Running Stepper Reference Test Suite..."
	timeout 10s env UBSAN_OPTIONS=halt_on_error=1:abort_on_error=1 ASAN_OPTIONS=halt_on_error=1:detect_leaks=0 LSAN_OPTIONS=detect_leaks=0 $(TEST_BUILD_DIR)/test_stepper_reference

# Compile-only gate for the B2 tick API contract test: the implementation may
# not exist yet, so linking is expected to fail until it lands.
build-trajectory-window-objects: $(TEST_BUILD_DIR)/test_trajectory_window.o $(TEST_BUILD_DIR)/stepper_reference.o

test-trajectory-window: $(TEST_BUILD_DIR)/test_trajectory_window
	@echo "Running Trajectory Window Test Suite..."
	timeout 10s env UBSAN_OPTIONS=halt_on_error=1:abort_on_error=1 ASAN_OPTIONS=halt_on_error=1:detect_leaks=0 LSAN_OPTIONS=detect_leaks=0 $(TEST_BUILD_DIR)/test_trajectory_window

$(TEST_BUILD_DIR)/test_trajectory_window: tests/test_trajectory_window.c $(CORE_OBJS) $(MOCK_OBJS)
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) $^ $(LDFLAGS) -o $@

$(TEST_BUILD_DIR)/test_stepper_reference: tests/test_stepper_reference.c $(CORE_OBJS) $(MOCK_OBJS)
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) $^ $(LDFLAGS) -o $@

$(TEST_BUILD_DIR)/test_rate_crosscheck: tests/test_rate_crosscheck.c tests/rate_reference.c $(CORE_OBJS) $(MOCK_OBJS)
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) $^ $(LDFLAGS) -o $@

test-rate-crosscheck: $(TEST_BUILD_DIR)/test_rate_crosscheck
	@echo "Running Rate Motion Core Crosscheck Test..."
	timeout 10s env UBSAN_OPTIONS=halt_on_error=1:abort_on_error=1 ASAN_OPTIONS=halt_on_error=1:detect_leaks=0 LSAN_OPTIONS=detect_leaks=0 $(TEST_BUILD_DIR)/test_rate_crosscheck

all: $(TEST_BINS)

$(BUILD_DIR)/src/%.o: src/%.c
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -c $< -o $@

$(BUILD_DIR)/tests/%.o: tests/%.c
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -c $< -o $@

$(TEST_BUILD_DIR)/%: tests/%.c $(CORE_OBJS) $(MOCK_OBJS)
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) $< $(CORE_OBJS) $(MOCK_OBJS) $(LDFLAGS) -o $@

-include $(ALL_DEPS)

# Compact view for agents iterating on a change: build every native test
# (continuing past errors), print compile errors, then one line per test (ok,
# or FAIL with its first assertion). Exits nonzero if anything failed.
test-summary:
	@mkdir -p $(BUILD_DIR); $(MAKE) -k all > $(BUILD_DIR)/test-summary.log 2>&1; \
	grep -E "error:" $(BUILD_DIR)/test-summary.log | head -20; fail=0; \
	for t in $(TEST_BINS); do \
		if [ ! -x $$t ]; then echo "FAIL $$t: not built"; fail=1; continue; fi; \
		out=$$(timeout $(TEST_TIMEOUT) env UBSAN_OPTIONS=halt_on_error=1:abort_on_error=1 ASAN_OPTIONS=halt_on_error=1:detect_leaks=0 LSAN_OPTIONS=detect_leaks=0 $$t 2>&1); rc=$$?; \
		if [ $$rc -eq 0 ]; then echo "ok   $$t"; \
		elif [ $$rc -eq 124 ]; then echo "TIMEOUT $$t (after $(TEST_TIMEOUT))"; fail=1; \
		else echo "FAIL $$t: $$(echo "$$out" | grep -E "Assertion|runtime error|ERROR" | head -1 | cut -c1-240)"; fail=1; fi; \
	done; exit $$fail

test: all
	@echo "================================================="
	@echo "Running Klipper-MMU Native Test Suite"
	@echo "================================================="
	@for t in $(TEST_BINS); do \
		echo "Executing $$t ..."; \
		timeout $(TEST_TIMEOUT) env UBSAN_OPTIONS=halt_on_error=1:abort_on_error=1 ASAN_OPTIONS=halt_on_error=1:detect_leaks=0 LSAN_OPTIONS=detect_leaks=0 $$t; rc=$$?; \
		if [ $$rc -eq 124 ]; then echo "TIMEOUT $$t (after $(TEST_TIMEOUT))"; exit 1; fi; \
		if [ $$rc -ne 0 ]; then echo "FAIL $$t (exit $$rc)"; exit 1; fi; \
	done
	@echo "================================================="
	@echo "All native tests passed successfully under ASAN/UBSAN!"
	@echo "================================================="

clean:
	rm -rf $(BUILD_DIR)
