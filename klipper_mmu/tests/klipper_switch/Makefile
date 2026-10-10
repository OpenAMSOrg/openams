# SPDX-License-Identifier: GPL-3.0-or-later
CC ?= gcc
BUILD_DIR ?= build/klipper_switch
CFLAGS := -std=c11 -Wall -Wextra -Werror -pedantic -Wconversion -Wsign-conversion \
 -fsanitize=address,undefined -fno-sanitize-recover=all -g -O2 -MMD -MP \
 -DCONFIG_KLIPPER_MMU_RATE_TICK_US=1000 \
 -Itests/klipper_switch/shim_include -Itests/klipper_stepdir/shim_include \
 -Itests/klipper_stepdir -Iinclude -Imcu -Itests/stub_stepper_local
LDFLAGS := -fsanitize=address,undefined -fno-sanitize-recover=all -lm
SRCS := mcu/switch_gpio.c mcu/controller.c mcu/clock.c src/mcu_clock.c \
 src/operation_core.c src/selector_home.c src/rate.c src/trajectory.c src/step_segments.c src/follower.c src/buffer_sensor.c \
 src/current_regulator.c src/event_queue.c src/device_profile.c src/protocol_wire.c \
 tests/klipper_stepdir/shim_klipper.c tests/mock_driver.c tests/stub_stepper_local/stepper_local_stub.c \
 tests/stub_stepper_local/stepper_local_ref.c tests/stub_stepper_local/stepdir_gpio_accessor_stub.c \
 tests/stepper_reference.c tests/klipper_switch/test_switch.c
OBJS := $(patsubst %.c,$(BUILD_DIR)/%.o,$(SRCS))
# The INDEXED selector's per-gate switches (klipper_mmu/selector.h) need a
# build option most images don't carry (docs/selectors.md "no room for gate
# switches"): this shares every source above but test_switch.c, adds
# test_selector_gate.c, and turns the option on, the same shape as
# tests/klipper_board_startup's selector-endstop binary.
GATE_DIR := $(BUILD_DIR)/gate
GATE_CFLAGS := $(CFLAGS) -DCONFIG_KLIPPER_MMU_SELECTOR_KIND_INDEXED=1
GATE_SRCS := $(filter-out tests/klipper_switch/test_switch.c,$(SRCS)) tests/klipper_switch/test_selector_gate.c
GATE_OBJS := $(patsubst %.c,$(GATE_DIR)/%.o,$(GATE_SRCS))
.PHONY: test
test: $(BUILD_DIR)/test_switch $(GATE_DIR)/test_selector_gate
	timeout 60s env ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1 $(BUILD_DIR)/test_switch
	timeout 60s env ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1 $(GATE_DIR)/test_selector_gate
$(BUILD_DIR)/test_switch: $(OBJS)
	timeout 60s $(CC) $(OBJS) $(LDFLAGS) -o $@
$(GATE_DIR)/test_selector_gate: $(GATE_OBJS)
	timeout 60s $(CC) $(GATE_OBJS) $(LDFLAGS) -o $@
$(BUILD_DIR)/%.o: %.c
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(CFLAGS) -c $< -o $@
$(GATE_DIR)/%.o: %.c
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(GATE_CFLAGS) -c $< -o $@
-include $(OBJS:.o=.d)
-include $(GATE_OBJS:.o=.d)
