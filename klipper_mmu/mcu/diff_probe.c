// SPDX-License-Identifier: GPL-3.0-or-later
// Receive-only raw DiffA/DiffB acquisition probe. No calibration or motion.
#include "autoconf.h"

#if !defined(CONFIG_MACH_STM32H723) || !CONFIG_MACH_STM32H723 \
    || !defined(CONFIG_KLIPPER_MMU_DIFF_BUFFER) || !CONFIG_KLIPPER_MMU_DIFF_BUFFER \
    || !defined(CONFIG_WANT_ADC_LOCAL) || !CONFIG_WANT_ADC_LOCAL \
    || !defined(CONFIG_WANT_GPIO_LOCAL) || !CONFIG_WANT_GPIO_LOCAL \
    || (defined(CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN) && CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN)
#error "diff_probe.c requires non-actuating H723 local GPIO and DiffA/DiffB ADC support"
#endif

#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include "basecmd.h"
#include "command.h"
#include "sched.h"
#include "stm32/gpio_local.h"
#include "adc.h"
#include "adc_config.h"
#include "clock.h"
#include "pin_config.h"

DECL_CONSTANT("KLIPPER_MMU_DIFF_PROBE", 1);

#ifndef CONFIG_KLIPPER_MMU_DIAGNOSTIC_TIMEOUT_US
#define CONFIG_KLIPPER_MMU_DIAGNOSTIC_TIMEOUT_US 1000000u
#endif
#define KM_DIFF_PROBE_TIMEOUT_US CONFIG_KLIPPER_MMU_DIAGNOSTIC_TIMEOUT_US
#define KM_DIFF_PROBE_TIMEOUT_TICKS \
    ((uint64_t)KM_DIFF_PROBE_TIMEOUT_US * ((uint64_t)CONFIG_CLOCK_FREQ / 1000000u))
_Static_assert(KM_DIFF_PROBE_TIMEOUT_US >= 1000u
               && KM_DIFF_PROBE_TIMEOUT_US <= 1000000u,
               "Diff probe timeout outside 1000..1000000 us");
_Static_assert(CONFIG_CLOCK_FREQ >= 1000000u
               && (CONFIG_CLOCK_FREQ % 1000000u) == 0u,
               "Diff probe requires an integral-MHz clock");
_Static_assert(KM_DIFF_PROBE_TIMEOUT_TICKS < (1ull << 31),
               "Diff probe timeout exceeds raw-tick half horizon");

enum probe_state { PROBE_IDLE = 0, PROBE_WAITING = 1, PROBE_VALID = 2, PROBE_FAULTED = 3 };
static enum probe_state probe_state;
static km_result_t probe_result = KM_ERR_UNAVAILABLE;
static const km_pin_config_t *probe_pins;
static bool pins_reserved;
static bool start_requested;

static void
start_probe(void)
{
    km_resource_diagnostic_t diagnostic;
    km_adc_pair_config_t config;
    uint32_t local[2];
    km_result_t result;
    int gpio_result;

    if (sched_is_shutdown())
        return;
    if (probe_state != PROBE_IDLE)
        return;

    /* Saved configuration is validated before taking persistent GPIO claims. */
    memset(&diagnostic, 0, sizeof(diagnostic));
    result = km_config_pins_view(&probe_pins, &diagnostic);
    if (result != KM_OK) {
        probe_result = result;
        probe_state = PROBE_FAULTED;
        return;
    }
    if (probe_pins->pin[KM_PIN_BUFFER_A][0] == KM_PIN_UNUSED
        || probe_pins->pin[KM_PIN_BUFFER_B][0] == KM_PIN_UNUSED) {
        probe_result = KM_ERR_UNAVAILABLE;
        probe_state = PROBE_FAULTED;
        return;
    }
    local[0] = probe_pins->pin[KM_PIN_BUFFER_A][0];
    local[1] = probe_pins->pin[KM_PIN_BUFFER_B][0];
    result = km_config_adc_pair(&config);
    if (result != KM_OK) {
        probe_result = result;
        probe_state = PROBE_FAULTED;
        return;
    }
    enum km_clock_state clock = km_clock_get_state();
    if (clock == KM_CLOCK_STATE_UNAVAILABLE) {
        /* The start command is retryable until clock initialization completes. */
        probe_result = KM_ERR_UNAVAILABLE;
        return;
    }
    if (clock != KM_CLOCK_STATE_READY) {
        probe_result = KM_ERR_FAULTED;
        probe_state = PROBE_FAULTED;
        return;
    }
    if (!pins_reserved) {
        gpio_result = gpio_local_reserve(local, 2);
        if (gpio_result != GPIO_LOCAL_OK) {
            probe_result = gpio_result == GPIO_LOCAL_ERR_INVALID ? KM_ERR_INVALID_PARAM
                          : gpio_result == GPIO_LOCAL_ERR_BUSY ? KM_ERR_CONFLICT : KM_ERR_FAULTED;
            probe_state = PROBE_FAULTED;
            return;
        }
        pins_reserved = true;
    }
    result = km_adc_start(local[0], local[1], &config, KM_DIFF_PROBE_TIMEOUT_US);
    if (result != KM_OK) {
        probe_result = result;
        if (result != KM_ERR_UNAVAILABLE)
            probe_state = PROBE_FAULTED;
        return;
    }
    probe_result = KM_ERR_UNAVAILABLE;
    probe_state = PROBE_WAITING;
}

void
command_km_diff_probe_start(uint32_t *args)
{
    (void)args;
    start_requested = true;
    start_probe();
}
DECL_COMMAND(command_km_diff_probe_start, "km_diff_probe_start");

void
command_km_diff_probe_query(uint32_t *args)
{
    uint32_t challenge = args[0];
    uint16_t raw_a = 0, raw_b = 0;
    uint32_t a_attempt_us = 0, b_ready_us = 0, observed_us = 0;
    if (probe_state == PROBE_IDLE && start_requested && !sched_is_shutdown())
        start_probe();
    km_result_t result = probe_result;
    enum probe_state state = probe_state;

    if (state == PROBE_WAITING || state == PROBE_VALID) {
        km_adc_pair_sample_t sample;
        km_core_timepoint_t query_time;
        memset(&sample, 0, sizeof(sample));
        result = km_adc_get_sample_at(&sample, &query_time);
        if (result == KM_ERR_FAULTED || km_adc_get_state() == KM_ADC_STATE_FAULTED
            || km_adc_get_state() == KM_ADC_STATE_SHUTDOWN) {
            state = PROBE_FAULTED;
            probe_state = state;
            probe_result = result == KM_OK ? KM_ERR_FAULTED : result;
            result = probe_result;
        } else if (result == KM_OK) {
            observed_us = query_time.us;
            result = sample.result;
            if (sample.is_valid && sample.result == KM_OK) {
                raw_a = sample.raw_a;
                raw_b = sample.raw_b;
                a_attempt_us = sample.a_attempt_us;
                b_ready_us = sample.b_complete_us;
                state = PROBE_VALID;
                probe_result = KM_OK;
            } else {
                state = PROBE_WAITING;
                probe_result = sample.result;
            }
            probe_state = state;
        } else {
            state = PROBE_WAITING;
        }
    }
    sendf("km_diff_probe_state challenge=%u state=%c result=%i a_raw=%hu b_raw=%hu a_attempt_us=%u b_ready_us=%u observed_us=%u",
          challenge, (uint32_t)state, (int32_t)result, raw_a, raw_b,
          a_attempt_us, b_ready_us, observed_us);
}
DECL_COMMAND_FLAGS(command_km_diff_probe_query, HF_IN_SHUTDOWN,
                   "km_diff_probe_query challenge=%u");

/* Keep the selected pair reserved for the life of this MCU image. */
void
km_diff_probe_shutdown(void)
{
    if (probe_state != PROBE_IDLE || start_requested) {
        probe_state = PROBE_FAULTED;
        probe_result = KM_ERR_FAULTED;
    }
}
DECL_SHUTDOWN(km_diff_probe_shutdown);
