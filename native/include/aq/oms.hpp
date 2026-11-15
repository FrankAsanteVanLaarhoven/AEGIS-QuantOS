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
    std::string account_id = "default";
    std::string env;
    std::string symbol;
    uint8_t side = AQ_SIDE_BUY;
    uint8_t order_type = AQ_ORDER_LIMIT;
    std::string time_in_force = "DAY";
    int64_t qty = 0;
    int64_t filled_qty = 0;
    int64_t limit_price_cents = 0;
    int64_t stop_cents = 0;
    int64_t target_cents = 0;
    int64_t fee_allowance_bps = 25;
    int64_t reserved_cash_cents = 0;
    int64_t reserved_sell_qty = 0;
    int64_t cumulative_executed_notional_cents = 0;
    int64_t cumulative_fees_cents = 0;
    OrderState state = OrderState::VALIDATED;
    uint64_t account_revision = 0;
    uint64_t control_epoch = 0;
    std::string policy_hash;
    std::string request_hash;
    std::string broker_order_id;
    uint32_t reason_mask = 0;
    std::string last_error;
    bool legacy_unverified = false;
};

struct OmsSubmitResult {
    bool accepted_for_reservation = false;
    bool duplicate_idempotent = false;
    bool conflict = false;  // R2: same client_order_id with different canonical request_hash
    aq_status_t abi_status = AQ_OK;
    aq_risk_decision_t decision{};
    OmsOrderRecord order;
};

struct DispatchOutcome {
    OrderState remote_state = OrderState::DISPATCHED_UNKNOWN;
    std::string broker_order_id;
    std::string exec_id;             // Unique execution ID for exactly-once settlement (optional if cumulative)
    bool is_cumulative = true;       // true = filled_qty & avg_fill_price_cents are cumulative; false = incremental
    bool is_bust = false;            // true = trade bust/correction
    int64_t filled_qty = 0;          // cumulative filled_qty (if is_cumulative) or incremental qty (if !is_cumulative)
    int64_t avg_fill_price_cents = 0;
    int64_t fee_cents = 0;           // incremental fee for this event
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

struct WriterLeaseInfo {
    std::string env;
    std::string owner_id;
    uint64_t authority_epoch = 0;
    bool active = false;
};

class NativeOms {
public:
    explicit NativeOms(const std::string& db_path);
    ~NativeOms();

    NativeOms(const NativeOms&) = delete;
    NativeOms& operator=(const NativeOms&) = delete;

    // R1: Non-destructive create-if-absent seed. Never overwrites existing durable balances.
    void seed_account_if_absent(const std::string& env, int64_t cash_cents, int64_t equity_cents);
    // R1: Explicit audited reset (distinct from startup).
    void reset_account(const std::string& env, int64_t cash_cents, int64_t equity_cents, const std::string& scope_note);
    void upsert_position(const std::string& env, const std::string& symbol, int64_t qty, int64_t mark_cents);
    void set_account_health(const std::string& env, bool integrity_ok, int64_t daily_pnl_bps, int64_t total_drawdown_bps);
    bool verify_accounting_consistency(const std::string& env, std::string* out_error = nullptr) const;

    // R4: Monotonic writer lease fencing with compare-and-swap (no unconditional overwrite).
    bool try_acquire_writer_lease(const std::string& env, const std::string& owner_id, uint64_t expected_prev_epoch, uint64_t new_epoch);
    bool release_writer_lease(const std::string& env, const std::string& owner_id, uint64_t current_epoch);
    bool has_active_writer_lease(const std::string& env, const std::string& owner_id, uint64_t authority_epoch) const;
    WriterLeaseInfo writer_lease_info(const std::string& env) const;

    ControlState control_state() const;
    HaltResult halt(const std::string& env, const std::string& reason);
    ControlState resume(const std::string& reason);

    OmsAccountView account_snapshot(const std::string& env) const;
    std::vector<OmsOrderRecord> list_orders(const std::string& env) const;
    OmsOrderRecord get_order(const std::string& client_order_id) const;

    // R2: Canonical field-serialized request hash.
    static std::string canonical_request_hash(
        const std::string& account_id,
        const std::string& env,
        const aq_order_intent_t& intent,
        const std::string& time_in_force);

    // Transactional reservation (B2 + C2 + R2). Scoped to (account_id, env, client_order_id).
    OmsSubmitResult submit_intent(
        const std::string& env,
        const std::string& client_order_id,
        const aq_instrument_spec_t& spec,
        const aq_order_intent_t& intent,
        const aq_risk_policy_t& policy,
        int64_t fee_allowance_bps = 25,
        const std::string& account_id = "default",
        const std::string& time_in_force = "DAY");

    // Sole dispatch boundary: revalidates lease, halt, epoch, policy_hash, persists DISPATCHED_UNKNOWN before transport.
    OmsOrderRecord dispatch_reserved(
        const std::string& env,
        const std::string& client_order_id,
        const std::string& writer_owner_id,
        uint64_t writer_epoch,
        const aq_risk_policy_t& current_policy,
        const RemoteDispatchFn& transport,
        bool simulate_crash_before_transport = false);

    // R3: Normalized execution accounting with direct remaining reserve, exec_id dedup, cancel+fill, late fill/bust handling.
    OmsOrderRecord reconcile_order(const std::string& client_order_id, const DispatchOutcome& outcome);
    OmsOrderRecord request_cancel(const std::string& client_order_id);

    // Startup crash recovery: returns orders still in PENDING_DISPATCH, DISPATCHED_UNKNOWN, or CANCEL_PENDING.
    std::vector<OmsOrderRecord> recover_in_flight(const std::string& env) const;

    static std::string hash_policy(const aq_risk_policy_t& policy);

    static constexpr int kCurrentSchemaVersion = 2;
    struct SchemaMeta {
        int schema_version = 0;
        std::string code_sha;
        std::string model_format;
        std::string backup_hash;
        std::string updated_at;
    };
    struct BackupManifest {
        std::string backup_db_path;
        std::string manifest_path;
        std::string code_sha;
        int schema_version = 0;
        std::string model_format;
        std::string backup_hash;
    };
    SchemaMeta schema_meta() const;
    BackupManifest create_backup_manifest(
        const std::string& out_dir,
        const std::string& code_sha,
        const std::string& model_format = "aq_ckpt_v1") const;
    static void restore_from_backup_manifest(
        const std::string& manifest_path,
        const std::string& target_db_path);

private:
    struct Impl;
    Impl* impl_ = nullptr;
};

}  // namespace aq
