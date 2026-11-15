#include "aq/core.h"
#include "aq/inference.h"

#include <stdlib.h>

#define CHECK(cond) do { if (!(cond)) { fprintf(stderr, "CHECK failed at %s:%d: %s\n", __FILE__, __LINE__, #cond); abort(); } } while (0)
#include <limits.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

static void test_checked_math(void) {
    int64_t out = 0;
    CHECK(aq_checked_add_i64(INT64_MAX, 1, &out) == AQ_ERR_OVERFLOW);
    CHECK(aq_checked_sub_i64(INT64_MIN, 1, &out) == AQ_ERR_OVERFLOW);
    CHECK(aq_checked_mul_i64(INT64_MAX, 2, &out) == AQ_ERR_OVERFLOW);
    CHECK(aq_checked_mul_i64(12500, 10, &out) == AQ_OK && out == 125000);
}

static void test_abi_and_risk(void) {
    aq_instrument_spec_t spec;
    memset(&spec, 0, sizeof(spec));
    spec.abi_version = AQ_CORE_ABI_VERSION;
    spec.struct_size = sizeof(spec);
    snprintf(spec.symbol, sizeof(spec.symbol), "NVDA");
    snprintf(spec.currency, sizeof(spec.currency), "USD");
    spec.price_scale = 100;
    spec.qty_scale = 1;
    spec.tick_size = 1;

    aq_account_snapshot_t acct;
    memset(&acct, 0, sizeof(acct));
    acct.abi_version = AQ_CORE_ABI_VERSION;
    acct.struct_size = sizeof(acct);
    acct.cash_cents = 10000000;
    acct.equity_cents = 10000000;
    acct.daily_pnl_bps = 0;
    acct.total_drawdown_bps = 0;
    acct.account_revision = 1;
    acct.control_epoch = 1;
    acct.integrity_ok = 1;

    aq_risk_policy_t pol;
    memset(&pol, 0, sizeof(pol));
    pol.abi_version = AQ_CORE_ABI_VERSION;
    pol.struct_size = sizeof(pol);
    pol.max_order_notional_bps = 2500;
    pol.max_concentration_bps = 3500;
    pol.max_gross_exposure_bps = 9000;
    pol.max_daily_loss_bps = 500;
    pol.max_total_loss_bps = 1000;
    pol.max_data_age_ms = 30000;
    pol.no_short_selling = 1;
    pol.allow_protective_brackets = 0;

    aq_order_intent_t intent;
    memset(&intent, 0, sizeof(intent));
    intent.abi_version = AQ_CORE_ABI_VERSION;
    intent.struct_size = sizeof(intent);
    snprintf(intent.symbol, sizeof(intent.symbol), "NVDA");
    intent.side = AQ_SIDE_BUY;
    intent.order_type = AQ_ORDER_MARKET;
    intent.qty = 10;
    intent.price_cents = 12000;
    intent.data_age_ms = 500;

    aq_risk_decision_t dec;
    memset(&dec, 0, sizeof(dec));
    dec.abi_version = AQ_CORE_ABI_VERSION;
    dec.struct_size = sizeof(dec);

    CHECK(aq_evaluate_risk(&spec, &acct, &pol, &intent, &dec) == AQ_OK);
    CHECK(dec.approved == 1 && dec.reason_mask == AQ_REJ_NONE);

    /* Unsupported stop/target must be rejected before dispatch. */
    intent.stop_cents = 9000;
    intent.target_cents = 15000;
    CHECK(aq_evaluate_risk(&spec, &acct, &pol, &intent, &dec) == AQ_OK);
    CHECK(dec.approved == 0 && (dec.reason_mask & AQ_REJ_UNSUPPORTED_PROTECTION));
    intent.stop_cents = 0;
    intent.target_cents = 0;

    /* ABI mismatch / null / struct_size mismatch fail closed. */
    CHECK(aq_evaluate_risk(NULL, &acct, &pol, &intent, &dec) == AQ_ERR_NULL);
    intent.struct_size = 4;
    CHECK(aq_evaluate_risk(&spec, &acct, &pol, &intent, &dec) == AQ_ERR_STRUCT_SIZE);
    intent.struct_size = sizeof(intent);

    /* Non-USD or fractional scale rejected. */
    snprintf(spec.currency, sizeof(spec.currency), "EUR");
    CHECK(aq_evaluate_risk(&spec, &acct, &pol, &intent, &dec) == AQ_ERR_UNSUPPORTED_UNIT);
}

int main(int argc, char** argv) {
    test_checked_math();
    test_abi_and_risk();

    if (argc >= 2) {
        aq_model_handle* h = NULL;
        CHECK(aq_model_load(argv[1], &h) == AQ_OK && h != NULL);
        aq_obs_v1_t obs;
        memset(&obs, 0, sizeof(obs));
        obs.abi_version = AQ_INFERENCE_ABI_VERSION;
        obs.struct_size = sizeof(obs);
        obs.feature_schema_version = 1;
        obs.features[4] = 0.0;
        obs.features[5] = 1.0;
        obs.features[7] = 1.0;

        aq_proposal_v1_t prop;
        memset(&prop, 0, sizeof(prop));
        prop.abi_version = AQ_INFERENCE_ABI_VERSION;
        prop.struct_size = sizeof(prop);
        CHECK(aq_model_predict_v1(h, &obs, &prop) == AQ_OK);
        CHECK(prop.execution_authorized == 0);
        printf("{\"c_abi_infer\":\"ok\",\"chosen_action\":%u,\"target_exposure_pct\":%.2f,\"execution_authorized\":%u}\n",
               prop.chosen_action, prop.target_exposure_pct, (unsigned)prop.execution_authorized);
        aq_model_free(h);
    } else {
        printf("c_abi_smoke: all C17 ABI checks passed\n");
    }
    return 0;
}
