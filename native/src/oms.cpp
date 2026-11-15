#include "aq/oms.hpp"
#include "aq/json.hpp"

#include <sqlite3.h>

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <map>
#include <mutex>
#include <set>
#include <sstream>
#include <stdexcept>

namespace aq {

namespace {

std::string now_utc_iso() {
    auto now = std::chrono::system_clock::now();
    std::time_t t = std::chrono::system_clock::to_time_t(now);
    std::tm tm_utc{};
    gmtime_r(&t, &tm_utc);
    char buf[80]{};
    std::snprintf(
        buf, sizeof(buf), "%04d-%02d-%02dT%02d:%02d:%02dZ",
        tm_utc.tm_year + 1900, tm_utc.tm_mon + 1, tm_utc.tm_mday,
        tm_utc.tm_hour, tm_utc.tm_min, tm_utc.tm_sec);
    return std::string(buf);
}

void exec_sql(sqlite3* db, const char* sql) {
    char* err = nullptr;
    int rc = sqlite3_exec(db, sql, nullptr, nullptr, &err);
    if (rc != SQLITE_OK) {
        std::string msg = err ? err : "sqlite error";
        sqlite3_free(err);
        throw std::runtime_error(msg);
    }
}

struct TxGuard {
    sqlite3* db = nullptr;
    bool active = false;
    explicit TxGuard(sqlite3* d) : db(d) {
        exec_sql(db, "BEGIN IMMEDIATE;");
        active = true;
    }
    void commit() {
        if (active) {
            exec_sql(db, "COMMIT;");
            active = false;
        }
    }
    ~TxGuard() {
        if (active && db) {
            sqlite3_exec(db, "ROLLBACK;", nullptr, nullptr, nullptr);
        }
    }
};

struct StmtGuard {
    sqlite3_stmt* stmt = nullptr;
    explicit StmtGuard(sqlite3* db, const char* sql) {
        if (sqlite3_prepare_v2(db, sql, -1, &stmt, nullptr) != SQLITE_OK) {
            throw std::runtime_error(sqlite3_errmsg(db));
        }
    }
    ~StmtGuard() {
        if (stmt) sqlite3_finalize(stmt);
    }
};

bool is_active_reservation_state(OrderState st) {
    return st == OrderState::RESERVED ||
           st == OrderState::PENDING_DISPATCH ||
           st == OrderState::DISPATCHED_UNKNOWN ||
           st == OrderState::ACCEPTED ||
           st == OrderState::PARTIAL ||
           st == OrderState::CANCEL_PENDING;
}

bool is_terminal_state(OrderState st) {
    return st == OrderState::FILLED ||
           st == OrderState::REJECTED ||
           st == OrderState::CANCELLED ||
           st == OrderState::EXPIRED;
}

std::string col_str_or_empty(sqlite3_stmt* s, int col) {
    if (sqlite3_column_type(s, col) == SQLITE_NULL) return "";
    const unsigned char* p = sqlite3_column_text(s, col);
    int n = sqlite3_column_bytes(s, col);
    return p ? std::string(reinterpret_cast<const char*>(p), static_cast<size_t>(n)) : "";
}

int64_t col_i64_or_zero(sqlite3_stmt* s, int col) {
    return (sqlite3_column_type(s, col) == SQLITE_NULL) ? 0 : sqlite3_column_int64(s, col);
}

OmsOrderRecord row_to_order(sqlite3_stmt* s) {
    OmsOrderRecord r;
    r.client_order_id = col_str_or_empty(s, 0);
    r.account_id = col_str_or_empty(s, 1);
    r.env = col_str_or_empty(s, 2);
    r.symbol = col_str_or_empty(s, 3);
    r.side = static_cast<uint8_t>(sqlite3_column_int(s, 4));
    r.order_type = static_cast<uint8_t>(sqlite3_column_int(s, 5));
    r.time_in_force = col_str_or_empty(s, 6);
    r.qty = sqlite3_column_int64(s, 7);
    r.filled_qty = sqlite3_column_int64(s, 8);
    r.limit_price_cents = sqlite3_column_int64(s, 9);
    r.stop_cents = col_i64_or_zero(s, 10);
    r.target_cents = col_i64_or_zero(s, 11);
    r.fee_allowance_bps = col_i64_or_zero(s, 12);
    r.reserved_cash_cents = sqlite3_column_int64(s, 13);
    r.reserved_sell_qty = sqlite3_column_int64(s, 14);
    r.cumulative_executed_notional_cents = col_i64_or_zero(s, 15);
    r.cumulative_fees_cents = col_i64_or_zero(s, 16);
    r.state = order_state_from_str(col_str_or_empty(s, 17));
    r.account_revision = static_cast<uint64_t>(sqlite3_column_int64(s, 18));
    r.control_epoch = static_cast<uint64_t>(sqlite3_column_int64(s, 19));
    r.policy_hash = col_str_or_empty(s, 20);
    r.request_hash = col_str_or_empty(s, 21);
    r.broker_order_id = col_str_or_empty(s, 22);
    r.reason_mask = static_cast<uint32_t>(sqlite3_column_int64(s, 23));
    r.last_error = col_str_or_empty(s, 24);
    r.legacy_unverified = (sqlite3_column_int(s, 25) != 0);
    return r;
}

const char* kSelectOrderCols =
    "SELECT client_order_id, account_id, env, symbol, side, order_type, time_in_force,"
    " qty, filled_qty, limit_price_cents, stop_cents, target_cents, fee_allowance_bps,"
    " reserved_cash_cents, reserved_sell_qty, cumulative_executed_notional_cents,"
    " cumulative_fees_cents, state, account_revision, control_epoch, policy_hash,"
    " request_hash, broker_order_id, reason_mask, last_error, legacy_unverified FROM aq_oms_orders ";

int64_t compute_buy_reserve(int64_t rem_qty, int64_t limit_px, int64_t fee_bps) {
    if (rem_qty <= 0) return 0;
    int64_t principal = 0;
    int64_t fee_num = 0;
    int64_t total = 0;
    if (aq_checked_mul_i64(rem_qty, limit_px, &principal) != AQ_OK ||
        aq_checked_mul_i64(principal, fee_bps, &fee_num) != AQ_OK ||
        aq_checked_add_i64(principal, (fee_num + 9999) / 10000, &total) != AQ_OK) {
        throw std::overflow_error("compute_buy_reserve overflow");
    }
    return total;
}

}  // namespace

std::string order_state_to_str(OrderState st) {
    switch (st) {
        case OrderState::VALIDATED: return "VALIDATED";
        case OrderState::RESERVED: return "RESERVED";
        case OrderState::PENDING_DISPATCH: return "PENDING_DISPATCH";
        case OrderState::DISPATCHED_UNKNOWN: return "DISPATCHED_UNKNOWN";
        case OrderState::ACCEPTED: return "ACCEPTED";
        case OrderState::PARTIAL: return "PARTIAL";
        case OrderState::FILLED: return "FILLED";
        case OrderState::REJECTED: return "REJECTED";
        case OrderState::CANCEL_PENDING: return "CANCEL_PENDING";
        case OrderState::CANCELLED: return "CANCELLED";
        case OrderState::EXPIRED: return "EXPIRED";
    }
    return "UNKNOWN";
}

OrderState order_state_from_str(const std::string& s) {
    if (s == "VALIDATED") return OrderState::VALIDATED;
    if (s == "RESERVED") return OrderState::RESERVED;
    if (s == "PENDING_DISPATCH") return OrderState::PENDING_DISPATCH;
    if (s == "DISPATCHED_UNKNOWN") return OrderState::DISPATCHED_UNKNOWN;
    if (s == "ACCEPTED") return OrderState::ACCEPTED;
    if (s == "PARTIAL") return OrderState::PARTIAL;
    if (s == "FILLED") return OrderState::FILLED;
    if (s == "REJECTED") return OrderState::REJECTED;
    if (s == "CANCEL_PENDING") return OrderState::CANCEL_PENDING;
    if (s == "CANCELLED") return OrderState::CANCELLED;
    if (s == "EXPIRED") return OrderState::EXPIRED;
    throw std::runtime_error("Unknown OrderState: " + s);
}

struct NativeOms::Impl {
    sqlite3* db = nullptr;
    std::string db_path;
    mutable std::mutex mu;

    static bool table_exists(sqlite3* conn, const char* name) {
        StmtGuard st(conn, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?1;");
        sqlite3_bind_text(st.stmt, 1, name, -1, SQLITE_STATIC);
        return sqlite3_step(st.stmt) == SQLITE_ROW;
    }

    static std::set<std::string> table_columns(sqlite3* conn, const char* name) {
        std::set<std::string> cols;
        std::string sql = std::string("PRAGMA table_info(") + name + ");";
        StmtGuard st(conn, sql.c_str());
        while (sqlite3_step(st.stmt) == SQLITE_ROW) {
            const unsigned char* c = sqlite3_column_text(st.stmt, 1);
            if (c) cols.insert(reinterpret_cast<const char*>(c));
        }
        return cols;
    }

    static int read_user_version(sqlite3* conn) {
        StmtGuard st(conn, "PRAGMA user_version;");
        if (sqlite3_step(st.stmt) == SQLITE_ROW) {
            return sqlite3_column_int(st.stmt, 0);
        }
        return 0;
    }

    void validate_schema_before_mutation(bool& orders_exists, std::set<std::string>& ord_cols) {
        // Pre-mutation validation: reject unsupported schema versions BEFORE writing anything (even WAL header).
        int uv = read_user_version(db);
        if (uv > NativeOms::kCurrentSchemaVersion) {
            throw std::runtime_error("Unsupported database user_version: " + std::to_string(uv));
        }
        if (table_exists(db, "aq_schema_meta")) {
            StmtGuard st(db, "SELECT schema_version FROM aq_schema_meta WHERE id=1;");
            if (sqlite3_step(st.stmt) == SQLITE_ROW) {
                int sv = sqlite3_column_int(st.stmt, 0);
                if (sv < 1 || sv > NativeOms::kCurrentSchemaVersion) {
                    throw std::runtime_error("Unsupported aq_schema_meta.schema_version: " + std::to_string(sv));
                }
            }
        }

        orders_exists = table_exists(db, "aq_oms_orders");
        ord_cols.clear();
        if (orders_exists) {
            ord_cols = table_columns(db, "aq_oms_orders");
            static const char* kRequiredV1Cols[] = {
                "client_order_id", "env", "symbol", "side", "order_type",
                "qty", "filled_qty", "limit_price_cents",
                "reserved_cash_cents", "reserved_sell_qty", "state",
                "account_revision", "control_epoch", "policy_hash",
                "broker_order_id", "reason_mask", "last_error"
            };
            for (const char* col : kRequiredV1Cols) {
                if (!ord_cols.count(col)) {
                    throw std::runtime_error(std::string("Incompatible legacy aq_oms_orders schema: missing ") + col);
                }
            }
            if (!table_exists(db, "aq_oms_account")) {
                throw std::runtime_error("Incompatible legacy schema: aq_oms_orders exists without aq_oms_account");
            }
        }
    }

    void init_or_migrate_schema(bool orders_exists, const std::set<std::string>& ord_cols) {

        TxGuard tx(db);
        exec_sql(db,
            "CREATE TABLE IF NOT EXISTS aq_oms_account ("
            "  env TEXT PRIMARY KEY,"
            "  cash_cents INTEGER NOT NULL,"
            "  equity_cents INTEGER NOT NULL,"
            "  account_revision INTEGER NOT NULL,"
            "  integrity_ok INTEGER NOT NULL,"
            "  daily_pnl_bps INTEGER NOT NULL,"
            "  total_drawdown_bps INTEGER NOT NULL,"
            "  updated_at TEXT NOT NULL"
            ");"
            "CREATE TABLE IF NOT EXISTS aq_oms_positions ("
            "  env TEXT NOT NULL,"
            "  symbol TEXT NOT NULL,"
            "  qty INTEGER NOT NULL,"
            "  mark_cents INTEGER NOT NULL,"
            "  PRIMARY KEY(env, symbol)"
            ");"
            "CREATE TABLE IF NOT EXISTS aq_oms_executions ("
            "  exec_id TEXT PRIMARY KEY,"
            "  client_order_id TEXT NOT NULL,"
            "  exec_qty INTEGER NOT NULL,"
            "  exec_price_cents INTEGER NOT NULL,"
            "  fee_cents INTEGER NOT NULL,"
            "  is_bust INTEGER NOT NULL,"
            "  created_at TEXT NOT NULL"
            ");"
            "CREATE TABLE IF NOT EXISTS aq_oms_transitions ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  client_order_id TEXT NOT NULL,"
            "  from_state TEXT NOT NULL,"
            "  to_state TEXT NOT NULL,"
            "  detail TEXT NOT NULL,"
            "  created_at TEXT NOT NULL"
            ");"
            "CREATE TABLE IF NOT EXISTS aq_oms_writer_lease ("
            "  env TEXT PRIMARY KEY,"
            "  owner_id TEXT NOT NULL,"
            "  authority_epoch INTEGER NOT NULL,"
            "  active INTEGER NOT NULL,"
            "  updated_at TEXT NOT NULL"
            ");"
            "CREATE TABLE IF NOT EXISTS native_control_state ("
            "  id INTEGER PRIMARY KEY CHECK (id = 1),"
            "  halted INTEGER NOT NULL,"
            "  halt_reason TEXT,"
            "  control_epoch INTEGER NOT NULL"
            ");"
            "INSERT OR IGNORE INTO native_control_state(id, halted, halt_reason, control_epoch) VALUES(1, 0, NULL, 1);"
            "CREATE TABLE IF NOT EXISTS aq_oms_control_meta ("
            "  id INTEGER PRIMARY KEY CHECK (id = 1),"
            "  reason TEXT NOT NULL,"
            "  updated_at TEXT NOT NULL"
            ");"
            "CREATE TABLE IF NOT EXISTS aq_oms_audit_log ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  env TEXT NOT NULL,"
            "  event_type TEXT NOT NULL,"
            "  detail TEXT NOT NULL,"
            "  created_at TEXT NOT NULL"
            ");"
            "CREATE TABLE IF NOT EXISTS aq_schema_meta ("
            "  id INTEGER PRIMARY KEY CHECK (id = 1),"
            "  schema_version INTEGER NOT NULL,"
            "  code_sha TEXT NOT NULL,"
            "  model_format TEXT NOT NULL,"
            "  backup_hash TEXT NOT NULL,"
            "  updated_at TEXT NOT NULL"
            ");");

        if (!orders_exists) {
            exec_sql(db,
                "CREATE TABLE aq_oms_orders ("
                "  client_order_id TEXT PRIMARY KEY,"
                "  account_id TEXT,"
                "  env TEXT NOT NULL,"
                "  symbol TEXT NOT NULL,"
                "  side INTEGER NOT NULL,"
                "  order_type INTEGER NOT NULL,"
                "  time_in_force TEXT,"
                "  qty INTEGER NOT NULL,"
                "  filled_qty INTEGER NOT NULL,"
                "  limit_price_cents INTEGER NOT NULL,"
                "  stop_cents INTEGER,"
                "  target_cents INTEGER,"
                "  fee_allowance_bps INTEGER,"
                "  reserved_cash_cents INTEGER NOT NULL,"
                "  reserved_sell_qty INTEGER NOT NULL,"
                "  cumulative_executed_notional_cents INTEGER,"
                "  cumulative_fees_cents INTEGER,"
                "  state TEXT NOT NULL,"
                "  account_revision INTEGER NOT NULL,"
                "  control_epoch INTEGER NOT NULL,"
                "  policy_hash TEXT NOT NULL,"
                "  request_hash TEXT,"
                "  broker_order_id TEXT NOT NULL,"
                "  reason_mask INTEGER NOT NULL,"
                "  last_error TEXT NOT NULL,"
                "  legacy_unverified INTEGER NOT NULL DEFAULT 0,"
                "  created_at TEXT NOT NULL,"
                "  updated_at TEXT NOT NULL"
                ");");
        } else {
            // Controlled v1 -> v2 migration: add missing columns as NULLable without fabricating values.
            bool migrated_from_v1 = !ord_cols.count("account_id") || !ord_cols.count("request_hash");
            struct ColDef { const char* name; const char* decl; };
            static const ColDef kV2Cols[] = {
                {"account_id", "TEXT"},
                {"time_in_force", "TEXT"},
                {"stop_cents", "INTEGER"},
                {"target_cents", "INTEGER"},
                {"fee_allowance_bps", "INTEGER"},
                {"cumulative_executed_notional_cents", "INTEGER"},
                {"cumulative_fees_cents", "INTEGER"},
                {"request_hash", "TEXT"},
                {"legacy_unverified", "INTEGER NOT NULL DEFAULT 0"}
            };
            for (const auto& c : kV2Cols) {
                if (!ord_cols.count(c.name)) {
                    std::string alter = std::string("ALTER TABLE aq_oms_orders ADD COLUMN ") + c.name + " " + c.decl + ";";
                    exec_sql(db, alter.c_str());
                }
            }
            if (migrated_from_v1) {
                exec_sql(db,
                    "UPDATE aq_oms_orders SET legacy_unverified=1 "
                    "WHERE account_id IS NULL OR request_hash IS NULL;");
                exec_sql(db,
                    "UPDATE aq_oms_orders SET last_error='LEGACY_UNVERIFIED_REQUIRES_RECONCILIATION' "
                    "WHERE legacy_unverified=1 AND (last_error='' OR last_error IS NULL) "
                    "AND state IN ('PARTIAL','DISPATCHED_UNKNOWN','RESERVED','ACCEPTED','CANCEL_PENDING');");
                // Quarantine any legacy account whose orders violate basic invariants.
                StmtGuard bad_chk(db,
                    "SELECT DISTINCT env FROM aq_oms_orders "
                    "WHERE qty <= 0 OR filled_qty < 0 OR filled_qty > qty OR reserved_cash_cents < 0 OR reserved_sell_qty < 0;");
                bool found_corrupt = false;
                while (sqlite3_step(bad_chk.stmt) == SQLITE_ROW) {
                    found_corrupt = true;
                    std::string bad_env = col_str_or_empty(bad_chk.stmt, 0);
                    StmtGuard q(db, "UPDATE aq_oms_account SET integrity_ok=0, account_revision=account_revision+1 WHERE env=?1;");
                    sqlite3_bind_text(q.stmt, 1, bad_env.c_str(), static_cast<int>(bad_env.size()), SQLITE_TRANSIENT);
                    if (sqlite3_step(q.stmt) != SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(db));
                    record_audit(bad_env, "LEGACY_CORRUPT_STATE_QUARANTINED", "Corrupt legacy order invariants detected during v1->v2 migration");
                }
                if (found_corrupt) {
                    set_halt_in_tx(true, "LEGACY_CORRUPT_STATE_QUARANTINED");
                }
                record_audit("system", "LEGACY_V1_TO_V2_MIGRATED", "Migrated pre-closure schema to v2 without fabricating missing fields");
            }
        }

        std::string ts = now_utc_iso();
        StmtGuard sm(db,
            "INSERT INTO aq_schema_meta(id, schema_version, code_sha, model_format, backup_hash, updated_at) "
            "VALUES(1, ?1, COALESCE((SELECT code_sha FROM aq_schema_meta WHERE id=1), ''), "
            "COALESCE((SELECT model_format FROM aq_schema_meta WHERE id=1), 'aq_ckpt_v1'), "
            "COALESCE((SELECT backup_hash FROM aq_schema_meta WHERE id=1), ''), ?2) "
            "ON CONFLICT(id) DO UPDATE SET schema_version=excluded.schema_version, updated_at=excluded.updated_at;");
        sqlite3_bind_int(sm.stmt, 1, NativeOms::kCurrentSchemaVersion);
        sqlite3_bind_text(sm.stmt, 2, ts.c_str(), static_cast<int>(ts.size()), SQLITE_TRANSIENT);
        if (sqlite3_step(sm.stmt) != SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(db));

        exec_sql(db, "PRAGMA user_version = 2;");
        tx.commit();
    }

    explicit Impl(const std::string& path) : db_path(path) {
        if (sqlite3_open(path.c_str(), &db) != SQLITE_OK) {
            std::string err = db ? sqlite3_errmsg(db) : "open failed";
            if (db) { sqlite3_close(db); db = nullptr; }
            throw std::runtime_error("NativeOms sqlite open failed: " + err);
        }
        try {
            sqlite3_busy_timeout(db, 5000);
            bool orders_exists = false;
            std::set<std::string> ord_cols;
            validate_schema_before_mutation(orders_exists, ord_cols);
            exec_sql(db, "PRAGMA journal_mode=WAL;");
            exec_sql(db, "PRAGMA synchronous=FULL;");
            init_or_migrate_schema(orders_exists, ord_cols);
        } catch (...) {
            if (db) { sqlite3_close(db); db = nullptr; }
            throw;
        }
    }

    ~Impl() {
        if (db) sqlite3_close(db);
    }

    void record_transition(const std::string& cid, const std::string& from_st, const std::string& to_st, const std::string& detail) {
        StmtGuard st(db, "INSERT INTO aq_oms_transitions(client_order_id, from_state, to_state, detail, created_at) VALUES(?1,?2,?3,?4,?5);");
        sqlite3_bind_text(st.stmt, 1, cid.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(st.stmt, 2, from_st.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(st.stmt, 3, to_st.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(st.stmt, 4, detail.c_str(), -1, SQLITE_TRANSIENT);
        std::string ts = now_utc_iso();
        sqlite3_bind_text(st.stmt, 5, ts.c_str(), -1, SQLITE_TRANSIENT);
        if (sqlite3_step(st.stmt) != SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(db));
    }

    void record_audit(const std::string& env, const std::string& ev_type, const std::string& detail) {
        StmtGuard st(db, "INSERT INTO aq_oms_audit_log(env, event_type, detail, created_at) VALUES(?1,?2,?3,?4);");
        sqlite3_bind_text(st.stmt, 1, env.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(st.stmt, 2, ev_type.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(st.stmt, 3, detail.c_str(), -1, SQLITE_TRANSIENT);
        std::string ts = now_utc_iso();
        sqlite3_bind_text(st.stmt, 4, ts.c_str(), -1, SQLITE_TRANSIENT);
        if (sqlite3_step(st.stmt) != SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(db));
    }

    void save_control_reason(const std::string& reason) {
        StmtGuard st(db,
            "INSERT INTO aq_oms_control_meta(id, reason, updated_at) VALUES(1,?1,?2) "
            "ON CONFLICT(id) DO UPDATE SET reason=excluded.reason, updated_at=excluded.updated_at;");
        sqlite3_bind_text(st.stmt, 1, reason.c_str(), -1, SQLITE_TRANSIENT);
        std::string ts = now_utc_iso();
        sqlite3_bind_text(st.stmt, 2, ts.c_str(), -1, SQLITE_TRANSIENT);
        if (sqlite3_step(st.stmt) != SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(db));
    }

    std::string load_control_reason() const {
        StmtGuard st(db, "SELECT reason FROM aq_oms_control_meta WHERE id=1;");
        if (sqlite3_step(st.stmt) == SQLITE_ROW) {
            const unsigned char* r = sqlite3_column_text(st.stmt, 0);
            return r ? reinterpret_cast<const char*>(r) : "";
        }
        return "";
    }

    uint64_t set_halt_in_tx(bool enabled, const std::string& reason) {
        StmtGuard upd(db,
            "UPDATE native_control_state SET halted=?1, halt_reason=?2, control_epoch=control_epoch+1 WHERE id=1;");
        sqlite3_bind_int(upd.stmt, 1, enabled ? 1 : 0);
        sqlite3_bind_text(upd.stmt, 2, reason.c_str(), static_cast<int>(reason.size()), SQLITE_TRANSIENT);
        if (sqlite3_step(upd.stmt) != SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(db));
        save_control_reason(reason);
        StmtGuard sel(db, "SELECT control_epoch FROM native_control_state WHERE id=1;");
        if (sqlite3_step(sel.stmt) != SQLITE_ROW) throw std::runtime_error("native_control_state missing");
        return static_cast<uint64_t>(sqlite3_column_int64(sel.stmt, 0));
    }

    ControlState get_control_state() const {
        StmtGuard st(db, "SELECT halted, control_epoch, halt_reason FROM native_control_state WHERE id=1;");
        bool halted = true;
        uint64_t ep = 1;
        std::string reason = load_control_reason();
        if (sqlite3_step(st.stmt) == SQLITE_ROW) {
            halted = (sqlite3_column_int(st.stmt, 0) != 0);
            ep = static_cast<uint64_t>(sqlite3_column_int64(st.stmt, 1));
            if (reason.empty()) reason = col_str_or_empty(st.stmt, 2);
        }
        return ControlState{halted, ep, reason};
    }

    OmsOrderRecord record_conflict_and_halt_in_tx(
        const OmsOrderRecord& cur,
        const DispatchOutcome& outcome,
        const std::string& reason) {
        std::string ts = now_utc_iso();
        set_halt_in_tx(true, reason);
        {
            StmtGuard q(db,
                "UPDATE aq_oms_account SET integrity_ok=0, account_revision=account_revision+1, updated_at=?2 WHERE env=?1;");
            sqlite3_bind_text(q.stmt, 1, cur.env.c_str(), static_cast<int>(cur.env.size()), SQLITE_TRANSIENT);
            sqlite3_bind_text(q.stmt, 2, ts.c_str(), static_cast<int>(ts.size()), SQLITE_TRANSIENT);
            if (sqlite3_step(q.stmt) != SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(db));
        }
        if (!outcome.exec_id.empty()) {
            StmtGuard ex(db,
                "INSERT OR IGNORE INTO aq_oms_executions(exec_id, client_order_id, exec_qty, exec_price_cents, fee_cents, is_bust, created_at) "
                "VALUES(?1,?2,?3,?4,?5,1,?6);");
            sqlite3_bind_text(ex.stmt, 1, outcome.exec_id.c_str(), static_cast<int>(outcome.exec_id.size()), SQLITE_TRANSIENT);
            sqlite3_bind_text(ex.stmt, 2, cur.client_order_id.c_str(), static_cast<int>(cur.client_order_id.size()), SQLITE_TRANSIENT);
            sqlite3_bind_int64(ex.stmt, 3, outcome.filled_qty);
            sqlite3_bind_int64(ex.stmt, 4, outcome.avg_fill_price_cents);
            sqlite3_bind_int64(ex.stmt, 5, outcome.fee_cents);
            sqlite3_bind_text(ex.stmt, 6, ts.c_str(), static_cast<int>(ts.size()), SQLITE_TRANSIENT);
            if (sqlite3_step(ex.stmt) != SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(db));
        }
        {
            StmtGuard upd(db, "UPDATE aq_oms_orders SET last_error=?2, updated_at=?3 WHERE client_order_id=?1;");
            sqlite3_bind_text(upd.stmt, 1, cur.client_order_id.c_str(), static_cast<int>(cur.client_order_id.size()), SQLITE_TRANSIENT);
            sqlite3_bind_text(upd.stmt, 2, reason.c_str(), static_cast<int>(reason.size()), SQLITE_TRANSIENT);
            sqlite3_bind_text(upd.stmt, 3, ts.c_str(), static_cast<int>(ts.size()), SQLITE_TRANSIENT);
            if (sqlite3_step(upd.stmt) != SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(db));
        }
        record_audit(cur.env, "EXECUTION_CONFLICT", "cid=" + cur.client_order_id + ";reason=" + reason + ";detail=" + outcome.detail);
        std::string st_str = order_state_to_str(cur.state);
        record_transition(cur.client_order_id, st_str, st_str, reason);
        return load_order_unlocked(cur.client_order_id);
    }

    OmsAccountView load_account_unlocked(const std::string& env) const {
        OmsAccountView view;
        view.env = env;
        {
            StmtGuard st(db, "SELECT cash_cents, equity_cents, account_revision, integrity_ok, daily_pnl_bps, total_drawdown_bps FROM aq_oms_account WHERE env=?1;");
            sqlite3_bind_text(st.stmt, 1, env.c_str(), -1, SQLITE_TRANSIENT);
            if (sqlite3_step(st.stmt) != SQLITE_ROW) {
                throw std::runtime_error("OMS account not initialized for env: " + env);
            }
            view.cash_cents = sqlite3_column_int64(st.stmt, 0);
            view.equity_cents = sqlite3_column_int64(st.stmt, 1);
            view.account_revision = static_cast<uint64_t>(sqlite3_column_int64(st.stmt, 2));
            view.integrity_ok = (sqlite3_column_int(st.stmt, 3) != 0);
            view.daily_pnl_bps = sqlite3_column_int64(st.stmt, 4);
            view.total_drawdown_bps = sqlite3_column_int64(st.stmt, 5);
        }
        {
            StmtGuard st(db, "SELECT symbol, qty, mark_cents FROM aq_oms_positions WHERE env=?1 ORDER BY symbol;");
            sqlite3_bind_text(st.stmt, 1, env.c_str(), -1, SQLITE_TRANSIENT);
            while (sqlite3_step(st.stmt) == SQLITE_ROW) {
                aq_position_mark_t pm{};
                const char* sym = reinterpret_cast<const char*>(sqlite3_column_text(st.stmt, 0));
                std::snprintf(pm.symbol, sizeof(pm.symbol), "%s", sym ? sym : "");
                pm.qty = sqlite3_column_int64(st.stmt, 1);
                pm.mark_cents = sqlite3_column_int64(st.stmt, 2);
                view.positions.push_back(pm);
            }
        }
        {
            StmtGuard st(db,
                "SELECT symbol, side, reserved_cash_cents, qty - filled_qty, reserved_sell_qty, state "
                "FROM aq_oms_orders WHERE env=?1;");
            sqlite3_bind_text(st.stmt, 1, env.c_str(), -1, SQLITE_TRANSIENT);
            std::map<std::string, aq_symbol_reservation_t> by_sym;
            while (sqlite3_step(st.stmt) == SQLITE_ROW) {
                std::string st_str = reinterpret_cast<const char*>(sqlite3_column_text(st.stmt, 5));
                OrderState ost = order_state_from_str(st_str);
                if (!is_active_reservation_state(ost)) continue;
                std::string sym = reinterpret_cast<const char*>(sqlite3_column_text(st.stmt, 0));
                int side = sqlite3_column_int(st.stmt, 1);
                int64_t rcash = sqlite3_column_int64(st.stmt, 2);
                int64_t rem_qty = std::max<int64_t>(0, sqlite3_column_int64(st.stmt, 3));
                int64_t rsell = sqlite3_column_int64(st.stmt, 4);
                auto& entry = by_sym[sym];
                std::snprintf(entry.symbol, sizeof(entry.symbol), "%s", sym.c_str());
                if (side == AQ_SIDE_BUY) {
                    if (aq_checked_add_i64(entry.reserved_buy_cents, rcash, &entry.reserved_buy_cents) != AQ_OK ||
                        aq_checked_add_i64(entry.reserved_buy_qty, rem_qty, &entry.reserved_buy_qty) != AQ_OK ||
                        aq_checked_add_i64(view.total_reserved_buy_cents, rcash, &view.total_reserved_buy_cents) != AQ_OK) {
                        throw std::overflow_error("reservation aggregation overflow");
                    }
                } else {
                    if (aq_checked_add_i64(entry.reserved_sell_qty, rsell, &entry.reserved_sell_qty) != AQ_OK) {
                        throw std::overflow_error("sell reservation aggregation overflow");
                    }
                }
            }
            for (const auto& kv : by_sym) {
                view.reservations.push_back(kv.second);
            }
        }
        return view;
    }

    OmsOrderRecord load_order_unlocked(const std::string& cid) const {
        std::string sql = std::string(kSelectOrderCols) + "WHERE client_order_id=?1;";
        StmtGuard st(db, sql.c_str());
        sqlite3_bind_text(st.stmt, 1, cid.c_str(), -1, SQLITE_TRANSIENT);
        if (sqlite3_step(st.stmt) != SQLITE_ROW) {
            throw std::runtime_error("Order not found: " + cid);
        }
        return row_to_order(st.stmt);
    }

    bool find_order_by_scope_unlocked(const std::string& account_id, const std::string& env, const std::string& cid, OmsOrderRecord& out) const {
        std::string sql = std::string(kSelectOrderCols) + "WHERE client_order_id=?1 AND account_id=?2 AND env=?3;";
        StmtGuard st(db, sql.c_str());
        sqlite3_bind_text(st.stmt, 1, cid.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(st.stmt, 2, account_id.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(st.stmt, 3, env.c_str(), -1, SQLITE_TRANSIENT);
        if (sqlite3_step(st.stmt) != SQLITE_ROW) return false;
        out = row_to_order(st.stmt);
        return true;
    }

    bool check_lease_unlocked(const std::string& env, const std::string& owner_id, uint64_t epoch) const {
        StmtGuard st(db, "SELECT owner_id, authority_epoch, active FROM aq_oms_writer_lease WHERE env=?1;");
        sqlite3_bind_text(st.stmt, 1, env.c_str(), -1, SQLITE_TRANSIENT);
        if (sqlite3_step(st.stmt) != SQLITE_ROW) return false;
        std::string cur_owner = reinterpret_cast<const char*>(sqlite3_column_text(st.stmt, 0));
        uint64_t cur_epoch = static_cast<uint64_t>(sqlite3_column_int64(st.stmt, 1));
        int active = sqlite3_column_int(st.stmt, 2);
        return active == 1 && cur_owner == owner_id && cur_epoch == epoch;
    }
};

NativeOms::NativeOms(const std::string& db_path) : impl_(new Impl(db_path)) {}
NativeOms::~NativeOms() { delete impl_; }

std::string NativeOms::hash_policy(const aq_risk_policy_t& policy) {
    std::ostringstream oss;
    oss << policy.abi_version << "|" << policy.max_order_notional_bps << "|"
        << policy.max_concentration_bps << "|" << policy.max_gross_exposure_bps << "|"
        << policy.max_daily_loss_bps << "|" << policy.max_total_loss_bps << "|"
        << policy.max_data_age_ms << "|" << static_cast<int>(policy.no_short_selling) << "|"
        << static_cast<int>(policy.allow_protective_brackets);
    return sha256_hex(oss.str());
}

std::string NativeOms::canonical_request_hash(
    const std::string& account_id,
    const std::string& env,
    const aq_order_intent_t& intent,
    const std::string& time_in_force) {
    std::ostringstream oss;
    oss << account_id << "|" << env << "|" << intent.symbol << "|"
        << static_cast<int>(intent.side) << "|" << static_cast<int>(intent.order_type) << "|"
        << time_in_force << "|" << intent.qty << "|" << intent.price_cents << "|"
        << intent.stop_cents << "|" << intent.target_cents;
    return sha256_hex(oss.str());
}

void NativeOms::seed_account_if_absent(const std::string& env, int64_t cash_cents, int64_t equity_cents) {
    std::lock_guard<std::mutex> lk(impl_->mu);
    TxGuard tx(impl_->db);
    StmtGuard st(impl_->db,
        "INSERT OR IGNORE INTO aq_oms_account(env, cash_cents, equity_cents, account_revision, integrity_ok, daily_pnl_bps, total_drawdown_bps, updated_at) "
        "VALUES(?1,?2,?3,1,1,0,0,?4);");
    sqlite3_bind_text(st.stmt, 1, env.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_int64(st.stmt, 2, cash_cents);
    sqlite3_bind_int64(st.stmt, 3, equity_cents);
    std::string ts = now_utc_iso();
    sqlite3_bind_text(st.stmt, 4, ts.c_str(), -1, SQLITE_TRANSIENT);
    if (sqlite3_step(st.stmt) != SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(impl_->db));
    tx.commit();
}

void NativeOms::reset_account(const std::string& env, int64_t cash_cents, int64_t equity_cents, const std::string& scope_note) {
    if (scope_note.empty()) throw std::invalid_argument("reset_account requires explicit scope_note");
    std::lock_guard<std::mutex> lk(impl_->mu);
    TxGuard tx(impl_->db);
    OmsAccountView prev = impl_->load_account_unlocked(env);
    std::ostringstream snap;
    snap << "scope=" << scope_note << ";prev_cash=" << prev.cash_cents
         << ";prev_equity=" << prev.equity_cents << ";prev_rev=" << prev.account_revision;
    impl_->record_audit(env, "EXPLICIT_ACCOUNT_RESET", snap.str());

    StmtGuard del_pos(impl_->db, "DELETE FROM aq_oms_positions WHERE env=?1;");
    sqlite3_bind_text(del_pos.stmt, 1, env.c_str(), -1, SQLITE_TRANSIENT);
    if (sqlite3_step(del_pos.stmt) != SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(impl_->db));

    StmtGuard st(impl_->db,
        "UPDATE aq_oms_account SET cash_cents=?2, equity_cents=?3, account_revision=account_revision+1, "
        "integrity_ok=1, daily_pnl_bps=0, total_drawdown_bps=0, updated_at=?4 WHERE env=?1;");
    sqlite3_bind_text(st.stmt, 1, env.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_int64(st.stmt, 2, cash_cents);
    sqlite3_bind_int64(st.stmt, 3, equity_cents);
    std::string ts = now_utc_iso();
    sqlite3_bind_text(st.stmt, 4, ts.c_str(), -1, SQLITE_TRANSIENT);
    if (sqlite3_step(st.stmt) != SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(impl_->db));
    tx.commit();
}

void NativeOms::upsert_position(const std::string& env, const std::string& symbol, int64_t qty, int64_t mark_cents) {
    std::lock_guard<std::mutex> lk(impl_->mu);
    TxGuard tx(impl_->db);
    StmtGuard st(impl_->db,
        "INSERT INTO aq_oms_positions(env, symbol, qty, mark_cents) VALUES(?1,?2,?3,?4) "
        "ON CONFLICT(env, symbol) DO UPDATE SET qty=excluded.qty, mark_cents=excluded.mark_cents;");
    sqlite3_bind_text(st.stmt, 1, env.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(st.stmt, 2, symbol.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_int64(st.stmt, 3, qty);
    sqlite3_bind_int64(st.stmt, 4, mark_cents);
    if (sqlite3_step(st.stmt) != SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(impl_->db));
    tx.commit();
}

void NativeOms::set_account_health(const std::string& env, bool integrity_ok, int64_t daily_pnl_bps, int64_t total_drawdown_bps) {
    std::lock_guard<std::mutex> lk(impl_->mu);
    TxGuard tx(impl_->db);
    StmtGuard st(impl_->db,
        "UPDATE aq_oms_account SET integrity_ok=?2, daily_pnl_bps=?3, total_drawdown_bps=?4, account_revision=account_revision+1, updated_at=?5 WHERE env=?1;");
    sqlite3_bind_text(st.stmt, 1, env.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_int(st.stmt, 2, integrity_ok ? 1 : 0);
    sqlite3_bind_int64(st.stmt, 3, daily_pnl_bps);
    sqlite3_bind_int64(st.stmt, 4, total_drawdown_bps);
    std::string ts = now_utc_iso();
    sqlite3_bind_text(st.stmt, 5, ts.c_str(), -1, SQLITE_TRANSIENT);
    if (sqlite3_step(st.stmt) != SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(impl_->db));
    tx.commit();
}

bool NativeOms::verify_accounting_consistency(const std::string& env, std::string* out_error) const {
    std::lock_guard<std::mutex> lk(impl_->mu);
    try {
        OmsAccountView view = impl_->load_account_unlocked(env);
        if (view.cash_cents < 0) {
            if (out_error) *out_error = "Negative cash_cents";
            return false;
        }
        if (view.total_reserved_buy_cents > view.cash_cents) {
            if (out_error) *out_error = "Reserved buy cash exceeds cash_cents";
            return false;
        }
        StmtGuard st(impl_->db,
            "SELECT client_order_id, qty, filled_qty, reserved_cash_cents, state FROM aq_oms_orders WHERE env=?1;");
        sqlite3_bind_text(st.stmt, 1, env.c_str(), -1, SQLITE_TRANSIENT);
        while (sqlite3_step(st.stmt) == SQLITE_ROW) {
            int64_t qty = sqlite3_column_int64(st.stmt, 1);
            int64_t filled = sqlite3_column_int64(st.stmt, 2);
            int64_t rcash = sqlite3_column_int64(st.stmt, 3);
            OrderState ost = order_state_from_str(reinterpret_cast<const char*>(sqlite3_column_text(st.stmt, 4)));
            if (filled < 0 || filled > qty || rcash < 0) {
                if (out_error) *out_error = "Corrupt order fill/reserve bounds";
                return false;
            }
            if (is_terminal_state(ost) && rcash != 0) {
                if (out_error) *out_error = "Terminal order retains nonzero reservation";
                return false;
            }
        }
        return true;
    } catch (const std::exception& e) {
        if (out_error) *out_error = e.what();
        return false;
    }
}

bool NativeOms::try_acquire_writer_lease(const std::string& env, const std::string& owner_id, uint64_t expected_prev_epoch, uint64_t new_epoch) {
    if (owner_id.empty() || new_epoch == 0) return false;
    std::lock_guard<std::mutex> lk(impl_->mu);
    TxGuard tx(impl_->db);
    StmtGuard sel(impl_->db, "SELECT authority_epoch, active FROM aq_oms_writer_lease WHERE env=?1;");
    sqlite3_bind_text(sel.stmt, 1, env.c_str(), -1, SQLITE_TRANSIENT);
    std::string ts = now_utc_iso();
    if (sqlite3_step(sel.stmt) != SQLITE_ROW) {
        if (expected_prev_epoch != 0) return false;
        StmtGuard ins(impl_->db, "INSERT INTO aq_oms_writer_lease(env, owner_id, authority_epoch, active, updated_at) VALUES(?1,?2,?3,1,?4);");
        sqlite3_bind_text(ins.stmt, 1, env.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(ins.stmt, 2, owner_id.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_int64(ins.stmt, 3, static_cast<int64_t>(new_epoch));
        sqlite3_bind_text(ins.stmt, 4, ts.c_str(), -1, SQLITE_TRANSIENT);
        if (sqlite3_step(ins.stmt) != SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(impl_->db));
        tx.commit();
        return true;
    }
    uint64_t cur_epoch = static_cast<uint64_t>(sqlite3_column_int64(sel.stmt, 0));
    if (cur_epoch != expected_prev_epoch || new_epoch <= cur_epoch) {
        return false;
    }
    StmtGuard upd(impl_->db,
        "UPDATE aq_oms_writer_lease SET owner_id=?2, authority_epoch=?3, active=1, updated_at=?4 WHERE env=?1 AND authority_epoch=?5;");
    sqlite3_bind_text(upd.stmt, 1, env.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(upd.stmt, 2, owner_id.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_int64(upd.stmt, 3, static_cast<int64_t>(new_epoch));
    sqlite3_bind_text(upd.stmt, 4, ts.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_int64(upd.stmt, 5, static_cast<int64_t>(expected_prev_epoch));
    if (sqlite3_step(upd.stmt) != SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(impl_->db));
    bool changed = (sqlite3_changes(impl_->db) == 1);
    tx.commit();
    return changed;
}

bool NativeOms::release_writer_lease(const std::string& env, const std::string& owner_id, uint64_t current_epoch) {
    std::lock_guard<std::mutex> lk(impl_->mu);
    TxGuard tx(impl_->db);
    StmtGuard upd(impl_->db,
        "UPDATE aq_oms_writer_lease SET active=0, updated_at=?4 WHERE env=?1 AND owner_id=?2 AND authority_epoch=?3;");
    sqlite3_bind_text(upd.stmt, 1, env.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(upd.stmt, 2, owner_id.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_int64(upd.stmt, 3, static_cast<int64_t>(current_epoch));
    std::string ts = now_utc_iso();
    sqlite3_bind_text(upd.stmt, 4, ts.c_str(), -1, SQLITE_TRANSIENT);
    if (sqlite3_step(upd.stmt) != SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(impl_->db));
    bool changed = (sqlite3_changes(impl_->db) == 1);
    tx.commit();
    return changed;
}

bool NativeOms::has_active_writer_lease(const std::string& env, const std::string& owner_id, uint64_t authority_epoch) const {
    std::lock_guard<std::mutex> lk(impl_->mu);
    return impl_->check_lease_unlocked(env, owner_id, authority_epoch);
}

WriterLeaseInfo NativeOms::writer_lease_info(const std::string& env) const {
    std::lock_guard<std::mutex> lk(impl_->mu);
    WriterLeaseInfo info;
    info.env = env;
    StmtGuard st(impl_->db, "SELECT owner_id, authority_epoch, active FROM aq_oms_writer_lease WHERE env=?1;");
    sqlite3_bind_text(st.stmt, 1, env.c_str(), -1, SQLITE_TRANSIENT);
    if (sqlite3_step(st.stmt) == SQLITE_ROW) {
        info.owner_id = reinterpret_cast<const char*>(sqlite3_column_text(st.stmt, 0));
        info.authority_epoch = static_cast<uint64_t>(sqlite3_column_int64(st.stmt, 1));
        info.active = (sqlite3_column_int(st.stmt, 2) == 1);
    }
    return info;
}

ControlState NativeOms::control_state() const {
    std::lock_guard<std::mutex> lk(impl_->mu);
    return impl_->get_control_state();
}

HaltResult NativeOms::halt(const std::string& env, const std::string& reason) {
    std::lock_guard<std::mutex> lk(impl_->mu);
    TxGuard tx(impl_->db);
    uint64_t ep = impl_->set_halt_in_tx(true, reason);
    size_t unknown_cnt = 0;
    StmtGuard st(impl_->db, "SELECT COUNT(*) FROM aq_oms_orders WHERE env=?1 AND state='DISPATCHED_UNKNOWN';");
    sqlite3_bind_text(st.stmt, 1, env.c_str(), static_cast<int>(env.size()), SQLITE_TRANSIENT);
    if (sqlite3_step(st.stmt) == SQLITE_ROW) {
        unknown_cnt = static_cast<size_t>(sqlite3_column_int64(st.stmt, 0));
    }
    tx.commit();
    return HaltResult{
        true,
        ep,
        unknown_cnt,
        "Halt blocks new and pending dispatch; already-transmitted DISPATCHED_UNKNOWN orders cannot be guaranteed cancelled without broker acknowledgement."};
}

ControlState NativeOms::resume(const std::string& reason) {
    std::lock_guard<std::mutex> lk(impl_->mu);
    TxGuard tx(impl_->db);
    uint64_t ep = impl_->set_halt_in_tx(false, reason);
    tx.commit();
    return ControlState{false, ep, reason};
}

OmsAccountView NativeOms::account_snapshot(const std::string& env) const {
    std::lock_guard<std::mutex> lk(impl_->mu);
    return impl_->load_account_unlocked(env);
}

std::vector<OmsOrderRecord> NativeOms::list_orders(const std::string& env) const {
    std::lock_guard<std::mutex> lk(impl_->mu);
    std::string sql = std::string(kSelectOrderCols) + "WHERE env=?1 ORDER BY rowid DESC;";
    StmtGuard st(impl_->db, sql.c_str());
    sqlite3_bind_text(st.stmt, 1, env.c_str(), -1, SQLITE_TRANSIENT);
    std::vector<OmsOrderRecord> out;
    while (sqlite3_step(st.stmt) == SQLITE_ROW) {
        out.push_back(row_to_order(st.stmt));
    }
    return out;
}

OmsOrderRecord NativeOms::get_order(const std::string& client_order_id) const {
    std::lock_guard<std::mutex> lk(impl_->mu);
    return impl_->load_order_unlocked(client_order_id);
}

OmsSubmitResult NativeOms::submit_intent(
    const std::string& env,
    const std::string& client_order_id,
    const aq_instrument_spec_t& spec,
    const aq_order_intent_t& intent,
    const aq_risk_policy_t& policy,
    int64_t fee_allowance_bps,
    const std::string& account_id,
    const std::string& time_in_force) {
    if (client_order_id.empty()) throw std::invalid_argument("client_order_id required");
    if (has_embedded_nul(client_order_id) || has_embedded_nul(account_id) || has_embedded_nul(env)) {
        throw std::invalid_argument("Embedded NUL not permitted in order identifiers");
    }
    if (fee_allowance_bps < 0 || fee_allowance_bps > 1000) throw std::invalid_argument("fee_allowance_bps out of range");

    std::string req_hash = canonical_request_hash(account_id, env, intent, time_in_force);

    std::lock_guard<std::mutex> lk(impl_->mu);
    TxGuard tx(impl_->db);
    ControlState cs = impl_->get_control_state();

    OmsOrderRecord existing;
    if (impl_->find_order_by_scope_unlocked(account_id, env, client_order_id, existing)) {
        if (existing.request_hash != req_hash) {
            OmsSubmitResult conf;
            conf.conflict = true;
            conf.order = existing;
            tx.commit();
            return conf;
        }
        OmsSubmitResult dup;
        dup.accepted_for_reservation = is_active_reservation_state(existing.state) || existing.state == OrderState::FILLED;
        dup.duplicate_idempotent = true;
        dup.abi_status = AQ_OK;
        dup.decision.abi_version = AQ_CORE_ABI_VERSION;
        dup.decision.struct_size = sizeof(aq_risk_decision_t);
        dup.decision.approved = dup.accepted_for_reservation ? 1u : 0u;
        dup.decision.reason_mask = existing.reason_mask;
        dup.decision.account_revision = existing.account_revision;
        dup.decision.control_epoch = existing.control_epoch;
        dup.order = existing;
        tx.commit();
        return dup;
    }

    OmsAccountView view = impl_->load_account_unlocked(env);
    aq_account_snapshot_t acct{};
    acct.abi_version = AQ_CORE_ABI_VERSION;
    acct.struct_size = sizeof(aq_account_snapshot_t);
    acct.cash_cents = view.cash_cents;
    acct.equity_cents = view.equity_cents;
    acct.reserved_buy_cents = view.total_reserved_buy_cents;
    acct.daily_pnl_bps = view.daily_pnl_bps;
    acct.total_drawdown_bps = view.total_drawdown_bps;
    acct.account_revision = view.account_revision;
    acct.control_epoch = cs.control_epoch;
    acct.halted = cs.halted ? 1u : 0u;
    acct.integrity_ok = view.integrity_ok ? 1u : 0u;
    acct.position_count = static_cast<uint32_t>(view.positions.size());
    acct.positions = view.positions.empty() ? nullptr : view.positions.data();
    acct.reservation_count = static_cast<uint32_t>(view.reservations.size());
    acct.reservations = view.reservations.empty() ? nullptr : view.reservations.data();

    aq_risk_decision_t dec{};
    dec.abi_version = AQ_CORE_ABI_VERSION;
    dec.struct_size = sizeof(aq_risk_decision_t);
    aq_status_t st = aq_evaluate_risk(&spec, &acct, &policy, &intent, &dec);

    int64_t res_cash = 0;
    int64_t res_sell = 0;
    OrderState next_state = OrderState::REJECTED;
    if (st == AQ_OK && dec.approved) {
        if (intent.side == AQ_SIDE_BUY) {
            res_cash = compute_buy_reserve(intent.qty, intent.price_cents, fee_allowance_bps);
            int64_t avail_cash = 0;
            if (aq_checked_sub_i64(view.cash_cents, view.total_reserved_buy_cents, &avail_cash) != AQ_OK) {
                throw std::overflow_error("avail_cash overflow");
            }
            if (res_cash > avail_cash) {
                dec.approved = 0;
                dec.reason_mask |= AQ_REJ_INSUFFICIENT_CASH;
                res_cash = 0;
            } else {
                next_state = OrderState::RESERVED;
            }
        } else {
            res_sell = intent.qty;
            next_state = OrderState::RESERVED;
        }
    }

    std::string phash = hash_policy(policy);
    std::string ts = now_utc_iso();
    {
        StmtGuard ins(impl_->db,
            "INSERT INTO aq_oms_orders("
            "client_order_id, account_id, env, symbol, side, order_type, time_in_force,"
            "qty, filled_qty, limit_price_cents, stop_cents, target_cents, fee_allowance_bps,"
            "reserved_cash_cents, reserved_sell_qty, cumulative_executed_notional_cents,"
            "cumulative_fees_cents, state, account_revision, control_epoch,"
            "policy_hash, request_hash, broker_order_id, reason_mask, last_error, created_at, updated_at) "
            "VALUES(?1,?2,?3,?4,?5,?6,?7,?8,0,?9,?10,?11,?12,?13,?14,0,0,?15,?16,?17,?18,?19,'',?20,'',?21,?21);");
        sqlite3_bind_text(ins.stmt, 1, client_order_id.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(ins.stmt, 2, account_id.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(ins.stmt, 3, env.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(ins.stmt, 4, intent.symbol, -1, SQLITE_TRANSIENT);
        sqlite3_bind_int(ins.stmt, 5, intent.side);
        sqlite3_bind_int(ins.stmt, 6, intent.order_type);
        sqlite3_bind_text(ins.stmt, 7, time_in_force.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_int64(ins.stmt, 8, intent.qty);
        sqlite3_bind_int64(ins.stmt, 9, intent.price_cents);
        sqlite3_bind_int64(ins.stmt, 10, intent.stop_cents);
        sqlite3_bind_int64(ins.stmt, 11, intent.target_cents);
        sqlite3_bind_int64(ins.stmt, 12, fee_allowance_bps);
        sqlite3_bind_int64(ins.stmt, 13, res_cash);
        sqlite3_bind_int64(ins.stmt, 14, res_sell);
        std::string st_str = order_state_to_str(next_state);
        sqlite3_bind_text(ins.stmt, 15, st_str.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_int64(ins.stmt, 16, static_cast<int64_t>(view.account_revision));
        sqlite3_bind_int64(ins.stmt, 17, static_cast<int64_t>(cs.control_epoch));
        sqlite3_bind_text(ins.stmt, 18, phash.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(ins.stmt, 19, req_hash.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_int64(ins.stmt, 20, static_cast<int64_t>(dec.reason_mask));
        sqlite3_bind_text(ins.stmt, 21, ts.c_str(), -1, SQLITE_TRANSIENT);
        if (sqlite3_step(ins.stmt) != SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(impl_->db));
    }
    impl_->record_transition(client_order_id, "VALIDATED", order_state_to_str(next_state),
        next_state == OrderState::RESERVED ? "reserved_ok" : "rejected_by_risk");

    if (next_state == OrderState::RESERVED) {
        StmtGuard bump(impl_->db, "UPDATE aq_oms_account SET account_revision=account_revision+1, updated_at=?2 WHERE env=?1;");
        sqlite3_bind_text(bump.stmt, 1, env.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(bump.stmt, 2, ts.c_str(), -1, SQLITE_TRANSIENT);
        if (sqlite3_step(bump.stmt) != SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(impl_->db));
    }

    OmsSubmitResult out;
    out.accepted_for_reservation = (next_state == OrderState::RESERVED);
    out.duplicate_idempotent = false;
    out.abi_status = st;
    out.decision = dec;
    out.order = impl_->load_order_unlocked(client_order_id);
    tx.commit();
    return out;
}

OmsOrderRecord NativeOms::dispatch_reserved(
    const std::string& env,
    const std::string& client_order_id,
    const std::string& writer_owner_id,
    uint64_t writer_epoch,
    const aq_risk_policy_t& current_policy,
    const RemoteDispatchFn& transport,
    bool simulate_crash_before_transport) {
    std::string cur_phash = hash_policy(current_policy);
    OmsOrderRecord order_copy;

    {
        std::lock_guard<std::mutex> lk(impl_->mu);
        TxGuard tx(impl_->db);
        ControlState cs = impl_->get_control_state();
        if (!impl_->check_lease_unlocked(env, writer_owner_id, writer_epoch)) {
            throw std::runtime_error("DISPATCH_BLOCKED_NO_LEASE");
        }
        OmsAccountView acct = impl_->load_account_unlocked(env);
        order_copy = impl_->load_order_unlocked(client_order_id);
        if (order_copy.env != env) throw std::runtime_error("Order env mismatch");
        if (order_copy.state != OrderState::RESERVED) {
            throw std::runtime_error("Order not in RESERVED state: " + order_state_to_str(order_copy.state));
        }

        std::string reject_reason;
        if (cs.halted) reject_reason = "HALTED_BEFORE_DISPATCH";
        else if (!acct.integrity_ok) reject_reason = "ACCOUNT_QUARANTINED";
        else if (order_copy.legacy_unverified) reject_reason = "LEGACY_UNVERIFIED_REQUIRES_RECONCILIATION";
        else if (order_copy.control_epoch != cs.control_epoch) reject_reason = "CONTROL_EPOCH_STALE";
        else if (order_copy.policy_hash != cur_phash) reject_reason = "POLICY_HASH_STALE";

        std::string ts = now_utc_iso();
        if (!reject_reason.empty()) {
            StmtGuard upd(impl_->db,
                "UPDATE aq_oms_orders SET state='REJECTED', reserved_cash_cents=0, reserved_sell_qty=0, last_error=?2, updated_at=?3 WHERE client_order_id=?1;");
            sqlite3_bind_text(upd.stmt, 1, client_order_id.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_text(upd.stmt, 2, reject_reason.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_text(upd.stmt, 3, ts.c_str(), -1, SQLITE_TRANSIENT);
            if (sqlite3_step(upd.stmt) != SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(impl_->db));
            impl_->record_transition(client_order_id, "RESERVED", "REJECTED", reject_reason);
            order_copy = impl_->load_order_unlocked(client_order_id);
            tx.commit();
            return order_copy;
        }

        StmtGuard upd(impl_->db, "UPDATE aq_oms_orders SET state='DISPATCHED_UNKNOWN', updated_at=?2 WHERE client_order_id=?1;");
        sqlite3_bind_text(upd.stmt, 1, client_order_id.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(upd.stmt, 2, ts.c_str(), -1, SQLITE_TRANSIENT);
        if (sqlite3_step(upd.stmt) != SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(impl_->db));
        impl_->record_transition(client_order_id, "RESERVED", "PENDING_DISPATCH", "dispatch_boundary_locked");
        impl_->record_transition(client_order_id, "PENDING_DISPATCH", "DISPATCHED_UNKNOWN", "durable_before_remote");
        order_copy = impl_->load_order_unlocked(client_order_id);
        tx.commit();
    }

    if (simulate_crash_before_transport) {
        return order_copy;
    }

    DispatchOutcome outcome = transport(order_copy);
    if (outcome.remote_state == OrderState::DISPATCHED_UNKNOWN) {
        return order_copy;
    }
    return reconcile_order(client_order_id, outcome);
}

OmsOrderRecord NativeOms::reconcile_order(const std::string& client_order_id, const DispatchOutcome& outcome) {
    if (outcome.filled_qty < 0 || outcome.avg_fill_price_cents < 0 || outcome.fee_cents < 0) {
        throw std::invalid_argument("Negative execution quantities/prices/fees rejected");
    }

    std::lock_guard<std::mutex> lk(impl_->mu);
    TxGuard tx(impl_->db);
    OmsOrderRecord cur = impl_->load_order_unlocked(client_order_id);

    // R3: Exact execution ID deduplication.
    if (!outcome.exec_id.empty()) {
        StmtGuard chk(impl_->db, "SELECT 1 FROM aq_oms_executions WHERE exec_id=?1;");
        sqlite3_bind_text(chk.stmt, 1, outcome.exec_id.c_str(), -1, SQLITE_TRANSIENT);
        if (sqlite3_step(chk.stmt) == SQLITE_ROW) {
            tx.commit();
            return cur;
        }
    }

    std::string ts = now_utc_iso();

    // G1: Handle bust/correction, zero-price fill, missing exec_id on incremental fill, or over-fill conflict
    // atomically in the SAME database transaction (control + quarantine + conflict evidence + preserved obligations).
    if (outcome.is_bust) {
        OmsOrderRecord out = impl_->record_conflict_and_halt_in_tx(cur, outcome, "EXECUTION_CONFLICT_OR_BUST");
        tx.commit();
        return out;
    }
    if (outcome.filled_qty > 0 && outcome.avg_fill_price_cents <= 0) {
        OmsOrderRecord out = impl_->record_conflict_and_halt_in_tx(cur, outcome, "ZERO_PRICE_FILL_CONFLICT");
        tx.commit();
        return out;
    }
    if (!outcome.is_cumulative && (outcome.filled_qty > 0 || outcome.fee_cents > 0) && outcome.exec_id.empty()) {
        OmsOrderRecord out = impl_->record_conflict_and_halt_in_tx(cur, outcome, "MISSING_EXEC_ID_FOR_INCREMENTAL");
        tx.commit();
        return out;
    }
    if (outcome.is_cumulative && outcome.filled_qty > cur.qty) {
        OmsOrderRecord out = impl_->record_conflict_and_halt_in_tx(cur, outcome, "CUMULATIVE_OVERFILL_CONFLICT");
        tx.commit();
        return out;
    }

    // R3: Out-of-order stale cumulative update must NOT roll back filled_qty or release active reserve.
    if (outcome.is_cumulative && outcome.filled_qty > 0 && outcome.filled_qty < cur.filled_qty) {
        impl_->record_audit(cur.env, "STALE_CUMULATIVE_IGNORED", "cid=" + client_order_id);
        tx.commit();
        return cur;
    }

    int64_t delta_fill = 0;
    int64_t delta_notional = 0;
    int64_t new_filled = cur.filled_qty;
    int64_t new_cum_notional = cur.cumulative_executed_notional_cents;

    if (outcome.filled_qty > 0) {
        if (outcome.is_cumulative) {
            int64_t target_filled = std::min(cur.qty, outcome.filled_qty);
            delta_fill = target_filled - cur.filled_qty;
            if (delta_fill > 0) {
                int64_t target_cum_notional = 0;
                if (aq_checked_mul_i64(target_filled, outcome.avg_fill_price_cents, &target_cum_notional) != AQ_OK ||
                    aq_checked_sub_i64(target_cum_notional, cur.cumulative_executed_notional_cents, &delta_notional) != AQ_OK) {
                    throw std::overflow_error("cumulative notional delta overflow");
                }
                if (delta_notional < 0) {
                    OmsOrderRecord out = impl_->record_conflict_and_halt_in_tx(
                        cur, outcome, "NEGATIVE_CUMULATIVE_NOTIONAL_CONFLICT");
                    tx.commit();
                    return out;
                }
                new_filled = target_filled;
                new_cum_notional = target_cum_notional;
            }
        } else {
            if (aq_checked_add_i64(cur.filled_qty, outcome.filled_qty, &new_filled) != AQ_OK ||
                new_filled > cur.qty) {
                OmsOrderRecord out = impl_->record_conflict_and_halt_in_tx(
                    cur, outcome, "INCREMENTAL_OVERFILL_CONFLICT");
                tx.commit();
                return out;
            }
            delta_fill = outcome.filled_qty;
            if (aq_checked_mul_i64(delta_fill, outcome.avg_fill_price_cents, &delta_notional) != AQ_OK ||
                aq_checked_add_i64(cur.cumulative_executed_notional_cents, delta_notional, &new_cum_notional) != AQ_OK) {
                throw std::overflow_error("incremental notional overflow");
            }
        }
    }

    if (delta_fill > 0 || outcome.fee_cents > 0) {
        if (!outcome.exec_id.empty()) {
            StmtGuard ins_ex(impl_->db,
                "INSERT INTO aq_oms_executions(exec_id, client_order_id, exec_qty, exec_price_cents, fee_cents, is_bust, created_at) "
                "VALUES(?1,?2,?3,?4,?5,0,?6);");
            sqlite3_bind_text(ins_ex.stmt, 1, outcome.exec_id.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_text(ins_ex.stmt, 2, client_order_id.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_int64(ins_ex.stmt, 3, delta_fill);
            sqlite3_bind_int64(ins_ex.stmt, 4, outcome.avg_fill_price_cents);
            sqlite3_bind_int64(ins_ex.stmt, 5, outcome.fee_cents);
            sqlite3_bind_text(ins_ex.stmt, 6, ts.c_str(), -1, SQLITE_TRANSIENT);
            if (sqlite3_step(ins_ex.stmt) != SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(impl_->db));
        }

        OmsAccountView view = impl_->load_account_unlocked(cur.env);
        int64_t cur_pos_qty = 0;
        for (const auto& p : view.positions) {
            if (std::strncmp(p.symbol, cur.symbol.c_str(), AQ_MAX_SYMBOL_LEN) == 0) {
                cur_pos_qty = p.qty;
                break;
            }
        }
        int64_t new_cash = view.cash_cents;
        int64_t new_pos_qty = cur_pos_qty;
        if (cur.side == AQ_SIDE_BUY) {
            int64_t total_spend = 0;
            if (aq_checked_add_i64(delta_notional, outcome.fee_cents, &total_spend) != AQ_OK ||
                aq_checked_sub_i64(new_cash, total_spend, &new_cash) != AQ_OK ||
                aq_checked_add_i64(new_pos_qty, delta_fill, &new_pos_qty) != AQ_OK) {
                throw std::overflow_error("buy settlement overflow");
            }
        } else {
            int64_t net_proc = 0;
            if (aq_checked_sub_i64(delta_notional, outcome.fee_cents, &net_proc) != AQ_OK ||
                aq_checked_add_i64(new_cash, net_proc, &new_cash) != AQ_OK ||
                aq_checked_sub_i64(new_pos_qty, delta_fill, &new_pos_qty) != AQ_OK) {
                throw std::overflow_error("sell settlement overflow");
            }
        }

        StmtGuard up_acc(impl_->db,
            "UPDATE aq_oms_account SET cash_cents=?2, account_revision=account_revision+1, updated_at=?3 WHERE env=?1;");
        sqlite3_bind_text(up_acc.stmt, 1, cur.env.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_int64(up_acc.stmt, 2, new_cash);
        sqlite3_bind_text(up_acc.stmt, 3, ts.c_str(), -1, SQLITE_TRANSIENT);
        if (sqlite3_step(up_acc.stmt) != SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(impl_->db));

        if (delta_fill > 0) {
            StmtGuard up_pos(impl_->db,
                "INSERT INTO aq_oms_positions(env, symbol, qty, mark_cents) VALUES(?1,?2,?3,?4) "
                "ON CONFLICT(env, symbol) DO UPDATE SET qty=excluded.qty, mark_cents=excluded.mark_cents;");
            sqlite3_bind_text(up_pos.stmt, 1, cur.env.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_text(up_pos.stmt, 2, cur.symbol.c_str(), -1, SQLITE_TRANSIENT);
            sqlite3_bind_int64(up_pos.stmt, 3, new_pos_qty);
            sqlite3_bind_int64(up_pos.stmt, 4, outcome.avg_fill_price_cents > 0 ? outcome.avg_fill_price_cents : cur.limit_price_cents);
            if (sqlite3_step(up_pos.stmt) != SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(impl_->db));
        }
    }

    OrderState next_st = outcome.remote_state;
    if (new_filled >= cur.qty) {
        next_st = OrderState::FILLED;
    } else if (is_terminal_state(cur.state) && delta_fill == 0) {
        next_st = cur.state;
    }

    // R3: Direct remaining-obligation reservation computation (never recursive rescaling!).
    int64_t rem_qty = std::max<int64_t>(0, cur.qty - new_filled);
    int64_t new_res_cash = 0;
    int64_t new_res_sell = 0;
    if (!is_terminal_state(next_st) && rem_qty > 0) {
        if (cur.side == AQ_SIDE_BUY) {
            new_res_cash = compute_buy_reserve(rem_qty, cur.limit_price_cents, cur.fee_allowance_bps);
        } else {
            new_res_sell = rem_qty;
        }
    }

    int64_t new_cum_fees = 0;
    if (aq_checked_add_i64(cur.cumulative_fees_cents, outcome.fee_cents, &new_cum_fees) != AQ_OK) {
        throw std::overflow_error("cumulative fees overflow");
    }

    StmtGuard upd(impl_->db,
        "UPDATE aq_oms_orders SET state=?2, filled_qty=?3, reserved_cash_cents=?4, reserved_sell_qty=?5,"
        " cumulative_executed_notional_cents=?6, cumulative_fees_cents=?7,"
        " broker_order_id=CASE WHEN ?8<>'' THEN ?8 ELSE broker_order_id END, last_error=?9, updated_at=?10 "
        "WHERE client_order_id=?1;");
    sqlite3_bind_text(upd.stmt, 1, client_order_id.c_str(), -1, SQLITE_TRANSIENT);
    std::string nst = order_state_to_str(next_st);
    sqlite3_bind_text(upd.stmt, 2, nst.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_int64(upd.stmt, 3, new_filled);
    sqlite3_bind_int64(upd.stmt, 4, new_res_cash);
    sqlite3_bind_int64(upd.stmt, 5, new_res_sell);
    sqlite3_bind_int64(upd.stmt, 6, new_cum_notional);
    sqlite3_bind_int64(upd.stmt, 7, new_cum_fees);
    sqlite3_bind_text(upd.stmt, 8, outcome.broker_order_id.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(upd.stmt, 9, outcome.detail.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(upd.stmt, 10, ts.c_str(), -1, SQLITE_TRANSIENT);
    if (sqlite3_step(upd.stmt) != SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(impl_->db));

    impl_->record_transition(client_order_id, order_state_to_str(cur.state), nst, outcome.detail);
    OmsOrderRecord out = impl_->load_order_unlocked(client_order_id);
    tx.commit();
    return out;
}

OmsOrderRecord NativeOms::request_cancel(const std::string& client_order_id) {
    std::lock_guard<std::mutex> lk(impl_->mu);
    TxGuard tx(impl_->db);
    OmsOrderRecord cur = impl_->load_order_unlocked(client_order_id);
    std::string ts = now_utc_iso();
    if (cur.state == OrderState::RESERVED) {
        StmtGuard upd(impl_->db,
            "UPDATE aq_oms_orders SET state='CANCELLED', reserved_cash_cents=0, reserved_sell_qty=0, updated_at=?2 WHERE client_order_id=?1;");
        sqlite3_bind_text(upd.stmt, 1, client_order_id.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(upd.stmt, 2, ts.c_str(), -1, SQLITE_TRANSIENT);
        if (sqlite3_step(upd.stmt) != SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(impl_->db));
        impl_->record_transition(client_order_id, "RESERVED", "CANCELLED", "cancelled_before_dispatch");
    } else if (!is_terminal_state(cur.state)) {
        StmtGuard upd(impl_->db, "UPDATE aq_oms_orders SET state='CANCEL_PENDING', updated_at=?2 WHERE client_order_id=?1;");
        sqlite3_bind_text(upd.stmt, 1, client_order_id.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(upd.stmt, 2, ts.c_str(), -1, SQLITE_TRANSIENT);
        if (sqlite3_step(upd.stmt) != SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(impl_->db));
        impl_->record_transition(client_order_id, order_state_to_str(cur.state), "CANCEL_PENDING", "cancel_requested_for_in_flight");
    }
    OmsOrderRecord out = impl_->load_order_unlocked(client_order_id);
    tx.commit();
    return out;
}

std::vector<OmsOrderRecord> NativeOms::recover_in_flight(const std::string& env) const {
    std::lock_guard<std::mutex> lk(impl_->mu);
    std::string sql = std::string(kSelectOrderCols) +
                      "WHERE env=?1 AND state IN ('PENDING_DISPATCH','DISPATCHED_UNKNOWN','CANCEL_PENDING') ORDER BY rowid;";
    StmtGuard st(impl_->db, sql.c_str());
    sqlite3_bind_text(st.stmt, 1, env.c_str(), -1, SQLITE_TRANSIENT);
    std::vector<OmsOrderRecord> out;
    while (sqlite3_step(st.stmt) == SQLITE_ROW) {
        out.push_back(row_to_order(st.stmt));
    }
    return out;
}

NativeOms::SchemaMeta NativeOms::schema_meta() const {
    std::lock_guard<std::mutex> lk(impl_->mu);
    SchemaMeta m;
    StmtGuard st(impl_->db,
        "SELECT schema_version, code_sha, model_format, backup_hash, updated_at FROM aq_schema_meta WHERE id=1;");
    if (sqlite3_step(st.stmt) == SQLITE_ROW) {
        m.schema_version = sqlite3_column_int(st.stmt, 0);
        m.code_sha = col_str_or_empty(st.stmt, 1);
        m.model_format = col_str_or_empty(st.stmt, 2);
        m.backup_hash = col_str_or_empty(st.stmt, 3);
        m.updated_at = col_str_or_empty(st.stmt, 4);
    }
    return m;
}

namespace {
std::string sha256_file_bytes(const std::string& path) {
    std::ifstream in(path, std::ios::binary);
    if (!in) throw std::runtime_error("Cannot open file for hashing: " + path);
    std::ostringstream ss;
    ss << in.rdbuf();
    std::string data = ss.str();
    return sha256_hex(reinterpret_cast<const uint8_t*>(data.data()), data.size());
}
}  // namespace

NativeOms::BackupManifest NativeOms::create_backup_manifest(
    const std::string& out_dir,
    const std::string& code_sha,
    const std::string& model_format) const {
    std::filesystem::create_directories(out_dir);
    std::string backup_db = (std::filesystem::path(out_dir) / "oms_backup.db").string();
    std::string manifest_path = (std::filesystem::path(out_dir) / "release_manifest.json").string();

    int sv = kCurrentSchemaVersion;
    {
        std::lock_guard<std::mutex> lk(impl_->mu);
        sqlite3* dst = nullptr;
        if (sqlite3_open(backup_db.c_str(), &dst) != SQLITE_OK) {
            std::string err = dst ? sqlite3_errmsg(dst) : "open failed";
            if (dst) sqlite3_close(dst);
            throw std::runtime_error("Backup sqlite open failed: " + err);
        }
        sqlite3_backup* bk = sqlite3_backup_init(dst, "main", impl_->db, "main");
        if (!bk) {
            std::string err = sqlite3_errmsg(dst);
            sqlite3_close(dst);
            throw std::runtime_error("sqlite3_backup_init failed: " + err);
        }
        int rc = sqlite3_backup_step(bk, -1);
        sqlite3_backup_finish(bk);
        sqlite3_close(dst);
        if (rc != SQLITE_DONE) {
            throw std::runtime_error("sqlite3_backup_step failed rc=" + std::to_string(rc));
        }

        StmtGuard st(impl_->db, "SELECT schema_version FROM aq_schema_meta WHERE id=1;");
        if (sqlite3_step(st.stmt) == SQLITE_ROW) sv = sqlite3_column_int(st.stmt, 0);
    }

    std::string bhash = sha256_file_bytes(backup_db);
    {
        std::lock_guard<std::mutex> lk(impl_->mu);
        TxGuard tx(impl_->db);
        StmtGuard upd(impl_->db,
            "UPDATE aq_schema_meta SET code_sha=?1, model_format=?2, backup_hash=?3, updated_at=?4 WHERE id=1;");
        sqlite3_bind_text(upd.stmt, 1, code_sha.c_str(), static_cast<int>(code_sha.size()), SQLITE_TRANSIENT);
        sqlite3_bind_text(upd.stmt, 2, model_format.c_str(), static_cast<int>(model_format.size()), SQLITE_TRANSIENT);
        sqlite3_bind_text(upd.stmt, 3, bhash.c_str(), static_cast<int>(bhash.size()), SQLITE_TRANSIENT);
        std::string ts = now_utc_iso();
        sqlite3_bind_text(upd.stmt, 4, ts.c_str(), static_cast<int>(ts.size()), SQLITE_TRANSIENT);
        if (sqlite3_step(upd.stmt) != SQLITE_DONE) throw std::runtime_error(sqlite3_errmsg(impl_->db));
        tx.commit();
    }

    std::ofstream mf(manifest_path, std::ios::binary | std::ios::trunc);
    if (!mf) throw std::runtime_error("Cannot write manifest: " + manifest_path);
    mf << "{\n"
       << "  \"code_sha\": \"" << code_sha << "\",\n"
       << "  \"schema_version\": " << sv << ",\n"
       << "  \"model_format\": \"" << model_format << "\",\n"
       << "  \"backup_db\": \"" << backup_db << "\",\n"
       << "  \"backup_hash\": \"" << bhash << "\"\n"
       << "}\n";
    mf.close();

    return BackupManifest{backup_db, manifest_path, code_sha, sv, model_format, bhash};
}

void NativeOms::restore_from_backup_manifest(
    const std::string& manifest_path,
    const std::string& target_db_path) {
    std::ifstream mf(manifest_path, std::ios::binary);
    if (!mf) throw std::runtime_error("Cannot open manifest: " + manifest_path);
    std::ostringstream ss;
    ss << mf.rdbuf();
    JsonValue j = parse_json_strict(ss.str());
    const auto& obj = j.as_object();
    std::string backup_db = obj.at("backup_db").as_string();
    std::string expected_hash = obj.at("backup_hash").as_string();
    int64_t sv = obj.at("schema_version").as_int64();
    if (sv < 1 || sv > kCurrentSchemaVersion) {
        throw std::runtime_error("Manifest schema_version unsupported for restore");
    }
    std::string actual_hash = sha256_file_bytes(backup_db);
    if (actual_hash != expected_hash) {
        throw std::runtime_error("Backup hash mismatch: snapshot tampered or corrupted");
    }
    std::filesystem::remove(target_db_path + "-wal");
    std::filesystem::remove(target_db_path + "-shm");
    std::filesystem::copy_file(backup_db, target_db_path, std::filesystem::copy_options::overwrite_existing);
}

}  // namespace aq
