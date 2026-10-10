// SPDX-License-Identifier: GPL-3.0-or-later
#include "sensors.h"
#include "switch_gpio.h"
#include "adc.h"
#include "controller.h"
#include "clock.h"
#include "shim_klipper.h"
#include "mock_driver.h"
#include "stub_stepper.h"
#include "board/irq.h"
#include "board/misc.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <sys/wait.h>

static struct timer *queued[16], *executing;
static unsigned queued_count;
extern void __real_sched_add_timer(struct timer *t);
extern void __real_sched_del_timer(struct timer *t);
void __wrap_sched_add_timer(struct timer *t)
{
    assert(test_shim_irq_is_disabled() && t != executing);
    assert(timer_is_before(test_shim_ticks, t->waketime));
    for (unsigned i=0; i<queued_count; ++i) assert(queued[i] != t);
    assert(queued_count < 16);
    queued[queued_count++] = t;
    __real_sched_add_timer(t);
}
void __wrap_sched_del_timer(struct timer *t)
{
    assert(test_shim_irq_is_disabled());
    assert(t != executing || test_shim_is_shutdown);
    for (unsigned i=0; i<queued_count; ++i)
        if (queued[i] == t) { queued[i] = queued[--queued_count]; break; }
    __real_sched_del_timer(t);
}
static void clear_queue(void) { queued_count=0; executing=NULL; }
DECL_SHUTDOWN(clear_queue);
static void dispatch(void);
static km_adc_pair_sample_t newer_pair(uint32_t attempt_us);
static void dispatch(void)
{
    struct timer *t=shim_find_earliest_timer();
    assert(t && !executing);
    if (timer_is_before(test_shim_ticks,t->waketime)) test_shim_ticks=t->waketime;
    irqstatus_t lock=irq_save();
    executing=t;
    uint_fast8_t result=t->func(t);
    executing=NULL;
    if (result==SF_DONE) __wrap_sched_del_timer(t);
    else assert(result==SF_RESCHEDULE && timer_is_before(test_shim_ticks,t->waketime));
    assert((unsigned)shim_active_timer_count()==queued_count);
    irq_restore(lock);
    shim_run_task(); /* The controller timer only wakes its task. */
}

static km_operation_core_t core;
static mock_driver_t feeder;
static bool levels[128];
static unsigned setups, reads;
static uint32_t read_cost, decode_cost;
static unsigned decodes, newer_pairs_during_decode;
struct gpio_in gpio_in_setup(uint32_t pin, int32_t pull)
{
    assert(!test_shim_irq_is_disabled() && pin<128 && pull>=-1 && pull<=1);
    ++setups;
    return (struct gpio_in){pin ^ 0x5a5a};
}
uint8_t gpio_in_read(struct gpio_in g)
{
    uint32_t pin=g.encoded ^ 0x5a5a;
    assert(!test_shim_irq_is_disabled() && pin<128 && !test_shim_is_shutdown); /* Task context. */
    ++reads;
    shim_advance_ticks(read_cost);
    return levels[pin] ? 1u : 0u;
}
extern km_result_t __real_km_buffer_update_adc_pair(km_buffer_sensor_t *,
    const km_adc_pair_sample_t *, uint32_t, uint32_t);
km_result_t __wrap_km_buffer_update_adc_pair(km_buffer_sensor_t *b,
    const km_adc_pair_sample_t *s, uint32_t ms, uint32_t us)
{
    assert(!test_shim_irq_is_disabled()); /* The decode no longer masks interrupts. */
    ++decodes;
    km_result_t result=__real_km_buffer_update_adc_pair(b,s,ms,us);
    shim_advance_ticks(decode_cost);
    if (newer_pairs_during_decode) {
        --newer_pairs_during_decode;
        (void)newer_pair(s->a_attempt_us); /* The ADC timer preempts the decode. */
    }
    return result;
}
static void fixture(bool analog, uint32_t hz)
{
    shim_reset(hz,1,1,8);
    shim_run_init();
    assert(shim_active_timer_count()==0 && !shim_adc_setup_a_called);
    shim_set_board_ready(true);
    shim_run_task();
    km_device_profile_t p;
    km_profile_init_nightowl(&p);
    p.lane_count=8;
    p.has_proportional_buffer=p.has_diff_calibration=analog;
    p.proportional_gain_milli=analog ? 8000u : 0u;
    if (analog) p.diff_calibration=(km_diff_adc_config_t){
        .vref_mv=3300,.adc_max_counts=4095,.path_tolerance_counts=32,.point_count=3,
        .points={{0,2048,0},{2048,2048,500},{2048,4095,1000}}};
    assert(km_core_init(&core,&p)==KM_OK);
    km_switch_gpio_config_t pins={0};
    for (unsigned i=0; i<8; ++i) {
        pins.bay[i]=(km_switch_input_t){(uint16_t)i,0,true};
        levels[i]=true;
    }
    pins.hub=(km_switch_input_t){100,0,true};
    pins.toolhead=(km_switch_input_t){101,0,true};
    pins.buffer_low=(km_switch_input_t){analog ? UINT16_MAX : 102,0,true};
    pins.buffer_high=(km_switch_input_t){analog ? UINT16_MAX : 103,0,true};
    assert(km_switch_gpio_setup(&core,&pins)==KM_OK);
    assert(setups==(analog ? 10u : 12u));
}
static void sample(void) { km_sensors_sample(&core,NULL); }
static void stable_samples(void)
{
    /* Exercise production qualification with timely samples, including read cost. */
    for (unsigned i=0; i<8; ++i) {
        shim_advance_ticks(timer_from_us(1000));
        sample();
    }
}
static void invalid(void)
{
    assert(!core.buffer_sensor.is_valid && core.buffer_sensor.fault==KM_FAULT_BUFFER_SIGNAL);
}
static void switched(uint32_t hz)
{
    fixture(false,hz);
    read_cost=timer_from_us(110);
    sample();
    assert(!core.buffer_sensor.is_valid && !core.bay_sensors_valid);
    stable_samples();
    reads=0;
    km_core_timepoint_t before;
    assert(km_clock_get_time(&before)==KM_OK);
    sample();
    assert(core.buffer_sensor.is_valid && core.buffer_sensor.position_permille==500);
    assert(core.buffer_sensor.last_sample_time_ms==before.ms && reads==12);
    for (unsigned i=0; i<8; ++i) assert(core.bay_valid[i] && core.filament_present_bay[i]);
    levels[102]=true;
    sample(); assert(core.buffer_sensor.position_permille==500);
    stable_samples(); assert(core.buffer_sensor.position_permille==250);
    levels[103]=true;
    sample(); assert(core.buffer_sensor.is_valid);
    stable_samples(); invalid();
    assert(!shim_adc_setup_a_called && !shim_adc_setup_b_called);
    assert(shim_adc_sample_count[0]==0 && shim_adc_sample_count[1]==0);
    assert(!test_shim_irq_is_disabled());
}
static const km_adc_pair_config_t adc_config={4095,500,1000,2000};
static void start_adc(void)
{
    shim_adc_raw_value[0]=1024;
    shim_adc_raw_value[1]=2048;
    shim_adc_delay_ticks[0]=shim_adc_delay_ticks[1]=timer_from_us(40);
    shim_adc_sample_return_ticks[0]=shim_adc_sample_return_ticks[1]=timer_from_us(10);
    assert(km_adc_start(36,37,&adc_config,100000u)==KM_OK);
}
static km_adc_pair_sample_t acquire(void)
{
    km_adc_pair_sample_t s={0};
    for (unsigned i=0; i<200; ++i) {
        dispatch();
        if (km_adc_get_sample(&s)==KM_OK && s.is_valid) return s;
    }
    assert(false);
    return s;
}
static km_adc_pair_sample_t newer_pair(uint32_t attempt_us)
{
    km_adc_pair_sample_t s={0};
    for (unsigned i=0; i<400; ++i) {
        dispatch();
        if (km_adc_get_sample(&s)==KM_OK && s.is_valid && s.a_attempt_us!=attempt_us) return s;
    }
    assert(false);
    return s;
}
/* A pair completed during a decode supersedes it and is decoded once instead;
 * one completing during that second decode too leaves feedback invalid. */
static void superseded_decode(void)
{
    fixture(true,1000000);
    start_adc();
    km_adc_pair_sample_t first=acquire();
    shim_adc_raw_value[0]=2048; /* The newer pair decodes to a different position. */
    newer_pairs_during_decode=1; decodes=0;
    sample();
    km_adc_pair_sample_t latest;
    assert(km_adc_get_sample(&latest)==KM_OK && latest.a_attempt_us!=first.a_attempt_us);
    assert(decodes==2 && core.buffer_sensor.is_valid && core.buffer_sensor.position_permille==500);
    assert(core.buffer_sensor.last_sample_time_ms==latest.a_attempt_us/1000u);
    newer_pairs_during_decode=2; decodes=0;
    sample(); invalid();
    assert(decodes==2);
}
static void unavailable(void)
{
    fixture(true,1000000);
    km_adc_pair_sample_t s={0};
    km_core_timepoint_t t={123,456};
    assert(km_adc_get_sample_at(NULL,&t)==KM_ERR_INVALID_PARAM);
    assert(km_adc_get_sample_at(&s,NULL)==KM_ERR_INVALID_PARAM);
    assert(km_adc_get_sample_at(&s,&t)==KM_ERR_UNAVAILABLE && t.ms==123 && t.us==456);
    sample(); invalid();
    assert(!core.bay_sensors_valid && !core.hub_sensor_valid);
    stable_samples(); invalid();
    assert(core.bay_sensors_valid && core.hub_sensor_valid);
    start_adc();
    assert(km_adc_get_sample_at(&s,&t)==KM_ERR_UNAVAILABLE && t.ms==123 && t.us==456);
    sample(); invalid();
}
static void valid_pair(uint32_t hz)
{
    fixture(true,hz);
    shim_advance_ticks(timer_from_us(950));
    start_adc();
    km_adc_pair_sample_t s=acquire();
    km_core_timepoint_t t;
    assert(km_adc_get_sample_at(&s,&t)==KM_OK && t.us-t.ms*1000u<1000);
    sample();
#if CONFIG_WANT_ADC
    assert(core.buffer_sensor.is_valid && core.buffer_sensor.position_permille==250);
    assert(core.buffer_sensor.last_sample_time_ms==s.a_attempt_us/1000u);
    uint32_t acquired_ms=core.buffer_sensor.last_sample_time_ms;
    int prior_reads=shim_adc_read_count[0]+shim_adc_read_count[1];
    shim_advance_ticks(timer_from_us(500));
    sample();
    assert(core.buffer_sensor.is_valid && core.buffer_sensor.last_sample_time_ms==acquired_ms);
    assert(shim_adc_read_count[0]+shim_adc_read_count[1]==prior_reads);
#else
    invalid(); /* Even an explicitly running ADC cannot bypass the build gate. */
#endif
    assert(reads>=10 && !shim_adc_read_before_ready_error && !shim_adc_b_started_before_a_consumed);
    assert(!shim_adc_irq_state_violations && !test_shim_irq_is_disabled());
}
static void age_boundary(bool during_decode)
{
    fixture(true,1000000);
    start_adc();
    km_adc_pair_sample_t s=acquire();
    shim_set_ticks(s.a_attempt_us+adc_config.max_sample_age_us);
    if (during_decode) decode_cost=1;
    sample();
    if (during_decode) invalid();
    else {
        assert(core.buffer_sensor.is_valid);
        shim_advance_ticks(1);
        sample(); invalid();
    }
    assert(core.buffer_sensor.last_sample_time_ms==s.a_attempt_us/1000u);
}
static void bad_reading(unsigned kind)
{
    fixture(true,1000000);
    start_adc();
    (void)acquire(); sample(); assert(core.buffer_sensor.is_valid);
    if (kind==0) {
        shim_adc_delay_ticks[1]=5000; /* Publish a partial timeout after the good pair. */
        km_adc_pair_sample_t s={0};
        for (unsigned i=0; i<300; ++i) {
            dispatch();
            if (km_adc_get_sample(&s)==KM_OK && s.result!=KM_OK) break;
        }
        assert(!s.is_valid && s.result!=KM_OK && s.present_mask==1);
    } else if (kind==1) {
        shim_adc_raw_value[0]=shim_adc_raw_value[1]=4000;
        int prior=shim_adc_read_count[1];
        for (unsigned i=0; i<200 && shim_adc_read_count[1]==prior; ++i) dispatch();
        assert(shim_adc_read_count[1]>prior);
    } else core.buffer_sensor.diff_config.adc_max_counts=4094;
    sample(); invalid();
}
static void normal_stop(void)
{
    fixture(true,1000000); start_adc(); (void)acquire(); sample();
    assert(core.buffer_sensor.is_valid);
    assert(km_adc_stop()==KM_OK); sample(); invalid();
    start_adc(); sample(); invalid();
    (void)acquire(); sample(); assert(core.buffer_sensor.is_valid);
}
static void shutdown_case(bool clock_fault)
{
    fixture(true,1000000); start_adc(); (void)acquire(); sample();
    assert(core.buffer_sensor.is_valid);
    test_shim_use_longjmp=true;
    if (setjmp(test_shim_shutdown_jump)==0) {
        if (clock_fault) { shim_advance_ticks(0x80000000u); sample(); }
        else shutdown("test emergency");
        assert(false);
    }
    test_shim_use_longjmp=false;
    invalid(); assert(!core.bay_sensors_valid && !core.hub_sensor_valid);
    unsigned prior=reads;
    sched_clear_shutdown();
    sample(); invalid();
    assert(reads==prior && km_adc_start(36,37,&adc_config,100000u)==KM_ERR_FAULTED);
}
static void fine_wrap(void)
{
    fixture(true,1000000);
    km_core_timepoint_t t;
    for (unsigned i=0; i<4; ++i) {
        shim_advance_ticks(1000000000); assert(km_clock_get_time(&t)==KM_OK);
    }
    shim_set_ticks(UINT32_MAX-20);
    assert(km_clock_get_time(&t)==KM_OK);
    start_adc();
    km_adc_pair_sample_t s=acquire();
    sample(); assert(core.buffer_sensor.is_valid);
    assert(s.a_attempt_us>UINT32_MAX-20 && test_shim_ticks<200);
    assert(core.buffer_sensor.last_sample_time_ms==UINT32_MAX/1000u);
    assert(km_clock_get_time(&t)==KM_OK);
    assert(t.ms==(uint32_t)((UINT64_C(4294967296)+test_shim_ticks)/1000u));
}
static void controller_follow(void)
{
    fixture(true,1000000);
    mock_driver_init(&feeder,2,10,false);
    assert(km_core_bind_lane_driver(&core,0,&feeder.base,NULL)==KM_OK);
    stable_samples();
    start_adc(); (void)acquire(); sample();
    assert(km_controller_attach(&core,km_sensors_sample,NULL)==KM_OK);
    /* Motion needs a completed control cycle (the lease). */
    for (unsigned i=0; i<8 && !core.control_clock_valid; ++i) dispatch();
    assert(core.control_clock_valid);
    km_controller_changed(); /* Task context: collect the edge output first. */
    km_core_timepoint_t now;
    assert(km_clock_get_time(&now)==KM_OK);
    km_core_host_comm_ready(&core,now.ms);
    km_request_identity_t req={.client=KM_CLIENT_HOST,.session_id=1,.req_id=101,
        .op_type=KM_OP_TYPE_LOAD,.lane_idx=0};
    assert(km_core_request_start(&core,&req,KM_DIR_FORWARD,now)==KM_REQ_ACCEPTED);
    km_controller_changed();
    for (unsigned i=0; i<20000 && core.stage!=KM_STAGE_FOLLOW; ++i) {
        if (core.trajectory.current_position_steps>=5) levels[100]=true;
        if (core.stage==KM_STAGE_LOAD_FEED_TO_BUFFER && core.trajectory.current_position_steps>=10)
            levels[101]=true;
        dispatch();
    }
    assert(core.stage==KM_STAGE_FOLLOW && core.op_state==KM_OP_STATE_FOLLOWING);
    assert(stub_stepper_steps(&feeder.base)>=10 && feeder.base.enabled);
    assert(km_adc_stop()==KM_OK);
    for (unsigned i=0; i<500 && feeder.base.enabled; ++i) dispatch();
    invalid();
    assert(core.op_state==KM_OP_STATE_FAULT && !feeder.base.enabled && !feeder.base.step_pin_high);
    /* The controller steps the stub local stepper, not the driver ops, so the
     * mock's set_step_edge is never called and pulse_width_violations measures
     * nothing here; the property that matters is the stub's own count of steps
     * that ran while the driver was disabled, which must stay zero. */
    assert(stub_stepper_disabled_steps(&feeder.base)==0);
}

static void switch_sample_delay(void)
{
    fixture(false, 1000000);
    read_cost = timer_from_us(5);
    uint32_t delay = UINT32_MAX;
    assert(!km_controller_get_switch_sample_delay(&delay) && delay == UINT32_MAX);
    assert(km_controller_attach(&core, km_sensors_sample, NULL) == KM_OK);
    for (unsigned i = 0; i < 30 && !km_controller_get_switch_sample_delay(&delay); ++i)
        dispatch();
    assert(km_controller_get_switch_sample_delay(&delay));
    assert(delay >= 12u * 5u && delay < 1000u);
    levels[102] = levels[103] = true;
    unsigned prior_reads = reads;
    for (unsigned i = 0; i < 40 && core.buffer_sensor.is_valid; ++i)
        dispatch();
    assert(reads > prior_reads);
    assert(!km_controller_get_switch_sample_delay(&delay));
    assert(delay >= 12u * 5u && delay < 1000u);
    uint32_t count = 0, max_us = 0;
    km_controller_get_switch_sample_stats(&count, &max_us);
    assert(count >= 1u && max_us >= delay);
}

static void switch_sample_delay_wrap(void)
{
    fixture(false, 1000000);
    km_core_timepoint_t now;
    for (unsigned i = 0; i < 4; ++i) {
        shim_advance_ticks(1000000000u);
        assert(km_clock_get_time(&now) == KM_OK);
    }
    shim_set_ticks(UINT32_MAX - 1030u);
    assert(km_clock_get_time(&now) == KM_OK);
    assert(now.us > UINT32_MAX - 2000u);
    read_cost = timer_from_us(5);
    assert(km_controller_attach(&core, km_sensors_sample, NULL) == KM_OK);
    uint32_t delay = UINT32_MAX;
    for (unsigned i = 0; i < 30 && !km_controller_get_switch_sample_delay(&delay); ++i)
        dispatch();
    assert(km_controller_get_switch_sample_delay(&delay));
    assert(delay >= 12u * 5u && delay < 1000u);
    uint32_t count = 0, max_us = 0;
    km_controller_get_switch_sample_stats(&count, &max_us);
    assert(count >= 1u && max_us >= delay);
}

typedef void (*test_fn)(void);
static void switched_1(void) { switched(1000000); }
static void switched_fractional(void) { switched(12288000); }
static void switched_520(void) { switched(520000000); }
static void paired_1(void) { valid_pair(1000000); }
static void paired_fractional(void)
{
    fixture(true, 12288000u);
    int timers = shim_active_timer_count();
    assert(timers >= 1);
    assert(km_adc_start(36, 37, &adc_config, 100000u) == KM_ERR_INVALID_PARAM);
    assert(!shim_adc_setup_a_called && !shim_adc_setup_b_called);
    assert(shim_active_timer_count() == timers);
    sample();
    invalid();
}
static void paired_520(void) { valid_pair(520000000); }
static void age(void) { age_boundary(false); }
static void decode_age(void) { age_boundary(true); }
static void partial(void) { bad_reading(0); }
static void off_path(void) { bad_reading(1); }
static void wrong_scale(void) { bad_reading(2); }
static void global_shutdown(void) { shutdown_case(false); }
static void clock_shutdown(void) { shutdown_case(true); }
int main(void)
{
    const test_fn tests[]={switched_1,switched_fractional,switched_520,unavailable,
        paired_1,paired_fractional,paired_520,age,decode_age,partial,off_path,
        wrong_scale,normal_stop,global_shutdown,clock_shutdown,fine_wrap,controller_follow,
        switch_sample_delay,switch_sample_delay_wrap,superseded_decode};
    unsigned count=CONFIG_WANT_ADC ? sizeof(tests)/sizeof(tests[0]) : 7u;
    for (unsigned i=0; i<count; ++i) {
        pid_t pid=fork(); assert(pid>=0);
        if (pid==0) { alarm(5); tests[i](); _exit(0); }
        int status=0; assert(waitpid(pid,&status,0)==pid);
        if (!WIFEXITED(status) || WEXITSTATUS(status)) {
            fprintf(stderr,"FAIL sensor composition case %u (ADC=%d), status %d\n",i,CONFIG_WANT_ADC,status);
            return 1;
        }
        printf("PASS sensor composition case %u (ADC=%d)\n",i,CONFIG_WANT_ADC);
    }
    return 0;
}
