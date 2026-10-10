// SPDX-License-Identifier: GPL-3.0-or-later
#include "plant.h"
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

static const mmu_sim_parameters_t parameters = {
    .step_mm = 0.125, .forward_transfer = 1, .reverse_transfer = 1,
    .hub_mm = 10, .arrival_mm = 30, .buffer_capacity_mm = 20,
    .switch_hysteresis_mm = 0.25, .rewind_max_mm_s = 10, .rewind_time_constant_s = 1
};
static void near(double observed, double expected)
{
    assert(isfinite(observed) && fabs(observed - expected) < 1e-9);
}
static void steps(mmu_sim_plant_t *p, int direction, unsigned count)
{
    while (count--) assert(mmu_sim_step(p, direction));
    near(mmu_sim_material_error_mm(p), 0);
}

/* Steps at a fixed interval, as the STEP edges would arrive. */
static void steps_every(mmu_sim_plant_t *p, int direction, unsigned count, uint32_t interval_us)
{
    while (count--) {
        assert(mmu_sim_advance(p, interval_us, 0));
        assert(mmu_sim_step(p, direction));
    }
}

static void forces(void)
{
    mmu_sim_parameters_t m = parameters;
    m.arrival_mm = 400; m.hub_mm = 300; m.buffer_capacity_mm = 400;
    /* Load 5 + 0.05*v meets motor force 20*50/v at exactly 100 mm/s. */
    m.drag_n = 5; m.viscous_n_per_mm_s = 0.05; m.motor_n = 20; m.corner_mm_s = 50;
    struct { uint32_t interval_us; bool stalls; } cases[] = {
        {1389, false}, /* 90 mm/s: load 9.5 N, motor 11.1 N */
        {1136, true},  /* 110 mm/s: load 10.5 N, motor 9.1 N */
    };
    for (unsigned i = 0; i < sizeof cases / sizeof *cases; ++i) {
        mmu_sim_plant_t p;
        assert(mmu_sim_init(&p, &m, 0, 0));
        steps_every(&p, 1, 1, 200000);          /* the first step starts from rest */
        steps_every(&p, 1, 200, cases[i].interval_us);
        assert(p.stalled_steps == (cases[i].stalls ? 200u : 0u) && p.slipped_steps == 0);
        near(p.lost_travel_mm, cases[i].stalls ? 200 * m.step_mm : 0);
        if (!cases[i].stalls) assert(fabs(mmu_sim_load_ratio(&p) - 9.5 / (1000.0 / 90)) < 0.01);
    }
    /* 2 kg, 5 N drag, 8 N grip: accelerating 20 -> 40 mm/s in one step needs
     * 5 + 2 * 6.4 = 17.8 N and slips; cruising at 40 mm/s needs 5 N. */
    m = parameters; m.drag_n = 5; m.moving_kg = 2; m.grip_n = 8;
    mmu_sim_plant_t p;
    assert(mmu_sim_init(&p, &m, 0, 0));
    steps_every(&p, 1, 1, 200000);
    steps_every(&p, 1, 1, 6250);
    steps_every(&p, 1, 1, 3125);
    steps_every(&p, 1, 20, 3125);
    assert(p.slipped_steps == 1 && p.stalled_steps == 0);
    near(p.lost_travel_mm, m.step_mm);
    near(mmu_sim_material_error_mm(&p), 0);
    assert(mmu_sim_load_ratio(&p) == 0); /* no motor limit */
}

static void travel_and_slip(void)
{
    mmu_sim_plant_t p;
    assert(mmu_sim_init(&p, &parameters, 0, 0));
    steps(&p, 1, 80); near(p.material_mm, 10); assert(!p.hub_present);
    steps(&p, 1, 2); assert(p.hub_present && !p.arrival_present);
    steps(&p, -1, 4); near(p.material_mm, 9.75); assert(p.hub_present);
    steps(&p, -1, 1); assert(!p.hub_present);
    steps(&p, 1, 165); near(p.material_mm, 30.25); assert(p.arrival_present);
    near(mmu_sim_buffer_mm(&p), 0.25);
    near(p.spool_payout_mm, 30.25); near(p.upstream_slack_mm, 0);
    p.blocked = true; steps(&p, 1, 8); near(p.material_mm, 30.25); near(p.lost_travel_mm, 1);
    p.blocked = false;
    mmu_sim_parameters_t slipping = parameters; slipping.forward_transfer = 0.5;
    assert(mmu_sim_init(&p, &slipping, 0, 0));
    steps(&p, 1, 160); near(p.material_mm, 10); near(p.lost_travel_mm, 10);
    assert(p.shaft_steps == 160 && !p.arrival_present);
    steps(&p, -1, 80); near(p.material_mm, 0); near(p.upstream_slack_mm, 10);
    near(p.spool_payout_mm, 10);
}

static void limits_and_consumption(void)
{
    mmu_sim_plant_t p; assert(mmu_sim_init(&p, &parameters, 0, 0));
    steps(&p, -1, 8); near(p.material_mm, 0); near(p.lost_travel_mm, 1);
    steps(&p, 1, 408); near(p.material_mm, 50); near(p.lost_travel_mm, 2);
    assert(mmu_sim_advance(&p, 1000000, 7));
    near(p.material_mm, 43); near(p.consumed_mm, 7); near(mmu_sim_buffer_mm(&p), 13);
    assert(mmu_sim_advance(&p, 2000000, 10));
    near(p.material_mm, 30); near(p.consumed_mm, 20); near(p.unmet_consumption_mm, 7);
    assert(p.arrival_present && p.time_us == 3000000);
    near(mmu_sim_material_error_mm(&p), 0);
}

static void optional_load_switch(void)
{
    mmu_sim_plant_t p;
    assert(mmu_sim_init(&p, &parameters, 0, 0));
    steps(&p, 1, 90);
    assert(!p.load_present);
    mmu_sim_parameters_t local = parameters;
    local.load_mm = 5;
    assert(mmu_sim_init(&p, &local, 0, 0));
    steps(&p, 1, 40); assert(!p.load_present && !p.hub_present);
    steps(&p, 1, 2); assert(p.load_present && !p.hub_present);
    steps(&p, -1, 4); assert(p.load_present);
    steps(&p, -1, 1); assert(!p.load_present);
    mmu_sim_plant_t before = p;
    local.load_mm = local.hub_mm;
    assert(!mmu_sim_init(&p, &local, 0, 0));
    assert(memcmp(&p, &before, sizeof(p)) == 0);
}

static void rewind_response(void)
{
    mmu_sim_plant_t whole, chunks;
    assert(mmu_sim_init(&whole, &parameters, 40, 20)); chunks = whole;
    assert(mmu_sim_rewind(&whole, 1000) && mmu_sim_rewind(&chunks, 1000));
    assert(mmu_sim_advance(&whole, 1000000, 2));
    for (unsigned i = 0; i < 1000; ++i) assert(mmu_sim_advance(&chunks, 1000, 2));
    near(whole.rewind_velocity_mm_s, 6.321205588285577);
    near(whole.upstream_slack_mm, 16.321205588285577);
    near(whole.consumed_mm, 2); near(whole.material_mm, 38);
    near(chunks.upstream_slack_mm, whole.upstream_slack_mm);
    near(chunks.rewind_velocity_mm_s, whole.rewind_velocity_mm_s);
    near(chunks.material_mm, whole.material_mm);
    near(mmu_sim_material_error_mm(&whole), 0);
    near(mmu_sim_material_error_mm(&chunks), 0);
    assert(mmu_sim_rewind(&whole, 0));
    assert(mmu_sim_advance(&whole, 1000000, 0));
    near(whole.rewind_velocity_mm_s, 2.325441579348296);
    near(whole.upstream_slack_mm, 12.325441579348296);
    mmu_sim_parameters_t instant = parameters; instant.rewind_time_constant_s = 0;
    assert(mmu_sim_init(&whole, &instant, 0, 1));
    assert(mmu_sim_rewind(&whole, 1000));
    assert(mmu_sim_advance(&whole, 1000000, 0));
    near(whole.upstream_slack_mm, 0); near(whole.material_mm, 0);
    near(whole.spool_payout_mm, -1); near(mmu_sim_material_error_mm(&whole), 0);
}

static void rejected_inputs(void)
{
    mmu_sim_plant_t p; assert(mmu_sim_init(&p, &parameters, 5, 2));
    mmu_sim_plant_t before = p;
    const double invalid[] = {NAN, INFINITY, -1};
    for (unsigned i = 0; i < sizeof(invalid) / sizeof(invalid[0]); ++i) {
        assert(!mmu_sim_advance(&p, 1000, invalid[i]));
        assert(!mmu_sim_init(&p, &parameters, invalid[i], 0));
        assert(memcmp(&p, &before, sizeof(p)) == 0);
        mmu_sim_parameters_t bad = parameters; bad.forward_transfer = invalid[i];
        assert(!mmu_sim_init(&p, &bad, 0, 0));
        assert(memcmp(&p, &before, sizeof(p)) == 0);
    }
    assert(!mmu_sim_step(&p, 0) && !mmu_sim_step(&p, 2));
    assert(!mmu_sim_rewind(&p, 1001));
    assert(memcmp(&p, &before, sizeof(p)) == 0);
    p.time_us = UINT64_MAX;
    assert(!mmu_sim_advance(&p, 1, 0) && p.time_us == UINT64_MAX);
    p.shaft_steps = INT64_MAX; assert(!mmu_sim_step(&p, 1));
    p.shaft_steps = INT64_MIN; assert(!mmu_sim_step(&p, -1));
}
static void bay_retreat_edge(void)
{
    mmu_sim_parameters_t edge = parameters;
    edge.bay_clear_mm = 2;
    mmu_sim_plant_t p;
    assert(mmu_sim_init(&p, &edge, 0, 0) && !p.bay_present);
    steps(&p, 1, 17);
    near(p.material_mm, 2.125);
    assert(!p.bay_present);
    steps(&p, 1, 1);
    near(p.material_mm, 2.25);
    assert(p.bay_present);
    steps(&p, -1, 4);
    near(p.material_mm, 1.75);
    assert(p.bay_present);
    steps(&p, -1, 1);
    assert(!p.bay_present);
    near(mmu_sim_material_error_mm(&p), 0);
    mmu_sim_plant_t before = p;
    edge.bay_clear_mm = edge.switch_hysteresis_mm;
    assert(!mmu_sim_init(&p, &edge, 0, 0));
    assert(memcmp(&p, &before, sizeof(p)) == 0);
    edge.bay_clear_mm = 2;
    edge.finite_spool = true;
    edge.spool_budget_mm = 10;
    assert(!mmu_sim_init(&p, &edge, 0, 0));
    assert(memcmp(&p, &before, sizeof(p)) == 0);
}

static void finite_spool(void)
{
    mmu_sim_parameters_t finite = parameters;
    finite.finite_spool = true;
    finite.spool_budget_mm = 1;
    mmu_sim_plant_t p;
    assert(mmu_sim_init(&p, &finite, 0, .5) && p.bay_present);
    steps(&p, 1, 16);
    near(p.material_mm, 1.5);
    near(p.spool_payout_mm, 1);
    near(p.upstream_slack_mm, 0);
    assert(!p.bay_present);
    steps(&p, 1, 1);
    near(p.material_mm, 1.5);
    near(p.lost_travel_mm, .625);
    steps(&p, -1, 1);
    assert(p.bay_present);
    steps(&p, 1, 1);
    assert(!p.bay_present);
    near(mmu_sim_material_error_mm(&p), 0);
    assert(mmu_sim_advance(&p, 1000000, 1));
    near(p.material_mm, 1.5);
    near(p.unmet_consumption_mm, 1);
    assert(!p.trailing_end_active);
    near(mmu_sim_material_error_mm(&p), 0);
    mmu_sim_plant_t before = p;
    const double invalid[] = {NAN, INFINITY, -1};
    for (unsigned i = 0; i < sizeof(invalid) / sizeof(invalid[0]); ++i) {
        finite.spool_budget_mm = invalid[i];
        assert(!mmu_sim_init(&p, &finite, 0, 0));
        assert(memcmp(&p, &before, sizeof(p)) == 0);
    }
    assert(mmu_sim_init(&p, &parameters, 0, 0) && p.bay_present);
}

static void finite_spool_trailing_end(void)
{
    mmu_sim_parameters_t finite = parameters;
    finite.finite_spool = true;
    finite.spool_budget_mm = 20;
    finite.load_mm = 5;
    mmu_sim_plant_t p;
    assert(mmu_sim_init(&p, &finite, 30, 0));
    assert(p.bay_present && p.load_present && p.hub_present && !p.arrival_present);
    steps(&p, 1, 160);
    near(p.material_mm, 50);
    near(p.spool_payout_mm, 20);
    assert(!p.bay_present && p.load_present && p.hub_present && p.arrival_present);
    assert(mmu_sim_advance(&p, 1000000, 20));
    near(p.material_mm, 30);
    assert(p.load_present && p.hub_present && p.arrival_present);
    assert(!p.trailing_end_active);
    assert(mmu_sim_advance(&p, 1000000, 6));
    near(p.material_mm, 24);
    assert(p.trailing_end_active && !p.load_present && p.hub_present && p.arrival_present);
    double lost_before = p.lost_travel_mm;
    int64_t shaft_before = p.shaft_steps;
    steps(&p, -1, 1);
    steps(&p, 1, 1);
    assert(p.shaft_steps == shaft_before);
    near(p.lost_travel_mm, lost_before + 2 * finite.step_mm);
    near(p.material_mm, 24);
    near(p.upstream_slack_mm, 0);
    near(p.spool_payout_mm, 20);
    assert(!p.bay_present && !p.load_present && p.hub_present && p.arrival_present);
    near(mmu_sim_material_error_mm(&p), 0);
    assert(mmu_sim_advance(&p, 1000000, 5));
    near(p.material_mm, 19);
    assert(!p.load_present && !p.hub_present && p.arrival_present);
    assert(mmu_sim_advance(&p, 1000000, 25));
    near(p.material_mm, 0);
    assert(!p.load_present && !p.hub_present && !p.arrival_present);
    near(p.consumed_mm, 50);
    near(p.unmet_consumption_mm, 6);
    near(p.spool_payout_mm, 20);
    near(mmu_sim_material_error_mm(&p), 0);
}

int main(void)
{
    forces(); travel_and_slip(); optional_load_switch(); limits_and_consumption(); rewind_response();
    rejected_inputs(); bay_retreat_edge(); finite_spool(); finite_spool_trailing_end();
    puts("plant: forces, causal travel, slip, hysteresis, conservation, consumption and rewind passed");
    return 0;
}
