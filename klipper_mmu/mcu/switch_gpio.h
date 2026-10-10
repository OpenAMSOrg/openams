// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_SWITCH_GPIO_H
#define KLIPPER_MMU_SWITCH_GPIO_H

#include "klipper_mmu/operation_core.h"

typedef struct {
    uint16_t pin;       /* Canonical H723 PA0..PH15 ID. */
    int8_t pull;        /* -1 pull-down, 0 unbiased, 1 pull-up. */
    bool active_high;
} km_switch_input_t;

/* Virtual-HUB profiles must leave HUB physically unassigned. A profile without
 * a toolhead switch (allowed only with fps_arrival) leaves toolhead unassigned. */
#define KM_SWITCH_PIN_UNUSED UINT16_MAX
#define KM_SWITCH_DEBOUNCE_US 5000u
/* The LINEAR/ROTARY selector home endstop: two consecutive 1 ms samples.
 * The 5 ms presence debounce would add about 24 steps of homing overshoot at
 * 60 mm/s, which a calibration counts as travel. */
#define KM_SWITCH_SELECTOR_ENDSTOP_DEBOUNCE_US 1000u
#define KM_SWITCH_MAX_SAMPLE_GAP_US 2500u

typedef struct {
    km_switch_input_t bay[KM_MAX_LANES];
    km_switch_input_t load[KM_MAX_LANES];
    km_switch_input_t hub, toolhead;
    /* Used only for a switched buffer. Analog DiffA/B are never read as GPIO.
     * buffer_high.pin may be KM_SWITCH_PIN_UNUSED for a tension-only buffer
     * (docs/devices/qidi_box.md): it is then never claimed or sampled, and
     * the missing switch's state is reported inactive, never fabricated. */
    km_switch_input_t buffer_low, buffer_high;
    /* Selector home endstop (profile.has_selector; klipper_mmu/selector.h). */
    km_switch_input_t selector_endstop;
    /* INDEXED only: one index switch per gate, in place of a gate position
     * table (klipper_mmu/selector.h). */
    km_switch_input_t selector_gate[KM_MAX_LANES];
} km_switch_gpio_config_t;

/* One immutable binding until MCU reset. Caller owns the initialized static core
 * and still owes full resource admission. Call from ready task context with IRQs
 * enabled, before controller attachment; no core operation may be active.
 * Validates all used pins, pulls and collisions before setup. A virtual-HUB
 * profile requires hub.pin == KM_SWITCH_PIN_UNUSED and samples LOAD once;
 * the core derives HUB presence and freshness from that LOAD sample. Without
 * has_toolhead_sensor, fps_arrival is required, toolhead.pin must be
 * KM_SWITCH_PIN_UNUSED, and toolhead presence is always published absent. With
 * CONFIG_WANT_GPIO_LOCAL, every pin actually used (bays, hub, toolhead, and
 * buffer low/high only for switched feedback, and enabled LOAD inputs) must
 * already be locally reserved.
 * A missing, ordinary or partial claim returns KM_ERR_CONFLICT with the core and
 * binding unchanged and retryable. This binding does not reserve pins. Shutdown
 * is checked under the publication lock before the claim is published. Claims
 * survive a partial setup/shutdown. See board_startup.h: km_board_startup_begin
 * validates the profile, saved configuration, and complete claims, initializes the
 * core, then under CONFIG_WANT_GPIO_LOCAL reserves exactly the expected MMU
 * KM_RESOURCE_PIN entries once before switch/ADC/feeder/PWM setup. Prior-local
 * requirements, conflict side effects, claim immutability, and partial setup are
 * unchanged. The caller still owes genuine complete external MCU admission,
 * host/transport/crystal and other resource ownership, and actuation authorization.
 * Automatic boot and host-command attachment remain unfinished. ACTUATION stays 0.
 * No outputs, timers or physical qualification are claimed. Settings are copied. */
KM_API km_result_t km_switch_gpio_setup(km_operation_core_t *core,
                                       const km_switch_gpio_config_t *config);

/* Bounded controller sample callback; context must be NULL. Reads each configured
 * input once under an IRQ lock, then publishes a batch dated BEFORE the first
 * read (conservative freshness bound). Presence inputs must remain unchanged for
 * KM_SWITCH_DEBOUNCE_US across consecutive samples (the LINEAR/ROTARY selector
 * home endstop instead uses the shorter KM_SWITCH_SELECTOR_ENDSTOP_DEBOUNCE_US);
 * a sampling gap over
 * KM_SWITCH_MAX_SAMPLE_GAP_US invalidates prior evidence and restarts qualification.
 * A qualified prior level stays valid while a transition is being qualified;
 * startup and acquisition gaps are invalid until qualified again. No inferred
 * bay source, encoder,
 * analog samples, current measurement, host I/O or motor actuation. A different
 * core/non-NULL context is a programming error and triggers Klipper shutdown.
 * Shutdown invalidates bound inputs and cannot resume after clear_shutdown. */
KM_API void km_switch_gpio_sample(km_operation_core_t *core, void *context);
void km_switch_gpio_shutdown(void);

/* Diagnostic only; see docs/diagnostics.md and the definition above
 * km_switch_gpio_get_bounce in switch_gpio.c. */
KM_API void km_switch_gpio_get_bounce(uint16_t bay[KM_MAX_LANES],
                                      uint16_t load[KM_MAX_LANES],
                                      uint16_t *hub, uint16_t *toolhead);

/* Diagnostic only: the selector home endstop's own bounce count. */
KM_API void km_switch_gpio_get_selector_bounce(uint16_t *out);

/* Diagnostic only: each INDEXED gate index switch's own bounce count (see
 * km_switch_gpio_get_bounce above); zero for every lane when unbound, not
 * ready, or the profile's selector kind is not INDEXED. */
KM_API void km_switch_gpio_get_selector_gate_bounce(uint16_t out[KM_MAX_LANES]);

#endif
