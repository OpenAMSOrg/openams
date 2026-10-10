# SPDX-License-Identifier: GPL-3.0-or-later
BOARD_TEST := ../klipper_board_startup
BUILD_DIR ?= build/can-actuation
IMAGE_AUTOCONF ?=
KLIPPER_DICTIONARY ?=
# Optional absolute overlay header, e.g. switch_arrival_autoconf.h for an
# FPS-arrival image header.
EXTRA_CONFIG ?=

TARGET := $(abspath $(BUILD_DIR)/host_h723_join)
BOARD_BUILD := $(abspath $(BUILD_DIR)/board)
BOARD_DIFF := $(BOARD_BUILD)/diff
# diff: EXTRA_CONFIG switch overlay; switched: switched image; fps: image as built.
MODE ?= diff
HOST_JOIN := $(if $(filter switched,$(MODE)),y,n)


.PHONY: all build test clean
all: test

build:
	@test -f "$(IMAGE_AUTOCONF)" || { echo "Set IMAGE_AUTOCONF to the BoxTurtle H723 actuation autoconf.h"; exit 2; }
	@test -f "$(KLIPPER_DICTIONARY)" || { echo "Set KLIPPER_DICTIONARY to the matching klipper.dict"; exit 2; }
	$(MAKE) -C $(BOARD_TEST) BUILD_DIR=$(BOARD_BUILD) DIFF_DIR=$(BOARD_DIFF) \
		DIFF_TARGET=$(TARGET) IMAGE_AUTOCONF=$(IMAGE_AUTOCONF) \
		DIFF_EXTRA_CONFIG=$(EXTRA_CONFIG) HOST_JOIN=$(HOST_JOIN) $(TARGET)

test: build
	KM_WIRE_DICTIONARY=$(KLIPPER_DICTIONARY) timeout 90s python3 run_join.py $(TARGET) $(MODE)

clean:
	rm -rf $(BUILD_DIR)
