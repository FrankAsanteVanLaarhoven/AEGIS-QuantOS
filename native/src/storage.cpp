#include "aq/storage.hpp"

#include <openssl/evp.h>
#include <sqlite3.h>

#include <cstdio>
#include <stdexcept>

namespace aq {

std::string sha256_hex(const uint8_t* data, size_t len) {
    EVP_MD_CTX* ctx = EVP_MD_CTX_new();
    if (!ctx) throw std::runtime_error("EVP_MD_CTX_new failed");
    uint8_t digest[EVP_MAX_MD_SIZE];
    unsigned int dlen = 0;
    if (EVP_DigestInit_ex(ctx, EVP_sha256(), nullptr) != 1 ||
        (len > 0 && EVP_DigestUpdate(ctx, data, len) != 1) ||
        EVP_DigestFinal_ex(ctx, digest, &dlen) != 1) {
        EVP_MD_CTX_free(ctx);
        throw std::runtime_error("EVP SHA-256 failed");
    }
    EVP_MD_CTX_free(ctx);
    std::string out;
    out.reserve(dlen * 2);
    static const char kHex[] = "0123456789abcdef";
    for (unsigned int i = 0; i < dlen; ++i) {
        out.push_back(kHex[(digest[i] >> 4) & 0xF]);
        out.push_back(kHex[digest[i] & 0xF]);
    }
    return out;
}

std::string sha256_hex(const std::string& s) {
    return sha256_hex(reinterpret_cast<const uint8_t*>(s.data()), s.size());
}

namespace {
struct TxGuard {
    sqlite3* db;
    bool committed = false;
    explicit TxGuard(sqlite3* d) : db(d) {
        char* err = nullptr;
        int rc = sqlite3_exec(db, "BEGIN IMMEDIATE;", nullptr, nullptr, &err);
        if (rc != SQLITE_OK) {
            std::string msg = err ? err : sqlite3_errmsg(db);
            sqlite3_free(err);
            throw std::runtime_error("BEGIN IMMEDIATE failed: " + msg);
        }
    }
    void commit() {
        char* err = nullptr;
        int rc = sqlite3_exec(db, "COMMIT;", nullptr, nullptr, &err);
        if (rc != SQLITE_OK) {
            std::string msg = err ? err : sqlite3_errmsg(db);
            sqlite3_free(err);
            throw std::runtime_error("COMMIT failed: " + msg);
        }
        committed = true;
    }
    ~TxGuard() {
        if (!committed) {
            sqlite3_exec(db, "ROLLBACK;", nullptr, nullptr, nullptr);
        }
    }
};

struct StmtGuard {
    sqlite3_stmt* stmt = nullptr;
    StmtGuard(sqlite3* db, const char* sql) {
        if (sqlite3_prepare_v2(db, sql, -1, &stmt, nullptr) != SQLITE_OK) {
            throw std::runtime_error(std::string("sqlite3_prepare_v2 failed: ") + sqlite3_errmsg(db));
        }
    }
    ~StmtGuard() {
        if (stmt) sqlite3_finalize(stmt);
    }
};
}  // namespace

SqliteStore::SqliteStore(const std::string& path) {
    if (sqlite3_open(path.c_str(), &db_) != SQLITE_OK) {
        std::string msg = db_ ? sqlite3_errmsg(db_) : "sqlite3_open failed";
        if (db_) sqlite3_close(db_);
        db_ = nullptr;
        throw std::runtime_error(msg);
    }
    sqlite3_busy_timeout(db_, 5000);
    exec("PRAGMA journal_mode=WAL;");
    exec("PRAGMA synchronous=FULL;");
}

SqliteStore::~SqliteStore() {
    if (db_) sqlite3_close(db_);
}

void SqliteStore::exec(const std::string& sql) {
    char* err = nullptr;
    if (sqlite3_exec(db_, sql.c_str(), nullptr, nullptr, &err) != SQLITE_OK) {
        std::string msg = err ? err : sqlite3_errmsg(db_);
        sqlite3_free(err);
        throw std::runtime_error("sqlite3_exec failed: " + msg);
    }
}

BudgetLedgerNative::BudgetLedgerNative(SqliteStore& store, int64_t job_cap_nano, int64_t daily_cap_nano, int64_t project_cap_nano)
    : store_(store), job_cap_nano_(job_cap_nano), daily_cap_nano_(daily_cap_nano), project_cap_nano_(project_cap_nano) {
    ensure_schema();
}

void BudgetLedgerNative::ensure_schema() {
    store_.exec(
        "CREATE TABLE IF NOT EXISTS budget_reservations ("
        "  reservation_id TEXT PRIMARY KEY,"
        "  job_id TEXT NOT NULL,"
        "  originating_day TEXT NOT NULL,"
        "  reserved_nano INTEGER NOT NULL,"
        "  actual_nano INTEGER,"
        "  status TEXT NOT NULL"
        ");"
        "CREATE TABLE IF NOT EXISTS budget_job_spent (job_id TEXT PRIMARY KEY, spent_nano INTEGER NOT NULL);"
        "CREATE TABLE IF NOT EXISTS budget_daily_spent (day TEXT PRIMARY KEY, spent_nano INTEGER NOT NULL);"
        "CREATE TABLE IF NOT EXISTS budget_project_spent (id INTEGER PRIMARY KEY CHECK (id = 1), spent_nano INTEGER NOT NULL);"
        "INSERT OR IGNORE INTO budget_project_spent(id, spent_nano) VALUES (1, 0);");
}

bool BudgetLedgerNative::reserve(const std::string& reservation_id, const std::string& job_id, const std::string& day, int64_t est_nano) {
    if (est_nano <= 0 || reservation_id.empty() || job_id.empty() || day.empty()) return false;
    TxGuard tx(store_.db());

    int64_t job_spent = 0;
    {
        StmtGuard st(store_.db(), "SELECT spent_nano FROM budget_job_spent WHERE job_id = ?;");
        sqlite3_bind_text(st.stmt, 1, job_id.c_str(), -1, SQLITE_TRANSIENT);
        if (sqlite3_step(st.stmt) == SQLITE_ROW) job_spent = sqlite3_column_int64(st.stmt, 0);
    }
    int64_t day_spent = 0;
    {
        StmtGuard st(store_.db(), "SELECT spent_nano FROM budget_daily_spent WHERE day = ?;");
        sqlite3_bind_text(st.stmt, 1, day.c_str(), -1, SQLITE_TRANSIENT);
        if (sqlite3_step(st.stmt) == SQLITE_ROW) day_spent = sqlite3_column_int64(st.stmt, 0);
    }
    int64_t proj_spent = 0;
    {
        StmtGuard st(store_.db(), "SELECT spent_nano FROM budget_project_spent WHERE id = 1;");
        if (sqlite3_step(st.stmt) == SQLITE_ROW) proj_spent = sqlite3_column_int64(st.stmt, 0);
    }

    if (job_spent + est_nano > job_cap_nano_ ||
        day_spent + est_nano > daily_cap_nano_ ||
        proj_spent + est_nano > project_cap_nano_) {
        return false;
    }

    {
        StmtGuard st(store_.db(),
                     "INSERT INTO budget_reservations(reservation_id, job_id, originating_day, reserved_nano, actual_nano, status) "
                     "VALUES (?, ?, ?, ?, NULL, 'RESERVED');");
        sqlite3_bind_text(st.stmt, 1, reservation_id.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(st.stmt, 2, job_id.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(st.stmt, 3, day.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_int64(st.stmt, 4, est_nano);
        if (sqlite3_step(st.stmt) != SQLITE_DONE) return false;
    }
    {
        StmtGuard st(store_.db(),
                     "INSERT INTO budget_job_spent(job_id, spent_nano) VALUES (?, ?) "
                     "ON CONFLICT(job_id) DO UPDATE SET spent_nano = spent_nano + excluded.spent_nano;");
        sqlite3_bind_text(st.stmt, 1, job_id.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_int64(st.stmt, 2, est_nano);
        if (sqlite3_step(st.stmt) != SQLITE_DONE) return false;
    }
    {
        StmtGuard st(store_.db(),
                     "INSERT INTO budget_daily_spent(day, spent_nano) VALUES (?, ?) "
                     "ON CONFLICT(day) DO UPDATE SET spent_nano = spent_nano + excluded.spent_nano;");
        sqlite3_bind_text(st.stmt, 1, day.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_int64(st.stmt, 2, est_nano);
        if (sqlite3_step(st.stmt) != SQLITE_DONE) return false;
    }
    {
        StmtGuard st(store_.db(), "UPDATE budget_project_spent SET spent_nano = spent_nano + ? WHERE id = 1;");
        sqlite3_bind_int64(st.stmt, 1, est_nano);
        if (sqlite3_step(st.stmt) != SQLITE_DONE) return false;
    }

    tx.commit();
    return true;
}

bool BudgetLedgerNative::reconcile(const std::string& reservation_id, std::optional<int64_t> actual_nano, bool unknown_outcome) {
    TxGuard tx(store_.db());
    int64_t reserved_nano = 0;
    std::string job_id;
    std::string day;
    std::string status;
    {
        StmtGuard st(store_.db(),
                     "SELECT job_id, originating_day, reserved_nano, status FROM budget_reservations WHERE reservation_id = ?;");
        sqlite3_bind_text(st.stmt, 1, reservation_id.c_str(), -1, SQLITE_TRANSIENT);
        if (sqlite3_step(st.stmt) != SQLITE_ROW) return false;
        job_id = reinterpret_cast<const char*>(sqlite3_column_text(st.stmt, 0));
        day = reinterpret_cast<const char*>(sqlite3_column_text(st.stmt, 1));
        reserved_nano = sqlite3_column_int64(st.stmt, 2);
        status = reinterpret_cast<const char*>(sqlite3_column_text(st.stmt, 3));
    }
    if (status == "SETTLED" || status == "RELEASED") {
        tx.commit();
        return true;
    }
    if (unknown_outcome || !actual_nano.has_value()) {
        StmtGuard st(store_.db(), "UPDATE budget_reservations SET status = 'UNKNOWN_OUTCOME', actual_nano = NULL WHERE reservation_id = ?;");
        sqlite3_bind_text(st.stmt, 1, reservation_id.c_str(), -1, SQLITE_TRANSIENT);
        if (sqlite3_step(st.stmt) != SQLITE_DONE) return false;
        tx.commit();
        return true;
    }

    int64_t delta = *actual_nano - reserved_nano;
    {
        StmtGuard st(store_.db(), "UPDATE budget_job_spent SET spent_nano = MAX(0, spent_nano + ?) WHERE job_id = ?;");
        sqlite3_bind_int64(st.stmt, 1, delta);
        sqlite3_bind_text(st.stmt, 2, job_id.c_str(), -1, SQLITE_TRANSIENT);
        if (sqlite3_step(st.stmt) != SQLITE_DONE) return false;
    }
    {
        StmtGuard st(store_.db(), "UPDATE budget_daily_spent SET spent_nano = MAX(0, spent_nano + ?) WHERE day = ?;");
        sqlite3_bind_int64(st.stmt, 1, delta);
        sqlite3_bind_text(st.stmt, 2, day.c_str(), -1, SQLITE_TRANSIENT);
        if (sqlite3_step(st.stmt) != SQLITE_DONE) return false;
    }
    {
        StmtGuard st(store_.db(), "UPDATE budget_project_spent SET spent_nano = MAX(0, spent_nano + ?) WHERE id = 1;");
        sqlite3_bind_int64(st.stmt, 1, delta);
        if (sqlite3_step(st.stmt) != SQLITE_DONE) return false;
    }
    {
        StmtGuard st(store_.db(), "UPDATE budget_reservations SET status = 'SETTLED', actual_nano = ? WHERE reservation_id = ?;");
        sqlite3_bind_int64(st.stmt, 1, *actual_nano);
        sqlite3_bind_text(st.stmt, 2, reservation_id.c_str(), -1, SQLITE_TRANSIENT);
        if (sqlite3_step(st.stmt) != SQLITE_DONE) return false;
    }
    tx.commit();
    return true;
}

BudgetSnapshot BudgetLedgerNative::snapshot(const std::string& day) {
    BudgetSnapshot snap;
    {
        StmtGuard st(store_.db(), "SELECT spent_nano FROM budget_project_spent WHERE id = 1;");
        if (sqlite3_step(st.stmt) == SQLITE_ROW) snap.project_spent_nano = sqlite3_column_int64(st.stmt, 0);
    }
    {
        StmtGuard st(store_.db(), "SELECT spent_nano FROM budget_daily_spent WHERE day = ?;");
        sqlite3_bind_text(st.stmt, 1, day.c_str(), -1, SQLITE_TRANSIENT);
        if (sqlite3_step(st.stmt) == SQLITE_ROW) snap.daily_spent_nano = sqlite3_column_int64(st.stmt, 0);
    }
    {
        StmtGuard st(store_.db(), "SELECT COUNT(*) FROM budget_reservations WHERE status = 'RESERVED';");
        if (sqlite3_step(st.stmt) == SQLITE_ROW) snap.active_reservations = sqlite3_column_int64(st.stmt, 0);
    }
    {
        StmtGuard st(store_.db(), "SELECT COUNT(*) FROM budget_reservations WHERE status = 'UNKNOWN_OUTCOME';");
        if (sqlite3_step(st.stmt) == SQLITE_ROW) snap.unknown_outcomes = sqlite3_column_int64(st.stmt, 0);
    }
    return snap;
}

ControlJournal::ControlJournal(SqliteStore& store) : store_(store) {
    ensure_schema();
}

void ControlJournal::ensure_schema() {
    store_.exec(
        "CREATE TABLE IF NOT EXISTS native_control_state ("
        "  id INTEGER PRIMARY KEY CHECK (id = 1),"
        "  halted INTEGER NOT NULL,"
        "  halt_reason TEXT,"
        "  control_epoch INTEGER NOT NULL"
        ");"
        "INSERT OR IGNORE INTO native_control_state(id, halted, halt_reason, control_epoch) VALUES (1, 0, NULL, 1);"
        "CREATE TABLE IF NOT EXISTS native_intent_queue ("
        "  client_order_id TEXT PRIMARY KEY,"
        "  intent_hash TEXT NOT NULL,"
        "  control_epoch INTEGER NOT NULL,"
        "  state TEXT NOT NULL"
        ");");
}

uint64_t ControlJournal::current_epoch() {
    StmtGuard st(store_.db(), "SELECT control_epoch FROM native_control_state WHERE id = 1;");
    if (sqlite3_step(st.stmt) == SQLITE_ROW) return static_cast<uint64_t>(sqlite3_column_int64(st.stmt, 0));
    return 1;
}

bool ControlJournal::is_halted() {
    StmtGuard st(store_.db(), "SELECT halted FROM native_control_state WHERE id = 1;");
    if (sqlite3_step(st.stmt) == SQLITE_ROW) return sqlite3_column_int(st.stmt, 0) != 0;
    return true;
}

uint64_t ControlJournal::set_halt(bool enabled, const std::string& reason) {
    TxGuard tx(store_.db());
    {
        StmtGuard st(store_.db(),
                     "UPDATE native_control_state SET halted = ?, halt_reason = ?, control_epoch = control_epoch + 1 WHERE id = 1;");
        sqlite3_bind_int(st.stmt, 1, enabled ? 1 : 0);
        if (enabled) sqlite3_bind_text(st.stmt, 2, reason.c_str(), -1, SQLITE_TRANSIENT);
        else sqlite3_bind_null(st.stmt, 2);
        sqlite3_step(st.stmt);
    }
    uint64_t ep = current_epoch();
    tx.commit();
    return ep;
}

bool ControlJournal::enqueue_intent(const std::string& client_order_id, const std::string& intent_hash, uint64_t expected_epoch) {
    TxGuard tx(store_.db());
    if (is_halted() || current_epoch() != expected_epoch) return false;
    StmtGuard st(store_.db(),
                 "INSERT INTO native_intent_queue(client_order_id, intent_hash, control_epoch, state) VALUES (?, ?, ?, 'QUEUED');");
    sqlite3_bind_text(st.stmt, 1, client_order_id.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(st.stmt, 2, intent_hash.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_int64(st.stmt, 3, static_cast<int64_t>(expected_epoch));
    if (sqlite3_step(st.stmt) != SQLITE_DONE) return false;
    tx.commit();
    return true;
}

bool ControlJournal::claim_for_dispatch(const std::string& client_order_id, uint64_t expected_epoch) {
    TxGuard tx(store_.db());
    if (is_halted() || current_epoch() != expected_epoch) return false;
    StmtGuard st(store_.db(),
                 "UPDATE native_intent_queue SET state = 'CLAIMED' "
                 "WHERE client_order_id = ? AND control_epoch = ? AND state = 'QUEUED';");
    sqlite3_bind_text(st.stmt, 1, client_order_id.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_int64(st.stmt, 2, static_cast<int64_t>(expected_epoch));
    if (sqlite3_step(st.stmt) != SQLITE_DONE) return false;
    int changes = sqlite3_changes(store_.db());
    tx.commit();
    return changes == 1;
}

}  // namespace aq
