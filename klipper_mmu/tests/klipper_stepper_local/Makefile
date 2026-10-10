# SPDX-License-Identifier: GPL-3.0-or-later
PYTHON ?= python3
BUILD_DIR ?= build/stepper-local

.PHONY: test

test:
	timeout 280s $(PYTHON) -B tests/klipper_stepper_local/run_tests.py $(BUILD_DIR)
