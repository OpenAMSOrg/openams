# SPDX-License-Identifier: GPL-3.0-or-later
# Klipper-MMU MCU makefile fragment

# Kconfig.driver_api declares these options; the separately installed
# upstream extension implements them. Stop a build, not menuconfig/clean.
ifneq ($(filter y,$(CONFIG_WANT_ADC_LOCAL) $(CONFIG_WANT_HARD_PWM_QUERY) $(CONFIG_WANT_HARD_PWM_LOCAL_PAIR) $(CONFIG_WANT_TMCUART_LOCAL) $(CONFIG_WANT_GPIO_LOCAL) $(CONFIG_WANT_STEPPER_LOCAL)),)
ifneq ($(words $(wildcard src/tmcuart.h src/stm32/pwm.h src/stm32/adc_local.h src/stm32/gpio_local.h)),4)
.PHONY: klipper-mmu-driver-api
$(OUT)autoconf.h: klipper-mmu-driver-api
klipper-mmu-driver-api:
	$(error Klipper-MMU: enabled driver API options need tools/install.py install-driver-api; install it or disable them)
endif
endif

ifeq ($(CONFIG_KLIPPER_MMU),y)
dirs-y += src/klipper_mmu/mcu src/klipper_mmu/src
src-y += klipper_mmu/mcu/clock.c klipper_mmu/src/mcu_clock.c
src-y += klipper_mmu/mcu/messages.c
src-y += klipper_mmu/mcu/commands.c klipper_mmu/mcu/enumerations.c
src-y += klipper_mmu/mcu/controller.c
src-y += klipper_mmu/mcu/config.c
src-y += klipper_mmu/src/rate.c klipper_mmu/src/trajectory.c klipper_mmu/src/step_segments.c klipper_mmu/src/buffer_sensor.c
src-y += klipper_mmu/src/follower.c klipper_mmu/src/operation_core.c
src-y += klipper_mmu/src/event_queue.c klipper_mmu/src/device_profile.c
src-y += klipper_mmu/src/protocol_wire.c klipper_mmu/src/current_regulator.c
CFLAGS += -Isrc/klipper_mmu/include
# 128 KiB parts (KLIPPER_MMU_SIZE_OPT_BUILD): this package's own objects build
# -Os (the last -O wins; LTO keeps it per function). Upstream objects and every
# larger image keep Klipper's -O2.
ifeq ($(CONFIG_KLIPPER_MMU_SIZE_OPT_BUILD),y)
$(OUT)src/klipper_mmu/%.o: CFLAGS += -Os
endif
# selector_home.c and operation_core.c's own selection-phase state machine
# (KM_SELECTOR_BUILD below) are only reachable through request_home()/
# start_select(), themselves only reachable when this image drives a
# selector actuator. A KLIPPER_MMU_SELECTOR_KIND_NONE image (independent
# per-lane feeders) never selects a lane, so it does not need to link or
# carry that code -- flash on the smallest targets (e.g. G0B0) has no room
# to spare for a phase it can never enter.
ifneq ($(filter y,$(CONFIG_KLIPPER_MMU_SELECTOR_STEPPER) $(CONFIG_KLIPPER_MMU_SERVO)),)
src-y += klipper_mmu/src/selector_home.c
else
CFLAGS += -DKM_SELECTOR_BUILD=0
endif
# Images of up to 8 lanes keep the 8-lane layout (include/klipper_mmu/types.h).
CFLAGS += -DKM_MAX_LANES=$(if $(filter 9 10 11 12,$(CONFIG_KLIPPER_MMU_LANE_COUNT)),12,8)u
# KM_PIN_CONFIG_MAX_CLAIMS/KM_RESOURCE_MAX_CLAIMS (mcu/pin_config.h,
# include/klipper_mmu/resource_plan.h) carry one extra per-lane claim each
# for the INDEXED selector kind's per-gate index switch (KM_PIN_SELECTOR_GATE,
# include/klipper_mmu/selector.h). Only BTT ViViD and Claymore select it; every
# other image gets the smaller per-image capacity instead of paying bss for a
# role it never claims. Both headers fall back to the full (INDEXED-sized)
# formula when neither define is passed in, so native and test builds are
# unaffected.
ifeq ($(CONFIG_KLIPPER_MMU_SELECTOR_KIND_INDEXED),y)
CFLAGS += -DKM_PIN_CONFIG_MAX_CLAIMS='(11u * KM_MAX_LANES + 12u)'
CFLAGS += -DKM_RESOURCE_MAX_CLAIMS='(9u * KM_MAX_LANES + 64u)'
else
CFLAGS += -DKM_PIN_CONFIG_MAX_CLAIMS='(10u * KM_MAX_LANES + 12u)'
CFLAGS += -DKM_RESOURCE_MAX_CLAIMS='(8u * KM_MAX_LANES + 64u)'
endif
CFLAGS_klipper.elf += -lm

ifeq ($(CONFIG_MACH_STM32),y)
src-y += klipper_mmu/mcu/pin_config.c klipper_mmu/src/resource_plan.c
ifeq ($(CONFIG_KLIPPER_MMU_PIN_CONFIG),y)
# Refuse a pin map the firmware would reject at startup, before compiling.
$(OUT)autoconf.h: $(OUT)klipper-mmu-pins.ok
$(OUT)klipper-mmu-pins.ok: $(KCONFIG_CONFIG) $(wildcard src/stm32/hard_pwm.c src/stm32/stm32h7_adc.c)
	@echo "  Checking klipper-mmu pin map"
	$(Q)mkdir -p $(OUT)
	$(Q)$(PYTHON) src/klipper_mmu/tools/pin_plan_check.py $(KCONFIG_CONFIG) src
	$(Q)touch $@
endif
src-y += klipper_mmu/mcu/feeder_config.c klipper_mmu/src/step_dir_driver.c
src-y += klipper_mmu/mcu/tmc_config.c klipper_mmu/src/tmc2209.c
src-y += klipper_mmu/src/tmc_uart_config.c
src-y += klipper_mmu/mcu/switch_config.c
endif
# These baseline board services use upstream GPIO and STEP/DIR on both
# candidate targets. Optional H723-only PWM/ADC remain gated below.
ifneq ($(filter y,$(CONFIG_MACH_STM32H723) $(CONFIG_KLIPPER_MMU_F207_BOOT_GATES) $(CONFIG_KLIPPER_MMU_F4X5_BOOT_GATES) $(CONFIG_KLIPPER_MMU_F765_BOOT_GATES) $(CONFIG_KLIPPER_MMU_G4_BOOT_GATES) $(CONFIG_KLIPPER_MMU_H743_H750_BOOT_GATES) $(CONFIG_KLIPPER_MMU_G0B_BOOT_GATES)),)
src-y += klipper_mmu/mcu/board_startup.c
ifeq ($(CONFIG_KLIPPER_MMU_DEVICE_BOXTURTLE),y)
ifeq ($(CONFIG_KLIPPER_MMU_BOARD_AFC_LITE),y)
# Analog startup needs host-delivered calibration; hosted startup owns its
# own init command. Neither may run the legacy early boot task.
ifneq ($(CONFIG_KLIPPER_MMU_DIFF_BUFFER),y)
ifneq ($(CONFIG_KLIPPER_MMU_DIAGNOSTIC_STARTUP),y)
src-y += klipper_mmu/mcu/boxturtle_boot.c
endif
endif
endif
endif
src-y += klipper_mmu/mcu/stepdir_gpio.c
src-y += klipper_mmu/mcu/h723_resources.c
src-y += klipper_mmu/mcu/switch_gpio.c
src-y += klipper_mmu/mcu/sensors.c
# Only an image with the encoder capability links its driver.
ifeq ($(CONFIG_KLIPPER_MMU_ENCODER),y)
src-y += klipper_mmu/mcu/encoder.c
else
CFLAGS += -DKM_ENCODER_BUILD=0
endif
# The rewind driver core and its GPIO backend both have exactly one caller
# (board_startup.c's KM_HAS_PWM_SUPPORT block, mcu/pwm_bridge_gpio.c itself),
# and both require this same option; neither is reachable without it.
ifeq ($(CONFIG_WANT_HARD_PWM_LOCAL_PAIR),y)
src-y += klipper_mmu/mcu/rewind_config.c klipper_mmu/src/pwm_bridge_driver.c
src-y += klipper_mmu/mcu/pwm_bridge_gpio.c
endif
# adc.c/adc_pair.c/adc_config.c implement the DiffA/DiffB analog buffer; only
# WANT_ADC_LOCAL (the H723 diff-buffer path; board_startup.c's KM_HAS_ADC_
# SUPPORT requires it too) ever calls into them. Plain WANT_ADC defaults on
# for every non-limited-code-size STM32 part (upstream src/Kconfig), which
# used to pull these in on every such image (e.g. G0B0/G0B1) whether or not
# it could use them. mcu/sensors.c's own ADC decode is gated the same way
# (KM_DIFF_BUFFER_BUILD) so it stops calling them when they are not linked.
ifeq ($(CONFIG_WANT_ADC_LOCAL),y)
src-y += klipper_mmu/mcu/adc.c klipper_mmu/src/adc_pair.c
src-y += klipper_mmu/mcu/adc_config.c
ifeq ($(CONFIG_KLIPPER_MMU_DIFF_BUFFER),y)
src-y += klipper_mmu/mcu/calibration.c
ifeq ($(CONFIG_WANT_GPIO_LOCAL),y)
ifneq ($(CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN),y)
src-y += klipper_mmu/mcu/diff_probe.c
endif
endif
endif
else
CFLAGS += -DKM_DIFF_BUFFER_BUILD=0
endif
# A host-epoch build links no hardware identity provider; diagnostic startup
# binds the host epoch and has no RNG or deterministic fallback.
ifneq ($(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_HOST),y)
ifeq ($(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H723),y)
src-y += klipper_mmu/mcu/boot_identity_h723.c
endif
ifneq ($(filter y,$(CONFIG_KLIPPER_MMU_F207_BOOT_GATES) $(CONFIG_KLIPPER_MMU_F4X5_BOOT_GATES) $(CONFIG_KLIPPER_MMU_F765_BOOT_GATES) $(CONFIG_KLIPPER_MMU_G4_BOOT_GATES) $(CONFIG_KLIPPER_MMU_H743_H750_BOOT_GATES)),)
ifneq ($(filter y,$(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F207) $(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F4X5) $(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F765) $(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_G4) $(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H743_H750)),)
src-y += klipper_mmu/mcu/boot_identity_rng_stm32.c
endif
endif
endif
ifeq ($(CONFIG_KLIPPER_MMU_DIAGNOSTIC_STARTUP),y)
src-y += klipper_mmu/mcu/diagnostic_startup.c
endif
endif
ifeq ($(CONFIG_WANT_TMCUART_LOCAL),y)
ifeq ($(CONFIG_KLIPPER_MMU_TMC_UART_SHARED),y)
src-y += klipper_mmu/src/tmc_uart_codec.c
src-y += klipper_mmu/mcu/tmc_uart_gpio.c
src-y += klipper_mmu/mcu/tmc_feeder.c
ifneq ($(filter y,$(CONFIG_MACH_STM32F207) $(CONFIG_MACH_STM32F405) $(CONFIG_MACH_STM32F407) $(CONFIG_MACH_STM32F765)),)
ifeq ($(filter y,$(CONFIG_KLIPPER_MMU_F207_BOOT_GATES) $(CONFIG_KLIPPER_MMU_F4X5_BOOT_GATES) $(CONFIG_KLIPPER_MMU_F765_BOOT_GATES)),)
src-y += klipper_mmu/mcu/stepdir_gpio.c
endif
endif
endif
endif
# Selector servo (klipper_mmu/selector.h): Klipper's own scheduled software
# PWM on a plain GPIO, no extra driver API. Independent of WANT_TMCUART_LOCAL.
ifeq ($(CONFIG_KLIPPER_MMU_SERVO),y)
src-y += klipper_mmu/mcu/servo_pwm.c
endif
endif
