// SPDX-License-Identifier: GPL-3.0-or-later
#include "plant.h"
#include <math.h>

static bool parameters_valid(const mmu_sim_parameters_t *p)
{
    return p && isfinite(p->step_mm) && p->step_mm > 0
        && isfinite(p->forward_transfer) && p->forward_transfer >= 0 && p->forward_transfer <= 1
        && isfinite(p->reverse_transfer) && p->reverse_transfer >= 0 && p->reverse_transfer <= 1
        && isfinite(p->hub_mm) && p->hub_mm > 0
        && isfinite(p->load_mm) && p->load_mm >= 0
        && isfinite(p->arrival_mm) && p->arrival_mm > p->hub_mm
        && isfinite(p->buffer_capacity_mm) && p->buffer_capacity_mm > 0
        && isfinite(p->arrival_mm + p->buffer_capacity_mm)
        && isfinite(p->switch_hysteresis_mm) && p->switch_hysteresis_mm >= 0
        && isfinite(p->bay_clear_mm) && p->bay_clear_mm >= 0
        && (p->bay_clear_mm == 0 || (p->bay_clear_mm > p->switch_hysteresis_mm
            && !p->finite_spool))
        && p->switch_hysteresis_mm < p->hub_mm
        && (p->load_mm == 0 || (p->load_mm > p->switch_hysteresis_mm
            && p->load_mm + 2 * p->switch_hysteresis_mm < p->hub_mm))
        && 2 * p->switch_hysteresis_mm < p->arrival_mm - p->hub_mm
        && p->switch_hysteresis_mm < p->buffer_capacity_mm
        && isfinite(p->rewind_max_mm_s) && p->rewind_max_mm_s >= 0
        && isfinite(p->rewind_time_constant_s) && p->rewind_time_constant_s >= 0
        && (!p->finite_spool || (isfinite(p->spool_budget_mm) && p->spool_budget_mm >= 0))
        && isfinite(p->drag_n) && p->drag_n >= 0
        && isfinite(p->viscous_n_per_mm_s) && p->viscous_n_per_mm_s >= 0
        && isfinite(p->moving_kg) && p->moving_kg >= 0 && isfinite(p->grip_n) && p->grip_n >= 0
        && isfinite(p->motor_n) && p->motor_n >= 0
        && isfinite(p->corner_mm_s) && (p->motor_n == 0 || p->corner_mm_s > 0);
}

static bool present(bool prior, double material, double threshold, double hysteresis)
{
    return prior ? material >= threshold - hysteresis : material >= threshold + hysteresis;
}

static void switches(mmu_sim_plant_t *p)
{
    p->bay_present = p->parameters.bay_clear_mm > 0
        ? present(p->bay_present, p->material_mm, p->parameters.bay_clear_mm,
                  p->parameters.switch_hysteresis_mm)
        : (!p->parameters.finite_spool || p->upstream_slack_mm > 0
           || p->spool_payout_mm < p->parameters.spool_budget_mm);
    if (p->trailing_end_active) {
        /* Remaining stock is measured backward from the printer end. */
        p->load_present = p->parameters.load_mm > 0
            && present(p->load_present, p->material_mm,
                       p->parameters.arrival_mm - p->parameters.load_mm,
                       p->parameters.switch_hysteresis_mm);
        p->hub_present = present(p->hub_present, p->material_mm,
                                 p->parameters.arrival_mm - p->parameters.hub_mm,
                                 p->parameters.switch_hysteresis_mm);
        p->arrival_present = p->material_mm > 0;
    } else {
        p->load_present = p->parameters.load_mm > 0
            && present(p->load_present, p->material_mm, p->parameters.load_mm,
                       p->parameters.switch_hysteresis_mm);
        p->hub_present = present(p->hub_present, p->material_mm, p->parameters.hub_mm,
                                 p->parameters.switch_hysteresis_mm);
        p->arrival_present = present(p->arrival_present, p->material_mm,
                                     p->parameters.arrival_mm,
                                     p->parameters.switch_hysteresis_mm);
    }
}

bool mmu_sim_init(mmu_sim_plant_t *p, const mmu_sim_parameters_t *parameters,
                  double material, double slack)
{
    if (!p || !parameters_valid(parameters) || !isfinite(material) || material < 0
        || material > parameters->arrival_mm + parameters->buffer_capacity_mm
        || !isfinite(slack) || slack < 0 || !isfinite(material + slack)) return false;
    mmu_sim_plant_t next = {.parameters = *parameters, .material_mm = material,
        .upstream_slack_mm = slack, .initial_material_mm = material + slack};
    switches(&next); *p = next;
    return true;
}

bool mmu_sim_step(mmu_sim_plant_t *p, int direction)
{
    if (!p || (direction != 1 && direction != -1)
        || (direction == 1 && p->shaft_steps == INT64_MAX)
        || (direction == -1 && p->shaft_steps == INT64_MIN)) return false;
    const mmu_sim_parameters_t *m = &p->parameters;
    double requested = m->step_mm;
    /* Speed and acceleration from the commanded step timing; a step after a
     * pause (or the first) starts from rest. */
    double dt = (double)(p->time_us - p->last_step_us) * 1e-6;
    bool moving = p->shaft_steps != 0 && dt > 0 && dt < 0.1;
    double speed = moving ? direction * requested / dt : 0;
    double accel = moving && p->speed_mm_s != 0 ? (speed - p->speed_mm_s) / dt : 0;
    double force = fabs(m->drag_n + m->viscous_n_per_mm_s * fabs(speed)
                        + m->moving_kg * 1e-3 * accel * direction);
    double motor = m->motor_n == 0 ? INFINITY
        : fabs(speed) <= m->corner_mm_s ? m->motor_n : m->motor_n * m->corner_mm_s / fabs(speed);
    bool stalled = force > motor, slipped = !stalled && m->grip_n > 0 && force > m->grip_n;
    /* The feeder cannot grip filament after its trailing end has passed. */
    double transferred = (p->blocked || p->trailing_end_active || stalled || slipped) ? 0
        : requested * (direction > 0 ? m->forward_transfer : m->reverse_transfer);
    double available = direction > 0
        ? p->parameters.arrival_mm + p->parameters.buffer_capacity_mm - p->material_mm
        : p->material_mm;
    if (direction > 0 && p->parameters.finite_spool)
        available = fmin(available, p->upstream_slack_mm
            + fmax(0, p->parameters.spool_budget_mm - p->spool_payout_mm));
    double actual = fmin(transferred, available);
    mmu_sim_plant_t next = *p;
    next.shaft_steps += direction;
    next.lost_travel_mm += requested - actual;
    next.last_step_us = p->time_us; next.speed_mm_s = speed;
    next.force_n = force; next.motor_force_n = m->motor_n == 0 ? 0 : motor;
    next.stalled_steps += stalled; next.slipped_steps += slipped;
    next.material_mm += direction * actual;
    if (direction > 0) {
        double from_slack = fmin(next.upstream_slack_mm, actual);
        next.upstream_slack_mm -= from_slack;
        next.spool_payout_mm += actual - from_slack;
    } else {
        next.upstream_slack_mm += actual;
    }
    if (!isfinite(next.lost_travel_mm) || !isfinite(next.upstream_slack_mm)
        || !isfinite(next.spool_payout_mm)) return false;
    switches(&next); *p = next;
    return true;
}

bool mmu_sim_rewind(mmu_sim_plant_t *p, uint16_t duty)
{
    if (!p || duty > 1000) return false;
    p->rewind_duty = duty;
    return true;
}

double mmu_sim_load_ratio(const mmu_sim_plant_t *p)
{
    return p && p->motor_force_n > 0 ? p->force_n / p->motor_force_n : 0;
}

double mmu_sim_buffer_mm(const mmu_sim_plant_t *p)
{
    return fmax(0, p->material_mm - p->parameters.arrival_mm);
}

bool mmu_sim_advance(mmu_sim_plant_t *p, uint32_t elapsed_us, double demand)
{
    if (!p || !isfinite(demand) || demand < 0 || elapsed_us > UINT64_MAX - p->time_us)
        return false;
    if (!elapsed_us) return true;
    double seconds = elapsed_us / 1000000.0;
    double target = p->parameters.rewind_max_mm_s * (p->rewind_duty / 1000.0);
    double tau = p->parameters.rewind_time_constant_s;
    double velocity = target, takeup = target * seconds;
    if (tau > 0) {
        double response = -expm1(-seconds / tau);
        velocity = p->rewind_velocity_mm_s + (target - p->rewind_velocity_mm_s) * response;
        takeup += (p->rewind_velocity_mm_s - target) * tau * response;
    }
    double requested = demand * seconds;
    if (!isfinite(velocity) || !isfinite(takeup) || !isfinite(requested)) return false;
    /* Small negative roundoff in the analytic integral cannot create material. */
    takeup = fmin(p->upstream_slack_mm, fmax(0, takeup));
    /* Once the finite spool and upstream slack are exhausted, printer demand
     * pulls the trailing end through the remaining path after the buffer. */
    double available = mmu_sim_buffer_mm(p);
    bool pull_tail = p->parameters.finite_spool && p->upstream_slack_mm == 0 &&
        p->spool_payout_mm >= p->parameters.spool_budget_mm &&
        (p->trailing_end_active || p->arrival_present) && requested > available;
    if (pull_tail || p->trailing_end_active)
        available = p->material_mm;
    double consumed = fmin(available, requested);
    mmu_sim_plant_t next = *p;
    next.time_us += elapsed_us;
    next.rewind_velocity_mm_s = velocity;
    next.material_mm -= consumed;
    next.trailing_end_active = p->trailing_end_active || pull_tail;
    next.consumed_mm += consumed;
    next.unmet_consumption_mm += requested - consumed;
    next.upstream_slack_mm -= takeup;
    next.spool_payout_mm -= takeup;
    if (!isfinite(next.consumed_mm) || !isfinite(next.unmet_consumption_mm)
        || !isfinite(next.spool_payout_mm)) return false;
    switches(&next); *p = next;
    return true;
}

double mmu_sim_material_error_mm(const mmu_sim_plant_t *p)
{
    return p->initial_material_mm + p->spool_payout_mm - p->consumed_mm
        - p->material_mm - p->upstream_slack_mm;
}
