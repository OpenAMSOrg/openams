// SPDX-License-Identifier: GPL-3.0-or-later
// Register definitions/CRC/current equation: TMC2209 datasheet rev1.09.
#include "klipper_mmu/tmc2209.h"
#include <string.h>

static const uint8_t write_registers[KM_TMC2209_CONFIG_WRITES] = {
    0x22, 0x00, 0x03, 0x6c, 0x10, 0x11, 0x13, 0x14, 0x42, 0x70, 0x01
};

uint8_t km_tmc2209_crc(const uint8_t *bytes, uint8_t length)
{
    uint8_t crc = 0;
    if (!bytes) return 0;
    for (uint8_t i = 0; i < length; ++i) {
        uint8_t value = bytes[i];
        for (unsigned bit = 0; bit < 8; ++bit) {
            unsigned feedback = ((unsigned)crc >> 7) ^ (value & 1u);
            crc = (uint8_t)((unsigned)crc << 1);
            if (feedback) crc ^= 0x07u;
            value >>= 1;
        }
    }
    return crc;
}
km_result_t km_tmc2209_read_frame(uint8_t address, uint8_t reg, uint8_t out[4])
{
    if (!out || address > 3 || reg > 0x7f) return KM_ERR_INVALID_PARAM;
    out[0] = 0x05; out[1] = address; out[2] = reg;
    out[3] = km_tmc2209_crc(out, 3);
    return KM_OK;
}
km_result_t km_tmc2209_write_frame(uint8_t address, uint8_t reg, uint32_t value, uint8_t out[8])
{
    if (!out || address > 3 || reg > 0x7f || reg == 0x04 || reg == 0x07)
        return KM_ERR_INVALID_PARAM; /* Never expose OTP/factory programming. */
    out[0] = 0x05; out[1] = address; out[2] = reg | 0x80u;
    for (unsigned i = 0; i < 4; ++i) out[3 + i] = (uint8_t)(value >> (24u - i * 8u));
    out[7] = km_tmc2209_crc(out, 7);
    return KM_OK;
}
km_result_t km_tmc2209_decode(uint8_t reg, const uint8_t *rx, uint8_t length, uint32_t *value)
{
    if (!rx || !value || length != 8 || reg > 0x7f) return KM_ERR_INVALID_PARAM;
    if (rx[0] != 0x05 || rx[1] != 0xff || rx[2] != reg || rx[7] != km_tmc2209_crc(rx, 7))
        return KM_ERR_FAULTED;
    *value = ((uint32_t)rx[3] << 24) | ((uint32_t)rx[4] << 16) | ((uint32_t)rx[5] << 8) | rx[6];
    return KM_OK;
}

static km_result_t fail(km_tmc2209_t *t, km_fault_t fault)
{
    t->ready = false; t->fault = fault; t->state = KM_TMC_FAULT;
    t->pending = t->waiting = false;
    t->transport->force_off(t->context);
    t->transport->abort(t->context);
    return KM_ERR_FAULTED;
}

/* The chip reported nothing, so resubmit the same transaction instead of
 * latching, up to the bound. `release` aborts a transfer that never finished. */
static km_result_t retry(km_tmc2209_t *t, bool release)
{
    if (++t->attempts >= KM_TMC2209_MAX_ATTEMPTS) return fail(t, KM_FAULT_DRIVER_FAULT);
    if (release) t->transport->abort(t->context);
    t->pending = t->waiting = false;
    return KM_OK;
}

static bool current_bits(const km_tmc2209_t *t, uint16_t ma, uint8_t *bits, uint16_t *nominal)
{
    if (!ma || ma > t->config.maximum_current_ma) return false;
    uint32_t resistance = (uint32_t)t->config.sense_resistor_milliohms + 20u;
    uint32_t fullscale_mv = t->vsense ? 180u : 325u;
    /* Round the nominal current down rather than silently exceed a requested
     * setpoint. Physical tolerances still require board/motor qualification. */
    uint64_t count = (uint64_t)ma * resistance * 32u * 1414214u
        / ((uint64_t)fullscale_mv * 1000000000u);
    if (!count || count > 32) return false;
    *bits = (uint8_t)(count - 1u);
    *nominal = (uint16_t)(count * fullscale_mv * 1000000000u / ((uint64_t)32u * resistance * 1414214u));
    return true;
}

static bool current_register(km_tmc2209_t *t, uint16_t run, uint16_t hold, uint32_t *value)
{
    uint8_t irun, ihold;
    uint16_t run_nominal, hold_nominal;
    if (hold > run || !current_bits(t, run, &irun, &run_nominal)
        || !current_bits(t, hold, &ihold, &hold_nominal)) return false;
    *value = (8u << 16) | ((uint32_t)irun << 8) | ihold;
    t->nominal_run_ma = run_nominal; t->nominal_hold_ma = hold_nominal;
    return true;
}

static km_result_t prepare_registers(km_tmc2209_t *t)
{
    const km_tmc2209_config_t saved = t->config;
    const km_tmc2209_config_t *cfg = &saved;
    if (cfg->address > 3 || !cfg->sense_resistor_milliohms || cfg->sense_resistor_milliohms > 1000
        || !cfg->maximum_current_ma || cfg->maximum_current_ma > 2000
        || !cfg->microsteps || cfg->microsteps > 256 || (cfg->microsteps & (cfg->microsteps - 1u))
        || cfg->transaction_timeout_us < 1000 || cfg->transaction_timeout_us > 100000
        || cfg->diagnostic_interval_us < cfg->transaction_timeout_us || cfg->diagnostic_interval_us > 1000000)
        return KM_ERR_INVALID_PARAM;
    uint64_t required = (uint64_t)saved.maximum_current_ma * ((uint32_t)saved.sense_resistor_milliohms + 20u) * 1414214u;
    if (required > 325000000000u) return KM_ERR_INVALID_PARAM;
    t->vsense = required <= 180000000000u;
    unsigned mres = 0;
    for (unsigned steps = saved.microsteps; steps < 256; steps *= 2u) ++mres;
    t->registers[0] = 0; /* VACTUAL: external step/dir only. */
    t->registers[1] = (1u << 6) | (1u << 7) | (1u << 8) | (saved.stealthchop ? 0 : 1u << 2);
    t->registers[2] = 2u << 8; /* SENDDELAY */
    t->registers[3] = 3u | (5u << 4) | (2u << 15) | (t->vsense ? 1u << 17 : 0)
        | (mres << 24) | (saved.interpolate ? 1u << 28 : 0);
    if (!current_register(t, saved.run_current_ma, saved.hold_current_ma, &t->registers[4]))
        return KM_ERR_INVALID_PARAM;
    t->registers[5] = 20; /* TPOWERDOWN */
    t->registers[6] = 0; /* TPWMTHRS */
    t->registers[7] = 0; /* TCOOLTHRS: no implicit current adaptation. */
    t->registers[8] = 0; /* COOLCONF */
    t->registers[9] = 0xc80d0e24u; /* PWMCONF: explicit autoscale/autograd settings. */
    t->registers[10] = 1; /* Clear only initial reset flag, never mask drv_err/uv_cp. */
    return KM_OK;
}

km_result_t km_tmc2209_config_validate(const km_tmc2209_config_t *cfg)
{
    if (!cfg) return KM_ERR_INVALID_PARAM;
    km_tmc2209_t candidate = {0};
    candidate.config = *cfg;
    return prepare_registers(&candidate);
}

km_result_t km_tmc2209_init(km_tmc2209_t *t, const km_tmc2209_config_t *cfg,
                            const km_tmc2209_transport_t *transport, void *context)
{
    if (!t) return KM_ERR_INVALID_PARAM;
    /* Preserve aliased configuration and the existing rejected-init safe-off. */
    km_tmc2209_config_t saved = {0};
    if (cfg) saved = *cfg;
    memset(t, 0, sizeof(*t));
    t->state = KM_TMC_FAULT;
    t->fault = KM_FAULT_DRIVER_FAULT;
    if (transport && transport->force_off) transport->force_off(context);
    if (!cfg || !transport || !transport->begin || !transport->abort || !transport->force_off)
        return KM_ERR_INVALID_PARAM;
    t->config = saved; t->transport = transport; t->context = context;
    if (prepare_registers(t) != KM_OK) return KM_ERR_INVALID_PARAM;
    t->configured = true;
    return km_tmc2209_restart(t);
}

km_result_t km_tmc2209_restart(km_tmc2209_t *t)
{
    if (!t || !t->transport || !t->configured) return KM_ERR_INVALID_PARAM;
    t->transport->force_off(t->context); t->transport->abort(t->context);
    t->pending = t->waiting = t->ready = t->current_update = false;
    t->confirmed_run_ma = t->confirmed_hold_ma = 0;
    t->confirmed_nominal_run_ma = t->confirmed_nominal_hold_ma = 0;
    t->fault = KM_FAULT_NONE; t->state = KM_TMC_IDENTIFY; t->index = 0; t->attempts = 0;
    return KM_OK;
}

static km_result_t submit(km_tmc2209_t *t, uint8_t reg, bool write, uint32_t value, uint32_t now)
{
    if (!t->waiting) { t->started_us = now; t->waiting = true; }
    if (km_time_elapsed(now, t->started_us) >= t->config.transaction_timeout_us)
        return fail(t, KM_FAULT_DRIVER_FAULT);
    t->expected_register = reg; t->expected_rx_length = write ? 0 : 8;
    km_result_t result = write ? km_tmc2209_write_frame(t->config.address, reg, value, t->tx)
                              : km_tmc2209_read_frame(t->config.address, reg, t->tx);
    if (result != KM_OK) return fail(t, KM_FAULT_DRIVER_FAULT);
    ++t->token; if (!t->token) ++t->token;
    result = t->transport->begin(t->context, t->token, t->tx, write ? 8 : 4, t->expected_rx_length);
    if (result == KM_ERR_BUSY) return result;
    if (result != KM_OK) return fail(t, KM_FAULT_DRIVER_FAULT);
    t->pending = true;
    return KM_OK;
}

km_result_t km_tmc2209_service(km_tmc2209_t *t, uint32_t now)
{
    if (!t || !t->transport || !t->configured) return KM_ERR_INVALID_PARAM;
    if (t->fault != KM_FAULT_NONE) return KM_ERR_FAULTED;
    if (t->pending) {
        if (km_time_elapsed(now, t->started_us) < t->config.transaction_timeout_us)
            return KM_ERR_BUSY;
        return retry(t, true) == KM_OK ? KM_ERR_BUSY : KM_ERR_FAULTED; /* No reply. */
    }
    switch (t->state) {
    case KM_TMC_IDENTIFY: return submit(t, 0x06, false, 0, now);
    case KM_TMC_INITIAL_STATUS: return submit(t, 0x01, false, 0, now);
    case KM_TMC_COUNTER: return submit(t, 0x02, false, 0, now);
    case KM_TMC_WRITE: return submit(t, write_registers[t->index], true, t->registers[t->index], now);
    case KM_TMC_CONFIRM: return submit(t, 0x02, false, 0, now);
    case KM_TMC_READ_GCONF: return submit(t, 0x00, false, 0, now);
    case KM_TMC_READ_CHOPCONF: return submit(t, 0x6c, false, 0, now);
    case KM_TMC_READ_PWMCONF: return submit(t, 0x70, false, 0, now);
    case KM_TMC_CHECK_GSTAT: return submit(t, 0x01, false, 0, now);
    case KM_TMC_CHECK_DRIVER: return submit(t, 0x6f, false, 0, now);
    case KM_TMC_READY:
        if (km_time_before(now, t->next_diagnostic_us)) return KM_OK;
        t->state = KM_TMC_CHECK_GSTAT;
        return submit(t, 0x01, false, 0, now);
    case KM_TMC_FAULT: return KM_ERR_FAULTED;
    }
    return fail(t, KM_FAULT_DRIVER_FAULT);
}

km_result_t km_tmc2209_complete(km_tmc2209_t *t, uint32_t token, km_result_t result,
                                const uint8_t *rx, uint8_t length, uint32_t now)
{
    if (!t || !t->transport || !t->configured) return KM_ERR_INVALID_PARAM;
    if (!t->pending || token != t->token) return KM_ERR_STALE;
    uint32_t value = 0;
    if (result != KM_OK || length != t->expected_rx_length
        || km_time_elapsed(now, t->started_us) >= t->config.transaction_timeout_us
        || (length && km_tmc2209_decode(t->expected_register, rx, length, &value) != KM_OK))
        return retry(t, false);
    t->pending = t->waiting = false;
    switch (t->state) {
    case KM_TMC_IDENTIFY:
        if ((value >> 24) != 0x21) return fail(t, KM_FAULT_DRIVER_FAULT);
        t->state = KM_TMC_INITIAL_STATUS; break;
    case KM_TMC_INITIAL_STATUS:
        if (value & 6u) return fail(t, KM_FAULT_DRIVER_FAULT);
        t->state = KM_TMC_COUNTER; break;
    case KM_TMC_COUNTER:
        t->ifcnt = (uint8_t)value; t->state = KM_TMC_WRITE; break;
    case KM_TMC_WRITE: t->state = KM_TMC_CONFIRM; break;
    case KM_TMC_CONFIRM:
        if ((uint8_t)value != (uint8_t)(t->ifcnt + 1u)) {
            /* The write was not accepted; resend it against the new count. */
            if (++t->attempts >= KM_TMC2209_MAX_ATTEMPTS) return fail(t, KM_FAULT_DRIVER_FAULT);
            t->ifcnt = (uint8_t)value; t->state = KM_TMC_WRITE;
            return KM_OK;
        }
        t->ifcnt = (uint8_t)value;
        if (t->current_update) { t->current_update = false; t->state = KM_TMC_CHECK_GSTAT; }
        else if (++t->index < KM_TMC2209_CONFIG_WRITES) t->state = KM_TMC_WRITE;
        else t->state = KM_TMC_READ_GCONF;
        break;
    case KM_TMC_READ_GCONF:
        if ((value & 0x3ffu) != t->registers[1]) return fail(t, KM_FAULT_DRIVER_FAULT);
        t->state = KM_TMC_READ_CHOPCONF; break;
    case KM_TMC_READ_CHOPCONF:
        if ((value & 0xff0387ffu) != t->registers[3]) return fail(t, KM_FAULT_DRIVER_FAULT);
        t->state = KM_TMC_READ_PWMCONF; break;
    case KM_TMC_READ_PWMCONF:
        if (value != t->registers[9]) return fail(t, KM_FAULT_DRIVER_FAULT);
        t->state = KM_TMC_CHECK_GSTAT; break;
    case KM_TMC_CHECK_GSTAT:
        t->gstat = value;
        if (value & 7u) return fail(t, KM_FAULT_DRIVER_FAULT);
        t->state = KM_TMC_CHECK_DRIVER; break;
    case KM_TMC_CHECK_DRIVER:
        t->drv_status = value;
        if (value & 0x3cu) return fail(t, KM_FAULT_DRIVER_OVERCURRENT);
        if (value & 2u) return fail(t, KM_FAULT_DRIVER_FAULT);
        t->last_diagnostic_us = now;
        t->next_diagnostic_us = now + t->config.diagnostic_interval_us;
        t->confirmed_run_ma = t->config.run_current_ma;
        t->confirmed_hold_ma = t->config.hold_current_ma;
        t->confirmed_nominal_run_ma = t->nominal_run_ma;
        t->confirmed_nominal_hold_ma = t->nominal_hold_ma;
        t->ready = true; t->state = KM_TMC_READY; break;
    default: return fail(t, KM_FAULT_DRIVER_FAULT);
    }
    if (t->state != KM_TMC_CONFIRM) t->attempts = 0; /* A write shares its confirm's budget. */
    return KM_OK;
}

km_result_t km_tmc2209_set_current(km_tmc2209_t *t, uint16_t run, uint16_t hold)
{
    if (!t || !t->transport || !t->configured) return KM_ERR_INVALID_PARAM;
    if (t->fault != KM_FAULT_NONE) return KM_ERR_FAULTED;
    if (!t->ready || t->pending || t->state != KM_TMC_READY) return KM_ERR_BUSY;
    uint32_t value;
    if (!current_register(t, run, hold, &value)) return KM_ERR_INVALID_PARAM;
    if (run == t->confirmed_run_ma && hold == t->confirmed_hold_ma) return KM_OK;
    /* This register-only layer gates the actuator during asynchronous changes.
     * A future live-update policy needs a separately verified backend contract. */
    t->transport->force_off(t->context);
    t->ready = false; t->current_update = true;
    t->config.run_current_ma = run; t->config.hold_current_ma = hold;
    t->registers[4] = value; t->index = 4; t->state = KM_TMC_COUNTER; t->attempts = 0;
    return KM_OK;
}
