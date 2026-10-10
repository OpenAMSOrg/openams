// SPDX-License-Identifier: GPL-3.0-or-later
#include "klipper_mmu/mcu_clock.h"
#include "klipper_mmu/operation_core.h"
#include "klipper_mmu/trajectory.h"
#include "mock_driver.h"
#include "fake_clock.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>

/* =========================================================================
 * 1. Independent Reference Oracle
 *
 * Derives conceptual elapsed time directly from cumulative elapsed raw ticks
 * using wide integer division:
 *   elapsed_us = floor(total_raw_ticks * 1000000 / clock_hz)
 *   total_us = origin_us + elapsed_us
 *   expected_ms = floor(total_us / 1000) mod 2^32
 *   expected_us = total_us mod 2^32
 * Completely independent of internal implementation fields.
 * ========================================================================= */
typedef struct {
    uint32_t clock_hz;
    uint32_t initial_ticks;
    km_core_timepoint_t origin;
    uint64_t total_raw_ticks;
} ref_oracle_t;

static void ref_oracle_init(ref_oracle_t *ref, uint32_t clock_hz, uint32_t initial_ticks, km_core_timepoint_t origin)
{
    ref->clock_hz = clock_hz;
    ref->initial_ticks = initial_ticks;
    ref->origin = origin;
    ref->total_raw_ticks = 0;
}

static void ref_oracle_advance(ref_oracle_t *ref, uint32_t delta_ticks)
{
    ref->total_raw_ticks += (uint64_t)delta_ticks;
}

/* Pure ISO C11 wide division: (total_ticks * 1000000) / clock_hz */
static uint64_t ref_compute_elapsed_us(uint64_t total_ticks, uint32_t clock_hz)
{
    if (total_ticks <= (UINT64_MAX / 1000000ULL)) {
        return (total_ticks * 1000000ULL) / (uint64_t)clock_hz;
    }

    uint64_t t_hi = total_ticks >> 32;
    uint64_t t_lo = total_ticks & 0xFFFFFFFFULL;

    uint64_t p_lo = t_lo * 1000000ULL;
    uint64_t p_hi = t_hi * 1000000ULL + (p_lo >> 32);
    p_lo &= 0xFFFFFFFFULL;

    uint64_t q_hi = p_hi / (uint64_t)clock_hz;
    uint64_t rem = p_hi % (uint64_t)clock_hz;

    uint64_t dividend_lo = (rem << 32) | p_lo;
    uint64_t q_lo = dividend_lo / (uint64_t)clock_hz;

    return (q_hi << 32) | q_lo;
}

static km_core_timepoint_t ref_oracle_get_expected(const ref_oracle_t *ref)
{
    uint32_t phase0 = ref->origin.us - ref->origin.ms * 1000u;
    uint64_t elapsed_us = ref_compute_elapsed_us(ref->total_raw_ticks, ref->clock_hz);
    uint64_t total_us = (uint64_t)ref->origin.ms * 1000ULL + (uint64_t)phase0 + elapsed_us;
    km_core_timepoint_t pt;
    pt.us = (uint32_t)(total_us & 0xFFFFFFFFULL);
    pt.ms = (uint32_t)((total_us / 1000ULL) & 0xFFFFFFFFULL);
    return pt;
}

/* =========================================================================
 * 2. Null, Invalid Initialization, Inert States, and Pure Getters
 * ========================================================================= */
static void test_init_validation_and_inert_states(void)
{
    km_core_timepoint_t origin = { .ms = 100u, .us = 100500u };
    km_mcu_clock_t clk;

    /* 1. Null clock pointer */
    assert(km_mcu_clock_init(NULL, 1000000u, 0u, origin) == KM_ERR_INVALID_PARAM);

    /* 2. Clock frequency below 1 MHz: verifies inert uninitialized state */
    km_core_timepoint_t canary_time = { .ms = 0x12345678u, .us = 0x87654321u };
    uint32_t canary_t = 0xAA55AA55u;
    bool canary_d = true;

    memset(&clk, 0x5A, sizeof(clk));
    assert(km_mcu_clock_init(&clk, 0u, 0u, origin) == KM_ERR_INVALID_PARAM);
    assert(km_mcu_clock_get_time(&clk, &canary_time) == KM_ERR_UNAVAILABLE);
    assert(canary_time.ms == 0x12345678u && canary_time.us == 0x87654321u);
    assert(km_mcu_clock_observe(&clk, 100u, &canary_time) == KM_ERR_UNAVAILABLE);
    assert(canary_time.ms == 0x12345678u && canary_time.us == 0x87654321u);
    assert(km_mcu_clock_deadline_ticks(&clk, 100u, &canary_t, &canary_d) == KM_ERR_UNAVAILABLE);
    assert(canary_t == 0xAA55AA55u && canary_d == true);

    memset(&clk, 0x5A, sizeof(clk));
    assert(km_mcu_clock_init(&clk, 999999u, 0u, origin) == KM_ERR_INVALID_PARAM);
    assert(km_mcu_clock_get_time(&clk, &canary_time) == KM_ERR_UNAVAILABLE);

    /* 3. Incoherent origin: unsigned (origin.us - origin.ms * 1000) >= 1000 */
    km_core_timepoint_t bad_origin1 = { .ms = 10u, .us = 10000u + 1000u };
    memset(&clk, 0x5A, sizeof(clk));
    assert(km_mcu_clock_init(&clk, 1000000u, 0u, bad_origin1) == KM_ERR_INVALID_PARAM);
    assert(km_mcu_clock_get_time(&clk, &canary_time) == KM_ERR_UNAVAILABLE);

    km_core_timepoint_t bad_origin2 = { .ms = 10u, .us = 9999u };
    memset(&clk, 0x5A, sizeof(clk));
    assert(km_mcu_clock_init(&clk, 1000000u, 0u, bad_origin2) == KM_ERR_INVALID_PARAM);
    assert(km_mcu_clock_get_time(&clk, &canary_time) == KM_ERR_UNAVAILABLE);

    /* 4. Never-initialized zero-state clock */
    km_mcu_clock_t zero_clk;
    memset(&zero_clk, 0, sizeof(zero_clk));
    km_core_timepoint_t out_time = { .ms = 0xDEADBEEFu, .us = 0xCAFEBABEu };
    uint32_t out_ticks = 0xBAADF00Du;
    bool out_due = true;

    assert(km_mcu_clock_get_time(&zero_clk, &out_time) == KM_ERR_UNAVAILABLE);
    assert(out_time.ms == 0xDEADBEEFu && out_time.us == 0xCAFEBABEu);

    assert(km_mcu_clock_observe(&zero_clk, 100u, &out_time) == KM_ERR_UNAVAILABLE);
    assert(out_time.ms == 0xDEADBEEFu && out_time.us == 0xCAFEBABEu);

    assert(km_mcu_clock_deadline_ticks(&zero_clk, 100u, &out_ticks, &out_due) == KM_ERR_UNAVAILABLE);
    assert(out_ticks == 0xBAADF00Du && out_due == true);

    /* 5. Pure getters and valid initial snapshot */
    assert(km_mcu_clock_init(&clk, 1000000u, 12345u, origin) == KM_OK);
    assert(km_mcu_clock_get_time(&clk, &out_time) == KM_OK);
    assert(out_time.ms == origin.ms && out_time.us == origin.us);

    km_core_timepoint_t snap2;
    assert(km_mcu_clock_get_time(&clk, &snap2) == KM_OK);
    assert(snap2.ms == origin.ms && snap2.us == origin.us);

    /* 6. Ordinary invalid arguments do not advance or fault a healthy clock */
    assert(km_mcu_clock_observe(NULL, 12350u, &out_time) == KM_ERR_INVALID_PARAM);
    assert(km_mcu_clock_observe(&clk, 12350u, NULL) == KM_ERR_INVALID_PARAM);
    assert(km_mcu_clock_get_time(NULL, &out_time) == KM_ERR_INVALID_PARAM);
    assert(km_mcu_clock_get_time(&clk, NULL) == KM_ERR_INVALID_PARAM);
    assert(km_mcu_clock_deadline_ticks(NULL, 100u, &out_ticks, &out_due) == KM_ERR_INVALID_PARAM);
    assert(km_mcu_clock_deadline_ticks(&clk, 100u, NULL, &out_due) == KM_ERR_INVALID_PARAM);
    assert(km_mcu_clock_deadline_ticks(&clk, 100u, &out_ticks, NULL) == KM_ERR_INVALID_PARAM);

    /* Clock remains healthy and at origin */
    assert(km_mcu_clock_get_time(&clk, &out_time) == KM_OK);
    assert(out_time.ms == origin.ms && out_time.us == origin.us);
}

/* =========================================================================
 * 3. Rate Coverage, Chunks, Sub-Microsecond Samples, and Reference Oracles
 * ========================================================================= */
static void test_rates_and_reference_oracles(void)
{
    const uint32_t rates[] = {
        1000000u,    /* 1 MHz */
        2000000u,    /* 2 MHz */
        20000000u,   /* 20 MHz */
        168000000u,  /* 168 MHz (STM32F4) */
        520000000u,  /* 520 MHz */
        3333333u,    /* non-integral MHz */
        UINT32_MAX   /* 4.295 GHz max frequency */
    };

    const uint32_t initial_tick_seeds[] = {
        0u,
        0x12345678u,
        UINT32_MAX - 500u
    };

    const uint32_t chunk_sequence[] = {
        0u,            /* Repeated identical sample */
        1u,            /* Sub-microsecond at high rates */
        1u,
        5u,
        17u,
        100u,
        1000u,
        50000u,
        1000000u,
        2147483600u,   /* Near half-range legal step */
        50u,
        600u           /* Crosses raw timer wrap */
    };

    for (size_t r = 0; r < sizeof(rates) / sizeof(rates[0]); ++r) {
        uint32_t hz = rates[r];
        for (size_t s = 0; s < sizeof(initial_tick_seeds) / sizeof(initial_tick_seeds[0]); ++s) {
            uint32_t init_tick = initial_tick_seeds[s];
            km_core_timepoint_t origin = { .ms = 50u, .us = 50250u };

            km_mcu_clock_t clk;
            ref_oracle_t oracle;
            assert(km_mcu_clock_init(&clk, hz, init_tick, origin) == KM_OK);
            ref_oracle_init(&oracle, hz, init_tick, origin);

            uint32_t current_tick = init_tick;

            for (size_t c = 0; c < sizeof(chunk_sequence) / sizeof(chunk_sequence[0]); ++c) {
                uint32_t delta = chunk_sequence[c];
                current_tick += delta;
                ref_oracle_advance(&oracle, delta);

                km_core_timepoint_t out;
                assert(km_mcu_clock_observe(&clk, current_tick, &out) == KM_OK);

                /* Verify against independent oracle */
                km_core_timepoint_t expected = ref_oracle_get_expected(&oracle);
                assert(out.ms == expected.ms);
                assert(out.us == expected.us);

                /* Verify pure get_time snapshot */
                km_core_timepoint_t snap;
                assert(km_mcu_clock_get_time(&clk, &snap) == KM_OK);
                assert(snap.ms == expected.ms && snap.us == expected.us);

                /* Verify coherence contract */
                uint32_t phase = out.us - out.ms * 1000u;
                assert(phase < 1000u);
            }
        }
    }
}

/* =========================================================================
 * 4. Chunking Invariance Test
 * ========================================================================= */
static void test_chunking_invariance(void)
{
    const uint32_t test_frequencies[] = { 1000000u, 3333333u, 20000000u, 520000000u, UINT32_MAX };
    const uint32_t chunks[] = {
        0u, 1u, 0u, 7u, 19u, 103u, 0u, 54321u, 999999u, 1u, 12345678u, 100000000u
    };

    uint32_t total_ticks = 0;
    for (size_t i = 0; i < sizeof(chunks) / sizeof(chunks[0]); ++i) {
        total_ticks += chunks[i];
    }

    km_core_timepoint_t origin = { .ms = 123u, .us = 123456u };

    for (size_t f = 0; f < sizeof(test_frequencies) / sizeof(test_frequencies[0]); ++f) {
        uint32_t hz = test_frequencies[f];

        km_mcu_clock_t clk_split, clk_single;
        assert(km_mcu_clock_init(&clk_split, hz, 1000u, origin) == KM_OK);
        assert(km_mcu_clock_init(&clk_single, hz, 1000u, origin) == KM_OK);

        uint32_t tick_acc = 1000u;
        for (size_t i = 0; i < sizeof(chunks) / sizeof(chunks[0]); ++i) {
            tick_acc += chunks[i];
            km_core_timepoint_t pt;
            assert(km_mcu_clock_observe(&clk_split, tick_acc, &pt) == KM_OK);
        }

        km_core_timepoint_t pt_single;
        assert(km_mcu_clock_observe(&clk_single, 1000u + total_ticks, &pt_single) == KM_OK);

        km_core_timepoint_t snap_split, snap_single;
        assert(km_mcu_clock_get_time(&clk_split, &snap_split) == KM_OK);
        assert(km_mcu_clock_get_time(&clk_single, &snap_single) == KM_OK);

        assert(snap_split.ms == snap_single.ms);
        assert(snap_split.us == snap_single.us);
        assert((snap_split.us - snap_split.ms * 1000u) < 1000u);
    }
}

/* =========================================================================
 * 5. Complete Microsecond and Millisecond Wrap Tests
 * ========================================================================= */
static void test_us_and_ms_wraps(void)
{
    /* Microsecond counter wrap (~71.58 minutes):
     * Seed origin close to 2^32 - 1 us. */
    {
        uint32_t hz = 1000000u;
        km_core_timepoint_t origin = { .ms = 4294966u, .us = 4294966999u };
        km_mcu_clock_t clk;
        ref_oracle_t oracle;

        assert(km_mcu_clock_init(&clk, hz, 0u, origin) == KM_OK);
        ref_oracle_init(&oracle, hz, 0u, origin);

        /* Advance across the 2^32 boundary in bounded steps */
        uint32_t ticks = 0;
        for (unsigned i = 0; i < 20; ++i) {
            ticks += 100u;
            ref_oracle_advance(&oracle, 100u);

            km_core_timepoint_t out;
            assert(km_mcu_clock_observe(&clk, ticks, &out) == KM_OK);

            km_core_timepoint_t exp = ref_oracle_get_expected(&oracle);
            assert(out.ms == exp.ms && out.us == exp.us);
            assert((out.us - out.ms * 1000u) < 1000u);
        }
    }

    /* Millisecond counter wrap (~49.7 days):
     * Seed origin.ms close to UINT32_MAX with coherent us. */
    {
        uint32_t hz = 2000000u; /* 2 MHz: 2 ticks per us */
        uint32_t origin_ms = UINT32_MAX - 3u;
        uint32_t origin_us = origin_ms * 1000u + 400u;
        km_core_timepoint_t origin = { .ms = origin_ms, .us = origin_us };
        assert((origin.us - origin.ms * 1000u) == 400u);

        km_mcu_clock_t clk;
        ref_oracle_t oracle;
        assert(km_mcu_clock_init(&clk, hz, 5000u, origin) == KM_OK);
        ref_oracle_init(&oracle, hz, 5000u, origin);

        uint32_t ticks = 5000u;
        /* Advance 10 milliseconds (20000 ticks) across the UINT32_MAX millisecond wrap */
        for (unsigned i = 0; i < 10; ++i) {
            ticks += 2000u;
            ref_oracle_advance(&oracle, 2000u);

            km_core_timepoint_t out;
            assert(km_mcu_clock_observe(&clk, ticks, &out) == KM_OK);

            km_core_timepoint_t exp = ref_oracle_get_expected(&oracle);
            assert(out.ms == exp.ms && out.us == exp.us);
            assert((out.us - out.ms * 1000u) < 1000u);
        }
    }
}

/* =========================================================================
 * 6. Clock Fault Latching, Persistence, and Output Preservation
 * ========================================================================= */
static void test_fault_latching_and_persistence(void)
{
    km_core_timepoint_t origin = { .ms = 10u, .us = 10200u };
    km_mcu_clock_t clk;
    assert(km_mcu_clock_init(&clk, 1000000u, 1000u, origin) == KM_OK);

    km_core_timepoint_t canary = { .ms = 0xAA55AA55u, .us = 0x55AA55AAu };

    /* Backward delta: ticks = 999 (delta = 0xFFFFFFFF >= 2^31) */
    assert(km_mcu_clock_observe(&clk, 999u, &canary) == KM_ERR_FAULTED);
    assert(canary.ms == 0xAA55AA55u && canary.us == 0x55AA55AAu);

    /* Fault stays latched on subsequent forward sample */
    assert(km_mcu_clock_observe(&clk, 1010u, &canary) == KM_ERR_FAULTED);
    assert(canary.ms == 0xAA55AA55u && canary.us == 0x55AA55AAu);

    /* get_time returns KM_ERR_FAULTED and leaves output untouched */
    assert(km_mcu_clock_get_time(&clk, &canary) == KM_ERR_FAULTED);
    assert(canary.ms == 0xAA55AA55u && canary.us == 0x55AA55AAu);

    /* deadline_ticks returns KM_ERR_FAULTED and leaves outputs untouched */
    uint32_t out_ticks = 0xCAFEBEEFu;
    bool out_due = true;
    assert(km_mcu_clock_deadline_ticks(&clk, 20000u, &out_ticks, &out_due) == KM_ERR_FAULTED);
    assert(out_ticks == 0xCAFEBEEFu && out_due == true);

    /* Half-range ambiguous delta test (ticks - last_ticks == 0x80000000u) */
    km_mcu_clock_t clk2;
    assert(km_mcu_clock_init(&clk2, 1000000u, 500u, origin) == KM_OK);
    assert(km_mcu_clock_observe(&clk2, 500u + 0x80000000u, &canary) == KM_ERR_FAULTED);
    assert(canary.ms == 0xAA55AA55u && canary.us == 0x55AA55AAu);

    /* Quiescent re-initialization resets fault safely */
    km_core_timepoint_t new_origin = { .ms = 200u, .us = 200100u };
    assert(km_mcu_clock_init(&clk, 2000000u, 0u, new_origin) == KM_OK);

    km_core_timepoint_t recovered;
    assert(km_mcu_clock_get_time(&clk, &recovered) == KM_OK);
    assert(recovered.ms == new_origin.ms && recovered.us == new_origin.us);

    assert(km_mcu_clock_observe(&clk, 200u, &recovered) == KM_OK);
    assert(recovered.us == new_origin.us + 100u);
}

/* =========================================================================
 * 7. Deadline Conversion Contract Tests
 * ========================================================================= */
static void test_deadline_conversion(void)
{
    km_core_timepoint_t origin = { .ms = 100u, .us = 100500u };
    km_mcu_clock_t clk;
    assert(km_mcu_clock_init(&clk, 10000000u, 50000u, origin) == KM_OK); /* 10 MHz: 10 ticks per us */

    uint32_t target_ticks = 0;
    bool due = false;

    /* 1. Deadline equal to current us */
    assert(km_mcu_clock_deadline_ticks(&clk, origin.us, &target_ticks, &due) == KM_OK);
    assert(due == true);
    assert(target_ticks == 50000u);

    /* 2. Past deadline (1 us ago) */
    assert(km_mcu_clock_deadline_ticks(&clk, origin.us - 1u, &target_ticks, &due) == KM_OK);
    assert(due == true);
    assert(target_ticks == 50000u);

    /* 3. Past deadline with modular wrap */
    assert(km_mcu_clock_deadline_ticks(&clk, origin.us + 0xFFFFFFFFu, &target_ticks, &due) == KM_OK);
    assert(due == true);
    assert(target_ticks == 50000u);

    /* 4. Exact half-range difference (ambiguous) */
    uint32_t canary_ticks = 0x11223344u;
    bool canary_due = true;
    assert(km_mcu_clock_deadline_ticks(&clk, origin.us + 0x80000000u, &canary_ticks, &canary_due) == KM_ERR_INVALID_PARAM);
    assert(canary_ticks == 0x11223344u && canary_due == true);

    /* 5. Future deadline with us wrap */
    km_mcu_clock_t clk_wrap;
    km_core_timepoint_t origin_wrap = { .ms = 4294967u, .us = UINT32_MAX - 10u };
    assert(km_mcu_clock_init(&clk_wrap, 1000000u, 100u, origin_wrap) == KM_OK);
    assert(km_mcu_clock_deadline_ticks(&clk_wrap, 15u, &target_ticks, &due) == KM_OK);
    assert(due == false);
    assert(target_ticks == 100u + 26u);

    /* 6. Very large future-us inputs that overflow naive 32-bit multiplication:
     * diff_us = 100,000,000 (~100s) at 20 MHz (20,000,000 Hz).
     * diff_us * hz = 2 * 10^15 (exceeds 32-bit uint32_t 4.29e9).
     * required_ticks = 2,000,000,000 < 2^31. */
    km_mcu_clock_t clk_20m;
    assert(km_mcu_clock_init(&clk_20m, 20000000u, 0u, origin) == KM_OK);
    assert(km_mcu_clock_deadline_ticks(&clk_20m, origin.us + 100000000u, &target_ticks, &due) == KM_OK);
    assert(due == false);
    assert(target_ticks == 2000000000u);

    /* 7. Maximum convertible raw tick interval and first rejected endpoint */
    /* At 2 MHz, max convertible diff_us yields required_ticks < 2^31. */
    km_mcu_clock_t clk_2m;
    assert(km_mcu_clock_init(&clk_2m, 2000000u, 0u, origin) == KM_OK);
    uint32_t max_valid_us = 1073741823u; /* diff_us * 2 = 2147483646 < 2^31 */
    assert(km_mcu_clock_deadline_ticks(&clk_2m, origin.us + max_valid_us, &target_ticks, &due) == KM_OK);
    assert(due == false);
    assert(target_ticks == 2147483646u);

    /* First rejected endpoint: required_ticks >= 2^31 */
    uint32_t rejected_us = 1073741824u; /* diff_us * 2 = 2147483648 == 2^31 */
    canary_ticks = 0xAA55AA55u;
    canary_due = true;
    assert(km_mcu_clock_deadline_ticks(&clk_2m, origin.us + rejected_us, &canary_ticks, &canary_due) == KM_ERR_INVALID_PARAM);
    assert(canary_ticks == 0xAA55AA55u && canary_due == true);
    /* Verify clock is NOT faulted by conversion range limit */
    km_core_timepoint_t chk;
    assert(km_mcu_clock_get_time(&clk_2m, &chk) == KM_OK);

    /* 8. Independent demonstration that target_ticks reaches deadline and target_ticks - 1 does not */
    const uint32_t test_rates[] = { 1000000u, 2000000u, 20000000u, 168000000u, 520000000u, 3333333u, UINT32_MAX };
    for (size_t r = 0; r < sizeof(test_rates) / sizeof(test_rates[0]); ++r) {
        uint32_t hz = test_rates[r];
        km_mcu_clock_t c;
        assert(km_mcu_clock_init(&c, hz, 10000u, origin) == KM_OK);

        /* Advance clock by an arbitrary number of ticks to seed a non-zero fractional residue */
        km_core_timepoint_t cur;
        assert(km_mcu_clock_observe(&c, 10000u + 17u, &cur) == KM_OK);

        const uint32_t test_deadlines_us[] = {
            cur.us + 1u,
            cur.us + 5u,
            cur.us + 100u,
            cur.us + 5000u
        };

        for (size_t d = 0; d < sizeof(test_deadlines_us) / sizeof(test_deadlines_us[0]); ++d) {
            uint32_t d_us = test_deadlines_us[d];
            uint32_t t_ticks = 0;
            bool is_due = true;

            /* Even UINT32_MAX Hz needs fewer than 2^31 ticks for 5000 us. */
            assert(km_mcu_clock_deadline_ticks(&c, d_us, &t_ticks, &is_due) == KM_OK);
            assert(is_due == false);

            /* Clone 1: advance to target_ticks. Must reach deadline. */
            km_mcu_clock_t clone1 = c;
            km_core_timepoint_t out1;
            assert(km_mcu_clock_observe(&clone1, t_ticks, &out1) == KM_OK);
            assert(!km_time_before(out1.us, d_us)); /* out1.us >= d_us */

            /* Clone 2: advance to target_ticks - 1. Must NOT reach deadline. */
            km_mcu_clock_t clone2 = c;
            km_core_timepoint_t out2;
            assert(km_mcu_clock_observe(&clone2, t_ticks - 1u, &out2) == KM_OK);
            assert(km_time_before(out2.us, d_us)); /* out2.us < d_us */
        }
    }
}

/* Section 7: production profile + operation core + MCU clock + trajectory on
 * the tick path. Pre-tick, one step edge was serviced per clock-converted
 * deadline (km_trajectory_get_next_deadline + km_mcu_clock_deadline_ticks);
 * there is no per-edge deadline now, so motion runs as whole-step windows via
 * fake_clock.h's km_fake_clock_run_trajectory, exactly as the MCU adapter
 * drives it. The MCU clock still supplies the core's {ms, us}: each tick
 * observes it and asserts the bridge reproduces motion time, keeping core
 * state and executed steps on one timeline across the raw timer wrap.
 * Dropped for no tick-path meaning: per-edge deadline polling and its
 * km_mcu_clock_deadline_ticks/is_due conversion (section 6 and the tick suites
 * cover that contract), and the mock driver's edge counters (the reference
 * executor records steps, not edge callbacks). */
#define COMPOSITION_CLOCK_HZ 20000000u /* 20 ticks per microsecond */
#define COMPOSITION_TICK_US 1000u

typedef struct {
    km_operation_core_t core;
    mock_driver_t feeder;
    mock_driver_t rewinder;
    km_mcu_clock_t clock;
    km_fake_clock_t motion;
    uint32_t clock_hz;
    uint32_t initial_ticks;
    uint32_t origin_us;
    uint32_t last_observed_ticks;
} composition_fixture_t;

static void composition_samples(composition_fixture_t *f, km_core_timepoint_t pt)
{
    km_core_set_sensors_at(&f->core, 0, true, false, false, true, pt.ms);
    km_core_set_encoder_at(&f->core, 0, true, pt.ms);
    assert(km_buffer_update_switches(&f->core.buffer_sensor, false, false, pt.ms) == KM_OK);
}

/* Run one service tick: motion to now + COMPOSITION_TICK_US, then sample the
 * raw timer at that instant. The raw counter starts 25 ms before 2^32, so LOAD
 * crosses the hardware wrap mid-ramp as the old per-edge service did. */
static km_core_timepoint_t composition_tick(composition_fixture_t *f, bool *crossed_wrap)
{
    assert(km_fake_clock_run_trajectory(&f->motion, &f->core.trajectory,
                                        f->motion.now_us + COMPOSITION_TICK_US) == KM_OK);
    uint32_t elapsed_us = f->motion.now_us - f->origin_us;
    uint32_t ticks = f->initial_ticks
        + (uint32_t)(((uint64_t)elapsed_us * f->clock_hz) / 1000000u);
    if (ticks < f->last_observed_ticks) *crossed_wrap = true;
    f->last_observed_ticks = ticks;

    km_core_timepoint_t now;
    assert(km_mcu_clock_observe(&f->clock, ticks, &now) == KM_OK);
    /* clock_hz is a whole multiple of 1e6, so no fractional microsecond
     * residue: the bridge must reproduce the motion clock exactly. */
    assert(now.us == f->motion.now_us);
    composition_samples(f, now);
    km_core_host_comm_ready(&f->core, now.ms);
    km_core_update(&f->core, now.ms, now.us);
    assert(f->core.op_state != KM_OP_STATE_FAULT);
    return now;
}

static void test_production_core_composition(void)
{
    composition_fixture_t f;
    memset(&f, 0, sizeof(f));

    km_device_profile_t profile;
    km_profile_init_boxturtle(&profile);
    profile.has_powered_rewind = true;
    profile.has_encoder = false;
    assert(km_core_init(&f.core, &profile) == KM_OK);

    mock_driver_init(&f.feeder, 2, 10, false);
    mock_driver_init(&f.rewinder, 2, 10, false);
    assert(km_core_bind_lane_driver(&f.core, 0, &f.feeder.base, &f.rewinder.base) == KM_OK);

    /* Initialize MCU clock at 20 MHz, starting near raw hardware wrap */
    f.clock_hz = COMPOSITION_CLOCK_HZ;
    f.initial_ticks = UINT32_MAX - 500000u;
    f.origin_us = 100500u;
    km_core_timepoint_t origin = { .ms = 100u, .us = f.origin_us };
    assert(km_mcu_clock_init(&f.clock, f.clock_hz, f.initial_ticks, origin) == KM_OK);
    km_fake_clock_init(&f.motion, f.origin_us);
    f.last_observed_ticks = f.initial_ticks;

    km_core_timepoint_t now;
    assert(km_mcu_clock_get_time(&f.clock, &now) == KM_OK);
    assert(now.ms == origin.ms && now.us == origin.us);

    km_core_host_comm_ready(&f.core, now.ms);
    composition_samples(&f, now);
    km_core_update(&f.core, now.ms, now.us);
    assert(f.core.op_state != KM_OP_STATE_FAULT);

    /* Request load */
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 88u, 2001u, 0u, now) == KM_REQ_ACCEPTED);
    assert(f.core.trajectory.last_update_us == now.us);
    assert(km_trajectory_is_moving(&f.core.trajectory));

    /* Service the tick path until the raw timer wraps, then briefly further as
     * the old loop did. hub/toolhead stay clear, so the open-loop LOAD ramp is
     * still moving when we break out. */
    bool crossed_wrap = false;
    uint32_t service_ticks = 0;
    const uint32_t max_ticks = 100000;

    while (km_trajectory_is_moving(&f.core.trajectory) && service_ticks < max_ticks) {
        now = composition_tick(&f, &crossed_wrap);
        service_ticks++;
        if (crossed_wrap && service_ticks > 100) break; /* crossed during ramp */
    }
    assert(crossed_wrap == true);
    /* The reference records executed steps, not the mock driver's edge
     * callbacks: the old rising_edge_count > 0 is its position instead. */
    assert(km_ref_position(&f.motion.ref) != 0);

    /* Request cancellation */
    assert(km_mcu_clock_get_time(&f.clock, &now) == KM_OK);
    assert(now.us == f.motion.now_us);
    assert(km_core_request_cancel5(&f.core, KM_CLIENT_HOST, 88u, 2001u, now) == KM_REQ_ACCEPTED);
    assert(f.core.shared_path_owned && f.core.stage == KM_STAGE_BRAKING);

    /* Decelerate on the tick path until the core releases the shared path */
    while (f.core.shared_path_owned && service_ticks < max_ticks * 2u) {
        now = composition_tick(&f, &crossed_wrap);
        service_ticks++;
    }
    assert(!f.core.shared_path_owned && f.core.stage == KM_STAGE_STOPPED);
    assert(!km_trajectory_is_moving(&f.core.trajectory));

    /* Check cancellation event was queued and matches */
    km_event_t evt;
    assert(km_event_queue_pop(&f.core.event_queue, &evt) == KM_OK);
    assert(evt.type == KM_EVENT_LOAD_CANCELLED);
    assert(evt.req_id == 2001u);
    assert(evt.client_id == KM_CLIENT_HOST);

    /* The old driver edge telemetry is inert on this path; assert what it
     * stood for: motion settled, driver healthy and off, trajectory clean. */
    assert(!f.feeder.base.enabled);
    assert(km_driver_get_fault(&f.feeder.base) == KM_FAULT_NONE);
    assert(f.core.trajectory.faulted == false);
}

int main(void)
{
    puts("=== Running Klipper-MMU MCU Clock Test Suite ===");

    puts("1. Testing initialization validation, inert states, and pure getters...");
    test_init_validation_and_inert_states();

    puts("2. Testing rates, observation chunks, sub-microsecond samples, and oracles...");
    test_rates_and_reference_oracles();

    puts("3. Testing chunking invariance across frequencies...");
    test_chunking_invariance();

    puts("4. Testing complete microsecond and millisecond counter wraps...");
    test_us_and_ms_wraps();

    puts("5. Testing clock fault latching, persistence, and output preservation...");
    test_fault_latching_and_persistence();

    puts("6. Testing deadline conversion contract, rounding, and range limits...");
    test_deadline_conversion();

    puts("7. Testing production core composition on the tick path across raw timer wrap...");
    test_production_core_composition();

    puts("=== All MCU Clock Tests Passed Successfully! ===");
    return 0;
}
