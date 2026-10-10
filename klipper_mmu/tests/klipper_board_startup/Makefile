# SPDX-License-Identifier: GPL-3.0-or-later
CC ?= gcc
PYTHON ?= python3
PRODUCT_ROOT ?= ../..
PROTOTYPE_ROOT ?= ../..
BUILD_DIR ?= build
TARGET ?= $(BUILD_DIR)/test_board_startup

# The selector scenario's build options; image builds take theirs from IMAGE_AUTOCONF.
SELECTOR_DEFS := -DCONFIG_KLIPPER_MMU_SELECTOR_STEPPER=1 -DCONFIG_KLIPPER_MMU_SERVO=1
CFLAGS := -std=gnu11 -Wall -Wextra -Werror -Wno-unused-parameter \
          -DCONFIG_WANT_GPIO_LOCAL=1 $(SELECTOR_DEFS) \
          -fsanitize=address,undefined -fno-sanitize-recover=all -g -O2 -MMD -MP \
          -fno-pie -no-pie \
          -Ishim \
          -I$(PROTOTYPE_ROOT)/mcu \
          -I$(PRODUCT_ROOT)/mcu \
          -I$(PRODUCT_ROOT)/include \
          -I$(PRODUCT_ROOT)/tests \
          -I$(PRODUCT_ROOT)

LDFLAGS := -fsanitize=address,undefined -fno-sanitize-recover=all -no-pie -lm

SRCS := $(PROTOTYPE_ROOT)/mcu/board_startup.c \
        $(PROTOTYPE_ROOT)/mcu/servo_pwm.c \
        $(PRODUCT_ROOT)/src/operation_core.c \
        $(PRODUCT_ROOT)/src/selector_home.c \
        $(PRODUCT_ROOT)/src/device_profile.c \
        $(PRODUCT_ROOT)/src/resource_plan.c \
        $(PRODUCT_ROOT)/src/event_queue.c \
        $(PRODUCT_ROOT)/src/buffer_sensor.c \
        $(PRODUCT_ROOT)/src/current_regulator.c \
        $(PRODUCT_ROOT)/src/follower.c \
        $(PRODUCT_ROOT)/src/rate.c \
        $(PRODUCT_ROOT)/src/trajectory.c \
          src/step_segments.c \
        $(PRODUCT_ROOT)/src/protocol_wire.c \
        $(PRODUCT_ROOT)/src/step_dir_driver.c \
        $(PRODUCT_ROOT)/src/pwm_bridge_driver.c \
        $(PRODUCT_ROOT)/src/adc_pair.c \
        $(PRODUCT_ROOT)/src/tmc2209.c \
        $(PRODUCT_ROOT)/src/tmc_uart_codec.c \
        $(PRODUCT_ROOT)/src/tmc_uart_config.c \
        $(PRODUCT_ROOT)/src/mcu_clock.c \
        $(PRODUCT_ROOT)/tests/mock_driver.c \
        test_board_startup.c

OBJS := $(addprefix $(BUILD_DIR)/, $(notdir $(SRCS:.c=.o)))

.PHONY: all build test test-diff test-fps-composed clean run

all: test

build: $(TARGET)

$(TARGET): $(OBJS)
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(CFLAGS) $(OBJS) $(LDFLAGS) -o $@

$(BUILD_DIR)/board_startup.o: $(PROTOTYPE_ROOT)/mcu/board_startup.c
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(CFLAGS) -c $< -o $@

$(BUILD_DIR)/servo_pwm.o: $(PROTOTYPE_ROOT)/mcu/servo_pwm.c
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(CFLAGS) -c $< -o $@

$(BUILD_DIR)/mock_driver.o: $(PRODUCT_ROOT)/tests/mock_driver.c
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(CFLAGS) -c $< -o $@

$(BUILD_DIR)/test_board_startup.o: test_board_startup.c
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(CFLAGS) -c $< -o $@

$(BUILD_DIR)/%.o: $(PRODUCT_ROOT)/src/%.c
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(CFLAGS) -c $< -o $@

BOOT_DIR := $(BUILD_DIR)/boot
BOOT_TARGET := $(BOOT_DIR)/test_boxturtle_boot
IMAGE_AUTOCONF ?= $(PRODUCT_ROOT)/build/boxturtle-boot-image/klipper/out/autoconf.h
# B2 drives STEP/DIR through Klipper's local stepper, which these fixtures do
# not link (that needs the real patched stepper.c, see tests/klipper_controller).
# tests/stub_stepper_local/stepper.h stands in for its declarations and the
# reference executor (tests/stepper_reference.c) plays its stepping, so both
# the fixture's fake clock and mcu/controller.c's edge service have somewhere
# to step. See tests/klipper_sensors/Makefile for the same pattern.
STEPPER_STUB_DIR := $(PRODUCT_ROOT)/tests/stub_stepper_local
STEPPER_TEST_OBJS := stepper_reference.o stepper_local_stub.o \
                     stepper_local_ref.o stepdir_gpio_accessor_stub.o
BOOT_CFLAGS := $(filter-out -DCONFIG_WANT_GPIO_LOCAL=1 $(SELECTOR_DEFS),$(CFLAGS)) \
               -I$(STEPPER_STUB_DIR) -include $(IMAGE_AUTOCONF)
BOOT_CORE := $(filter-out $(BUILD_DIR)/board_startup.o $(BUILD_DIR)/test_board_startup.o $(BUILD_DIR)/servo_pwm.o,$(OBJS))
FACTORIES := h723_resources.c tmc_config.c switch_config.c rewind_config.c feeder_config.c
FACTORY_OBJS := $(addprefix $(BOOT_DIR)/, $(FACTORIES:.c=.o))
# The BOOT variant preprocesses like the Diff one (no KM_TEST_HOSTED_RUNTIME),
# so it needs the same shipped production objects: the image factories and the
# command, sensor, ADC and hosted-startup services test_boxturtle_boot.c calls.
BOOT_PROD := $(BOOT_DIR)/config.o $(BOOT_DIR)/adc_config.o $(BOOT_DIR)/adc.o \
             $(BOOT_DIR)/switch_gpio.o $(BOOT_DIR)/sensors.o $(BOOT_DIR)/commands.o \
             $(BOOT_DIR)/messages.o $(BOOT_DIR)/diagnostic_startup.o $(BOOT_DIR)/calibration.o \
             $(BOOT_DIR)/encoder.o
BOOT_STEPPER_OBJS := $(addprefix $(BOOT_DIR)/, $(STEPPER_TEST_OBJS))
DIFF_DIR := $(BUILD_DIR)/diff
DIFF_EXTRA_CONFIG ?= shim/diff_autoconf.h
HOST_JOIN ?= n
PRODUCTION_CONTROLLER ?= n
DIFF_CFLAGS := $(BOOT_CFLAGS) $(if $(DIFF_EXTRA_CONFIG),-include $(DIFF_EXTRA_CONFIG)) $(if $(filter y,$(HOST_JOIN)),-DKM_TEST_HOSTED_RUNTIME -DKM_TEST_HOST_JOIN,) $(if $(filter y,$(PRODUCTION_CONTROLLER)),-DKM_TEST_PRODUCTION_CONTROLLER,)
DIFF_FACTORIES := config.c h723_resources.c tmc_config.c switch_config.c rewind_config.c feeder_config.c
DIFF_FACTORY_OBJS := $(addprefix $(DIFF_DIR)/, $(DIFF_FACTORIES:.c=.o))
DIFF_TARGET := $(DIFF_DIR)/test_boxturtle_diff
DIFF_CORE := $(BOOT_CORE)
DIFF_CONTROLLER_OBJ := $(if $(filter y,$(PRODUCTION_CONTROLLER)),$(DIFF_DIR)/controller.o,)
DIFF_HOSTED := $(DIFF_DIR)/diagnostic_startup.o
DIFF_ADC_OBJS :=
DIFF_STEPPER_OBJS := $(addprefix $(DIFF_DIR)/, $(STEPPER_TEST_OBJS))
ifneq ($(HOST_JOIN),y)
DIFF_FACTORIES += adc_config.c
DIFF_FACTORY_OBJS := $(addprefix $(DIFF_DIR)/, $(DIFF_FACTORIES:.c=.o))
DIFF_HOSTED += $(DIFF_DIR)/calibration.o
DIFF_ADC_OBJS += $(DIFF_DIR)/adc.o
endif

test: $(TARGET) $(BOOT_TARGET) $(DIFF_TARGET)
	@timeout 60s env ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1:abort_on_error=1 $(TARGET)
	@timeout 60s env ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1:abort_on_error=1 $(BOOT_TARGET)
	@timeout 60s env ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1:abort_on_error=1 $(DIFF_TARGET)

test-diff: $(DIFF_TARGET)
	@timeout 60s env ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1:abort_on_error=1 $(DIFF_TARGET)

test-fps-composed:
	@$(MAKE) --no-print-directory PRODUCTION_CONTROLLER=y DIFF_DIR=$(BUILD_DIR)/fps_toolhead DIFF_EXTRA_CONFIG=shim/fps_toolhead_autoconf.h $(BUILD_DIR)/fps_toolhead/test_boxturtle_diff
	@timeout 60s env ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1:abort_on_error=1 $(BUILD_DIR)/fps_toolhead/test_boxturtle_diff fps-toolhead-success
	@timeout 60s env ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1:abort_on_error=1 $(BUILD_DIR)/fps_toolhead/test_boxturtle_diff fps-toolhead-required
	@timeout 60s env ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1:abort_on_error=1 $(BUILD_DIR)/fps_toolhead/test_boxturtle_diff fps-invalid-pair
	@timeout 60s env ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1:abort_on_error=1 $(BUILD_DIR)/fps_toolhead/test_boxturtle_diff fps-max-travel
	@$(MAKE) --no-print-directory PRODUCTION_CONTROLLER=y DIFF_DIR=$(BUILD_DIR)/fps_no_toolhead DIFF_EXTRA_CONFIG=shim/fps_no_toolhead_autoconf.h $(BUILD_DIR)/fps_no_toolhead/test_boxturtle_diff
	@timeout 60s env ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1:abort_on_error=1 $(BUILD_DIR)/fps_no_toolhead/test_boxturtle_diff fps-no-toolhead-success

$(BOOT_DIR)/board_startup.o: $(PROTOTYPE_ROOT)/mcu/board_startup.c $(IMAGE_AUTOCONF)
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(BOOT_CFLAGS) -c $< -o $@

$(BOOT_DIR)/test_boxturtle_boot.o: test_boxturtle_boot.c $(IMAGE_AUTOCONF)
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(BOOT_CFLAGS) -c $< -o $@

$(BOOT_DIR)/pin_config.o: $(PRODUCT_ROOT)/mcu/pin_config.c $(IMAGE_AUTOCONF)
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(BOOT_CFLAGS) -c $< -o $@

$(BOOT_DIR)/%.o: $(PRODUCT_ROOT)/mcu/%.c $(IMAGE_AUTOCONF)
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(BOOT_CFLAGS) -c $< -o $@

$(BOOT_DIR)/stepper_reference.o: $(PRODUCT_ROOT)/tests/stepper_reference.c $(IMAGE_AUTOCONF)
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(BOOT_CFLAGS) -c $< -o $@

$(BOOT_DIR)/stepper_local_stub.o $(BOOT_DIR)/stepper_local_ref.o $(BOOT_DIR)/stepdir_gpio_accessor_stub.o: $(BOOT_DIR)/%.o: $(STEPPER_STUB_DIR)/%.c $(IMAGE_AUTOCONF)
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(BOOT_CFLAGS) -c $< -o $@

$(BOOT_TARGET): $(BOOT_DIR)/board_startup.o $(BOOT_DIR)/pin_config.o $(FACTORY_OBJS) $(BOOT_PROD) $(BOOT_DIR)/test_boxturtle_boot.o $(BOOT_STEPPER_OBJS) $(BOOT_CORE)
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(BOOT_CFLAGS) $^ $(LDFLAGS) -o $@

$(DIFF_DIR)/%.o: $(PRODUCT_ROOT)/mcu/%.c $(IMAGE_AUTOCONF) $(DIFF_EXTRA_CONFIG)
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(DIFF_CFLAGS) -c $< -o $@

$(DIFF_DIR)/controller.o: $(PRODUCT_ROOT)/mcu/controller.c $(IMAGE_AUTOCONF) $(DIFF_EXTRA_CONFIG)
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(DIFF_CFLAGS) -c $< -o $@

$(DIFF_DIR)/board_startup.o: $(PROTOTYPE_ROOT)/mcu/board_startup.c $(IMAGE_AUTOCONF) $(DIFF_EXTRA_CONFIG)
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(DIFF_CFLAGS) -c $< -o $@

$(DIFF_DIR)/test_boxturtle_boot.o: test_boxturtle_boot.c $(IMAGE_AUTOCONF) $(DIFF_EXTRA_CONFIG)
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(DIFF_CFLAGS) -c $< -o $@

$(DIFF_DIR)/commands.o: $(PRODUCT_ROOT)/mcu/commands.c $(IMAGE_AUTOCONF) $(DIFF_EXTRA_CONFIG)
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(DIFF_CFLAGS) -c $< -o $@

$(DIFF_DIR)/messages.o: $(PRODUCT_ROOT)/mcu/messages.c $(IMAGE_AUTOCONF) $(DIFF_EXTRA_CONFIG)
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(DIFF_CFLAGS) -c $< -o $@

$(DIFF_DIR)/stepper_reference.o: $(PRODUCT_ROOT)/tests/stepper_reference.c $(IMAGE_AUTOCONF) $(DIFF_EXTRA_CONFIG)
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(DIFF_CFLAGS) -c $< -o $@

$(DIFF_DIR)/stepper_local_stub.o $(DIFF_DIR)/stepper_local_ref.o $(DIFF_DIR)/stepdir_gpio_accessor_stub.o: $(DIFF_DIR)/%.o: $(STEPPER_STUB_DIR)/%.c $(IMAGE_AUTOCONF) $(DIFF_EXTRA_CONFIG)
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(DIFF_CFLAGS) -c $< -o $@

$(DIFF_TARGET): $(DIFF_DIR)/board_startup.o $(DIFF_DIR)/pin_config.o $(DIFF_FACTORY_OBJS) $(DIFF_ADC_OBJS) $(DIFF_DIR)/switch_gpio.o $(DIFF_DIR)/sensors.o $(DIFF_DIR)/encoder.o $(DIFF_DIR)/commands.o $(DIFF_DIR)/messages.o $(DIFF_HOSTED) $(DIFF_DIR)/test_boxturtle_boot.o $(DIFF_CONTROLLER_OBJ) $(DIFF_STEPPER_OBJS) $(DIFF_CORE)
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(DIFF_CFLAGS) $^ $(LDFLAGS) -o $@

# Selector home endstop (klipper_mmu/selector.h): real mcu/switch_gpio.c and
# mcu/controller.c against tests/klipper_stepdir's shared hosted shim, the
# same way tests/klipper_switch tests switch_gpio.c's other roles.
ENDSTOP_DIR := $(BUILD_DIR)/selector_endstop
ENDSTOP_TARGET := $(ENDSTOP_DIR)/test_selector_endstop
ENDSTOP_CFLAGS := -std=c11 -Wall -Wextra -Werror -pedantic -Wconversion -Wsign-conversion \
                  -fsanitize=address,undefined -fno-sanitize-recover=all -g -O2 -MMD -MP \
                  -DCONFIG_KLIPPER_MMU_RATE_TICK_US=1000 \
                  -I$(PRODUCT_ROOT)/tests/klipper_switch/shim_include \
                  -I$(PRODUCT_ROOT)/tests/klipper_stepdir/shim_include \
                  -I$(PRODUCT_ROOT)/tests/klipper_stepdir -I$(PRODUCT_ROOT)/include \
                  -I$(PRODUCT_ROOT)/mcu -I$(PRODUCT_ROOT)/tests/stub_stepper_local
ENDSTOP_LDFLAGS := -fsanitize=address,undefined -fno-sanitize-recover=all -lm
ENDSTOP_SRCS := $(PRODUCT_ROOT)/mcu/switch_gpio.c $(PRODUCT_ROOT)/mcu/controller.c \
                $(PRODUCT_ROOT)/mcu/clock.c $(PRODUCT_ROOT)/src/mcu_clock.c \
                $(PRODUCT_ROOT)/src/operation_core.c $(PRODUCT_ROOT)/src/selector_home.c $(PRODUCT_ROOT)/src/rate.c \
                $(PRODUCT_ROOT)/src/trajectory.c $(PRODUCT_ROOT)/src/step_segments.c \
                $(PRODUCT_ROOT)/src/follower.c $(PRODUCT_ROOT)/src/buffer_sensor.c \
                $(PRODUCT_ROOT)/src/current_regulator.c $(PRODUCT_ROOT)/src/event_queue.c \
                $(PRODUCT_ROOT)/src/device_profile.c $(PRODUCT_ROOT)/src/protocol_wire.c \
                $(PRODUCT_ROOT)/tests/klipper_stepdir/shim_klipper.c \
                $(PRODUCT_ROOT)/tests/mock_driver.c \
                $(PRODUCT_ROOT)/tests/stub_stepper_local/stepper_local_stub.c \
                $(PRODUCT_ROOT)/tests/stub_stepper_local/stepper_local_ref.c \
                $(PRODUCT_ROOT)/tests/stub_stepper_local/stepdir_gpio_accessor_stub.c \
                $(PRODUCT_ROOT)/tests/stepper_reference.c \
                test_selector_endstop.c
ENDSTOP_OBJS := $(patsubst $(PRODUCT_ROOT)/%.c,$(ENDSTOP_DIR)/%.o,$(filter $(PRODUCT_ROOT)/%,$(ENDSTOP_SRCS))) \
                $(ENDSTOP_DIR)/test_selector_endstop.o

.PHONY: test-selector-endstop
test-selector-endstop: $(ENDSTOP_TARGET)
	timeout 60s env ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1 $(ENDSTOP_TARGET)

$(ENDSTOP_DIR)/%.o: $(PRODUCT_ROOT)/%.c
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(ENDSTOP_CFLAGS) -c $< -o $@

$(ENDSTOP_DIR)/test_selector_endstop.o: test_selector_endstop.c
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(ENDSTOP_CFLAGS) -c $< -o $@

$(ENDSTOP_TARGET): $(ENDSTOP_OBJS)
	timeout 60s $(CC) $(ENDSTOP_OBJS) $(ENDSTOP_LDFLAGS) -o $@

run:
	@timeout 120s $(PYTHON) run_tests.py

clean:
	rm -rf $(BUILD_DIR)

-include $(OBJS:.o=.d)
