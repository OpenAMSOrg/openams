# SPDX-License-Identifier: GPL-3.0-or-later
# Native tests for mcu/encoder.c's GPIO counting logic (see test_encoder.c's
# own header comment for what is and is not covered here).
CC ?= gcc
BUILD_DIR ?= build/klipper_encoder
CFLAGS := -std=c11 -Wall -Wextra -Werror -pedantic -Wconversion -Wsign-conversion \
 -fsanitize=address,undefined -fno-sanitize-recover=all -g -O2 -MMD -MP \
 -Itests/klipper_encoder/shim -Iinclude -Imcu
LDFLAGS := -fsanitize=address,undefined -fno-sanitize-recover=all -lm
SRCS := mcu/encoder.c tests/klipper_encoder/test_encoder.c
OBJS := $(patsubst %.c,$(BUILD_DIR)/%.o,$(SRCS))
.PHONY: test
test: $(BUILD_DIR)/test_encoder
	timeout 60s env ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1 $<
$(BUILD_DIR)/test_encoder: $(OBJS)
	timeout 60s $(CC) $(OBJS) $(LDFLAGS) -o $@
$(BUILD_DIR)/%.o: %.c
	@mkdir -p $(dir $@)
	timeout 60s $(CC) $(CFLAGS) -c $< -o $@
-include $(OBJS:.o=.d)
