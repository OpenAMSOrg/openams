# SPDX-License-Identifier: GPL-3.0-or-later
CC ?= gcc
BUILD_DIR ?= build/klipper_sensors
ADC ?= 1
CFLAGS := -std=c11 -Wall -Wextra -Werror -pedantic -Wconversion -Wsign-conversion \
 -fsanitize=address,undefined -fno-sanitize-recover=all -g -O2 -MMD -MP \
 -DCONFIG_WANT_ADC=$(ADC) -DCONFIG_KLIPPER_MMU_RATE_TICK_US=1000 -Itests/klipper_sensors/shim_include \
 -Itests/klipper_adc/shim_include -Itests/klipper_adc -Itests -Iinclude -Imcu \
 -Itests/stub_stepper_local
LDFLAGS := -fsanitize=address,undefined -fno-sanitize-recover=all -lm \
 -Wl,--wrap=km_buffer_update_adc_pair,--wrap=sched_add_timer,--wrap=sched_del_timer
SRCS := mcu/sensors.c mcu/switch_gpio.c mcu/adc.c mcu/controller.c mcu/clock.c \
 src/mcu_clock.c src/adc_pair.c src/buffer_sensor.c src/operation_core.c src/selector_home.c \
 src/rate.c src/trajectory.c src/step_segments.c src/follower.c src/current_regulator.c src/event_queue.c \
 src/device_profile.c src/protocol_wire.c tests/mock_driver.c \
 tests/klipper_adc/shim_klipper.c tests/stub_stepper_local/stepper_local_stub.c tests/stub_stepper_local/stepper_local_ref.c \
 tests/stub_stepper_local/stepdir_gpio_accessor_stub.c tests/stepper_reference.c \
 tests/klipper_sensors/test_sensors.c
OBJS := $(patsubst %.c,$(BUILD_DIR)/%.o,$(SRCS))
.PHONY: test
test: $(BUILD_DIR)/test_sensors
	timeout 60s env ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1 $<
$(BUILD_DIR)/test_sensors: $(OBJS)
	timeout 60s $(CC) $(OBJS) $(LDFLAGS) -o $@
$(BUILD_DIR)/%.o: %.c
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(CFLAGS) -c $< -o $@
-include $(OBJS:.o=.d)
