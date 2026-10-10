// SPDX-License-Identifier: GPL-3.0-or-later
// Native receiver tests using the pinned Klipper parser and oid allocator.
#include <assert.h>
#include <setjmp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "command.h"
#include "basecmd.h"
#include "sched.h"
#include "board/irq.h"
#include "klipper_mmu/buffer_sensor.h"

extern const struct command_encoder encode_acknak;
void alloc_init(void);
void command_allocate_oids(uint32_t *);
void command_finalize_config(uint32_t *);

// Compile the production receiver in this fixture and invoke its shutdown hook.
#include "../../mcu/calibration.c"

static uint8_t arena[65536], frame[MESSAGE_MAX];
static uint8_t stopped;
static jmp_buf fail_jump;
static unsigned failures;
static uint8_t seq = MESSAGE_DEST;

const uint8_t params[] = {PT_byte, PT_buffer};
const struct command_parser command_index[] = {
    [128] = {128, 3, 0, 2, params, command_config_km_diff_calibration},
};
const uint16_t command_index_size = 129;
const uint8_t command_identify_data[] = {0};
const uint32_t command_identify_size = 0;

void *dynmem_start(void) { return arena; }
void *dynmem_end(void) { return arena + sizeof(arena); }
void *console_receive_buffer(void) { return frame; }
void console_sendf(const struct command_encoder *ce, va_list args)
{ (void)ce; (void)args; }
uint8_t sched_is_shutdown(void) { return stopped; }
void sched_clear_shutdown(void) { stopped = 0; }
void sched_timer_reset(void) { }
void sched_report_shutdown(void) { failures++; }
void sched_shutdown(uint_fast8_t reason)
{ (void)reason; stopped = 1; km_cal_shutdown(); longjmp(fail_jump, 1); }
uint8_t ctr_lookup_static_string(const char *s) { (void)s; return 1; }
const struct command_encoder *ctr_lookup_encoder(const char *s)
{ (void)s; return &encode_acknak; }
const struct command_encoder *ctr_lookup_output(const char *s)
{ (void)s; return &encode_acknak; }
irqstatus_t irq_save(void) { return 0; }
void irq_restore(irqstatus_t s) { (void)s; }
void irq_disable(void) { }
void irq_enable(void) { }
void irq_poll(void) { }
uint32_t timer_read_time(void) { return 0; }
uint32_t timer_from_us(uint32_t us) { return us; }
uint16_t crc16_ccitt(uint8_t *p, uint_fast8_t n)
{
    uint16_t crc = 0xffff;
    while (n--) {
        crc ^= (uint16_t)*p++ << 8;
        for (int bit = 0; bit < 8; bit++)
            crc = (uint16_t)((crc << 1) ^ ((crc & 0x8000) ? 0x1021 : 0));
    }
    return crc;
}

static void put16(uint8_t *p, unsigned x)
{ p[0] = (uint8_t)x; p[1] = (uint8_t)(x >> 8); }

// Independent published wire fixture: 10-byte header, four-byte endpoints,
// six-byte interior points. The middle position is intentionally nonlinear.
static unsigned record(uint8_t *p, unsigned n, unsigned map)
{
    memset(p, 0, 54);
    p[0] = (uint8_t)map; p[1] = (uint8_t)n;
    put16(p + 2, 3300); put16(p + 4, 100); put16(p + 6, 3200);
    put16(p + 8, 80);
    unsigned off = 10;
    for (unsigned i = 0; i < n; i++) {
        unsigned a = 200 + 1800 * i / (n - 1);
        unsigned b = 2100 + 1800 * i / (n - 1);
        put16(p + off, a); put16(p + off + 2, b); off += 4;
        if (i && i + 1 < n) { put16(p + off, i == 1 ? 100 : 1000 * i / (n - 1)); off += 2; }
    }
    assert(off == 6 * n + 6);
    return off;
}

static unsigned vlq(uint8_t *p, unsigned v)
{
    if (v < 0x80) { p[0] = (uint8_t)v; return 1; }
    p[0] = (uint8_t)((v >> 7) | 0x80); p[1] = (uint8_t)(v & 0x7f); return 2;
}

static int submit(unsigned oid, const uint8_t *data, unsigned len)
{
    unsigned at = MESSAGE_HEADER_SIZE;
    frame[at++] = 0x81; frame[at++] = 0x00;
    at += vlq(frame + at, oid);
    frame[at++] = (uint8_t)len;
    memcpy(frame + at, data, len); at += len;
    unsigned total = at + MESSAGE_TRAILER_SIZE;
    assert(total <= MESSAGE_MAX);
    frame[0] = (uint8_t)total; frame[1] = seq;
    uint16_t crc = crc16_ccitt(frame, (uint_fast8_t)at);
    frame[at++] = (uint8_t)(crc >> 8); frame[at++] = (uint8_t)crc;
    frame[at++] = MESSAGE_SYNC;
    assert(at == total);
    if (setjmp(fail_jump)) return -1;
    uint_fast8_t pop = 0;
    assert(command_find_block(frame, (uint_fast8_t)total, &pop) == 1);
    assert(pop == total);
    seq = (uint8_t)(((seq + 1) & MESSAGE_SEQ_MASK) | MESSAGE_DEST);
    command_dispatch(frame, (uint_fast8_t)total);
    return 0;
}

static void setup(void)
{
    alloc_init();
    uint32_t count[] = {200};
    command_allocate_oids(count);
    assert(km_diff_calibration_get() == NULL);
}

int main(int argc, char **argv)
{
    assert(argc == 2);
    uint8_t data[54];
    setup();
    const char *name = argv[1];
    if (!strcmp(name, "max") || !strcmp(name, "map")) {
        unsigned n = !strcmp(name, "max") ? 8 : 2;
        unsigned len = record(data, n, !strcmp(name, "map"));
        unsigned oid = n == 8 ? 128 : 2;
        assert(submit(oid, data, len) == 0);
        const km_diff_adc_config_t *p = km_diff_calibration_get();
        assert(p && p == oid_lookup((uint8_t)oid, command_config_km_diff_calibration));
        assert(p->point_count == n && p->channel_map == (!strcmp(name, "map")));
        assert(p->points[0].position_permille == 0);
        assert(p->points[n-1].position_permille == 1000);
        if (n == 8) assert(p->points[1].position_permille == 100);
        if (n == 8) assert(frame[0] == MESSAGE_MAX); // 54-byte record, 2-byte ID/OID.
        km_buffer_sensor_t sensor;
        assert(km_buffer_init_diff_adc(&sensor, p, 200) == KM_OK);
        uint16_t physical_a = p->points[0].ch_a_raw;
        uint16_t physical_b = p->points[0].ch_b_raw;
        if (p->channel_map == KM_DIFF_CH_B_LOW_A_HIGH) {
            physical_a = p->points[0].ch_b_raw;
            physical_b = p->points[0].ch_a_raw;
        }
        assert(km_buffer_update_diff_adc(&sensor, physical_a, physical_b, 1) == KM_OK);
        assert(sensor.is_valid && sensor.position_permille == 0);
        assert(km_diff_calibration_get() == p); // Matching-CRC reconnect: no replay.
    } else if (!strcmp(name, "late")) {
        unsigned len = record(data, 2, 0);
        uint32_t crc[] = {0x12345678};
        command_finalize_config(crc);
        assert(submit(2, data, len) == -1);
        assert(km_diff_calibration_get() == NULL);
    } else if (!strcmp(name, "shutdown_reset")) {
        unsigned len = record(data, 2, 0);
        assert(submit(2, data, len) == 0);
        km_cal_shutdown(); stopped = 1;
        assert(km_diff_calibration_get() == NULL);
        config_reset(NULL);
        assert(km_diff_calibration_get() == NULL);
        assert(submit(2, data, len) == -1);
    } else {
        unsigned len = record(data, 2, 0);
        if (!strcmp(name, "count1")) data[1] = 1;
        else if (!strcmp(name, "count9")) data[1] = 9;
        else if (!strcmp(name, "short")) len--;
        else if (!strcmp(name, "long")) { data[len++] = 0; }
        else if (!strcmp(name, "raw")) put16(data + 10, 4096);
        else if (!strcmp(name, "vref")) put16(data + 2, 0);
        else if (!strcmp(name, "tolerance")) put16(data + 8, 0);
        else if (!strcmp(name, "cross")) put16(data + 14, 50);
        else if (!strcmp(name, "oid256") || !strcmp(name, "oid200")) { /* oid selected below */ }
        else if (!strcmp(name, "duplicate")) {
            assert(submit(2, data, len) == 0);
            assert(submit(3, data, len) == -1);
            assert(km_diff_calibration_get() == NULL);
            goto done;
        } else abort();
        unsigned oid = !strcmp(name, "oid256") ? 256 : !strcmp(name, "oid200") ? 200 : 2;
        assert(submit(oid, data, len) == -1);
        assert(km_diff_calibration_get() == NULL);
    }
done:
    assert(failures == 0);
    puts(name);
    return 0;
}
