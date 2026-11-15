#pragma once

#include "aq/data.hpp"
#include "aq/json.hpp"
#include "aq/oms.hpp"
#include "aq/rl.hpp"

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace aq {

struct HttpRequest {
    std::string method;
    std::string path;
    std::map<std::string, std::string> headers;
    std::string body;
    std::string remote_addr = "127.0.0.1";
};

struct HttpResponse {
    int status_code = 200;
    std::string content_type = "application/json";
    std::map<std::string, std::string> headers;
    std::string body;
};

struct ServiceConfig {
    std::string db_path;
    std::string env = "paper_native";
    std::string data_namespace = "SYNTHETIC";
    std::string auth_token;
    std::string writer_owner_id = "native-writer-1";
    uint64_t writer_epoch = 1;
    bool enable_paper_writer = false;  // C5: disabled by default; read-only instance never revokes another owner's lease
    std::string host = "127.0.0.1";
    uint16_t port = 0;                 // 0 = ephemeral port
    uint32_t io_timeout_ms = 3000;     // R5: read/write deadline for slow/truncated clients
    int64_t override_now_epoch_s = 0;  // 0 = system_clock::now(); >0 for deterministic freshness tests
};

struct ReplayRepeatabilityReport {
    bool exact_repeatability = false;
    int64_t final_equity_cents = 0;
    uint64_t steps_compared = 0;
    std::string receipt_hash;
};

ReplayRepeatabilityReport run_replay_repeatability_check(
    const Dataset& ds,
    const SimConfig& scfg,
    const ActorCritic* model_or_null,
    const std::string& model_hash);

enum class ParityMutation {
    NONE = 0,
    MUTATE_FEE = 1,
    MUTATE_VERDICT = 2,
    MUTATE_EVENT_ORDER = 3
};

struct IndependentParityReport {
    bool exact_per_event_parity = false;
    size_t events_compared = 0;
    size_t first_divergence_step = 0;
    std::string first_divergence_field;
    std::string detail;
    int64_t ref_final_equity_cents = 0;
    int64_t native_final_equity_cents = 0;
};

// R6: Compares CausalEnv (with C17 aq_evaluate_risk) step-by-step against an independent pure-integer specification oracle.
IndependentParityReport run_independent_shadow_parity(
    const Dataset& ds,
    const SimConfig& scfg,
    const std::vector<uint32_t>& action_tape,
    ParityMutation fault = ParityMutation::NONE);

class NativeHttpServer {
public:
    explicit NativeHttpServer(ServiceConfig cfg);
    ~NativeHttpServer();

    NativeHttpServer(const NativeHttpServer&) = delete;
    NativeHttpServer& operator=(const NativeHttpServer&) = delete;

    void load_dataset(Dataset ds);
    void set_policy(const aq_risk_policy_t& policy);
    void set_override_now_epoch_s(int64_t epoch_s);
    NativeOms& oms() { return oms_; }

    HttpResponse handle_request(const HttpRequest& req);

    void start();
    void stop();
    uint16_t bound_port() const { return bound_port_; }

private:
    struct SessionInfo {
        std::string csrf;
        int64_t expires_epoch_s = 0;
    };
    struct TrainingJobInfo {
        std::string job_id;
        std::string status;  // QUEUED, RUNNING, COMPLETED, CANCELLED, FAILED
        uint32_t updates = 0;
        uint64_t seed = 26;
        std::string checkpoint_hash;
        double eval_equity_usd = 0.0;
        std::string error;
        std::atomic<bool> cancel_requested{false};
    };

    bool check_auth(const HttpRequest& req, bool require_csrf) const;
    std::string json_escape(const std::string& s) const;
    void worker_loop();

    ServiceConfig cfg_;
    NativeOms oms_;
    aq_risk_policy_t policy_{};
    Dataset dataset_;
    bool dataset_loaded_ = false;
    mutable std::mutex mu_;
    std::map<std::string, SessionInfo> sessions_;
    std::map<std::string, std::vector<int64_t>> login_attempts_;

    // R5: Out-of-thread bounded training worker
    std::mutex jobs_mu_;
    std::condition_variable jobs_cv_;
    std::vector<std::shared_ptr<TrainingJobInfo>> jobs_;
    std::deque<std::shared_ptr<TrainingJobInfo>> job_queue_;
    std::atomic<bool> worker_stop_{false};
    std::thread worker_thread_;

    std::atomic<uint64_t> req_count_{0};
    std::atomic<uint64_t> auth_fail_count_{0};
    std::atomic<bool> running_{false};
    uint16_t bound_port_ = 0;

    struct NetImpl;
    NetImpl* net_ = nullptr;
};

}  // namespace aq
