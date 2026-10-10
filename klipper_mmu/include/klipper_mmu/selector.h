// SPDX-License-Identifier: GPL-3.0-or-later
/* Shared-drive selector machines: one gear driver feeds every lane through a
 * selector. Three families share this one description (owner decisions,
 * 2026-09-26; design input docs/plan/selector-primitives.md):
 *
 *   LINEAR  ERCF 1.1/2.0/3.0, Tradrack: stepper axis homed to an endstop,
 *           a servo grips the filament against the gear (engage/release/move).
 *   ROTARY  MMX6, 3D Chameleon, HTLF: stepper axis homed to an endstop; a
 *           gate is engaged by position and released by moving to that
 *           gate's release position. The single gear motor drives different
 *           gates by turning different ways (Happy Hare selector_gate_directions);
 *           the core inverts the gear's physical direction while a REVERSE
 *           gate is selected, never the selector stepper's own direction.
 *   SERVO   MMX: no selector stepper; one servo angle per gate selects it.
 *   INDEXED BTT ViViD, Claymore: stepper axis, but no gate position table --
 *           each gate has its own index switch. A selection moves in
 *           whichever direction reaches that gate's switch soonest along the
 *           physical gate order (gate_order), then nudges half the switch
 *           width (center_nudge_steps) to center on it. Homing is the same
 *           move aimed at gate 0 (mmu_indexed_selector.py _find_gate/
 *           _home_selector). An all-zero gate_order means no physical order
 *           is known: selection always searches forward instead, never a
 *           shorter reverse hop, and center_nudge_steps may then be 0 (no
 *           calibrated switch width either) -- AFC's own selector, which has
 *           neither (extras/AFC_vivid.py:159-199 select_lane).
 *
 * Rules every implementation follows:
 * - The gear is one feeder driver bound to every lane (profile.has_selector).
 * - The selector stepper reuses the core's single trajectory: selector and
 *   gear never move at the same time.
 * - The selector moves only while the shared path is clear of filament, or
 *   when the requested lane is already selected (no move needed).
 * - The first selection after boot or after a selector fault homes first.
 *   KM_OP_TYPE_HOME homes on request.
 * - Gate positions live in firmware: Kconfig defaults, replaced by the host's
 *   calibrated table at configuration (km_core_set_selector_gate_steps).
 * - The servo is driven by Klipper's scheduled software PWM on any GPIO; a
 *   hardware PWM channel is optional, never required. */
#ifndef KLIPPER_MMU_SELECTOR_H
#define KLIPPER_MMU_SELECTOR_H
#include "klipper_mmu/selector_home.h"

typedef enum {
    KM_SELECTOR_NONE    = 0, /* Independent per-lane feeders. */
    KM_SELECTOR_LINEAR  = 1,
    KM_SELECTOR_ROTARY  = 2,
    KM_SELECTOR_SERVO   = 3,
    KM_SELECTOR_INDEXED = 4
} km_selector_kind_t;

typedef struct {
    km_selector_kind_t kind;

    /* Stepper axis (LINEAR, ROTARY): selector microsteps from home. -1 means
     * this gate is not calibrated yet (Happy Hare's own sentinel for an
     * unmeasured selector_offset); selecting it is refused until measured. */
    km_selector_home_config_t home;
    int32_t gate_steps[KM_MAX_LANES];
    uint32_t velocity_steps_s;
    uint32_t acceleration_steps_s2;
    /* ROTARY only: the gear (feeder) drive direction for this gate (Happy
     * Hare selector_gate_directions: 3D Chameleon's single motor drives
     * different gates by turning different ways), and the gate whose
     * position releases this one (selector_release_gates). REVERSE inverts
     * every gear motion while this gate is selected (feed turns the gear
     * backwards, retract forwards); positions, HUB/arrival logic and events
     * are unchanged from the lane's own point of view. Not a selector
     * approach direction. */
    km_direction_t gate_direction[KM_MAX_LANES];
    uint8_t release_gate[KM_MAX_LANES];

    /* INDEXED only: no gate_steps table -- gate_order[i] is the lane found at
     * physical position i around the selector (Happy Hare selector_gate_order),
     * exactly lane_count entries, a permutation of every lane. Selection
     * chooses whichever of the two directions around this order reaches the
     * target soonest (mmu_indexed_selector.py:246-261 _best_rotation_direction).
     * center_nudge_steps is half the calibrated switch width (selector_endstop_
     * width/2, in selector steps): the move made after a gate's switch first
     * asserts, to land on its center rather than its leading edge.
     * All-zero is this pair's own sentinel for "neither is known" (AFC's
     * selector, extras/AFC_vivid.py:159-199 select_lane): every gate_order[i]
     * is 0 (never a real permutation once lane_count > 1) and
     * center_nudge_steps is 0, and selection always searches forward instead
     * of picking a direction from an order that does not exist. */
    uint8_t gate_order[KM_MAX_LANES];
    uint32_t center_nudge_steps;
    /* INDEXED without a gate order (AFC's rule): how far to move forward off
     * a gate's switch that is already pressed before searching for it, AFC's
     * unselect_lane distance (extras/AFC_vivid.py:262). Unused with an order,
     * whose back-off is the known switch width. */
    uint32_t unselect_steps;

    /* Servo (LINEAR grip, SERVO selection). Pulse widths in microseconds;
     * 0 means the position is not used. */
    bool has_servo;
    uint16_t servo_engage_us;  /* Happy Hare servo_down_angle */
    uint16_t servo_release_us; /* servo_up_angle */
    uint16_t servo_move_us;    /* servo_move_angle: while the selector moves */
    uint16_t servo_gate_us[KM_MAX_LANES]; /* SERVO: servo_gate_angles */
    uint32_t servo_dwell_ms;   /* Time for the servo to reach a position. */
    bool servo_hold;           /* Keep pulsing after the dwell. */
} km_selector_profile_t;

/* The MCU's servo output. set_pulse_us(self, 0) stops the pulse train. */
typedef struct km_servo km_servo_t;
struct km_servo {
    km_result_t (*set_pulse_us)(km_servo_t *self, uint16_t pulse_us);
    void *user_data;
};

#endif
