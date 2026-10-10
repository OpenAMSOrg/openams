// SPDX-License-Identifier: GPL-3.0-or-later
#include "adc_config.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

int main(int argc, char **argv)
{
    km_adc_pair_config_t out, before;
    memset(&out, 0xa5, sizeof(out)); memcpy(&before, &out, sizeof(out));
    km_result_t result = km_config_adc_pair(argc > 1 && !strcmp(argv[1], "null") ? NULL : &out);
    if (result != KM_OK) assert(!memcmp(&out, &before, sizeof(out)));
    else assert(km_adc_pair_config_validate(&out) == KM_OK);
    printf("{\"result\":%d,\"limits\":[", (int)result);
    if (result == KM_OK) printf("%u,%u,%u,%u", out.max_raw, out.conversion_timeout_us,
                               out.max_pair_span_us, out.max_sample_age_us);
    puts("]}");
    return 0;
}
