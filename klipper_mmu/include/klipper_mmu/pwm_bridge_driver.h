// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_PWM_BRIDGE_DRIVER_H
#define KLIPPER_MMU_PWM_BRIDGE_DRIVER_H
#include "klipper_mmu/driver.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Board adapter owns exclusive pins, logical-to-electrical mapping and
 * serialized, immediate, bounded callbacks. No queued output writes.
 * See docs/pwm-bridge-backend.md for the complete board contract. */
typedef struct {
    /* Must physically remove enable even when ordinary writes fail. */
    void (*force_disable)(void *context);
    km_result_t (*write_enable)(void *context, bool enabled);
    km_result_t (*write_pwm)(void *context, km_direction_t channel, uint16_t duty_permille);
    uint32_t (*now_us)(void *context);
    bool (*ready)(void *context);
    km_fault_t (*fault)(void *context); /* Optional cached physical status. */
    /* Optional real, validity-qualified measurement; zero is a valid reading. */
    km_result_t (*read_current_ma)(void *context, uint16_t *measured_ma);
} km_pwm_bridge_io_t;

typedef struct {
    uint16_t maximum_duty_permille;     /* 1..1000, logical energization duty. */
    uint32_t reversal_deadtime_us;      /* 1..2^31-1, board-characterized. */
    uint16_t maximum_call_lateness_us;  /* 1..1000, direction timestamp limit. */
} km_pwm_bridge_config_t;

typedef struct {
    km_driver_t base;
    km_pwm_bridge_config_t config;
    km_pwm_bridge_io_t io;
    void *context;
    km_fault_t latched_fault;
    uint32_t last_call_us;
    uint32_t last_output_us;
    uint32_t off_age_us; /* Saturates at configured dead time. */
    bool off_tracking;
    km_direction_t last_energized_direction;
    bool has_been_energized;
    bool clock_seen;
} km_pwm_bridge_driver_t;

/* Pure limit validation, shared by saved configuration and driver startup. */
KM_API km_result_t km_pwm_bridge_config_validate(const km_pwm_bridge_config_t *config);

/* Fresh initialization, never concurrent with live operation. context must not
 * alias driver storage. Config/HAL may alias their fields in the driver.
 * KM_OK means valid initialization, not permission to energize: faults and
 * readiness are checked separately. Always begins physically disabled. */
KM_API km_result_t km_pwm_bridge_init(km_pwm_bridge_driver_t *driver,
    const km_pwm_bridge_config_t *config, const km_pwm_bridge_io_t *io, void *context);
/* Call with gaps <2^31 us even while idle/faulted, and before core evaluation.
 * The board must choose a much shorter cadence for its protection needs. */
KM_API void km_pwm_bridge_service(km_pwm_bridge_driver_t *driver);
/* Immediate stop; must be serialized with all other calls, including in ISRs. */
KM_API void km_pwm_bridge_trip(km_pwm_bridge_driver_t *driver, km_fault_t fault);

#ifdef __cplusplus
}
#endif
#endif
