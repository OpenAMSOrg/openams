// SPDX-License-Identifier: GPL-3.0-or-later
#include "tmc_feeder.h"
#include "autoconf.h"
#include "klipper_mmu/tmc_uart_config.h"

typedef struct {
    bool claimed;
    bool uart_started;
    bool activated;
    bool restarting;
    uint32_t slot;
    uint16_t run_ma, hold_ma;
} feeder_lane_t;

static feeder_lane_t lanes[KM_MAX_LANES];

static bool pin_matches(uint32_t pin, const uint32_t pins[4])
{
    for (unsigned i = 0; i < 4u; ++i)
        if (pin == pins[i]) return true;
    return false;
}

static km_result_t claims_available(uint32_t slot, const uint32_t pins[4])
{
    const uint32_t step_pin = pins[0], dir_pin = pins[1], enable_pin = pins[2];
    for (uint32_t i = 0; i < KM_MAX_LANES; ++i) {
        uint32_t step, dir, enable, uart;
        if (km_stepdir_gpio_get_slot_pins(i, &step, &dir, &enable) == KM_OK) {
            if (i == slot) return KM_ERR_BUSY;
            if (pin_matches(step, pins) || pin_matches(dir, pins)
                || pin_matches(enable, pins)) return KM_ERR_CONFLICT;
        }
        if (km_tmc_uart_gpio_get_pin(i, &uart) == KM_OK) {
            if (i == slot) return KM_ERR_BUSY;
            /* An existing lane's UART pin landing on this slot's own
             * STEP/DIR/ENABLE is always a conflict. Landing on this slot's
             * own UART pin is the one legitimate shared single-wire TMC2209
             * case (Happy Hare's EASY-BRD wiring, docs/selectors.md);
             * km_tmc_uart_gpio_setup is the sole authority there and still
             * refuses two lanes sharing one pin at the same TMC address. */
            if (uart == step_pin || uart == dir_pin || uart == enable_pin)
                return KM_ERR_CONFLICT;
        }
    }
    return KM_OK;
}

static km_result_t lane_status(const feeder_lane_t *lane, km_tmc_uart_status_t *status)
{
    if (!lane || !lane->uart_started) return KM_ERR_UNAVAILABLE;
    return km_tmc_uart_gpio_get_status(lane->slot, status);
}

static bool status_ready(const feeder_lane_t *lane, const km_tmc_uart_status_t *st)
{
    return st->ready && st->configured && st->fault == KM_FAULT_NONE
        && st->requested_run_ma == lane->run_ma
        && st->requested_hold_ma == lane->hold_ma
        && st->confirmed_run_ma == lane->run_ma
        && st->confirmed_hold_ma == lane->hold_ma;
}

static bool lane_ready(void *context)
{
    feeder_lane_t *lane = (feeder_lane_t *)context;
    km_tmc_uart_status_t st;
    return lane_status(lane, &st) == KM_OK && status_ready(lane, &st);
}

static km_fault_t lane_fault(void *context)
{
    feeder_lane_t *lane = (feeder_lane_t *)context;
    km_tmc_uart_status_t st;
    if (lane_status(lane, &st) != KM_OK) return KM_FAULT_DRIVER_FAULT;
    if (st.fault != KM_FAULT_NONE) return st.fault;
    /* A ready UART with different current is a broken fixed-current contract. */
    if (st.ready && (st.requested_run_ma != lane->run_ma
                     || st.requested_hold_ma != lane->hold_ma
                     || st.confirmed_run_ma != lane->run_ma
                     || st.confirmed_hold_ma != lane->hold_ma))
        return KM_FAULT_DRIVER_FAULT;
    return KM_FAULT_NONE;
}

KM_API km_result_t km_tmc_feeder_setup(uint32_t slot,
                                       uint32_t step_pin, uint32_t dir_pin,
                                       uint32_t enable_pin, uint32_t uart_pin,
                                       bool uart_pull_up,
                                       const km_step_dir_config_t *step_config,
                                       const km_tmc2209_config_t *tmc_config,
                                       uint16_t expected_run_ma,
                                       uint16_t expected_hold_ma)
{
    if (slot >= KM_MAX_LANES || !step_config || !tmc_config)
        return KM_ERR_INVALID_PARAM;
    if (lanes[slot].claimed) return KM_ERR_BUSY;
    const uint32_t pins[4] = {step_pin, dir_pin, enable_pin, uart_pin};
    for (unsigned i = 0; i < 4u; ++i) {
        if (pins[i] >= 128u) return KM_ERR_INVALID_PARAM;
        for (unsigned j = 0; j < i; ++j)
            if (pins[i] == pins[j]) return KM_ERR_INVALID_PARAM;
    }
    if (tmc_config->run_current_ma != expected_run_ma
        || tmc_config->hold_current_ma != expected_hold_ma
        || km_step_dir_config_validate(step_config) != KM_OK)
        return KM_ERR_INVALID_PARAM;
    const km_tmc_uart_config_t uart_config = {
        .tmc = *tmc_config, .clock_hz = CONFIG_CLOCK_FREQ
    };
    if (km_tmc_uart_config_validate(&uart_config) != KM_OK)
        return KM_ERR_INVALID_PARAM;
    km_result_t res = claims_available(slot, pins);
    if (res != KM_OK) return res;

    feeder_lane_t *lane = &lanes[slot];
    lane->slot = slot;
    lane->run_ma = expected_run_ma;
    lane->hold_ma = expected_hold_ma;
    void *safe_context = NULL;
    res = km_stepdir_gpio_prepare(slot, step_pin, dir_pin, enable_pin,
                                  step_config, lane_ready, lane_fault, lane,
                                  &safe_context);
    if (res != KM_OK) return res;
    lane->claimed = true; /* STEP/DIR pins are now immutable, even on UART failure. */
    res = km_tmc_uart_gpio_setup(slot, uart_pin, uart_pull_up, tmc_config,
                                 km_stepdir_gpio_force_off, safe_context);
    if (res != KM_OK) return res;
    lane->uart_started = true;
    return KM_OK;
}

KM_API km_result_t km_tmc_feeder_activate(uint32_t slot)
{
    if (slot >= KM_MAX_LANES) return KM_ERR_INVALID_PARAM;
    feeder_lane_t *lane = &lanes[slot];
    if (!lane->claimed) return KM_ERR_UNAVAILABLE;
    if (lane->activated) return KM_ERR_BUSY;
    if (!lane->uart_started) return KM_ERR_FAULTED;
    km_tmc_uart_status_t st;
    if (lane_status(lane, &st) != KM_OK) return KM_ERR_FAULTED;
    if (st.fault != KM_FAULT_NONE) return KM_ERR_FAULTED;
    if (!status_ready(lane, &st)) return KM_ERR_UNAVAILABLE;
    km_result_t res = km_stepdir_gpio_activate(slot);
    if (res == KM_OK) {
        /* Activation rechecks the same fixed-current predicate before publish.
         * Only now may the public driver report confirmed register settings. */
        km_driver_t *driver = km_stepdir_gpio_get_driver(slot);
        if (!driver) return KM_ERR_FAULTED;
        driver->run_current_ma = st.confirmed_run_ma;
        driver->hold_current_ma = st.confirmed_hold_ma;
        lane->activated = true;
    }
    return res;
}

KM_API km_result_t km_tmc_feeder_restart(uint32_t slot)
{
    if (slot >= KM_MAX_LANES) return KM_ERR_INVALID_PARAM;
    feeder_lane_t *lane = &lanes[slot];
    if (!lane->activated) return KM_ERR_UNAVAILABLE;
    if (!lane->restarting) {
        /* Forces the actuator off, which also latches the STEP/DIR fault. */
        km_result_t res = km_tmc_uart_gpio_restart(slot);
        lane->restarting = res == KM_OK;
        return res == KM_OK ? KM_ERR_UNAVAILABLE : res;
    }
    km_tmc_uart_status_t st;
    km_result_t res = lane_status(lane, &st);
    if (res == KM_OK && st.fault != KM_FAULT_NONE) res = KM_ERR_FAULTED;
    if (res == KM_OK && !status_ready(lane, &st)) return KM_ERR_UNAVAILABLE;
    if (res == KM_OK) res = km_stepdir_gpio_clear_fault(slot);
    /* A fault landing after the status read is reported by the next call. */
    if (res != KM_ERR_UNAVAILABLE) lane->restarting = false;
    return res;
}

KM_API km_driver_t *km_tmc_feeder_get_driver(uint32_t slot)
{
    if (slot >= KM_MAX_LANES || !lanes[slot].activated) return NULL;
    return km_stepdir_gpio_get_driver(slot);
}
