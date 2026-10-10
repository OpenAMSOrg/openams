// SPDX-License-Identifier: GPL-3.0-or-later
#include "h723_resources.h"
#include "autoconf.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

#if defined(CONFIG_MACH_STM32H723) && CONFIG_MACH_STM32H723 \
    && defined(CONFIG_WANT_ADC) && CONFIG_WANT_ADC \
    && defined(CONFIG_WANT_ADC_LOCAL) && CONFIG_WANT_ADC_LOCAL
#include "stm32/adc_local.h"
#define HAVE_ADC_ROUTE 1
void adc_watch_arm(void);
int adc_watch_unchanged(void);
#else
#define HAVE_ADC_ROUTE 0
#endif

int main(int argc, char **argv)
{
    if (argc > 1 && !strcmp(argv[1], "routes")) {
        printf("{\"routes\":[");
        bool comma = false;
        for (unsigned pin = 0; pin < 256; ++pin) {
            km_h723_pwm_route_t r = {99,99};
            km_result_t result = km_h723_pwm_route((uint16_t)pin, &r);
            if (result != KM_OK) { assert(r.timer==99 && r.channel==99); continue; }
            assert(pin < 128 && r.timer && r.timer <= 17 && r.channel && r.channel <= 4);
            printf("%s[%u,%u,%u]", comma ? "," : "", pin,r.timer,r.channel);
            comma = true;
        }
        assert(km_h723_pwm_route(0, NULL) == KM_ERR_INVALID_PARAM);
        km_h723_pwm_route_t r = {0};
        assert(km_h723_pwm_route(UINT16_MAX, &r) == KM_ERR_NOT_SUPPORTED);
        puts("]}"); return 0;
    }
#if HAVE_ADC_ROUTE
    if (argc > 1 && !strcmp(argv[1], "adc-routes")) {
        printf("{\"routes\":[");
        bool comma = false;
        adc_watch_arm();
        for (unsigned pin = 0; pin < 128; ++pin) {
            struct gpio_adc_route route = {0};
            if (gpio_adc_get_route(pin, &route) != 1) continue;
            assert(route.unit >= 1 && route.unit <= 3);
            printf("%s[%u,%u,%u]", comma ? "," : "", pin, route.unit, route.channel);
            comma = true;
        }
        struct gpio_adc_route missed = {0};
        assert(gpio_adc_get_route(0xffffffffu, &missed) != 1);
        int unchanged = adc_watch_unchanged();
        printf("],\"registers\":%d}\n", unchanged);
        assert(unchanged == 1);
        return 0;
    }
#endif
    km_h723_resource_plan_t out;
    memset(&out, 0xa5, sizeof(out));
    km_resource_diagnostic_t d;
    bool is_null = argc > 1 && !strcmp(argv[1], "null");
#if HAVE_ADC_ROUTE
    adc_watch_arm();
#endif
    km_result_t result = km_config_h723_resources(is_null ? NULL : &out, &d);
#if HAVE_ADC_ROUTE
    assert(adc_watch_unchanged() == 1);
#endif
    if (result != KM_OK && !is_null) assert(out.count == 0);
    km_result_t external = KM_OK;
    if (result == KM_OK && argc > 1) {
        size_t n = out.count;
        assert(n + 2 <= KM_RESOURCE_MAX_CLAIMS);
        if (!strcmp(argv[1], "timer")) {
            out.claims[n++] = (km_resource_claim_t){.kind=KM_RESOURCE_TIMER_PWM_BASE,.instance=2,
                .owner=99,.period_ticks=1000,.tick_hz=1000000};
            out.claims[n++] = (km_resource_claim_t){.kind=KM_RESOURCE_TIMER_CHANNEL,.instance=2,
                .subresource=4,.owner=99};
        } else if (!strcmp(argv[1], "adc")) {
            uint16_t unit = 1;
            for (size_t i = 0; i < n; ++i)
                if (out.claims[i].kind == KM_RESOURCE_ADC_UNIT)
                    unit = out.claims[i].instance;
            out.claims[n++] = (km_resource_claim_t){.kind=KM_RESOURCE_ADC_UNIT,
                .instance=unit,.owner=99};
        } else if (!strcmp(argv[1], "free")) {
            out.claims[n++] = (km_resource_claim_t){.kind=KM_RESOURCE_TIMER_EXCLUSIVE,.instance=17,.owner=99};
        }
        external = km_resource_plan_validate(out.claims, n, NULL);
    }
    printf("{\"result\":%d,\"external\":%d,\"reason\":%u,\"first\":%u,\"second\":%u,\"claims\":[",
           (int)result,(int)external,(unsigned)d.reason,d.first_index,d.second_index);
    if (result == KM_OK)
        for (size_t i=0; i<out.count; ++i) {
            km_resource_claim_t *c=&out.claims[i];
            printf("%s[%u,%u,%u,%u]", i?",":"", (unsigned)c->kind,c->instance,c->subresource,c->owner);
        }
    printf("],\"rewind\":[");
    if (result == KM_OK)
        for (unsigned lane=0; lane<KM_MAX_LANES; ++lane)
            for (unsigned direction=0; direction<2; ++direction) {
                km_h723_pwm_route_t *r=&out.rewind[lane][direction];
                printf("%s[%u,%u]", lane || direction ? ",":"",r->timer,r->channel);
            }
    puts("]}"); return 0;
}
