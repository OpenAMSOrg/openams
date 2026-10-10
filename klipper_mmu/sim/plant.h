// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef MMU_SIM_PLANT_H
#define MMU_SIM_PLANT_H
#include <stdbool.h>
#include <stdint.h>

/* Host-side, one-lane material model; not linked into MCU firmware. */
typedef struct {
    double step_mm;
    double forward_transfer, reverse_transfer; /* Actual/shaft travel, 0..1. */
    double load_mm, hub_mm, arrival_mm, buffer_capacity_mm, switch_hysteresis_mm;
    double bay_clear_mm; /* Optional synthetic retreat edge. Zero keeps spool presence.
                          * A positive edge is exclusive of finite_spool. */
    double rewind_max_mm_s, rewind_time_constant_s;
    bool finite_spool;
    double spool_budget_mm; /* Available payout when finite_spool is true. */
    /* Optional forces at the drive gear; zero disables each (N, N per mm/s, kg, mm/s).
     * Load = drag + viscous*|speed| + mass*acceleration along the motion. The motor
     * gives motor_n up to corner_mm_s, then motor_n*corner/|speed|. A step whose load
     * exceeds the motor force is lost (stall); one that exceeds grip_n turns the
     * shaft without moving filament (slip). */
    double drag_n, viscous_n_per_mm_s, moving_kg, motor_n, corner_mm_s, grip_n;
} mmu_sim_parameters_t;

typedef struct {
    mmu_sim_parameters_t parameters;
    uint64_t time_us;
    int64_t shaft_steps;
    double material_mm;       /* Downstream material; after finite exhaustion,
                               * remaining length from printer end to tail. */
    double upstream_slack_mm;
    double initial_material_mm;
    double spool_payout_mm;   /* Signed net material leaving the spool. */
    double consumed_mm, unmet_consumption_mm, lost_travel_mm;
    double rewind_velocity_mm_s;
    uint16_t rewind_duty;
    bool blocked, load_present, hub_present, arrival_present;
    bool bay_present;
    bool trailing_end_active; /* Printer has pulled the finite-spool tail past feeder. */
    uint64_t last_step_us, stalled_steps, slipped_steps;
    double speed_mm_s;        /* From the previous commanded step interval. */
    double force_n, motor_force_n; /* Last step's load and available motor force. */
} mmu_sim_plant_t;

/* Invalid inputs leave caller storage unchanged. All distances are millimeters.
 * Remaining calls require a successfully initialized instance. Reinitialize to
 * change parameters; blocked and rewind duty are runtime inputs. */
bool mmu_sim_init(mmu_sim_plant_t *plant, const mmu_sim_parameters_t *parameters,
                  double material_mm, double upstream_slack_mm);
/* Called only for a physical motor step; direction is +1 or -1. */
bool mmu_sim_step(mmu_sim_plant_t *plant, int direction);
/* Positive duty takes up upstream slack. Duty zero lets modeled inertia decay. */
bool mmu_sim_rewind(mmu_sim_plant_t *plant, uint16_t duty_permille);
/* Advance at a constant nonnegative printer demand until the next input event. */
bool mmu_sim_advance(mmu_sim_plant_t *plant, uint32_t elapsed_us,
                     double printer_demand_mm_s);
double mmu_sim_buffer_mm(const mmu_sim_plant_t *plant);
/* Last step's load over available motor force (0 without a motor limit): the
 * stand-in for the driver's load margin (TMC StallGuard). */
double mmu_sim_load_ratio(const mmu_sim_plant_t *plant);
/* Conservation residual: zero apart from numerical roundoff. */
double mmu_sim_material_error_mm(const mmu_sim_plant_t *plant);
#endif
