# SPDX-License-Identifier: GPL-3.0-or-later
PYTHON ?= python3
BUILD_DIR ?= build/shared-pwm-routes
.PHONY: test
test:
	timeout 170s $(PYTHON) -B tests/klipper_pwm_routes/run_tests.py $(BUILD_DIR)
