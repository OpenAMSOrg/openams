CC ?= gcc
ROOT := ../..
BUILD := $(ROOT)/build/klipper_startup_composition
CFLAGS := -std=c11 -Wall -Wextra -Werror -Wconversion -Wsign-conversion -fsanitize=address,undefined -fno-sanitize-recover=all -g -O1 -fno-pie -no-pie \
 -DCONFIG_CLOCK_FREQ=520000000u -DCONFIG_KLIPPER_MMU_DIAGNOSTIC_TIMEOUT_US=1000000u \
 -DCONFIG_KLIPPER_MMU_DIFF_BUFFER=1 -DCONFIG_WANT_GPIO_LOCAL=1 \
 -DCONFIG_HARD_PWM_LOCAL_MAX_PAIRS=8 -DCONFIG_KLIPPER_MMU_LANE_COUNT=4 \
 -DCONFIG_KLIPPER_MMU_RATE_TICK_US=1000 \
 -Ishim -I$(ROOT)/tests/klipper_board_startup/shim -I$(ROOT)/mcu -I$(ROOT)/include -I$(ROOT)/tests -I$(ROOT) \
 -I$(ROOT)/tests/stub_stepper_local
SRC := test_composition.c $(ROOT)/mcu/board_startup.c $(ROOT)/mcu/diagnostic_startup.c \
 $(ROOT)/mcu/pwm_bridge_gpio.c \
 $(ROOT)/mcu/controller.c $(ROOT)/mcu/commands.c $(ROOT)/mcu/messages.c \
 $(ROOT)/src/operation_core.c $(ROOT)/src/selector_home.c $(ROOT)/src/device_profile.c $(ROOT)/src/resource_plan.c \
 $(ROOT)/src/event_queue.c $(ROOT)/src/buffer_sensor.c $(ROOT)/src/current_regulator.c \
 $(ROOT)/src/follower.c $(ROOT)/src/rate.c $(ROOT)/src/trajectory.c $(ROOT)/src/step_segments.c $(ROOT)/src/protocol_wire.c \
 $(ROOT)/src/step_dir_driver.c $(ROOT)/src/pwm_bridge_driver.c $(ROOT)/src/adc_pair.c \
 $(ROOT)/src/tmc2209.c $(ROOT)/src/tmc_uart_codec.c $(ROOT)/src/tmc_uart_config.c \
 $(ROOT)/src/mcu_clock.c $(ROOT)/tests/mock_driver.c $(ROOT)/tests/stub_stepper_local/stepper_local_stub.c \
 $(ROOT)/tests/stub_stepper_local/stepper_local_ref.c $(ROOT)/tests/stub_stepper_local/stepdir_gpio_accessor_stub.c \
 $(ROOT)/tests/stepper_reference.c
.PHONY: test
$(BUILD)/test_composition: $(SRC)
	@mkdir -p $(BUILD)
	timeout 60s $(CC) $(CFLAGS) $(SRC) -fsanitize=address,undefined -no-pie -lm -o $@
test: $(BUILD)/test_composition
	@set -e; for case in ready missing_calibration fault timeout powered_ready powered_timer_conflict powered_conflict powered_partial powered_fault; do \
	  timeout 5s env ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 $(BUILD)/test_composition $$case > $(BUILD)/$$case.log 2>&1 || { cat $(BUILD)/$$case.log; exit 1; }; \
	done
	@printf 'pass: ready missing_calibration fault timeout powered_ready powered_timer_conflict powered_conflict powered_partial powered_fault\n' | tee $(BUILD)/summary.txt
