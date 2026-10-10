// SPDX-License-Identifier: GPL-3.0-or-later
#include "rewind_config.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

static unsigned disables, pwm_writes, enables;
static void off(void *ctx) { (void)ctx; ++disables; }
static km_result_t enable(void *ctx, bool on) {
    (void)ctx; assert(!on); ++enables; return KM_OK;
}
static km_result_t pwm(void *ctx, km_direction_t direction, uint16_t duty) {
    (void)ctx; (void)direction; assert(!duty); ++pwm_writes; return KM_OK;
}
static uint32_t now(void *ctx) { (void)ctx; return 100u; }
static bool ready(void *ctx) { (void)ctx; return true; }
static const km_pwm_bridge_io_t io = {
    .force_disable=off, .write_enable=enable, .write_pwm=pwm, .now_us=now, .ready=ready
};

static void check_startup(const km_pwm_bridge_config_t *config, km_result_t expected)
{
    km_pwm_bridge_driver_t driver;
    disables = pwm_writes = enables = 0;
    assert(km_pwm_bridge_config_validate(config) == expected);
    assert(!disables && !pwm_writes && !enables);
    assert(km_pwm_bridge_init(&driver, config, &io, NULL) == expected);
    assert(disables && pwm_writes && !enables);
    assert(!driver.base.enabled && !driver.base.pwm_duty_permille);
    assert(driver.base.initialized == (expected == KM_OK));
}

int main(int argc, char **argv)
{
    km_pwm_bridge_config_t out, before;
    memset(&out, 0xa5, sizeof(out)); memcpy(&before, &out, sizeof(out));
    km_result_t result = km_config_rewind(argc > 1 && !strcmp(argv[1], "null") ? NULL : &out);
    assert(!disables && !pwm_writes && !enables);
    if (result != KM_OK) assert(!memcmp(&out, &before, sizeof(out)));
    else check_startup(&out, KM_OK);
    printf("{\"result\":%d,\"limits\":[", (int)result);
    if (result == KM_OK) printf("%u,%u,%u", out.maximum_duty_permille,
                               out.reversal_deadtime_us, out.maximum_call_lateness_us);
    puts("]}");

    /* Exercise raw values that Kconfig would normalize. Startup and pure
     * validation must agree; invalid startup still removes physical enable. */
    check_startup(NULL, KM_ERR_INVALID_PARAM);
    const uint16_t duty[] = {0, 1, 1000, 1001, UINT16_MAX};
    const uint32_t deadtime[] = {0, 1, 2147483647u, 2147483648u, UINT32_MAX};
    const uint16_t lateness[] = {0, 1, 1000, 1001, UINT16_MAX};
    for (unsigned i = 0; i < 5; ++i) {
        km_result_t expected = i == 1 || i == 2 ? KM_OK : KM_ERR_INVALID_PARAM;
        km_pwm_bridge_config_t c = {duty[i], 100, 50};
        check_startup(&c, expected);
        c = (km_pwm_bridge_config_t){800, deadtime[i], 50};
        check_startup(&c, expected);
        c = (km_pwm_bridge_config_t){800, 100, lateness[i]};
        check_startup(&c, expected);
    }
    return 0;
}
