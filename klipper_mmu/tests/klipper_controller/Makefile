# SPDX-License-Identifier: GPL-3.0-or-later
# Makefile for klipper_controller integration test suite
#
# Self-contained: `make test` fetches and patches its own copy of Klipper's
# src/ (tools/klipper_src_fixture.py, the same pattern
# tests/klipper_stepper_local/run_tests.py uses) so the real, patched
# stepper.c links against mcu/controller.c and mcu/stepdir_gpio.c exactly as
# it will on the MCU. No external KLIPPER_SRC setup is required.

CC ?= gcc
BUILD_DIR ?= build/klipper_controller
TARGET ?= $(BUILD_DIR)/test_klipper_controller
KLIPPER_SRC_DIR := $(BUILD_DIR)/klipper-src
KLIPPER_SRC ?= $(KLIPPER_SRC_DIR)/src

CFLAGS := -std=gnu11 -Wall -Wextra -Werror -Wno-unused-parameter \
          -DCONFIG_KLIPPER_MMU_ACTUATION_OPT_IN=1 -DCONFIG_WANT_STEPPER_LOCAL=1 -DCONFIG_STEPPER_LOCAL_COUNT=8 -DCONFIG_STEPPER_LOCAL_MOVES=224 -DCONFIG_KLIPPER_MMU_RATE_TICK_US=1000 \
          -fsanitize=address,undefined -fno-sanitize-recover=all -g -O2 -MMD -MP \
          -Itests/klipper_stepdir/shim_include -Itests/klipper_stepdir -Iinclude -Imcu -I. \
          -I$(KLIPPER_SRC)

LDFLAGS := -fsanitize=address,undefined -fno-sanitize-recover=all -lm \
           -Wl,--wrap=sched_add_timer,--wrap=sched_del_timer,--wrap=command_sendf,--wrap=irq_restore

SRCS := mcu/controller.c \
        mcu/stepdir_gpio.c \
        mcu/servo_pwm.c \
        mcu/clock.c \
        mcu/commands.c \
        mcu/messages.c \
        src/operation_core.c \
        src/selector_home.c \
        src/rate.c \
        src/trajectory.c \
        src/step_segments.c \
        src/step_dir_driver.c \
        src/mcu_clock.c \
        src/follower.c \
        src/buffer_sensor.c \
        src/current_regulator.c \
        src/event_queue.c \
        src/device_profile.c \
        src/protocol_wire.c \
        tests/klipper_stepdir/shim_klipper.c \
        tests/klipper_controller/test_klipper_controller.c

OBJS := $(patsubst %.c,$(BUILD_DIR)/%.o,$(SRCS))
STEPPER_OBJ := $(BUILD_DIR)/klipper-stepper.o

.PHONY: all build test clean

all: test

build: $(TARGET)

# Materialize the patched Klipper src/ once per build tree; every compile
# (including the real stepper.c) depends on it being in place.
$(KLIPPER_SRC_DIR)/.stamp:
	@mkdir -p $(BUILD_DIR)
	timeout 120s python3 tools/klipper_src_fixture.py $(KLIPPER_SRC_DIR)
	@touch $@

$(STEPPER_OBJ): $(KLIPPER_SRC_DIR)/.stamp
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(CFLAGS) -c $(KLIPPER_SRC)/stepper.c -o $@

$(TARGET): $(OBJS) $(STEPPER_OBJ)
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(CFLAGS) $(OBJS) $(STEPPER_OBJ) $(LDFLAGS) -o $@

$(BUILD_DIR)/%.o: %.c $(KLIPPER_SRC_DIR)/.stamp
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(CFLAGS) -c $< -o $@

test: $(TARGET)
	@timeout 60s env ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1 $(TARGET)

clean:
	rm -rf $(BUILD_DIR)

-include $(OBJS:.o=.d)
