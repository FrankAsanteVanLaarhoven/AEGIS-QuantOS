#pragma once

#include "aq/data.hpp"
#include "aq/oms.hpp"
#include "aq/rl.hpp"

#include <atomic>
#include <cstdint>
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
    bool enable_paper_writer = false;  // C5: disabled by default until qualified
    std::string host = "127.0.0.1";
    uint16_t port = 0;  // 0 = ephemeral port
};

struct ShadowComparisonReport {
    bool exact_accounting_parity = false;
    bool exact_verdict_parity = false;
    int64_t ref_final_equity_cents = 0;
    int64_t native_final_equity_cents = 0;
    uint64_t ref_risk_interventions = 0;
    uint64_t native_risk_interventions = 0;
    uint64_t steps_compared = 0;
    std::string ref_receipt_hash;
    std::string native_receipt_hash;
};

ShadowComparisonReport run_shadow_parity_check(
    const Dataset& ds,
    const SimConfig& scfg,
    const ActorCritic* model_or_null,
    const std::string& model_hash);

class NativeHttpServer {
public:
    explicit NativeHttpServer(ServiceConfig cfg);
    ~NativeHttpServer();

    NativeHttpServer(const NativeHttpServer&) = delete;
    NativeHttpServer& operator=(const NativeHttpServer&) = delete;

    void load_dataset(Dataset ds);
    void set_policy(const aq_risk_policy_t& policy);
    NativeOms& oms() { return oms_; }

    // Direct in-process request dispatch (deterministic, no socket overhead).
    HttpResponse handle_request(const HttpRequest& req);

    // Real POSIX TCP HTTP/1.1 listener.
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
        std::string status;
        uint32_t updates = 0;
        std::string checkpoint_hash;
        double eval_equity_usd = 0.0;
    };

    bool check_auth(const HttpRequest& req, bool require_csrf) const;
    std::string json_escape(const std::string& s) const;

    ServiceConfig cfg_;
    NativeOms oms_;
    aq_risk_policy_t policy_{};
    Dataset dataset_;
    bool dataset_loaded_ = false;
    mutable std::mutex mu_;
    std::map<std::string, SessionInfo> sessions_;
    std::map<std::string, std::vector<int64_t>> login_attempts_;
    std::vector<TrainingJobInfo> jobs_;
    std::atomic<uint64_t> req_count_{0};
    std::atomic<uint64_t> auth_fail_count_{0};
    std::atomic<bool> running_{false};
    int listen_fd_ = -1;
    uint16_t bound_port_ = 0;
    std::thread accept_thread_;
};

}  // namespace aq
