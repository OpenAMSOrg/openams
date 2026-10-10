// SPDX-License-Identifier: GPL-3.0-or-later
// Test-only: a pin-level TMC2209 model for the sim board.
//
// klipper-mmu's firmware (mcu/tmc_uart_gpio.c, src/tmc2209.c) is the UART
// host: it drives Klipper's shared bit-banged src/tmcuart.c (see
// tests/fixtures/shared_tmcuart/src/tmcuart.c for the unmodified reference)
// over one single-wire pin per lane. This file plays the chip on the other
// end: it decodes the host's timed TX edges into datagrams the same way the
// real chip's UART auto-baud sync does, applies register writes, and drives
// the line with a reply after SENDDELAY bit-times, exactly like real
// hardware. See docs/plan/linux-mcu-integration.md, "Time, or how to
// bit-bang a UART on an operating system that naps": every edge and sample
// here is a function of the sim's virtual time, recovered from the recorded
// edge log, never a side effect of how many times a caller happens to poll.
//
// Register table/field names: sim/board/tmc2209_regs.h. CRC and the 8-byte
// reply framing reuse the firmware's own km_tmc2209_crc/km_tmc_uart_encode
// (src/tmc2209.c, src/tmc_uart_codec.c) rather than a second implementation.
#include <string.h>
#include <stdio.h>                       // fprintf: evidence lines into the MCU log
#include "compiler.h"                    // container_of
#include "sched.h"                       // struct timer, sched_add_timer, SF_*
#include "board/misc.h"                  // timer_is_before
#include "stm32/internal.h"              // GPIO()
#include "klipper_mmu/tmc2209.h"         // km_tmc2209_crc
#include "klipper_mmu/tmc_uart_codec.h"  // km_tmc_uart_encode
#include "tmc2209_regs.h"
#include "tmc2209.h"

#define KM_SIM_TMC_MAX_CHIPS 5
#define KM_SIM_TMC_MAX_EDGES 96
#define KM_SIM_TMC_REG_COUNT 0x73u
#define KM_SIM_TMC_HOST_SYNC 0x05u   /* klipper-mmu's own request sync+reserved byte
                                      * (src/tmc2209.c km_tmc2209_read_frame/write_frame);
                                      * klippy's stock tmc_uart.py uses 0xf5 for the same
                                      * field, but that driver is not in this datapath. */
#define KM_SIM_TMC_CHIP_SYNC 0x05u   /* Reply sync+reserved byte, both conventions. */
#define KM_SIM_TMC_MASTER_ADDR 0xffu
#define KM_SIM_TMC_DRV_STATUS_REG 0x6fu        /* DRV_STATUS */
#define KM_SIM_TMC_DRV_STATUS_OT (1u << 1)     /* DRV_STATUS.ot, bit 1 */

typedef struct {
    uint32_t time;
    uint8_t level;
} km_sim_edge_t;

typedef struct {
    uint8_t in_use;
    uint32_t pin;
    uint8_t addr;

    /* Receiver: edges recorded since the current frame's start bit. */
    uint8_t receiving;
    km_sim_edge_t edges[KM_SIM_TMC_MAX_EDGES];
    uint8_t edge_count;
    uint32_t frame_start;
    uint32_t bit_time;
    struct timer header_timer;
    struct timer frame_timer;

    /* Register file, indexed directly by TMC register address. */
    uint32_t regs[KM_SIM_TMC_REG_COUNT];
    uint8_t have[KM_SIM_TMC_REG_COUNT];
    /* SENDDELAY, in bit-times: TMC2209 datasheet rev1.09 SLAVECONF table
     * (field 0 or 1 -> 8, the chip's power-on default; field N>=2 -> 4*N).
     * This floor matters here, not just for datasheet fidelity: Klipper's
     * shared tmcuart.c (tmcuart_send_finish_event) always waits a fixed 4
     * bit-times before it starts polling for the reply's sync edge, so a
     * reply that started any earlier would be missed or misread as noise. */
    uint16_t senddelay_bits;

    /* Reply transmitter. Its own copy of bit_time: abort_frame() (called
     * right after schedule_reply() arms the first bit) resets the receiver's
     * bit_time for the next frame, but the reply is still mid-flight and
     * needs a stable bit period for every subsequent SF_RESCHEDULE. */
    struct timer reply_timer;
    uint32_t reply_bit_time;
    uint8_t reply_encoded[KM_TMC_UART_ENCODED_MAX];
    uint8_t reply_bit_count;
    uint8_t reply_pos;
    uint8_t chip_level;
    /* Injected fault (control socket "tmc <pin> fault on"): report the
     * DRV_STATUS ot (overtemperature) bit on every read of 0x6f, the
     * register klipper-mmu's own driver check polls. */
    uint8_t drv_status_fault;

    /* Evidence, logged to stderr (captured into the MCU log by the test
     * harness) so a host-side test can assert zero retries/malformed frames
     * without new IPC. */
    uint32_t valid_frames, malformed_frames, foreign_frames, replies_sent;
} km_sim_tmc_chip_t;

static km_sim_tmc_chip_t chips[KM_SIM_TMC_MAX_CHIPS];

static km_sim_tmc_chip_t *find_by_pin(uint32_t pin)
{
    for (unsigned i = 0; i < KM_SIM_TMC_MAX_CHIPS; ++i)
        if (chips[i].in_use && chips[i].pin == pin)
            return &chips[i];
    return NULL;
}

static void abort_frame(km_sim_tmc_chip_t *c)
{
    c->receiving = 0;
    c->edge_count = 0;
    c->bit_time = 0;
}

// The line's level at time t, as a pure function of the recorded edge log
// (never "the next queued bit"): the level set by the latest edge at or
// before t, or the start bit's 0 before the first recorded edge.
static uint8_t level_at(const km_sim_tmc_chip_t *c, uint32_t t)
{
    uint8_t level = 0;
    for (uint8_t i = 0; i < c->edge_count; ++i) {
        if (timer_is_before(t, c->edges[i].time))
            break;
        level = c->edges[i].level;
    }
    return level;
}

// Decode one 8N1 byte (start=0, 8 data bits LSB-first, stop=1) starting at
// bit position `byte_index` of the current frame. *ok is cleared on a
// framing error (this is how a genuinely malformed frame is caught, not
// just a CRC mismatch).
static uint8_t decode_byte(const km_sim_tmc_chip_t *c, uint8_t byte_index, uint8_t *ok)
{
    uint32_t base = c->frame_start + (uint32_t)byte_index * 10u * c->bit_time;
    uint8_t half = (uint8_t)(c->bit_time / 2u);
    if (level_at(c, base + half) != 0 || level_at(c, base + 9u * c->bit_time + half) != 1) {
        *ok = 0;
        return 0;
    }
    uint8_t value = 0;
    for (uint8_t b = 0; b < 8; ++b)
        if (level_at(c, base + (1u + b) * c->bit_time + half))
            value |= (uint8_t)(1u << b);
    *ok = 1;
    return value;
}

static void apply_write(km_sim_tmc_chip_t *c, uint8_t reg, uint32_t value)
{
    if (reg == 0x01u) {
        c->regs[0x01u] &= ~value;   /* GSTAT: write-1-to-clear. */
    } else {
        c->regs[reg] = value;
        c->have[reg] = 1;
        if (reg == 0x03u) {
            uint16_t field = (uint16_t)((value >> 8) & 0xfu);
            c->senddelay_bits = field <= 2u ? 8u : (uint16_t)(field * 4u);
        }
    }
    c->regs[0x02u] = (c->regs[0x02u] + 1u) & 0xffu;   /* IFCNT: any accepted write. */
    c->have[0x02u] = 1;
}

#if defined(CONFIG_WANT_TMCUART_LOCAL) && CONFIG_WANT_TMCUART_LOCAL && \
    defined(CONFIG_KLIPPER_MMU_TMC_UART_SHARED) && CONFIG_KLIPPER_MMU_TMC_UART_SHARED
static uint_fast8_t reply_bit_event(struct timer *timer)
{
    km_sim_tmc_chip_t *c = container_of(timer, km_sim_tmc_chip_t, reply_timer);
    uint8_t bit = (c->reply_encoded[c->reply_pos >> 3] >> (c->reply_pos & 7u)) & 1u;
    c->chip_level = bit;
    c->reply_pos++;
    if (c->reply_pos < c->reply_bit_count) {
        timer->waketime += c->reply_bit_time;
        return SF_RESCHEDULE;
    }
    c->chip_level = 1;   /* Idle high (pull-up) once the reply is sent. */
    c->replies_sent++;
    return SF_DONE;
}
#endif

static void schedule_reply(km_sim_tmc_chip_t *c, uint32_t request_end, uint8_t reg)
{
#if defined(CONFIG_WANT_TMCUART_LOCAL) && CONFIG_WANT_TMCUART_LOCAL && \
    defined(CONFIG_KLIPPER_MMU_TMC_UART_SHARED) && CONFIG_KLIPPER_MMU_TMC_UART_SHARED
    uint8_t raw[8] = {
        KM_SIM_TMC_CHIP_SYNC, KM_SIM_TMC_MASTER_ADDR, reg,
        (uint8_t)(c->regs[reg] >> 24), (uint8_t)(c->regs[reg] >> 16),
        (uint8_t)(c->regs[reg] >> 8), (uint8_t)c->regs[reg], 0
    };
    /* Injected DRV_STATUS.ot, before the CRC covers it: this model has a
     * register file and reply framing but no stuck-low line driver, so a
     * fault has to be something the chip says rather than something the
     * wire does. */
    if (c->drv_status_fault && reg == KM_SIM_TMC_DRV_STATUS_REG)
        raw[6] |= KM_SIM_TMC_DRV_STATUS_OT;
    raw[7] = km_tmc2209_crc(raw, 7);
    uint8_t count = km_tmc_uart_encode(raw, 8, c->reply_encoded);
    if (!count)
        return;
    c->reply_bit_count = (uint8_t)(count * 8u);
    c->reply_pos = 0;
    c->reply_bit_time = c->bit_time;   /* Frozen: abort_frame() clears bit_time next. */
    c->reply_timer.func = reply_bit_event;
    /* Reply phase: half a bit past SENDDELAY, per the task spec's default. */
    c->reply_timer.waketime = request_end + c->senddelay_bits * c->reply_bit_time
        + c->reply_bit_time / 2u;
    sched_add_timer(&c->reply_timer);
#else
    /* No image links the shared TMC UART reply codec (mcu/Makefile's own
     * WANT_TMCUART_LOCAL && KLIPPER_MMU_TMC_UART_SHARED gate on src/
     * tmc_uart_codec.c) -- a gear with no TMC UART chip at all (e.g. QIDI
     * Box) never drives a real bus write into this model, so this can only
     * be reached by a test bug, never real firmware behaviour. */
    (void)c; (void)request_end; (void)reg;
#endif
}

static uint_fast8_t frame_complete_event(struct timer *timer)
{
    km_sim_tmc_chip_t *c = container_of(timer, km_sim_tmc_chip_t, frame_timer);
    uint8_t ok0, ok1, ok2, sync, addr, regbyte;
    sync = decode_byte(c, 0, &ok0);
    addr = decode_byte(c, 1, &ok1);
    regbyte = decode_byte(c, 2, &ok2);
    uint8_t write = (regbyte & 0x80u) != 0;
    uint8_t total = write ? 8 : 4;
    uint8_t bytes[8] = {sync, addr, regbyte, 0, 0, 0, 0, 0};
    uint8_t ok = ok0 && ok1 && ok2;
    for (uint8_t i = 3; ok && i < total; ++i) {
        uint8_t bok;
        bytes[i] = decode_byte(c, i, &bok);
        ok = ok && bok;
    }
    uint32_t request_end = c->frame_start + (uint32_t)total * 10u * c->bit_time;
    if (!ok || sync != KM_SIM_TMC_HOST_SYNC
        || bytes[total - 1] != km_tmc2209_crc(bytes, (uint8_t)(total - 1))) {
        c->malformed_frames++;
        fprintf(stderr, "km_sim_tmc2209 pin=%u event=malformed count=%u\n",
                (unsigned)c->pin, (unsigned)c->malformed_frames);
        abort_frame(c);
        return SF_DONE;
    }
    if (addr != c->addr) {
        c->foreign_frames++;
        fprintf(stderr, "km_sim_tmc2209 pin=%u event=foreign count=%u\n",
                (unsigned)c->pin, (unsigned)c->foreign_frames);
        abort_frame(c);
        return SF_DONE;
    }
    c->valid_frames++;
    if (write) {
        uint8_t reg = (uint8_t)(regbyte & 0x7fu);
        apply_write(c, reg, ((uint32_t)bytes[3] << 24)
                    | ((uint32_t)bytes[4] << 16) | ((uint32_t)bytes[5] << 8) | bytes[6]);
        fprintf(stderr, "km_sim_tmc2209 pin=%u event=write reg=0x%02x ifcnt=%u frames=%u\n",
                (unsigned)c->pin, reg, (unsigned)c->regs[0x02u], (unsigned)c->valid_frames);
    } else {
        uint8_t reg = (uint8_t)(regbyte & 0x7fu);
        /* Only startup reads are logged (the boot checks count them); the
         * firmware then polls every driver constantly, and an unbuffered
         * write per read occasionally blocked the sim for tens of ms. */
        if (c->valid_frames <= 64u)
            fprintf(stderr, "km_sim_tmc2209 pin=%u event=read reg=0x%02x frames=%u\n",
                    (unsigned)c->pin, reg, (unsigned)c->valid_frames);
        if (reg < KM_SIM_TMC_REG_COUNT && c->have[reg])
            schedule_reply(c, request_end, reg);
    }
    abort_frame(c);
    return SF_DONE;
}

static uint_fast8_t header_ready_event(struct timer *timer)
{
    km_sim_tmc_chip_t *c = container_of(timer, km_sim_tmc_chip_t, header_timer);
    uint8_t ok0, ok1, ok2, sync, regbyte;
    sync = decode_byte(c, 0, &ok0);
    decode_byte(c, 1, &ok1);
    regbyte = decode_byte(c, 2, &ok2);
    if (!ok0 || !ok1 || !ok2 || sync != KM_SIM_TMC_HOST_SYNC) {
        c->malformed_frames++;
        abort_frame(c);
        return SF_DONE;
    }
    uint8_t total = (regbyte & 0x80u) ? 8 : 4;
    c->frame_timer.func = frame_complete_event;
    c->frame_timer.waketime = c->frame_start + (uint32_t)total * 10u * c->bit_time;
    sched_add_timer(&c->frame_timer);
    return SF_DONE;
}

void km_sim_tmc2209_notify_write(uint32_t pin, uint8_t level, uint32_t time)
{
    km_sim_tmc_chip_t *c = find_by_pin(pin);
    if (!c)
        return;
    if (!c->receiving) {
        if (level != 0)
            return;   /* Only a falling edge starts a frame (idle is high). */
        c->receiving = 1;
        c->edge_count = 0;
        c->bit_time = 0;
        c->frame_start = time;
    }
    if (c->edge_count < KM_SIM_TMC_MAX_EDGES) {
        c->edges[c->edge_count].time = time;
        c->edges[c->edge_count].level = level;
        c->edge_count++;
    }
    /* Baud auto-detection, exactly the host's own tmcuart_send_sync_event
     * math applied to the edges it produced: the sync+reserved byte's fixed
     * low nibble (0b0101) always yields a clean edge every bit for the
     * first 5 edges, so edge5-edge1 is exactly 4 bit-times. */
    if (!c->bit_time && c->edge_count >= 5) {
        c->bit_time = (c->edges[4].time - c->edges[0].time) / 4u;
        if (!c->bit_time) {
            abort_frame(c);
            return;
        }
        c->header_timer.func = header_ready_event;
        c->header_timer.waketime = c->frame_start + 30u * c->bit_time;
        sched_add_timer(&c->header_timer);
    }
}

uint8_t km_sim_tmc2209_chip_level(uint32_t pin)
{
    km_sim_tmc_chip_t *c = find_by_pin(pin);
    return c ? c->chip_level : 1;
}

int km_sim_tmc2209_set_fault(uint32_t pin, uint8_t on)
{
    km_sim_tmc_chip_t *c = find_by_pin(pin);
    if (!c) return -1;
    c->drv_status_fault = on ? 1 : 0;
    return 0;
}

static void attach(uint32_t pin, uint8_t addr)
{
    km_sim_tmc_chip_t *c = NULL;
    for (unsigned i = 0; i < KM_SIM_TMC_MAX_CHIPS; ++i)
        if (!chips[i].in_use) { c = &chips[i]; break; }
    if (!c)
        return;
    memset(c, 0, sizeof(*c));
    c->in_use = 1;
    c->pin = pin;
    c->addr = (uint8_t)(addr & 3u);
    c->chip_level = 1;
    c->senddelay_bits = 8;   /* Chip default before SLAVECONF is written. */
    for (unsigned i = 0; i < ARRAY_SIZE(km_tmc2209_regs); ++i) {
        const km_tmc2209_reg_t *r = &km_tmc2209_regs[i];
        if (r->readable && r->addr < KM_SIM_TMC_REG_COUNT) {
            c->have[r->addr] = 1;
            c->regs[r->addr] = 0;
        }
    }
    c->regs[0x06u] = 0x21u << 24;   /* IOIN: TMC2209 version byte. */
    c->have[0x06u] = 1;
    c->regs[0x01u] = 1;             /* GSTAT: reset flag set at power-on. */
    c->have[0x01u] = 1;
    c->regs[0x02u] = 0;             /* IFCNT starts at zero. */
    c->have[0x02u] = 1;
}

// One chip per single-wire UART pin, address 0 (mcu/Kconfig.tmc's AFC Lite
// default "0,0,0,0"): the BoxTurtle/AFC Lite preset's Feeder pins, in lane
// order (mcu/Kconfig.pins KLIPPER_MMU_PINS_UART default), plus EMU's own
// fifth-lane UART pin (PD10, tests/linux_mcu/configs/emu-h723-sim-motion.
// config, matching sim/board/plant.c's extended feeder tables). This sim
// board targets only that one topology (tests/linux_mcu/README.md); a
// different preset would need different pins here. Attaching a chip that
// a smaller-lane-count image never drives is harmless: gpio_in_read()
// checks the plant model first, and no plant pin table places a switch on
// PD10 (sim/board/plant.c).
void km_sim_tmc2209_init(void)
{
    static const uint32_t pins[KM_SIM_TMC_MAX_CHIPS] = {
        GPIO('D', 5), GPIO('D', 0), GPIO('E', 1), GPIO('C', 6), GPIO('D', 10)
    };
    for (unsigned i = 0; i < KM_SIM_TMC_MAX_CHIPS; ++i)
        attach(pins[i], 0);
}
DECL_INIT(km_sim_tmc2209_init);
