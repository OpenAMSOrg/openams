// SPDX-License-Identifier: GPL-3.0-or-later
/* Process-isolated integration tests: actual portable bridge plus actual binding,
 * with a narrow model of the already-tested shared pair API. It models confirmed
 * compare counts, not physical output timing. */
#include "pwm_bridge_gpio.h"
#include "clock.h"
#include "h723_resources.h"
#include "pin_config.h"
#include "rewind_config.h"
#include "stm32/pwm.h"
#include "board/gpio.h"
#include "board/irq.h"
#ifdef KM_TEST_CONFIGURED_ADMISSION
#include "stm32/gpio_local.h"
uint8_t km_test_local_pins[5];
#endif
#include <assert.h>
#include <setjmp.h>
#include <stdio.h>
#include <string.h>

struct gpio_pwm_pair { unsigned live; } pair_handle;
static struct {
    unsigned setup, update, raw, gpio_setup, gpio_write, clock_calls;
    unsigned irq_disabled, sched_shutdown, pair_fail, info_fail, clock_fail, clock_fail_after, quantized_zero, bad_pin, bad_cycle;
    bool enable; uint32_t fwd, rev, now; km_fault_t fault; bool ready;
    km_result_t plan_result, pins_result, resources_result, limits_result, outputs_result;
} m;

static km_resource_claim_t expected_claims[4];
static size_t expected_count;
static jmp_buf stopped;
static const char *stop_reason;

irqstatus_t irq_save(void) { irqstatus_t old=m.irq_disabled; m.irq_disabled=1; return old; }
void irq_restore(irqstatus_t state) { m.irq_disabled=(unsigned)state; }
uint8_t sched_is_shutdown(void) { return (uint8_t)m.sched_shutdown; }
_Noreturn void shutdown(const char *message) { stop_reason=message; longjmp(stopped,1); }
struct gpio_out gpio_out_setup(uint32_t pin, uint32_t value) { m.gpio_setup++; m.enable=value; return (struct gpio_out){pin}; }
void gpio_out_write(struct gpio_out out, uint32_t value) { (void)out; m.gpio_write++; m.enable=value; }

enum km_clock_state km_clock_get_state(void) { return m.clock_fail ? KM_CLOCK_STATE_FAULTED : KM_CLOCK_STATE_READY; }
km_result_t km_clock_get_completion_us(uint32_t *out) {
    m.clock_calls++; if (!out) return KM_ERR_INVALID_PARAM;
    if (m.clock_fail || (m.clock_fail_after && --m.clock_fail_after == 0u)) return KM_ERR_FAULTED;
    *out=m.now; return KM_OK;
}

km_result_t km_resource_plan_validate(const km_resource_claim_t *claims, size_t count, km_resource_diagnostic_t *d) {
    (void)claims; (void)count; if (d) *d=(km_resource_diagnostic_t){0}; return m.plan_result;
}
static km_pin_config_t configured_pins;
static km_result_t configure_pins(void) {
    if (m.pins_result != KM_OK) return m.pins_result;
    memset(&configured_pins, 0xff, sizeof(configured_pins)); configured_pins.count=3;
    configured_pins.pin[KM_PIN_REWIND_FORWARD][0]=0; configured_pins.pin[KM_PIN_REWIND_REVERSE][0]=2;
    configured_pins.pin[KM_PIN_REWIND_ENABLE][0]=4;
    if (m.bad_pin) configured_pins.pin[KM_PIN_REWIND_FORWARD][0]=128;
    return KM_OK;
}
km_result_t km_config_pins_view(const km_pin_config_t **out, km_resource_diagnostic_t *d) {
    if (d) *d=(km_resource_diagnostic_t){0};
    if (!out) return KM_ERR_INVALID_PARAM;
    km_result_t result = configure_pins();
    if (result == KM_OK) *out = &configured_pins;
    return result;
}
km_result_t km_config_pins(km_pin_config_t *out, km_resource_diagnostic_t *d) {
    if (d) *d=(km_resource_diagnostic_t){0};
    if (!out) return KM_ERR_INVALID_PARAM;
    const km_pin_config_t *view;
    km_result_t result = km_config_pins_view(&view, NULL);
    if (result == KM_OK) *out = *view;
    return result;
}
#ifndef USE_REAL_H723_RESOURCES
km_result_t km_config_h723_resources(km_h723_resource_plan_t *out, km_resource_diagnostic_t *d) {
    if (d) *d=(km_resource_diagnostic_t){0};
    if (m.resources_result != KM_OK) return m.resources_result;
    memset(out, 0, sizeof(*out)); memcpy(out->claims, expected_claims, sizeof(expected_claims)); out->count=expected_count;
    out->rewind[0][0]=(km_h723_pwm_route_t){2,1}; out->rewind[0][1]=(km_h723_pwm_route_t){2,3}; return KM_OK;
}
#endif
km_result_t km_config_rewind(km_pwm_bridge_config_t *out) {
    if (m.limits_result != KM_OK) return m.limits_result;
    *out=(km_pwm_bridge_config_t){1000,10,100}; return KM_OK;
}
km_result_t km_config_rewind_outputs(km_rewind_output_config_t out[KM_MAX_LANES]) {
    if (m.outputs_result != KM_OK) return m.outputs_result;
    memset(out, 0, sizeof(km_rewind_output_config_t)*KM_MAX_LANES);
    out[0]=(km_rewind_output_config_t){m.bad_cycle ? 0u : 100u, true, true, true}; return KM_OK;
}

int gpio_pwm_pair_setup(const struct gpio_pwm_pair_config *cfg, struct gpio_pwm_pair **out) {
    assert(cfg && out && cfg->raw_force_off); m.setup++; cfg->raw_force_off(cfg->raw_force_off_context);
    if (m.pair_fail) return GPIO_PWM_PAIR_ERR_HARDWARE;
    pair_handle.live=1; *out=&pair_handle; return GPIO_PWM_PAIR_OK;
}
int gpio_pwm_pair_update(struct gpio_pwm_pair *pair, uint32_t fwd, uint32_t rev) {
    assert(pair==&pair_handle && !(fwd && rev)); m.update++; if (m.pair_fail) return GPIO_PWM_PAIR_ERR_HARDWARE;
    m.fwd=fwd; m.rev=rev; return GPIO_PWM_PAIR_OK;
}
void gpio_pwm_pair_raw_disable(struct gpio_pwm_pair *pair) { assert(pair==&pair_handle); m.raw++; pair_handle.live=0; }
int gpio_pwm_pair_is_ready(const struct gpio_pwm_pair *pair) { return pair==&pair_handle && pair_handle.live; }
int gpio_pwm_pair_get_info(const struct gpio_pwm_pair *pair, struct gpio_pwm_pair_info *out) {
    if (m.info_fail || pair!=&pair_handle || !out) return GPIO_PWM_PAIR_ERR_HARDWARE;
    *out=(struct gpio_pwm_pair_info){.confirmed_forward_counts=m.quantized_zero ? 0u : m.fwd,
        .confirmed_reverse_counts=m.quantized_zero ? 0u : m.rev,.is_ready=(uint8_t)pair_handle.live}; return GPIO_PWM_PAIR_OK;
}
void gpio_pwm_pair_shutdown(void) { pair_handle.live=0; }

static bool ready(void *unused) { (void)unused; return m.ready; }
static km_fault_t fault(void *unused) { (void)unused; return m.fault; }
static void reset(void) {
    memset(&m,0,sizeof(m)); m.ready=true; m.plan_result=m.pins_result=m.resources_result=m.limits_result=m.outputs_result=KM_OK;
    expected_claims[0]=(km_resource_claim_t){.kind=KM_RESOURCE_PIN,.instance=1,.owner=1}; expected_count=1; pair_handle.live=0; stop_reason=NULL;
}
static km_driver_t *setup(void) { assert(km_pwm_bridge_gpio_setup(0,expected_claims,expected_count,ready,fault,NULL)==KM_OK); km_driver_t *d=km_pwm_bridge_gpio_get_driver(0); assert(d); return d; }

static void normal(void) {
    km_driver_t *d=setup(); assert(!m.enable && !m.raw);
    m.now=10; assert(km_driver_set_enable(d,true)==KM_OK && m.enable);
    m.now=20; assert(km_driver_set_direction(d,KM_DIR_FORWARD,m.now)==KM_OK);
    m.now=30; assert(km_driver_set_pwm_duty(d,500)==KM_OK && m.fwd==16384 && !m.rev);
    m.now=40; assert(km_driver_set_pwm_duty(d,0)==KM_OK && !m.fwd && !m.rev);
    m.now=60; assert(km_driver_set_direction(d,KM_DIR_REVERSE,m.now)==KM_OK);
    m.now=70; assert(km_driver_set_pwm_duty(d,500)==KM_OK && !m.fwd && m.rev==16384);
    assert(km_driver_set_enable(d,false)==KM_OK && !m.enable && !m.raw);
    m.now=90; assert(km_driver_set_enable(d,true)==KM_OK && m.enable && !m.raw);
}
static void failure(void) {
    km_driver_t *d=setup(); m.now=1; assert(km_driver_set_enable(d,true)==KM_OK); m.now=2;
    m.pair_fail=1; assert(km_driver_set_pwm_duty(d,500)==KM_ERR_FAULTED); assert(!m.enable && m.raw && !km_pwm_bridge_gpio_is_slot_active(0));
}
static void clock_failure(void) {
    km_driver_t *d=setup(); m.now=1; assert(km_driver_set_enable(d,true)==KM_OK); m.clock_fail=1;
    if (!setjmp(stopped)) {
        (void)km_driver_set_pwm_duty(d,500);
        assert(!"clock callback returned after an unavailable completion observation");
    }
    assert(stop_reason && !m.enable && m.raw && !pair_handle.live);
}
static void clear_fault_clock_failure(void) {
    km_driver_t *d=setup(); m.now=1; assert(km_driver_set_enable(d,true)==KM_OK);
    m.fault=KM_FAULT_DRIVER_FAULT; km_pwm_bridge_gpio_service(0);
    assert(!m.enable && !m.raw && km_driver_get_fault(d)==KM_FAULT_DRIVER_FAULT);
    m.fault=KM_FAULT_NONE;
    /* The two zero writes observe completion successfully; io_now_us fails last. */
    m.clock_fail_after=3;
    if (!setjmp(stopped)) {
        (void)km_driver_clear_fault(d);
        assert(!"clear_fault returned after its final completion observation failed");
    }
    assert(stop_reason && !m.enable && m.raw && !pair_handle.live);
}
static void subcount(void) {
    km_driver_t *d=setup(); m.now=1; assert(km_driver_set_enable(d,true)==KM_OK);
    m.now=2; assert(km_driver_set_direction(d,KM_DIR_FORWARD,m.now)==KM_OK);
    m.quantized_zero=1; m.now=3;
    assert(km_driver_set_pwm_duty(d,1)==KM_OK && m.fwd==32 && !m.rev && m.enable);
}
static void invalid(void) {
    assert(km_pwm_bridge_gpio_setup(0,NULL,1,ready,fault,NULL)==KM_ERR_INVALID_PARAM && !m.gpio_setup && !m.setup);
    assert(km_pwm_bridge_gpio_setup(0,expected_claims,0,ready,fault,NULL)==KM_ERR_INVALID_PARAM && !m.gpio_setup);
    assert(km_pwm_bridge_gpio_setup(0,expected_claims,expected_count,NULL,fault,NULL)==KM_ERR_INVALID_PARAM && !m.gpio_setup);
#if !defined(CONFIG_WANT_GPIO_LOCAL) || CONFIG_WANT_GPIO_LOCAL == 0
    assert(km_pwm_bridge_gpio_setup(0,NULL,0,ready,fault,NULL)==KM_ERR_INVALID_PARAM && !m.gpio_setup && !m.setup);
#endif
    km_resource_claim_t other=(km_resource_claim_t){.kind=KM_RESOURCE_PIN,.instance=2,.owner=1};
    assert(km_pwm_bridge_gpio_setup(0,&other,1,ready,fault,NULL)==KM_ERR_CONFLICT && !m.gpio_setup);
}
static void malformed(void) {
    m.bad_pin=1;
    assert(km_pwm_bridge_gpio_setup(0,expected_claims,expected_count,ready,fault,NULL)==KM_ERR_INVALID_PARAM);
    assert(!m.gpio_setup && !m.setup);
    reset(); m.bad_cycle=1;
    assert(km_pwm_bridge_gpio_setup(0,expected_claims,expected_count,ready,fault,NULL)==KM_ERR_INVALID_PARAM);
    assert(!m.gpio_setup && !m.setup);
}
static void setup_failure(void) {
    m.pair_fail=1; assert(km_pwm_bridge_gpio_setup(0,expected_claims,expected_count,ready,fault,NULL)==KM_ERR_FAULTED);
    assert(m.gpio_setup && !m.enable && !km_pwm_bridge_gpio_get_driver(0));
}
static void irq(void) { m.irq_disabled=1; assert(km_pwm_bridge_gpio_setup(0,expected_claims,expected_count,ready,fault,NULL)==KM_ERR_INVALID_PARAM && !m.gpio_setup); }
static void capacity(void) {
#ifdef EXPECT_CAPACITY_REJECT
    assert(km_pwm_bridge_gpio_setup(0,expected_claims,expected_count,ready,fault,NULL)==KM_ERR_NOT_SUPPORTED);
    assert(!m.gpio_setup && !m.setup);
#else
    assert(!"capacity scenario must be compiled with an insufficient shared-pair capacity");
#endif
}
static void query_unavailable(void) {
#ifdef USE_REAL_H723_RESOURCES
    assert(km_pwm_bridge_gpio_setup(0,expected_claims,expected_count,ready,fault,NULL)==KM_ERR_UNAVAILABLE);
    assert(!m.gpio_setup && !m.setup);
#else
    assert(!"query-unavailable case must link the real H723 resource planner");
#endif
}
static void configured_admission(void) {
#ifdef KM_TEST_CONFIGURED_ADMISSION
    /* Modeled ownership boundary for saved pins 0, 2, and 4. Not the shared registry. */
    unsigned gpio_setup, setup_count, gpio_write, update, raw;
    assert(km_pwm_bridge_gpio_setup(0,NULL,1,ready,NULL,NULL)==KM_ERR_INVALID_PARAM);
    assert(km_pwm_bridge_gpio_setup(0,expected_claims,0,ready,NULL,NULL)==KM_ERR_INVALID_PARAM);
    assert(km_pwm_bridge_gpio_setup(0,NULL,0,NULL,NULL,NULL)==KM_ERR_INVALID_PARAM);
    assert(!m.gpio_setup && !m.gpio_write && !m.setup && !m.update && !m.raw);
    assert(!km_pwm_bridge_gpio_get_driver(0) && !km_pwm_bridge_gpio_is_slot_active(0));

    memset(km_test_local_pins, 0, sizeof(km_test_local_pins));
    gpio_setup=m.gpio_setup; setup_count=m.setup; gpio_write=m.gpio_write; update=m.update; raw=m.raw;
    assert(km_pwm_bridge_gpio_setup(0,NULL,0,ready,NULL,NULL)==KM_ERR_CONFLICT);
    assert(m.gpio_setup==gpio_setup && m.setup==setup_count && m.gpio_write==gpio_write && m.update==update && m.raw==raw);
    assert(!km_pwm_bridge_gpio_get_driver(0) && !km_pwm_bridge_gpio_is_slot_active(0));

    (void)gpio_out_setup(0, 0);
    gpio_setup=m.gpio_setup; setup_count=m.setup; gpio_write=m.gpio_write; update=m.update; raw=m.raw;
    assert(km_pwm_bridge_gpio_setup(0,NULL,0,ready,NULL,NULL)==KM_ERR_CONFLICT);
    assert(m.gpio_setup==gpio_setup && m.setup==setup_count && m.gpio_write==gpio_write && m.update==update && m.raw==raw);
    assert(!km_pwm_bridge_gpio_get_driver(0) && !km_pwm_bridge_gpio_is_slot_active(0));

    km_test_local_pins[0]=1u; km_test_local_pins[2]=1u;
    gpio_setup=m.gpio_setup; setup_count=m.setup; gpio_write=m.gpio_write; update=m.update; raw=m.raw;
    assert(km_pwm_bridge_gpio_setup(0,NULL,0,ready,NULL,NULL)==KM_ERR_CONFLICT);
    assert(m.gpio_setup==gpio_setup && m.setup==setup_count && m.gpio_write==gpio_write && m.update==update && m.raw==raw);
    assert(!km_pwm_bridge_gpio_get_driver(0) && !km_pwm_bridge_gpio_is_slot_active(0));

    km_test_local_pins[0]=1u; km_test_local_pins[2]=1u; km_test_local_pins[4]=1u;
    assert(km_pwm_bridge_gpio_setup(0,NULL,0,ready,NULL,NULL)==KM_OK);
    assert(!m.enable && km_pwm_bridge_gpio_get_driver(0));
    gpio_setup=m.gpio_setup; setup_count=m.setup;
    assert(km_pwm_bridge_gpio_setup(0,NULL,0,ready,NULL,NULL)==KM_ERR_BUSY);
    assert(!m.enable && m.gpio_setup==gpio_setup && m.setup==setup_count);
    assert(km_pwm_bridge_gpio_get_driver(0) && km_pwm_bridge_gpio_is_slot_active(0));
#else
    assert(!"configured admission requires KM_TEST_CONFIGURED_ADMISSION");
#endif
}
static void planner_failure(void) {
    const km_result_t failures[] = {
        KM_ERR_INVALID_PARAM, KM_ERR_NOT_SUPPORTED, KM_ERR_CONFLICT,
        KM_ERR_UNAVAILABLE, KM_ERR_FAULTED
    };
    for (unsigned i = 0; i < sizeof failures / sizeof failures[0]; ++i) {
        m.resources_result = failures[i];
        assert(km_pwm_bridge_gpio_setup(0, expected_claims, expected_count, ready, fault, NULL)
               == failures[i]);
        assert(!m.gpio_setup && !m.gpio_write && !m.setup && !m.update && !m.raw);
        assert(!km_pwm_bridge_gpio_get_driver(0) && !km_pwm_bridge_gpio_is_slot_active(0));
        uint32_t forward, reverse, enable;
        assert(km_pwm_bridge_gpio_get_slot_pins(0, &forward, &reverse, &enable)
               == KM_ERR_UNAVAILABLE);
    }
    m.resources_result = KM_OK;
    assert(km_pwm_bridge_gpio_setup(0, expected_claims, expected_count, ready, fault, NULL)
           == KM_OK);
    assert(km_pwm_bridge_gpio_is_slot_active(0) && m.gpio_setup && m.setup);
}
int main(int argc,char **argv) { assert(argc==2); reset();
    if (!strcmp(argv[1],"normal")) normal(); else if (!strcmp(argv[1],"failure")) failure();
    else if (!strcmp(argv[1],"clock")) clock_failure(); else if (!strcmp(argv[1],"subcount")) subcount(); else if (!strcmp(argv[1],"invalid")) invalid();
    else if (!strcmp(argv[1],"setupfail")) setup_failure(); else if (!strcmp(argv[1],"irq")) irq();
    else if (!strcmp(argv[1],"clearclock")) clear_fault_clock_failure(); else if (!strcmp(argv[1],"capacity")) capacity();
    else if (!strcmp(argv[1],"malformed")) malformed(); else if (!strcmp(argv[1],"planner")) planner_failure();
    else if (!strcmp(argv[1],"queryoff")) query_unavailable();
    else if (!strcmp(argv[1],"configured")) configured_admission(); else return 2;
    puts("pass"); return 0; }
