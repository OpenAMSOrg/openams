// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_ENCODER_AUTOCONF_H
#define TEST_ENCODER_AUTOCONF_H
/* This suite tests the GPIO counting logic (km_encoder_gpio_setup/_sample/
 * _get_clicks) only, not the Kconfig pin-resolution factory km_config_encoder
 * (covered by the mock board_startup.c suite's contract-level fakes and by
 * tests/test_pin_config.py's real Kconfig-driven pin plan). Disabling pin
 * config here removes km_config_encoder's only real dependency, so no
 * km_config_pins_view fake is needed to link this suite. */
#define CONFIG_WANT_GPIO_LOCAL 0
#define CONFIG_KLIPPER_MMU_PIN_CONFIG 0
/* The whole point of this suite: exercise the enabled counting/GPIO logic,
 * not the disabled-build stub (mcu/encoder.c's own #else branch covers
 * that shape; it is trivial by construction). */
#define CONFIG_KLIPPER_MMU_ENCODER 1
#endif
