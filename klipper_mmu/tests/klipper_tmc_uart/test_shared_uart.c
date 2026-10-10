// SPDX-License-Identifier: GPL-3.0-or-later
/* Public binding probes; links the actual upstream bit engine and virtual chip. */
#include "tmc_uart_gpio.h"
#include "klipper_mmu/tmc_uart_config.h"
#include "clock.h"
#include "shim_tmc.h"
#include "board/irq.h"
#include "board/misc.h"
#include "basecmd.h"
#include "tmcuart.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct { bool disabled; unsigned calls; uint32_t cost, ended; } actuator_state_t;
static actuator_state_t actuators[KM_MAX_LANES];
#define actuator actuators[0]
static bool fail_cancel;
static unsigned clock_reads;
static uint32_t clock_read_cost, task_interval, last_task_tick;
uint32_t __real_timer_read_time(void);
uint32_t __wrap_timer_read_time(void)
{
    ++clock_reads;
    uint32_t tick=__real_timer_read_time(); shim_advance_ticks(clock_read_cost); return tick;
}
int __real_tmcuart_local_cancel(struct tmcuart_local *channel);
int __wrap_tmcuart_local_cancel(struct tmcuart_local *channel)
{
    if (fail_cancel) { fail_cancel=false; return TMCUART_ERR_SHUTDOWN; }
    return __real_tmcuart_local_cancel(channel);
}
void *oid_alloc(uint8_t oid, oid_type type, size_t size)
{ (void)oid; (void)type; (void)size; abort(); }
void *oid_lookup(uint8_t oid, oid_type type)
{ (void)oid; (void)type; abort(); }
void *review_oid(uint8_t oid, oid_type type)
{ (void)oid; (void)type; return NULL; }
void *command_decode_ptr(uint32_t value) { return (void *)(uintptr_t)value; }

static void force_off(void *context)
{
    actuator_state_t *state=NULL;
    for (unsigned i=0;i<KM_MAX_LANES;++i)
        if (context==&actuators[i]) state=&actuators[i];
    assert(state);
    state->disabled=true; ++state->calls;
    shim_advance_ticks(state->cost);
    state->ended=test_shim_ticks; /* Test observation; callback never reads a clock. */
}
static km_tmc2209_config_t config(uint8_t address)
{
    return (km_tmc2209_config_t){
        .address=address, .sense_resistor_milliohms=110, .maximum_current_ma=1500,
        .run_current_ma=500, .hold_current_ma=200, .microsteps=16,
        .interpolate=true, .stealthchop=true, .transaction_timeout_us=10000,
        .diagnostic_interval_us=100000,
    };
}
static void dispatch(bool tasks)
{
    assert(shim_find_earliest_timer());
    irqstatus_t saved=irq_save();
    shim_dispatch_earliest_timer();
    irq_restore(saved);
    if (tasks && (test_shim_ticks-last_task_tick)>=task_interval) {
        last_task_tick=test_shim_ticks; shim_run_task();
    }
}
static void ready(void)
{
    for (unsigned i=0;i<20000;++i) {
        if (km_tmc_uart_gpio_is_ready(0)) return;
        if (km_tmc_uart_gpio_get_fault(0)!=KM_FAULT_NONE) {
            fprintf(stderr,"fault before ready: %u, frames=%u\n",
                    km_tmc_uart_gpio_get_fault(0),shim_get_virtual_tmc(10)->valid_frames);
            abort();
        }
        dispatch(true);
    }
    assert(!"readiness event bound exhausted");
}
static void run_us(uint32_t duration, bool tasks)
{
    uint32_t begin=test_shim_ticks, ticks=timer_from_us(duration);
    uint32_t last_delta=0;
    for (unsigned i=0;(test_shim_ticks-begin)<ticks;++i) {
        if (i==30000) {
            fprintf(stderr,"event bound: elapsed_ticks=%u last_delta=%u frames=%u tasks=%d\n",
                    test_shim_ticks-begin,last_delta,shim_get_virtual_tmc(10)->valid_frames,tasks);
            abort();
        }
        uint32_t before=test_shim_ticks;
        dispatch(tasks); last_delta=test_shim_ticks-before;
    }
}
static void expect_fault(void)
{
    assert(actuator.disabled && !km_tmc_uart_gpio_is_ready(0));
    assert(km_tmc_uart_gpio_get_fault(0)!=KM_FAULT_NONE);
    km_tmc_uart_status_t status;
    assert(km_tmc_uart_gpio_get_status(0,&status)==KM_OK);
    assert(!status.ready && status.fault!=KM_FAULT_NONE);
}

int main(int argc, char **argv)
{
    assert(argc==4);
    uint32_t clock=(uint32_t)strtoul(argv[1],NULL,10);
    uint8_t address=(uint8_t)strtoul(argv[2],NULL,10);
    const char *scenario=argv[3];
    shim_reset(clock);
    km_tmc2209_config_t cfg=config(address);
    if (!strcmp(scenario,"pre_ready")) {
        assert(km_tmc_uart_gpio_setup(0,10,1,&cfg,force_off,&actuator)==KM_ERR_UNAVAILABLE);
        assert(!km_tmc_uart_gpio_is_slot_active(0) && !shim_active_timer_count());
        return 0;
    }
    if (!strcmp(scenario,"wrap")) shim_set_ticks(UINT32_MAX-timer_from_us(2000));
    shim_run_init(); shim_run_task();
    assert(km_clock_get_state()==KM_CLOCK_STATE_READY);
    if (!strcmp(scenario,"delayed_tasks")) {
        clock_read_cost=3; task_interval=timer_from_us(300); last_task_tick=test_shim_ticks;
    }
    shim_attach_virtual_tmc(10,address,clock);
    virtual_tmc_t *chip=shim_get_virtual_tmc(10);
    if (!strcmp(scenario,"crc")) chip->corrupt_reply_crc=true;
    if (!strcmp(scenario,"silent")) chip->no_reply=true;
    /* One attempt short of the bound, during startup: absorbed, not latched. */
    if (!strcmp(scenario,"crc_transient")) chip->corrupt_reply_count=KM_TMC2209_MAX_ATTEMPTS-1u;
    if (!strcmp(scenario,"silent_transient")) chip->no_reply_count=KM_TMC2209_MAX_ATTEMPTS-1u;
    if (!strcmp(scenario,"setup_irq")) {
        irq_disable();
        assert(km_tmc_uart_gpio_setup(0,10,1,&cfg,force_off,&actuator)==KM_ERR_INVALID_PARAM);
        assert(test_shim_irq_is_disabled() && !km_tmc_uart_gpio_is_slot_active(0));
        irq_enable(); return 0;
    }
    if (!strcmp(scenario,"initial_guard")) actuator.cost=timer_from_us(7);
    assert(km_tmc_uart_gpio_setup(0,10,1,&cfg,force_off,&actuator)==KM_OK);
    assert(!km_tmc_uart_gpio_is_ready(0) && actuator.disabled);
    if (!strcmp(scenario,"multislot")) {
        for (unsigned i=1;i<KM_MAX_LANES;++i) {
            km_tmc2209_config_t other=config((uint8_t)(i%4));
            shim_attach_virtual_tmc(10+i,(uint8_t)(i%4),clock);
            assert(km_tmc_uart_gpio_setup(i,10+i,1,&other,force_off,&actuators[i])==KM_OK);
        }
        for (unsigned event=0;;++event) {
            assert(event<100000);
            bool all_ready=true;
            for (unsigned i=0;i<KM_MAX_LANES;++i) {
                assert(km_tmc_uart_gpio_get_fault(i)==KM_FAULT_NONE);
                all_ready=all_ready && km_tmc_uart_gpio_is_ready(i);
            }
            if (all_ready) break;
            dispatch(true);
        }
        for (unsigned i=0;i<KM_MAX_LANES;++i) actuators[i].disabled=false;
        assert(km_tmc_uart_gpio_set_current(1,700,250)==KM_OK);
        assert(actuators[1].disabled);
        for (unsigned event=0;!km_tmc_uart_gpio_is_ready(1);++event) {
            assert(event<10000); dispatch(true);
        }
        for (unsigned i=0;i<KM_MAX_LANES;++i) {
            km_tmc_uart_status_t status;
            assert(km_tmc_uart_gpio_get_status(i,&status)==KM_OK);
            assert(status.ready && status.confirmed_run_ma==(i==1 ? 700 : 500));
            if (i!=1) assert(!actuators[i].disabled);
            actuators[i].disabled=false;
        }
        shim_get_virtual_tmc(12)->registers[0x6f]=4;
        run_us(140000,true);
        for (unsigned i=0;i<KM_MAX_LANES;++i) {
            assert(actuators[i].disabled==(i==2));
            assert(km_tmc_uart_gpio_is_ready(i)==(i!=2));
            assert((km_tmc_uart_gpio_get_fault(i)!=KM_FAULT_NONE)==(i==2));
            if (i==2) assert(km_tmc_uart_gpio_get_fault(i)==KM_FAULT_DRIVER_OVERCURRENT);
        }
        /* An explicit restart recovers only that slot once its chip is clear;
         * the same protection flag then latches again. */
        unsigned calls[KM_MAX_LANES];
        for (unsigned i=0;i<KM_MAX_LANES;++i) { calls[i]=actuators[i].calls; actuators[i].disabled=false; }
        shim_get_virtual_tmc(12)->registers[0x6f]=0;
        assert(km_tmc_uart_gpio_restart(2)==KM_OK && !km_tmc_uart_gpio_is_ready(2));
        for (unsigned event=0;!km_tmc_uart_gpio_is_ready(2);++event) {
            assert(event<10000 && km_tmc_uart_gpio_get_fault(2)==KM_FAULT_NONE); dispatch(true);
        }
        run_us(140000,true);
        for (unsigned i=0;i<KM_MAX_LANES;++i) {
            assert(km_tmc_uart_gpio_is_ready(i) && km_tmc_uart_gpio_get_fault(i)==KM_FAULT_NONE);
            assert(i==2 ? actuators[i].calls>calls[i] : actuators[i].calls==calls[i]);
            assert(actuators[i].disabled==(i==2));
        }
        shim_get_virtual_tmc(12)->registers[0x6f]=4;
        run_us(140000,true);
        for (unsigned i=0;i<KM_MAX_LANES;++i)
            assert((km_tmc_uart_gpio_get_fault(i)!=KM_FAULT_NONE)==(i==2) && km_tmc_uart_gpio_is_ready(i)==(i!=2));
        assert(km_tmc_uart_gpio_get_fault(2)==KM_FAULT_DRIVER_OVERCURRENT);
        printf("PASS eight independent slots at %u Hz\n",clock);
        return 0;
    }
    if (!strcmp(scenario,"current_busy"))
        assert(km_tmc_uart_gpio_set_current(0,700,250)==KM_ERR_BUSY);
    if (!strcmp(scenario,"initial_guard")) {
        for (unsigned i=0;chip->edge_count<2;++i) { assert(i<3000); dispatch(true); }
        /* Setup drives idle high; locate the first actual falling start edge. */
        unsigned i=0;
        while (i<chip->edge_count && chip->edge_levels[i]) ++i;
        assert(i<chip->edge_count);
        assert((int32_t)(chip->edge_history[i]-actuator.ended)>= (int32_t)timer_from_us(6400));
        actuator.cost=0;
    }
    if (!strcmp(scenario,"crc") || !strcmp(scenario,"silent")) {
        /* Every attempt fails: timeout plus the 6.4 ms post-error guard each. */
        run_us(KM_TMC2209_MAX_ATTEMPTS*(10000u+6400u)+10000u,true); expect_fault();
    } else if (!strcmp(scenario,"starved_pending")) {
        for (unsigned i=0;!chip->valid_frames;++i) { assert(i<5000); dispatch(true); }
        actuator.disabled=false;
        run_us(20000,false); expect_fault(); /* No cleanup task before these queries. */
        shim_run_task(); expect_fault();
    } else {
        ready();
        km_tmc_uart_status_t status;
        assert(km_tmc_uart_gpio_get_status(0,&status)==KM_OK);
        assert(status.configured && status.ready && status.ifcnt==5);
        assert(status.requested_run_ma==500 && status.confirmed_run_ma==500);
        assert(status.confirmed_hold_ma==200 && chip->valid_frames>=30);
        if (!strcmp(scenario,"current")) {
            actuator.disabled=false;
            assert(km_tmc_uart_gpio_set_current(0,700,250)==KM_OK);
            assert(actuator.disabled && !km_tmc_uart_gpio_is_ready(0));
            ready();
            assert(km_tmc_uart_gpio_get_status(0,&status)==KM_OK);
            assert(status.confirmed_run_ma==700 && status.confirmed_hold_ma==250 && status.ifcnt==6);
        } else if (!strcmp(scenario,"invalid_current") || !strcmp(scenario,"delayed_tasks")) {
            if (!strcmp(scenario,"invalid_current"))
                assert(km_tmc_uart_gpio_set_current(0,5000,200)==KM_ERR_INVALID_PARAM);
            assert(km_tmc_uart_gpio_is_ready(0));
            run_us(130000,true);
            if (!km_tmc_uart_gpio_is_ready(0)) {
                (void)km_tmc_uart_gpio_get_status(0,&status);
                fprintf(stderr,"monitoring lost readiness: fault=%u frames=%u timers=%d\n",
                        status.fault,chip->valid_frames,shim_active_timer_count());
            }
            assert(km_tmc_uart_gpio_is_ready(0));
        } else if (!strcmp(scenario,"current_irq") || !strcmp(scenario,"restart_irq")) {
            int before=shim_active_timer_count(); irq_disable();
            km_result_t result=!strcmp(scenario,"current_irq")
                ? km_tmc_uart_gpio_set_current(0,700,250) : km_tmc_uart_gpio_restart(0);
            assert(result!=KM_OK && test_shim_irq_is_disabled());
            irq_enable();
            assert(before==shim_active_timer_count() && km_tmc_uart_gpio_is_ready(0));
        } else if (!strcmp(scenario,"starved_diagnostic")) {
            /* The hard bound is the interval plus the whole retry budget. */
            actuator.disabled=false;
            run_us(100000u+(uint32_t)km_tmc_uart_diagnostic_budget_us(10000u)-5000u,false);
            assert(km_tmc_uart_gpio_get_fault(0)==KM_FAULT_NONE && !actuator.disabled);
            run_us(10000u,false); expect_fault();
        } else if (!strcmp(scenario,"crc_diagnostic_transient")) {
            /* A running motor keeps its enable through absorbed diagnostic errors. */
            actuator.disabled=false;
            chip->corrupt_reply_count=KM_TMC2209_MAX_ATTEMPTS-1u;
            run_us(130000,true);
            assert(!chip->corrupt_reply_count && km_tmc_uart_gpio_is_ready(0));
            assert(!actuator.disabled && km_tmc_uart_gpio_get_fault(0)==KM_FAULT_NONE);
            chip->corrupt_reply_count=KM_TMC2209_MAX_ATTEMPTS;
            run_us(200000,true); expect_fault();
        } else if (!strcmp(scenario,"diagnostic") || !strcmp(scenario,"restart")) {
            chip->registers[0x6f]=4;
            actuator.disabled=false;
            run_us(130000,true); expect_fault();
            assert(km_tmc_uart_gpio_get_fault(0)==KM_FAULT_DRIVER_OVERCURRENT);
            if (!strcmp(scenario,"restart")) {
                chip->registers[0x6f]=0;
                assert(km_tmc_uart_gpio_restart(0)==KM_OK && !km_tmc_uart_gpio_is_ready(0));
                ready();
            }
        } else if (!strcmp(scenario,"claim")) {
            assert(km_tmc_uart_gpio_setup(0,12,1,&cfg,force_off,&actuator)==KM_ERR_BUSY);
            assert(km_tmc_uart_gpio_setup(1,10,1,&cfg,force_off,&actuator)==KM_ERR_CONFLICT);
            assert(km_tmc_uart_gpio_is_ready(0));
        } else if (!strcmp(scenario,"restart_cancel_failure")) {
            fail_cancel=true; actuator.disabled=false;
            assert(km_tmc_uart_gpio_restart(0)==KM_ERR_FAULTED);
            expect_fault();
        } else if (!strcmp(scenario,"queries_pure")) {
            unsigned before=clock_reads, frames=chip->valid_frames, off=actuator.calls;
            int timers=shim_active_timer_count();
            irq_disable();
            uint32_t pin;
            assert(km_tmc_uart_gpio_get_status(0,&status)==KM_OK && status.ready);
            assert(km_tmc_uart_gpio_is_ready(0) && km_tmc_uart_gpio_is_slot_active(0));
            assert(km_tmc_uart_gpio_get_fault(0)==KM_FAULT_NONE);
            assert(km_tmc_uart_gpio_get_pin(0,&pin)==KM_OK && pin==10);
            assert(!km_tmc_uart_gpio_is_shutdown() && test_shim_irq_is_disabled());
            irq_enable();
            assert(before==clock_reads && frames==chip->valid_frames && off==actuator.calls);
            assert(timers==shim_active_timer_count());
        } else if (!strcmp(scenario,"shutdown")) {
            actuator.disabled=false; test_shim_use_longjmp=true;
            if (!setjmp(test_shim_shutdown_jump)) shutdown("binding shutdown probe");
            assert(actuator.disabled && !shim_active_timer_count());
            assert(!test_shim_clock_reads_in_shutdown);
            sched_clear_shutdown();
            assert(km_tmc_uart_gpio_is_shutdown() && !km_tmc_uart_gpio_is_ready(0));
            assert(km_tmc_uart_gpio_restart(0)==KM_ERR_FAULTED);
        }
    }
    printf("PASS %s clock=%u address=%u frames=%u off=%u\n",scenario,clock,address,
           chip->valid_frames,actuator.calls);
}
