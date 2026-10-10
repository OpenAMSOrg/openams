// SPDX-License-Identifier: GPL-3.0-or-later
#include "rewind_config.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

int main(int argc, char **argv)
{
    km_rewind_output_config_t out[KM_MAX_LANES], before[KM_MAX_LANES];
    memset(out, 0xa5, sizeof(out));
    memcpy(before, out, sizeof(out));
    km_result_t result = km_config_rewind_outputs(
        argc > 1 && !strcmp(argv[1], "null") ? NULL : out);
    if (result != KM_OK) assert(!memcmp(out, before, sizeof(out)));
    printf("{\"result\":%d,\"lanes\":[", (int)result);
    if (result == KM_OK) {
        for (unsigned lane = 0; lane < KM_MAX_LANES; ++lane)
            printf("%s[%u,%u,%u,%u]", lane ? "," : "", out[lane].cycle_us,
                   (unsigned)out[lane].enable_active_high,
                   (unsigned)out[lane].forward_active_high,
                   (unsigned)out[lane].reverse_active_high);
    }
    puts("]}");
    return 0;
}
