// SPDX-License-Identifier: GPL-3.0-or-later
/* Shared H723 native startup fixture. BoxTurtle paths exercise the legacy and
 * analog board startup; the EMU path below reuses its timer, pin, clock, and
 * mock-driver services with the saved five-lane image configuration. */
#include "board_startup.h"
#if !defined(KM_TEST_HOSTED_RUNTIME)
#include "boxturtle_boot.c"
#endif
#include "clock.h"
#include "config.h"
#include "pin_config.h"
#include "h723_resources.h"
#include "feeder_config.h"
#include "feeder_mechanics.h"
#include "tmc_config.h"
#include "switch_config.h"
#include "rewind_config.h"
#include "adc_config.h"
#include "switch_gpio.h"
#include "adc.h"
#include "tmc_feeder.h"
#include "pwm_bridge_gpio.h"
#include "stepdir_gpio.h"
#include "tmc_uart_gpio.h"
#include "sensors.h"
#include "controller.h"
#include "commands.h"
#include "diagnostic_startup.h"
#include "calibration.h"
#include "boot_identity.h"
#include "basecmd.h"
#include "board/irq.h"
#include "sched.h"
#include "stm32/gpio_local.h"
#include "stm32/adc_local.h"
#include "stm32/internal.h"
#include "stm32/pwm.h"
#include <stdio.h>
#include <stdarg.h>
#include "mock_driver.h"
#include "fake_clock.h"

#include <assert.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/wait.h>

/* Saved image headers can predate the optional FPS menu fields. */
#ifndef CONFIG_KLIPPER_MMU_FPS_ARRIVAL
#define CONFIG_KLIPPER_MMU_FPS_ARRIVAL 0
#define CONFIG_KLIPPER_MMU_FPS_ARRIVAL_PERMILLE 0
#define CONFIG_KLIPPER_MMU_FPS_ARRIVAL_SAMPLES 0
#define CONFIG_KLIPPER_MMU_FPS_ARRIVAL_HOLD_MS 0
#define CONFIG_KLIPPER_MMU_FPS_ARRIVAL_MIN_MM 0
#define CONFIG_KLIPPER_MMU_FPS_ARRIVAL_MAX_MM 0
#endif
#ifndef CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR
#define CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR 1
#endif

_Static_assert(CONFIG_WANT_GPIO_LOCAL, "boot test requires local GPIO admission");
_Static_assert(CONFIG_WANT_TMCUART_LOCAL && CONFIG_KLIPPER_MMU_TMC_UART_SHARED,
               "boot test requires the shared TMC UART gate");
#if !defined(KM_TEST_HOSTED_RUNTIME)
_Static_assert(CONFIG_WANT_HARD_PWM_LOCAL_PAIR, "boot test requires the PWM pair gate");
_Static_assert(CONFIG_WANT_HARD_PWM_QUERY, "boot test requires the PWM route query");
#elif defined(KM_TEST_EMU_RUNTIME)
_Static_assert(CONFIG_KLIPPER_MMU_DEVICE_EMU && CONFIG_KLIPPER_MMU_LANE_COUNT == 5,
               "EMU runtime test requires the saved five-lane image");
_Static_assert(CONFIG_KLIPPER_MMU_DIAGNOSTIC_STARTUP,
               "EMU runtime test requires production diagnostic startup");
#elif defined(KM_TEST_NIGHTOWL_RUNTIME)
_Static_assert(CONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL && CONFIG_KLIPPER_MMU_LANE_COUNT == 4,
               "NightOwl runtime test requires the saved four-lane image");
_Static_assert(CONFIG_KLIPPER_MMU_DIAGNOSTIC_STARTUP,
               "NightOwl runtime test requires production diagnostic startup");
#if CONFIG_MACH_STM32F207 || CONFIG_MACH_STM32F405 || CONFIG_MACH_STM32F407 || CONFIG_MACH_STM32F429 || CONFIG_MACH_STM32F765 || CONFIG_MACH_STM32G431 || CONFIG_MACH_STM32G474 || CONFIG_MACH_STM32H743 || CONFIG_MACH_STM32H750 || CONFIG_MACH_STM32G0B0 || CONFIG_MACH_STM32G0B1
#if CONFIG_MACH_STM32F207
_Static_assert(CONFIG_KLIPPER_MMU_F207_BOOT_GATES,
               "NightOwl STM32 runtime requires admitted local startup drivers");
#elif CONFIG_MACH_STM32F765
_Static_assert(CONFIG_KLIPPER_MMU_F765_BOOT_GATES,
               "NightOwl STM32 runtime requires admitted local startup drivers");
#elif CONFIG_MACH_STM32G431 || CONFIG_MACH_STM32G474
_Static_assert(CONFIG_KLIPPER_MMU_G4_BOOT_GATES,
               "NightOwl G4 runtime requires admitted local startup drivers");
_Static_assert(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_G4,
               "NightOwl G4 runtime requires its target identity gate");
#elif CONFIG_MACH_STM32H743 || CONFIG_MACH_STM32H750
_Static_assert(CONFIG_KLIPPER_MMU_H743_H750_BOOT_GATES,
               "NightOwl H7 runtime requires admitted local startup drivers");
_Static_assert(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H743_H750,
               "NightOwl H7 runtime requires its target identity gate");
#elif CONFIG_MACH_STM32G0B0 || CONFIG_MACH_STM32G0B1
_Static_assert(CONFIG_KLIPPER_MMU_G0B_BOOT_GATES,
               "NightOwl G0B runtime requires admitted local startup drivers");
_Static_assert(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_HOST,
               "NightOwl G0B runtime requires its host-epoch identity gate");
#else
_Static_assert(CONFIG_KLIPPER_MMU_F4X5_BOOT_GATES,
               "NightOwl STM32 runtime requires admitted local startup drivers");
#endif
#else
_Static_assert(CONFIG_KLIPPER_MMU_NIGHTOWL_BOOT_GATES,
               "NightOwl H723 runtime requires admitted local startup drivers");
#endif
#elif defined(KM_TEST_PASSIVE_RUNTIME)
/* KMS, Angry Beaver, 3MS and QuattroBox 1.0/1.1/2: same four-lane, no-PWM,
 * no-ADC shape, so one runtime scenario (below) covers all of them. Angry
 * Beaver is also this catalogue's one G0B0/G0B1 row (Happy Hare's real MMB
 * 2.0 board, mcu/Kconfig KLIPPER_MMU_G0B_BOOT_GATES); every other passive-
 * four device is H723-only. */
_Static_assert((CONFIG_KLIPPER_MMU_DEVICE_KMS || CONFIG_KLIPPER_MMU_DEVICE_ANGRY_BEAVER
                || CONFIG_KLIPPER_MMU_DEVICE_THREE_MS || CONFIG_KLIPPER_MMU_DEVICE_QUATTROBOX_1_0
                || CONFIG_KLIPPER_MMU_DEVICE_QUATTROBOX_1_1 || CONFIG_KLIPPER_MMU_DEVICE_QUATTROBOX_2)
               && CONFIG_KLIPPER_MMU_LANE_COUNT == 4,
               "passive-feeder runtime test requires a saved four-lane image");
_Static_assert(CONFIG_KLIPPER_MMU_DIAGNOSTIC_STARTUP,
               "passive-feeder runtime test requires production diagnostic startup");
#if CONFIG_MACH_STM32G0B0 || CONFIG_MACH_STM32G0B1
_Static_assert(CONFIG_KLIPPER_MMU_DEVICE_ANGRY_BEAVER,
               "passive-feeder G0B runtime is Angry Beaver only");
_Static_assert(CONFIG_KLIPPER_MMU_G0B_BOOT_GATES,
               "passive-feeder G0B runtime requires admitted local startup drivers");
_Static_assert(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_HOST,
               "passive-feeder G0B runtime requires its host-epoch identity gate");
#else
_Static_assert(CONFIG_KLIPPER_MMU_PASSIVE_FEEDER_BOOT_GATES,
               "passive-feeder H723 runtime requires admitted local startup drivers");
#endif
#elif defined(KM_TEST_F407_RUNTIME)
_Static_assert((CONFIG_MACH_STM32F207 || CONFIG_MACH_STM32F405 || CONFIG_MACH_STM32F407 || CONFIG_MACH_STM32F429 || CONFIG_MACH_STM32F765 || CONFIG_MACH_STM32G431 || CONFIG_MACH_STM32G474 || CONFIG_MACH_STM32H743 || CONFIG_MACH_STM32H750 || CONFIG_MACH_STM32G0B0 || CONFIG_MACH_STM32G0B1)
               && CONFIG_KLIPPER_MMU_DEVICE_CUSTOM
               && CONFIG_KLIPPER_MMU_LANE_COUNT == 1,
               "STM32 runtime test requires the saved one-lane Custom image");
_Static_assert(CONFIG_KLIPPER_MMU_DIAGNOSTIC_STARTUP,
               "STM32 runtime test requires production diagnostic startup gates");
#if CONFIG_MACH_STM32F207
_Static_assert(CONFIG_KLIPPER_MMU_F207_BOOT_GATES,
               "STM32 runtime test requires F207 startup gates");
#elif CONFIG_MACH_STM32F765
_Static_assert(CONFIG_KLIPPER_MMU_F765_BOOT_GATES,
               "STM32 runtime test requires F765 startup gates");
#elif CONFIG_MACH_STM32G431 || CONFIG_MACH_STM32G474
_Static_assert(CONFIG_KLIPPER_MMU_G4_BOOT_GATES,
               "G4 runtime test requires G4 startup gates");
_Static_assert(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_G4,
               "G4 runtime test requires its target identity gate");
#elif CONFIG_MACH_STM32H743 || CONFIG_MACH_STM32H750
_Static_assert(CONFIG_KLIPPER_MMU_H743_H750_BOOT_GATES,
               "H7 runtime test requires H7 startup gates");
_Static_assert(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H743_H750,
               "H7 runtime test requires its target identity gate");
#elif CONFIG_MACH_STM32G0B0 || CONFIG_MACH_STM32G0B1
_Static_assert(CONFIG_KLIPPER_MMU_G0B_BOOT_GATES,
               "G0B runtime test requires G0B startup gates");
_Static_assert(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_HOST,
               "G0B runtime test requires host-epoch identity gate");
#else
_Static_assert(CONFIG_KLIPPER_MMU_F4X5_BOOT_GATES,
               "STM32 runtime test requires F4x5 startup gates");
#endif
#endif
_Static_assert(CONFIG_KLIPPER_MMU_TMC_CURRENT_CEILING == 800, "image TMC ceiling");
_Static_assert(CONFIG_KLIPPER_MMU_DIFF_BUFFER == 0 || CONFIG_KLIPPER_MMU_DIFF_BUFFER == 1,
               "fixture must have a concrete buffer mode");
#if CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR
_Static_assert(CONFIG_KLIPPER_MMU_TOOLHEAD_ACTIVE_HIGH == 0, "image arrival level");
_Static_assert(CONFIG_KLIPPER_MMU_TOOLHEAD_PULL == 1, "image arrival pull");
#endif
#if !CONFIG_KLIPPER_MMU_DIFF_BUFFER
_Static_assert(CONFIG_KLIPPER_MMU_BUFFER_LOW_A == 1, "image buffer A");
#endif
#if !defined(KM_TEST_HOSTED_RUNTIME)
_Static_assert(CONFIG_KLIPPER_MMU_REWIND_CYCLE_US == 50, "image rewind period");
_Static_assert(CONFIG_KLIPPER_MMU_REWIND_BRIDGE_CEILING == 600, "image rewind ceiling");
_Static_assert(CONFIG_KLIPPER_MMU_REWIND_DEADTIME_US == 50, "image rewind deadtime");
_Static_assert(CONFIG_KLIPPER_MMU_REWIND_LATENESS_US == 50, "image rewind lateness");
#endif

TIM_TypeDef km_test_tim1, km_test_tim2, km_test_tim3, km_test_tim4;
TIM_TypeDef km_test_tim5, km_test_tim8, km_test_tim12, km_test_tim13;
TIM_TypeDef km_test_tim14, km_test_tim15, km_test_tim16, km_test_tim17;

/* First-match H7 routes for the AFC Lite rewind pairs: TIM2 PA0/PA1,
 * TIM3 PA6/PA7, TIM12 PB14/PB15, TIM4 PD12/PD13. */
int gpio_pwm_get_route(uint32_t pin, struct gpio_pwm_route *out)
{
    struct { uint32_t pin; TIM_TypeDef *timer; uint8_t channel; } table[] = {
        {0u, TIM2, 1}, {1u, TIM2, 2},
        {6u, TIM3, 1}, {7u, TIM3, 2},
        {30u, TIM12, 1}, {31u, TIM12, 2},
        {60u, TIM4, 1}, {61u, TIM4, 2},
    };
    if (!out) return 0;
    for (size_t i = 0; i < sizeof(table) / sizeof(table[0]); ++i) {
        if (table[i].pin != pin) continue;
        out->timer_id = (uintptr_t)table[i].timer;
        out->channel = table[i].channel;
        return 1;
    }
    return 0;
}

int gpio_pwm_pair_validate_pins(uint32_t forward_pin, uint32_t reverse_pin)
{
    struct gpio_pwm_route forward, reverse;
    if (forward_pin == reverse_pin) return GPIO_PWM_PAIR_ERR_INVALID;
    if (!gpio_pwm_get_route(forward_pin, &forward) || !gpio_pwm_get_route(reverse_pin, &reverse))
        return GPIO_PWM_PAIR_ERR_NOT_SUPPORTED;
    if (forward.timer_id != reverse.timer_id || forward.channel == reverse.channel)
        return GPIO_PWM_PAIR_ERR_INVALID;
    return GPIO_PWM_PAIR_OK;
}

static km_device_profile_t profile;
static km_tmc_uart_config_t tmc[KM_MAX_LANES];
static bool lane_ready[KM_MAX_LANES];
static km_result_t lane_error[KM_MAX_LANES];
static uint32_t seen_step[KM_MAX_LANES];
static bool irq_blocked;
static enum km_clock_state clock_state = KM_CLOCK_STATE_UNAVAILABLE;
static uint32_t now_us = 1000;
static struct timer *armed_timer;
static bool timer_queue_mode;
static struct timer *timer_queue[4];
static size_t timer_queue_count;
static mock_driver_t feeders[KM_MAX_LANES];
static mock_driver_t rewinds[KM_MAX_LANES];
static uint32_t reserved_pins[KM_RESOURCE_MAX_CLAIMS];
static uint32_t reserved_count;
static uint8_t input_levels[128];
#if CONFIG_KLIPPER_MMU_DIFF_BUFFER
static uint16_t upstream_adc_raw_a = 2000u, upstream_adc_raw_b = 2000u;
static uint32_t adc_setup_a, adc_setup_b, adc_reads_a, adc_reads_b;
#endif
static unsigned reserve_calls;
#if defined(KM_TEST_HOSTED_RUNTIME) && !CONFIG_KLIPPER_MMU_DIFF_BUFFER
static uint64_t test_boot_identity = UINT64_C(0x123456789abcdef0);
km_result_t km_boot_identity_get(uint64_t *out)
{
    if (!out) return KM_ERR_INVALID_PARAM;
    *out = test_boot_identity;
    return KM_OK;
}
/* mcu/config.c's per-lane config commands (feeder rotation, and selector
 * gate/FPS steps on a build with that hardware) call oid_alloc purely for
 * its "refuse a stale duplicate" side effect -- every real caller here
 * passes size 0 -- so a scratch buffer sized for the largest request any of
 * them actually makes is enough; nothing here reads it back through the
 * returned pointer. The CONFIG_KLIPPER_MMU_DIFF_BUFFER block below defines
 * its own (that build's diff-calibration blob is the one real user of the
 * returned storage), so this one only exists to cover its absence. */
void *oid_alloc(uint8_t oid, oid_type type, size_t size)
{
    static unsigned char scratch[64];
    (void)oid;
    (void)type;
    return size <= sizeof(scratch) ? scratch : NULL;
}
void command_km_diagnostic_start(uint32_t *args);
void command_km_diagnostic_start_host(uint32_t *args);
void command_km_diagnostic_query(uint32_t *args);
void km_diagnostic_task(void);
enum km_diagnostic_state km_diagnostic_get_state(void);
km_result_t km_diagnostic_get_result(void);
void command_km_start(uint32_t *args);
void command_km_heartbeat(uint32_t *args);
void command_km_event_peek(uint32_t *args);
void command_km_event_ack(uint32_t *args);
void command_km_terminal_query(uint32_t *args);
#ifndef KM_TEST_HOST_JOIN
static void test_diagnostic_start(void)
{
#if CONFIG_KLIPPER_MMU_BOOT_IDENTITY_HOST
    uint32_t epoch[] = {(uint32_t)(test_boot_identity >> 32),
                        (uint32_t)test_boot_identity};
    command_km_diagnostic_start_host(epoch);
#else
    uint32_t challenge[] = {0x4567u};
    command_km_diagnostic_start(challenge);
#endif
}
#endif
#endif
#if CONFIG_KLIPPER_MMU_DIFF_BUFFER
static union { max_align_t align; unsigned char bytes[sizeof(km_diff_adc_config_t)]; } calibration_oid;
static const uint8_t *host_calibration_data;
static size_t host_calibration_size;
static uint64_t test_boot_identity = UINT64_C(0x123456789abcdef0);

void *oid_alloc(uint8_t oid, oid_type type, size_t size)
{
    (void)oid;
    (void)type;
    if (size > sizeof(calibration_oid.bytes)) return NULL;
    memset(&calibration_oid, 0, sizeof(calibration_oid));
    return calibration_oid.bytes;
}

void *command_decode_ptr(uint32_t value)
{
    return value == 1u ? (void *)host_calibration_data : NULL;
}

km_result_t km_boot_identity_get(uint64_t *out)
{
    if (!out) return KM_ERR_INVALID_PARAM;
    *out = test_boot_identity;
    return KM_OK;
}

void command_config_km_diff_calibration(uint32_t *args);
#endif
#if CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN && (CONFIG_KLIPPER_MMU_DIFF_BUFFER || defined(KM_TEST_HOST_JOIN))
static uint8_t host_lane;
/* Step pulses the tick path has completed for the selected lane's feeder,
 * accumulated from the shared trajectory's completed-step position. That is
 * what the mock driver's per-edge counter used to count: a direction-agnostic
 * pulse total, so a reverse UNLOAD adds to it exactly as set_step_edge did. */
static uint32_t host_pulses;
static int64_t host_pulses_from;
#endif
#if CONFIG_KLIPPER_MMU_DIFF_BUFFER || defined(KM_TEST_HOSTED_RUNTIME)
static char last_send_format[256];
static uint32_t last_send_values[24];
static size_t last_send_count;
void command_km_start(uint32_t *args);
void command_km_heartbeat(uint32_t *args);
void command_km_stop(uint32_t *args);
void command_km_event_peek(uint32_t *args);
void command_km_event_ack(uint32_t *args);
void command_km_snapshot_part(uint32_t *args);
void command_km_sensor_part(uint32_t *args);
void command_km_path_reconcile(uint32_t *args);
void command_km_load_attempts_query(uint32_t *args);
void command_km_diag_part(uint32_t *args);

void sendf(const char *format, ...)
{
    (void)snprintf(last_send_format, sizeof(last_send_format), "%s", format);
    last_send_count = 0u;
    if (strncmp(format, "km_", 3u) == 0) {
        va_list args;
        va_start(args, format);
        size_t count = 0u;
        for (const char *p = format; *p; ++p)
            if (*p == '%') {
                assert(count < sizeof(last_send_values) / sizeof(last_send_values[0]));
                last_send_values[count++] = va_arg(args, uint32_t);
            }
        last_send_count = count;
        va_end(args);
    }
}

#if CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN && !defined(KM_TEST_HOST_JOIN)
static void issue_command_start(uint64_t instance, uint32_t session,
                                uint32_t request, uint8_t operation,
                                uint8_t lane, uint8_t direction)
{
    uint32_t args[] = {(uint32_t)(instance >> 32), (uint32_t)instance,
                       KM_CLIENT_HOST, session, request, operation, lane,
                       direction};
    last_send_format[0] = '\0';
    command_km_start(args);
    assert(strcmp(last_send_format, "km_admission instance_hi=%u instance_lo=%u client=%c session=%u request=%u operation=%c lane=%c admission=%c fault=%c") == 0);
}
#endif

#if (!defined(KM_TEST_HOSTED_RUNTIME) || CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN) && !defined(KM_TEST_HOST_JOIN)
static void issue_command_heartbeat(uint64_t instance, uint32_t session)
{
    uint32_t args[] = {(uint32_t)(instance >> 32), (uint32_t)instance,
                       session};
    last_send_format[0] = '\0';
    command_km_heartbeat(args);
    assert(strcmp(last_send_format, "km_command_result instance_hi=%u instance_lo=%u command=%c query=%u result=%c") == 0);
}
#endif

#endif

static uint16_t pin_id(char port, unsigned bit)
{
    return (uint16_t)(((unsigned)(port - 'A')) * 16u + bit);
}

static void prepare_drivers(void)
{
    for (size_t lane = 0; lane < KM_MAX_LANES; ++lane) {
        tmc[lane].tmc.run_current_ma = profile.default_run_current_ma;
        tmc[lane].tmc.hold_current_ma = profile.default_hold_current_ma;
        lane_ready[lane] = false;
        seen_step[lane] = UINT32_MAX;
        mock_driver_init(&feeders[lane], 2, 10, false);
        mock_driver_init(&rewinds[lane], 2, 10, false);
        /* Fast enough for the profile's own feed and retract; the mock's
         * 20000 steps/s default already covers BoxTurtle's 26 mm/s. */
        uint32_t needed = profile.default_feed_velocity_steps_s
            > profile.default_rewind_velocity_steps_s
            ? profile.default_feed_velocity_steps_s : profile.default_rewind_velocity_steps_s;
        if (needed > feeders[lane].base.caps.max_velocity_steps_s)
            feeders[lane].base.caps.max_velocity_steps_s = needed;
    }
}

irqstatus_t irq_save(void) { return irq_blocked ? 1ul : 0ul; }
void irq_restore(irqstatus_t status) { (void)status; }
uint32_t timer_read_time(void) { return now_us; }
uint32_t timer_from_us(uint32_t us) { return us; }
int timer_is_before(uint32_t a, uint32_t b) { return (int32_t)(a - b) < 0; }
void sched_wake_task(struct task_wake *wake) { wake->wake = 1; }
uint8_t sched_check_wake(struct task_wake *wake)
{
    if (!wake->wake) return 0;
    wake->wake = 0;
    return 1;
}
void sched_add_timer(struct timer *timer)
{
    armed_timer = timer;
    if (!timer_queue_mode) return;
    for (size_t i = 0u; i < timer_queue_count; ++i) {
        assert(timer_queue[i] != timer); /* A live timer may not be registered twice. */
    }
    assert(timer_queue_count < sizeof(timer_queue) / sizeof(timer_queue[0]));
    timer_queue[timer_queue_count++] = timer;
}
void sched_del_timer(struct timer *timer)
{
    if (armed_timer == timer) armed_timer = NULL;
    if (!timer_queue_mode) return;
    for (size_t i = 0u; i < timer_queue_count; ++i) {
        if (timer_queue[i] != timer) continue;
        for (size_t j = i + 1u; j < timer_queue_count; ++j)
            timer_queue[j - 1u] = timer_queue[j];
        --timer_queue_count;
        return;
    }
    assert(0 && "deleting an unregistered timer");
}
uint8_t sched_is_shutdown(void) { return 0; }
void register_shutdown(shutdown_func_t function) { (void)function; }
void shutdown(const char *msg) { (void)msg; assert(0); }

enum km_clock_state km_clock_get_state(void) { return clock_state; }
km_result_t km_clock_get_time(km_core_timepoint_t *out)
{
    if (clock_state != KM_CLOCK_STATE_READY) return KM_ERR_UNAVAILABLE;
    out->us = now_us;
    out->ms = now_us / 1000u;
    return KM_OK;
}
km_result_t km_clock_get_completion_us(uint32_t *out)
{
    if (!out || clock_state != KM_CLOCK_STATE_READY) return KM_ERR_UNAVAILABLE;
    *out = now_us;
    return KM_OK;
}
km_result_t km_clock_deadline_ticks(uint32_t deadline, uint32_t *ticks, bool *due)
{
    if (!ticks || !due || clock_state != KM_CLOCK_STATE_READY) return KM_ERR_UNAVAILABLE;
    *due = !km_time_before(now_us, deadline);
    *ticks = *due ? now_us : deadline;
    return KM_OK;
}

#if !CONFIG_KLIPPER_MMU_DIFF_BUFFER && !defined(KM_TEST_HOSTED_RUNTIME)
km_result_t km_config_profile(km_device_profile_t *out, const km_diff_adc_config_t *cal)
{
    if (cal) return KM_ERR_INVALID_PARAM;
    *out = profile;
    return KM_OK;
}
#endif
#if !CONFIG_KLIPPER_MMU_DIFF_BUFFER && !defined(KM_TEST_HOSTED_RUNTIME)
km_result_t km_config_adc_pair(km_adc_pair_config_t *out)
{
    memset(out, 0, sizeof(*out));
    return KM_OK;
}
enum km_adc_state km_adc_get_state(void) { return KM_ADC_STATE_STOPPED; }
km_result_t km_adc_start(uint32_t pin_a, uint32_t pin_b, const km_adc_pair_config_t *config, uint32_t timeout_us)
{
    (void)pin_a; (void)pin_b; (void)config; (void)timeout_us;
    return KM_ERR_NOT_SUPPORTED;
}
void km_adc_shutdown(void) {}
#endif
#if defined(KM_TEST_HOSTED_RUNTIME)
km_result_t km_adc_get_sample_at(km_adc_pair_sample_t *sample,
                                 km_core_timepoint_t *query_time)
{
    (void)sample;
    (void)query_time;
    return KM_ERR_UNAVAILABLE; /* EMU's exact image uses switched-buffer sensing. */
}
#endif

int gpio_local_reserve(const uint32_t *list, uint32_t count)
{
    assert(list && count && count <= KM_RESOURCE_MAX_CLAIMS);
    memcpy(reserved_pins, list, count * sizeof(*list));
    reserved_count = count;
    reserve_calls++;
    return GPIO_LOCAL_OK;
}
#if CONFIG_KLIPPER_MMU_DIFF_BUFFER || defined(KM_TEST_HOSTED_RUNTIME)
int gpio_pin_is_local(uint32_t pin)
{
    for (uint32_t i = 0; i < reserved_count; ++i)
        if (reserved_pins[i] == pin) return 1;
    return 0;
}
struct gpio_in gpio_in_setup_local(uint32_t pin, int32_t pull_up)
{
    (void)pull_up;
    return (struct gpio_in){.encoded = pin};
}
struct gpio_in gpio_in_setup(uint32_t pin, int32_t pull_up)
{
    (void)pull_up;
    return (struct gpio_in){.encoded = pin};
}
uint8_t gpio_in_read(struct gpio_in in)
{
    return input_levels[in.encoded];
}
#endif
#if CONFIG_KLIPPER_MMU_DIFF_BUFFER
int gpio_adc_get_route(uint32_t pin, struct gpio_adc_route *out)
{
    if (!out || (pin != pin_id('C', 4) && pin != pin_id('C', 5))) return 0;
    out->unit = 1;
    out->channel = pin == pin_id('C', 4) ? 4 : 8;
    return 1;
}
int gpio_adc_setup_local_pair_timeout(uint32_t pin_a, uint32_t pin_b,
                                      uint32_t timeout_us, struct gpio_adc *out_a,
                                      struct gpio_adc *out_b)
{
    assert(pin_a == pin_id('C', 4) && pin_b == pin_id('C', 5));
    assert(timeout_us > 0u && out_a && out_b);
    adc_setup_a = pin_a;
    adc_setup_b = pin_b;
    *out_a = (struct gpio_adc){.pin = pin_a};
    *out_b = (struct gpio_adc){.pin = pin_b};
    return GPIO_ADC_LOCAL_OK;
}
uint32_t gpio_adc_sample(struct gpio_adc adc)
{
    (void)adc;
    return 0;
}
uint16_t gpio_adc_read(struct gpio_adc adc)
{
    if (adc.pin == pin_id('C', 4)) { adc_reads_a++; return upstream_adc_raw_a; }
    assert(adc.pin == pin_id('C', 5));
    adc_reads_b++;
    return upstream_adc_raw_b;
}
void gpio_adc_cancel_sample(struct gpio_adc adc) { (void)adc; }
#endif

#if !CONFIG_KLIPPER_MMU_DIFF_BUFFER && !defined(KM_TEST_HOSTED_RUNTIME)
km_result_t km_switch_gpio_setup(km_operation_core_t *core, const km_switch_gpio_config_t *config)
{
    (void)core; (void)config;
    return KM_OK;
}
void km_switch_gpio_shutdown(void) {}
/* Diagnostic-only bounce counters (docs/diagnostics.md); not exercised here. */
void km_switch_gpio_get_bounce(uint16_t bay[KM_MAX_LANES], uint16_t load[KM_MAX_LANES],
                               uint16_t *hub, uint16_t *toolhead)
{
    for (unsigned i = 0; i < KM_MAX_LANES; ++i) bay[i] = load[i] = 0;
    *hub = *toolhead = 0;
}
void km_switch_gpio_get_selector_bounce(uint16_t *selector) { *selector = 0; }
#endif
km_result_t km_stepdir_gpio_get_slot_pins(uint32_t slot, uint32_t *step, uint32_t *dir, uint32_t *enable)
{
    (void)slot; (void)step; (void)dir; (void)enable;
    return KM_ERR_UNAVAILABLE;
}
bool km_stepdir_gpio_is_slot_active(uint32_t slot) { (void)slot; return false; }
bool km_stepdir_gpio_is_shutdown(void) { return false; }
void km_stepdir_gpio_shutdown(void) {}
km_result_t km_tmc_uart_gpio_get_pin(uint32_t slot, uint32_t *pin)
{
    (void)slot; (void)pin;
    return KM_ERR_UNAVAILABLE;
}
/* Diagnostic-only gstat/drv_status read (docs/diagnostics.md); not exercised
 * by this fixture's scenarios. */
km_result_t km_tmc_uart_gpio_get_status(uint32_t slot, km_tmc_uart_status_t *out_status)
{
    (void)slot;
    if (!out_status) return KM_ERR_INVALID_PARAM;
    *out_status = (km_tmc_uart_status_t){0};
    return KM_ERR_UNAVAILABLE;
}
bool km_tmc_uart_gpio_is_slot_active(uint32_t slot) { (void)slot; return false; }
bool km_tmc_uart_gpio_is_shutdown(void) { return false; }
void km_tmc_uart_gpio_shutdown(void) {}
km_result_t km_tmc_feeder_setup(uint32_t slot, uint32_t step_pin, uint32_t dir_pin,
                                uint32_t enable_pin, uint32_t uart_pin, bool uart_pull_up,
                                const km_step_dir_config_t *step_config,
                                const km_tmc2209_config_t *tmc_config,
                                uint16_t expected_run_ma, uint16_t expected_hold_ma)
{
    (void)dir_pin; (void)enable_pin; (void)uart_pin; (void)uart_pull_up;
    (void)step_config; (void)tmc_config; (void)expected_run_ma; (void)expected_hold_ma;
    assert(slot < KM_MAX_LANES);
    seen_step[slot] = step_pin;
    return KM_OK;
}
km_result_t km_tmc_feeder_activate(uint32_t slot)
{
    if (slot >= KM_MAX_LANES) return KM_ERR_INVALID_PARAM;
    if (lane_error[slot] != KM_OK) return lane_error[slot];
    return lane_ready[slot] ? KM_OK : KM_ERR_UNAVAILABLE;
}
km_driver_t *km_tmc_feeder_get_driver(uint32_t slot)
{
    return slot < KM_MAX_LANES ? &feeders[slot].base : NULL;
}
km_result_t km_tmc_feeder_restart(uint32_t slot)
{
    /* Linked by the TMC image's restart command; not exercised here. */
    (void)slot;
    return KM_ERR_NOT_SUPPORTED;
}
km_result_t km_pwm_bridge_gpio_get_slot_pins(uint32_t slot, uint32_t *forward, uint32_t *reverse, uint32_t *enable)
{
    (void)slot; (void)forward; (void)reverse; (void)enable;
    return KM_ERR_UNAVAILABLE;
}
bool km_pwm_bridge_gpio_is_slot_active(uint32_t slot) { (void)slot; return false; }
bool km_pwm_bridge_gpio_is_shutdown(void) { return false; }
void km_pwm_bridge_gpio_service(uint32_t slot) { (void)slot; }
km_result_t km_pwm_bridge_gpio_setup(uint32_t slot, const km_resource_claim_t *claims, size_t count,
                                     bool (*ready)(void *context), km_fault_t (*fault)(void *context),
                                     void *context)
{
    (void)claims; (void)count; (void)ready; (void)fault; (void)context;
    assert(slot < KM_MAX_LANES);
    assert(claims == NULL && count == 0);
    return KM_OK;
}
km_driver_t *km_pwm_bridge_gpio_get_driver(uint32_t slot)
{
    return slot < KM_MAX_LANES ? &rewinds[slot].base : NULL;
}
void km_pwm_bridge_gpio_shutdown(void) {}
#if !defined(KM_TEST_PRODUCTION_CONTROLLER)
bool km_controller_is_attached(void) { return false; }
bool km_controller_is_shutdown(void) { return false; }
void km_controller_changed(void) {}
km_result_t km_controller_attach(km_operation_core_t *core, km_controller_sample_fn sample, void *context)
{
    (void)core; (void)sample; (void)context;
    return KM_OK;
}
void km_controller_shutdown(void) {}
void km_controller_note_switch_sample(km_operation_core_t *core, uint32_t sample_us)
{ (void)core; (void)sample_us; }
bool km_controller_get_switch_sample_delay(uint32_t *out_us)
{ (void)out_us; return false; }
void km_controller_get_switch_sample_stats(uint32_t *count, uint32_t *max_us)
{ if (count) *count = 0u; if (max_us) *max_us = 0u; }
#endif
#if !CONFIG_KLIPPER_MMU_DIFF_BUFFER && !defined(KM_TEST_HOSTED_RUNTIME)
void km_sensors_sample(km_operation_core_t *core, void *context)
{
    (void)core; (void)context;
}
#endif

static void fire_timer(void)
{
    struct timer *timer = armed_timer;
    if (timer_queue_mode) {
        timer = NULL;
        for (size_t i = 0u; i < timer_queue_count; ++i) {
            if (!timer || timer_is_before(timer_queue[i]->waketime, timer->waketime))
                timer = timer_queue[i];
        }
    }
    assert(timer && timer->func);
    if (timer_is_before(now_us, timer->waketime)) now_us = timer->waketime;
    uint8_t result = timer->func(timer);
    if (timer_queue_mode && result == SF_DONE) sched_del_timer(timer);
    else assert(result == SF_RESCHEDULE);
#if defined(KM_TEST_PRODUCTION_CONTROLLER)
    km_controller_task(); /* The controller timer only wakes its task. */
#endif
}

#if CONFIG_KLIPPER_MMU_DIFF_BUFFER /* Only the Diff startup and FPS cases queue timers. */
static void run_timers_until(uint32_t target_us)
{
    assert(timer_queue_mode);
    for (unsigned count = 0u; count < 10000u; ++count) {
        struct timer *next = NULL;
        for (size_t i = 0u; i < timer_queue_count; ++i) {
            if (!next || timer_is_before(timer_queue[i]->waketime, next->waketime))
                next = timer_queue[i];
        }
        if (!next || timer_is_before(target_us, next->waketime)) {
            now_us = target_us;
            return;
        }
        now_us = next->waketime;
        fire_timer();
    }
    assert(0 && "timer queue failed to make bounded progress");
}
#endif

#if !defined(KM_TEST_HOSTED_RUNTIME)
static void assert_outputs_idle(const km_operation_core_t *core)
{
    for (size_t lane = 0; lane < core->profile.lane_count; ++lane) {
        assert(core->feeder_drivers[lane] == &feeders[lane].base);
        assert(core->rewinder_drivers[lane] == &rewinds[lane].base);
        assert(!feeders[lane].base.enabled);
        assert(feeders[lane].base.pwm_duty_permille == 0);
        assert(feeders[lane].step_count == 0);
        assert(!rewinds[lane].base.enabled);
        assert(rewinds[lane].base.pwm_duty_permille == 0);
        assert(rewinds[lane].step_count == 0);
    }
}
#endif

#if !defined(KM_TEST_HOSTED_RUNTIME)
static void test_reject_does_not_latch(void)
{
    km_profile_init_boxturtle(&profile);
    prepare_drivers();
    clock_state = KM_CLOCK_STATE_READY;
    irq_blocked = true;
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_UNINITIALIZED);
    km_boxturtle_boot_init();
    km_boxturtle_boot_task();
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_UNINITIALIZED);
    assert(km_board_startup_get_result() == KM_ERR_UNAVAILABLE);
    assert(reserve_calls == 0);
    assert(feeders[0].step_count == 0);
}

/* On the saved legacy preset, SW1/PC4 and SW2/PC5 are still assigned to the
 * hub and first bay switch. Reusing them as analog DiffA/DiffB inputs must
 * fail pin-plan validation until those legacy aliases are remapped. This is a
 * resource-conflict check, not an ADC or board-wiring test. */
static void test_diff_aliases_require_remap(const km_pin_config_t *pins)
{
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    memcpy(claims, pins->claims, pins->count * sizeof(claims[0]));
    bool selected_a = false, selected_b = false;
    for (size_t i = 0; i < pins->count; ++i) {
        if (claims[i].owner == KM_PIN_BUFFER_A) {
            assert(claims[i].instance == pin_id('E', 12));
            claims[i].instance = pin_id('C', 4);
            selected_a = true;
        } else if (claims[i].owner == KM_PIN_BUFFER_B) {
            assert(claims[i].instance == pin_id('E', 13));
            claims[i].instance = pin_id('C', 5);
            selected_b = true;
        }
    }
    assert(selected_a && selected_b);
    km_resource_diagnostic_t diagnostic;
    assert(km_resource_plan_validate(claims, pins->count, &diagnostic) == KM_ERR_CONFLICT);
    assert(diagnostic.reason == KM_RESOURCE_DIAG_DUPLICATE_RESOURCE);

    /* Free reassignment isolates the SW1/SW2 overlap. These replacement pins
     * are illustrative only, not qualified BoxTurtle or ADC assignments. */
    bool remapped_hub = false, remapped_bay = false;
    memcpy(claims, pins->claims, pins->count * sizeof(claims[0]));
    for (size_t i = 0; i < pins->count; ++i) {
        if (claims[i].owner == KM_PIN_HUB) {
            claims[i].instance = pin_id('F', 0);
            remapped_hub = true;
        } else if (claims[i].owner == KM_PIN_BAY && claims[i].instance == pin_id('C', 5)) {
            claims[i].instance = pin_id('F', 1);
            remapped_bay = true;
        } else if (claims[i].owner == KM_PIN_BUFFER_A) {
            claims[i].instance = pin_id('C', 4);
        } else if (claims[i].owner == KM_PIN_BUFFER_B) {
            claims[i].instance = pin_id('C', 5);
        }
    }
    assert(remapped_hub && remapped_bay);
    assert(km_resource_plan_validate(claims, pins->count, &diagnostic) == KM_OK);
}

static void test_boxturtle_from_uninitialized(void)
{
    km_profile_init_boxturtle(&profile);
    assert(km_profile_validate(&profile) == KM_OK);
    km_h723_resource_plan_t plan;
    assert(km_config_h723_resources(&plan, NULL) == KM_OK);
    const km_h723_pwm_route_t expect_routes[4][2] = {
        {{2, 2}, {2, 1}}, {{3, 2}, {3, 1}}, {{12, 2}, {12, 1}}, {{4, 2}, {4, 1}}
    };
    for (size_t lane = 0; lane < 4; ++lane) {
        assert(plan.rewind[lane][0].timer == expect_routes[lane][0].timer);
        assert(plan.rewind[lane][0].channel == expect_routes[lane][0].channel);
        assert(plan.rewind[lane][1].timer == expect_routes[lane][1].timer);
        assert(plan.rewind[lane][1].channel == expect_routes[lane][1].channel);
    }
    km_tmc_uart_config_t tmc_out[KM_MAX_LANES];
    km_pin_config_t pins;
    km_switch_gpio_config_t switches;
    km_pwm_bridge_config_t limits;
    km_step_dir_config_t feeders_out[KM_MAX_LANES];
    assert(km_config_feeders(feeders_out) == KM_OK);
    assert(km_config_tmc(tmc_out) == KM_OK);
    assert(tmc_out[0].tmc.run_current_ma == profile.default_run_current_ma);
    assert(tmc_out[0].tmc.maximum_current_ma == 800u);
    assert(km_config_pins(&pins, NULL) == KM_OK);
    assert(pins.pin[KM_PIN_BAY][0] == pin_id('C', 5));
    assert(pins.pin[KM_PIN_HUB][0] == pin_id('C', 4));
    assert(pins.pin[KM_PIN_TOOLHEAD][0] == pin_id('A', 3));
    assert(pins.pin[KM_PIN_BUFFER_A][0] == pin_id('E', 12));
    assert(pins.pin[KM_PIN_BUFFER_B][0] == pin_id('E', 13));
    test_diff_aliases_require_remap(&pins);
    assert(km_config_switches(&switches) == KM_OK);
    assert(switches.bay[0].pin == pin_id('C', 5));
    assert(switches.hub.pin == pin_id('C', 4));
    assert(switches.toolhead.pin == pin_id('A', 3));
    assert(switches.buffer_low.pin == pin_id('E', 12));
    assert(switches.buffer_high.pin == pin_id('E', 13));
    assert(km_config_rewind(&limits) == KM_OK);
    assert(limits.maximum_duty_permille == 600u);
    prepare_drivers();
    clock_state = KM_CLOCK_STATE_UNAVAILABLE;
    irq_blocked = false;
    km_boxturtle_boot_init();
    km_boxturtle_boot_task();
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_UNINITIALIZED);
    assert(reserve_calls == 0);

    clock_state = KM_CLOCK_STATE_READY;
    fire_timer();
    km_boxturtle_boot_task();
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_WAITING);
    assert(reserve_calls == 1);
    km_board_startup_detail_t detail;
    km_board_startup_get_detail(&detail);
    assert(detail.step == KM_STARTUP_STEP_DRIVER_CONFIRM && detail.lane == 0);
    assert(detail.cause == 0);
    const uint16_t step_ids[4] = {
        pin_id('D', 4), pin_id('C', 12), pin_id('E', 2), pin_id('D', 15)};
    const uint16_t toolhead = pin_id('A', 3);
    const uint16_t rewind_forward = pin_id('A', 1);
    assert(step_ids[0] != rewind_forward && toolhead != step_ids[0] && toolhead != rewind_forward);
    for (size_t lane = 0; lane < 4; ++lane)
        assert(seen_step[lane] == step_ids[lane]);
    bool saw_toolhead = false, saw_forward = false;
    for (uint32_t i = 0; i < reserved_count; ++i) {
        if (reserved_pins[i] == toolhead) saw_toolhead = true;
        if (reserved_pins[i] == rewind_forward) saw_forward = true;
        for (uint32_t j = i + 1; j < reserved_count; ++j)
            assert(reserved_pins[i] != reserved_pins[j]);
    }
    assert(saw_toolhead && saw_forward);
    /* The startup path uses the shipped pin parser and submits the complete
     * digital pin claim batch to this fixture. The fixture records ownership;
     * it does not execute Klipper's production GPIO registry. */
    assert(reserved_count == pins.count);
    for (size_t i = 0; i < pins.count; ++i) {
        assert(pins.claims[i].kind == KM_RESOURCE_PIN);
        assert(reserved_pins[i] == pins.claims[i].instance);
    }

    fire_timer();
    km_boxturtle_boot_task();
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_WAITING);

    for (size_t lane = 0; lane < 4; ++lane) lane_ready[lane] = true;
    fire_timer();
    km_boxturtle_boot_task();
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);
    assert(km_board_startup_get_result() == KM_OK);
    km_operation_core_t *core = km_board_startup_get_core();
    assert(core && strcmp(core->profile.name, "BoxTurtle-Baseline") == 0);
    assert_outputs_idle(core);
}

/* A host load is refused until the heartbeat marks the host live and the
 * bay, hub, toolhead, and buffer samples are fresh. Follow stays on the lane
 * load selected. Unload then retracts that lane until hub and toolhead clear
 * and leaves every feeder and rewind output disabled. */
static void publish_path(km_operation_core_t *core, uint32_t ms, bool hub, bool toolhead)
{
    for (uint8_t lane = 0; lane < 4; ++lane)
        km_core_set_sensors_at(core, lane, true, hub, toolhead, true, ms);
    assert(km_buffer_update_switches(&core->buffer_sensor, false, false, ms) == KM_OK);
}

static void advance_motion(km_operation_core_t *core, km_fake_clock_t *clock,
                           uint32_t *ms, uint32_t duration)
{
    for (uint32_t i = 0; i < duration; ++i) {
        *ms += 1u;
        now_us = *ms * 1000u;
        assert(km_fake_clock_run_trajectory(clock, &core->trajectory, now_us) == KM_OK);
        publish_path(core, *ms, core->filament_present_hub, core->filament_present_toolhead);
        km_core_update(core, *ms, now_us);
    }
}

static void test_print_motion_from_idle(void)
{
    km_operation_core_t *core = km_board_startup_get_core();
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);
    assert_outputs_idle(core);

    uint32_t ms = now_us / 1000u;
    km_core_timepoint_t blocked = {ms, now_us};
    km_admission_result_t rejected = km_core_request_load(
        core, KM_CLIENT_HOST, 1, 1, 1, blocked);
    printf("print-motion before-heartbeat admission=%u steps=%u\n",
           (unsigned)rejected, feeders[0].step_count);
    fflush(stdout);
    assert(rejected == KM_REQ_UNAVAILABLE);
    assert_outputs_idle(core);

    km_core_host_comm_ready(core, ms);
    publish_path(core, ms, false, false);
    km_fake_clock_t clock;
    km_fake_clock_init(&clock, now_us);
    km_core_timepoint_t start = {ms, now_us};
    km_admission_result_t loaded = km_core_request_load(
        core, KM_CLIENT_HOST, 1, 1, 1, start);
    printf("print-motion load lane=1 admission=%u\n", (unsigned)loaded);
    fflush(stdout);
    assert(loaded == KM_REQ_ACCEPTED);

    advance_motion(core, &clock, &ms, 10);
    core->filament_present_hub = true;
    advance_motion(core, &clock, &ms, 1);
    assert(core->stage == KM_STAGE_LOAD_FEED_TO_BUFFER);
    advance_motion(core, &clock, &ms, 10);
    core->filament_present_toolhead = true;
    advance_motion(core, &clock, &ms, 1);
    assert(core->stage == KM_STAGE_FOLLOW);
    km_event_t done;
    assert(km_event_queue_pop(&core->event_queue, &done) == KM_OK);
    assert(done.type == KM_EVENT_LOAD_COMPLETE && done.lane_idx == 1);

    km_core_timepoint_t follow = {ms, now_us};
    km_admission_result_t following = km_core_request_follow(
        core, KM_CLIENT_HOST, 1, 2, true, KM_DIR_FORWARD, follow);
    printf("print-motion follow admission=%u stage=%u steps=%u\n",
           (unsigned)following, (unsigned)core->stage, feeders[1].step_count);
    fflush(stdout);
    assert(following == KM_REQ_ACCEPTED);
    assert(core->stage == KM_STAGE_FOLLOW);
    assert(core->op_state == KM_OP_STATE_FOLLOWING);
    uint32_t loaded_steps = feeders[1].step_count;
    advance_motion(core, &clock, &ms, 10);
    assert(core->stage == KM_STAGE_FOLLOW);
    assert(feeders[1].step_count > loaded_steps);
    for (size_t lane = 0; lane < 4; ++lane) {
        if (lane != 1)
            assert(feeders[lane].step_count == 0);
        assert(rewinds[lane].step_count == 0);
        assert(!rewinds[lane].base.enabled);
    }
    printf("print-motion lane=1 follow steps=%u other_lanes=0 rewind_steps=0\n",
           feeders[1].step_count);
    fflush(stdout);

    km_core_host_comm_ready(core, ms);
    km_core_timepoint_t unload_at = {ms, now_us};
    km_admission_result_t unloading = km_core_request_unload(
        core, KM_CLIENT_HOST, 1, 3, unload_at);
    printf("print-motion unload admission=%u stage=%u\n",
           (unsigned)unloading, (unsigned)core->stage);
    fflush(stdout);
    assert(unloading == KM_REQ_ACCEPTED);
    assert(core->stage == KM_STAGE_UNLOAD_RETRACT_TO_HUB);
    assert(km_event_queue_pop(&core->event_queue, &done) == KM_OK);
    assert(done.type == KM_EVENT_STOPPED && done.lane_idx == 1);

    advance_motion(core, &clock, &ms, 80);
    assert(core->stage == KM_STAGE_UNLOAD_RETRACT_TO_HUB);
    assert(feeders[1].base.enabled);
    assert(feeders[1].step_count > loaded_steps);
    for (size_t lane = 0; lane < 4; ++lane) {
        if (lane != 1)
            assert(feeders[lane].step_count == 0 && !feeders[lane].base.enabled);
        assert(rewinds[lane].step_count == 0);
    }

    core->filament_present_hub = false;
    core->filament_present_toolhead = false;
    bool parked = false;
    for (uint32_t i = 0; i < 1500u; ++i) {
        if ((i % 100u) == 0u)
            km_core_host_comm_ready(core, ms);
        advance_motion(core, &clock, &ms, 1);
        if (core->stage == KM_STAGE_STOPPED) {
            parked = true;
            break;
        }
    }
    printf("print-motion unload stage=%u op=%u fault=%u steps=%u parked=%u\n",
           (unsigned)core->stage, (unsigned)core->op_state,
           (unsigned)core->system_fault, feeders[1].step_count, parked ? 1u : 0u);
    fflush(stdout);
    assert(parked);
    assert(core->op_state == KM_OP_STATE_IDLE);
    assert(!core->path_occupied && !core->shared_path_owned);
    assert(core->system_fault == KM_FAULT_NONE && core->motion_fault == KM_FAULT_NONE);
    assert(km_event_queue_pop(&core->event_queue, &done) == KM_OK);
    assert(done.type == KM_EVENT_UNLOAD_COMPLETE && done.lane_idx == 1);
    for (size_t lane = 0; lane < 4; ++lane) {
        assert(!feeders[lane].base.enabled);
        assert(feeders[lane].base.pwm_duty_permille == 0);
        assert(!rewinds[lane].base.enabled);
        assert(rewinds[lane].base.pwm_duty_permille == 0);
        assert(rewinds[lane].step_count == 0);
        if (lane != 1)
            assert(feeders[lane].step_count == 0);
    }
    printf("print-motion lane=1 unload outputs disabled other_lanes=0\n");
    fflush(stdout);

    uint32_t lane1_steps = feeders[1].step_count;
    km_core_host_comm_ready(core, ms);
    publish_path(core, ms, false, false);
    km_core_timepoint_t next = {ms, now_us};
    km_admission_result_t next_load = km_core_request_load(
        core, KM_CLIENT_HOST, 1, 4, 2, next);
    printf("print-motion load lane=2 admission=%u\n", (unsigned)next_load);
    fflush(stdout);
    assert(next_load == KM_REQ_ACCEPTED);
    advance_motion(core, &clock, &ms, 10);
    core->filament_present_hub = true;
    advance_motion(core, &clock, &ms, 1);
    assert(core->stage == KM_STAGE_LOAD_FEED_TO_BUFFER);
    advance_motion(core, &clock, &ms, 10);
    core->filament_present_toolhead = true;
    advance_motion(core, &clock, &ms, 1);
    assert(core->stage == KM_STAGE_FOLLOW);
    assert(km_event_queue_pop(&core->event_queue, &done) == KM_OK);
    assert(done.type == KM_EVENT_LOAD_COMPLETE && done.lane_idx == 2);
    km_core_timepoint_t follow2 = {ms, now_us};
    km_admission_result_t following2 = km_core_request_follow(
        core, KM_CLIENT_HOST, 1, 5, true, KM_DIR_FORWARD, follow2);
    uint32_t lane2_steps = feeders[2].step_count;
    advance_motion(core, &clock, &ms, 10);
    printf("print-motion lane=2 follow steps=%u lane1_held=%u\n",
           feeders[2].step_count, lane1_steps);
    fflush(stdout);
    assert(following2 == KM_REQ_ACCEPTED);
    assert(core->active_lane == 2);
    assert(feeders[2].step_count > lane2_steps);
    assert(feeders[1].step_count == lane1_steps);
    assert(feeders[0].step_count == 0 && feeders[3].step_count == 0);
    for (size_t lane = 0; lane < 4; ++lane)
        assert(rewinds[lane].step_count == 0);
}

/* A lane whose driver answers wrongly: startup names that lane and the
 * driver's own error, not only the latched KM_ERR_FAULTED. */
static void test_driver_fault_detail(void)
{
    km_profile_init_boxturtle(&profile);
    prepare_drivers();
    clock_state = KM_CLOCK_STATE_UNAVAILABLE;
    irq_blocked = false;
    km_boxturtle_boot_init();
    km_boxturtle_boot_task();
    clock_state = KM_CLOCK_STATE_READY;
    fire_timer();
    km_boxturtle_boot_task();
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_WAITING);
    km_board_startup_detail_t d;
    km_board_startup_get_detail(&d);
    assert(d.step == KM_STARTUP_STEP_DRIVER_CONFIRM && d.lane == 0 && d.cause == 0);
    lane_ready[0] = lane_ready[1] = true;
    lane_error[2] = KM_ERR_TIMEOUT;
    fire_timer();
    km_boxturtle_boot_task();
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_get_result() == KM_ERR_FAULTED);
    km_board_startup_get_detail(&d);
    assert(d.step == KM_STARTUP_STEP_DRIVER_CONFIRM && d.lane == 2);
    assert(d.cause == KM_ERR_TIMEOUT && d.pin[0] == KM_PIN_UNUSED);
}

static int legacy_main(void)
{
    pid_t fault_pid = fork();
    assert(fault_pid >= 0);
    if (fault_pid == 0) {
        test_driver_fault_detail();
        return 0;
    }
    int fault_status = 0;
    assert(waitpid(fault_pid, &fault_status, 0) == fault_pid);
    assert(WIFEXITED(fault_status) && WEXITSTATUS(fault_status) == 0);

    pid_t pid = fork();
    assert(pid >= 0);
    if (pid == 0) {
        test_reject_does_not_latch();
        return 0;
    }
    int status = 0;
    assert(waitpid(pid, &status, 0) == pid);
    assert(WIFEXITED(status) && WEXITSTATUS(status) == 0);
    test_boxturtle_from_uninitialized();
    puts("test_boxturtle_from_uninitialized called km_boxturtle_boot_task");
    puts("begin(NULL, 0) under GPIO local, shared TMC UART, and PWM pair");
    puts("READY on legacy AFC Lite switched-buffer preset (DIFF_BUFFER=0); HUB=PC4, BAY0=PC5");
    puts("feeder/rewind idle step_count 0 STEP PD4,PC12,PE2,PD15");
    puts("sensor transitions below are synthetic core samples; GPIO pin sampling is not exercised");
    test_print_motion_from_idle();
    return 0;
}
#endif /* !KM_TEST_EMU_RUNTIME */

#if CONFIG_KLIPPER_MMU_DIFF_BUFFER
#if CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN
static void ack_head_event(km_operation_core_t *core, uint64_t instance,
                           uint32_t session, uint32_t request, uint8_t operation)
{
    uint32_t query[] = {(uint32_t)(instance >> 32), (uint32_t)instance};
    last_send_format[0] = '\0';
    command_km_event_peek(query);
    assert(strcmp(last_send_format, "km_event instance_hi=%u instance_lo=%u seq=%u timestamp_ms=%u client=%c session=%u request=%u operation=%c lane=%c event_type=%c result_code=%c value=%u flags=%c") == 0);
    km_event_t event;
    assert(km_event_queue_peek(&core->event_queue, &event) == KM_OK);
    assert(event.client_id == KM_CLIENT_HOST && event.session_id == session);
    assert(event.req_id == request && event.op_type == operation);
    uint32_t ack[] = {(uint32_t)(instance >> 32), (uint32_t)instance,
                      event.client_id, event.session_id, event.req_id,
                      event.op_type, event.lane_idx, event.seq};
    uint32_t expected_seq = event.seq;
    ack[7] = expected_seq + 1u;
    command_km_event_ack(ack);
    assert(core->event_queue.count == 1u);
    ack[7] = expected_seq;
    last_send_format[0] = '\0';
    command_km_event_ack(ack);
    assert(strcmp(last_send_format, "km_command_result instance_hi=%u instance_lo=%u command=%c query=%u result=%c") == 0);
    assert(core->event_queue.count == 0u);
}
#endif

#if CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN && (CONFIG_KLIPPER_MMU_DIFF_BUFFER || defined(KM_TEST_HOST_JOIN))
static void diff_set_path(bool hub, bool toolhead)
{
    input_levels[pin_id('E', 12)] = (uint8_t)(hub ? 1u : 0u);
    input_levels[pin_id('E', 7)] = (uint8_t)(toolhead ? 0u : 1u);
}

static void diff_advance_impl(km_operation_core_t *core, km_fake_clock_t *clock,
                             uint32_t *ms, uint32_t duration,
                             bool synthesize_host_refresh)
{
    for (uint32_t i = 0u; i < duration; ++i) {
        uint32_t target = now_us + 1000u;
        while (armed_timer && !timer_is_before(target, armed_timer->waketime)) {
            now_us = armed_timer->waketime;
            fire_timer();
        }
        now_us = target;
        *ms = now_us / 1000u;
        assert(km_fake_clock_run_trajectory(clock, &core->trajectory, now_us) == KM_OK);
        km_sensors_sample(core, NULL);
        *ms = now_us / 1000u;
        if (synthesize_host_refresh && (i % 100u) == 0u)
            km_core_host_comm_ready(core, *ms);
        km_core_update(core, *ms, now_us);
    }
}

static void diff_advance(km_operation_core_t *core, km_fake_clock_t *clock,
                         uint32_t *ms, uint32_t duration)
{
    diff_advance_impl(core, clock, ms, duration, true);
}
#endif

static km_operation_core_t *start_diff_firmware(const uint8_t *encoded_calibration,
                                                size_t calibration_size,
                                                uint8_t selected_lane)
{
    km_diff_adc_config_t calibration = {
        .channel_map = KM_DIFF_CH_A_LOW_B_HIGH,
        .vref_mv = 3300u,
        .adc_max_counts = 4095u,
        .min_valid_mv = 100u,
        .max_valid_mv = 3200u,
        .path_tolerance_counts = 80u,
        .point_count = 2u,
        .points = {{100u, 2100u, 0}, {1900u, 4000u, 1000}},
    };
    /* Simulate Klipper's host-delivered config object before the ordinary
     * diagnostic start command. The diagnostic task consumes only the
     * production calibration getter, never a fixture-local pointer. */
    assert(km_diff_calibration_get() == NULL);
    assert(encoded_calibration && calibration_size <= sizeof(calibration_oid.bytes));
    host_calibration_data = encoded_calibration;
    host_calibration_size = calibration_size;
    uint32_t config_args[] = {1u, (uint32_t)host_calibration_size, 1u};
    command_config_km_diff_calibration(config_args);
    const km_diff_adc_config_t *delivered = km_diff_calibration_get();
    assert(delivered && km_diff_adc_config_validate(delivered) == KM_OK);
    assert(delivered->channel_map == calibration.channel_map);
    assert(delivered->point_count == calibration.point_count);
    km_profile_init_boxturtle(&profile);
    km_device_profile_t configured_profile;
    assert(km_config_profile(&configured_profile, &calibration) == KM_OK);
    assert(configured_profile.has_proportional_buffer);
    prepare_drivers();
    for (unsigned lane = 0; lane < 4u; ++lane) lane_ready[lane] = true;
    clock_state = KM_CLOCK_STATE_READY;
    now_us = 1000u;
    upstream_adc_raw_a = 1000u;  /* 0.81 V: DiffA lower half */
    upstream_adc_raw_b = 3000u;  /* 2.42 V: DiffB upper half */
    adc_setup_a = adc_setup_b = adc_reads_a = adc_reads_b = 0u;
    memset(input_levels, 1, sizeof(input_levels));
    input_levels[pin_id('E', 12)] = 0u; /* active-high HUB, initially clear */
#if CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR
    input_levels[pin_id('E', 7)] = 1u;  /* active-low TOOLHEAD, initially clear */
#endif

    km_pin_config_t pins;
    km_switch_gpio_config_t switches;
    km_adc_pair_config_t acquisition;
    assert(km_config_pins(&pins, NULL) == KM_OK);
    assert(selected_lane < 4u);
    for (size_t lane = 0u; lane < 4u; ++lane) {
        assert(pins.pin[KM_PIN_BAY][lane] != KM_PIN_UNUSED);
        assert(pins.pin[KM_PIN_LOAD][lane] != KM_PIN_UNUSED);
        input_levels[pins.pin[KM_PIN_BAY][lane]] = (uint8_t)(lane == selected_lane ? 0u : 1u);
        input_levels[pins.pin[KM_PIN_LOAD][lane]] = (uint8_t)(lane == selected_lane ? 1u : 0u);
    }
    assert(pins.pin[KM_PIN_BUFFER_A][0] == pin_id('C', 4));
    assert(pins.pin[KM_PIN_BUFFER_B][0] == pin_id('C', 5));
    assert(pins.pin[KM_PIN_HUB][0] == pin_id('E', 12));
    assert(pins.pin[KM_PIN_BAY][0] == pin_id('E', 13));
    assert(pins.pin[KM_PIN_LOAD][0] == pin_id('E', 8));
    assert(km_config_switches(&switches) == KM_OK);
    assert(switches.hub.pin == pin_id('E', 12));
    for (size_t lane = 0u; lane < 4u; ++lane) {
        assert(switches.bay[lane].pin == pins.pin[KM_PIN_BAY][lane]);
        assert(switches.load[lane].pin == pins.pin[KM_PIN_LOAD][lane]);
        assert(!switches.bay[lane].active_high);
        assert(switches.bay[lane].pull == 1);
        assert(switches.load[lane].active_high);
        assert(switches.load[lane].pull == 1);
    }
#if CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR
    assert(switches.toolhead.pin == pin_id('E', 7));
#else
    assert(switches.toolhead.pin == KM_PIN_UNUSED);
#endif
    assert(km_config_adc_pair(&acquisition) == KM_OK);

    uint32_t diagnostic_args[] = {0u};
    command_km_diagnostic_start(diagnostic_args);
    for (unsigned attempt = 0u; attempt < 100u
         && km_diagnostic_get_state() == KM_DIAGNOSTIC_WAITING; ++attempt) {
        km_diagnostic_task();
        if (km_diagnostic_get_state() == KM_DIAGNOSTIC_WAITING && armed_timer)
            fire_timer();
    }
    assert(km_diagnostic_get_state() == KM_DIAGNOSTIC_READY);
    assert(km_diagnostic_get_result() == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);
    assert(adc_setup_a == pin_id('C', 4) && adc_setup_b == pin_id('C', 5));
    assert(km_adc_get_state() == KM_ADC_STATE_RUNNING);
    km_operation_core_t *core = km_board_startup_get_core();
    assert(core && core->buffer_sensor.type == KM_BUFFER_SENSOR_ANALOG_DIFF);
#if defined(KM_TEST_PRODUCTION_CONTROLLER)
    assert(km_controller_is_attached());
#endif

    /* Exercise the production ADC timer and controller sensor callback, with
     * only Klipper's underlying GPIO/ADC observations replaced by this fixture. */
    for (unsigned i = 0; i < 4u; ++i) {
        if (timer_queue_mode) run_timers_until(now_us + 10u);
        else { now_us += 10u; fire_timer(); }
    }
    assert(adc_reads_a > 0u && adc_reads_b > 0u);
    /* Switch inputs publish only after the production 5 ms debounce interval.
     * Advance the normal sample schedule and fire due ADC timers while waiting. */
    for (unsigned sample = 0u; sample < 7u; ++sample) {
        uint32_t target = now_us + 1000u;
        if (timer_queue_mode) run_timers_until(target);
        else {
            while (armed_timer && !timer_is_before(target, armed_timer->waketime)) {
                now_us = armed_timer->waketime;
                fire_timer();
            }
            now_us = target;
            km_sensors_sample(core, NULL);
        }
    }
    assert(core->bay_sensors_valid);
    assert(core->hub_sensor_valid && !core->filament_present_hub);
#if CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR
    assert(core->toolhead_sensor_valid && !core->filament_present_toolhead);
#else
    assert(!core->profile.has_toolhead_sensor);
    assert(!core->filament_present_toolhead);
#endif
    for (size_t lane = 0u; lane < 4u; ++lane) {
        assert(core->bay_valid[lane]
               && core->filament_present_bay[lane] == (lane == selected_lane));
        assert(core->load_valid[lane]
               && core->filament_present_load[lane] == (lane == selected_lane));
    }
    assert(core->buffer_sensor.is_valid);

    assert_outputs_idle(core);
    return core;
}

static void test_diff_startup_sensor_pipeline(void)
{
    static const uint8_t encoded_calibration[18] = {
        (uint8_t)KM_DIFF_CH_A_LOW_B_HIGH, 2u,
        0xe4u, 0x0cu, 100u, 0u, 128u, 12u, 80u, 0u,
        100u, 0u, 52u, 8u, 108u, 7u, 160u, 15u
    };
#if CONFIG_KLIPPER_MMU_FPS_ARRIVAL /* Otherwise the command is not built. */
    /* LOAD now needs a calibrated HUB-to-arrival distance first. 109091 steps
     * (150 mm) puts the calibrated minimum (S - the real image's 50 mm
     * window, 36364 steps) at exactly 72727 -- the outer minimum this test
     * already waits for below -- and its maximum well inside the outer 1900
     * mm bound, so the rest of this test's travel and timing are unchanged. */
    uint32_t steps_args[] = {2u, 109091u};
    command_config_km_fps_steps(steps_args);
#endif
    km_operation_core_t *core = start_diff_firmware(
        encoded_calibration, sizeof(encoded_calibration), 0u);
    const uint64_t instance = UINT64_C(0x123456789abcdef0);
    const uint32_t session = 1u;
#if CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN
    assert(core->profile.unload_hub_clear_steps
           == KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_UNLOAD_HUB_CLEAR_MM));
    assert(core->profile.unload_hub_clear_steps > 0u);
    /* Exercise the production LOAD/FOLLOW/UNLOAD handler sequence with ADC
     * and switch sampling on scheduled production timer deadlines. */
    diff_set_path(false, false);
    uint32_t ms = now_us / 1000u;
    km_fake_clock_t motion_clock;
    km_fake_clock_init(&motion_clock, now_us);
    issue_command_heartbeat(instance, session);
    issue_command_start(instance, session, 1u, KM_OP_TYPE_LOAD, 0u,
                        KM_DIR_FORWARD);
    assert(core->stage == KM_STAGE_LOAD_FEED_TO_HUB);
    diff_advance(core, &motion_clock, &ms, 50u);
    /* The B2 tick path steps through tests/fake_clock.h's reference executor,
     * so the bound mock driver's set_step_edge counter stays 0: completed
     * reference steps are the count feeders[0].step_count used to show. */
    assert(core->stage == KM_STAGE_LOAD_FEED_TO_HUB
           && km_ref_position(&motion_clock.ref) > 0);
    /* Only lane 0's driver is bound to the shared trajectory and the other
     * lanes' drivers never enabled, so the tick path cannot step them. */
    for (size_t lane = 1u; lane < 4u; ++lane)
        assert(!feeders[lane].base.enabled
               && core->trajectory.driver != &feeders[lane].base);
    diff_set_path(true, false);
    diff_advance(core, &motion_clock, &ms, 15u);
    assert(core->stage == KM_STAGE_LOAD_FEED_TO_BUFFER);
    assert(core->filament_present_hub && core->hub_sensor_valid);
#if CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR
    diff_set_path(true, true);
    diff_advance(core, &motion_clock, &ms, 15u);
    assert(core->filament_present_toolhead && core->toolhead_sensor_valid);
#else
    /* The image's own FPS arrival values: feed past its minimum travel, then
     * hold the buffer position at 800 as if stopped at the extruder. */
    while (km_trajectory_get_position(&core->trajectory) - core->fps_hub_steps
           < (int64_t)core->profile.fps_arrival_min_steps) {
        assert(core->stage == KM_STAGE_LOAD_FEED_TO_BUFFER);
        diff_advance(core, &motion_clock, &ms, 10u);
    }
    diff_advance(core, &motion_clock, &ms, 5u);
    assert(core->stage == KM_STAGE_LOAD_FEED_TO_BUFFER);
    upstream_adc_raw_a = 1540u;
    upstream_adc_raw_b = 3620u;
    diff_advance(core, &motion_clock, &ms, 30u);
    upstream_adc_raw_a = 1000u;
    upstream_adc_raw_b = 3000u;
#endif
    assert(core->stage == KM_STAGE_FOLLOW);
    km_event_t terminal;
    assert(km_event_queue_peek(&core->event_queue, &terminal) == KM_OK);
    assert(terminal.type == KM_EVENT_LOAD_COMPLETE && terminal.lane_idx == 0u);
    ack_head_event(core, instance, session, 1u, KM_OP_TYPE_LOAD);
    issue_command_start(instance, session, 2u, KM_OP_TYPE_FOLLOW, 0u,
                        KM_DIR_FORWARD);
    /* FOLLOW's progress is the tick path's completed-step count; the mock
     * driver's edge counter no longer moves. */
    int64_t following_steps = km_ref_position(&motion_clock.ref);
    diff_advance(core, &motion_clock, &ms, 5u);
    assert(core->stage == KM_STAGE_FOLLOW
           && km_ref_position(&motion_clock.ref) > following_steps);
    /* Only lane 0's driver is bound to the shared trajectory and the other
     * lanes' drivers never enabled, so the tick path cannot step them. */
    for (size_t lane = 1u; lane < 4u; ++lane)
        assert(!feeders[lane].base.enabled
               && core->trajectory.driver != &feeders[lane].base);
    issue_command_start(instance, session, 3u, KM_OP_TYPE_UNLOAD, 0u,
                        KM_DIR_REVERSE);
    assert(core->stage == KM_STAGE_UNLOAD_RETRACT_TO_HUB);
    assert(km_event_queue_peek(&core->event_queue, &terminal) == KM_OK);
    assert(terminal.type == KM_EVENT_STOPPED && terminal.lane_idx == 0u);
    ack_head_event(core, instance, session, 2u, KM_OP_TYPE_FOLLOW);
    diff_advance(core, &motion_clock, &ms, 5u);
    assert(feeders[0].base.enabled);
#if CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR
    diff_set_path(false, true);
    diff_advance(core, &motion_clock, &ms, 5u);
    assert(core->stage == KM_STAGE_UNLOAD_RETRACT_TO_HUB);
#endif
    diff_set_path(false, false);
    diff_advance(core, &motion_clock, &ms, 100u);
    assert(core->path_occupied);
    assert(core->stage == KM_STAGE_UNLOAD_STOP_SETTLE
           || core->stage == KM_STAGE_UNLOAD_REWIND_TO_SPOOL);
    for (unsigned i = 0u; i < 1500u && core->stage == KM_STAGE_UNLOAD_STOP_SETTLE; ++i)
        diff_advance(core, &motion_clock, &ms, 1u);
    assert(core->stage == KM_STAGE_UNLOAD_REWIND_TO_SPOOL);
    assert(core->trajectory.has_target_position);
    int64_t target = core->trajectory.target_position_steps;
    assert(km_trajectory_get_model_position(&core->trajectory) - target
           <= (int64_t)core->profile.unload_hub_clear_steps);
    for (unsigned i = 0u; i < 5000u && core->stage != KM_STAGE_STOPPED; ++i)
        diff_advance(core, &motion_clock, &ms, 1u);
    assert(core->stage == KM_STAGE_STOPPED && core->op_state == KM_OP_STATE_IDLE);
    assert(km_trajectory_get_model_position(&core->trajectory) == target);
    assert(km_event_queue_peek(&core->event_queue, &terminal) == KM_OK);
    assert(terminal.type == KM_EVENT_UNLOAD_COMPLETE && terminal.lane_idx == 0u);
    ack_head_event(core, instance, session, 3u, KM_OP_TYPE_UNLOAD);
    for (size_t lane = 0u; lane < 4u; ++lane) {
        assert(!feeders[lane].base.enabled && feeders[lane].base.pwm_duty_permille == 0u);
        assert(!rewinds[lane].base.enabled && rewinds[lane].base.pwm_duty_permille == 0u);
        assert(rewinds[lane].step_count == 0u);
        /* The tick path (B2) only ever steps the shared trajectory's driver, so
         * a lane whose driver is not bound to it provably never moved; the
         * mock's per-edge counter that used to prove this stays 0 regardless. */
        if (lane != 0u)
            assert(core->trajectory.driver != &feeders[lane].base);
    }
#else
    issue_command_heartbeat(instance, session);
    uint32_t start[] = {(uint32_t)(instance >> 32), (uint32_t)instance,
                        KM_CLIENT_HOST, session, 1u, KM_OP_TYPE_LOAD, 0u,
                        KM_DIR_FORWARD};
    command_km_start(start);
    assert(strcmp(last_send_format,
                  "km_command_result instance_hi=%u instance_lo=%u command=%c query=%u result=%c") == 0);
    assert(last_send_count == 5u && last_send_values[4] == KM_COMMAND_UNAVAILABLE);
    assert(!core->shared_path_owned && core->event_queue.count == 0u);
    assert_outputs_idle(core);
#endif

    /* A fresh pair outside configured electrical limits faults closed. */
    upstream_adc_raw_a = 0u;
    now_us += 10u; fire_timer(); now_us += 10u; fire_timer();
    now_us += 10u; fire_timer(); now_us += 10u; fire_timer();
    km_sensors_sample(core, NULL);
    assert(!core->buffer_sensor.is_valid);
    assert(core->buffer_sensor.fault == KM_FAULT_BUFFER_SIGNAL);

    /* Restore valid raw input, then age the production sample beyond its cap. */
    upstream_adc_raw_a = 1000u;
    for (unsigned i = 0; i < 4u; ++i) {
        now_us += 10u;
        fire_timer();
    }
    km_sensors_sample(core, NULL);
    assert(core->buffer_sensor.is_valid);
    now_us += CONFIG_KLIPPER_MMU_ADC_SAMPLE_AGE_US + 1u;
    km_sensors_sample(core, NULL);
    assert(!core->buffer_sensor.is_valid);
    assert(core->buffer_sensor.fault == KM_FAULT_BUFFER_SIGNAL);
#if CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN
    puts("DIFF startup: production command/ACK and sampled sensor path passed with mocked upstream IO");
#else
    puts("DIFF safe startup: sampled sensors, refused START and idle outputs passed with mocked upstream IO");
#endif
}

/* FPS arrival starts LOAD, so it exists only in the actuation image. */
#if CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN
static void fps_advance_controller(uint32_t duration_ms)
{
    assert(timer_queue_mode);
    for (uint32_t ms = 0u; ms < duration_ms; ++ms)
        run_timers_until(now_us + 1000u);
}

static void test_fps_composed(const char *scenario)
{
    static const uint8_t encoded_calibration[18] = {
        (uint8_t)KM_DIFF_CH_A_LOW_B_HIGH, 2u,
        0xe4u, 0x0cu, 100u, 0u, 128u, 12u, 80u, 0u,
        100u, 0u, 52u, 8u, 108u, 7u, 160u, 15u
    };
    const uint64_t instance = UINT64_C(0x123456789abcdef0);
    const uint32_t session = 7u;
    /* LOAD now needs a calibrated HUB-to-arrival distance before it runs at
     * all: a config command carries it, delivered before the calibration
     * object like Klipper's other config commands. The shim's 1 mm window
     * is 727 microsteps at the fixture mechanics (KM_FEEDER_MM_TO_STEPS(1)),
     * so S +/- 727 is the actual arrival window firmware uses. Max travel
     * calibrates so that window's far edge lands at 2182 microsteps (the
     * same failure point the old host-narrowed outer bounds gave); the
     * other scenarios calibrate so its near edge lands at 727 (the outer
     * minimum this fixture already waits for), which the mocked ADC then
     * satisfies well inside. */
    const bool narrowed = strcmp(scenario, "fps-max-travel") == 0;
    const uint32_t calibrated_steps = narrowed ? 1455u : 1454u;
    const uint32_t calibrated_min = narrowed ? 728u : 727u;
#if CONFIG_KLIPPER_MMU_FPS_ARRIVAL /* Otherwise the command is not built. */
    uint32_t steps_args[] = {2u, calibrated_steps};
    command_config_km_fps_steps(steps_args);
#endif
    timer_queue_mode = true;
    km_operation_core_t *core = start_diff_firmware(
        encoded_calibration, sizeof(encoded_calibration), 0u);
    assert(core->profile.fps_arrival);
    assert(core->profile.fps_arrival_permille == 700u);
    assert(core->profile.fps_arrival_samples == 3u);
    /* The outer travel bounds never narrow now; only the calibrated
     * distance, read back below, does. */
    assert(core->profile.fps_arrival_min_steps == 727u);
    assert(core->profile.fps_arrival_max_steps == 2909u);
    assert(core->calibrated_arrival_steps == calibrated_steps);
    assert(core->profile.has_toolhead_sensor == (CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR != 0));
    assert(timer_queue_count >= 2u); /* ADC and controller both remain scheduled. */

    issue_command_heartbeat(instance, session);
    issue_command_start(instance, session, 1u, KM_OP_TYPE_LOAD, 0u,
                        KM_DIR_FORWARD);
    assert(last_send_values[7] == KM_REQ_ACCEPTED);
    assert(core->stage == KM_STAGE_LOAD_FEED_TO_HUB);
    /* The tick path steps through the linked mcu/controller.c and reports
     * completed steps via km_trajectory_observe, so the trajectory's own
     * completed count is the observable that the mock driver's per-edge
     * counter used to be. */
    int64_t initial_steps = km_trajectory_get_position(&core->trajectory);
    fps_advance_controller(100u);
    assert(km_trajectory_get_position(&core->trajectory) > initial_steps);
    assert(core->stage == KM_STAGE_LOAD_FEED_TO_HUB);

    diff_set_path(true, false);
    fps_advance_controller(8u); /* Actual 5 ms debounce plus periodic control. */
    assert(core->filament_present_hub && core->hub_sensor_valid);
    assert(core->stage == KM_STAGE_LOAD_FEED_TO_BUFFER);
    assert(km_trajectory_get_position(&core->trajectory) >= core->fps_hub_steps);
    /* Establish a fresh low sample after minimum travel before applying the
     * compression transition; FPS requires release-then-arrival evidence, and
     * the calibrated window's minimum (calibrated_min) governs it now, not
     * the outer travel bound. */
    for (unsigned tick = 0u; tick < 200u; ++tick) {
        if (km_trajectory_get_position(&core->trajectory) - core->fps_hub_steps
            >= (int64_t)calibrated_min) break;
        assert(core->stage == KM_STAGE_LOAD_FEED_TO_BUFFER);
        fps_advance_controller(10u);
    }
    assert(km_trajectory_get_position(&core->trajectory) - core->fps_hub_steps
           >= (int64_t)calibrated_min);
    fps_advance_controller(8u);

    if (strcmp(scenario, "fps-max-travel") == 0) {
        /* Each attempt stops at the narrowed bound; the retries retract
         * behind the HUB observation and feed again, then LOAD fails once.
         * The host keeps its lease through the seconds of retraction. */
        for (uint32_t ms = 0u; ms < 30000u && core->op_state != KM_OP_STATE_FAULT; ++ms) {
            if (ms % 500u == 0u) issue_command_heartbeat(instance, session);
            fps_advance_controller(1u);
        }
        assert(core->system_fault == KM_FAULT_NO_PROGRESS);
        assert(core->load_attempt_count == KM_LOAD_ATTEMPTS);
        for (unsigned i = 0u; i < KM_LOAD_ATTEMPTS; ++i)
            assert(core->load_attempts[i].stage == KM_STAGE_LOAD_FEED_TO_BUFFER
                   && core->load_attempts[i].fault == KM_FAULT_NO_PROGRESS
                   && core->load_attempts[i].travel_steps >= 2182u
                   && core->load_attempts[i].travel_steps < 2909u);
        assert(core->stage == KM_STAGE_STOPPED && core->op_state == KM_OP_STATE_FAULT);
        assert(!feeders[0].base.enabled && core->event_queue.count == 1u);
        puts("Composed FPS max-travel failure after three attempts passed");
        return;
    }

    if (strcmp(scenario, "fps-invalid-pair") == 0) {
        upstream_adc_raw_a = 4095u;
        upstream_adc_raw_b = 4095u;
        fps_advance_controller(8u);
        assert(core->system_fault == KM_FAULT_BUFFER_SIGNAL);
        assert(core->stage == KM_STAGE_STOPPED && core->op_state == KM_OP_STATE_FAULT);
        assert(!core->buffer_sensor.is_valid && !feeders[0].base.enabled);
        puts("Composed FPS invalid-pair failure passed");
        return;
    }

#if CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR
    if (strcmp(scenario, "fps-toolhead-success") == 0) {
        diff_set_path(true, true);
        fps_advance_controller(7u);
        assert(core->toolhead_sensor_valid && core->filament_present_toolhead);
    } else {
        assert(strcmp(scenario, "fps-toolhead-required") == 0);
        assert(core->toolhead_sensor_valid && !core->filament_present_toolhead);
    }
#else
    assert(strcmp(scenario, "fps-no-toolhead-success") == 0);
    assert(!core->profile.has_toolhead_sensor);
    assert(!core->filament_present_toolhead);
#endif

    upstream_adc_raw_a = 1540u;
    upstream_adc_raw_b = 3620u;
    fps_advance_controller(12u);
    if (CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR
        && strcmp(scenario, "fps-toolhead-required") == 0) {
        assert(core->system_fault == KM_FAULT_SENSOR_FAILURE);
        assert(core->stage == KM_STAGE_STOPPED && core->op_state == KM_OP_STATE_FAULT);
        assert(core->event_queue.count != 0u);
        km_event_t event;
        assert(km_event_queue_peek(&core->event_queue, &event) == KM_OK);
        assert(event.type == KM_EVENT_LOAD_FAILED);
        puts("Composed FPS required-but-absent toolhead failure passed");
        return;
    }
    assert(core->stage == KM_STAGE_FOLLOW);
    assert(core->op_state == KM_OP_STATE_FOLLOWING);
    assert(!core->fps_min_reached || core->fps_count >= 3u);
    if (!CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR)
        assert(!core->filament_present_toolhead);
    km_event_t event;
    assert(km_event_queue_peek(&core->event_queue, &event) == KM_OK);
    assert(event.type == KM_EVENT_LOAD_COMPLETE && event.lane_idx == 0u);
    printf("Composed FPS %s passed through scheduled controller and ADC timers\n", scenario);
}
#endif /* CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN */
#endif /* CONFIG_KLIPPER_MMU_DIFF_BUFFER */

#if CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN && (CONFIG_KLIPPER_MMU_DIFF_BUFFER || defined(KM_TEST_HOST_JOIN))
static uint32_t parse_host_word(char *text)
{
    assert(text && *text && *text != '-');
    char *end = NULL;
    unsigned long value = strtoul(text, &end, 10);
    assert(end && !*end && value <= UINT32_MAX);
    return (uint32_t)value;
}

static void print_host_response(void)
{
    assert(last_send_count > 0u);
    printf("{\"format\":\"%s\",\"values\":[", last_send_format);
    for (size_t i = 0u; i < last_send_count; ++i)
        printf("%s%u", i ? "," : "", last_send_values[i]);
    puts("]}");
    fflush(stdout);
}

static void host_step(uint32_t duration, bool hub, bool toolhead)
{
    km_operation_core_t *core = km_board_startup_get_core();
    assert(core);
#if CONFIG_KLIPPER_MMU_DIFF_BUFFER
    diff_set_path(hub, toolhead);
#else
    input_levels[pin_id('C', 4)] = (uint8_t)(hub ? 1u : 0u);
    input_levels[pin_id('E', 7)] = (uint8_t)(toolhead ? 0u : 1u);
#endif
    uint32_t ms = now_us / 1000u;
    static km_fake_clock_t motion_clock;
    if (motion_clock.now_us == 0u) km_fake_clock_init(&motion_clock, now_us);
    for (uint32_t i = 0u; i < duration; ++i) {
        uint32_t target = now_us + 1000u;
        while (armed_timer && !timer_is_before(target, armed_timer->waketime)) {
            now_us = armed_timer->waketime;
            fire_timer();
        }
        now_us = target;
        ms = now_us / 1000u;
        assert(km_fake_clock_run_trajectory(&motion_clock, &core->trajectory, now_us) == KM_OK);
        /* The tick path emits this lane's feeder steps inside the shared
         * trajectory, so each tick's completed steps are the pulses it drove
         * that tick, in whichever direction it stepped. Their magnitude total
         * is exactly the mock driver's old per-edge step_count, which no
         * longer moves: a reverse UNLOAD adds to it just as the old falling
         * edges did. Accumulating per tick (not per print) keeps the pulses a
         * tick reverses inside from cancelling against each other. */
        int64_t position = km_trajectory_get_position(&core->trajectory);
        int64_t pulse_delta = position - host_pulses_from;
        host_pulses += (uint32_t)(pulse_delta < 0 ? -pulse_delta : pulse_delta);
        host_pulses_from = position;
        km_sensors_sample(core, NULL);
        km_core_update(core, ms, now_us);
    }
    bool other_lanes_still = true;
    for (size_t lane = 0u; lane < 4u; ++lane)
        if (lane != host_lane)
            other_lanes_still = other_lanes_still
                && core->trajectory.driver != &feeders[lane].base;
    printf("{\"stage\":%d,\"buffer\":%d,\"hub_valid\":%s,\"hub\":%s,"
           "\"toolhead_valid\":%s,\"toolhead\":%s,"
           "\"bay_valid\":%s,\"bay\":%s,\"load_valid\":%s,\"load\":%s,"
           "\"active_steps\":%u,\"model_position\":%lld,"
           "\"other_lanes_still\":%s}\n",
           (int)core->stage,
           core->buffer_sensor.is_valid ? (int)core->buffer_sensor.position_permille : -1,
           core->hub_sensor_valid ? "true" : "false",
           core->filament_present_hub ? "true" : "false",
           core->toolhead_sensor_valid ? "true" : "false",
           core->filament_present_toolhead ? "true" : "false",
           (core->bay_sensors_valid && core->bay_valid[host_lane]) ? "true" : "false",
           core->filament_present_bay[host_lane] ? "true" : "false",
           core->load_valid[host_lane] ? "true" : "false",
           core->filament_present_load[host_lane] ? "true" : "false",
           host_pulses,
           (long long)km_trajectory_get_model_position(&core->trajectory),
           other_lanes_still ? "true" : "false");
    fflush(stdout);
}

#if defined(KM_TEST_HOST_JOIN) && !CONFIG_KLIPPER_MMU_DIFF_BUFFER
static km_operation_core_t *start_switched_firmware(uint8_t selected_lane)
{
    assert(selected_lane < 4u);
    km_profile_init_boxturtle(&profile);
    assert(km_config_profile(&profile, NULL) == KM_OK);
    prepare_drivers();
    for (unsigned lane = 0u; lane < 4u; ++lane) lane_ready[lane] = true;
    clock_state = KM_CLOCK_STATE_READY;
    now_us = 1000u;
    memset(input_levels, 1, sizeof(input_levels));
    input_levels[pin_id('C', 4)] = 0u; /* HUB clear; active high. */
    input_levels[pin_id('E', 7)] = 1u; /* TOOLHEAD clear; active low. */
    input_levels[pin_id('E', 12)] = 1u; /* Both switched-buffer inputs idle. */
    input_levels[pin_id('E', 13)] = 1u;

    km_pin_config_t pins;
    assert(km_config_pins(&pins, NULL) == KM_OK);
    assert(pins.pin[KM_PIN_HUB][0] == pin_id('C', 4));
    assert(pins.pin[KM_PIN_BAY][0] == pin_id('C', 5));
    assert(pins.pin[KM_PIN_BUFFER_A][0] == pin_id('E', 12));
    assert(pins.pin[KM_PIN_BUFFER_B][0] == pin_id('E', 13));
    for (size_t lane = 0u; lane < 4u; ++lane) {
        input_levels[pins.pin[KM_PIN_BAY][lane]] = (uint8_t)(lane == selected_lane ? 0u : 1u);
        input_levels[pins.pin[KM_PIN_LOAD][lane]] = (uint8_t)(lane == selected_lane ? 1u : 0u);
    }

    uint32_t diagnostic_args[] = {0u};
    command_km_diagnostic_start(diagnostic_args);
    for (unsigned attempt = 0u; attempt < 100u
         && km_diagnostic_get_state() == KM_DIAGNOSTIC_WAITING; ++attempt) {
        km_diagnostic_task();
        if (km_diagnostic_get_state() == KM_DIAGNOSTIC_WAITING && armed_timer)
            fire_timer();
    }
    assert(km_diagnostic_get_state() == KM_DIAGNOSTIC_READY);
    assert(km_diagnostic_get_result() == KM_OK);
    km_operation_core_t *core = km_board_startup_get_core();
    assert(core && core->buffer_sensor.type == KM_BUFFER_SENSOR_SWITCHED);
    for (unsigned sample = 0u; sample < 8u; ++sample) {
        now_us += 1000u;
        km_sensors_sample(core, NULL);
    }
    assert(core->buffer_sensor.is_valid);
    return core;
}
#endif

static void host_session(uint8_t selected_lane)
{
    assert(selected_lane < 4u);
    host_lane = selected_lane;
#if CONFIG_KLIPPER_MMU_DIFF_BUFFER
    static uint8_t calibration_bytes[54];
#endif
    char line[256];
    while (fgets(line, sizeof(line), stdin)) {
        char *name = strtok(line, " \t\r\n");
        assert(name);
        if (!strcmp(name, "startup")) {
#if CONFIG_KLIPPER_MMU_DIFF_BUFFER
            char *hex = strtok(NULL, " \t\r\n");
            assert(hex);
            size_t chars = strlen(hex);
            assert(chars >= 36u && chars <= sizeof(calibration_bytes) * 2u
                   && chars % 2u == 0u);
            for (size_t i = 0u; i < chars / 2u; ++i) {
                unsigned byte = 0u;
                assert(sscanf(hex + i * 2u, "%2x", &byte) == 1);
                calibration_bytes[i] = (uint8_t)byte;
            }
            assert(!strtok(NULL, " \t\r\n"));
            km_operation_core_t *core = start_diff_firmware(
                calibration_bytes, chars / 2u, host_lane);
            assert(core && core->buffer_sensor.is_valid);
#else
            assert(!strtok(NULL, " \t\r\n"));
            km_operation_core_t *core = start_switched_firmware(host_lane);
            assert(core && core->buffer_sensor.is_valid);
#endif
            printf("{\"startup\":true,\"fps_min_steps\":%u,\"fps_max_steps\":%u,"
                   "\"calibrated_steps\":%u}\n",
                   core->profile.fps_arrival_min_steps, core->profile.fps_arrival_max_steps,
                   core->calibrated_arrival_steps);
#if CONFIG_KLIPPER_MMU_FPS_ARRIVAL
        } else if (!strcmp(name, "config_km_fps_steps")) {
            /* Klipper's config phase: delivered before the startup init. */
            uint32_t args[2];
            for (unsigned i = 0u; i < 2u; ++i)
                args[i] = parse_host_word(strtok(NULL, " \t\r\n"));
            command_config_km_fps_steps(args);
            puts("{\"steps\":true}");
#endif
#if CONFIG_KLIPPER_MMU_DIFF_BUFFER
        } else if (!strcmp(name, "fixture_buffer")) {
            /* Raw DiffA/DiffB counts for the next ADC acquisitions. */
            upstream_adc_raw_a = (uint16_t)parse_host_word(strtok(NULL, " \t\r\n"));
            upstream_adc_raw_b = (uint16_t)parse_host_word(strtok(NULL, " \t\r\n"));
            puts("{\"buffer\":true}");
#endif
        } else if (!strcmp(name, "fixture_lane")) {
            /* Seat a spool in another bay and follow that lane's feeder. */
            uint32_t lane = parse_host_word(strtok(NULL, " \t\r\n"));
            assert(lane < 4u);
            km_pin_config_t pins;
            assert(km_config_pins(&pins, NULL) == KM_OK);
            input_levels[pins.pin[KM_PIN_BAY][lane]] = 0u;
            input_levels[pins.pin[KM_PIN_LOAD][lane]] = 1u;
            host_lane = (uint8_t)lane;
            puts("{\"lane\":true}");
        } else if (!strcmp(name, "fixture_advance")) {
            uint32_t duration = parse_host_word(strtok(NULL, " \t\r\n"));
            uint32_t hub = parse_host_word(strtok(NULL, " \t\r\n"));
            uint32_t toolhead = parse_host_word(strtok(NULL, " \t\r\n"));
            assert(duration <= 1000u && hub <= 1u && toolhead <= 1u);
            assert(!strtok(NULL, " \t\r\n"));
            host_step(duration, hub != 0u, toolhead != 0u);
        } else if (!strcmp(name, "fixture_outputs")) {
            assert(!strtok(NULL, " \t\r\n"));
            km_operation_core_t *core = km_board_startup_get_core();
            assert(core);
            bool off = true;
            for (size_t lane = 0u; lane < 4u; ++lane)
                off = off && !feeders[lane].base.enabled
                    && feeders[lane].base.pwm_duty_permille == 0u
                    && !rewinds[lane].base.enabled
                    && rewinds[lane].base.pwm_duty_permille == 0u;
            printf("{\"outputs_off\":%s}\n", off ? "true" : "false");
            fflush(stdout);
        } else {
            uint32_t args[8] = {0u};
            unsigned count;
            if (!strcmp(name, "km_transport_query")
                    || !strcmp(name, "km_diagnostic_query")) count = 1u;
            else if (!strcmp(name, "km_heartbeat")) count = 3u;
            else if (!strcmp(name, "km_start")) count = 8u;
            else if (!strcmp(name, "km_event_peek")) count = 2u;
            else if (!strcmp(name, "km_event_ack")) count = 8u;
            else if (!strcmp(name, "km_snapshot_part")
                     || !strcmp(name, "km_sensor_part")
                     || !strcmp(name, "km_diag_part")) count = 4u;
            else if (!strcmp(name, "km_path_reconcile")) count = 5u;
            else if (!strcmp(name, "km_load_attempts_query")) count = 7u;
            else assert(!"unexpected host command");
            for (unsigned i = 0u; i < count; ++i)
                args[i] = parse_host_word(strtok(NULL, " \t\r\n"));
            assert(!strtok(NULL, " \t\r\n"));
            last_send_count = 0u;
            last_send_format[0] = '\0';
            if (!strcmp(name, "km_transport_query")) {
                command_km_transport_query(args);
            } else if (!strcmp(name, "km_diagnostic_query")) {
                command_km_diagnostic_query(args);
            } else if (!strcmp(name, "km_heartbeat")) {
                command_km_heartbeat(args);
                assert(strcmp(last_send_format,
                              "km_command_result instance_hi=%u instance_lo=%u command=%c query=%u result=%c") == 0);
                assert(last_send_count == 5u && last_send_values[4] == KM_COMMAND_OK);
            } else if (!strcmp(name, "km_start")) {
                km_operation_core_t *core = km_board_startup_get_core();
                int64_t admitted_at = core
                    ? km_trajectory_get_position(&core->trajectory) : 0;
                command_km_start(args);
                assert(strcmp(last_send_format,
                              "km_admission instance_hi=%u instance_lo=%u client=%c session=%u request=%u operation=%c lane=%c admission=%c fault=%c") == 0);
                assert(last_send_count == 9u && last_send_values[7] == KM_REQ_ACCEPTED);
                assert(last_send_values[6] == host_lane && args[6] == host_lane);
                assert(core && core->shared_path_owned);
                if (args[5] == KM_OP_TYPE_LOAD)
                    /* Admission plans the move; it emits no step. The mock
                     * driver's per-edge counter used to show that (and no
                     * longer moves), so the tick path's completed-step
                     * position stands in for it: it must be exactly where it
                     * was before the command was admitted. */
                    assert(core->stage == KM_STAGE_LOAD_FEED_TO_HUB
                           && km_trajectory_get_position(&core->trajectory)
                              == admitted_at);
            } else if (!strcmp(name, "km_event_peek")) {
                command_km_event_peek(args);
            } else if (!strcmp(name, "km_event_ack")) {
                command_km_event_ack(args);
            } else if (!strcmp(name, "km_snapshot_part")) {
                command_km_snapshot_part(args);
            } else if (!strcmp(name, "km_path_reconcile")) {
                command_km_path_reconcile(args);
            } else if (!strcmp(name, "km_load_attempts_query")) {
                command_km_load_attempts_query(args);
            } else if (!strcmp(name, "km_diag_part")) {
                command_km_diag_part(args);
            } else {
                command_km_sensor_part(args);
            }
            print_host_response();
        }
        fflush(stdout);
    }
}
#endif

#if defined(KM_TEST_HOSTED_RUNTIME) && !defined(KM_TEST_HOST_JOIN)
#if CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN
static void emu_ack_terminal(km_operation_core_t *core, uint64_t instance,
                             uint32_t session, uint32_t request,
                             uint8_t operation, uint8_t lane,
                             uint8_t expected_type)
{
    uint32_t peek[] = {(uint32_t)(instance >> 32), (uint32_t)instance};
    last_send_format[0] = '\0';
    command_km_event_peek(peek);
    assert(strcmp(last_send_format,
        "km_event instance_hi=%u instance_lo=%u seq=%u timestamp_ms=%u client=%c session=%u request=%u operation=%c lane=%c event_type=%c result_code=%c value=%u flags=%c") == 0);
    km_event_t event;
    assert(km_event_queue_peek(&core->event_queue, &event) == KM_OK);
    assert(event.client_id == KM_CLIENT_HOST && event.session_id == session);
    assert(event.req_id == request && event.op_type == operation
           && event.lane_idx == lane && event.type == expected_type);
    uint32_t ack[] = {(uint32_t)(instance >> 32), (uint32_t)instance,
        KM_CLIENT_HOST, session, request, operation, lane, event.seq + 1u};
    command_km_event_ack(ack);
    assert(core->event_queue.count == 1u);
    assert(last_send_values[4] == KM_COMMAND_STALE);
    ack[7] = event.seq;
    command_km_event_ack(ack);
    assert(strcmp(last_send_format,
        "km_command_result instance_hi=%u instance_lo=%u command=%c query=%u result=%c") == 0);
    assert(last_send_values[4] == KM_COMMAND_OK && core->event_queue.count == 0u);

    uint32_t terminal[] = {(uint32_t)(instance >> 32), (uint32_t)instance,
        KM_CLIENT_HOST, session, request, operation, lane};
    last_send_format[0] = '\0';
    command_km_terminal_query(terminal);
    assert(strcmp(last_send_format,
        "km_event instance_hi=%u instance_lo=%u seq=%u timestamp_ms=%u client=%c session=%u request=%u operation=%c lane=%c event_type=%c result_code=%c value=%u flags=%c") == 0);
    assert(last_send_values[6] == request && last_send_values[7] == operation
           && last_send_values[8] == lane && last_send_values[9] == expected_type);
}
#endif

static void emu_sample(km_operation_core_t *core)
{
    km_sensors_sample(core, NULL);
}

/* Startup switch evidence needs five milliseconds of timely production
 * samples. Keep the service cadence at one millisecond while the fixture
 * waits for its first qualified levels. */
static void emu_qualify(km_operation_core_t *core)
{
    for (unsigned i = 0u; i < 7u; ++i) {
        now_us += 1000u;
        emu_sample(core);
    }
    assert(core->bay_sensors_valid);
}

#if CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN
static void emu_advance(km_operation_core_t *core, km_fake_clock_t *motion,
                        uint32_t duration_ms, uint32_t session)
{
    for (uint32_t i = 0u; i < duration_ms; ++i) {
        uint32_t target_us = now_us + 1000u;
        while (armed_timer && !timer_is_before(target_us, armed_timer->waketime)) {
            now_us = armed_timer->waketime;
            fire_timer();
        }
        now_us = target_us;
        assert(km_fake_clock_run_trajectory(motion, &core->trajectory, now_us) == KM_OK);
        emu_sample(core);
        if (session && (i % 100u) == 0u)
            issue_command_heartbeat(test_boot_identity, session);
        km_core_update(core, now_us / 1000u, now_us);
    }
}

/* The rate core brakes jerk-limited, so the settle that the old trapezoidal
 * planner completed within a few milliseconds now takes far longer: stopping
 * from the retract velocity at the configured jerk is a few hundred
 * milliseconds. Advance one millisecond at a time until the core reports
 * STOPPED instead of asserting the old planner's exact settle time. */
static void emu_advance_to_stopped(km_operation_core_t *core, km_fake_clock_t *motion,
                                   uint32_t session)
{
    /* Up to 10 simulated seconds: a 100 mm HUB clearance at Happy Hare's
     * 100 mm/s^2 alone takes about two. */
    for (unsigned attempt = 0u; attempt < 10000u && core->stage != KM_STAGE_STOPPED; ++attempt)
        emu_advance(core, motion, 1u, session);
}
#endif

#if defined(KM_TEST_EMU_RUNTIME)
static void test_emu_hosted_load_follow_unload(void)
{
    km_device_profile_t configured;
    km_pin_config_t pins;
    assert(km_config_profile(&configured, NULL) == KM_OK);
    assert(configured.lane_count == 5u && configured.load_is_virtual_hub
           && configured.has_load_sensors && configured.has_hub_sensor);
    profile = configured;
    prepare_drivers();
    for (unsigned lane = 0; lane < profile.lane_count; ++lane)
        lane_ready[lane] = true;
    assert(km_config_pins(&pins, NULL) == KM_OK);
    memset(input_levels, 1, sizeof(input_levels)); /* inactive for the saved active-low image */
    assert(pins.pin[KM_PIN_BAY][0] == pin_id('E', 5));
    assert(pins.pin[KM_PIN_LOAD][0] == pin_id('E', 10));
    assert(pins.pin[KM_PIN_LOAD][4] == pin_id('E', 14));
    assert(pins.pin[KM_PIN_TOOLHEAD][0] == pin_id('C', 0));
    input_levels[pins.pin[KM_PIN_BAY][0]] = 0u; /* selected lane has filament at BAY */

    clock_state = KM_CLOCK_STATE_READY;
    now_us = 1000u;
    test_diagnostic_start();
    for (unsigned attempt = 0u; attempt < 100u
         && km_diagnostic_get_state() == KM_DIAGNOSTIC_WAITING; ++attempt) {
        km_diagnostic_task();
        if (km_diagnostic_get_state() == KM_DIAGNOSTIC_WAITING && armed_timer)
            fire_timer();
    }
    assert(km_diagnostic_get_state() == KM_DIAGNOSTIC_READY);
    assert(km_diagnostic_get_result() == KM_OK);
    uint32_t challenge[] = {0x4567u};
    last_send_format[0] = '\0';
    command_km_diagnostic_query(challenge);
    assert(strcmp(last_send_format,
        "km_diagnostic_state challenge=%u state=%c result=%i") == 0);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);
    km_operation_core_t *core = km_board_startup_get_core();
    assert(core && core->profile.lane_count == 5u);
    assert(core->buffer_sensor.type == KM_BUFFER_SENSOR_SWITCHED);
    assert(reserve_calls == 1u && reserved_count == pins.count);
    for (size_t lane = 0u; lane < profile.lane_count; ++lane) {
        assert(core->feeder_drivers[lane] == &feeders[lane].base);
        /* The tick path drives one shared trajectory, not the drivers' edge
         * callbacks: zero completed steps is the fact the inert mock
         * step_count used to stand for, and every driver is still off. */
        assert(!feeders[lane].base.enabled
               && km_trajectory_get_position(&core->trajectory) == 0);
    }

    /* The shipped switch service reads the selected lane's physical LOAD pin
     * and projects it as the shared virtual HUB while that lane owns the path. */
    emu_qualify(core);
    assert(core->bay_valid[0] && core->filament_present_bay[0]);
    for (size_t lane = 0u; lane < profile.lane_count; ++lane)
        assert(core->load_valid[lane] && !core->filament_present_load[lane]);

    const uint32_t session = 23u;
    const uint64_t instance = test_boot_identity;
    km_fake_clock_t motion;
    km_fake_clock_init(&motion, now_us);
    issue_command_heartbeat(instance, session);
    issue_command_start(instance, session, 1u, KM_OP_TYPE_LOAD, 0u,
                        KM_DIR_FORWARD);
    assert(last_send_values[7] == KM_REQ_ACCEPTED);
    assert(core->stage == KM_STAGE_LOAD_FEED_TO_HUB);
    /* The first step from rest takes (6/J)^(1/3), 33 ms at the default
     * J = 20 x 8000 steps/s^3. */
    emu_advance(core, &motion, 60u, session);
    /* The reference executor plays stepper.c on the tick path, so the feeder
     * really moved iff it completed steps: its position, not the mock's
     * set_step_edge counter. */
    assert(km_ref_position(&motion.ref) > 0);
    for (size_t lane = 1u; lane < profile.lane_count; ++lane)
        /* Only the bound lane-0 driver is fed the shared trajectory's steps;
         * the other lanes are unbound and disabled, so the tick path cannot
         * step them. */
        assert(!feeders[lane].base.enabled
               && core->trajectory.driver != &feeders[lane].base);

    input_levels[pins.pin[KM_PIN_LOAD][0]] = 0u;
    emu_qualify(core);
    assert(core->load_valid[0] && core->filament_present_load[0]);
    assert(core->hub_sensor_valid && core->filament_present_hub);
    emu_advance(core, &motion, 5u, session);
    assert(core->stage == KM_STAGE_LOAD_FEED_TO_BUFFER);
    input_levels[pins.pin[KM_PIN_TOOLHEAD][0]] = 0u;
    emu_advance(core, &motion, 7u, session);
    assert(core->stage == KM_STAGE_FOLLOW);
    assert(core->filament_present_toolhead && core->toolhead_sensor_valid);
    emu_ack_terminal(core, instance, session, 1u, KM_OP_TYPE_LOAD, 0u,
                     KM_EVENT_LOAD_COMPLETE);

    issue_command_start(instance, session, 2u, KM_OP_TYPE_FOLLOW, 0u,
                        KM_DIR_FORWARD);
    assert(last_send_values[7] == KM_REQ_ACCEPTED);
    /* FOLLOW's progress is the tick path's completed-step count; the mock
     * edge counter no longer moves. */
    int64_t follow_steps = km_ref_position(&motion.ref);
    emu_advance(core, &motion, 20u, session);
    assert(core->stage == KM_STAGE_FOLLOW
           && km_ref_position(&motion.ref) > follow_steps);
    for (size_t lane = 1u; lane < profile.lane_count; ++lane)
        assert(!feeders[lane].base.enabled
               && core->trajectory.driver != &feeders[lane].base);

    issue_command_start(instance, session, 3u, KM_OP_TYPE_UNLOAD, 0u,
                        KM_DIR_REVERSE);
    assert(last_send_values[7] == KM_REQ_ACCEPTED);
    assert(core->stage == KM_STAGE_UNLOAD_RETRACT_TO_HUB);
    emu_ack_terminal(core, instance, session, 2u, KM_OP_TYPE_FOLLOW, 0u,
                     KM_EVENT_STOPPED);
    input_levels[pins.pin[KM_PIN_LOAD][0]] = 1u;
    input_levels[pins.pin[KM_PIN_TOOLHEAD][0]] = 1u;
    emu_advance_to_stopped(core, &motion, session);
    assert(core->stage == KM_STAGE_STOPPED && core->op_state == KM_OP_STATE_IDLE);
    emu_ack_terminal(core, instance, session, 3u, KM_OP_TYPE_UNLOAD, 0u,
                     KM_EVENT_UNLOAD_COMPLETE);
    assert(!core->path_occupied && !core->shared_path_owned);
    for (size_t lane = 0u; lane < profile.lane_count; ++lane) {
        assert(!feeders[lane].base.enabled);
        /* All tick-path steps went to the one bound lane-0 driver; every other
         * lane is unbound and its driver was disabled, so none stepped. */
        assert(lane == 0u || core->trajectory.driver != &feeders[lane].base);
    }
    puts("EMU exact-image hosted diagnostic startup, LOAD virtual-HUB input, production command cycle and retained ACK passed");
}
#endif

#if defined(KM_TEST_NIGHTOWL_RUNTIME)
static void nightowl_assert_pins(const km_pin_config_t *pins)
{
    for (unsigned lane = 0u; lane < 4u; ++lane) {
#if CONFIG_MACH_STM32F207 || CONFIG_MACH_STM32F405 || CONFIG_MACH_STM32F407 || CONFIG_MACH_STM32F429 || CONFIG_MACH_STM32F765 || CONFIG_MACH_STM32G431 || CONFIG_MACH_STM32G474 || CONFIG_MACH_STM32H743 || CONFIG_MACH_STM32H750 || CONFIG_MACH_STM32G0B0 || CONFIG_MACH_STM32G0B1
        assert(pins->pin[KM_PIN_STEP][lane] == pin_id('A', lane + 1u));
        assert(pins->pin[KM_PIN_DIR][lane] == pin_id('A', lane + 5u));
        assert(pins->pin[KM_PIN_ENABLE][lane] == pin_id('B', lane));
        assert(pins->pin[KM_PIN_UART][lane] == pin_id('C', lane + 4u));
        assert(pins->pin[KM_PIN_BAY][lane] == pin_id('B', lane + 12u));
        assert(pins->pin[KM_PIN_LOAD][lane] == pin_id('B', lane + 8u));
#else
        assert(pins->pin[KM_PIN_STEP][lane] == pin_id('D', lane));
        assert(pins->pin[KM_PIN_DIR][lane] == pin_id('D', lane + 4u));
        assert(pins->pin[KM_PIN_ENABLE][lane] == pin_id('D', lane + 8u));
        assert(pins->pin[KM_PIN_UART][lane] == pin_id('E', lane));
        assert(pins->pin[KM_PIN_BAY][lane] == pin_id('E', lane + 4u));
        assert(pins->pin[KM_PIN_LOAD][lane] == pin_id('E', lane + 8u));
#endif
    }
    assert(pins->pin[KM_PIN_HUB][0] == pin_id('C', 0));
    assert(pins->pin[KM_PIN_TOOLHEAD][0] == pin_id('C', 1));
    assert(pins->pin[KM_PIN_BUFFER_A][0] == pin_id('C', 2));
    assert(pins->pin[KM_PIN_BUFFER_B][0] == pin_id('C', 3));
}

#if !CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN
static void test_nightowl_safe_startup_denies_start(void)
{
    km_device_profile_t configured;
    km_pin_config_t pins;
    km_switch_gpio_config_t switches;
    assert(km_config_profile(&configured, NULL) == KM_OK);
    assert(configured.lane_count == 4u && !configured.load_is_virtual_hub
           && !configured.has_powered_rewind && configured.has_load_sensors
           && configured.has_hub_sensor && configured.has_toolhead_sensor);
    profile = configured;
    prepare_drivers();
    for (unsigned lane = 0; lane < profile.lane_count; ++lane)
        lane_ready[lane] = true;
    assert(km_config_pins(&pins, NULL) == KM_OK);
    assert(km_config_switches(&switches) == KM_OK);
    memset(input_levels, 1, sizeof(input_levels));
    nightowl_assert_pins(&pins);
    assert(switches.hub.pin == pins.pin[KM_PIN_HUB][0]);
    assert(km_config_tmc(tmc) == KM_OK);
    for (unsigned lane = 0u; lane < profile.lane_count; ++lane) {
        assert(tmc[lane].tmc.address == 0u && tmc[lane].uart_pull_up);
        assert(tmc[lane].tmc.sense_resistor_milliohms == 110u);
        assert(tmc[lane].tmc.maximum_current_ma == 800u);
        assert(tmc[lane].tmc.run_current_ma == 700u);
        assert(tmc[lane].tmc.hold_current_ma == 150u);
    }
    input_levels[pins.pin[KM_PIN_BAY][0]] = 0u;
    input_levels[pins.pin[KM_PIN_LOAD][0]] = 0u;

    clock_state = KM_CLOCK_STATE_READY;
    now_us = 1000u;
    test_diagnostic_start();
    for (unsigned attempt = 0u; attempt < 100u
         && km_diagnostic_get_state() == KM_DIAGNOSTIC_WAITING; ++attempt) {
        km_diagnostic_task();
        if (km_diagnostic_get_state() == KM_DIAGNOSTIC_WAITING && armed_timer)
            fire_timer();
    }
    assert(km_diagnostic_get_state() == KM_DIAGNOSTIC_READY);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);
    assert(reserve_calls == 1u && reserved_count == pins.count);
    km_operation_core_t *core = km_board_startup_get_core();
    assert(core && core->profile.lane_count == 4u);
    emu_qualify(core);
    assert(core->filament_present_bay[0] && core->filament_present_load[0]);
    /* Zero completed trajectory steps is the tick-path fact the inert mock
     * step_count used to stand for; the driver is still off. */
    assert(!feeders[0].base.enabled
           && km_trajectory_get_position(&core->trajectory) == 0);

    uint32_t heartbeat[] = {(uint32_t)(test_boot_identity >> 32),
        (uint32_t)test_boot_identity, KM_CLIENT_HOST, 23u};
    command_km_heartbeat(heartbeat);
    uint32_t start[] = {(uint32_t)(test_boot_identity >> 32),
        (uint32_t)test_boot_identity, KM_CLIENT_HOST, 23u, 1u,
        KM_OP_TYPE_LOAD, 0u, KM_DIR_FORWARD};
    command_km_start(start);
    assert(strcmp(last_send_format,
        "km_command_result instance_hi=%u instance_lo=%u command=%c query=%u result=%c") == 0);
    assert(last_send_values[4] == KM_COMMAND_UNAVAILABLE);
    /* The denied START never bound or fed a driver, so the tick path completed
     * no steps and no driver came on. */
    for (size_t lane = 0u; lane < profile.lane_count; ++lane)
        assert(!feeders[lane].base.enabled
               && km_trajectory_get_position(&core->trajectory) == 0);
    puts("NightOwl safe image reached diagnostic READY and denied START with all feeders idle");
}
#endif

#if CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN
static void test_nightowl_hosted_load_follow_unload(void)
{
    km_device_profile_t configured;
    km_pin_config_t pins;
    km_switch_gpio_config_t switches;
    assert(CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN);
    assert(km_config_profile(&configured, NULL) == KM_OK);
    assert(configured.lane_count == 4u && !configured.load_is_virtual_hub
           && !configured.has_powered_rewind && configured.has_load_sensors);
    profile = configured;
    prepare_drivers();
    for (unsigned lane = 0u; lane < profile.lane_count; ++lane)
        lane_ready[lane] = true;
    assert(km_config_pins(&pins, NULL) == KM_OK);
    assert(km_config_switches(&switches) == KM_OK);
    memset(input_levels, 1, sizeof(input_levels));
    nightowl_assert_pins(&pins);
    assert(switches.hub.pin == pins.pin[KM_PIN_HUB][0]);
    assert(km_config_tmc(tmc) == KM_OK);
    for (unsigned lane = 0u; lane < profile.lane_count; ++lane) {
        assert(tmc[lane].tmc.address == 0u && tmc[lane].uart_pull_up);
        assert(tmc[lane].tmc.sense_resistor_milliohms == 110u);
        assert(tmc[lane].tmc.maximum_current_ma == 800u);
        assert(tmc[lane].tmc.run_current_ma == 700u);
        assert(tmc[lane].tmc.hold_current_ma == 150u);
    }
    input_levels[pins.pin[KM_PIN_BAY][0]] = 0u;
    input_levels[pins.pin[KM_PIN_LOAD][0]] = 0u;

    clock_state = KM_CLOCK_STATE_READY;
    now_us = 1000u;
    test_diagnostic_start();
    for (unsigned attempt = 0u; attempt < 100u
         && km_diagnostic_get_state() == KM_DIAGNOSTIC_WAITING; ++attempt) {
        km_diagnostic_task();
        if (km_diagnostic_get_state() == KM_DIAGNOSTIC_WAITING && armed_timer)
            fire_timer();
    }
    assert(km_diagnostic_get_state() == KM_DIAGNOSTIC_READY);
    assert(km_diagnostic_get_result() == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);
    km_operation_core_t *core = km_board_startup_get_core();
    assert(core && core->profile.lane_count == 4u);
    assert(core->buffer_sensor.type == KM_BUFFER_SENSOR_SWITCHED);
    assert(reserve_calls == 1u && reserved_count == pins.count);
    for (size_t lane = 0u; lane < profile.lane_count; ++lane)
        /* Zero completed trajectory steps is the tick-path fact the inert mock
         * step_count used to stand for, and every driver is still off. */
        assert(!feeders[lane].base.enabled
               && km_trajectory_get_position(&core->trajectory) == 0);

    emu_qualify(core);
    assert(core->bay_valid[0] && core->filament_present_bay[0]);
    assert(core->load_valid[0] && core->filament_present_load[0]);
    assert(core->hub_sensor_valid && !core->filament_present_hub);
    const uint32_t session = 23u;
    const uint64_t instance = test_boot_identity;
    km_fake_clock_t motion;
    km_fake_clock_init(&motion, now_us);
    issue_command_heartbeat(instance, session);
    issue_command_start(instance, session, 1u, KM_OP_TYPE_LOAD, 0u, KM_DIR_FORWARD);
    assert(last_send_values[7] == KM_REQ_ACCEPTED);
    assert(core->stage == KM_STAGE_LOAD_FEED_TO_HUB);
    /* The first step from rest takes (6/J)^(1/3), 33 ms at the default
     * J = 20 x 8000 steps/s^3. */
    emu_advance(core, &motion, 60u, session);
    /* The reference executor plays stepper.c on the tick path, so the feeder
     * really moved iff it completed steps: its position, not the mock's
     * set_step_edge counter. */
    assert(km_ref_position(&motion.ref) > 0);
    for (size_t lane = 1u; lane < profile.lane_count; ++lane)
        /* Only the bound lane-0 driver is fed the shared trajectory's steps;
         * the other lanes are unbound and disabled, so the tick path cannot
         * step them. */
        assert(!feeders[lane].base.enabled
               && core->trajectory.driver != &feeders[lane].base);

    input_levels[pins.pin[KM_PIN_HUB][0]] = 0u;
    emu_qualify(core);
    assert(core->hub_sensor_valid && core->filament_present_hub);
    emu_advance(core, &motion, 5u, session);
    assert(core->stage == KM_STAGE_LOAD_FEED_TO_BUFFER);
    input_levels[pins.pin[KM_PIN_TOOLHEAD][0]] = 0u;
    emu_advance(core, &motion, 7u, session);
    assert(core->stage == KM_STAGE_FOLLOW);
    assert(core->filament_present_toolhead && core->toolhead_sensor_valid);
    emu_ack_terminal(core, instance, session, 1u, KM_OP_TYPE_LOAD, 0u,
                     KM_EVENT_LOAD_COMPLETE);

    issue_command_start(instance, session, 2u, KM_OP_TYPE_FOLLOW, 0u, KM_DIR_FORWARD);
    assert(last_send_values[7] == KM_REQ_ACCEPTED);
    /* FOLLOW's progress is the tick path's completed-step count; the mock
     * edge counter no longer moves. */
    int64_t follow_steps = km_ref_position(&motion.ref);
    emu_advance(core, &motion, 20u, session);
    assert(core->stage == KM_STAGE_FOLLOW
           && km_ref_position(&motion.ref) > follow_steps);
    for (size_t lane = 1u; lane < profile.lane_count; ++lane)
        assert(!feeders[lane].base.enabled
               && core->trajectory.driver != &feeders[lane].base);

    issue_command_start(instance, session, 3u, KM_OP_TYPE_UNLOAD, 0u, KM_DIR_REVERSE);
    assert(last_send_values[7] == KM_REQ_ACCEPTED);
    assert(core->stage == KM_STAGE_UNLOAD_RETRACT_TO_HUB);
    emu_ack_terminal(core, instance, session, 2u, KM_OP_TYPE_FOLLOW, 0u, KM_EVENT_STOPPED);
    input_levels[pins.pin[KM_PIN_HUB][0]] = 1u;
    input_levels[pins.pin[KM_PIN_TOOLHEAD][0]] = 1u;
    emu_advance_to_stopped(core, &motion, session);
    assert(core->stage == KM_STAGE_STOPPED && core->op_state == KM_OP_STATE_IDLE);
    emu_ack_terminal(core, instance, session, 3u, KM_OP_TYPE_UNLOAD, 0u,
                     KM_EVENT_UNLOAD_COMPLETE);
    assert(!core->path_occupied && !core->shared_path_owned);
    for (size_t lane = 0u; lane < profile.lane_count; ++lane) {
        assert(!feeders[lane].base.enabled);
        /* All tick-path steps went to the one bound lane-0 driver; every other
         * lane is unbound and its driver was disabled, so none stepped. */
        assert(lane == 0u || core->trajectory.driver != &feeders[lane].base);
    }

    input_levels[pins.pin[KM_PIN_BAY][0]] = 0u;
    input_levels[pins.pin[KM_PIN_LOAD][0]] = 0u;
    input_levels[pins.pin[KM_PIN_HUB][0]] = 1u;
    input_levels[pins.pin[KM_PIN_TOOLHEAD][0]] = 1u;
    emu_qualify(core);
    assert(core->bay_valid[0] && core->filament_present_bay[0]);
    assert(core->load_valid[0] && core->filament_present_load[0]);
    assert(core->hub_sensor_valid && !core->filament_present_hub);
    assert(core->toolhead_sensor_valid && !core->filament_present_toolhead);
    issue_command_heartbeat(instance, session);
    issue_command_start(instance, session, 4u, KM_OP_TYPE_LOAD, 0u, KM_DIR_FORWARD);
    assert(last_send_values[7] == KM_REQ_ACCEPTED);
    /* The lease-loss run's own baseline: completed steps from the tick path,
     * not the inert mock edge counter. */
    int64_t loss_steps = km_ref_position(&motion.ref);
    /* The first step from rest takes (6/J)^(1/3), 33 ms at the default
     * J = 20 x 8000 steps/s^3. */
    emu_advance(core, &motion, 60u, session);
    assert(km_ref_position(&motion.ref) > loss_steps);
    for (size_t lane = 1u; lane < profile.lane_count; ++lane)
        assert(!feeders[lane].base.enabled
               && core->trajectory.driver != &feeders[lane].base);
    emu_advance(core, &motion, core->profile.host_watchdog_ms, 0u);
    assert(core->system_fault == KM_FAULT_HOST_COMM_LOST);
    assert(core->stage == KM_STAGE_STOPPED && core->op_state == KM_OP_STATE_FAULT);
    for (size_t lane = 0u; lane < profile.lane_count; ++lane) {
        assert(!feeders[lane].base.enabled);
        /* The host-lease fault stopped the one bound lane-0 driver and never
         * bound another, so no other lane could have stepped. */
        assert(lane == 0u || core->trajectory.driver != &feeders[lane].base);
    }
    emu_ack_terminal(core, instance, session, 4u, KM_OP_TYPE_LOAD, 0u,
                     KM_EVENT_LOAD_FAILED);
    puts("NightOwl exact-image hosted startup, physical HUB, LOAD/FOLLOW/UNLOAD, retained ACK and host-lease loss passed");
}
#endif
#endif

/* KMS, Angry Beaver, 3MS and QuattroBox 1.0/1.1/2: one parametrised scenario
 * for all six -- same four-lane, physical-HUB, no-PWM/ADC shape as NightOwl.
 * Only KMS's traced profile has a per-lane LOAD/exit switch
 * (CONFIG_KLIPPER_MMU_LOAD_SENSORS), read here at compile time from the
 * saved image so no per-device branch is needed. */
#if defined(KM_TEST_PASSIVE_RUNTIME)
static void passive_assert_pins(const km_pin_config_t *pins)
{
#if CONFIG_MACH_STM32G0B0 || CONFIG_MACH_STM32G0B1
    /* Angry Beaver's one G0B row: Happy Hare's real MMB 2.0 MULTIGEAR pins
     * (installer/boards/Kconfig.mmb_2_0:15-49), not the H723 illustrative
     * shape below. This device has no BAY/LOAD switch (CONFIG_KLIPPER_MMU_
     * BAY_SENSORS is off), so neither #if below ever fires for it. */
    static const char step_port[4] = {'D', 'C', 'C', 'C'};
    static const unsigned step_bit[4] = {4u, 9u, 15u, 13u};
    static const char dir_port[4] = {'D', 'C', 'C', 'C'};
    static const unsigned dir_bit[4] = {3u, 8u, 11u, 12u};
    static const char enable_port[4] = {'D', 'D', 'C', 'C'};
    static const unsigned enable_bit[4] = {5u, 2u, 10u, 14u};
    static const char uart_port[4] = {'B', 'B', 'B', 'D'};
    static const unsigned uart_bit[4] = {5u, 4u, 3u, 6u};
    for (unsigned lane = 0u; lane < 4u; ++lane) {
        assert(pins->pin[KM_PIN_STEP][lane] == pin_id(step_port[lane], step_bit[lane]));
        assert(pins->pin[KM_PIN_DIR][lane] == pin_id(dir_port[lane], dir_bit[lane]));
        assert(pins->pin[KM_PIN_ENABLE][lane] == pin_id(enable_port[lane], enable_bit[lane]));
        assert(pins->pin[KM_PIN_UART][lane] == pin_id(uart_port[lane], uart_bit[lane]));
    }
#if !CONFIG_KLIPPER_MMU_VIRTUAL_HUB
    assert(pins->pin[KM_PIN_HUB][0] == pin_id('A', 15u));
#endif
#else
    for (unsigned lane = 0u; lane < 4u; ++lane) {
        assert(pins->pin[KM_PIN_STEP][lane] == pin_id('D', lane));
        assert(pins->pin[KM_PIN_DIR][lane] == pin_id('D', lane + 4u));
        assert(pins->pin[KM_PIN_ENABLE][lane] == pin_id('D', lane + 8u));
        assert(pins->pin[KM_PIN_UART][lane] == pin_id('E', lane));
#if CONFIG_KLIPPER_MMU_BAY_SENSORS
        assert(pins->pin[KM_PIN_BAY][lane] == pin_id('E', lane + 4u));
#endif
#if CONFIG_KLIPPER_MMU_LOAD_SENSORS
        assert(pins->pin[KM_PIN_LOAD][lane] == pin_id('E', lane + 8u));
#endif
    }
#if !CONFIG_KLIPPER_MMU_VIRTUAL_HUB
    assert(pins->pin[KM_PIN_HUB][0] == pin_id('C', 0));
#endif
#endif
    assert(pins->pin[KM_PIN_TOOLHEAD][0] == pin_id('C', 1));
    assert(pins->pin[KM_PIN_BUFFER_A][0] == pin_id('C', 2));
    assert(pins->pin[KM_PIN_BUFFER_B][0] == pin_id('C', 3));
}

#if !CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN
static void test_passive_safe_startup_denies_start(void)
{
    km_device_profile_t configured;
    km_pin_config_t pins;
    assert(km_config_profile(&configured, NULL) == KM_OK);
    assert(configured.lane_count == 4u
           && configured.load_is_virtual_hub == (bool)CONFIG_KLIPPER_MMU_VIRTUAL_HUB
           && !configured.has_powered_rewind && configured.has_hub_sensor
           && configured.has_toolhead_sensor
           && configured.has_bay_sensors == (bool)CONFIG_KLIPPER_MMU_BAY_SENSORS
           && configured.has_load_sensors == (bool)CONFIG_KLIPPER_MMU_LOAD_SENSORS);
    profile = configured;
    prepare_drivers();
    for (unsigned lane = 0; lane < profile.lane_count; ++lane)
        lane_ready[lane] = true;
    assert(km_config_pins(&pins, NULL) == KM_OK);
    memset(input_levels, 1, sizeof(input_levels));
    passive_assert_pins(&pins);
#if CONFIG_KLIPPER_MMU_BAY_SENSORS
    input_levels[pins.pin[KM_PIN_BAY][0]] = 0u;
#endif
#if CONFIG_KLIPPER_MMU_LOAD_SENSORS && !CONFIG_KLIPPER_MMU_VIRTUAL_HUB
    /* A physical-HUB device's per-lane LOAD/exit switch is incidental to
     * this startup step: pre-set it, unlike a virtual-hub device, where the
     * owning lane's LOAD switch IS the hub boundary and must start clear. */
    input_levels[pins.pin[KM_PIN_LOAD][0]] = 0u;
#endif

    clock_state = KM_CLOCK_STATE_READY;
    now_us = 1000u;
    test_diagnostic_start();
    for (unsigned attempt = 0u; attempt < 100u
         && km_diagnostic_get_state() == KM_DIAGNOSTIC_WAITING; ++attempt) {
        km_diagnostic_task();
        if (km_diagnostic_get_state() == KM_DIAGNOSTIC_WAITING && armed_timer)
            fire_timer();
    }
    assert(km_diagnostic_get_state() == KM_DIAGNOSTIC_READY);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);
    assert(reserve_calls == 1u && reserved_count == pins.count);
    km_operation_core_t *core = km_board_startup_get_core();
    assert(core && core->profile.lane_count == 4u);
    emu_qualify(core);
#if CONFIG_KLIPPER_MMU_BAY_SENSORS
    assert(core->filament_present_bay[0]);
#endif
    assert(!feeders[0].base.enabled
           && km_trajectory_get_position(&core->trajectory) == 0);
    uint32_t start[] = {(uint32_t)(test_boot_identity >> 32),
        (uint32_t)test_boot_identity, KM_CLIENT_HOST, 23u, 1u,
        KM_OP_TYPE_LOAD, 0u, KM_DIR_FORWARD};
    command_km_start(start);
    assert(strcmp(last_send_format,
        "km_command_result instance_hi=%u instance_lo=%u command=%c query=%u result=%c") == 0);
    assert(last_send_values[4] == KM_COMMAND_UNAVAILABLE);
    for (size_t lane = 0u; lane < profile.lane_count; ++lane)
        assert(!feeders[lane].base.enabled
               && km_trajectory_get_position(&core->trajectory) == 0);
    puts("Passive-feeder safe image reached diagnostic READY and denied START with all feeders idle");
}
#endif

#if CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN
static void test_passive_hosted_load_follow_unload(void)
{
    km_device_profile_t configured;
    km_pin_config_t pins;
    km_switch_gpio_config_t switches;
    assert(CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN);
    assert(km_config_profile(&configured, NULL) == KM_OK);
    assert(configured.lane_count == 4u
           && configured.load_is_virtual_hub == (bool)CONFIG_KLIPPER_MMU_VIRTUAL_HUB
           && !configured.has_powered_rewind
           && configured.has_bay_sensors == (bool)CONFIG_KLIPPER_MMU_BAY_SENSORS
           && configured.has_load_sensors == (bool)CONFIG_KLIPPER_MMU_LOAD_SENSORS);
    profile = configured;
    prepare_drivers();
    for (unsigned lane = 0u; lane < profile.lane_count; ++lane)
        lane_ready[lane] = true;
    assert(km_config_pins(&pins, NULL) == KM_OK);
    assert(km_config_switches(&switches) == KM_OK);
    memset(input_levels, 1, sizeof(input_levels));
    passive_assert_pins(&pins);
#if CONFIG_KLIPPER_MMU_VIRTUAL_HUB
    assert(switches.hub.pin == KM_SWITCH_PIN_UNUSED);
#else
    assert(switches.hub.pin == pins.pin[KM_PIN_HUB][0]);
#endif
    assert(km_config_tmc(tmc) == KM_OK);
    for (unsigned lane = 0u; lane < profile.lane_count; ++lane) {
        assert(tmc[lane].tmc.address == 0u && tmc[lane].uart_pull_up);
        assert(tmc[lane].tmc.sense_resistor_milliohms == 110u);
        assert(tmc[lane].tmc.maximum_current_ma == 800u);
        assert(tmc[lane].tmc.run_current_ma == 700u);
        assert(tmc[lane].tmc.hold_current_ma == 150u);
    }
#if CONFIG_KLIPPER_MMU_BAY_SENSORS
    input_levels[pins.pin[KM_PIN_BAY][0]] = 0u;
#endif
#if CONFIG_KLIPPER_MMU_LOAD_SENSORS && !CONFIG_KLIPPER_MMU_VIRTUAL_HUB
    /* A physical-HUB device's per-lane LOAD/exit switch is incidental to
     * this startup step: pre-set it, unlike a virtual-hub device, where the
     * owning lane's LOAD switch IS the hub boundary and must start clear. */
    input_levels[pins.pin[KM_PIN_LOAD][0]] = 0u;
#endif

    clock_state = KM_CLOCK_STATE_READY;
    now_us = 1000u;
    test_diagnostic_start();
    for (unsigned attempt = 0u; attempt < 100u
         && km_diagnostic_get_state() == KM_DIAGNOSTIC_WAITING; ++attempt) {
        km_diagnostic_task();
        if (km_diagnostic_get_state() == KM_DIAGNOSTIC_WAITING && armed_timer)
            fire_timer();
    }
    assert(km_diagnostic_get_state() == KM_DIAGNOSTIC_READY);
    assert(km_diagnostic_get_result() == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);
    km_operation_core_t *core = km_board_startup_get_core();
    assert(core && core->profile.lane_count == 4u);
    assert(core->buffer_sensor.type == KM_BUFFER_SENSOR_SWITCHED);
    assert(reserve_calls == 1u && reserved_count == pins.count);
    for (size_t lane = 0u; lane < profile.lane_count; ++lane)
        assert(!feeders[lane].base.enabled
               && km_trajectory_get_position(&core->trajectory) == 0);

    emu_qualify(core);
#if CONFIG_KLIPPER_MMU_BAY_SENSORS
    assert(core->bay_valid[0] && core->filament_present_bay[0]);
#endif
#if CONFIG_KLIPPER_MMU_VIRTUAL_HUB
    for (size_t lane = 0u; lane < profile.lane_count; ++lane)
        assert(core->load_valid[lane] && !core->filament_present_load[lane]);
#else
    assert(core->hub_sensor_valid && !core->filament_present_hub);
#endif
    const uint32_t session = 23u;
    const uint64_t instance = test_boot_identity;
    km_fake_clock_t motion;
    km_fake_clock_init(&motion, now_us);
    issue_command_heartbeat(instance, session);
    issue_command_start(instance, session, 1u, KM_OP_TYPE_LOAD, 0u, KM_DIR_FORWARD);
    assert(last_send_values[7] == KM_REQ_ACCEPTED);
    assert(core->stage == KM_STAGE_LOAD_FEED_TO_HUB);
    /* The first step from rest takes (6/J)^(1/3), 33 ms at the default
     * J = 20 x 8000 steps/s^3. */
    emu_advance(core, &motion, 60u, session);
    assert(km_ref_position(&motion.ref) > 0);
    for (size_t lane = 1u; lane < profile.lane_count; ++lane)
        assert(!feeders[lane].base.enabled
               && core->trajectory.driver != &feeders[lane].base);

#if CONFIG_KLIPPER_MMU_VIRTUAL_HUB
    input_levels[pins.pin[KM_PIN_LOAD][0]] = 0u;
    emu_qualify(core);
    assert(core->load_valid[0] && core->filament_present_load[0]);
    assert(core->hub_sensor_valid && core->filament_present_hub);
#else
    input_levels[pins.pin[KM_PIN_HUB][0]] = 0u;
    emu_qualify(core);
    assert(core->hub_sensor_valid && core->filament_present_hub);
#endif
    emu_advance(core, &motion, 5u, session);
    assert(core->stage == KM_STAGE_LOAD_FEED_TO_BUFFER);
    input_levels[pins.pin[KM_PIN_TOOLHEAD][0]] = 0u;
    emu_advance(core, &motion, 7u, session);
    assert(core->stage == KM_STAGE_FOLLOW);
    assert(core->filament_present_toolhead && core->toolhead_sensor_valid);
    emu_ack_terminal(core, instance, session, 1u, KM_OP_TYPE_LOAD, 0u,
                     KM_EVENT_LOAD_COMPLETE);

    issue_command_start(instance, session, 2u, KM_OP_TYPE_FOLLOW, 0u, KM_DIR_FORWARD);
    assert(last_send_values[7] == KM_REQ_ACCEPTED);
    int64_t follow_steps = km_ref_position(&motion.ref);
    emu_advance(core, &motion, 20u, session);
    assert(core->stage == KM_STAGE_FOLLOW
           && km_ref_position(&motion.ref) > follow_steps);
    for (size_t lane = 1u; lane < profile.lane_count; ++lane)
        assert(!feeders[lane].base.enabled
               && core->trajectory.driver != &feeders[lane].base);

    issue_command_start(instance, session, 3u, KM_OP_TYPE_UNLOAD, 0u, KM_DIR_REVERSE);
    assert(last_send_values[7] == KM_REQ_ACCEPTED);
    assert(core->stage == KM_STAGE_UNLOAD_RETRACT_TO_HUB);
    emu_ack_terminal(core, instance, session, 2u, KM_OP_TYPE_FOLLOW, 0u, KM_EVENT_STOPPED);
#if CONFIG_KLIPPER_MMU_VIRTUAL_HUB
    input_levels[pins.pin[KM_PIN_LOAD][0]] = 1u;
#else
    input_levels[pins.pin[KM_PIN_HUB][0]] = 1u;
#endif
    input_levels[pins.pin[KM_PIN_TOOLHEAD][0]] = 1u;
    emu_advance_to_stopped(core, &motion, session);
    assert(core->stage == KM_STAGE_STOPPED && core->op_state == KM_OP_STATE_IDLE);
    emu_ack_terminal(core, instance, session, 3u, KM_OP_TYPE_UNLOAD, 0u,
                     KM_EVENT_UNLOAD_COMPLETE);
    assert(!core->path_occupied && !core->shared_path_owned);
    for (size_t lane = 0u; lane < profile.lane_count; ++lane) {
        assert(!feeders[lane].base.enabled);
        assert(lane == 0u || core->trajectory.driver != &feeders[lane].base);
    }
    puts("Passive-feeder exact-image hosted startup, physical HUB, LOAD/FOLLOW/UNLOAD and retained ACK passed");
}
#endif
#endif

#if defined(KM_TEST_F407_RUNTIME)
static void f407_prepare(km_device_profile_t *configured, km_pin_config_t *pins)
{
    assert(km_config_profile(configured, NULL) == KM_OK);
    assert(configured->lane_count == 1u && configured->has_load_sensors
           && !configured->has_powered_rewind && !configured->has_proportional_buffer);
    profile = *configured;
    prepare_drivers();
    lane_ready[0] = true;
    assert(km_config_pins(pins, NULL) == KM_OK);
    assert(pins->pin[KM_PIN_STEP][0] == pin_id('A', 1));
    assert(pins->pin[KM_PIN_DIR][0] == pin_id('A', 2));
    assert(pins->pin[KM_PIN_ENABLE][0] == pin_id('A', 3));
    assert(pins->pin[KM_PIN_UART][0] == pin_id('A', 4));
    assert(pins->pin[KM_PIN_BAY][0] == pin_id('A', 5));
    assert(pins->pin[KM_PIN_HUB][0] == pin_id('A', 6));
    assert(pins->pin[KM_PIN_TOOLHEAD][0] == pin_id('A', 7));
    assert(pins->pin[KM_PIN_BUFFER_A][0] == pin_id('B', 0));
    assert(pins->pin[KM_PIN_BUFFER_B][0] == pin_id('B', 1));
    assert(pins->pin[KM_PIN_LOAD][0] == pin_id('B', 2));
    memset(input_levels, 1, sizeof(input_levels));
}

#if !CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN
static void test_f407_safe_image_denies_start(void)
{
    km_device_profile_t configured;
    km_pin_config_t pins;
    f407_prepare(&configured, &pins);
    input_levels[pins.pin[KM_PIN_BAY][0]] = 0u;
    clock_state = KM_CLOCK_STATE_READY;
    now_us = 1000u;
    test_diagnostic_start();
    for (unsigned attempt = 0u; attempt < 100u
         && km_diagnostic_get_state() == KM_DIAGNOSTIC_WAITING; ++attempt) {
        km_diagnostic_task();
        if (km_diagnostic_get_state() == KM_DIAGNOSTIC_WAITING && armed_timer)
            fire_timer();
    }
    assert(km_diagnostic_get_state() == KM_DIAGNOSTIC_READY);
    assert(km_diagnostic_get_result() == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);
    assert(reserve_calls == 1u && reserved_count == pins.count);
    km_operation_core_t *core = km_board_startup_get_core();
    assert(core && core->profile.lane_count == 1u);
    emu_qualify(core);
    assert(core->filament_present_bay[0] && !core->filament_present_load[0]);
    /* Zero completed trajectory steps is the tick-path fact the inert mock
     * step_count used to stand for; the driver is still off. */
    assert(!feeders[0].base.enabled
           && km_trajectory_get_position(&core->trajectory) == 0);
    uint32_t start[] = {(uint32_t)(test_boot_identity >> 32),
        (uint32_t)test_boot_identity, KM_CLIENT_HOST, 23u, 1u,
        KM_OP_TYPE_LOAD, 0u, KM_DIR_FORWARD};
    command_km_start(start);
    assert(last_send_values[4] == KM_COMMAND_UNAVAILABLE);
    /* The denied START never bound or fed a driver, so the tick path completed
     * no steps and the only driver stayed off. */
    assert(!feeders[0].base.enabled
           && km_trajectory_get_position(&core->trajectory) == 0);
    puts("F4x5 exact-image diagnostic startup reached READY; START denied and output idle");
}
#else
static void test_f407_hosted_load_follow_unload(void)
{
    km_device_profile_t configured;
    km_pin_config_t pins;
    f407_prepare(&configured, &pins);
    input_levels[pins.pin[KM_PIN_BAY][0]] = 0u;
    clock_state = KM_CLOCK_STATE_READY;
    now_us = 1000u;
    test_diagnostic_start();
    for (unsigned attempt = 0u; attempt < 100u
         && km_diagnostic_get_state() == KM_DIAGNOSTIC_WAITING; ++attempt) {
        km_diagnostic_task();
        if (km_diagnostic_get_state() == KM_DIAGNOSTIC_WAITING && armed_timer)
            fire_timer();
    }
    assert(km_diagnostic_get_state() == KM_DIAGNOSTIC_READY);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);
    km_operation_core_t *core = km_board_startup_get_core();
    assert(core && core->profile.lane_count == 1u);
    emu_qualify(core);
    assert(core->filament_present_bay[0] && !core->filament_present_load[0]);
    input_levels[pins.pin[KM_PIN_LOAD][0]] = 0u;
    emu_qualify(core);
    assert(core->filament_present_load[0]);
    const uint32_t session = 23u;
    km_fake_clock_t motion;
    km_fake_clock_init(&motion, now_us);
    issue_command_heartbeat(test_boot_identity, session);
    issue_command_start(test_boot_identity, session, 1u, KM_OP_TYPE_LOAD, 0u,
                        KM_DIR_FORWARD);
    assert(last_send_values[7] == KM_REQ_ACCEPTED);
    /* The first step from rest takes (6/J)^(1/3), 33 ms at the default
     * J = 20 x 8000 steps/s^3. */
    emu_advance(core, &motion, 60u, session);
    /* The reference executor plays stepper.c on the tick path, so the feeder
     * really moved iff it completed steps: its position, not the mock's
     * set_step_edge counter. */
    assert(km_ref_position(&motion.ref) > 0);
    input_levels[pins.pin[KM_PIN_HUB][0]] = 0u;
    emu_sample(core);
    emu_advance(core, &motion, 5u, session);
    assert(core->stage == KM_STAGE_LOAD_FEED_TO_BUFFER);
    input_levels[pins.pin[KM_PIN_TOOLHEAD][0]] = 0u;
    emu_advance(core, &motion, 7u, session);
    assert(core->stage == KM_STAGE_FOLLOW);
    emu_ack_terminal(core, test_boot_identity, session, 1u, KM_OP_TYPE_LOAD, 0u,
                     KM_EVENT_LOAD_COMPLETE);
    issue_command_start(test_boot_identity, session, 2u, KM_OP_TYPE_FOLLOW, 0u,
                        KM_DIR_FORWARD);
    /* FOLLOW's progress is the tick path's completed-step count; the mock
     * edge counter no longer moves. */
    int64_t follow_steps = km_ref_position(&motion.ref);
    emu_advance(core, &motion, 20u, session);
    assert(core->stage == KM_STAGE_FOLLOW
           && km_ref_position(&motion.ref) > follow_steps);
    issue_command_start(test_boot_identity, session, 3u, KM_OP_TYPE_UNLOAD, 0u,
                        KM_DIR_REVERSE);
    emu_ack_terminal(core, test_boot_identity, session, 2u, KM_OP_TYPE_FOLLOW, 0u,
                     KM_EVENT_STOPPED);
    input_levels[pins.pin[KM_PIN_HUB][0]] = 1u;
    input_levels[pins.pin[KM_PIN_TOOLHEAD][0]] = 1u;
    emu_advance_to_stopped(core, &motion, session);
    assert(core->stage == KM_STAGE_STOPPED && core->op_state == KM_OP_STATE_IDLE);
    emu_ack_terminal(core, test_boot_identity, session, 3u, KM_OP_TYPE_UNLOAD, 0u,
                     KM_EVENT_UNLOAD_COMPLETE);
    assert(!core->path_occupied && !core->shared_path_owned);
    assert(!feeders[0].base.enabled);
    puts("F4x5 exact-image hosted LOAD/FOLLOW/UNLOAD and event ACK cycle passed");
}
#endif
#endif
#endif

int main(int argc, char **argv)
{
#if defined(KM_TEST_HOSTED_RUNTIME)
#if defined(KM_TEST_HOST_JOIN)
    assert(argc == 3 && strcmp(argv[1], "host_session") == 0);
    char *end = NULL;
    unsigned long lane = strtoul(argv[2], &end, 10);
    assert(end && *end == '\0' && lane < 4ul);
    host_session((uint8_t)lane);
    return 0;
#else
    (void)argc;
    (void)argv;
#if defined(KM_TEST_EMU_RUNTIME)
    test_emu_hosted_load_follow_unload();
#elif defined(KM_TEST_NIGHTOWL_RUNTIME)
#if CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN
    test_nightowl_hosted_load_follow_unload();
#else
    test_nightowl_safe_startup_denies_start();
#endif
#elif defined(KM_TEST_PASSIVE_RUNTIME)
#if CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN
    test_passive_hosted_load_follow_unload();
#else
    test_passive_safe_startup_denies_start();
#endif
#elif defined(KM_TEST_F407_RUNTIME)
#if CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN
    test_f407_hosted_load_follow_unload();
#else
    test_f407_safe_image_denies_start();
#endif
#endif
    return 0;
#endif
#else
#if CONFIG_KLIPPER_MMU_DIFF_BUFFER
#if CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN
    if (argc == 2 && strncmp(argv[1], "fps-", 4u) == 0) {
        test_fps_composed(argv[1]);
        return 0;
    }
    if (argc == 3 && strcmp(argv[1], "host_session") == 0) {
        char *end = NULL;
        unsigned long lane = strtoul(argv[2], &end, 10);
        assert(end && *end == '\0' && lane < 4ul);
        host_session((uint8_t)lane);
        return 0;
    }
#else
    (void)argc;
    (void)argv;
#endif
    assert(argc == 1);
    (void)legacy_main; /* Keep the switched-mode suite available in this TU. */
    test_diff_startup_sensor_pipeline();
#else
    (void)argv;
    assert(argc == 1);
    return legacy_main();
#endif
#endif
    return 0;
}
