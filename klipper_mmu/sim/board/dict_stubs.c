// SPDX-License-Identifier: GPL-3.0-or-later
#include "command.h"
#include "sched.h"

DECL_CONSTANT("ADC_MAX", 4095);
DECL_CONSTANT_STR("BUS_PINS_i2c1_PB6_PB7", "PB6,PB7");
DECL_CONSTANT_STR("BUS_PINS_i2c1_PB8_PB9", "PB8,PB9");
DECL_CONSTANT_STR("BUS_PINS_i2c2_PB10_PB11", "PB10,PB11");
DECL_CONSTANT_STR("BUS_PINS_i2c3_PA8_PC9", "PA8,PC9");
DECL_CONSTANT_STR("BUS_PINS_spi1", "PA6,PA7,PA5");
DECL_CONSTANT_STR("BUS_PINS_spi1a", "PB4,PB5,PB3");
DECL_CONSTANT_STR("BUS_PINS_spi2", "PB14,PB15,PB13");
DECL_CONSTANT_STR("BUS_PINS_spi2a", "PC2,PC3,PB10");
DECL_CONSTANT_STR("BUS_PINS_spi2b", "PI2,PI3,PI1");
DECL_CONSTANT_STR("BUS_PINS_spi3a", "PC11,PC12,PC10");
DECL_CONSTANT_STR("BUS_PINS_spi4", "PE13,PE14,PE12");
DECL_CONSTANT_STR("BUS_PINS_spi5", "PF8,PF9,PF7");
DECL_CONSTANT_STR("BUS_PINS_spi5a", "PH7,PF11,PH6");
DECL_CONSTANT_STR("BUS_PINS_spi6", "PG12,PG14,PG13");
DECL_CONSTANT_STR("MCU", "stm32h723xx");
DECL_CONSTANT("PWM_MAX", 32768);
DECL_CONSTANT_STR("RESERVE_PINS_USB", "PA11,PA12");
DECL_CONSTANT_STR("RESERVE_PINS_crystal", "PH0,PH1");

DECL_ENUMERATION("i2c_bus", "i2c1_PB6_PB7", 0);
DECL_ENUMERATION("i2c_bus", "i2c1_PB8_PB9", 1);
DECL_ENUMERATION("i2c_bus", "i2c2_PB10_PB11", 2);
DECL_ENUMERATION("i2c_bus", "i2c3_PA8_PC9", 3);

DECL_ENUMERATION("spi_bus", "spi1", 1);
DECL_ENUMERATION("spi_bus", "spi1a", 2);
DECL_ENUMERATION("spi_bus", "spi2", 0);
DECL_ENUMERATION("spi_bus", "spi2a", 3);
DECL_ENUMERATION("spi_bus", "spi2b", 9);
DECL_ENUMERATION("spi_bus", "spi3a", 4);
DECL_ENUMERATION("spi_bus", "spi4", 5);
DECL_ENUMERATION("spi_bus", "spi5", 6);
DECL_ENUMERATION("spi_bus", "spi5a", 7);
DECL_ENUMERATION("spi_bus", "spi6", 8);

DECL_ENUMERATION("i2c_bus_status", "SUCCESS", 0);
DECL_ENUMERATION("i2c_bus_status", "NACK", 1);
DECL_ENUMERATION("i2c_bus_status", "BUS_TIMEOUT", 2);
DECL_ENUMERATION("i2c_bus_status", "START_NACK", 3);
DECL_ENUMERATION("i2c_bus_status", "START_READ_NACK", 4);

void command_reset(uint32_t *args);
DECL_COMMAND(command_reset, "reset");

void stm32_strings(void) {
    shutdown("Pin not reserved local");
    shutdown("Pin reserved local");
    shutdown("Not a valid pin");
    shutdown("Not a valid ADC pin");
    shutdown("ADC unit reserved by local pair");
    shutdown("Invalid spi bus");
    shutdown("Unsupported i2c bus");
    shutdown("Invalid PWM channel");
    shutdown("PWM already programmed with different pulse duration");
    shutdown("PWM already programmed at different speed");
    shutdown("PWM timer locally owned");
    shutdown("Not a valid PWM pin");
}
