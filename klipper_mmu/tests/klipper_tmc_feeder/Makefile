# SPDX-License-Identifier: GPL-3.0-or-later
PYTHON ?= python3
BUILD_DIR ?= build/shared-tmc-feeder
.PHONY: test build
test:
	timeout 90s $(PYTHON) -B tests/klipper_tmc_feeder/run_shared.py --results $(BUILD_DIR)/results.json $(if $(IMAGE_AUTOCONF),--image-autoconf "$(IMAGE_AUTOCONF)")
build:
	timeout 70s $(PYTHON) -B tests/klipper_tmc_feeder/run_shared.py --build-only $(if $(IMAGE_AUTOCONF),--image-autoconf "$(IMAGE_AUTOCONF)")
