// SPDX-License-Identifier: GPL-3.0-or-later
#include "switch_config.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

static void print_input(const km_switch_input_t *in)
{
    printf("[%u,%d,%u]", (unsigned)in->pin, (int)in->pull, (unsigned)in->active_high);
}

int main(int argc, char **argv)
{
    km_switch_gpio_config_t out, before;
    memset(&out, 0xa5, sizeof(out));
    memcpy(&before, &out, sizeof(out));
    km_result_t result = km_config_switches(argc > 1 && !strcmp(argv[1], "null") ? NULL : &out);
    if (result != KM_OK) assert(!memcmp(&out, &before, sizeof(out)));
    printf("{\"result\":%d,\"inputs\":[", (int)result);
    if (result == KM_OK) {
        for (unsigned i = 0; i < KM_MAX_LANES; ++i) {
            if (i) printf(",");
            print_input(&out.bay[i]);
        }
        printf(","); print_input(&out.hub);
        printf(","); print_input(&out.toolhead);
        printf(","); print_input(&out.buffer_low);
        printf(","); print_input(&out.buffer_high);
    }
    printf("],\"load\":[");
    if (result == KM_OK) {
        for (unsigned i = 0; i < KM_MAX_LANES; ++i) {
            if (i) printf(",");
            print_input(&out.load[i]);
        }
    }
    printf("],\"selector_gate\":[");
    if (result == KM_OK) {
        for (unsigned i = 0; i < KM_MAX_LANES; ++i) {
            if (i) printf(",");
            print_input(&out.selector_gate[i]);
        }
    }
    puts("]}");
    return 0;
}
