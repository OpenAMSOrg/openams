# SPDX-License-Identifier: GPL-3.0-or-later
CC ?= gcc
PRODUCT_ROOT ?= ../..
STARTUP_FIXTURE := $(PRODUCT_ROOT)/tests/klipper_board_startup
IMAGE_AUTOCONF ?= $(PRODUCT_ROOT)/build/artifacts/ercf-2-0-h723-usb/autoconf.h
BUILD_DIR ?= build
TARGET_NAME ?= test_selector_runtime
TARGET := $(BUILD_DIR)/$(TARGET_NAME)

CFLAGS := -std=c11 -Wall -Wextra -Werror -pedantic -Wconversion -Wsign-conversion \
	-DKM_TEST_HOSTED_RUNTIME=1 -fsanitize=address,undefined -fno-sanitize-recover=all \
	-g -O2 -fno-pie -no-pie -include $(IMAGE_AUTOCONF) \
	-I$(STARTUP_FIXTURE)/shim -I$(PRODUCT_ROOT)/mcu -I$(PRODUCT_ROOT)/include \
	-I$(PRODUCT_ROOT)/tests -I$(PRODUCT_ROOT)
LDFLAGS := -fsanitize=address,undefined -fno-sanitize-recover=all -no-pie -lm

SOURCES := $(PRODUCT_ROOT)/mcu/board_startup.c \
	$(PRODUCT_ROOT)/mcu/config.c $(PRODUCT_ROOT)/mcu/pin_config.c \
	$(PRODUCT_ROOT)/mcu/h723_resources.c $(PRODUCT_ROOT)/mcu/feeder_config.c \
	$(PRODUCT_ROOT)/mcu/tmc_config.c $(PRODUCT_ROOT)/mcu/switch_config.c \
	$(PRODUCT_ROOT)/mcu/rewind_config.c $(PRODUCT_ROOT)/mcu/switch_gpio.c \
	$(PRODUCT_ROOT)/mcu/sensors.c $(PRODUCT_ROOT)/mcu/encoder.c $(PRODUCT_ROOT)/mcu/commands.c \
	$(PRODUCT_ROOT)/mcu/messages.c $(PRODUCT_ROOT)/mcu/diagnostic_startup.c \
	$(PRODUCT_ROOT)/src/operation_core.c $(PRODUCT_ROOT)/src/selector_home.c $(PRODUCT_ROOT)/src/device_profile.c \
	$(PRODUCT_ROOT)/src/resource_plan.c $(PRODUCT_ROOT)/src/event_queue.c \
	$(PRODUCT_ROOT)/src/buffer_sensor.c $(PRODUCT_ROOT)/src/current_regulator.c \
	$(PRODUCT_ROOT)/src/follower.c $(PRODUCT_ROOT)/src/rate.c $(PRODUCT_ROOT)/src/trajectory.c \
	$(PRODUCT_ROOT)/src/step_segments.c $(PRODUCT_ROOT)/tests/stepper_reference.c \
	$(PRODUCT_ROOT)/src/protocol_wire.c $(PRODUCT_ROOT)/src/step_dir_driver.c \
	$(PRODUCT_ROOT)/src/pwm_bridge_driver.c $(PRODUCT_ROOT)/src/adc_pair.c \
	$(PRODUCT_ROOT)/src/tmc2209.c $(PRODUCT_ROOT)/src/tmc_uart_codec.c \
	$(PRODUCT_ROOT)/src/tmc_uart_config.c $(PRODUCT_ROOT)/src/mcu_clock.c \
	$(PRODUCT_ROOT)/tests/mock_driver.c test_selector_runtime.c

.PHONY: build test
build: $(TARGET)

$(TARGET): $(SOURCES) $(IMAGE_AUTOCONF)
	@mkdir -p $(dir $@)
	timeout 90s $(CC) $(CFLAGS) $(SOURCES) $(LDFLAGS) -o $@

test: $(TARGET)
	timeout 30s env ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1:abort_on_error=1 $(TARGET)
