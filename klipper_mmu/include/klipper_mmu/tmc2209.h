// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_TMC2209_H
#define KLIPPER_MMU_TMC2209_H
#include "klipper_mmu/types.h"

/* Raw UART datagrams, without start/stop bits or local echo. */
KM_API uint8_t km_tmc2209_crc(const uint8_t *bytes, uint8_t length);
KM_API km_result_t km_tmc2209_read_frame(uint8_t address, uint8_t reg, uint8_t out[4]);
KM_API km_result_t km_tmc2209_write_frame(uint8_t address, uint8_t reg, uint32_t value, uint8_t out[8]);
KM_API km_result_t km_tmc2209_decode(uint8_t reg, const uint8_t *response, uint8_t length, uint32_t *value);

typedef struct {
    uint8_t address;               /* Physical MS1/MS2 UART address, 0..3. */
    uint16_t sense_resistor_milliohms;
    uint16_t maximum_current_ma;   /* Explicit board/motor operating limit. */
    uint16_t run_current_ma;
    uint16_t hold_current_ma;      /* Zero means disable externally, not CS=0. */
    uint16_t microsteps;           /* Power of two, 1..256. */
    bool interpolate;
    bool stealthchop;
    uint32_t transaction_timeout_us;
    uint32_t diagnostic_interval_us;
} km_tmc2209_config_t;

typedef struct {
    /* Nonblocking. KM_ERR_BUSY means no transfer was started. Never call
     * complete synchronously from begin. Copy bytes or retain them until done. */
    km_result_t (*begin)(void *context, uint32_t token, const uint8_t *tx,
                         uint8_t tx_length, uint8_t rx_length);
    void (*abort)(void *context);
    void (*force_off)(void *context);
} km_tmc2209_transport_t;

typedef enum {
    KM_TMC_IDENTIFY, KM_TMC_INITIAL_STATUS, KM_TMC_COUNTER,
    KM_TMC_WRITE, KM_TMC_CONFIRM, KM_TMC_READ_GCONF, KM_TMC_READ_CHOPCONF,
    KM_TMC_READ_PWMCONF, KM_TMC_CHECK_GSTAT, KM_TMC_CHECK_DRIVER,
    KM_TMC_READY, KM_TMC_FAULT
} km_tmc2209_state_t;

#define KM_TMC2209_CONFIG_WRITES 11u
/* Attempts per register read, or per write and its IFCNT confirmation. A
 * lost, late or corrupt reply is a communication error, not a driver report,
 * so it is retried; consecutive failures up to this bound latch a fault.
 * Values the chip actually reports (identity, GSTAT, DRV_STATUS, readback)
 * are never retried. */
#define KM_TMC2209_MAX_ATTEMPTS 3u
typedef struct {
    km_tmc2209_config_t config;
    const km_tmc2209_transport_t *transport;
    void *context;
    km_tmc2209_state_t state;
    km_fault_t fault;
    bool configured;
    bool ready;
    bool pending;
    bool waiting;
    bool current_update;
    bool vsense;
    uint8_t index;
    uint8_t attempts;              /* Failed attempts of the current transaction. */
    uint8_t expected_register;
    uint8_t expected_rx_length;
    uint8_t ifcnt;
    uint8_t tx[8];
    uint32_t registers[KM_TMC2209_CONFIG_WRITES];
    uint32_t token;
    uint32_t started_us;
    uint32_t next_diagnostic_us;
    uint32_t drv_status;
    uint32_t gstat;
    uint32_t last_diagnostic_us;
    uint16_t confirmed_run_ma;
    uint16_t confirmed_hold_ma;
    uint16_t nominal_run_ma;       /* Requested quantized estimate, NOT a measurement. */
    uint16_t nominal_hold_ma;
    uint16_t confirmed_nominal_run_ma;
    uint16_t confirmed_nominal_hold_ma;
} km_tmc2209_t;

/* Pure validation, including representable current and register settings. No I/O. */
KM_API km_result_t km_tmc2209_config_validate(const km_tmc2209_config_t *config);
/* Fresh instance only. Init/restart force outputs off; readiness never enables. */
KM_API km_result_t km_tmc2209_init(km_tmc2209_t *tmc, const km_tmc2209_config_t *config,
                            const km_tmc2209_transport_t *transport, void *context);
KM_API km_result_t km_tmc2209_restart(km_tmc2209_t *tmc);
/* A timed-out transfer is aborted; KM_ERR_BUSY then means a retry is due. */
KM_API km_result_t km_tmc2209_service(km_tmc2209_t *tmc, uint32_t now_us);
/* KM_OK also when a communication error was absorbed for a retry. */
KM_API km_result_t km_tmc2209_complete(km_tmc2209_t *tmc, uint32_t token, km_result_t result,
                                const uint8_t *rx, uint8_t length, uint32_t now_us);
/* Asynchronous confirmation. KM_OK accepts the change, not a completed write.
 * Backend must gate its actuator while ready is false. */
KM_API km_result_t km_tmc2209_set_current(km_tmc2209_t *tmc, uint16_t run_ma, uint16_t hold_ma);
#endif
