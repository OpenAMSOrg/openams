// SPDX-License-Identifier: GPL-3.0-or-later
#include "feeder_config.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

int main(int argc, char **argv)
{
    km_step_dir_config_t out[KM_MAX_LANES], before[KM_MAX_LANES];
    memset(out, 0xa5, sizeof(out));
    memcpy(before, out, sizeof(out));
    km_result_t result = km_config_feeders(argc > 1 && !strcmp(argv[1], "null") ? NULL : out);
    if (result != KM_OK) assert(!memcmp(out, before, sizeof(out)));
    printf("{\"result\":%d,\"lanes\":[", (int)result);
    if (result == KM_OK) {
        for (unsigned i = 0; i < KM_MAX_LANES; ++i) {
            km_step_dir_config_t *c = &out[i];
            printf("%s[%u,%u,%u,%u,%u,%u]", i ? "," : "",
                   (unsigned)c->enable_active_high, (unsigned)c->step_active_high,
                   (unsigned)c->forward_high, c->pulse_high_us, c->pulse_low_us,
                   c->maximum_velocity_steps_s);
        }
    }
    puts("]}");
    return 0;
}
