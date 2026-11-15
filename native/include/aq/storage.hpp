#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

struct sqlite3;

namespace aq {

inline constexpr int64_t kNanoPerUsd = 1000000000LL;

struct BudgetSnapshot {
    int64_t project_spent_nano = 0;
    int64_t daily_spent_nano = 0;
    int64_t active_reservations = 0;
    int64_t unknown_outcomes = 0;
};

class SqliteStore {
public:
    explicit SqliteStore(const std::string& path);
    ~SqliteStore();

    SqliteStore(const SqliteStore&) = delete;
    SqliteStore& operator=(const SqliteStore&) = delete;

    sqlite3* db() const { return db_; }
    void exec(const std::string& sql);

private:
    sqlite3* db_ = nullptr;
};

class BudgetLedgerNative {
public:
    BudgetLedgerNative(SqliteStore& store, int64_t job_cap_nano, int64_t daily_cap_nano, int64_t project_cap_nano);

    bool reserve(const std::string& reservation_id, const std::string& job_id, const std::string& day, int64_t est_nano);
    bool reconcile(const std::string& reservation_id, std::optional<int64_t> actual_nano, bool unknown_outcome = false);
    BudgetSnapshot snapshot(const std::string& day);

private:
    SqliteStore& store_;
    int64_t job_cap_nano_;
    int64_t daily_cap_nano_;
    int64_t project_cap_nano_;
    void ensure_schema();
};

class ControlJournal {
public:
    explicit ControlJournal(SqliteStore& store);

    uint64_t current_epoch();
    bool is_halted();
    uint64_t set_halt(bool enabled, const std::string& reason);
    bool enqueue_intent(const std::string& client_order_id, const std::string& intent_hash, uint64_t expected_epoch);
    bool claim_for_dispatch(const std::string& client_order_id, uint64_t expected_epoch);

private:
    SqliteStore& store_;
    void ensure_schema();
};

std::string sha256_hex(const uint8_t* data, size_t len);
std::string sha256_hex(const std::string& s);

}  // namespace aq
