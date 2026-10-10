// SPDX-License-Identifier: GPL-3.0-or-later
#include "klipper_mmu/tmc2209.h"
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

/* Independent serial register model: reverse each input byte, then feed a
 * conventional MSB-first CRC polynomial. Never call the production codec. */
static uint8_t device_crc(const uint8_t *p, unsigned n)
{
    unsigned crc = 0;
    for (unsigned i = 0; i < n; ++i) {
        unsigned reversed = 0;
        for (unsigned j = 0; j < 8; ++j) reversed |= ((p[i] >> j) & 1u) << (7u - j);
        crc ^= reversed;
        for (unsigned j = 0; j < 8; ++j) crc = ((crc << 1) ^ ((crc & 128u) ? 7u : 0u)) & 255u;
    }
    return (uint8_t)crc;
}
typedef struct {
    km_tmc2209_t tmc;
    uint32_t registers[128];
    uint32_t now, token;
    uint8_t tx[8], tx_length, rx_length;
    unsigned begins, writes, offs, aborts, drop_writes, corrupt_replies;
    bool active, busy, enabled, drop_write, corrupt_crc, corrupt_readback;
    km_result_t begin_error;
} device_t;
static km_result_t begin(void *ctx, uint32_t token, const uint8_t *tx, uint8_t ntx, uint8_t nrx)
{
    device_t *d = ctx;
    ++d->begins;
    if (d->busy) return KM_ERR_BUSY;
    if (d->begin_error != KM_OK) return d->begin_error;
    assert(!d->active && token && (ntx == 4 || ntx == 8));
    assert(tx[0] == 5 && tx[1] == d->tmc.config.address);
    assert(tx[ntx - 1u] == device_crc(tx, (unsigned)ntx - 1u));
    assert(nrx == (ntx == 4 ? 8 : 0));
    d->active = true; d->token = token; d->tx_length = ntx; d->rx_length = nrx;
    memcpy(d->tx, tx, ntx);
    return KM_OK;
}
static void abort_transfer(void *ctx) { device_t *d = ctx; ++d->aborts; d->active = false; }
static void force_off(void *ctx) { device_t *d = ctx; ++d->offs; d->enabled = false; }
static const km_tmc2209_transport_t transport = {begin, abort_transfer, force_off};
static km_tmc2209_config_t config(void)
{
    return (km_tmc2209_config_t){.address=0, .sense_resistor_milliohms=110,
        .maximum_current_ma=1400, .run_current_ma=800, .hold_current_ma=400,
        .microsteps=16, .interpolate=true, .stealthchop=false,
        .transaction_timeout_us=10000, .diagnostic_interval_us=100000};
}
static void setup(device_t *d)
{
    memset(d, 0, sizeof(*d));
    d->registers[6] = 0x21000000; d->registers[1] = 1; d->registers[2] = 250;
    km_tmc2209_config_t cfg = config();
    assert(km_tmc2209_init(&d->tmc, &cfg, &transport, d) == KM_OK);
    assert(d->offs && !d->enabled && !d->tmc.ready);
}
static void reply_frame(uint8_t reg, uint32_t value, uint8_t rx[8])
{
    rx[0]=5; rx[1]=255; rx[2]=reg;
    for (unsigned i=0; i<4; ++i) rx[3+i]=(uint8_t)(value >> (24u-i*8u));
    rx[7]=device_crc(rx, 7);
}
static km_result_t finish(device_t *d)
{
    assert(d->active);
    d->active = false; d->now += 2000;
    uint8_t reg = d->tx[2] & 127u;
    if (d->tx_length == 8) {
        assert(reg != 4 && reg != 7 && (d->tx[2] & 128u));
        assert(!d->enabled); /* No configuration writes to an active motor. */
        ++d->writes;
        bool dropped = d->drop_write || d->drop_writes;
        if (d->drop_writes) --d->drop_writes;
        if (!dropped) {
            uint32_t value = ((uint32_t)d->tx[3]<<24)|((uint32_t)d->tx[4]<<16)
                |((uint32_t)d->tx[5]<<8)|d->tx[6];
            if (reg == 1) d->registers[reg] &= ~value;
            else d->registers[reg] = value;
            d->registers[2] = (d->registers[2] + 1u) & 255u;
        }
        return km_tmc2209_complete(&d->tmc,d->token,KM_OK,NULL,0,d->now);
    }
    uint32_t value = d->registers[reg];
    if (d->corrupt_readback && reg == 0) value ^= 4u;
    uint8_t rx[8]; reply_frame(reg,value,rx);
    if (d->corrupt_crc || d->corrupt_replies) rx[7] ^= 1u;
    if (d->corrupt_replies) --d->corrupt_replies;
    return km_tmc2209_complete(&d->tmc,d->token,KM_OK,rx,8,d->now);
}
static km_result_t exchange(device_t *d)
{
    km_result_t result = km_tmc2209_service(&d->tmc, d->now);
    if (result != KM_OK || !d->active) return result;
    assert(km_tmc2209_service(&d->tmc, d->now) == KM_ERR_BUSY);
    return finish(d);
}
static void until_ready(device_t *d)
{
    unsigned count=0;
    while (!d->tmc.ready) { assert(++count<40); assert(exchange(d)==KM_OK); }
    assert(!d->enabled && !d->active && d->tmc.fault==KM_FAULT_NONE);
}
static void assert_fault(device_t *d, km_fault_t fault)
{
    assert(d->tmc.fault==fault && !d->tmc.ready && !d->enabled && !d->active);
    unsigned starts=d->begins;
    assert(km_tmc2209_service(&d->tmc,d->now)==KM_ERR_FAULTED);
    assert(km_tmc2209_set_current(&d->tmc,700,300)==KM_ERR_FAULTED);
    assert(d->begins==starts);
}
static void test_codec(void)
{
    /* Values independently cross-checked against pinned upstream tmc_uart.py. */
    const uint8_t read0[]={5,0,0,0x48}, read6[]={5,0,6,0x6f}, read3[]={5,3,2,0x62};
    const uint8_t write[]={5,0,0x90,0,8,15,7,0x48};
    uint8_t frame[8];
    assert(km_tmc2209_read_frame(0,0,frame)==KM_OK && memcmp(frame,read0,4)==0);
    assert(km_tmc2209_read_frame(0,6,frame)==KM_OK && memcmp(frame,read6,4)==0);
    assert(km_tmc2209_read_frame(3,2,frame)==KM_OK && memcmp(frame,read3,4)==0);
    assert(km_tmc2209_write_frame(0,0x10,0x00080f07,frame)==KM_OK && memcmp(frame,write,8)==0);
    assert(km_tmc2209_read_frame(4,0,frame)==KM_ERR_INVALID_PARAM);
    assert(km_tmc2209_write_frame(0,4,0,frame)==KM_ERR_INVALID_PARAM);
    assert(km_tmc2209_write_frame(0,7,0,frame)==KM_ERR_INVALID_PARAM);
    uint8_t rx[]={5,255,6,0x21,0,0,0,0x41}; uint32_t value=0;
    assert(km_tmc2209_decode(6,rx,8,&value)==KM_OK && value==0x21000000);
    for (unsigned byte=0; byte<8; ++byte) for (unsigned bit=0; bit<8; ++bit) {
        uint8_t bad[8]; memcpy(bad,rx,8); bad[byte] ^= (uint8_t)(1u<<bit);
        value=0x12345678;
        assert(km_tmc2209_decode(6,bad,8,&value)==KM_ERR_FAULTED && value==0x12345678);
    }
    assert(km_tmc2209_decode(6,rx,7,&value)==KM_ERR_INVALID_PARAM);
    assert(km_tmc2209_decode(5,rx,8,&value)==KM_ERR_FAULTED);
}
static void test_initialization(void)
{
    device_t d; setup(&d); until_ready(&d);
    assert(d.writes==11 && d.registers[2]==5); /* IFCNT wrap. */
    assert(d.registers[0]==0x1c4 && d.registers[0x22]==0 && d.registers[3]==0x200);
    assert(d.registers[0x6c]==0x14010053 && d.registers[0x10]==0x00080d06);
    assert(d.registers[0x70]==0xc80d0e24 && d.registers[0x42]==0 && d.registers[1]==0);
    assert(d.tmc.confirmed_run_ma==800 && d.tmc.confirmed_hold_ma==400);
    assert(d.tmc.nominal_run_ma==773 && d.tmc.nominal_hold_ma==386);
    unsigned starts=d.begins;
    assert(km_tmc2209_service(&d.tmc,d.now+99999)==KM_OK && starts==d.begins);
    d.enabled=true; d.now=d.tmc.next_diagnostic_us;
    assert(exchange(&d)==KM_OK && exchange(&d)==KM_OK && d.tmc.ready && d.enabled);
}
static void test_invalid_config(void)
{
    for (unsigned which=0; which<14; ++which) {
        device_t d; memset(&d,0,sizeof(d)); memset(&d.tmc,0xa5,sizeof(d.tmc));
        d.enabled=true; km_tmc2209_config_t c=config();
        switch(which) {
        case 0:c.address=4;break; case 1:c.sense_resistor_milliohms=0;break;
        case 2:c.maximum_current_ma=2001;break; case 3:c.microsteps=3;break;
        case 4:c.microsteps=0;break; case 5:c.transaction_timeout_us=999;break;
        case 6:c.diagnostic_interval_us=999;break; case 7:c.run_current_ma=0;break;
        case 8:c.hold_current_ma=0;break; case 9:c.hold_current_ma=900;break;
        case 10:c.run_current_ma=1401;break; case 11:c.sense_resistor_milliohms=1000;break;
        case 12:c.hold_current_ma=1;break; case 13:c.maximum_current_ma=0;break;
        }
        assert(km_tmc2209_init(&d.tmc,&c,&transport,&d)==KM_ERR_INVALID_PARAM);
        assert(!d.enabled && !d.tmc.configured && !d.tmc.ready);
        assert(km_tmc2209_restart(&d.tmc)==KM_ERR_INVALID_PARAM);
        assert(km_tmc2209_service(&d.tmc,0)==KM_ERR_INVALID_PARAM && d.begins==0);
    }
    device_t d; memset(&d,0,sizeof(d));
    km_tmc2209_transport_t bad=transport; bad.begin=NULL; d.enabled=true;
    km_tmc2209_config_t c=config();
    assert(km_tmc2209_init(&d.tmc,&c,&bad,&d)==KM_ERR_INVALID_PARAM);
    assert(!d.enabled && !d.tmc.configured);
}
static void test_current_range(void)
{
    for (unsigned high=0; high<2; ++high) {
        for (unsigned steps=1; steps<=256; steps*=2) {
            device_t d; setup(&d); km_tmc2209_config_t c=config();
            c.maximum_current_ma=(uint16_t)(high ? 900 : 1400);
            c.microsteps=(uint16_t)steps; c.stealthchop=true;
            assert(km_tmc2209_init(&d.tmc,&c,&transport,&d)==KM_OK);
            until_ready(&d);
            assert(d.tmc.vsense==(high!=0) && d.registers[0]==0x1c0);
            unsigned mres=(d.registers[0x6c]>>24)&15u;
            assert((256u>>mres)==steps);
            for (unsigned run=100; run<=c.maximum_current_ma; run+=37) {
                uint16_t previous=d.tmc.confirmed_run_ma;
                assert(km_tmc2209_set_current(&d.tmc,(uint16_t)run,60)==KM_OK);
                assert(!d.tmc.ready && d.tmc.confirmed_run_ma==previous);
                until_ready(&d);
                unsigned count=((d.registers[0x10]>>8)&31u)+1u;
                double ma=(double)count/32.0*(high ? 180.0 : 325.0)/0.130/sqrt(2.0);
                double quantum=ma/(double)count;
                assert(ma <= (double)run && ma+quantum>(double)run);
                assert(fabs((double)d.tmc.confirmed_nominal_run_ma-ma)<1.001);
                assert(d.tmc.confirmed_run_ma==run && d.tmc.confirmed_hold_ma==60);
            }
        }
    }
}
static void test_current_confirmation(void)
{
    device_t d; setup(&d); until_ready(&d); d.enabled=true;
    unsigned offs=d.offs;
    assert(km_tmc2209_set_current(&d.tmc,800,400)==KM_OK && d.offs==offs && d.enabled);
    assert(km_tmc2209_set_current(&d.tmc,1401,400)==KM_ERR_INVALID_PARAM && d.enabled);
    assert(km_tmc2209_set_current(&d.tmc,700,800)==KM_ERR_INVALID_PARAM && d.enabled);
    assert(km_tmc2209_set_current(&d.tmc,700,300)==KM_OK && !d.enabled && !d.tmc.ready);
    assert(d.tmc.confirmed_run_ma==800 && d.tmc.confirmed_nominal_run_ma==773);
    assert(km_tmc2209_set_current(&d.tmc,600,300)==KM_ERR_BUSY);
    until_ready(&d); assert(d.tmc.confirmed_run_ma==700 && d.writes==12);
    assert(km_tmc2209_set_current(&d.tmc,600,300)==KM_OK);
    d.drop_write=true;
    unsigned attempts=0;
    while (exchange(&d)==KM_OK) assert(++attempts<20);
    assert_fault(&d,KM_FAULT_DRIVER_FAULT);
    assert(d.writes==12+KM_TMC2209_MAX_ATTEMPTS); /* Rewritten, never more. */
    assert(d.tmc.confirmed_run_ma==700);
}
static void test_startup_faults(void)
{
    for (unsigned kind=0; kind<5; ++kind) {
        device_t d; setup(&d);
        if (kind==0) d.registers[6]=0x20000000;
        if (kind==1) d.registers[1]=4;
        if (kind==2) d.corrupt_crc=true;
        if (kind==3) d.drop_write=true;
        if (kind==4) d.corrupt_readback=true;
        unsigned attempts=0;
        while (d.tmc.fault==KM_FAULT_NONE) { assert(++attempts<40); (void)exchange(&d); }
        assert_fault(&d,KM_FAULT_DRIVER_FAULT);
        if (kind<3) assert(d.writes==0);
        if (kind==0) assert(d.begins==1);  /* Wrong identity is reported, not retried. */
        if (kind==1) assert(d.begins==2);  /* Reset/protection flags likewise. */
        if (kind==2) assert(d.begins==KM_TMC2209_MAX_ATTEMPTS);
        if (kind==3) assert(d.writes==KM_TMC2209_MAX_ATTEMPTS);
    }
}
static void test_diagnostics(void)
{
    for (unsigned kind=0; kind<9; ++kind) {
        device_t d; setup(&d); until_ready(&d); d.enabled=true;
        d.now=d.tmc.next_diagnostic_us;
        if (kind<3) d.registers[1]=1u<<kind;
        else d.registers[0x6f]=1u<<(kind-2u); /* OT + four shorts + open load. */
        km_result_t result=exchange(&d);
        if (result==KM_OK) result=exchange(&d);
        if (kind==8) { assert(result==KM_OK && d.tmc.ready && d.enabled); }
        else {
            assert(result==KM_ERR_FAULTED);
            assert_fault(&d,(kind>=4) ? KM_FAULT_DRIVER_OVERCURRENT : KM_FAULT_DRIVER_FAULT);
        }
    }
    device_t d; setup(&d); until_ready(&d); d.enabled=true;
    d.registers[0x6f]=0x00ff00c1; /* OTPW/open-load/current scale: telemetry only. */
    d.now=d.tmc.next_diagnostic_us;
    assert(exchange(&d)==KM_OK && exchange(&d)==KM_OK && d.enabled);
    assert(d.tmc.drv_status==0x00ff00c1 && d.tmc.confirmed_nominal_run_ma==773);
}
static void test_timeout_restart_and_tokens(void)
{
    device_t d; setup(&d); d.now=UINT32_MAX-1000u;
    assert(km_tmc2209_service(&d.tmc,d.now)==KM_OK);
    uint32_t old=d.token;
    assert(km_tmc2209_complete(&d.tmc,old+1u,KM_OK,NULL,0,d.now)==KM_ERR_STALE && d.active);
    assert(km_tmc2209_service(&d.tmc,d.now+9999u)==KM_ERR_BUSY);
    d.now+=10000u;
    for (unsigned attempt=1; attempt<=KM_TMC2209_MAX_ATTEMPTS; ++attempt) {
        unsigned aborts=d.aborts;
        km_result_t r=km_tmc2209_service(&d.tmc,d.now);
        assert(d.aborts==aborts+1u && !d.active); /* Unanswered transfer released. */
        if (attempt==KM_TMC2209_MAX_ATTEMPTS) { assert(r==KM_ERR_FAULTED); break; }
        assert(r==KM_ERR_BUSY && d.tmc.fault==KM_FAULT_NONE && !d.tmc.pending);
        /* A late reply to the abandoned attempt is never accepted. */
        assert(km_tmc2209_complete(&d.tmc,d.token,KM_OK,NULL,0,d.now)==KM_ERR_STALE);
        assert(km_tmc2209_service(&d.tmc,d.now)==KM_OK && d.active && d.token!=old);
        old=d.token; d.now+=10000u;
    }
    assert_fault(&d,KM_FAULT_DRIVER_FAULT);
    assert(km_tmc2209_restart(&d.tmc)==KM_OK);
    assert(km_tmc2209_complete(&d.tmc,old,KM_OK,NULL,0,d.now)==KM_ERR_STALE);
    assert(km_tmc2209_service(&d.tmc,d.now)==KM_OK && d.token!=old);
    assert(km_tmc2209_complete(&d.tmc,old,KM_OK,NULL,0,d.now)==KM_ERR_STALE && d.active);
    assert(finish(&d)==KM_OK); until_ready(&d);
    assert(km_tmc2209_complete(&d.tmc,d.token,KM_OK,NULL,0,d.now)==KM_ERR_STALE);
    /* Explicit restart invalidates a pending read and disables an active motor. */
    d.enabled=true; d.now=d.tmc.next_diagnostic_us;
    assert(km_tmc2209_service(&d.tmc,d.now)==KM_OK); old=d.token;
    assert(km_tmc2209_restart(&d.tmc)==KM_OK && !d.active && !d.enabled);
    assert(km_tmc2209_complete(&d.tmc,old,KM_OK,NULL,0,d.now)==KM_ERR_STALE);
    d.registers[1]=1; until_ready(&d);
    setup(&d); d.busy=true;
    assert(km_tmc2209_service(&d.tmc,0)==KM_ERR_BUSY);
    assert(km_tmc2209_service(&d.tmc,9999)==KM_ERR_BUSY);
    assert(km_tmc2209_service(&d.tmc,10000)==KM_ERR_FAULTED);
    assert_fault(&d,KM_FAULT_DRIVER_FAULT);
    setup(&d); d.begin_error=KM_ERR_UNAVAILABLE;
    assert(km_tmc2209_service(&d.tmc,0)==KM_ERR_FAULTED);
    assert_fault(&d,KM_FAULT_DRIVER_FAULT);
    setup(&d); d.now=UINT32_MAX-200;
    until_ready(&d); /* Valid exchange across wrapping timestamp. */
    setup(&d); /* A valid reply after its deadline counts as a failed attempt. */
    for (unsigned attempt=1; attempt<=KM_TMC2209_MAX_ATTEMPTS; ++attempt) {
        assert(km_tmc2209_service(&d.tmc,0)==KM_OK);
        uint8_t rx[8]; reply_frame(6,0x21000000,rx); d.active=false;
        assert(km_tmc2209_complete(&d.tmc,d.token,KM_OK,rx,8,10000)
               ==(attempt<KM_TMC2209_MAX_ATTEMPTS ? KM_OK : KM_ERR_FAULTED));
    }
    assert_fault(&d,KM_FAULT_DRIVER_FAULT);
}
static void test_transient_retries(void)
{
    /* One corrupt identify reply is retried, not latched. */
    device_t d; setup(&d); until_ready(&d);
    unsigned clean_begins=d.begins;
    setup(&d); d.corrupt_replies=1;
    unsigned setup_offs=d.offs;
    until_ready(&d);
    assert(d.writes==11 && d.registers[2]==5 && d.offs==setup_offs);
    assert(d.begins==clean_begins+1u);
    /* A write the chip missed is resent against the observed count. */
    setup(&d); d.drop_writes=1; until_ready(&d);
    assert(d.writes==12 && d.registers[2]==5 && d.tmc.fault==KM_FAULT_NONE);
    /* Diagnostics tolerate MAX-1 failures per read with the motor running,
     * and the budget restarts after every successful read. */
    setup(&d); until_ready(&d); d.enabled=true;
    unsigned offs=d.offs;
    for (unsigned round=0; round<3; ++round) {
        d.now=d.tmc.next_diagnostic_us;
        d.corrupt_replies=KM_TMC2209_MAX_ATTEMPTS-1u;
        while (d.corrupt_replies) { assert(exchange(&d)==KM_OK && d.tmc.ready && d.enabled); }
        assert(exchange(&d)==KM_OK); /* GSTAT */
        d.corrupt_replies=KM_TMC2209_MAX_ATTEMPTS-1u;
        while (d.corrupt_replies) assert(exchange(&d)==KM_OK);
        assert(exchange(&d)==KM_OK && d.tmc.state==KM_TMC_READY); /* DRV_STATUS */
        assert(d.tmc.ready && d.enabled && d.offs==offs && d.tmc.attempts==0);
    }
    /* An unanswered diagnostic read is released and retried while running. */
    d.now=d.tmc.next_diagnostic_us;
    assert(km_tmc2209_service(&d.tmc,d.now)==KM_OK && d.active);
    d.now+=10000u;
    assert(km_tmc2209_service(&d.tmc,d.now)==KM_ERR_BUSY && !d.active && d.enabled);
    assert(exchange(&d)==KM_OK && exchange(&d)==KM_OK && d.tmc.ready && d.enabled);
    /* The bound is consecutive: MAX corrupt replies latch and force the motor off. */
    d.now=d.tmc.next_diagnostic_us;
    d.corrupt_replies=KM_TMC2209_MAX_ATTEMPTS;
    for (unsigned i=1; i<KM_TMC2209_MAX_ATTEMPTS; ++i) assert(exchange(&d)==KM_OK);
    assert(exchange(&d)==KM_ERR_FAULTED);
    assert_fault(&d,KM_FAULT_DRIVER_FAULT);
    assert(d.offs==offs+1u);
    /* A transport that refuses a frame outright is not retried. */
    setup(&d); d.begin_error=KM_ERR_FAULTED;
    assert(km_tmc2209_service(&d.tmc,0)==KM_ERR_FAULTED && d.begins==1);
    assert_fault(&d,KM_FAULT_DRIVER_FAULT);
}
int main(void)
{
    test_codec(); test_initialization(); test_invalid_config(); test_current_range();
    test_current_confirmation(); test_startup_faults(); test_diagnostics();
    test_timeout_restart_and_tokens(); test_transient_retries();
    puts("TMC2209: codec, startup, current, faults, retries, timeouts, tokens and wrap passed");
    return 0;
}
