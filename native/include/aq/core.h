#ifndef AQ_CORE_H
#define AQ_CORE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define AQ_CORE_ABI_VERSION 1u
#define AQ_MAX_SYMBOL_LEN 16u
#define AQ_MAX_REASONS 12u

typedef enum aq_status_t {
    AQ_OK = 0,
    AQ_ERR_NULL = 1,
    AQ_ERR_ABI_VERSION = 2,
    AQ_ERR_STRUCT_SIZE = 3,
    AQ_ERR_OVERFLOW = 4,
    AQ_ERR_INVALID_ARG = 5,
    AQ_ERR_UNSUPPORTED_UNIT = 6,
    AQ_ERR_UNSUPPORTED_PROTECTION = 7,
    AQ_ERR_CORRUPT = 8,
    AQ_ERR_IO = 9
} aq_status_t;

typedef enum aq_side_t {
    AQ_SIDE_BUY = 1,
    AQ_SIDE_SELL = 2
} aq_side_t;

typedef enum aq_order_type_t {
    AQ_ORDER_MARKET = 1,
    AQ_ORDER_LIMIT = 2
} aq_order_type_t;

typedef enum aq_reason_bit_t {
    AQ_REJ_NONE = 0,
    AQ_REJ_HALTED = 1u << 0,
    AQ_REJ_INTEGRITY = 1u << 1,
    AQ_REJ_STALE_DATA = 1u << 2,
    AQ_REJ_MISSING_LOSS_STATE = 1u << 3,
    AQ_REJ_DAILY_LOSS = 1u << 4,
    AQ_REJ_TOTAL_DRAWDOWN = 1u << 5,
    AQ_REJ_INSUFFICIENT_CASH = 1u << 6,
    AQ_REJ_NOTIONAL_LIMIT = 1u << 7,
    AQ_REJ_CONCENTRATION_LIMIT = 1u << 8,
    AQ_REJ_GROSS_EXPOSURE_LIMIT = 1u << 9,
    AQ_REJ_SHORT_SELLING = 1u << 10,
    AQ_REJ_UNSUPPORTED_PROTECTION = 1u << 11,
    AQ_REJ_UNSUPPORTED_UNIT = 1u << 12,
    AQ_REJ_INVALID_VALUATION = 1u << 13,
    AQ_REJ_MISSING_MARK = 1u << 14
} aq_reason_bit_t;

/* Explicit instrument unit & scale contract (v1 supports USD cents + whole shares). */
typedef struct aq_instrument_spec_t {
    uint32_t abi_version;
    uint32_t struct_size;
    char symbol[AQ_MAX_SYMBOL_LEN];
    char currency[4];       /* Must be "USD" in v1 */
    int64_t price_scale;    /* 100 = USD cents */
    int64_t qty_scale;      /* 1 = whole shares */
    int64_t tick_size;      /* >= 1 in price_scale units */
} aq_instrument_spec_t;

typedef struct aq_position_mark_t {
    char symbol[AQ_MAX_SYMBOL_LEN];
    int64_t qty;            /* Signed shares in qty_scale */
    int64_t mark_cents;     /* Price in USD cents (> 0) */
} aq_position_mark_t;

typedef struct aq_account_snapshot_t {
    uint32_t abi_version;
    uint32_t struct_size;
    int64_t cash_cents;
    int64_t equity_cents;
    int64_t reserved_buy_cents;
    int64_t daily_pnl_bps;          /* INT64_MIN = missing */
    int64_t total_drawdown_bps;     /* INT64_MIN = missing */
    uint64_t account_revision;
    uint64_t control_epoch;
    uint8_t halted;
    uint8_t integrity_ok;
    uint32_t position_count;
    const aq_position_mark_t* positions;
} aq_account_snapshot_t;

typedef struct aq_risk_policy_t {
    uint32_t abi_version;
    uint32_t struct_size;
    int64_t max_order_notional_bps; /* e.g. 2500 = 25% */
    int64_t max_concentration_bps;  /* e.g. 3500 = 35% */
    int64_t max_gross_exposure_bps; /* e.g. 9000 = 90% */
    int64_t max_daily_loss_bps;     /* e.g. 500 = 5% */
    int64_t max_total_loss_bps;     /* e.g. 1000 = 10% */
    int64_t max_data_age_ms;        /* e.g. 30000 = 30s */
    uint8_t no_short_selling;
    uint8_t allow_protective_brackets; /* 0 = reject stop/target before dispatch */
} aq_risk_policy_t;

typedef struct aq_order_intent_t {
    uint32_t abi_version;
    uint32_t struct_size;
    char symbol[AQ_MAX_SYMBOL_LEN];
    uint8_t side;           /* aq_side_t */
    uint8_t order_type;     /* aq_order_type_t */
    int64_t qty;            /* > 0 in qty_scale */
    int64_t price_cents;    /* Mark or limit price in cents (> 0) */
    int64_t stop_cents;     /* 0 if unset */
    int64_t target_cents;   /* 0 if unset */
    int64_t data_age_ms;    /* >= 0 */
    int64_t reserved_sell_qty; /* Already-reserved sell qty for symbol */
} aq_order_intent_t;

typedef struct aq_risk_decision_t {
    uint32_t abi_version;
    uint32_t struct_size;
    uint8_t approved;
    uint8_t risk_reducing;
    uint32_t reason_mask;
    uint64_t account_revision;
    uint64_t control_epoch;
    int64_t order_notional_cents;
} aq_risk_decision_t;

/* Checked integer arithmetic (fail-closed on overflow). */
aq_status_t aq_checked_add_i64(int64_t a, int64_t b, int64_t* out);
aq_status_t aq_checked_sub_i64(int64_t a, int64_t b, int64_t* out);
aq_status_t aq_checked_mul_i64(int64_t a, int64_t b, int64_t* out);

/* Validate instrument scale/currency contract. */
aq_status_t aq_validate_instrument(const aq_instrument_spec_t* spec);

/* Stateless deterministic risk evaluation (pure C17, no exceptions, no hidden defaults). */
aq_status_t aq_evaluate_risk(
    const aq_instrument_spec_t* spec,
    const aq_account_snapshot_t* acct,
    const aq_risk_policy_t* policy,
    const aq_order_intent_t* intent,
    aq_risk_decision_t* out_decision);

#ifdef __cplusplus
}
#endif

#endif /* AQ_CORE_H */
