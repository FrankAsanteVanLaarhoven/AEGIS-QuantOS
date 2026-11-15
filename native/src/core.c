#include "aq/core.h"

#include <limits.h>
#include <string.h>

aq_status_t aq_checked_add_i64(int64_t a, int64_t b, int64_t* out) {
    if (!out) return AQ_ERR_NULL;
    if ((b > 0 && a > INT64_MAX - b) || (b < 0 && a < INT64_MIN - b)) {
        return AQ_ERR_OVERFLOW;
    }
    *out = a + b;
    return AQ_OK;
}

aq_status_t aq_checked_sub_i64(int64_t a, int64_t b, int64_t* out) {
    if (!out) return AQ_ERR_NULL;
    if ((b < 0 && a > INT64_MAX + b) || (b > 0 && a < INT64_MIN + b)) {
        return AQ_ERR_OVERFLOW;
    }
    *out = a - b;
    return AQ_OK;
}

aq_status_t aq_checked_mul_i64(int64_t a, int64_t b, int64_t* out) {
    if (!out) return AQ_ERR_NULL;
    if (a == 0 || b == 0) {
        *out = 0;
        return AQ_OK;
    }
    if (a == -1 && b == INT64_MIN) return AQ_ERR_OVERFLOW;
    if (b == -1 && a == INT64_MIN) return AQ_ERR_OVERFLOW;
    if (a > 0) {
        if (b > 0 && a > INT64_MAX / b) return AQ_ERR_OVERFLOW;
        if (b < 0 && b < INT64_MIN / a) return AQ_ERR_OVERFLOW;
    } else {
        if (b > 0 && a < INT64_MIN / b) return AQ_ERR_OVERFLOW;
        if (b < 0 && a < INT64_MAX / b) return AQ_ERR_OVERFLOW;
    }
    *out = a * b;
    return AQ_OK;
}

aq_status_t aq_checked_abs_i64(int64_t a, int64_t* out) {
    if (!out) return AQ_ERR_NULL;
    if (a == INT64_MIN) return AQ_ERR_OVERFLOW;
    *out = (a < 0) ? -a : a;
    return AQ_OK;
}

static int sym_valid(const char* s) {
    if (!s || s[0] == '\0') return 0;
    return memchr(s, '\0', AQ_MAX_SYMBOL_LEN) != NULL;
}

aq_status_t aq_validate_instrument(const aq_instrument_spec_t* spec) {
    if (!spec) return AQ_ERR_NULL;
    if (spec->abi_version != AQ_CORE_ABI_VERSION) return AQ_ERR_ABI_VERSION;
    if (spec->struct_size != sizeof(aq_instrument_spec_t)) return AQ_ERR_STRUCT_SIZE;
    if (!sym_valid(spec->symbol)) return AQ_ERR_INVALID_ARG;
    if (spec->currency[3] != '\0' || strncmp(spec->currency, "USD", 4) != 0) {
        return AQ_ERR_UNSUPPORTED_UNIT;
    }
    if (spec->price_scale != 100 || spec->qty_scale != 1 || spec->tick_size < 1) {
        return AQ_ERR_UNSUPPORTED_UNIT;
    }
    return AQ_OK;
}

static int sym_eq(const char* a, const char* b) {
    return strncmp(a, b, AQ_MAX_SYMBOL_LEN) == 0;
}

aq_status_t aq_evaluate_risk(
    const aq_instrument_spec_t* spec,
    const aq_account_snapshot_t* acct,
    const aq_risk_policy_t* policy,
    const aq_order_intent_t* intent,
    aq_risk_decision_t* out_decision) {
    if (!spec || !acct || !policy || !intent || !out_decision) return AQ_ERR_NULL;
    if (acct->abi_version != AQ_CORE_ABI_VERSION ||
        policy->abi_version != AQ_CORE_ABI_VERSION ||
        intent->abi_version != AQ_CORE_ABI_VERSION) {
        return AQ_ERR_ABI_VERSION;
    }
    if (acct->struct_size != sizeof(aq_account_snapshot_t) ||
        policy->struct_size != sizeof(aq_risk_policy_t) ||
        intent->struct_size != sizeof(aq_order_intent_t) ||
        out_decision->struct_size != sizeof(aq_risk_decision_t)) {
        return AQ_ERR_STRUCT_SIZE;
    }

    memset(out_decision, 0, sizeof(*out_decision));
    out_decision->abi_version = AQ_CORE_ABI_VERSION;
    out_decision->struct_size = sizeof(aq_risk_decision_t);
    out_decision->account_revision = acct->account_revision;
    out_decision->control_epoch = acct->control_epoch;

    aq_status_t ist = aq_validate_instrument(spec);
    if (ist != AQ_OK) {
        out_decision->reason_mask |= AQ_REJ_UNSUPPORTED_UNIT;
        return ist;
    }
    if (!sym_valid(intent->symbol) || !sym_eq(spec->symbol, intent->symbol)) {
        return AQ_ERR_INVALID_ARG;
    }
    if (intent->side != AQ_SIDE_BUY && intent->side != AQ_SIDE_SELL) {
        return AQ_ERR_INVALID_ARG;
    }
    if (intent->order_type != AQ_ORDER_MARKET && intent->order_type != AQ_ORDER_LIMIT) {
        return AQ_ERR_INVALID_ARG;
    }
    if (policy->max_order_notional_bps <= 0 || policy->max_order_notional_bps > 10000 ||
        policy->max_concentration_bps <= 0 || policy->max_concentration_bps > 10000 ||
        policy->max_gross_exposure_bps <= 0 || policy->max_gross_exposure_bps > 50000 ||
        policy->max_daily_loss_bps <= 0 || policy->max_daily_loss_bps > 10000 ||
        policy->max_total_loss_bps <= 0 || policy->max_total_loss_bps > 10000 ||
        policy->max_data_age_ms < 0 || policy->no_short_selling > 1 ||
        policy->allow_protective_brackets > 1) {
        return AQ_ERR_INVALID_ARG;
    }
    if (intent->qty <= 0 || intent->price_cents <= 0 ||
        (intent->price_cents % spec->tick_size) != 0 ||
        intent->data_age_ms < 0 ||
        intent->reserved_sell_qty < 0 || acct->cash_cents < 0 || acct->equity_cents <= 0 ||
        acct->reserved_buy_cents < 0) {
        out_decision->reason_mask |= AQ_REJ_INVALID_VALUATION;
        return AQ_OK;
    }
    if (acct->position_count > 0 && !acct->positions) {
        return AQ_ERR_NULL;
    }
    if (acct->reservation_count > 0 && !acct->reservations) {
        return AQ_ERR_NULL;
    }

    int64_t order_notional = 0;
    if (aq_checked_mul_i64(intent->qty, intent->price_cents, &order_notional) != AQ_OK) {
        return AQ_ERR_OVERFLOW;
    }
    out_decision->order_notional_cents = order_notional;

    /* Never silently drop unsupported protective parameters. */
    if (!policy->allow_protective_brackets &&
        (intent->stop_cents != 0 || intent->target_cents != 0)) {
        out_decision->reason_mask |= AQ_REJ_UNSUPPORTED_PROTECTION;
        return AQ_OK;
    }

    int64_t held_qty = 0;
    int64_t sym_mark_cents = intent->price_cents;
    int64_t held_gross = 0;
    for (uint32_t i = 0; i < acct->position_count; ++i) {
        const aq_position_mark_t* pm = &acct->positions[i];
        if (!sym_valid(pm->symbol)) {
            return AQ_ERR_INVALID_ARG;
        }
        for (uint32_t j = 0; j < i; ++j) {
            if (sym_eq(acct->positions[j].symbol, pm->symbol)) {
                out_decision->reason_mask |= AQ_REJ_DUPLICATE_POSITION;
                return AQ_ERR_INVALID_ARG;
            }
        }
        int64_t abs_q = 0;
        if (aq_checked_abs_i64(pm->qty, &abs_q) != AQ_OK) {
            return AQ_ERR_OVERFLOW;
        }
        if (abs_q > 0 && pm->mark_cents <= 0) {
            out_decision->reason_mask |= AQ_REJ_MISSING_MARK;
        } else if (abs_q > 0) {
            int64_t pos_notional = 0;
            if (aq_checked_mul_i64(abs_q, pm->mark_cents, &pos_notional) != AQ_OK ||
                aq_checked_add_i64(held_gross, pos_notional, &held_gross) != AQ_OK) {
                return AQ_ERR_OVERFLOW;
            }
        }
        if (sym_eq(pm->symbol, intent->symbol)) {
            held_qty = pm->qty;
            if (pm->mark_cents > 0) sym_mark_cents = pm->mark_cents;
        }
    }

    int64_t sym_res_buy_cents = 0;
    int64_t sym_res_sell_qty = 0;
    int64_t sum_res_buy_cents = 0;
    for (uint32_t i = 0; i < acct->reservation_count; ++i) {
        const aq_symbol_reservation_t* r = &acct->reservations[i];
        if (!sym_valid(r->symbol) || r->reserved_buy_cents < 0 ||
            r->reserved_buy_qty < 0 || r->reserved_sell_qty < 0) {
            return AQ_ERR_INVALID_ARG;
        }
        for (uint32_t j = 0; j < i; ++j) {
            if (sym_eq(acct->reservations[j].symbol, r->symbol)) {
                return AQ_ERR_INVALID_ARG;
            }
        }
        if (aq_checked_add_i64(sum_res_buy_cents, r->reserved_buy_cents, &sum_res_buy_cents) != AQ_OK) {
            return AQ_ERR_OVERFLOW;
        }
        if (sym_eq(r->symbol, intent->symbol)) {
            sym_res_buy_cents = r->reserved_buy_cents;
            sym_res_sell_qty = r->reserved_sell_qty;
        }
    }
    if (sum_res_buy_cents > acct->reserved_buy_cents) {
        return AQ_ERR_INVALID_ARG;
    }

    int64_t effective_res_sell = intent->reserved_sell_qty > sym_res_sell_qty
                                     ? intent->reserved_sell_qty
                                     : sym_res_sell_qty;
    int64_t avail_sell_qty = 0;
    if (aq_checked_sub_i64(held_qty, effective_res_sell, &avail_sell_qty) != AQ_OK) {
        return AQ_ERR_OVERFLOW;
    }
    uint8_t is_risk_reducing =
        (intent->side == AQ_SIDE_SELL && acct->integrity_ok && avail_sell_qty >= intent->qty) ? 1u : 0u;
    out_decision->risk_reducing = is_risk_reducing;

    if (!acct->integrity_ok && !is_risk_reducing) {
        out_decision->reason_mask |= AQ_REJ_INTEGRITY;
    }
    if (acct->halted && !is_risk_reducing) {
        out_decision->reason_mask |= AQ_REJ_HALTED;
    }
    if (intent->data_age_ms > policy->max_data_age_ms && !is_risk_reducing) {
        out_decision->reason_mask |= AQ_REJ_STALE_DATA;
    }

    if (intent->side == AQ_SIDE_SELL && policy->no_short_selling && avail_sell_qty < intent->qty) {
        out_decision->reason_mask |= AQ_REJ_SHORT_SELLING;
    }

    if (!is_risk_reducing) {
        if (acct->daily_pnl_bps == INT64_MIN || acct->total_drawdown_bps == INT64_MIN) {
            out_decision->reason_mask |= AQ_REJ_MISSING_LOSS_STATE;
        } else {
            if (acct->daily_pnl_bps <= -policy->max_daily_loss_bps) {
                out_decision->reason_mask |= AQ_REJ_DAILY_LOSS;
            }
            if (acct->total_drawdown_bps >= policy->max_total_loss_bps) {
                out_decision->reason_mask |= AQ_REJ_TOTAL_DRAWDOWN;
            }
        }

        int64_t avail_cash = 0;
        if (aq_checked_sub_i64(acct->cash_cents, acct->reserved_buy_cents, &avail_cash) != AQ_OK) {
            return AQ_ERR_OVERFLOW;
        }
        if (intent->side == AQ_SIDE_BUY && order_notional > avail_cash) {
            out_decision->reason_mask |= AQ_REJ_INSUFFICIENT_CASH;
        }

        /* Compare order_notional * 10000 > equity_cents * max_bps without overflow. */
        int64_t lhs_notional = 0;
        int64_t rhs_notional = 0;
        if (aq_checked_mul_i64(order_notional, 10000, &lhs_notional) != AQ_OK ||
            aq_checked_mul_i64(acct->equity_cents, policy->max_order_notional_bps, &rhs_notional) != AQ_OK) {
            return AQ_ERR_OVERFLOW;
        }
        if (lhs_notional > rhs_notional) {
            out_decision->reason_mask |= AQ_REJ_NOTIONAL_LIMIT;
        }

        int64_t abs_held = 0;
        int64_t held_sym_notional = 0;
        int64_t next_sym_notional = 0;
        if (aq_checked_abs_i64(held_qty, &abs_held) != AQ_OK ||
            aq_checked_mul_i64(abs_held, sym_mark_cents, &held_sym_notional) != AQ_OK ||
            aq_checked_add_i64(held_sym_notional, sym_res_buy_cents, &next_sym_notional) != AQ_OK ||
            aq_checked_add_i64(next_sym_notional, order_notional, &next_sym_notional) != AQ_OK) {
            return AQ_ERR_OVERFLOW;
        }
        int64_t lhs_conc = 0;
        int64_t rhs_conc = 0;
        if (aq_checked_mul_i64(next_sym_notional, 10000, &lhs_conc) != AQ_OK ||
            aq_checked_mul_i64(acct->equity_cents, policy->max_concentration_bps, &rhs_conc) != AQ_OK) {
            return AQ_ERR_OVERFLOW;
        }
        if (intent->side == AQ_SIDE_BUY && lhs_conc > rhs_conc) {
            out_decision->reason_mask |= AQ_REJ_CONCENTRATION_LIMIT;
        }

        int64_t gross_next = 0;
        if (aq_checked_add_i64(held_gross, acct->reserved_buy_cents, &gross_next) != AQ_OK ||
            aq_checked_add_i64(gross_next, order_notional, &gross_next) != AQ_OK) {
            return AQ_ERR_OVERFLOW;
        }
        int64_t lhs_gross = 0;
        int64_t rhs_gross = 0;
        if (aq_checked_mul_i64(gross_next, 10000, &lhs_gross) != AQ_OK ||
            aq_checked_mul_i64(acct->equity_cents, policy->max_gross_exposure_bps, &rhs_gross) != AQ_OK) {
            return AQ_ERR_OVERFLOW;
        }
        if (intent->side == AQ_SIDE_BUY && lhs_gross > rhs_gross) {
            out_decision->reason_mask |= AQ_REJ_GROSS_EXPOSURE_LIMIT;
        }
    }

    out_decision->approved = (out_decision->reason_mask == AQ_REJ_NONE) ? 1u : 0u;
    return AQ_OK;
}

