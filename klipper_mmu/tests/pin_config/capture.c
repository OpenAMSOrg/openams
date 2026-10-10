// SPDX-License-Identifier: GPL-3.0-or-later
#include "pin_config.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

int main(int argc, char **argv)
{
    km_pin_config_t out, before;
    memset(&out, 0xa5, sizeof(out));
    memcpy(&before, &out, sizeof(out));
    km_resource_diagnostic_t d;
    bool view_mode = argc > 1 && !strcmp(argv[1], "view");
    km_result_t result;
    if (view_mode) {
        const km_pin_config_t *view = NULL;
        const km_pin_config_t *again = NULL;
        result = km_config_pins_view(&view, &d);
        if (result == KM_OK) {
            assert(view != NULL);
            assert(km_config_pins_view(&again, NULL) == KM_OK);
            assert(again == view);
            result = km_config_pins(&out, NULL);
            assert(result == KM_OK && !memcmp(view, &out, sizeof(out)));
        } else {
            assert(view == NULL);
            assert(!memcmp(&out, &before, sizeof(out)));
        }
    } else {
        result = km_config_pins(argc > 1 && !strcmp(argv[1], "null") ? NULL : &out, &d);
    }
    if (result != KM_OK) assert(!memcmp(&out, &before, sizeof(out)));
    printf("{\"result\":%d,\"reason\":%u,\"first\":%u,\"second\":%u,\"claims\":[",
           (int)result, (unsigned)d.reason, d.first_index, d.second_index);
    if (result == KM_OK) {
        for (size_t i = 0; i < out.count; ++i)
            printf("%s[%u,%u]", i ? "," : "", out.claims[i].instance, out.claims[i].owner);
    }
    printf("],\"pins\":[");
    if (result == KM_OK) {
        for (unsigned role = 0; role < KM_PIN_ROLE_COUNT; ++role) {
            printf("%s[", role ? "," : "");
            for (unsigned lane = 0; lane < KM_MAX_LANES; ++lane)
                printf("%s%u", lane ? "," : "", out.pin[role][lane]);
            putchar(']');
        }
    }
    puts("]}");
    return 0;
}
