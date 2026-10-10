// SPDX-License-Identifier: GPL-3.0-or-later
/* Startup composition tests. gpio_local_reserve is a model, not the registry. */
#define main legacy_main
#define gpio_local_reserve legacy_gpio_local_reserve
#include "test_board_startup.c"
#undef main
#undef gpio_local_reserve

#include "stm32/gpio_local.h"

#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAP_CAP KM_RESOURCE_MAX_CLAIMS
#define KIND_BATCH 1u
#define KIND_ORDINARY 2u
#define KIND_LOCAL 3u

static uint32_t map_id[MAP_CAP];
static uint8_t map_kind[MAP_CAP];
static uint32_t map_len;
static uint32_t seen[KM_RESOURCE_MAX_CLAIMS];
static uint32_t seen_count;
static uint32_t reserve_calls;
static int forced_status;
static unsigned post_reserve;
static uint32_t post_step;

static int map_find(uint32_t id)
{
    for (uint32_t i = 0; i < map_len; i++) {
        if (map_kind[i] != 0u && map_id[i] == id)
            return (int)i;
    }
    return -1;
}

static void map_add(uint32_t id, unsigned kind)
{
    assert(map_find(id) < 0);
    assert(map_len < MAP_CAP);
    map_id[map_len] = id;
    map_kind[map_len] = (uint8_t)kind;
    map_len++;
}

static void snapshot(uint32_t *ids, uint8_t *kinds, uint32_t *len)
{
    memcpy(ids, map_id, sizeof(map_id));
    memcpy(kinds, map_kind, sizeof(map_kind));
    *len = map_len;
}

static void same_map(const uint32_t *ids, const uint8_t *kinds, uint32_t len)
{
    assert(map_len == len);
    assert(memcmp(map_id, ids, sizeof(map_id)) == 0);
    assert(memcmp(map_kind, kinds, sizeof(map_kind)) == 0);
}

static void no_component_activity(void)
{
    assert(sim.switch_setup_count == 0u);
    assert(sim.switch_shutdown_count == 0u);
    assert(sim.adc_start_count == 0u);
    assert(sim.adc_shutdown_count == 0u);
    assert(sim.feeder_setup_count == 0u);
    assert(sim.feeder_activate_count == 0u);
    assert(sim.pwm_setup_count == 0u);
    assert(sim.pwm_shutdown_count == 0u);
    assert(sim.stepdir_shutdown_count == 0u);
    assert(sim.controller_attach_count == 0u);
    assert(sim.controller_shutdown_count == 0u);
    assert(sim.gpio_ops_count == 0u);
}

static void apply_post(void)
{
    if (post_reserve == 1u)
        sim.now_us += post_step;
    else if (post_reserve == 2u)
        sim.clock_state = KM_CLOCK_STATE_UNAVAILABLE;
    else if (post_reserve == 3u)
        sim.clock_state = KM_CLOCK_STATE_FAULTED;
    else if (post_reserve == 4u)
        sim.sched_shutdown = 1;
    else if (post_reserve == 5u)
        km_board_startup_shutdown();
}

int gpio_local_reserve(const uint32_t *pins, uint32_t count)
{
    no_component_activity();
    assert(sim.irq_status == 0ul);
    assert(sim.clock_read_count > 0u);
    reserve_calls++;
    seen_count = 0u;
    if (pins == NULL || count == 0u || count > KM_RESOURCE_MAX_CLAIMS)
        return GPIO_LOCAL_ERR_INVALID;
    memcpy(seen, pins, count * sizeof(*pins));
    seen_count = count;
    if (forced_status)
        return forced_status;
    for (uint32_t i = 0; i < count; i++) {
        if (map_find(pins[i]) >= 0)
            return GPIO_LOCAL_ERR_BUSY;
        for (uint32_t j = 0; j < i; j++) {
            if (pins[i] == pins[j])
                return GPIO_LOCAL_ERR_INVALID;
        }
    }
    if (map_len + count > MAP_CAP)
        return GPIO_LOCAL_ERR_INVALID;
    for (uint32_t i = 0; i < count; i++)
        map_add(pins[i], KIND_BATCH);
    apply_post();
    return GPIO_LOCAL_OK;
}

static void assert_seen_pins(void)
{
    unsigned n = 0u;
    for (size_t i = 0; i < sim.expected_plan.count; i++) {
        const km_resource_claim_t *c = &sim.expected_plan.claims[i];
        if (c->kind != KM_RESOURCE_PIN)
            continue;
        assert(n < seen_count);
        assert(seen[n] == (uint32_t)c->instance);
        n++;
    }
    assert(n == seen_count);
}

static void assert_reserved(void)
{
    assert_seen_pins();
    for (uint32_t i = 0; i < seen_count; i++) {
        int slot = map_find(seen[i]);
        assert(slot >= 0);
        assert(map_kind[slot] == KIND_BATCH);
    }
}

static uint32_t pin_id(const km_resource_claim_t *claims, size_t count, unsigned index)
{
    unsigned n = 0u;
    for (size_t i = 0; i < count; i++) {
        if (claims[i].kind != KM_RESOURCE_PIN)
            continue;
        if (n == index)
            return (uint32_t)claims[i].instance;
        n++;
    }
    return UINT32_MAX;
}

static unsigned pin_total(const km_resource_claim_t *claims, size_t count)
{
    unsigned n = 0u;
    for (size_t i = 0; i < count; i++) {
        if (claims[i].kind == KM_RESOURCE_PIN)
            n++;
    }
    return n;
}

static void fill_claims(km_resource_claim_t *claims, size_t *count)
{
    *count = populate_claims(claims);
}

static void case_order(void)
{
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS + 1u];
    size_t count;
    km_operation_core_t *core;

    sim.expected_plan.claims[0].instance = 257u;
    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){
        .kind = KM_RESOURCE_ADC_UNIT, .instance = 1u, .owner = 99u
    };
    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){
        .kind = KM_RESOURCE_ADC_CHANNEL, .instance = 1u, .subresource = 4u, .owner = 99u
    };
    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){
        .kind = KM_RESOURCE_TIMER_EXCLUSIVE, .instance = 2u, .owner = 100u
    };
    fill_claims(claims, &count);
    memset(&claims[count], 0, sizeof(claims[count]));
    claims[count].kind = KM_RESOURCE_PIN;
    claims[count].instance = 258u;
    claims[count].owner = 101u;
    count++;
    map_add(258u, KIND_ORDINARY);
    sim.lane_status[0] = LANE_STATUS_UNAVAILABLE;
    sim.lane_status[1] = LANE_STATUS_UNAVAILABLE;

    assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_OK);
    assert(reserve_calls == 1u);
    assert_reserved();
    assert(seen[0] == 257u);
    assert(seen_count == 8u);
    assert(map_kind[map_find(258u)] == KIND_ORDINARY);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_WAITING);
    assert(km_board_startup_get_result() == KM_ERR_UNAVAILABLE);
    assert(km_board_startup_get_core() == NULL);

    sim.now_us += 1000u;
    assert(km_board_startup_service() == KM_ERR_UNAVAILABLE);
    sim.lane_status[0] = LANE_STATUS_READY;
    sim.now_us += 1000u;
    assert(km_board_startup_service() == KM_ERR_UNAVAILABLE);
    sim.lane_status[1] = LANE_STATUS_READY;
    sim.now_us += 1000u;
    assert(km_board_startup_service() == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);
    assert(km_board_startup_get_result() == KM_OK);
    core = km_board_startup_get_core();
    assert(core != NULL);
    assert(core->feeder_drivers[0] == &sim_feeder_driver[0].base);
    assert(core->feeder_drivers[1] == &sim_feeder_driver[1].base);
    assert(reserve_calls == 1u);
    assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_ERR_BUSY);
    assert(reserve_calls == 1u);
    assert_reserved();
    assert(map_kind[map_find(258u)] == KIND_ORDINARY);
}

static void case_boundary(void)
{
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count;

    sim.expected_plan.count = KM_RESOURCE_MAX_CLAIMS;
    for (unsigned i = 0; i < KM_RESOURCE_MAX_CLAIMS; i++) {
        memset(&sim.expected_plan.claims[i], 0, sizeof(sim.expected_plan.claims[i]));
        sim.expected_plan.claims[i].kind = KM_RESOURCE_PIN;
        sim.expected_plan.claims[i].instance = (uint16_t)i;
        sim.expected_plan.claims[i].owner = 99u;
    }
    fill_claims(claims, &count);
    assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_OK);
    assert(reserve_calls == 1u);
    assert(seen_count == KM_RESOURCE_MAX_CLAIMS);
    assert_reserved();
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_WAITING);
    sim.lane_status[0] = LANE_STATUS_READY;
    sim.lane_status[1] = LANE_STATUS_READY;
    assert(km_board_startup_service() == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);
    assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_ERR_BUSY);
    assert(reserve_calls == 1u);
    assert_reserved();
}

static void case_mapping(unsigned n)
{
    static const int status[3] = {
        GPIO_LOCAL_ERR_INVALID, GPIO_LOCAL_ERR_BUSY, GPIO_LOCAL_ERR_SHUTDOWN
    };
    static const km_result_t want[3] = {
        KM_ERR_INVALID_PARAM, KM_ERR_CONFLICT, KM_ERR_FAULTED
    };
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count;
    uint32_t ids[MAP_CAP];
    uint8_t kinds[MAP_CAP];
    uint32_t len;

    fill_claims(claims, &count);
    forced_status = status[n];
    snapshot(ids, kinds, &len);
    assert(km_board_startup_begin(claims, count, 50000u, NULL) == want[n]);
    assert(reserve_calls == 1u);
    assert_seen_pins();
    same_map(ids, kinds, len);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_UNINITIALIZED);
    assert(km_board_startup_get_result() == KM_ERR_UNAVAILABLE);
    no_component_activity();
    forced_status = 0;
    assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_OK);
    assert(reserve_calls == 2u);
    assert_reserved();
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_WAITING);
}

static void case_conflict(unsigned index, unsigned kind)
{
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count;
    uint32_t id;
    uint32_t ids[MAP_CAP];
    uint8_t kinds[MAP_CAP];
    uint32_t len;

    fill_claims(claims, &count);
    if (index >= pin_total(claims, count))
        exit(2);
    id = pin_id(claims, count, index);
    map_add(id, kind);
    snapshot(ids, kinds, &len);
    assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_ERR_CONFLICT);
    assert(reserve_calls == 1u);
    assert_seen_pins();
    same_map(ids, kinds, len);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_UNINITIALIZED);
    no_component_activity();
    if (kind == KIND_ORDINARY) {
        map_kind[map_find(id)] = 0u;
        assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_OK);
        assert(reserve_calls == 2u);
        assert_reserved();
        assert(km_board_startup_get_state() == KM_BOARD_STARTUP_WAITING);
    } else {
        assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_ERR_CONFLICT);
        assert(reserve_calls == 2u);
        same_map(ids, kinds, len);
        assert(map_kind[map_find(id)] == KIND_LOCAL);
        assert(km_board_startup_get_state() == KM_BOARD_STARTUP_UNINITIALIZED);
        assert(km_board_startup_get_result() == KM_ERR_UNAVAILABLE);
        no_component_activity();
    }
}

static void case_deadline(unsigned n)
{
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count;
    const uint32_t timeout = 50000u;
    km_result_t want = n <= 1u ? KM_ERR_TIMEOUT : KM_ERR_FAULTED;

    if (n == 1u)
        sim.now_us = 0u - timeout;
    post_reserve = n <= 1u ? 1u : n;
    post_step = timeout;
    fill_claims(claims, &count);
    assert(km_board_startup_begin(claims, count, timeout, NULL) == want);
    assert(reserve_calls == 1u);
    assert_reserved();
    no_component_activity();
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_get_result() == want);
    sched_clear_shutdown();
    sim.clock_state = KM_CLOCK_STATE_READY;
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_get_result() == want);
    assert(km_board_startup_begin(claims, count, timeout, NULL) == KM_ERR_FAULTED);
    assert(reserve_calls == 1u);
    assert_reserved();
    no_component_activity();
    assert(sim.irq_status == 0ul);
}

static void case_switch_busy(void)
{
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count;

    sim.sim_switch_setup_busy = true;
    fill_claims(claims, &count);
    assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_ERR_BUSY);
    assert(reserve_calls == 1u);
    assert_reserved();
    no_component_activity();
    assert(sim.switch_setup_count == 0u);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_get_result() == KM_ERR_BUSY);
    sched_clear_shutdown();
    sim.sim_switch_setup_busy = false;
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_ERR_FAULTED);
    assert(reserve_calls == 1u);
    assert_reserved();
    no_component_activity();
}

static void case_configured(void)
{
    km_operation_core_t *core;

    sim.lane_status[0] = LANE_STATUS_UNAVAILABLE;
    sim.lane_status[1] = LANE_STATUS_UNAVAILABLE;
    assert(km_board_startup_begin(NULL, 0u, 50000u, NULL) == KM_OK);
    assert(reserve_calls == 1u);
    assert_reserved();
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_WAITING);
    assert(km_board_startup_get_core() == NULL);

    sim.lane_status[0] = LANE_STATUS_READY;
    sim.lane_status[1] = LANE_STATUS_READY;
    assert(km_board_startup_service() == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);
    core = km_board_startup_get_core();
    assert(core != NULL);
    assert(core->feeder_drivers[0] == &sim_feeder_driver[0].base);
    assert(core->feeder_drivers[1] == &sim_feeder_driver[1].base);
    assert(km_board_startup_begin(NULL, 0u, 50000u, NULL) == KM_ERR_BUSY);
    assert(reserve_calls == 1u);
    assert_reserved();
}

static void case_configured_conflict(unsigned kind)
{
    uint32_t ids[MAP_CAP];
    uint8_t kinds[MAP_CAP];
    uint32_t len;
    uint32_t id = pin_id(sim.expected_plan.claims, sim.expected_plan.count, 0u);

    assert(id != UINT32_MAX);
    map_add(id, kind);
    snapshot(ids, kinds, &len);
    assert(km_board_startup_begin(NULL, 0u, 50000u, NULL) == KM_ERR_CONFLICT);
    assert(reserve_calls == 1u);
    assert_seen_pins();
    same_map(ids, kinds, len);
    assert(map_kind[map_find(id)] == (uint8_t)kind);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_UNINITIALIZED);
    assert(km_board_startup_get_core() == NULL);
    no_component_activity();
}

static void case_configured_deadline(void)
{
    const uint32_t timeout = 50000u;

    post_reserve = 1u;
    post_step = timeout;
    assert(km_board_startup_begin(NULL, 0u, timeout, NULL) == KM_ERR_TIMEOUT);
    assert(reserve_calls == 1u);
    assert_reserved();
    no_component_activity();
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_get_result() == KM_ERR_TIMEOUT);
    assert(km_board_startup_get_core() == NULL);
    sched_clear_shutdown();
    sim.clock_state = KM_CLOCK_STATE_READY;
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_get_result() == KM_ERR_TIMEOUT);
    assert(km_board_startup_get_core() == NULL);
    assert(km_board_startup_begin(NULL, 0u, timeout, NULL) == KM_ERR_FAULTED);
    assert(reserve_calls == 1u);
    assert_reserved();
    no_component_activity();
}

static void case_adc_budget(unsigned n)
{
    km_diff_adc_config_t cal;
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count;
    const uint32_t budget = 100u;
    km_result_t res;

    sim.profile.has_proportional_buffer = true;
    sim.profile.has_powered_rewind = false;
    sim.profile.proportional_gain_milli = 8000;
    sim.profile.buffer_deadband_permille = 10;
    cal = (km_diff_adc_config_t){
        .channel_map = KM_DIFF_CH_A_LOW_B_HIGH,
        .vref_mv = 3300,
        .adc_max_counts = 4095,
        .path_tolerance_counts = 32,
        .point_count = 3,
        .points = {
            {0, 2048, 0},
            {2048, 2048, 500},
            {2048, 4095, 1000}
        }
    };
    sim.profile.diff_calibration = cal;
    sim.profile.has_diff_calibration = true;
    assert(km_profile_validate(&sim.profile) == KM_OK);
    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){
        .kind = KM_RESOURCE_ADC_UNIT, .instance = 1u, .owner = KM_H723_OWNER_DIFF_ADC
    };
    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){
        .kind = KM_RESOURCE_ADC_CHANNEL, .instance = 1u, .subresource = 4u,
        .owner = KM_H723_OWNER_DIFF_ADC
    };
    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){
        .kind = KM_RESOURCE_ADC_CHANNEL, .instance = 1u, .subresource = 8u,
        .owner = KM_H723_OWNER_DIFF_ADC
    };
    if (n == 1u)
        sim.now_us = UINT32_MAX - 10u;
    post_reserve = 1u;
    post_step = 7u;
    sim.sim_switch_setup_cost_us = (n == 2u) ? 93u : 11u;
    if (n == 3u) {
        sim.sim_adc_cost_us = 82u;
        sim.sim_adc_result = KM_ERR_TIMEOUT;
    } else if (n == 4u) {
        sim.sim_adc_cost_us = 82u;
        sim.sim_adc_result = KM_OK;
    }
    sim.lane_status[0] = LANE_STATUS_UNAVAILABLE;
    sim.lane_status[1] = LANE_STATUS_UNAVAILABLE;
    fill_claims(claims, &count);
    res = km_board_startup_begin(claims, count, budget, &cal);
    assert(reserve_calls == 1u);
    assert_reserved();
    assert(sim.pwm_setup_count == 0u);
    if (n <= 1u) {
        assert(res == KM_OK);
        assert(sim.captured_adc_timeout_us == 82u);
        assert(sim.adc_start_count == 1u);
        assert(km_board_startup_get_state() == KM_BOARD_STARTUP_WAITING);
        assert(km_board_startup_get_core() == NULL);
        return;
    }
    assert(res == KM_ERR_TIMEOUT);
    assert(sim.feeder_setup_count == 0u);
    assert(sim.controller_attach_count == 0u);
    assert(sim.stepdir_shutdown_count == 0u);
    assert(sim.pwm_shutdown_count == 0u);
    assert(sim.adc_stop_count == 0u);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_get_result() == KM_ERR_TIMEOUT);
    assert(km_board_startup_get_core() == NULL);
    if (n == 2u) {
        assert(sim.adc_start_count == 0u);
        assert(sim.adc_shutdown_count == 0u);
        assert(sim.switch_setup_count == 1u);
        assert(sim.switch_shutdown_count == 1u);
    } else {
        assert(sim.captured_adc_timeout_us == 82u);
        assert(sim.adc_start_count == 1u);
        assert(sim.adc_shutdown_count == 1u);
        assert(sim.adc_state == KM_ADC_STATE_SHUTDOWN);
        assert(sim.switch_shutdown_count == 1u);
    }
    sched_clear_shutdown();
    sim.clock_state = KM_CLOCK_STATE_READY;
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_get_result() == KM_ERR_TIMEOUT);
    assert(km_board_startup_begin(claims, count, budget, &cal) == KM_ERR_FAULTED);
    assert(reserve_calls == 1u);
    assert_reserved();
}

static void case_partial(int feeder)
{
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count;

    if (feeder)
        sim.sim_feeder_setup_fail_lane = 0;
    else
        sim.sim_switch_setup_fail = true;
    fill_claims(claims, &count);
    assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_ERR_FAULTED);
    assert(reserve_calls == 1u);
    assert_reserved();
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(sim.switch_shutdown_count >= 1u);
    assert(sim.adc_shutdown_count == 0u);
    assert(sim.pwm_shutdown_count == 0u);
    assert(sim.controller_shutdown_count == 0u);
    if (feeder) {
        assert(sim.switch_setup_count == 1u);
        assert(sim.feeder_setup_count >= 1u);
        assert(sim.stepdir_shutdown_count >= 1u);
    } else {
        assert(sim.switch_setup_count == 1u);
        assert(sim.feeder_setup_count == 0u);
        assert(sim.stepdir_shutdown_count == 0u);
    }
    assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_ERR_FAULTED);
    assert(reserve_calls == 1u);
    assert_reserved();
}

int main(int argc, char **argv)
{
    unsigned long argn;
    unsigned n;

    if (argc != 3) {
        fprintf(stderr, "usage: %s <case> <n>\n", argc > 0 ? argv[0] : "test_batch");
        return 2;
    }
    argn = strtoul(argv[2], NULL, 10);
    if (argn > 128ul)
        return 2;
    n = (unsigned)argn;
    reset_sim();
    if (strcmp(argv[1], "order") == 0)
        case_order();
    else if (strcmp(argv[1], "boundary") == 0)
        case_boundary();
    else if (strcmp(argv[1], "validation") == 0) {
        test_validation_no_io_rejection();
        assert(reserve_calls == 0u);
        no_component_activity();
    } else if (strcmp(argv[1], "mapping") == 0) {
        if (n > 2u)
            return 2;
        case_mapping(n);
    } else if (strcmp(argv[1], "ordinary") == 0)
        case_conflict(n, KIND_ORDINARY);
    else if (strcmp(argv[1], "local") == 0)
        case_conflict(n, KIND_LOCAL);
    else if (strcmp(argv[1], "deadline") == 0) {
        if (n > 5u)
            return 2;
        case_deadline(n);
    } else if (strcmp(argv[1], "switch_busy") == 0)
        case_switch_busy();
    else if (strcmp(argv[1], "switch_fail") == 0)
        case_partial(0);
    else if (strcmp(argv[1], "feeder_fail") == 0)
        case_partial(1);
    else if (strcmp(argv[1], "configured") == 0)
        case_configured();
    else if (strcmp(argv[1], "configured_ordinary") == 0)
        case_configured_conflict(KIND_ORDINARY);
    else if (strcmp(argv[1], "configured_local") == 0)
        case_configured_conflict(KIND_LOCAL);
    else if (strcmp(argv[1], "configured_deadline") == 0)
        case_configured_deadline();
    else if (strcmp(argv[1], "adc_budget") == 0) {
        if (n > 4u)
            return 2;
        case_adc_budget(n);
    } else {
        fprintf(stderr, "unknown case %s\n", argv[1]);
        return 2;
    }
    puts("pass");
    return 0;
}
