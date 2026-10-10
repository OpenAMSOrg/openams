// SPDX-License-Identifier: GPL-3.0-or-later
// Test-only: the sim board's control socket. See
// docs/plan/linux-mcu-integration.md, "A control socket": with the
// environment variable KM_SIM_CONTROL naming a path (unset means no socket
// at all), the sim MCU process serves a line-oriented Unix-domain socket so
// a test can inject trouble into the plant (sim/board/plant.c) and the TMC
// model (sim/board/tmc2209.c) that no G-code can reach, and read the
// plant's ground truth back. Nothing here exists in a release image: the
// object is built only for the sim board, and even there the socket is
// created, and the service timer registered, only when the environment asks
// for it (so preload_check's timer-gap report stays exactly as it was).
//
// Servicing runs from a self-rescheduling Klipper timer (bottom of this
// file), not from the MCU's main loop: every socket call is non-blocking
// and the callback never waits, so a slow or vanished client can delay the
// next command by at most one service period and can never stall the
// scheduler. The protocol is one command per line and exactly one reply
// line per command ("ok ..." or "error ..."), documented for users in
// tests/linux_mcu/README.md.
#include <errno.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/socket.h>
#include <sys/un.h>
#include "sched.h"               // DECL_INIT, struct timer, sched_add_timer, SF_*
#include "board/misc.h"          // timer_from_us, timer_read_time()
#include "stm32/internal.h"      // GPIO()
#include "plant.h"
#include "tmc2209.h"

#define KM_SIM_CONTROL_PERIOD_US 10000u    /* service ticks, all virtual: 10ms */
#define KM_SIM_CONTROL_MAX_CLIENTS 4
#define KM_SIM_CONTROL_LINE_MAX 127        /* longest accepted line, no newline */
/* A 4-lane non-selector truth reply needs ~900 bytes; a selector image's
 * per-lane fields are longer (selector_position_um=/servo_pulse_us=/
 * engaged_gate=/encoder_clicks=/encoder_um_per_click= repeat on every line)
 * and up to KM_MAX_LANES=12 lines, so both buffers are sized for that worst
 * case, not just the original 4. */
#define KM_SIM_CONTROL_OUT_MAX 4096         /* queued replies per client */
#define KM_SIM_CONTROL_REPLY_MAX 3584       /* one reply line; truth needs ~3400 */

// ---- Protocol: parsing ------------------------------------------------

static int parse_pin(const char *name, uint32_t *pin) {
    const char *p = name;
    if (*p == 'P' || *p == 'p') p++;
    char port = *p++;
    if (port >= 'a' && port <= 'h') port = (char)(port - 'a' + 'A');
    if (port < 'A' || port > 'H') return -1;
    int num = 0, digits = 0;
    while (digits < 2 && *p >= '0' && *p <= '9') {
        num = num * 10 + (*p - '0');
        p++;
        digits++;
    }
    if (!digits || *p || num > 15) return -1;
    *pin = GPIO(port, num);
    return 0;
}

static void pin_name(uint32_t pin, char *buf, size_t size) {
    snprintf(buf, size, "P%c%u", (char)('A' + pin / 16u), (unsigned)(pin % 16u));
}

// A lane number small enough not to wrap when narrowed to uint8_t; the
// plant's own setters are the authority on which lanes exist.
static int parse_lane(const char *s, uint8_t *lane) {
    char *end;
    errno = 0;
    unsigned long v = strtoul(s, &end, 10);
    if (errno || end == s || *end || v > 255) return -1;
    *lane = (uint8_t)v;
    return 0;
}

static int parse_i64(const char *s, int64_t *out) {
    char *end;
    errno = 0;
    long long v = strtoll(s, &end, 10);
    if (errno || end == s || *end) return -1;
    *out = (int64_t)v;
    return 0;
}

static int parse_u32(const char *s, uint32_t *out) {
    char *end;
    errno = 0;
    unsigned long v = strtoul(s, &end, 10);
    if (errno || end == s || *end || v > 0xfffffffful) return -1;
    *out = (uint32_t)v;
    return 0;
}

// ---- Protocol: one command line in, one reply line out -----------------
//
// reply is always either "ok ...\n" or "error ...\n", except for a blank
// line, which leaves it empty and is answered with nothing at all. An
// unrecognized verb and a recognized verb with unusable arguments are
// different errors on purpose: a test asserts the former.
static void handle_line(char *line, char *reply, size_t size) {
    char *save = NULL;
    char *cmd = strtok_r(line, " \t\r\n", &save);
    const char *a1 = strtok_r(NULL, " \t\r\n", &save);
    const char *a2 = strtok_r(NULL, " \t\r\n", &save);
    const char *a3 = strtok_r(NULL, " \t\r\n", &save);
    reply[0] = '\0';
    if (!cmd) return;

    if (!strcmp(cmd, "truth")) {
        km_sim_plant_format_truth(reply, size);
        return;
    }
    if (!strcmp(cmd, "plant")) {
        uint8_t lane;
        int64_t um;
        if (a1 && a2 && a3 && !strcmp(a1, "block") && !parse_lane(a2, &lane)
            && !parse_i64(a3, &um) && !km_sim_plant_set_block(lane, um)) {
            snprintf(reply, size, "ok plant block %u %lld\n",
                     (unsigned)lane, (long long)um);
            return;
        }
        if (a1 && a2 && !strcmp(a1, "unblock") && !parse_lane(a2, &lane)
            && !km_sim_plant_set_block(lane, -1)) {
            snprintf(reply, size, "ok plant unblock %u\n", (unsigned)lane);
            return;
        }
        if (a1 && a2 && !strcmp(a1, "runout") && !parse_lane(a2, &lane)
            && !km_sim_plant_set_runout(lane)) {
            snprintf(reply, size, "ok plant runout %u\n", (unsigned)lane);
            return;
        }
        snprintf(reply, size, "error bad argument\n");
        return;
    }
    if (!strcmp(cmd, "switch")) {
        uint32_t pin, period;
        int64_t level;
        char name[8];
        if (a1 && !parse_pin(a1, &pin) && a2) {
            pin_name(pin, name, sizeof(name));
            if (!strcmp(a2, "free")
                && !km_sim_plant_set_switch_override(
                       pin, KM_SIM_PLANT_SW_FREE, 0, 0)) {
                snprintf(reply, size, "ok switch %s free\n", name);
                return;
            }
            if (a3 && !strcmp(a2, "stuck") && !parse_i64(a3, &level)
                && (level == 0 || level == 1)
                && !km_sim_plant_set_switch_override(
                       pin, KM_SIM_PLANT_SW_STUCK, (uint8_t)level, 0)) {
                snprintf(reply, size, "ok switch %s stuck %u\n", name,
                         (unsigned)level);
                return;
            }
            if (a3 && !strcmp(a2, "bounce") && !parse_u32(a3, &period)
                && period
                && !km_sim_plant_set_switch_override(
                       pin, KM_SIM_PLANT_SW_BOUNCE, 0, period)) {
                snprintf(reply, size, "ok switch %s bounce %u\n", name, period);
                return;
            }
        }
        snprintf(reply, size, "error bad argument\n");
        return;
    }
    if (!strcmp(cmd, "tmc")) {
        uint32_t pin;
        char name[8];
        if (a1 && !parse_pin(a1, &pin) && a2 && a3 && !strcmp(a2, "fault")
            && (!strcmp(a3, "on") || !strcmp(a3, "off"))
            && !km_sim_tmc2209_set_fault(pin, !strcmp(a3, "on"))) {
            pin_name(pin, name, sizeof(name));
            snprintf(reply, size, "ok tmc %s fault %s\n", name, a3);
            return;
        }
        snprintf(reply, size, "error bad argument\n");
        return;
    }
    snprintf(reply, size, "error unknown command %s\n", cmd);
}

// ---- Transport: a non-blocking Unix-domain socket ----------------------
//
// One listening socket, a small fixed table of clients, and no blocking
// call anywhere: accept() is called until it reports EAGAIN, recv()/send()
// never wait, and a client that stops reading only loses its own queued
// replies. Every callback therefore returns in bounded time, which is what
// keeps the MCU's timers on schedule.

typedef struct {
    int fd;                          /* -1: free */
    unsigned in_len;
    uint8_t discarding;              /* mid-overlong-line: drop to the newline */
    char in[KM_SIM_CONTROL_LINE_MAX + 2];
    unsigned out_len;
    char out[KM_SIM_CONTROL_OUT_MAX];
} km_sim_control_client_t;

static int listen_fd = -1;
static km_sim_control_client_t clients[KM_SIM_CONTROL_MAX_CLIENTS];
static struct timer control_timer;

static void set_nonblocking(int fd) {
    int flags = fcntl(fd, F_GETFL, 0);
    if (flags >= 0) fcntl(fd, F_SETFL, flags | O_NONBLOCK);
}

static void client_close(km_sim_control_client_t *c) {
    if (c->fd >= 0) close(c->fd);
    c->fd = -1;
    c->in_len = 0;
    c->out_len = 0;
    c->discarding = 0;
}

// Queue one reply line. A client that never reads loses replies rather than
// growing this buffer or blocking the timer; a test tool that does that has
// no one to blame but itself.
static void queue_reply(km_sim_control_client_t *c, const char *text) {
    size_t len = strlen(text);
    if (!len || c->out_len + len > sizeof(c->out)) return;
    memcpy(c->out + c->out_len, text, len);
    c->out_len += (unsigned)len;
}

// Split every complete line out of the input buffer, answer each one, and
// keep whatever partial line is left for the next read.
static void consume_input(km_sim_control_client_t *c) {
    unsigned consumed = 0;
    for (;;) {
        char *nl = memchr(c->in + consumed, '\n', c->in_len - consumed);
        if (!nl) break;
        if (!c->discarding) {
            char reply[KM_SIM_CONTROL_REPLY_MAX];
            c->in[nl - c->in] = '\0';
            handle_line(c->in + consumed, reply, sizeof(reply));
            queue_reply(c, reply);
        }
        c->discarding = 0;
        consumed = (unsigned)(nl - c->in) + 1;
    }
    if (consumed) {
        memmove(c->in, c->in + consumed, c->in_len - consumed);
        c->in_len -= consumed;
    }
}

static void flush_client(km_sim_control_client_t *c) {
    while (c->out_len) {
        ssize_t n = send(c->fd, c->out, c->out_len, MSG_NOSIGNAL | MSG_DONTWAIT);
        if (n > 0) {
            memmove(c->out, c->out + n, c->out_len - (size_t)n);
            c->out_len -= (unsigned)n;
        } else if (n < 0 && errno == EINTR) {
            continue;
        } else if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            return;                       /* socket full: next tick */
        } else {
            client_close(c);              /* peer gone or unusable */
            return;
        }
    }
}

static void service_client(km_sim_control_client_t *c) {
    for (;;) {
        if (c->in_len == sizeof(c->in)) {
            /* A full buffer with no newline: this client is not speaking
             * the protocol. Say so once, then drop input to a newline. */
            c->in_len = 0;
            c->discarding = 1;
            queue_reply(c, "error line too long\n");
        }
        ssize_t n = recv(c->fd, c->in + c->in_len,
                         sizeof(c->in) - c->in_len, MSG_DONTWAIT);
        if (n > 0) {
            c->in_len += (unsigned)n;
            consume_input(c);
            continue;                     /* drain what is already waiting */
        }
        if (n == 0) { client_close(c); return; }        /* orderly EOF */
        if (errno == EINTR) continue;
        if (errno == EAGAIN || errno == EWOULDBLOCK) return;
        client_close(c);
        return;
    }
}

static void accept_clients(void) {
    for (;;) {
        int fd = accept(listen_fd, NULL, NULL);
        if (fd < 0) {
            if (errno == EINTR) continue;
            return;                       /* EAGAIN, or a dead listener */
        }
        set_nonblocking(fd);
        km_sim_control_client_t *slot = NULL;
        for (unsigned i = 0; i < KM_SIM_CONTROL_MAX_CLIENTS; ++i)
            if (clients[i].fd < 0) { slot = &clients[i]; break; }
        if (!slot) { close(fd); continue; }   /* full: refuse, never wait */
        slot->in_len = 0;
        slot->out_len = 0;
        slot->discarding = 0;
        slot->fd = fd;
    }
}

static void control_service(void) {
    accept_clients();
    for (unsigned i = 0; i < KM_SIM_CONTROL_MAX_CLIENTS; ++i) {
        km_sim_control_client_t *c = &clients[i];
        if (c->fd < 0) continue;
        service_client(c);
        if (c->fd >= 0) flush_client(c);
    }
}

static uint_fast8_t control_event(struct timer *timer) {
    control_service();
    timer->waketime += timer_from_us(KM_SIM_CONTROL_PERIOD_US);
    return SF_RESCHEDULE;
}

void km_sim_control_init(void);
void km_sim_control_init(void) {
    for (unsigned i = 0; i < KM_SIM_CONTROL_MAX_CLIENTS; ++i) clients[i].fd = -1;
    const char *path = getenv("KM_SIM_CONTROL");
    if (!path || !*path) return;          /* no socket asked for: nothing runs */
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    if (strlen(path) >= sizeof(addr.sun_path)) {
        fprintf(stderr, "km_sim_control: path too long for AF_UNIX: %s\n", path);
        return;
    }
    snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", path);
    listen_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (listen_fd < 0) {
        perror("km_sim_control: socket");
        return;
    }
    unlink(path);                         /* a leftover socket file */
    if (bind(listen_fd, (struct sockaddr *)&addr, sizeof(addr))
        || listen(listen_fd, KM_SIM_CONTROL_MAX_CLIENTS)) {
        perror("km_sim_control: bind/listen");
        close(listen_fd);
        listen_fd = -1;
        return;
    }
    set_nonblocking(listen_fd);
    control_timer.func = control_event;
    /* Relative to the clock, plus a full period: this init func runs after
     * simboard/timer.c's timer_init (see sim/board/Makefile's object order),
     * so the clock is real here, and a waketime already past would be a
     * "Timer too close" shutdown. */
    control_timer.waketime = timer_read_time()
        + timer_from_us(KM_SIM_CONTROL_PERIOD_US);
    sched_add_timer(&control_timer);
    fprintf(stderr, "km_sim_control: serving %s every %uus\n", path,
            (unsigned)KM_SIM_CONTROL_PERIOD_US);
}
DECL_INIT(km_sim_control_init);
