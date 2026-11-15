#pragma once

#include "aq/core.h"
#include "aq/storage.hpp"

#include <cstdint>
#include <functional>
#include <string>
#include <vector>

namespace aq {

enum class OrderState {
    VALIDATED,
    RESERVED,
    PENDING_DISPATCH,
    DISPATCHED_UNKNOWN,
    ACCEPTED,
    PARTIAL,
    FILLED,
    REJECTED,
    CANCEL_PENDING,
    CANCELLED,
    EXPIRED
};

std::string order_state_to_str(OrderState st);
OrderState order_state_from_str(const std::string& s);

struct OmsAccountView {
    std::string env = "paper_native";
    int64_t cash_cents = 10000000;
    int64_t equity_cents = 10000000;
    uint64_t account_revision = 1;
    bool integrity_ok = true;
    int64_t daily_pnl_bps = 0;
    int64_t total_drawdown_bps = 0;
    std::vector<aq_position_mark_t> positions;
    std::vector<aq_symbol_reservation_t> reservations;
    int64_t total_reserved_buy_cents = 0;
};

struct OmsOrderRecord {
    std::string client_order_id;
    std::string env;
    std::string symbol;
    uint8_t side = AQ_SIDE_BUY;
    uint8_t order_type = AQ_ORDER_LIMIT;
    int64_t qty = 0;
    int64_t filled_qty = 0;
    int64_t limit_price_cents = 0;
    int64_t reserved_cash_cents = 0;
    int64_t reserved_sell_qty = 0;
    OrderState state = OrderState::VALIDATED;
    uint64_t account_revision = 0;
    uint64_t control_epoch = 0;
    std::string policy_hash;
    std::string broker_order_id;
    uint32_t reason_mask = 0;
    std::string last_error;
};

struct OmsSubmitResult {
    bool accepted_for_reservation = false;
    bool duplicate_idempotent = false;
    aq_status_t abi_status = AQ_OK;
    aq_risk_decision_t decision{};
    OmsOrderRecord order;
};

struct DispatchOutcome {
    OrderState remote_state = OrderState::DISPATCHED_UNKNOWN;
    std::string broker_order_id;
    int64_t filled_qty = 0;
    int64_t avg_fill_price_cents = 0;
    int64_t fee_cents = 0;
    std::string detail;
};

using RemoteDispatchFn = std::function<DispatchOutcome(const OmsOrderRecord&)>;

struct ControlState {
    bool halted = false;
    uint64_t control_epoch = 1;
    std::string reason;
};

struct HaltResult {
    bool halted = true;
    uint64_t control_epoch = 0;
    size_t in_flight_unknown_count = 0;
    std::string note;
};

class NativeOms {
public:
    explicit NativeOms(const std::string& db_path);
    ~NativeOms();

    NativeOms(const NativeOms&) = delete;
    NativeOms& operator=(const NativeOms&) = delete;

    void init_account(const std::string& env, int64_t cash_cents, int64_t equity_cents);
    void upsert_position(const std::string& env, const std::string& symbol, int64_t qty, int64_t mark_cents);
    void set_account_health(const std::string& env, bool integrity_ok, int64_t daily_pnl_bps, int64_t total_drawdown_bps);

    // Single paper-execution writer lease fencing (C5).
    void acquire_writer_lease(const std::string& env, const std::string& owner_id, uint64_t authority_epoch);
    void revoke_writer_lease(const std::string& env);
    bool has_active_writer_lease(const std::string& env, const std::string& owner_id, uint64_t authority_epoch) const;

    ControlState control_state() const;
    HaltResult halt(const std::string& env, const std::string& reason);
    ControlState resume(const std::string& reason);

    OmsAccountView account_snapshot(const std::string& env) const;
    std::vector<OmsOrderRecord> list_orders(const std::string& env) const;
    OmsOrderRecord get_order(const std::string& client_order_id) const;

    // Transactional reservation (B2 + C2). Duplicate client_order_id returns existing record.
    OmsSubmitResult submit_intent(
        const std::string& env,
        const std::string& client_order_id,
        const aq_instrument_spec_t& spec,
        const aq_order_intent_t& intent,
        const aq_risk_policy_t& policy,
        int64_t fee_allowance_bps = 25);

    // Sole dispatch boundary: revalidates lease, halt, epoch, policy_hash, persists DISPATCHED_UNKNOWN before transport.
    OmsOrderRecord dispatch_reserved(
        const std::string& env,
        const std::string& client_order_id,
        const std::string& writer_owner_id,
        uint64_t writer_epoch,
        const aq_risk_policy_t& current_policy,
        const RemoteDispatchFn& transport,
        bool simulate_crash_before_transport = false);

    // Reconcile DISPATCHED_UNKNOWN / ACCEPTED / PARTIAL / CANCEL_PENDING by client_order_id.
    OmsOrderRecord reconcile_order(const std::string& client_order_id, const DispatchOutcome& outcome);
    OmsOrderRecord request_cancel(const std::string& client_order_id);

    // Startup crash recovery: returns orders still in PENDING_DISPATCH or DISPATCHED_UNKNOWN.
    std::vector<OmsOrderRecord> recover_in_flight(const std::string& env) const;

    static std::string hash_policy(const aq_risk_policy_t& policy);

private:
    struct Impl;
    Impl* impl_ = nullptr;
};

}  // namespace aq
