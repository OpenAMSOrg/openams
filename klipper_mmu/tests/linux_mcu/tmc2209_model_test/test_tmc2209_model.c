// SPDX-License-Identifier: GPL-3.0-or-later
// Native unit test for sim/board/tmc2209.c alone: drives it with synthetic
// edges (no Klipper scheduler, no gpio.c, no klippy) and checks it decodes
// requests and answers with correctly framed, CRC-valid replies. See
// tests/linux_mcu/README.md.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include "sched.h"
#include "tmc2209.h"
#include "klipper_mmu/tmc2209.h"
#include "klipper_mmu/tmc_uart_codec.h"

#define PIN_D5 53u  /* GPIO('D',5): lane 0's single-wire UART pin. */
#define PIN_D10 58u /* GPIO('D',10): EMU's fifth-lane single-wire UART pin. */
#define BIT_TIME 100u /* Arbitrary tick unit; only ratios matter here. */

/* --- Fake scheduler: a handful of pending timers, fired in waketime order. */
#define MAX_TIMERS 8
static struct timer *pending[MAX_TIMERS];
static int pending_count;

void sched_add_timer(struct timer *t)
{
    for (int i = 0; i < pending_count; ++i)
        if (pending[i] == t) return; /* Already tracked; waketime already updated. */
    assert(pending_count < MAX_TIMERS);
    pending[pending_count++] = t;
}

uint8_t timer_is_before(uint32_t a, uint32_t b) { return (int32_t)(a - b) < 0; }

/* Run every timer due at or before `now`, including any it reschedules,
 * until nothing is left due. Mirrors Klipper's own dispatch order closely
 * enough for this purpose: always fire the earliest pending timer first. */
static void run_until(uint32_t now)
{
    for (;;) {
        int best = -1;
        for (int i = 0; i < pending_count; ++i)
            if (!timer_is_before(now, pending[i]->waketime) &&
                (best < 0 || timer_is_before(pending[i]->waketime, pending[best]->waketime)))
                best = i;
        if (best < 0) return;
        struct timer *t = pending[best];
        uint_fast8_t r = t->func(t);
        if (r == SF_DONE) {
            pending[best] = pending[--pending_count];
        }
    }
}

/* --- Synthetic host TX: feed the model the same edges tmcuart_send_event
 * would produce for `raw`, at bit_time apart, starting at t0. Returns the
 * time the frame's last (stop) bit ends. */
static uint32_t drive_request(uint32_t pin, const uint8_t *raw, uint8_t raw_len, uint32_t t0)
{
    uint8_t encoded[KM_TMC_UART_ENCODED_MAX];
    uint8_t count = km_tmc_uart_encode(raw, raw_len, encoded);
    assert(count == raw_len * 10u / 8u);
    uint8_t bits = count * 8u;
    uint8_t level = 1; /* Idle high before the frame. */
    for (uint8_t pos = 0; pos < bits; ++pos) {
        uint8_t bit = (encoded[pos >> 3] >> (pos & 7u)) & 1u;
        if (bit != level) {
            level = bit;
            km_sim_tmc2209_notify_write(pin, level, t0 + pos * BIT_TIME);
        }
        run_until(t0 + pos * BIT_TIME); /* Let baud detection/header/frame timers fire as we go. */
    }
    uint32_t end = t0 + bits * BIT_TIME;
    run_until(end);
    return end;
}

/* --- Synthetic host RX: sample the chip's driven level at the midpoint of
 * each of the 80 reply bits starting at `reply_start`, exactly the way the
 * real tmcuart.c's mid-bit sampling (see docs/plan) would land once synced,
 * and decode the framed bytes back with the shared codec. */
static void read_reply(uint32_t pin, uint32_t reply_start, uint8_t raw_out[8])
{
    uint8_t encoded[KM_TMC_UART_ENCODED_MAX] = {0};
    for (uint8_t pos = 0; pos < 80; ++pos) {
        uint32_t sample_time = reply_start + pos * BIT_TIME + BIT_TIME / 2;
        run_until(sample_time);
        if (km_sim_tmc2209_chip_level(pin))
            encoded[pos >> 3] |= (uint8_t)(1u << (pos & 7u));
    }
    bool ok = km_tmc_uart_decode(encoded, KM_TMC_UART_ENCODED_MAX, raw_out);
    assert(ok);
}

static void check_crc(const uint8_t *raw, uint8_t len)
{
    assert(raw[len - 1] == km_tmc2209_crc(raw, (uint8_t)(len - 1)));
}

int main(void)
{
    km_sim_tmc2209_init(); /* Attaches 5 chips; lane 0 is PIN_D5, lane 4 is PIN_D10, both addr 0. */
    assert(km_sim_tmc2209_chip_level(PIN_D5) == 1); /* Idle high before any traffic. */
    assert(km_sim_tmc2209_chip_level(PIN_D10) == 1); /* Same for the fifth-lane chip. */

    uint32_t t = 1000;

    /* 1. Read IOIN (0x06): power-on default must report version 0x21. */
    {
        uint8_t req[4];
        assert(km_tmc2209_read_frame(0, 0x06, req) == KM_OK);
        uint32_t end = drive_request(PIN_D5, req, 4, t);
        uint8_t raw[8];
        read_reply(PIN_D5, end + 8 * BIT_TIME + BIT_TIME / 2, raw);
        check_crc(raw, 8);
        assert(raw[0] == 0x05 && raw[1] == 0xff && raw[2] == 0x06);
        assert(raw[3] == 0x21); /* IOIN top byte: TMC2209 version. */
        printf("PASS: IOIN read reports version 0x21\n");
        t = end + 20 * BIT_TIME;
    }

    /* 2. Write GCONF (0x00) then read it back: apply_write() must store the
     * exact value, and the reply datagram must carry it byte for byte. */
    {
        uint32_t value = 0x00000015u; /* A few plausible GCONF bits. */
        uint8_t wreq[8];
        assert(km_tmc2209_write_frame(0, 0x00, value, wreq) == KM_OK);
        uint32_t end = drive_request(PIN_D5, wreq, 8, t);
        t = end + 20 * BIT_TIME; /* Writes get no reply; just let it settle. */

        uint8_t rreq[4];
        assert(km_tmc2209_read_frame(0, 0x00, rreq) == KM_OK);
        end = drive_request(PIN_D5, rreq, 4, t);
        uint8_t raw[8];
        read_reply(PIN_D5, end + 8 * BIT_TIME + BIT_TIME / 2, raw);
        check_crc(raw, 8);
        uint32_t got = ((uint32_t)raw[3] << 24) | ((uint32_t)raw[4] << 16)
                     | ((uint32_t)raw[5] << 8) | raw[6];
        assert(got == value);
        printf("PASS: GCONF write is applied and read back exactly\n");
        t = end + 20 * BIT_TIME;
    }

    /* 3. Read IFCNT (0x02): exactly one accepted write so far. */
    {
        uint8_t req[4];
        assert(km_tmc2209_read_frame(0, 0x02, req) == KM_OK);
        uint32_t end = drive_request(PIN_D5, req, 4, t);
        uint8_t raw[8];
        read_reply(PIN_D5, end + 8 * BIT_TIME + BIT_TIME / 2, raw);
        check_crc(raw, 8);
        assert(raw[6] == 1); /* IFCNT low byte: one accepted write. */
        printf("PASS: IFCNT increments exactly once per accepted write\n");
        t = end + 20 * BIT_TIME;
    }

    /* 4. GSTAT (0x01) starts with the reset flag set; writing 1 clears it
     * (write-1-to-clear), not a literal overwrite. */
    {
        uint8_t rreq[4];
        assert(km_tmc2209_read_frame(0, 0x01, rreq) == KM_OK);
        uint32_t end = drive_request(PIN_D5, rreq, 4, t);
        uint8_t raw[8];
        read_reply(PIN_D5, end + 8 * BIT_TIME + BIT_TIME / 2, raw);
        check_crc(raw, 8);
        assert(raw[6] == 1); /* Power-on default: reset flag set. */
        t = end + 20 * BIT_TIME;

        uint8_t wreq[8];
        assert(km_tmc2209_write_frame(0, 0x01, 1, wreq) == KM_OK);
        end = drive_request(PIN_D5, wreq, 8, t);
        t = end + 20 * BIT_TIME;

        end = drive_request(PIN_D5, rreq, 4, t);
        read_reply(PIN_D5, end + 8 * BIT_TIME + BIT_TIME / 2, raw);
        check_crc(raw, 8);
        assert(raw[6] == 0); /* Cleared, not left at the written value. */
        printf("PASS: GSTAT write-1-to-clear semantics\n");
        t = end + 20 * BIT_TIME;
    }

    /* 5. A request to a foreign address gets no reply and no state change:
     * the line must stay idle high through where a reply would have been. */
    {
        uint8_t req[4];
        assert(km_tmc2209_read_frame(1, 0x06, req) == KM_OK); /* addr=1, this chip is addr=0 */
        uint32_t end = drive_request(PIN_D5, req, 4, t);
        run_until(end + 20 * BIT_TIME);
        assert(km_sim_tmc2209_chip_level(PIN_D5) == 1);
        printf("PASS: a foreign address gets no reply\n");
        t = end + 20 * BIT_TIME;
    }

    /* 6. Injected DRV_STATUS.ot (the control socket's "tmc <pin> fault on"):
     * the read of 0x6f klipper-mmu's own driver check polls must come back
     * with bit 1 set, and clearing the injection must clear it again. The
     * model has a register file and a reply framer but no stuck-low line
     * driver, so a fault is something the chip says, not something the wire
     * does -- see tests/linux_mcu/README.md. */
    {
        uint8_t req[4], raw[8];
        assert(km_tmc2209_read_frame(0, 0x6f, req) == KM_OK);
        uint32_t end = drive_request(PIN_D5, req, 4, t);
        read_reply(PIN_D5, end + 8 * BIT_TIME + BIT_TIME / 2, raw);
        check_crc(raw, 8);
        assert(raw[2] == 0x6f && (raw[6] & 0x02) == 0);
        t = end + 20 * BIT_TIME;

        assert(km_sim_tmc2209_set_fault(PIN_D5, 1) == 0);
        end = drive_request(PIN_D5, req, 4, t);
        read_reply(PIN_D5, end + 8 * BIT_TIME + BIT_TIME / 2, raw);
        check_crc(raw, 8);
        assert(raw[2] == 0x6f && (raw[6] & 0x02) != 0);
        printf("PASS: injected DRV_STATUS.ot shows up in a 0x6f read\n");
        t = end + 20 * BIT_TIME;

        assert(km_sim_tmc2209_set_fault(PIN_D5, 0) == 0);
        end = drive_request(PIN_D5, req, 4, t);
        read_reply(PIN_D5, end + 8 * BIT_TIME + BIT_TIME / 2, raw);
        check_crc(raw, 8);
        assert(raw[6] == 0);
        printf("PASS: clearing the injection clears DRV_STATUS.ot\n");
        t = end + 20 * BIT_TIME;
    }

    /* 7. EMU's fifth-lane chip (PIN_D10, added alongside the first four --
     * see sim/board/tmc2209.c's km_sim_tmc2209_init()) answers on its own
     * pin exactly like lane 0's chip does in check 1, and is a genuinely
     * separate chip: driving it does not touch PIN_D5's idle level. */
    {
        uint8_t req[4];
        assert(km_tmc2209_read_frame(0, 0x06, req) == KM_OK);
        uint32_t end = drive_request(PIN_D10, req, 4, t);
        assert(km_sim_tmc2209_chip_level(PIN_D5) == 1); /* Untouched by lane 4's traffic. */
        uint8_t raw[8];
        read_reply(PIN_D10, end + 8 * BIT_TIME + BIT_TIME / 2, raw);
        check_crc(raw, 8);
        assert(raw[0] == 0x05 && raw[1] == 0xff && raw[2] == 0x06);
        assert(raw[3] == 0x21); /* IOIN top byte: TMC2209 version. */
        printf("PASS: the fifth-lane chip (PIN_D10) answers independently\n");
    }

    printf("All tmc2209 model checks passed.\n");
    return 0;
}
