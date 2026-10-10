// SPDX-License-Identifier: GPL-3.0-or-later
#include <unistd.h>
#include <fcntl.h>
#include <string.h>
#include <stdlib.h>
#include "command.h"
#include "sched.h"
#include "stm32/gpio.h"
// shutdown(msg) normally registers msg as a new static_string_id via
// _DECL_STATIC_STR, which the dictionary gate would then see in the sim's
// klipper.dict and nowhere in the ARM one (SPI/I2C are not stubbed out on
// real hardware). These paths are only reachable if the add-on tries to use
// a bus this board doesn't model; shut down without minting a new string.
#undef shutdown
#define shutdown(msg) sched_shutdown(0)

struct spi_config spi_setup(uint32_t bus, uint8_t mode, uint32_t rate) { shutdown("not simulated"); return (struct spi_config){0}; }
void spi_prepare(struct spi_config config) { shutdown("not simulated"); }
void spi_transfer(struct spi_config config, uint8_t receive_data, uint8_t len, uint8_t *data) { shutdown("not simulated"); }

struct i2c_config i2c_setup(uint32_t bus, uint32_t rate, uint8_t addr) { shutdown("not simulated"); return (struct i2c_config){0}; }
int i2c_write(struct i2c_config config, uint8_t write_len, uint8_t *write) { shutdown("not simulated"); return 0; }
int i2c_read(struct i2c_config config, uint8_t reg_len, uint8_t *reg, uint8_t read_len, uint8_t *read) { shutdown("not simulated"); return 0; }

void command_reset(uint32_t *args) {
    char cmdline[256];
    int fd = open("/proc/self/cmdline", 0);
    if (fd >= 0) {
        int len = read(fd, cmdline, sizeof(cmdline) - 1);
        close(fd);
        if (len > 0) {
            cmdline[len] = '\0';
            char *args_out[16];
            int argc = 0;
            char *p = cmdline;
            while (p < cmdline + len && argc < 15) {
                args_out[argc++] = p;
                p += strlen(p) + 1;
            }
            args_out[argc] = NULL;
            execv("/proc/self/exe", args_out);
        }
    }
    exit(1);
}
