# SPDX-License-Identifier: GPL-3.0-or-later
# Makefile for klipper_adc adapter test suite

CC ?= gcc
BUILD_DIR ?= build/gemini-adc-deadlines-26
TARGET ?= $(BUILD_DIR)/test_klipper_adc

CFLAGS := -std=c11 -Wall -Wextra -Werror -pedantic -Wconversion -Wsign-conversion \
          -fsanitize=address,undefined -fno-sanitize-recover=all -g -O2 -MMD -MP \
          -Itests/klipper_adc/shim_include -Itests/klipper_adc -Iinclude -Imcu -I.

SRCS := mcu/adc.c \
        mcu/clock.c \
        src/adc_pair.c \
        src/mcu_clock.c \
        tests/klipper_adc/shim_klipper.c \
        tests/klipper_adc/test_klipper_adc.c

OBJS := $(patsubst %.c,$(BUILD_DIR)/%.o,$(SRCS))

.PHONY: all build test clean

all: test

build: $(TARGET)

$(TARGET): $(OBJS)
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(CFLAGS) $(OBJS) -o $@

$(BUILD_DIR)/%.o: %.c
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(CFLAGS) -c $< -o $@

test: $(TARGET)
	@timeout 15s env ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1 $(TARGET)

-include $(OBJS:.o=.d)

clean:
	rm -rf $(BUILD_DIR)
