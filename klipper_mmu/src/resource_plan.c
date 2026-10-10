// SPDX-License-Identifier: GPL-3.0-or-later
#include "klipper_mmu/resource_plan.h"
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

static inline bool is_valid_kind(km_resource_kind_t kind)
{
    return kind == KM_RESOURCE_PIN ||
           kind == KM_RESOURCE_TIMER_EXCLUSIVE ||
           kind == KM_RESOURCE_TIMER_PWM_BASE ||
           kind == KM_RESOURCE_TIMER_CHANNEL ||
           kind == KM_RESOURCE_ADC_UNIT ||
           kind == KM_RESOURCE_ADC_CHANNEL;
}

static inline bool is_timer_kind(km_resource_kind_t kind)
{
    return kind == KM_RESOURCE_TIMER_EXCLUSIVE ||
           kind == KM_RESOURCE_TIMER_PWM_BASE ||
           kind == KM_RESOURCE_TIMER_CHANNEL;
}

static inline bool is_adc_kind(km_resource_kind_t kind)
{
    return kind == KM_RESOURCE_ADC_UNIT ||
           kind == KM_RESOURCE_ADC_CHANNEL;
}

static bool claim_is_valid(const km_resource_claim_t *c)
{
    if (!is_valid_kind(c->kind)) {
        return false;
    }
    if (c->instance == UINT16_MAX) {
        return false;
    }
    if (c->owner == 0u || c->owner == UINT16_MAX) {
        return false;
    }

    switch (c->kind) {
    case KM_RESOURCE_PIN:
    case KM_RESOURCE_TIMER_EXCLUSIVE:
    case KM_RESOURCE_ADC_UNIT:
        if (c->subresource != 0u || c->period_ticks != 0u || c->tick_hz != 0u) {
            return false;
        }
        break;

    case KM_RESOURCE_TIMER_PWM_BASE:
        if (c->subresource != 0u) {
            return false;
        }
        if (c->period_ticks == 0u || c->tick_hz == 0u) {
            return false;
        }
        break;

    case KM_RESOURCE_TIMER_CHANNEL:
        if (c->subresource == 0u || c->subresource == UINT16_MAX) {
            return false;
        }
        if (c->period_ticks != 0u || c->tick_hz != 0u) {
            return false;
        }
        break;

    case KM_RESOURCE_ADC_CHANNEL:
        if (c->subresource == UINT16_MAX) {
            return false;
        }
        if (c->period_ticks != 0u || c->tick_hz != 0u) {
            return false;
        }
        break;

    default:
        return false;
    }

    return true;
}

static bool check_pair_conflict(const km_resource_claim_t *c1,
                                const km_resource_claim_t *c2,
                                km_resource_diag_reason_t *reason_out)
{
    /* Rule 1: PIN is exclusive by instance, regardless of owner. */
    if (c1->kind == KM_RESOURCE_PIN && c2->kind == KM_RESOURCE_PIN) {
        if (c1->instance == c2->instance) {
            *reason_out = KM_RESOURCE_DIAG_DUPLICATE_RESOURCE;
            return true;
        }
        return false;
    }

    /* Timer kinds share timer instance namespace */
    if (is_timer_kind(c1->kind) && is_timer_kind(c2->kind)) {
        if (c1->instance != c2->instance) {
            return false;
        }

        /* Rule 2: TIMER_EXCLUSIVE conflicts with any timer kind on same instance */
        if (c1->kind == KM_RESOURCE_TIMER_EXCLUSIVE || c2->kind == KM_RESOURCE_TIMER_EXCLUSIVE) {
            *reason_out = KM_RESOURCE_DIAG_DUPLICATE_RESOURCE;
            return true;
        }

        /* Rule 3: TIMER_PWM_BASE claims on same instance */
        if (c1->kind == KM_RESOURCE_TIMER_PWM_BASE && c2->kind == KM_RESOURCE_TIMER_PWM_BASE) {
            if (c1->period_ticks != c2->period_ticks || c1->tick_hz != c2->tick_hz) {
                *reason_out = KM_RESOURCE_DIAG_TIMER_CONFIGURATION;
                return true;
            }
            if (c1->owner == c2->owner) {
                *reason_out = KM_RESOURCE_DIAG_DUPLICATE_RESOURCE;
                return true;
            }
            /* Compatible shared PWM base between different owners */
            return false;
        }

        /* Rule 4: TIMER_CHANNEL is exclusive by (instance, subresource) regardless of owner */
        if (c1->kind == KM_RESOURCE_TIMER_CHANNEL && c2->kind == KM_RESOURCE_TIMER_CHANNEL) {
            if (c1->subresource == c2->subresource) {
                *reason_out = KM_RESOURCE_DIAG_DUPLICATE_RESOURCE;
                return true;
            }
            return false;
        }

        /* TIMER_PWM_BASE and TIMER_CHANNEL on same instance are compatible pairwise */
        return false;
    }

    /* ADC kinds share ADC unit instance namespace */
    if (is_adc_kind(c1->kind) && is_adc_kind(c2->kind)) {
        if (c1->instance != c2->instance) {
            return false;
        }

        /* Rule 5: ADC unit dedicated to single owner. Any different owners on same unit conflict */
        if (c1->owner != c2->owner) {
            *reason_out = KM_RESOURCE_DIAG_DUPLICATE_RESOURCE;
            return true;
        }

        /* Same owner: two ADC_UNIT claims conflict */
        if (c1->kind == KM_RESOURCE_ADC_UNIT && c2->kind == KM_RESOURCE_ADC_UNIT) {
            *reason_out = KM_RESOURCE_DIAG_DUPLICATE_RESOURCE;
            return true;
        }

        /* Same owner: duplicate channel claims conflict */
        if (c1->kind == KM_RESOURCE_ADC_CHANNEL && c2->kind == KM_RESOURCE_ADC_CHANNEL) {
            if (c1->subresource == c2->subresource) {
                *reason_out = KM_RESOURCE_DIAG_DUPLICATE_RESOURCE;
                return true;
            }
            return false;
        }

        /* Same owner: ADC_UNIT and ADC_CHANNEL, or different channels: compatible */
        return false;
    }

    /* Independent namespaces (PIN vs TIMER, PIN vs ADC, TIMER vs ADC) never collide */
    return false;
}

KM_API km_result_t km_resource_plan_validate(const km_resource_claim_t *claims,
                                             size_t count,
                                             km_resource_diagnostic_t *diagnostic)
{
    km_resource_diagnostic_t diag = {
        .reason = KM_RESOURCE_DIAG_NONE,
        .first_index = KM_RESOURCE_NO_INDEX,
        .second_index = KM_RESOURCE_NO_INDEX
    };

    /* 1. Plan-level argument validation */
    if (count > KM_RESOURCE_MAX_CLAIMS || (count > 0u && claims == NULL)) {
        diag.reason = KM_RESOURCE_DIAG_BAD_ARGUMENT;
        diag.first_index = KM_RESOURCE_NO_INDEX;
        diag.second_index = KM_RESOURCE_NO_INDEX;
        if (diagnostic != NULL) {
            *diagnostic = diag;
        }
        return KM_ERR_INVALID_PARAM;
    }

    if (count == 0u) {
        diag.reason = KM_RESOURCE_DIAG_NONE;
        diag.first_index = KM_RESOURCE_NO_INDEX;
        diag.second_index = KM_RESOURCE_NO_INDEX;
        if (diagnostic != NULL) {
            *diagnostic = diag;
        }
        return KM_OK;
    }

    /* 2. Individual record validation in ascending index order */
    for (size_t i = 0; i < count; ++i) {
        if (!claim_is_valid(&claims[i])) {
            diag.reason = KM_RESOURCE_DIAG_BAD_CLAIM;
            diag.first_index = (uint16_t)i;
            diag.second_index = KM_RESOURCE_NO_INDEX;
            if (diagnostic != NULL) {
                *diagnostic = diag;
            }
            return KM_ERR_INVALID_PARAM;
        }
    }

    /* 3. Pairwise compatibility validation in lexicographical order (i, j), i < j */
    for (size_t i = 0; i < count; ++i) {
        for (size_t j = i + 1u; j < count; ++j) {
            km_resource_diag_reason_t reason = KM_RESOURCE_DIAG_NONE;
            if (check_pair_conflict(&claims[i], &claims[j], &reason)) {
                diag.reason = reason;
                diag.first_index = (uint16_t)i;
                diag.second_index = (uint16_t)j;
                if (diagnostic != NULL) {
                    *diagnostic = diag;
                }
                return KM_ERR_CONFLICT;
            }
        }
    }

    /* 4. Missing required companion validation in ascending channel index order */
    for (size_t i = 0; i < count; ++i) {
        if (claims[i].kind == KM_RESOURCE_TIMER_CHANNEL) {
            bool found_base = false;
            for (size_t k = 0; k < count; ++k) {
                if (claims[k].kind == KM_RESOURCE_TIMER_PWM_BASE &&
                    claims[k].instance == claims[i].instance &&
                    claims[k].owner == claims[i].owner) {
                    found_base = true;
                    break;
                }
            }
            if (!found_base) {
                diag.reason = KM_RESOURCE_DIAG_MISSING_TIMER_BASE;
                diag.first_index = (uint16_t)i;
                diag.second_index = KM_RESOURCE_NO_INDEX;
                if (diagnostic != NULL) {
                    *diagnostic = diag;
                }
                return KM_ERR_INVALID_PARAM;
            }
        } else if (claims[i].kind == KM_RESOURCE_ADC_CHANNEL) {
            bool found_unit = false;
            for (size_t k = 0; k < count; ++k) {
                if (claims[k].kind == KM_RESOURCE_ADC_UNIT &&
                    claims[k].instance == claims[i].instance &&
                    claims[k].owner == claims[i].owner) {
                    found_unit = true;
                    break;
                }
            }
            if (!found_unit) {
                diag.reason = KM_RESOURCE_DIAG_MISSING_ADC_UNIT;
                diag.first_index = (uint16_t)i;
                diag.second_index = KM_RESOURCE_NO_INDEX;
                if (diagnostic != NULL) {
                    *diagnostic = diag;
                }
                return KM_ERR_INVALID_PARAM;
            }
        }
    }

    /* 5. Complete plan is valid */
    diag.reason = KM_RESOURCE_DIAG_NONE;
    diag.first_index = KM_RESOURCE_NO_INDEX;
    diag.second_index = KM_RESOURCE_NO_INDEX;
    if (diagnostic != NULL) {
        *diagnostic = diag;
    }
    return KM_OK;
}
