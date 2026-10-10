// SPDX-License-Identifier: GPL-3.0-or-later
#include "klipper_mmu/resource_plan.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>

/* =========================================================================
 * Test Section 1: Plan Arguments, Capacity, Diagnostics, and Immutability
 * ========================================================================= */
static void test_plan_arguments_and_capacity(void)
{
    printf("Running test_plan_arguments_and_capacity...\n");

    km_resource_diagnostic_t diag;

    /* Null claims, count 0: valid empty plan */
    memset(&diag, 0xAA, sizeof(diag));
    km_result_t res = km_resource_plan_validate(NULL, 0, &diag);
    assert(res == KM_OK);
    assert(diag.reason == KM_RESOURCE_DIAG_NONE);
    assert(diag.first_index == KM_RESOURCE_NO_INDEX);
    assert(diag.second_index == KM_RESOURCE_NO_INDEX);

    /* Non-null claims, count 0: valid empty plan */
    km_resource_claim_t dummy_claim = {
        .kind = KM_RESOURCE_PIN,
        .instance = 0,
        .subresource = 0,
        .owner = 1,
        .period_ticks = 0,
        .tick_hz = 0
    };
    memset(&diag, 0xAA, sizeof(diag));
    res = km_resource_plan_validate(&dummy_claim, 0, &diag);
    assert(res == KM_OK);
    assert(diag.reason == KM_RESOURCE_DIAG_NONE);
    assert(diag.first_index == KM_RESOURCE_NO_INDEX);
    assert(diag.second_index == KM_RESOURCE_NO_INDEX);

    /* Null claims, count 1: invalid parameter (BAD_ARGUMENT) */
    memset(&diag, 0xAA, sizeof(diag));
    res = km_resource_plan_validate(NULL, 1, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_BAD_ARGUMENT);
    assert(diag.first_index == KM_RESOURCE_NO_INDEX);
    assert(diag.second_index == KM_RESOURCE_NO_INDEX);

    /* Null claims, count KM_RESOURCE_MAX_CLAIMS: invalid parameter */
    memset(&diag, 0xAA, sizeof(diag));
    res = km_resource_plan_validate(NULL, KM_RESOURCE_MAX_CLAIMS, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_BAD_ARGUMENT);
    assert(diag.first_index == KM_RESOURCE_NO_INDEX);
    assert(diag.second_index == KM_RESOURCE_NO_INDEX);

    /* Oversize plan: count KM_RESOURCE_MAX_CLAIMS + 1 */
    memset(&diag, 0xAA, sizeof(diag));
    res = km_resource_plan_validate(&dummy_claim, KM_RESOURCE_MAX_CLAIMS + 1u, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_BAD_ARGUMENT);
    assert(diag.first_index == KM_RESOURCE_NO_INDEX);
    assert(diag.second_index == KM_RESOURCE_NO_INDEX);

    /* Oversize plan with NULL claims: BAD_ARGUMENT before dereference */
    memset(&diag, 0xAA, sizeof(diag));
    res = km_resource_plan_validate(NULL, KM_RESOURCE_MAX_CLAIMS + 1u, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_BAD_ARGUMENT);
    assert(diag.first_index == KM_RESOURCE_NO_INDEX);
    assert(diag.second_index == KM_RESOURCE_NO_INDEX);

    /* Exact capacity: KM_RESOURCE_MAX_CLAIMS distinct valid PIN claims */
    km_resource_claim_t capacity_claims[KM_RESOURCE_MAX_CLAIMS];
    for (uint16_t i = 0; i < (uint16_t)KM_RESOURCE_MAX_CLAIMS; ++i) {
        capacity_claims[i] = (km_resource_claim_t){
            .kind = KM_RESOURCE_PIN,
            .instance = i,
            .subresource = 0,
            .owner = 1,
            .period_ticks = 0,
            .tick_hz = 0
        };
    }
    memset(&diag, 0xAA, sizeof(diag));
    res = km_resource_plan_validate(capacity_claims, KM_RESOURCE_MAX_CLAIMS, &diag);
    assert(res == KM_OK);
    assert(diag.reason == KM_RESOURCE_DIAG_NONE);
    assert(diag.first_index == KM_RESOURCE_NO_INDEX);
    assert(diag.second_index == KM_RESOURCE_NO_INDEX);

    /* Optional diagnostic (NULL diagnostic pointer) */
    res = km_resource_plan_validate(NULL, 0, NULL);
    assert(res == KM_OK);
    res = km_resource_plan_validate(capacity_claims, KM_RESOURCE_MAX_CLAIMS, NULL);
    assert(res == KM_OK);
    res = km_resource_plan_validate(NULL, 1, NULL);
    assert(res == KM_ERR_INVALID_PARAM);
    res = km_resource_plan_validate(capacity_claims, KM_RESOURCE_MAX_CLAIMS + 1u, NULL);
    assert(res == KM_ERR_INVALID_PARAM);

    /* Input immutability on success */
    km_resource_claim_t copy_claims[KM_RESOURCE_MAX_CLAIMS];
    memcpy(copy_claims, capacity_claims, sizeof(capacity_claims));
    res = km_resource_plan_validate(capacity_claims, KM_RESOURCE_MAX_CLAIMS, &diag);
    assert(res == KM_OK);
    assert(memcmp(capacity_claims, copy_claims, sizeof(capacity_claims)) == 0);

    /* Input immutability on record failure */
    capacity_claims[5].owner = 0; /* Invalid owner */
    memcpy(copy_claims, capacity_claims, sizeof(capacity_claims));
    res = km_resource_plan_validate(capacity_claims, KM_RESOURCE_MAX_CLAIMS, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_BAD_CLAIM);
    assert(diag.first_index == 5);
    assert(memcmp(capacity_claims, copy_claims, sizeof(capacity_claims)) == 0);

    /* Input immutability on conflict failure */
    capacity_claims[5].owner = 1; /* Restore valid owner */
    capacity_claims[5].instance = capacity_claims[0].instance; /* Conflict between index 0 and 5 */
    memcpy(copy_claims, capacity_claims, sizeof(capacity_claims));
    res = km_resource_plan_validate(capacity_claims, KM_RESOURCE_MAX_CLAIMS, &diag);
    assert(res == KM_ERR_CONFLICT);
    assert(diag.reason == KM_RESOURCE_DIAG_DUPLICATE_RESOURCE);
    assert(diag.first_index == 0);
    assert(diag.second_index == 5);
    assert(memcmp(capacity_claims, copy_claims, sizeof(capacity_claims)) == 0);

    /* Input immutability on missing companion */
    km_resource_claim_t orphan_chan = {
        .kind = KM_RESOURCE_TIMER_CHANNEL,
        .instance = 1,
        .subresource = 1,
        .owner = 1,
        .period_ticks = 0,
        .tick_hz = 0
    };
    km_resource_claim_t orphan_copy = orphan_chan;
    res = km_resource_plan_validate(&orphan_chan, 1, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_MISSING_TIMER_BASE);
    assert(diag.first_index == 0);
    assert(memcmp(&orphan_chan, &orphan_copy, sizeof(orphan_chan)) == 0);

    printf("  -> Passed test_plan_arguments_and_capacity\n");
}

/* =========================================================================
 * Test Section 2: Individual Record Validation and Bounds
 * ========================================================================= */
static void test_record_validation_field_boundaries(void)
{
    printf("Running test_record_validation_field_boundaries...\n");

    km_resource_diagnostic_t diag;

    /* Out-of-range kind: 0 */
    km_resource_claim_t bad_kind_0 = {
        .kind = (km_resource_kind_t)0,
        .instance = 1,
        .subresource = 0,
        .owner = 1,
        .period_ticks = 0,
        .tick_hz = 0
    };
    km_result_t res = km_resource_plan_validate(&bad_kind_0, 1, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_BAD_CLAIM);
    assert(diag.first_index == 0);
    assert(diag.second_index == KM_RESOURCE_NO_INDEX);

    /* Out-of-range kind: 7 */
    km_resource_claim_t bad_kind_7 = bad_kind_0;
    bad_kind_7.kind = (km_resource_kind_t)7;
    res = km_resource_plan_validate(&bad_kind_7, 1, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_BAD_CLAIM);
    assert(diag.first_index == 0);

    /* Negative cast kind */
    km_resource_claim_t bad_kind_neg = bad_kind_0;
    bad_kind_neg.kind = (km_resource_kind_t)(-1);
    res = km_resource_plan_validate(&bad_kind_neg, 1, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_BAD_CLAIM);
    assert(diag.first_index == 0);

    /* Out-of-range instance: UINT16_MAX (65535) is reserved/invalid */
    km_resource_claim_t bad_instance = {
        .kind = KM_RESOURCE_PIN,
        .instance = UINT16_MAX,
        .subresource = 0,
        .owner = 1,
        .period_ticks = 0,
        .tick_hz = 0
    };
    res = km_resource_plan_validate(&bad_instance, 1, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_BAD_CLAIM);
    assert(diag.first_index == 0);

    /* Valid instance 0 and valid high instance UINT16_MAX - 1 */
    km_resource_claim_t valid_inst_0 = {
        .kind = KM_RESOURCE_PIN,
        .instance = 0,
        .subresource = 0,
        .owner = 1,
        .period_ticks = 0,
        .tick_hz = 0
    };
    res = km_resource_plan_validate(&valid_inst_0, 1, &diag);
    assert(res == KM_OK);
    assert(diag.reason == KM_RESOURCE_DIAG_NONE);

    km_resource_claim_t valid_inst_max = valid_inst_0;
    valid_inst_max.instance = UINT16_MAX - 1u;
    res = km_resource_plan_validate(&valid_inst_max, 1, &diag);
    assert(res == KM_OK);

    /* Out-of-range owner: 0 is invalid */
    km_resource_claim_t bad_owner_0 = valid_inst_0;
    bad_owner_0.owner = 0;
    res = km_resource_plan_validate(&bad_owner_0, 1, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_BAD_CLAIM);
    assert(diag.first_index == 0);

    /* Out-of-range owner: UINT16_MAX is invalid */
    km_resource_claim_t bad_owner_max = valid_inst_0;
    bad_owner_max.owner = UINT16_MAX;
    res = km_resource_plan_validate(&bad_owner_max, 1, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_BAD_CLAIM);
    assert(diag.first_index == 0);

    /* Subresource rules:
     * PIN, TIMER_EXCLUSIVE, TIMER_PWM_BASE, ADC_UNIT require subresource == 0 */
    km_resource_claim_t bad_sub_pin = valid_inst_0;
    bad_sub_pin.subresource = 1;
    res = km_resource_plan_validate(&bad_sub_pin, 1, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_BAD_CLAIM);

    km_resource_claim_t bad_sub_tmr_excl = {
        .kind = KM_RESOURCE_TIMER_EXCLUSIVE,
        .instance = 1,
        .subresource = 1,
        .owner = 1,
        .period_ticks = 0,
        .tick_hz = 0
    };
    res = km_resource_plan_validate(&bad_sub_tmr_excl, 1, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_BAD_CLAIM);

    km_resource_claim_t bad_sub_pwm_base = {
        .kind = KM_RESOURCE_TIMER_PWM_BASE,
        .instance = 1,
        .subresource = 1,
        .owner = 1,
        .period_ticks = 1000,
        .tick_hz = 1000000
    };
    res = km_resource_plan_validate(&bad_sub_pwm_base, 1, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_BAD_CLAIM);

    km_resource_claim_t bad_sub_adc_unit = {
        .kind = KM_RESOURCE_ADC_UNIT,
        .instance = 1,
        .subresource = 1,
        .owner = 1,
        .period_ticks = 0,
        .tick_hz = 0
    };
    res = km_resource_plan_validate(&bad_sub_adc_unit, 1, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_BAD_CLAIM);

    /* TIMER_CHANNEL subresource rules: 1..UINT16_MAX-1 (0 and UINT16_MAX invalid) */
    km_resource_claim_t bad_sub_tmr_chan_0 = {
        .kind = KM_RESOURCE_TIMER_CHANNEL,
        .instance = 1,
        .subresource = 0, /* 0 invalid for timer channel */
        .owner = 1,
        .period_ticks = 0,
        .tick_hz = 0
    };
    res = km_resource_plan_validate(&bad_sub_tmr_chan_0, 1, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_BAD_CLAIM);

    km_resource_claim_t bad_sub_tmr_chan_max = bad_sub_tmr_chan_0;
    bad_sub_tmr_chan_max.subresource = UINT16_MAX;
    res = km_resource_plan_validate(&bad_sub_tmr_chan_max, 1, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_BAD_CLAIM);

    /* ADC_CHANNEL subresource rules: 0..UINT16_MAX-1 (0 is valid, UINT16_MAX invalid) */
    km_resource_claim_t bad_sub_adc_chan_max = {
        .kind = KM_RESOURCE_ADC_CHANNEL,
        .instance = 1,
        .subresource = UINT16_MAX,
        .owner = 1,
        .period_ticks = 0,
        .tick_hz = 0
    };
    res = km_resource_plan_validate(&bad_sub_adc_chan_max, 1, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_BAD_CLAIM);

    /* Timing fields for TIMER_PWM_BASE: period_ticks > 0 and tick_hz > 0 */
    km_resource_claim_t bad_pwm_ticks = {
        .kind = KM_RESOURCE_TIMER_PWM_BASE,
        .instance = 1,
        .subresource = 0,
        .owner = 1,
        .period_ticks = 0, /* Invalid 0 */
        .tick_hz = 1000000
    };
    res = km_resource_plan_validate(&bad_pwm_ticks, 1, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_BAD_CLAIM);

    km_resource_claim_t bad_pwm_hz = bad_pwm_ticks;
    bad_pwm_hz.period_ticks = 1000;
    bad_pwm_hz.tick_hz = 0; /* Invalid 0 */
    res = km_resource_plan_validate(&bad_pwm_hz, 1, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_BAD_CLAIM);

    /* Timing fields for all non-PWM_BASE kinds must be zero */
    km_resource_claim_t bad_pin_ticks = valid_inst_0;
    bad_pin_ticks.period_ticks = 100;
    res = km_resource_plan_validate(&bad_pin_ticks, 1, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_BAD_CLAIM);

    km_resource_claim_t bad_pin_hz = valid_inst_0;
    bad_pin_hz.tick_hz = 10000;
    res = km_resource_plan_validate(&bad_pin_hz, 1, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_BAD_CLAIM);

    /* Record validation order precedence: ascending index order */
    km_resource_claim_t seq[3] = {
        valid_inst_0,
        bad_owner_0, /* Invalid at index 1 */
        bad_kind_7   /* Also invalid at index 2 */
    };
    res = km_resource_plan_validate(seq, 3, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_BAD_CLAIM);
    assert(diag.first_index == 1);
    assert(diag.second_index == KM_RESOURCE_NO_INDEX);

    printf("  -> Passed test_record_validation_field_boundaries\n");
}

/* =========================================================================
 * Test Section 3: Pin Exclusivity and Namespace Independence
 * ========================================================================= */
static void test_pin_semantics_and_namespaces(void)
{
    printf("Running test_pin_semantics_and_namespaces...\n");

    km_resource_diagnostic_t diag;

    /* Same owner duplicate pin conflict */
    km_resource_claim_t same_owner_pin[2] = {
        { .kind = KM_RESOURCE_PIN, .instance = 5, .subresource = 0, .owner = 1, .period_ticks = 0, .tick_hz = 0 },
        { .kind = KM_RESOURCE_PIN, .instance = 5, .subresource = 0, .owner = 1, .period_ticks = 0, .tick_hz = 0 }
    };
    km_result_t res = km_resource_plan_validate(same_owner_pin, 2, &diag);
    assert(res == KM_ERR_CONFLICT);
    assert(diag.reason == KM_RESOURCE_DIAG_DUPLICATE_RESOURCE);
    assert(diag.first_index == 0);
    assert(diag.second_index == 1);

    /* Different owner duplicate pin conflict */
    km_resource_claim_t diff_owner_pin[2] = {
        { .kind = KM_RESOURCE_PIN, .instance = 5, .subresource = 0, .owner = 1, .period_ticks = 0, .tick_hz = 0 },
        { .kind = KM_RESOURCE_PIN, .instance = 5, .subresource = 0, .owner = 2, .period_ticks = 0, .tick_hz = 0 }
    };
    res = km_resource_plan_validate(diff_owner_pin, 2, &diag);
    assert(res == KM_ERR_CONFLICT);
    assert(diag.reason == KM_RESOURCE_DIAG_DUPLICATE_RESOURCE);
    assert(diag.first_index == 0);
    assert(diag.second_index == 1);

    /* Distinct pins do not conflict */
    km_resource_claim_t distinct_pins[2] = {
        { .kind = KM_RESOURCE_PIN, .instance = 0, .subresource = 0, .owner = 1, .period_ticks = 0, .tick_hz = 0 },
        { .kind = KM_RESOURCE_PIN, .instance = 1, .subresource = 0, .owner = 1, .period_ticks = 0, .tick_hz = 0 }
    };
    res = km_resource_plan_validate(distinct_pins, 2, &diag);
    assert(res == KM_OK);
    assert(diag.reason == KM_RESOURCE_DIAG_NONE);

    /* Separate resource kinds have separate ID namespaces:
     * Pin 2, Timer 2, and ADC 2 do not collide just because numeric ID matches */
    km_resource_claim_t separate_namespaces[3] = {
        { .kind = KM_RESOURCE_PIN, .instance = 2, .subresource = 0, .owner = 1, .period_ticks = 0, .tick_hz = 0 },
        { .kind = KM_RESOURCE_TIMER_EXCLUSIVE, .instance = 2, .subresource = 0, .owner = 1, .period_ticks = 0, .tick_hz = 0 },
        { .kind = KM_RESOURCE_ADC_UNIT, .instance = 2, .subresource = 0, .owner = 1, .period_ticks = 0, .tick_hz = 0 }
    };
    res = km_resource_plan_validate(separate_namespaces, 3, &diag);
    assert(res == KM_OK);
    assert(diag.reason == KM_RESOURCE_DIAG_NONE);

    printf("  -> Passed test_pin_semantics_and_namespaces\n");
}

/* =========================================================================
 * Test Section 4: Timer Compatibility, PWM Base Sharing, and Channels
 * ========================================================================= */
static void test_timer_semantics_and_pwm_base_sharing(void)
{
    printf("Running test_timer_semantics_and_pwm_base_sharing...\n");

    km_resource_diagnostic_t diag;

    /* Standalone TIMER_EXCLUSIVE (orphan reservation): valid */
    km_resource_claim_t excl_alone = {
        .kind = KM_RESOURCE_TIMER_EXCLUSIVE,
        .instance = 3,
        .subresource = 0,
        .owner = 1,
        .period_ticks = 0,
        .tick_hz = 0
    };
    km_result_t res = km_resource_plan_validate(&excl_alone, 1, &diag);
    assert(res == KM_OK);
    assert(diag.reason == KM_RESOURCE_DIAG_NONE);

    /* TIMER_EXCLUSIVE conflicts with TIMER_EXCLUSIVE on same timer */
    km_resource_claim_t excl_vs_excl[2] = {
        excl_alone,
        { .kind = KM_RESOURCE_TIMER_EXCLUSIVE, .instance = 3, .subresource = 0, .owner = 2, .period_ticks = 0, .tick_hz = 0 }
    };
    res = km_resource_plan_validate(excl_vs_excl, 2, &diag);
    assert(res == KM_ERR_CONFLICT);
    assert(diag.reason == KM_RESOURCE_DIAG_DUPLICATE_RESOURCE);
    assert(diag.first_index == 0);
    assert(diag.second_index == 1);

    /* TIMER_EXCLUSIVE conflicts with TIMER_PWM_BASE on same timer */
    km_resource_claim_t excl_vs_base[2] = {
        excl_alone,
        { .kind = KM_RESOURCE_TIMER_PWM_BASE, .instance = 3, .subresource = 0, .owner = 2, .period_ticks = 1000, .tick_hz = 1000000 }
    };
    res = km_resource_plan_validate(excl_vs_base, 2, &diag);
    assert(res == KM_ERR_CONFLICT);
    assert(diag.reason == KM_RESOURCE_DIAG_DUPLICATE_RESOURCE);
    assert(diag.first_index == 0);
    assert(diag.second_index == 1);

    /* Reverse order: TIMER_PWM_BASE at 0, TIMER_EXCLUSIVE at 1 */
    km_resource_claim_t base_vs_excl[2] = {
        excl_vs_base[1],
        excl_vs_base[0]
    };
    res = km_resource_plan_validate(base_vs_excl, 2, &diag);
    assert(res == KM_ERR_CONFLICT);
    assert(diag.reason == KM_RESOURCE_DIAG_DUPLICATE_RESOURCE);
    assert(diag.first_index == 0);
    assert(diag.second_index == 1);

    /* TIMER_EXCLUSIVE conflicts with TIMER_CHANNEL on same timer */
    km_resource_claim_t excl_vs_chan[2] = {
        excl_alone,
        { .kind = KM_RESOURCE_TIMER_CHANNEL, .instance = 3, .subresource = 1, .owner = 1, .period_ticks = 0, .tick_hz = 0 }
    };
    res = km_resource_plan_validate(excl_vs_chan, 2, &diag);
    assert(res == KM_ERR_CONFLICT);
    assert(diag.reason == KM_RESOURCE_DIAG_DUPLICATE_RESOURCE);
    assert(diag.first_index == 0);
    assert(diag.second_index == 1);

    /* TIMER_PWM_BASE: same owner duplicate with matching timing -> DUPLICATE_RESOURCE */
    km_resource_claim_t same_owner_base[2] = {
        { .kind = KM_RESOURCE_TIMER_PWM_BASE, .instance = 2, .subresource = 0, .owner = 1, .period_ticks = 1000, .tick_hz = 1000000 },
        { .kind = KM_RESOURCE_TIMER_PWM_BASE, .instance = 2, .subresource = 0, .owner = 1, .period_ticks = 1000, .tick_hz = 1000000 }
    };
    res = km_resource_plan_validate(same_owner_base, 2, &diag);
    assert(res == KM_ERR_CONFLICT);
    assert(diag.reason == KM_RESOURCE_DIAG_DUPLICATE_RESOURCE);
    assert(diag.first_index == 0);
    assert(diag.second_index == 1);

    /* TIMER_PWM_BASE: same owner with mismatched timing -> TIMER_CONFIGURATION */
    km_resource_claim_t same_owner_bad_timing[2] = {
        { .kind = KM_RESOURCE_TIMER_PWM_BASE, .instance = 2, .subresource = 0, .owner = 1, .period_ticks = 1000, .tick_hz = 1000000 },
        { .kind = KM_RESOURCE_TIMER_PWM_BASE, .instance = 2, .subresource = 0, .owner = 1, .period_ticks = 2000, .tick_hz = 1000000 }
    };
    res = km_resource_plan_validate(same_owner_bad_timing, 2, &diag);
    assert(res == KM_ERR_CONFLICT);
    assert(diag.reason == KM_RESOURCE_DIAG_TIMER_CONFIGURATION);
    assert(diag.first_index == 0);
    assert(diag.second_index == 1);

    /* TIMER_PWM_BASE: different owners with matching timing -> valid compatible sharing */
    km_resource_claim_t shared_base[2] = {
        { .kind = KM_RESOURCE_TIMER_PWM_BASE, .instance = 2, .subresource = 0, .owner = 1, .period_ticks = 1000, .tick_hz = 1000000 },
        { .kind = KM_RESOURCE_TIMER_PWM_BASE, .instance = 2, .subresource = 0, .owner = 2, .period_ticks = 1000, .tick_hz = 1000000 }
    };
    res = km_resource_plan_validate(shared_base, 2, &diag);
    assert(res == KM_OK);
    assert(diag.reason == KM_RESOURCE_DIAG_NONE);

    /* TIMER_PWM_BASE: different owners with mismatched period_ticks -> TIMER_CONFIGURATION */
    km_resource_claim_t diff_owner_bad_period[2] = {
        { .kind = KM_RESOURCE_TIMER_PWM_BASE, .instance = 2, .subresource = 0, .owner = 1, .period_ticks = 1000, .tick_hz = 1000000 },
        { .kind = KM_RESOURCE_TIMER_PWM_BASE, .instance = 2, .subresource = 0, .owner = 2, .period_ticks = 2000, .tick_hz = 1000000 }
    };
    res = km_resource_plan_validate(diff_owner_bad_period, 2, &diag);
    assert(res == KM_ERR_CONFLICT);
    assert(diag.reason == KM_RESOURCE_DIAG_TIMER_CONFIGURATION);
    assert(diag.first_index == 0);
    assert(diag.second_index == 1);

    /* TIMER_PWM_BASE: different owners with mismatched tick_hz -> TIMER_CONFIGURATION */
    km_resource_claim_t diff_owner_bad_hz[2] = {
        { .kind = KM_RESOURCE_TIMER_PWM_BASE, .instance = 2, .subresource = 0, .owner = 1, .period_ticks = 1000, .tick_hz = 1000000 },
        { .kind = KM_RESOURCE_TIMER_PWM_BASE, .instance = 2, .subresource = 0, .owner = 2, .period_ticks = 1000, .tick_hz = 2000000 }
    };
    res = km_resource_plan_validate(diff_owner_bad_hz, 2, &diag);
    assert(res == KM_ERR_CONFLICT);
    assert(diag.reason == KM_RESOURCE_DIAG_TIMER_CONFIGURATION);
    assert(diag.first_index == 0);
    assert(diag.second_index == 1);

    /* TIMER_CHANNEL duplicate: same instance and same subresource (same owner) */
    km_resource_claim_t dup_chan_same_owner[3] = {
        { .kind = KM_RESOURCE_TIMER_PWM_BASE, .instance = 2, .subresource = 0, .owner = 1, .period_ticks = 1000, .tick_hz = 1000000 },
        { .kind = KM_RESOURCE_TIMER_CHANNEL,  .instance = 2, .subresource = 1, .owner = 1, .period_ticks = 0, .tick_hz = 0 },
        { .kind = KM_RESOURCE_TIMER_CHANNEL,  .instance = 2, .subresource = 1, .owner = 1, .period_ticks = 0, .tick_hz = 0 }
    };
    res = km_resource_plan_validate(dup_chan_same_owner, 3, &diag);
    assert(res == KM_ERR_CONFLICT);
    assert(diag.reason == KM_RESOURCE_DIAG_DUPLICATE_RESOURCE);
    assert(diag.first_index == 1);
    assert(diag.second_index == 2);

    /* TIMER_CHANNEL duplicate: same instance and same subresource (different owners) */
    km_resource_claim_t dup_chan_diff_owner[4] = {
        { .kind = KM_RESOURCE_TIMER_PWM_BASE, .instance = 2, .subresource = 0, .owner = 1, .period_ticks = 1000, .tick_hz = 1000000 },
        { .kind = KM_RESOURCE_TIMER_PWM_BASE, .instance = 2, .subresource = 0, .owner = 2, .period_ticks = 1000, .tick_hz = 1000000 },
        { .kind = KM_RESOURCE_TIMER_CHANNEL,  .instance = 2, .subresource = 1, .owner = 1, .period_ticks = 0, .tick_hz = 0 },
        { .kind = KM_RESOURCE_TIMER_CHANNEL,  .instance = 2, .subresource = 1, .owner = 2, .period_ticks = 0, .tick_hz = 0 }
    };
    res = km_resource_plan_validate(dup_chan_diff_owner, 4, &diag);
    assert(res == KM_ERR_CONFLICT);
    assert(diag.reason == KM_RESOURCE_DIAG_DUPLICATE_RESOURCE);
    assert(diag.first_index == 2);
    assert(diag.second_index == 3);

    /* Shared PWM base with different owners on different channels: completely valid */
    km_resource_claim_t shared_timer_valid[4] = {
        { .kind = KM_RESOURCE_TIMER_PWM_BASE, .instance = 2, .subresource = 0, .owner = 1, .period_ticks = 1000, .tick_hz = 1000000 },
        { .kind = KM_RESOURCE_TIMER_CHANNEL,  .instance = 2, .subresource = 1, .owner = 1, .period_ticks = 0, .tick_hz = 0 },
        { .kind = KM_RESOURCE_TIMER_PWM_BASE, .instance = 2, .subresource = 0, .owner = 2, .period_ticks = 1000, .tick_hz = 1000000 },
        { .kind = KM_RESOURCE_TIMER_CHANNEL,  .instance = 2, .subresource = 2, .owner = 2, .period_ticks = 0, .tick_hz = 0 }
    };
    res = km_resource_plan_validate(shared_timer_valid, 4, &diag);
    assert(res == KM_OK);
    assert(diag.reason == KM_RESOURCE_DIAG_NONE);

    /* Claim order permutation: channels before base: valid */
    km_resource_claim_t permuted_timer[4] = {
        shared_timer_valid[1], /* channel 1 (owner 1) */
        shared_timer_valid[3], /* channel 2 (owner 2) */
        shared_timer_valid[0], /* base (owner 1) */
        shared_timer_valid[2]  /* base (owner 2) */
    };
    res = km_resource_plan_validate(permuted_timer, 4, &diag);
    assert(res == KM_OK);
    assert(diag.reason == KM_RESOURCE_DIAG_NONE);

    /* Missing TIMER_PWM_BASE for channel */
    km_resource_claim_t missing_base[2] = {
        { .kind = KM_RESOURCE_PIN,            .instance = 0, .subresource = 0, .owner = 1, .period_ticks = 0, .tick_hz = 0 },
        { .kind = KM_RESOURCE_TIMER_CHANNEL,  .instance = 2, .subresource = 1, .owner = 1, .period_ticks = 0, .tick_hz = 0 }
    };
    res = km_resource_plan_validate(missing_base, 2, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_MISSING_TIMER_BASE);
    assert(diag.first_index == 1);
    assert(diag.second_index == KM_RESOURCE_NO_INDEX);

    /* Mismatched-owner TIMER_PWM_BASE: owner 2 base present, but channel is owner 1 */
    km_resource_claim_t wrong_owner_base[2] = {
        { .kind = KM_RESOURCE_TIMER_PWM_BASE, .instance = 2, .subresource = 0, .owner = 2, .period_ticks = 1000, .tick_hz = 1000000 },
        { .kind = KM_RESOURCE_TIMER_CHANNEL,  .instance = 2, .subresource = 1, .owner = 1, .period_ticks = 0, .tick_hz = 0 }
    };
    res = km_resource_plan_validate(wrong_owner_base, 2, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_MISSING_TIMER_BASE);
    assert(diag.first_index == 1);
    assert(diag.second_index == KM_RESOURCE_NO_INDEX);

    /* Independent timers TIM2 and TIM3 do not collide */
    km_resource_claim_t indep_timers[4] = {
        { .kind = KM_RESOURCE_TIMER_PWM_BASE, .instance = 2, .subresource = 0, .owner = 1, .period_ticks = 1000, .tick_hz = 1000000 },
        { .kind = KM_RESOURCE_TIMER_CHANNEL,  .instance = 2, .subresource = 1, .owner = 1, .period_ticks = 0, .tick_hz = 0 },
        { .kind = KM_RESOURCE_TIMER_PWM_BASE, .instance = 3, .subresource = 0, .owner = 1, .period_ticks = 500,  .tick_hz = 2000000 },
        { .kind = KM_RESOURCE_TIMER_CHANNEL,  .instance = 3, .subresource = 1, .owner = 1, .period_ticks = 0, .tick_hz = 0 }
    };
    res = km_resource_plan_validate(indep_timers, 4, &diag);
    assert(res == KM_OK);
    assert(diag.reason == KM_RESOURCE_DIAG_NONE);

    printf("  -> Passed test_timer_semantics_and_pwm_base_sharing\n");
}

/* =========================================================================
 * Test Section 5: ADC Serialized-Unit Semantics and Channels
 * ========================================================================= */
static void test_adc_semantics_and_serialized_unit(void)
{
    printf("Running test_adc_semantics_and_serialized_unit...\n");

    km_resource_diagnostic_t diag;

    /* Paired ADC on same unit and same owner: valid */
    km_resource_claim_t valid_adc[3] = {
        { .kind = KM_RESOURCE_ADC_UNIT,    .instance = 1, .subresource = 0, .owner = 10, .period_ticks = 0, .tick_hz = 0 },
        { .kind = KM_RESOURCE_ADC_CHANNEL, .instance = 1, .subresource = 4, .owner = 10, .period_ticks = 0, .tick_hz = 0 },
        { .kind = KM_RESOURCE_ADC_CHANNEL, .instance = 1, .subresource = 8, .owner = 10, .period_ticks = 0, .tick_hz = 0 }
    };
    km_result_t res = km_resource_plan_validate(valid_adc, 3, &diag);
    assert(res == KM_OK);
    assert(diag.reason == KM_RESOURCE_DIAG_NONE);

    /* ADC channel 0 is valid */
    km_resource_claim_t adc_ch0[2] = {
        { .kind = KM_RESOURCE_ADC_UNIT,    .instance = 1, .subresource = 0, .owner = 10, .period_ticks = 0, .tick_hz = 0 },
        { .kind = KM_RESOURCE_ADC_CHANNEL, .instance = 1, .subresource = 0, .owner = 10, .period_ticks = 0, .tick_hz = 0 }
    };
    res = km_resource_plan_validate(adc_ch0, 2, &diag);
    assert(res == KM_OK);

    /* Orphan ADC_UNIT reservation with no channels: valid */
    km_resource_claim_t orphan_unit = {
        .kind = KM_RESOURCE_ADC_UNIT,
        .instance = 1,
        .subresource = 0,
        .owner = 10,
        .period_ticks = 0,
        .tick_hz = 0
    };
    res = km_resource_plan_validate(&orphan_unit, 1, &diag);
    assert(res == KM_OK);

    /* Two ADC_UNIT claims on same instance (same owner): conflict */
    km_resource_claim_t dup_unit_same_owner[2] = {
        orphan_unit,
        orphan_unit
    };
    res = km_resource_plan_validate(dup_unit_same_owner, 2, &diag);
    assert(res == KM_ERR_CONFLICT);
    assert(diag.reason == KM_RESOURCE_DIAG_DUPLICATE_RESOURCE);
    assert(diag.first_index == 0);
    assert(diag.second_index == 1);

    /* Two ADC_UNIT claims on same instance (different owners): conflict */
    km_resource_claim_t dup_unit_diff_owner[2] = {
        orphan_unit,
        { .kind = KM_RESOURCE_ADC_UNIT, .instance = 1, .subresource = 0, .owner = 20, .period_ticks = 0, .tick_hz = 0 }
    };
    res = km_resource_plan_validate(dup_unit_diff_owner, 2, &diag);
    assert(res == KM_ERR_CONFLICT);
    assert(diag.reason == KM_RESOURCE_DIAG_DUPLICATE_RESOURCE);
    assert(diag.first_index == 0);
    assert(diag.second_index == 1);

    /* Different owners sharing an ADC unit (ADC_UNIT owner 10, ADC_CHANNEL owner 20): conflict */
    km_resource_claim_t diff_owner_unit_chan[2] = {
        { .kind = KM_RESOURCE_ADC_UNIT,    .instance = 1, .subresource = 0, .owner = 10, .period_ticks = 0, .tick_hz = 0 },
        { .kind = KM_RESOURCE_ADC_CHANNEL, .instance = 1, .subresource = 4, .owner = 20, .period_ticks = 0, .tick_hz = 0 }
    };
    res = km_resource_plan_validate(diff_owner_unit_chan, 2, &diag);
    assert(res == KM_ERR_CONFLICT);
    assert(diag.reason == KM_RESOURCE_DIAG_DUPLICATE_RESOURCE);
    assert(diag.first_index == 0);
    assert(diag.second_index == 1);

    /* Different owners sharing an ADC unit (two channels with different owners): conflict */
    km_resource_claim_t diff_owner_two_chans[2] = {
        { .kind = KM_RESOURCE_ADC_CHANNEL, .instance = 1, .subresource = 4, .owner = 10, .period_ticks = 0, .tick_hz = 0 },
        { .kind = KM_RESOURCE_ADC_CHANNEL, .instance = 1, .subresource = 8, .owner = 20, .period_ticks = 0, .tick_hz = 0 }
    };
    res = km_resource_plan_validate(diff_owner_two_chans, 2, &diag);
    assert(res == KM_ERR_CONFLICT);
    assert(diag.reason == KM_RESOURCE_DIAG_DUPLICATE_RESOURCE);
    assert(diag.first_index == 0);
    assert(diag.second_index == 1);

    /* Duplicate ADC channels on same instance/subresource (same owner): conflict */
    km_resource_claim_t dup_adc_chan[3] = {
        { .kind = KM_RESOURCE_ADC_UNIT,    .instance = 1, .subresource = 0, .owner = 10, .period_ticks = 0, .tick_hz = 0 },
        { .kind = KM_RESOURCE_ADC_CHANNEL, .instance = 1, .subresource = 4, .owner = 10, .period_ticks = 0, .tick_hz = 0 },
        { .kind = KM_RESOURCE_ADC_CHANNEL, .instance = 1, .subresource = 4, .owner = 10, .period_ticks = 0, .tick_hz = 0 }
    };
    res = km_resource_plan_validate(dup_adc_chan, 3, &diag);
    assert(res == KM_ERR_CONFLICT);
    assert(diag.reason == KM_RESOURCE_DIAG_DUPLICATE_RESOURCE);
    assert(diag.first_index == 1);
    assert(diag.second_index == 2);

    /* Missing ADC_UNIT companion for ADC_CHANNEL */
    km_resource_claim_t missing_adc_unit[1] = {
        { .kind = KM_RESOURCE_ADC_CHANNEL, .instance = 1, .subresource = 4, .owner = 10, .period_ticks = 0, .tick_hz = 0 }
    };
    res = km_resource_plan_validate(missing_adc_unit, 1, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_MISSING_ADC_UNIT);
    assert(diag.first_index == 0);
    assert(diag.second_index == KM_RESOURCE_NO_INDEX);

    /* Independent ADC units ADC1 and ADC2 do not collide */
    km_resource_claim_t indep_adc[4] = {
        { .kind = KM_RESOURCE_ADC_UNIT,    .instance = 1, .subresource = 0, .owner = 10, .period_ticks = 0, .tick_hz = 0 },
        { .kind = KM_RESOURCE_ADC_CHANNEL, .instance = 1, .subresource = 4, .owner = 10, .period_ticks = 0, .tick_hz = 0 },
        { .kind = KM_RESOURCE_ADC_UNIT,    .instance = 2, .subresource = 0, .owner = 20, .period_ticks = 0, .tick_hz = 0 },
        { .kind = KM_RESOURCE_ADC_CHANNEL, .instance = 2, .subresource = 4, .owner = 20, .period_ticks = 0, .tick_hz = 0 }
    };
    res = km_resource_plan_validate(indep_adc, 4, &diag);
    assert(res == KM_OK);
    assert(diag.reason == KM_RESOURCE_DIAG_NONE);

    /* GPIO collision when one function is ADC and another is a switch:
     * PIN claim for analog pin PC4 (36) collides with PIN claim for switch PC4 (36) */
    km_resource_claim_t adc_pin_vs_switch_pin[2] = {
        { .kind = KM_RESOURCE_PIN, .instance = 36, .subresource = 0, .owner = 10, .period_ticks = 0, .tick_hz = 0 }, /* Analog PC4 */
        { .kind = KM_RESOURCE_PIN, .instance = 36, .subresource = 0, .owner = 30, .period_ticks = 0, .tick_hz = 0 }  /* Switch PC4 */
    };
    res = km_resource_plan_validate(adc_pin_vs_switch_pin, 2, &diag);
    assert(res == KM_ERR_CONFLICT);
    assert(diag.reason == KM_RESOURCE_DIAG_DUPLICATE_RESOURCE);
    assert(diag.first_index == 0);
    assert(diag.second_index == 1);

    printf("  -> Passed test_adc_semantics_and_serialized_unit\n");
}

/* =========================================================================
 * Test Section 6: Diagnostic Precedence and Deterministic Indexing
 * ========================================================================= */
static void test_diagnostic_precedence_and_determinism(void)
{
    printf("Running test_diagnostic_precedence_and_determinism...\n");

    km_resource_diagnostic_t diag;

    /* 1. Plan-level error beats bad claim in array:
     * Count KM_RESOURCE_MAX_CLAIMS + 1 with malformed claim -> returns
     * BAD_ARGUMENT, NO_INDEX (the capacity check runs first). */
    km_resource_claim_t bad_claim = {
        .kind = (km_resource_kind_t)99,
        .instance = UINT16_MAX,
        .subresource = 5,
        .owner = 0,
        .period_ticks = 1,
        .tick_hz = 1
    };
    km_result_t res = km_resource_plan_validate(&bad_claim, KM_RESOURCE_MAX_CLAIMS + 1u, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_BAD_ARGUMENT);
    assert(diag.first_index == KM_RESOURCE_NO_INDEX);
    assert(diag.second_index == KM_RESOURCE_NO_INDEX);

    /* 2. Bad claim beats pair conflict:
     * claims[0] is malformed (bad owner 0); claims[1] and claims[2] are duplicate pins.
     * Must report BAD_CLAIM at index 0. */
    km_resource_claim_t claim_vs_conflict[3] = {
        { .kind = KM_RESOURCE_PIN, .instance = 1, .subresource = 0, .owner = 0, .period_ticks = 0, .tick_hz = 0 }, /* Bad claim */
        { .kind = KM_RESOURCE_PIN, .instance = 2, .subresource = 0, .owner = 1, .period_ticks = 0, .tick_hz = 0 },
        { .kind = KM_RESOURCE_PIN, .instance = 2, .subresource = 0, .owner = 1, .period_ticks = 0, .tick_hz = 0 }  /* Duplicate */
    };
    res = km_resource_plan_validate(claim_vs_conflict, 3, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_BAD_CLAIM);
    assert(diag.first_index == 0);
    assert(diag.second_index == KM_RESOURCE_NO_INDEX);

    /* 3. Pair conflict beats missing companion:
     * claims[0] and claims[1] are duplicate pins; claims[2] is TIMER_CHANNEL missing base.
     * Must report DUPLICATE_RESOURCE at (0, 1). */
    km_resource_claim_t conflict_vs_companion[3] = {
        { .kind = KM_RESOURCE_PIN,           .instance = 2, .subresource = 0, .owner = 1, .period_ticks = 0, .tick_hz = 0 },
        { .kind = KM_RESOURCE_PIN,           .instance = 2, .subresource = 0, .owner = 2, .period_ticks = 0, .tick_hz = 0 }, /* Dup */
        { .kind = KM_RESOURCE_TIMER_CHANNEL, .instance = 5, .subresource = 1, .owner = 1, .period_ticks = 0, .tick_hz = 0 }  /* Orphan */
    };
    res = km_resource_plan_validate(conflict_vs_companion, 3, &diag);
    assert(res == KM_ERR_CONFLICT);
    assert(diag.reason == KM_RESOURCE_DIAG_DUPLICATE_RESOURCE);
    assert(diag.first_index == 0);
    assert(diag.second_index == 1);

    /* 4. Lexicographical pair order determinism:
     * Plan contains two conflicts:
     * Pair A: (0, 3) (pin 10)
     * Pair B: (1, 2) (pin 20)
     * Lexicographic order visits (0, 1), (0, 2), (0, 3) -> Pair A found first! */
    km_resource_claim_t two_conflicts[4] = {
        { .kind = KM_RESOURCE_PIN, .instance = 10, .subresource = 0, .owner = 1, .period_ticks = 0, .tick_hz = 0 }, /* 0 */
        { .kind = KM_RESOURCE_PIN, .instance = 20, .subresource = 0, .owner = 1, .period_ticks = 0, .tick_hz = 0 }, /* 1 */
        { .kind = KM_RESOURCE_PIN, .instance = 20, .subresource = 0, .owner = 2, .period_ticks = 0, .tick_hz = 0 }, /* 2 */
        { .kind = KM_RESOURCE_PIN, .instance = 10, .subresource = 0, .owner = 2, .period_ticks = 0, .tick_hz = 0 }  /* 3 */
    };
    res = km_resource_plan_validate(two_conflicts, 4, &diag);
    assert(res == KM_ERR_CONFLICT);
    assert(diag.reason == KM_RESOURCE_DIAG_DUPLICATE_RESOURCE);
    assert(diag.first_index == 0);
    assert(diag.second_index == 3);

    /* Permute rows so Pair B (pin 20) is at indices (0, 1) */
    km_resource_claim_t permuted_conflicts[4] = {
        two_conflicts[1], /* pin 20 */
        two_conflicts[2], /* pin 20 */
        two_conflicts[0], /* pin 10 */
        two_conflicts[3]  /* pin 10 */
    };
    res = km_resource_plan_validate(permuted_conflicts, 4, &diag);
    assert(res == KM_ERR_CONFLICT);
    assert(diag.reason == KM_RESOURCE_DIAG_DUPLICATE_RESOURCE);
    assert(diag.first_index == 0);
    assert(diag.second_index == 1);

    /* 5. Ascending channel scan for missing companions:
     * Index 0: TIMER_CHANNEL missing base
     * Index 1: ADC_CHANNEL missing unit
     * Reports MISSING_TIMER_BASE at index 0 */
    km_resource_claim_t two_missing[2] = {
        { .kind = KM_RESOURCE_TIMER_CHANNEL, .instance = 2, .subresource = 1, .owner = 1, .period_ticks = 0, .tick_hz = 0 },
        { .kind = KM_RESOURCE_ADC_CHANNEL,   .instance = 1, .subresource = 4, .owner = 2, .period_ticks = 0, .tick_hz = 0 }
    };
    res = km_resource_plan_validate(two_missing, 2, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_MISSING_TIMER_BASE);
    assert(diag.first_index == 0);
    assert(diag.second_index == KM_RESOURCE_NO_INDEX);

    /* Swap order: ADC_CHANNEL at 0, TIMER_CHANNEL at 1 */
    km_resource_claim_t two_missing_swapped[2] = {
        two_missing[1],
        two_missing[0]
    };
    res = km_resource_plan_validate(two_missing_swapped, 2, &diag);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(diag.reason == KM_RESOURCE_DIAG_MISSING_ADC_UNIT);
    assert(diag.first_index == 0);
    assert(diag.second_index == KM_RESOURCE_NO_INDEX);

    printf("  -> Passed test_diagnostic_precedence_and_determinism\n");
}

/* =========================================================================
 * Test Section 7: Representative Logical AFC Lite / BoxTurtle Plan
 * ========================================================================= */
static uint16_t make_pin_id(uint8_t port, uint8_t pin)
{
    /* Canonical example fixture encoding: port * 16 + pin (A=0, B=1, C=2, D=3, E=4) */
    return (uint16_t)((uint16_t)port * 16u + (uint16_t)pin);
}

static void test_representative_afc_lite_boxturtle_fixture(void)
{
    printf("Running test_representative_afc_lite_boxturtle_fixture...\n");

    km_resource_diagnostic_t diag;

    /*
     * Build baseline 4-lane AFC Lite / BoxTurtle logical plan fixture:
     *
     * Port indices: A=0, B=1, C=2, D=3, E=4.
     *
     * Lane 1 (Feeder owner 1, Rewinder owner 2):
     *   Feeder pins: STEP=PD4, DIR=PD3, ENABLE=PD6, UART=PD5
     *   Rewinder pins: PWM_REW=PA0, PWM_FWD=PA1, EN=PA2
     *   Rewinder timer: TIM2, channels 1 & 2 (PA0/PA1)
     *
     * Lane 2 (Feeder owner 3, Rewinder owner 4):
     *   Feeder pins: STEP=PC12, DIR=PC11, ENABLE=PD1, UART=PD0
     *   Rewinder pins: PWM_REW=PA6, PWM_FWD=PA7, EN=PA5
     *   Rewinder timer: TIM3, channels 1 & 2 (PA6/PA7)
     *
     * Lane 3 (Feeder owner 5, Rewinder owner 6):
     *   Feeder pins: STEP=PE2, DIR=PE3, ENABLE=PE0, UART=PE1
     *   Rewinder pins: PWM_REW=PB14, PWM_FWD=PB15, EN=PB13
     *   Rewinder timer: TIM12, channels 1 & 2 (PB14/PB15)
     *
     * Lane 4 (Feeder owner 7, Rewinder owner 8):
     *   Feeder pins: STEP=PD15, DIR=PD14, ENABLE=PC7, UART=PC6
     *   Rewinder pins: PWM_REW=PD12, PWM_FWD=PD13, EN=PD11
     *   Rewinder timer: TIM4, channels 1 & 2 (PD12/PD13)
     *
     * Analog feedback pair (Owner 10):
     *   Pins: SW1=PC4, SW2=PC5
     *   ADC: ADC1 (instance 1), channels 4 and 8
     */

    km_resource_claim_t plan[64];
    size_t n = 0;

    /* --- Lane 1 --- */
    /* Feeder pins */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(3, 4), .subresource = 0, .owner = 1, .period_ticks = 0, .tick_hz = 0 }; /* PD4 */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(3, 3), .subresource = 0, .owner = 1, .period_ticks = 0, .tick_hz = 0 }; /* PD3 */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(3, 6), .subresource = 0, .owner = 1, .period_ticks = 0, .tick_hz = 0 }; /* PD6 */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(3, 5), .subresource = 0, .owner = 1, .period_ticks = 0, .tick_hz = 0 }; /* PD5 */
    /* Rewinder pins */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(0, 0), .subresource = 0, .owner = 2, .period_ticks = 0, .tick_hz = 0 }; /* PA0 */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(0, 1), .subresource = 0, .owner = 2, .period_ticks = 0, .tick_hz = 0 }; /* PA1 */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(0, 2), .subresource = 0, .owner = 2, .period_ticks = 0, .tick_hz = 0 }; /* PA2 */
    /* Rewinder TIM2 (instance 2) */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_TIMER_PWM_BASE, .instance = 2, .subresource = 0, .owner = 2, .period_ticks = 1000, .tick_hz = 1000000 };
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_TIMER_CHANNEL,  .instance = 2, .subresource = 1, .owner = 2, .period_ticks = 0,    .tick_hz = 0 };
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_TIMER_CHANNEL,  .instance = 2, .subresource = 2, .owner = 2, .period_ticks = 0,    .tick_hz = 0 };

    /* --- Lane 2 --- */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(2, 12), .subresource = 0, .owner = 3, .period_ticks = 0, .tick_hz = 0 }; /* PC12 */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(2, 11), .subresource = 0, .owner = 3, .period_ticks = 0, .tick_hz = 0 }; /* PC11 */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(3, 1),  .subresource = 0, .owner = 3, .period_ticks = 0, .tick_hz = 0 }; /* PD1 */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(3, 0),  .subresource = 0, .owner = 3, .period_ticks = 0, .tick_hz = 0 }; /* PD0 */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(0, 6),  .subresource = 0, .owner = 4, .period_ticks = 0, .tick_hz = 0 }; /* PA6 */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(0, 7),  .subresource = 0, .owner = 4, .period_ticks = 0, .tick_hz = 0 }; /* PA7 */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(0, 5),  .subresource = 0, .owner = 4, .period_ticks = 0, .tick_hz = 0 }; /* PA5 */
    /* Rewinder TIM3 (instance 3) */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_TIMER_PWM_BASE, .instance = 3, .subresource = 0, .owner = 4, .period_ticks = 1000, .tick_hz = 1000000 };
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_TIMER_CHANNEL,  .instance = 3, .subresource = 1, .owner = 4, .period_ticks = 0,    .tick_hz = 0 };
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_TIMER_CHANNEL,  .instance = 3, .subresource = 2, .owner = 4, .period_ticks = 0,    .tick_hz = 0 };

    /* --- Lane 3 --- */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(4, 2),  .subresource = 0, .owner = 5, .period_ticks = 0, .tick_hz = 0 }; /* PE2 */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(4, 3),  .subresource = 0, .owner = 5, .period_ticks = 0, .tick_hz = 0 }; /* PE3 */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(4, 0),  .subresource = 0, .owner = 5, .period_ticks = 0, .tick_hz = 0 }; /* PE0 */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(4, 1),  .subresource = 0, .owner = 5, .period_ticks = 0, .tick_hz = 0 }; /* PE1 */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(1, 14), .subresource = 0, .owner = 6, .period_ticks = 0, .tick_hz = 0 }; /* PB14 */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(1, 15), .subresource = 0, .owner = 6, .period_ticks = 0, .tick_hz = 0 }; /* PB15 */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(1, 13), .subresource = 0, .owner = 6, .period_ticks = 0, .tick_hz = 0 }; /* PB13 */
    /* Rewinder TIM12 (instance 12) */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_TIMER_PWM_BASE, .instance = 12, .subresource = 0, .owner = 6, .period_ticks = 1000, .tick_hz = 1000000 };
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_TIMER_CHANNEL,  .instance = 12, .subresource = 1, .owner = 6, .period_ticks = 0,    .tick_hz = 0 };
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_TIMER_CHANNEL,  .instance = 12, .subresource = 2, .owner = 6, .period_ticks = 0,    .tick_hz = 0 };

    /* --- Lane 4 --- */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(3, 15), .subresource = 0, .owner = 7, .period_ticks = 0, .tick_hz = 0 }; /* PD15 */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(3, 14), .subresource = 0, .owner = 7, .period_ticks = 0, .tick_hz = 0 }; /* PD14 */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(2, 7),  .subresource = 0, .owner = 7, .period_ticks = 0, .tick_hz = 0 }; /* PC7 */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(2, 6),  .subresource = 0, .owner = 7, .period_ticks = 0, .tick_hz = 0 }; /* PC6 */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(3, 12), .subresource = 0, .owner = 8, .period_ticks = 0, .tick_hz = 0 }; /* PD12 */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(3, 13), .subresource = 0, .owner = 8, .period_ticks = 0, .tick_hz = 0 }; /* PD13 */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(3, 11), .subresource = 0, .owner = 8, .period_ticks = 0, .tick_hz = 0 }; /* PD11 */
    /* Rewinder TIM4 (instance 4) */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_TIMER_PWM_BASE, .instance = 4, .subresource = 0, .owner = 8, .period_ticks = 1000, .tick_hz = 1000000 };
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_TIMER_CHANNEL,  .instance = 4, .subresource = 1, .owner = 8, .period_ticks = 0,    .tick_hz = 0 };
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_TIMER_CHANNEL,  .instance = 4, .subresource = 2, .owner = 8, .period_ticks = 0,    .tick_hz = 0 };

    /* --- Paired Analog Feedback Input (Owner 10) --- */
    size_t analog_pc4_idx = n;
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(2, 4), .subresource = 0, .owner = 10, .period_ticks = 0, .tick_hz = 0 }; /* PC4 */
    size_t analog_pc5_idx = n;
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_PIN, .instance = make_pin_id(2, 5), .subresource = 0, .owner = 10, .period_ticks = 0, .tick_hz = 0 }; /* PC5 */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_ADC_UNIT,    .instance = 1, .subresource = 0, .owner = 10, .period_ticks = 0, .tick_hz = 0 };        /* ADC1 */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_ADC_CHANNEL, .instance = 1, .subresource = 4, .owner = 10, .period_ticks = 0, .tick_hz = 0 };        /* ADC1 ch4 */
    plan[n++] = (km_resource_claim_t){ .kind = KM_RESOURCE_ADC_CHANNEL, .instance = 1, .subresource = 8, .owner = 10, .period_ticks = 0, .tick_hz = 0 };        /* ADC1 ch8 */

    size_t baseline_count = n;
    assert(baseline_count == 45);

    /* 1. Baseline plan with optional current/encoder claims absent must validate successfully */
    km_result_t res = km_resource_plan_validate(plan, baseline_count, &diag);
    assert(res == KM_OK);
    assert(diag.reason == KM_RESOURCE_DIAG_NONE);
    assert(diag.first_index == KM_RESOURCE_NO_INDEX);
    assert(diag.second_index == KM_RESOURCE_NO_INDEX);

    /* 2. Adding legacy HUB = PC4 switch claim must fail with DUPLICATE_RESOURCE */
    plan[baseline_count] = (km_resource_claim_t){
        .kind = KM_RESOURCE_PIN,
        .instance = make_pin_id(2, 4), /* PC4 collision with analog feedback pin */
        .subresource = 0,
        .owner = 30, /* Different owner (e.g. switch manager) */
        .period_ticks = 0,
        .tick_hz = 0
    };
    res = km_resource_plan_validate(plan, baseline_count + 1u, &diag);
    assert(res == KM_ERR_CONFLICT);
    assert(diag.reason == KM_RESOURCE_DIAG_DUPLICATE_RESOURCE);
    assert(diag.first_index == (uint16_t)analog_pc4_idx);
    assert(diag.second_index == (uint16_t)baseline_count);

    /* 3. Adding legacy TRG1 = PC5 switch claim must fail with DUPLICATE_RESOURCE */
    plan[baseline_count] = (km_resource_claim_t){
        .kind = KM_RESOURCE_PIN,
        .instance = make_pin_id(2, 5), /* PC5 collision with analog feedback pin */
        .subresource = 0,
        .owner = 30,
        .period_ticks = 0,
        .tick_hz = 0
    };
    res = km_resource_plan_validate(plan, baseline_count + 1u, &diag);
    assert(res == KM_ERR_CONFLICT);
    assert(diag.reason == KM_RESOURCE_DIAG_DUPLICATE_RESOURCE);
    assert(diag.first_index == (uint16_t)analog_pc5_idx);
    assert(diag.second_index == (uint16_t)baseline_count);

    /* 4. Remapping both switches to otherwise free synthetic fixture pins (e.g. PE4, PE5) must validate */
    plan[baseline_count] = (km_resource_claim_t){
        .kind = KM_RESOURCE_PIN,
        .instance = make_pin_id(4, 4), /* PE4: free fixture pin */
        .subresource = 0,
        .owner = 30,
        .period_ticks = 0,
        .tick_hz = 0
    };
    plan[baseline_count + 1u] = (km_resource_claim_t){
        .kind = KM_RESOURCE_PIN,
        .instance = make_pin_id(4, 5), /* PE5: free fixture pin */
        .subresource = 0,
        .owner = 30,
        .period_ticks = 0,
        .tick_hz = 0
    };
    res = km_resource_plan_validate(plan, baseline_count + 2u, &diag);
    assert(res == KM_OK);
    assert(diag.reason == KM_RESOURCE_DIAG_NONE);
    assert(diag.first_index == KM_RESOURCE_NO_INDEX);
    assert(diag.second_index == KM_RESOURCE_NO_INDEX);

    printf("  -> Passed test_representative_afc_lite_boxturtle_fixture\n");
}

/* =========================================================================
 * Main Entry Point
 * ========================================================================= */
/* =========================================================================
 * Test Section 7: A fully loaded 12-lane selector image's pin claim count
 * ========================================================================= */
/* mcu/h723_resources.c's own capacity assert requires
 * KM_PIN_CONFIG_MAX_CLAIMS + 2*KM_MAX_LANES + 3 <= KM_RESOURCE_MAX_CLAIMS.
 * At KM_MAX_LANES=12, mcu/pin_config.h's KM_PIN_CONFIG_MAX_CLAIMS (10 per
 * lane, plus the shared and selector roles) is 132: this is that many
 * claims (one PIN per lane role, times 11 roles, for 12 lanes), proving
 * KM_RESOURCE_MAX_CLAIMS holds them (with the H723's own extra headroom
 * still to spare, since it needs 132 + 24 + 3 = 159 in total). */
static void test_h723_twelve_lane_selector_claim_count(void)
{
    printf("Running test_h723_twelve_lane_selector_claim_count...\n");
    enum { LANES = 12, ROLES = 11, CLAIM_COUNT = LANES * ROLES };
    _Static_assert(CLAIM_COUNT == 132, "12 lanes x 11 roles is the 132-claim scenario");
    _Static_assert(CLAIM_COUNT <= KM_RESOURCE_MAX_CLAIMS, "132 claims must fit");

    km_resource_claim_t claims[CLAIM_COUNT];
    uint16_t i = 0;
    for (uint16_t role = 0; role < ROLES; ++role) {
        for (uint16_t lane = 0; lane < LANES; ++lane) {
            claims[i++] = (km_resource_claim_t){
                .kind = KM_RESOURCE_PIN,
                .instance = (uint16_t)(role * LANES + lane), /* Every lane/role pair its own pin. */
                .subresource = 0,
                .owner = (uint16_t)(role + 1u),
                .period_ticks = 0,
                .tick_hz = 0
            };
        }
    }
    km_resource_diagnostic_t diag;
    memset(&diag, 0xAA, sizeof(diag));
    km_result_t res = km_resource_plan_validate(claims, CLAIM_COUNT, &diag);
    assert(res == KM_OK);
    assert(diag.reason == KM_RESOURCE_DIAG_NONE);
    printf("  -> Passed test_h723_twelve_lane_selector_claim_count\n");
}

int main(void)
{
    printf("=================================================\n");
    printf("Running Klipper-MMU Resource Plan Validator Tests\n");
    printf("=================================================\n");

    test_plan_arguments_and_capacity();
    test_record_validation_field_boundaries();
    test_pin_semantics_and_namespaces();
    test_timer_semantics_and_pwm_base_sharing();
    test_adc_semantics_and_serialized_unit();
    test_diagnostic_precedence_and_determinism();
    test_representative_afc_lite_boxturtle_fixture();
    test_h723_twelve_lane_selector_claim_count();

    printf("=================================================\n");
    printf("All resource plan tests passed successfully!\n");
    printf("=================================================\n");
    return 0;
}
