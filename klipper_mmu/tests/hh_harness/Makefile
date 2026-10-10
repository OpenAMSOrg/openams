# SPDX-License-Identifier: GPL-3.0-or-later
# Happy Hare harness (HH-1, Milestone 1): build hh_shim.c into a shared
# library (the real core, tests/mock_driver.c, tests/stepper_reference.c; no
# new core code) and run hh_probe.py against the pinned, disposable extracted
# Happy Hare tree. See tests/hh_harness/README.md.
CC ?= gcc
PRODUCT_ROOT ?= ../..
BUILD_DIR ?= build
LIB := $(BUILD_DIR)/libhh_shim.so

# Shared build/ tree belongs to this package, so the extracted archive and the
# integration klippy venv resolve the same way from the main checkout and from
# any worktree. KLIPPER_MMU_BUILD_ROOT overrides it.
SHARED_BUILD ?= $(if $(KLIPPER_MMU_BUILD_ROOT),$(KLIPPER_MMU_BUILD_ROOT),$(PRODUCT_ROOT)/build)

ARCHIVE := $(PRODUCT_ROOT)/references/Happy-Hare-c54c736043a49f901ab685fa031d4f4edd066dc6.tar.gz
HH_ROOT ?= $(SHARED_BUILD)/upstream/happy-hare-c54c736
HH_PYTHON ?= $(SHARED_BUILD)/integration-env/klippy/bin/python

# No -fsanitize here: this is a shared library dlopen()'d into a plain
# (non-ASan) Python interpreter via ctypes, where an ASan-instrumented .so
# loaded after process start is unsupported (runtime not first in link order).
CFLAGS := -std=c11 -Wall -Wextra -Werror -pedantic -Wconversion -Wsign-conversion -fPIC \
	-I$(PRODUCT_ROOT)/include -I$(PRODUCT_ROOT)/tests -g -O2
LDFLAGS := -shared -lm

CORE_SRCS := $(addprefix $(PRODUCT_ROOT)/src/, rate.c trajectory.c selector_home.c \
	buffer_sensor.c follower.c operation_core.c event_queue.c device_profile.c \
	protocol_wire.c current_regulator.c tmc2209.c tmc_uart_codec.c tmc_uart_config.c \
	step_dir_driver.c step_segments.c adc_pair.c pwm_bridge_driver.c mcu_clock.c \
	resource_plan.c)
MOCK_SRCS := $(PRODUCT_ROOT)/tests/mock_driver.c $(PRODUCT_ROOT)/tests/stepper_reference.c
SOURCES := hh_shim.c $(CORE_SRCS) $(MOCK_SRCS)

# The milestone set (build/b2/review_hh_harness_review.md Milestone 1): the
# five-file teleport baseline for comparison, then the three real-core classes.
BASELINE := test.test_mmu_toolchange test.test_mmu_motion test.test_mmu_endless_spool \
	test.test_mmu_sensor_runout test.test_mmu_check_gate
REALCORE := test.test_mmu_motion.TestPreload test.test_mmu_motion.TestQuietPlacement \
	test.test_mmu_toolchange.TestUnload

.PHONY: build extract test clean

build: $(LIB)

$(LIB): $(SOURCES)
	@mkdir -p $(BUILD_DIR)
	timeout 120s $(CC) $(CFLAGS) $(SOURCES) $(LDFLAGS) -o $@

extract:
	@if [ ! -d "$(HH_ROOT)" ]; then \
		echo "extracting $(ARCHIVE) -> $(HH_ROOT)"; \
		mkdir -p "$(dir $(HH_ROOT))"; \
		tmp=$$(mktemp -d) && tar -xzf "$(ARCHIVE)" -C "$$tmp" && \
		mv "$$tmp"/Happy-Hare-* "$(HH_ROOT)" && rmdir "$$tmp"; \
	fi

test: build extract
	@if [ ! -x "$(HH_PYTHON)" ]; then \
		echo "SKIP: Happy Hare harness needs the extracted archive and a Python with greenlet+jinja2 (missing HH_PYTHON=$(HH_PYTHON))"; \
		exit 0; \
	fi
	@echo "== teleport baseline (five files, for comparison) =="
	@for f in $(BASELINE); do \
		( cd "$(HH_ROOT)" && timeout 60s env KM_HH_LIB=$(abspath $(LIB)) \
			$(HH_PYTHON) $(abspath hh_probe.py) --teleport $$f ) || exit 1; \
	done
	@echo "== real core: PRELOAD/UNLOAD (Milestone 1) =="
	@for f in $(REALCORE); do \
		( cd "$(HH_ROOT)" && timeout 120s env KM_HH_LIB=$(abspath $(LIB)) \
			$(HH_PYTHON) $(abspath hh_probe.py) $$f ) || exit 1; \
	done

clean:
	rm -rf $(BUILD_DIR)
