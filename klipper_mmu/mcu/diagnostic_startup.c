// SPDX-License-Identifier: GPL-3.0-or-later
#include "diagnostic_startup.h"
#include "autoconf.h"
#include "board_startup.h"
#include "boot_identity.h"
#include "clock.h"
#include "commands.h"
#include "controller.h"
#include "board/irq.h"
#include "board/misc.h"
#include "command.h"
#include "sched.h"
#include <stdbool.h>

/* Do not let a forced .config bypass a target-specific identity gate. */
#if !((defined(CONFIG_MACH_STM32H723) && CONFIG_MACH_STM32H723 \
       && defined(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H723) \
       && CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H723) \
      || (((defined(CONFIG_MACH_STM32F405) && CONFIG_MACH_STM32F405) \
           || (defined(CONFIG_MACH_STM32F407) && CONFIG_MACH_STM32F407) \
           || (defined(CONFIG_MACH_STM32F429) && CONFIG_MACH_STM32F429)) \
          && defined(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F4X5) \
          && CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F4X5 \
          && defined(CONFIG_USBSERIAL) && CONFIG_USBSERIAL \
          && (!defined(CONFIG_STM32_USB_PB14_PB15) \
              || !CONFIG_STM32_USB_PB14_PB15) \
          && CONFIG_CLOCK_FREQ == 168000000u \
          && (!defined(CONFIG_MACH_STM32F429) || !CONFIG_MACH_STM32F429 \
              || (defined(CONFIG_STM32_USB_PA11_PA12) && CONFIG_STM32_USB_PA11_PA12))) \
      || (defined(CONFIG_MACH_STM32F765) && CONFIG_MACH_STM32F765 \
          && defined(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F765) \
          && CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F765 \
          && defined(CONFIG_STM32_USB_PA11_PA12) && CONFIG_STM32_USB_PA11_PA12 \
          && CONFIG_CLOCK_FREQ == 216000000u) \
      || (defined(CONFIG_MACH_STM32F207) && CONFIG_MACH_STM32F207 \
          && defined(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F207) \
          && CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F207 \
          && defined(CONFIG_STM32_USB_PA11_PA12) && CONFIG_STM32_USB_PA11_PA12 \
          && CONFIG_CLOCK_FREQ == 120000000u) \
      || (((defined(CONFIG_MACH_STM32G431) && CONFIG_MACH_STM32G431) \
           || (defined(CONFIG_MACH_STM32G474) && CONFIG_MACH_STM32G474)) \
          && defined(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_G4) \
          && CONFIG_KLIPPER_MMU_BOOT_IDENTITY_G4 \
          && defined(CONFIG_USBSERIAL) && CONFIG_USBSERIAL \
          && defined(CONFIG_STM32_USB_PA11_PA12) && CONFIG_STM32_USB_PA11_PA12 \
          && CONFIG_CLOCK_FREQ == 170000000u \
          && (!defined(CONFIG_STM32_CLOCK_REF_25M) || !CONFIG_STM32_CLOCK_REF_25M)) \
      || (((defined(CONFIG_MACH_STM32H743) && CONFIG_MACH_STM32H743 \
            && CONFIG_CLOCK_FREQ == 400000000u) \
           || (defined(CONFIG_MACH_STM32H750) && CONFIG_MACH_STM32H750 \
               && CONFIG_CLOCK_FREQ == 480000000u)) \
          && defined(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H743_H750) \
          && CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H743_H750 \
          && defined(CONFIG_STM32_USB_PA11_PA12) && CONFIG_STM32_USB_PA11_PA12 \
          && (!defined(CONFIG_STM32_CLOCK_REF_INTERNAL) \
              || !CONFIG_STM32_CLOCK_REF_INTERNAL)) \
      || (defined(CONFIG_MACH_STM32) && CONFIG_MACH_STM32 \
          && defined(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_HOST) \
          && CONFIG_KLIPPER_MMU_BOOT_IDENTITY_HOST))
#error "diagnostic_startup.c requires a verified target-specific boot-identity provider"
#endif

#if defined(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_HOST) && CONFIG_KLIPPER_MMU_BOOT_IDENTITY_HOST
#define KM_DIAG_HOST_EPOCH 1
/* A forced .config must not pair a hardware provider with the host epoch. */
#if (defined(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H723) && CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H723) \
    || (defined(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F4X5) && CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F4X5) \
    || (defined(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F765) && CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F765) \
    || (defined(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F207) && CONFIG_KLIPPER_MMU_BOOT_IDENTITY_F207) \
    || (defined(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_G4) && CONFIG_KLIPPER_MMU_BOOT_IDENTITY_G4) \
    || (defined(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H743_H750) \
        && CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H743_H750)
#error "host epoch and a hardware boot-identity provider are mutually exclusive"
#endif
/* Host identity grants no peripheral route. Only targets with an existing
 * profile gate are admitted; their pin/GPIO/TMC gates still apply. */
#if !((defined(CONFIG_MACH_STM32H723) && CONFIG_MACH_STM32H723) \
      || (defined(CONFIG_MACH_STM32F207) && CONFIG_MACH_STM32F207) \
      || (defined(CONFIG_MACH_STM32F405) && CONFIG_MACH_STM32F405) \
      || (defined(CONFIG_MACH_STM32F407) && CONFIG_MACH_STM32F407) \
      || (defined(CONFIG_MACH_STM32F429) && CONFIG_MACH_STM32F429) \
      || (defined(CONFIG_MACH_STM32F765) && CONFIG_MACH_STM32F765) \
      || (defined(CONFIG_MACH_STM32G431) && CONFIG_MACH_STM32G431) \
      || (defined(CONFIG_MACH_STM32G474) && CONFIG_MACH_STM32G474) \
      || (defined(CONFIG_MACH_STM32H743) && CONFIG_MACH_STM32H743) \
      || (defined(CONFIG_MACH_STM32H750) && CONFIG_MACH_STM32H750) \
      || (defined(CONFIG_MACH_STM32G0B0) && CONFIG_MACH_STM32G0B0) \
      || (defined(CONFIG_MACH_STM32G0B1) && CONFIG_MACH_STM32G0B1))
#error "host-epoch diagnostic startup requires an admitted target profile gate"
#endif
#if ((defined(CONFIG_MACH_STM32G0B0) && CONFIG_MACH_STM32G0B0) \
     || (defined(CONFIG_MACH_STM32G0B1) && CONFIG_MACH_STM32G0B1)) \
    && (!defined(CONFIG_KLIPPER_MMU_G0B_BOOT_GATES) \
        || !CONFIG_KLIPPER_MMU_G0B_BOOT_GATES \
        || !defined(CONFIG_USBSERIAL) || !CONFIG_USBSERIAL \
        || !defined(CONFIG_STM32_USB_PA11_PA12) || !CONFIG_STM32_USB_PA11_PA12 \
        || CONFIG_CLOCK_FREQ != 64000000u)
#error "G0B host-epoch startup requires the 64 MHz USB PA11/PA12 profile gate"
#endif
#else
#define KM_DIAG_HOST_EPOCH 0
#endif

#if ((defined(CONFIG_MACH_STM32F207) && CONFIG_MACH_STM32F207) \
     || (defined(CONFIG_MACH_STM32F405) && CONFIG_MACH_STM32F405) \
     || (defined(CONFIG_MACH_STM32F407) && CONFIG_MACH_STM32F407) \
     || (defined(CONFIG_MACH_STM32F429) && CONFIG_MACH_STM32F429) \
     || (defined(CONFIG_MACH_STM32F765) && CONFIG_MACH_STM32F765) \
     || (defined(CONFIG_MACH_STM32G431) && CONFIG_MACH_STM32G431) \
     || (defined(CONFIG_MACH_STM32G474) && CONFIG_MACH_STM32G474) \
     || (defined(CONFIG_MACH_STM32H743) && CONFIG_MACH_STM32H743) \
     || (defined(CONFIG_MACH_STM32H750) && CONFIG_MACH_STM32H750)) \
    && (!defined(CONFIG_KLIPPER_MMU_LANE_COUNT) \
        || !((defined(CONFIG_KLIPPER_MMU_DEVICE_CUSTOM) \
              && CONFIG_KLIPPER_MMU_DEVICE_CUSTOM \
              && CONFIG_KLIPPER_MMU_LANE_COUNT == 1) \
             || (defined(CONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL) \
                 && CONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL \
                 && CONFIG_KLIPPER_MMU_LANE_COUNT == 4)) \
        || (defined(CONFIG_KLIPPER_MMU_POWERED_REWIND) \
            && CONFIG_KLIPPER_MMU_POWERED_REWIND) \
        || (defined(CONFIG_KLIPPER_MMU_DIFF_BUFFER) \
            && CONFIG_KLIPPER_MMU_DIFF_BUFFER))
#error "STM32 diagnostic startup requires Custom/1 or NightOwl/4 passive switched-buffer configuration"
#endif

/* G0B0/G0B1 additionally admit Angry Beaver (still passive, NightOwl's own
 * 4-lane shape) and ERCF 2.0/3.0 (Happy Hare's own MMB 2.0 board default for
 * both, installer/mmu_types/Kconfig.ercf:198,242 -- a LINEAR selector, its
 * own TMC UART, grip servo and encoder; mcu/Kconfig KLIPPER_MMU_G0B_BOOT_
 * GATES gates their compilation). Neither device uses powered rewind or the
 * analog buffer, so those stay refused here exactly like Custom/NightOwl. */
#if ((defined(CONFIG_MACH_STM32G0B0) && CONFIG_MACH_STM32G0B0) \
     || (defined(CONFIG_MACH_STM32G0B1) && CONFIG_MACH_STM32G0B1)) \
    && (!defined(CONFIG_KLIPPER_MMU_LANE_COUNT) \
        || !((defined(CONFIG_KLIPPER_MMU_DEVICE_CUSTOM) \
              && CONFIG_KLIPPER_MMU_DEVICE_CUSTOM \
              && CONFIG_KLIPPER_MMU_LANE_COUNT == 1) \
             || (defined(CONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL) \
                 && CONFIG_KLIPPER_MMU_DEVICE_NIGHTOWL \
                 && CONFIG_KLIPPER_MMU_LANE_COUNT == 4) \
             || (defined(CONFIG_KLIPPER_MMU_DEVICE_ANGRY_BEAVER) \
                 && CONFIG_KLIPPER_MMU_DEVICE_ANGRY_BEAVER \
                 && CONFIG_KLIPPER_MMU_LANE_COUNT == 4) \
             || (defined(CONFIG_KLIPPER_MMU_DEVICE_ERCF_2_0) \
                 && CONFIG_KLIPPER_MMU_DEVICE_ERCF_2_0 \
                 && CONFIG_KLIPPER_MMU_LANE_COUNT == 8) \
             || (defined(CONFIG_KLIPPER_MMU_DEVICE_ERCF_3_0) \
                 && CONFIG_KLIPPER_MMU_DEVICE_ERCF_3_0 \
                 && CONFIG_KLIPPER_MMU_LANE_COUNT == 8)) \
        || (defined(CONFIG_KLIPPER_MMU_POWERED_REWIND) \
            && CONFIG_KLIPPER_MMU_POWERED_REWIND) \
        || (defined(CONFIG_KLIPPER_MMU_DIFF_BUFFER) \
            && CONFIG_KLIPPER_MMU_DIFF_BUFFER))
#error "G0B diagnostic startup requires Custom/1 or NightOwl/4 passive switched-buffer configuration, or Angry Beaver/4 or ERCF 2.0/3.0/8"
#endif

#if defined(CONFIG_KLIPPER_MMU_DIFF_BUFFER) && CONFIG_KLIPPER_MMU_DIFF_BUFFER
#if !defined(CONFIG_MACH_STM32H723) || !CONFIG_MACH_STM32H723 \
    || !defined(CONFIG_WANT_ADC) || !CONFIG_WANT_ADC \
    || !defined(CONFIG_WANT_ADC_LOCAL) || !CONFIG_WANT_ADC_LOCAL
#error "Diff diagnostic startup requires H723 local ADC support"
#endif
#include "calibration.h"
#endif

/* Timeout ticks must fit the unsigned half range. Menu range is not sufficient.
 * H723 at 520 MHz: 1e6 us * 520 ticks/us = 5.2e8, below 2^31. */
#define KM_DIAG_TICKS_PER_US ((uint64_t)(CONFIG_CLOCK_FREQ / 1000000u))
#define KM_DIAG_TIMEOUT_TICKS \
    ((uint64_t)CONFIG_KLIPPER_MMU_DIAGNOSTIC_TIMEOUT_US * KM_DIAG_TICKS_PER_US)

_Static_assert(CONFIG_CLOCK_FREQ >= 1000000u, "diagnostic clock below 1 MHz");
_Static_assert(KM_DIAG_TICKS_PER_US > 0u, "diagnostic tick conversion is zero");
_Static_assert(CONFIG_KLIPPER_MMU_DIAGNOSTIC_TIMEOUT_US >= 1000
               && CONFIG_KLIPPER_MMU_DIAGNOSTIC_TIMEOUT_US <= 1000000,
               "diagnostic timeout outside 1000..1000000 us");
_Static_assert(CONFIG_KLIPPER_MMU_DIAGNOSTIC_TIMEOUT_US < (1u << 31),
               "diagnostic timeout exceeds board horizon");
_Static_assert(KM_DIAG_TIMEOUT_TICKS < (1ull << 31),
               "diagnostic timeout exceeds raw tick half horizon");
_Static_assert(KM_DIAG_TIMEOUT_TICKS
               == (uint64_t)((uint32_t)CONFIG_KLIPPER_MMU_DIAGNOSTIC_TIMEOUT_US
                             * (uint32_t)(CONFIG_CLOCK_FREQ / 1000000u)),
               "diagnostic timeout tick conversion does not fit uint32");

DECL_CONSTANT("KLIPPER_MMU_DIAGNOSTIC_STARTUP", 1);
#if KM_DIAG_HOST_EPOCH
DECL_CONSTANT("KLIPPER_MMU_BOOT_IDENTITY_HOST", 1);
#endif

#if defined(CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN) && CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN
#if !defined(CONFIG_KLIPPER_MMU_DIAGNOSTIC_STARTUP) || !CONFIG_KLIPPER_MMU_DIAGNOSTIC_STARTUP
#error "Actuator permission requires verified hosted diagnostic startup"
#endif
#define KM_DIAG_ACTUATION 1
#else
#define KM_DIAG_ACTUATION 0
#endif

static enum km_diagnostic_state diag_state = KM_DIAGNOSTIC_IDLE;
static km_result_t diag_result = KM_ERR_UNAVAILABLE;
/* A failure before the board began; afterwards the board's own detail names it. */
static enum km_startup_step diag_step = KM_STARTUP_STEP_NONE;
static bool diag_faulted;
static bool board_owned;
static bool board_begun;
static bool have_identity;
static bool commands_owned;
static km_operation_core_t *attached_core;

#if KM_DIAG_ACTUATION
static bool diagnostic_board_ready(void)
{
    return attached_core && km_board_startup_get_core() == attached_core;
}
#endif
static uint64_t verified_id;
static uint32_t start_tick;
static bool timer_armed;
static struct timer diag_timer;
static struct task_wake diag_wake;

static uint32_t timeout_ticks(void)
{
    return (uint32_t)CONFIG_KLIPPER_MMU_DIAGNOSTIC_TIMEOUT_US
        * (uint32_t)(CONFIG_CLOCK_FREQ / 1000000u);
}

static bool deadline_reached(void)
{
    uint32_t now = timer_read_time();
    return (uint32_t)(now - start_tick) >= timeout_ticks();
}

static uint32_t remaining_timeout_us(void)
{
    uint32_t now = timer_read_time();
    uint32_t elapsed = (uint32_t)(now - start_tick);
    uint32_t limit = timeout_ticks();
    uint32_t per_us = (uint32_t)(CONFIG_CLOCK_FREQ / 1000000u);
    if (elapsed >= limit || per_us == 0)
        return 0;
    return (limit - elapsed) / per_us;
}

/* IRQs disabled. Scheduler reset during global shutdown must not be deleted. */
static void disarm_timer_locked(void)
{
    if (!timer_armed || sched_is_shutdown()) {
        timer_armed = false;
        return;
    }
    timer_armed = false;
    sched_del_timer(&diag_timer);
}

static void note_fault(km_result_t err)
{
    irqstatus_t flag = irq_save();
    if (!diag_faulted) {
        diag_faulted = true;
        diag_state = KM_DIAGNOSTIC_FAULTED;
        if (diag_result == KM_OK || diag_result == KM_ERR_UNAVAILABLE)
            diag_result = (err != KM_OK && err != KM_ERR_UNAVAILABLE) ? err : KM_ERR_FAULTED;
    }
    disarm_timer_locked();
    irq_restore(flag);
}

static bool still_waiting(void)
{
    irqstatus_t flag = irq_save();
    bool waiting = diag_state == KM_DIAGNOSTIC_WAITING && !diag_faulted && !sched_is_shutdown();
    irq_restore(flag);
    return waiting;
}

static void safe_off_owned(void)
{
    if (commands_owned)
        km_commands_revoke_start(attached_core, verified_id);
    if (board_owned)
        km_board_startup_shutdown();
}

static uint_fast8_t diag_timer_event(struct timer *t)
{
    /* Timer context: wake only. No setup, attach, or sendf. */
    if (diag_faulted || sched_is_shutdown() || diag_state != KM_DIAGNOSTIC_WAITING) {
        timer_armed = false;
        return SF_DONE;
    }
    sched_wake_task(&diag_wake);
    t->waketime = timer_read_time() + timer_from_us(1000);
    return SF_RESCHEDULE;
}

/* IRQs disabled, state IDLE, not faulted or shut down. */
static void begin_startup_locked(void)
{
    start_tick = timer_read_time();
    diag_state = KM_DIAGNOSTIC_WAITING;
    diag_result = KM_ERR_UNAVAILABLE;
    diag_timer.func = diag_timer_event;
    diag_timer.waketime = start_tick + timer_from_us(1000);
    timer_armed = true;
    sched_add_timer(&diag_timer);
    sched_wake_task(&diag_wake);
}

#if KM_DIAG_HOST_EPOCH
/* One binding per MCU lifetime. Only an unbound IDLE firmware accepts an
 * epoch; any later value, including after disconnect, fault, or shutdown, is
 * ignored. The epoch is a lifetime discriminator, not authentication. */
void command_km_diagnostic_start_host(uint32_t *args)
{
    uint64_t epoch = ((uint64_t)args[0] << 32) | args[1];
    irqstatus_t flag = irq_save();
    if (have_identity || diag_faulted || sched_is_shutdown()
        || diag_state != KM_DIAGNOSTIC_IDLE) {
        irq_restore(flag);
        return;
    }
    if (!epoch) {
        diag_faulted = true;
        diag_state = KM_DIAGNOSTIC_FAULTED;
        diag_result = KM_ERR_INVALID_PARAM;
        irq_restore(flag);
        return;
    }
    verified_id = epoch;
    have_identity = true;
    begin_startup_locked();
    irq_restore(flag);
}
DECL_COMMAND(command_km_diagnostic_start_host,
             "km_diagnostic_start_host epoch_hi=%u epoch_lo=%u");
#else
void command_km_diagnostic_start(uint32_t *args)
{
    (void)args;
    irqstatus_t flag = irq_save();
    if (diag_faulted || sched_is_shutdown() || diag_state != KM_DIAGNOSTIC_IDLE) {
        irq_restore(flag);
        return;
    }
    begin_startup_locked();
    irq_restore(flag);
}
DECL_COMMAND(command_km_diagnostic_start, "km_diagnostic_start");
#endif

void command_km_diagnostic_query(uint32_t *args)
{
    irqstatus_t flag = irq_save();
    enum km_diagnostic_state st = diag_state;
    km_result_t result = diag_result;
    if (diag_faulted || sched_is_shutdown()) {
        st = KM_DIAGNOSTIC_FAULTED;
        if (result == KM_OK || result == KM_ERR_UNAVAILABLE)
            result = KM_ERR_FAULTED;
    }
    irq_restore(flag);
    sendf("km_diagnostic_state challenge=%u state=%c result=%i",
          args[0], (uint32_t)st, (int32_t)result);
}
DECL_COMMAND_FLAGS(command_km_diagnostic_query, HF_IN_SHUTDOWN,
                   "km_diagnostic_query challenge=%u");

/* Where startup stopped, for the host's plain-language report. */
void command_km_diagnostic_detail_query(uint32_t *args)
{
    km_board_startup_detail_t d;
    km_board_startup_get_detail(&d);
    irqstatus_t flag = irq_save();
    km_result_t result = diag_result;
    if (diag_step != KM_STARTUP_STEP_NONE)
        d = (km_board_startup_detail_t){.step = (uint8_t)diag_step,
            .lane = KM_STARTUP_NO_LANE, .pin = {KM_STARTUP_NO_PIN, KM_STARTUP_NO_PIN}};
    irq_restore(flag);
    int32_t cause = d.cause ? d.cause : (int32_t)result;
    sendf("km_diagnostic_detail challenge=%u step=%c lane=%c cause=%i reason=%c"
          " pin0=%hu role0=%c lane0=%c pin1=%hu role1=%c lane1=%c",
          args[0], d.step, d.lane, cause, d.reason, d.pin[0], d.role[0],
          d.pin_lane[0], d.pin[1], d.role[1], d.pin_lane[1]);
}
DECL_COMMAND_FLAGS(command_km_diagnostic_detail_query, HF_IN_SHUTDOWN,
                   "km_diagnostic_detail_query challenge=%u");

enum km_diagnostic_state km_diagnostic_get_state(void)
{
    irqstatus_t flag = irq_save();
    enum km_diagnostic_state st = diag_state;
    if (diag_faulted || sched_is_shutdown())
        st = KM_DIAGNOSTIC_FAULTED;
    irq_restore(flag);
    return st;
}

km_result_t km_diagnostic_get_result(void)
{
    irqstatus_t flag = irq_save();
    km_result_t result = diag_result;
    if (diag_faulted || sched_is_shutdown()) {
        if (result == KM_OK || result == KM_ERR_UNAVAILABLE)
            result = KM_ERR_FAULTED;
    }
    irq_restore(flag);
    return result;
}

static bool fail_if_late(void)
{
    if (!still_waiting()) {
        safe_off_owned();
        return true;
    }
    if (deadline_reached()) {
        note_fault(KM_ERR_TIMEOUT);
        safe_off_owned();
        return true;
    }
    return false;
}

static void fail_board(km_result_t err)
{
    note_fault(err);
    safe_off_owned();
}

static bool wait_clock_and_identity(void)
{
    enum km_clock_state clock = km_clock_get_state();
    if (clock == KM_CLOCK_STATE_UNAVAILABLE)
        return false;
    if (clock != KM_CLOCK_STATE_READY) {
        diag_step = KM_STARTUP_STEP_CLOCK;
        fail_board(KM_ERR_FAULTED);
        return false;
    }
    if (!have_identity) {
        diag_step = KM_STARTUP_STEP_IDENTITY;
#if KM_DIAG_HOST_EPOCH
        /* Unreachable: the host command binds before WAITING. No fallback. */
        fail_board(KM_ERR_FAULTED);
        return false;
#else
        uint64_t id = 0;
        km_result_t got = km_boot_identity_get(&id);
        if (got == KM_ERR_UNAVAILABLE)
            return false;
        if (got != KM_OK || id == 0) {
            fail_board(got != KM_OK ? got : KM_ERR_FAULTED);
            return false;
        }
        verified_id = id;
        have_identity = true;
        diag_step = KM_STARTUP_STEP_NONE;
#endif
    }
    return true;
}

static bool begin_owned_board(void)
{
    if (km_board_startup_get_state() != KM_BOARD_STARTUP_UNINITIALIZED) {
        diag_step = KM_STARTUP_STEP_ALREADY_STARTED;
        note_fault(KM_ERR_BUSY);
        return false;
    }
    uint32_t remaining = remaining_timeout_us();
    if (remaining == 0 || remaining >= (1u << 31)) {
        diag_step = KM_STARTUP_STEP_DEADLINE;
        note_fault(KM_ERR_TIMEOUT);
        return false;
    }
    const km_diff_adc_config_t *calibration = NULL;
#if defined(CONFIG_KLIPPER_MMU_DIFF_BUFFER) && CONFIG_KLIPPER_MMU_DIFF_BUFFER
    calibration = km_diff_calibration_get();
    if (!calibration) {
        diag_step = KM_STARTUP_STEP_CALIBRATION;
        note_fault(KM_ERR_FAULTED);
        return false;
    }
#endif
    km_result_t began = km_board_startup_begin(NULL, 0, remaining, calibration);
    if (began == KM_OK)
        board_owned = true;
    if (!still_waiting()) {
        safe_off_owned();
        return false;
    }
    if (deadline_reached()) {
        fail_board(KM_ERR_TIMEOUT);
        return false;
    }
    if (began != KM_OK) {
        note_fault(began);
        return false;
    }
    board_begun = true;
    return true;
}

static bool advance_owned_board(void)
{
    enum km_board_startup_state board = km_board_startup_get_state();
    if (board == KM_BOARD_STARTUP_WAITING) {
        km_result_t serviced = km_board_startup_service();
        if (fail_if_late())
            return false;
        if (serviced == KM_ERR_UNAVAILABLE)
            return false;
        if (serviced != KM_OK) {
            fail_board(serviced != KM_OK ? serviced : KM_ERR_FAULTED);
            return false;
        }
        board = km_board_startup_get_state();
    }
    if (board != KM_BOARD_STARTUP_READY || km_board_startup_get_result() != KM_OK) {
        km_result_t board_result = km_board_startup_get_result();
        fail_board(board_result != KM_OK && board_result != KM_ERR_UNAVAILABLE
                   ? board_result : KM_ERR_FAULTED);
        return false;
    }
    return true;
}

static void attach_and_publish(void)
{
    if (fail_if_late())
        return;
    if (km_clock_get_state() != KM_CLOCK_STATE_READY) {
        fail_board(KM_ERR_FAULTED);
        return;
    }
    km_operation_core_t *core = km_board_startup_get_core();
    if (!core || !have_identity || verified_id == 0) {
        fail_board(KM_ERR_FAULTED);
        return;
    }
    km_result_t attached = km_commands_attach(core, verified_id, km_controller_changed, false);
    if (attached == KM_OK) {
        attached_core = core;
        commands_owned = true;
    }
    if (!still_waiting()) {
        /* Attachment, when it succeeded, stays. No unbind API. Board still safe-off. */
        safe_off_owned();
        return;
    }
    if (deadline_reached()) {
        fail_board(KM_ERR_TIMEOUT);
        return;
    }
    if (attached != KM_OK) {
        fail_board(attached);
        return;
    }
    if (km_board_startup_get_state() != KM_BOARD_STARTUP_READY
        || km_board_startup_get_result() != KM_OK
        || km_clock_get_state() != KM_CLOCK_STATE_READY) {
        fail_board(KM_ERR_FAULTED);
        return;
    }
    irqstatus_t flag = irq_save();
    if (diag_faulted || sched_is_shutdown() || diag_state != KM_DIAGNOSTIC_WAITING) {
        irq_restore(flag);
        safe_off_owned();
        return;
    }
    if (deadline_reached()) {
        diag_faulted = true;
        diag_state = KM_DIAGNOSTIC_FAULTED;
        if (diag_result == KM_OK || diag_result == KM_ERR_UNAVAILABLE)
            diag_result = KM_ERR_TIMEOUT;
        disarm_timer_locked();
        irq_restore(flag);
        safe_off_owned();
        return;
    }
    diag_state = KM_DIAGNOSTIC_READY;
    diag_result = KM_OK;
    disarm_timer_locked();
    irq_restore(flag);
#if KM_DIAG_ACTUATION
    km_result_t enabled = km_commands_enable_start(core, verified_id, diagnostic_board_ready);
    if (enabled != KM_OK || sched_is_shutdown()
        || km_board_startup_get_state() != KM_BOARD_STARTUP_READY
        || km_board_startup_get_result() != KM_OK) {
        fail_board(enabled != KM_OK ? enabled : KM_ERR_FAULTED);
    }
#endif
}

void km_diagnostic_task(void)
{
    if (!sched_check_wake(&diag_wake))
        return;
    if (!still_waiting())
        return;
    if (deadline_reached()) {
        note_fault(KM_ERR_TIMEOUT);
        safe_off_owned();
        return;
    }
    if (!wait_clock_and_identity())
        return;
    if (fail_if_late())
        return;
    if (!board_begun && !begin_owned_board())
        return;
    if (!board_begun)
        return;
    if (!advance_owned_board())
        return;
    attach_and_publish();
}
DECL_TASK(km_diagnostic_task);

void km_diagnostic_shutdown(void)
{
    /* Clock-free. User timers are already cleared. Do not shut down a foreign board. */
    irqstatus_t flag = irq_save();
    diag_faulted = true;
    diag_state = KM_DIAGNOSTIC_FAULTED;
    if (diag_result == KM_OK || diag_result == KM_ERR_UNAVAILABLE)
        diag_result = KM_ERR_FAULTED;
    timer_armed = false;
    irq_restore(flag);
    if (commands_owned)
        km_commands_revoke_start(attached_core, verified_id);
}
DECL_SHUTDOWN(km_diagnostic_shutdown);
