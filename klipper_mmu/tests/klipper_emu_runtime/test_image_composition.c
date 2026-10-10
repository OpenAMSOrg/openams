// SPDX-License-Identifier: GPL-3.0-or-later
/* Compile the shipped EMU image settings with the production factories. */
#include "autoconf.h"
#include "config.h"
#include "feeder_config.h"
#include "h723_resources.h"
#include "pin_config.h"
#include "switch_config.h"
#include "tmc_config.h"
#include <assert.h>

#define PIN(port, bit) (((port) - 'A') * 16u + (bit))

int main(void)
{
    _Static_assert(CONFIG_MACH_STM32H723 && CONFIG_KLIPPER_MMU_DEVICE_EMU,
                   "requires the H723 EMU image");
    _Static_assert(CONFIG_KLIPPER_MMU_LANE_COUNT == 5,
                   "requires the five-lane EMU image");
    _Static_assert(CONFIG_KLIPPER_MMU_DIAGNOSTIC_STARTUP,
                   "requires hosted diagnostic startup");

    km_device_profile_t profile;
    km_pin_config_t pins;
    km_switch_gpio_config_t switches;
    km_h723_resource_plan_t plan;
    km_step_dir_config_t feeders[KM_MAX_LANES];
    km_tmc_uart_config_t tmc[KM_MAX_LANES];
    assert(km_config_profile(&profile, NULL) == KM_OK);
    assert(profile.lane_count == 5 && profile.load_is_virtual_hub);
    assert(profile.has_load_sensors && profile.has_hub_sensor);
    assert(!profile.has_powered_rewind && !profile.has_proportional_buffer);
    assert(km_config_pins(&pins, NULL) == KM_OK);
    assert(km_config_switches(&switches) == KM_OK);
    assert(km_config_feeders(feeders) == KM_OK);
    assert(km_config_tmc(tmc) == KM_OK);
    assert(km_config_h723_resources(&plan, NULL) == KM_OK);
    assert(pins.count == 33u && plan.count == pins.count);
    assert(switches.hub.pin == KM_SWITCH_PIN_UNUSED);
    assert(pins.pin[KM_PIN_HUB][0] == KM_PIN_UNUSED);
    for (unsigned lane = 0; lane < 5u; ++lane) {
        assert(pins.pin[KM_PIN_STEP][lane] == PIN('D', lane));
        assert(pins.pin[KM_PIN_DIR][lane] == PIN('D', lane + 5u));
        assert(pins.pin[KM_PIN_ENABLE][lane] == PIN('D', lane + 10u));
        assert(pins.pin[KM_PIN_UART][lane] == PIN('E', lane));
        assert(pins.pin[KM_PIN_BAY][lane] == PIN('E', lane + 5u));
        assert(pins.pin[KM_PIN_LOAD][lane] == PIN('E', lane + 10u));
        assert(switches.bay[lane].pin == pins.pin[KM_PIN_BAY][lane]);
        assert(switches.load[lane].pin == pins.pin[KM_PIN_LOAD][lane]);
        assert(tmc[lane].tmc.run_current_ma == profile.default_run_current_ma);
        assert(tmc[lane].tmc.hold_current_ma == profile.default_hold_current_ma);
        assert(feeders[lane].maximum_velocity_steps_s >= profile.default_feed_velocity_steps_s);
    }
    assert(switches.toolhead.pin == PIN('C', 0));
    assert(switches.buffer_low.pin == PIN('C', 1));
    assert(switches.buffer_high.pin == PIN('C', 2));
    for (size_t i = 0; i < plan.count; ++i) {
        assert(plan.claims[i].kind == KM_RESOURCE_PIN);
        for (size_t j = i + 1; j < plan.count; ++j)
            assert(plan.claims[i].instance != plan.claims[j].instance);
    }
    return 0;
}
