# SPDX-License-Identifier: GPL-3.0-or-later
# Makefile for klipper_stepdir adapter test suite
#
# Self-contained: `make test` fetches and patches its own copy of Klipper's
# src/ (tools/klipper_src_fixture.py, the same pattern
# tests/klipper_stepper_local/run_tests.py uses) so the real, patched
# stepper.c links against mcu/stepdir_gpio.c exactly as it will on the MCU.
# No external KLIPPER_SRC setup is required.

CC ?= gcc
BUILD_DIR ?= build/klipper_stepdir
TARGET ?= $(BUILD_DIR)/test_klipper_stepdir
KLIPPER_SRC_DIR := $(BUILD_DIR)/klipper-src
KLIPPER_SRC ?= $(KLIPPER_SRC_DIR)/src

CFLAGS := -std=gnu11 -Wall -Wextra -Werror -Wno-unused-parameter \
          -DCONFIG_WANT_STEPPER_LOCAL=1 -DCONFIG_STEPPER_LOCAL_COUNT=8 -DCONFIG_STEPPER_LOCAL_MOVES=224 \
          -fsanitize=address,undefined -fno-sanitize-recover=all -g -O2 -MMD -MP \
          -Itests/klipper_stepdir/shim_include -Itests/klipper_stepdir -Iinclude -Imcu -I. -I$(KLIPPER_SRC)

ifneq ($(strip $(IMAGE_AUTOCONF)),)
CFLAGS += -DKM_H723_IMAGE_AUTOCONF='"$(abspath $(IMAGE_AUTOCONF))"'
# The test object includes the image's autoconf.h, and that header states the
# stepper-local settings itself -- the image's configuration of record. Its
# count is the image's (one per lane), so repeating the test's own count on the
# command line would be a mismatched redefinition, which -Werror rejects. Only
# that one needs cancelling for this object: the count sizes Klipper's static
# pool in stepper.c, which the test exercises with two slots, while
# CONFIG_WANT_STEPPER_LOCAL and CONFIG_STEPPER_LOCAL_MOVES agreed already (the
# Klipper sources compiled here read the shim autoconf.h and keep the defaults).
$(BUILD_DIR)/tests/klipper_stepdir/test_klipper_stepdir.o: CFLAGS += -UCONFIG_STEPPER_LOCAL_COUNT
endif

# --wrap lets a test supply a scripted 32-bit stepper position so the 64-bit
# unwrap in mcu/stepdir_gpio.c can be exercised across the hardware wrap.
LDFLAGS := -fsanitize=address,undefined -fno-sanitize-recover=all -lm \
           -Wl,--wrap=stepper_local_position

SRCS := mcu/stepdir_gpio.c \
        mcu/clock.c \
        src/step_dir_driver.c \
        src/mcu_clock.c \
        src/rate.c \
        src/trajectory.c \
        src/step_segments.c \
        tests/klipper_stepdir/shim_klipper.c \
        tests/klipper_stepdir/test_klipper_stepdir.c

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


-include $(OBJS:.o=.d)

clean:
	rm -rf $(BUILD_DIR)
