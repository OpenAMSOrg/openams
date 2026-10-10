// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_AUTOCONF_H
#define TEST_AUTOCONF_H

#include <stdint.h>

extern uint32_t test_clock_freq;
extern uint32_t test_device_id;
extern uint32_t test_board_id;
extern uint32_t test_lane_count;
extern uint32_t test_protocol_version;

#define CONFIG_CLOCK_FREQ test_clock_freq
#define CONFIG_KLIPPER_MMU_DEVICE_ID test_device_id
#define CONFIG_KLIPPER_MMU_BOARD_ID test_board_id
#define CONFIG_KLIPPER_MMU_LANE_COUNT test_lane_count
#define CONFIG_KLIPPER_MMU_PROTOCOL_VERSION test_protocol_version
#define CONFIG_KLIPPER_MMU_INTEGRATION_VERSION 1

#endif
