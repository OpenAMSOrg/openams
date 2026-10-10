# SPDX-License-Identifier: GPL-3.0-or-later
PYTHON ?= python3
BUILD_DIR ?= build/shared-tmc-uart
.PHONY: test

test:
	timeout 410s $(PYTHON) -B tests/klipper_tmc_uart/run_shared.py $(BUILD_DIR)
