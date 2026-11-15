#include "aq/service.hpp"

#include <boost/asio.hpp>
#include <boost/beast/core.hpp>
#include <boost/beast/http.hpp>
#include <openssl/crypto.h>
#include <openssl/rand.h>

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <iomanip>
#include <set>
#include <sstream>
#include <stdexcept>

namespace aq {

namespace beast = boost::beast;
namespace http = beast::http;
namespace net = boost::asio;
using tcp = net::ip::tcp;

namespace {

constexpr size_t kMaxHttpBodyBytes = 1024 * 1024;
constexpr size_t kMaxHttpHeaderBytes = 16384;

std::string to_lower(std::string s) {
    for (char& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return s;
}

std::string random_hex(size_t bytes) {
    std::vector<unsigned char> buf(bytes);
    if (RAND_bytes(buf.data(), static_cast<int>(bytes)) != 1) {
        throw std::runtime_error("RAND_bytes failed");
    }
    std::ostringstream oss;
    oss << std::hex << std::setfill('0');
    for (unsigned char b : buf) oss << std::setw(2) << static_cast<unsigned>(b);
    return oss.str();
}

bool ct_equal(const std::string& a, const std::string& b) {
    if (a.size() != b.size() || a.empty()) return false;
    return CRYPTO_memcmp(a.data(), b.data(), a.size()) == 0;
}

std::string get_header(const HttpRequest& req, const std::string& key_lower) {
    for (const auto& kv : req.headers) {
        if (to_lower(kv.first) == key_lower) return kv.second;
    }
    return "";
}

std::string extract_cookie(const std::string& cookie_hdr, const std::string& name) {
    std::istringstream iss(cookie_hdr);
    std::string part;
    while (std::getline(iss, part, ';')) {
        size_t eq = part.find('=');
        if (eq == std::string::npos) continue;
        std::string k = part.substr(0, eq);
        std::string v = part.substr(eq + 1);
        while (!k.empty() && k.front() == ' ') k.erase(k.begin());
        while (!v.empty() && v.back() == ' ') v.pop_back();
        if (k == name) return v;
    }
    return "";
}

int64_t now_epoch_s() {
    return std::chrono::duration_cast<std::chrono::seconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

std::string reason_text(int code) {
    switch (code) {
        case 200: return "OK";
        case 400: return "Bad Request";
        case 401: return "Unauthorized";
        case 404: return "Not Found";
        case 409: return "Conflict";
        case 413: return "Payload Too Large";
        case 422: return "Unprocessable Entity";
        case 429: return "Too Many Requests";
        default: return "Internal Server Error";
    }
}

}  // namespace

ReplayRepeatabilityReport run_replay_repeatability_check(
    const Dataset& ds,
    const SimConfig& scfg,
    const ActorCritic* model_or_null,
    const std::string& model_hash) {
    auto r1 = run_replay_episode(ds, model_or_null, model_hash, scfg);
    auto r2 = run_replay_episode(ds, model_or_null, model_hash, scfg);
    ReplayRepeatabilityReport rep;
    rep.final_equity_cents = r1.final_equity_cents;
    rep.steps_compared = r1.steps;
    rep.receipt_hash = r1.receipt_hash;
    rep.exact_repeatability = (r1.final_cash_cents == r2.final_cash_cents &&
                               r1.final_shares == r2.final_shares &&
                               r1.final_equity_cents == r2.final_equity_cents &&
                               r1.risk_interventions == r2.risk_interventions &&
                               r1.trades_executed == r2.trades_executed &&
                               r1.receipt_hash == r2.receipt_hash);
    return rep;
}

IndependentParityReport run_independent_shadow_parity(
    const Dataset& ds,
    const SimConfig& scfg,
    const std::vector<uint32_t>& action_tape,
    ParityMutation fault) {
    IndependentParityReport rep;
    if (ds.bars.size() < scfg.warmup_bars + 2) {
        throw std::invalid_argument("Dataset too short for shadow parity");
    }

    // Candidate runner: real CausalEnv + C17 aq_evaluate_risk
    CausalEnv env(ds, scfg);
    env.reset(scfg.warmup_bars);

    // Independent specification oracle (pure integer cents, does not call CausalEnv or aq_evaluate_risk)
    int64_t ref_cash = scfg.initial_cash_cents;
    int64_t ref_shares = 0;
    int64_t ref_peak = scfg.initial_cash_cents;
    int64_t ref_day = ds.bars[scfg.warmup_bars].close_epoch_s / 86400;
    int64_t ref_day_start = scfg.initial_cash_cents;
    double ref_prev_dd = 0.0;
    bool fault_injected = false;
    static constexpr double kTargets[3] = {0.0, 0.25, 0.50};

    size_t max_steps = std::min(action_tape.size(), ds.bars.size() - scfg.warmup_bars - 1);
    for (size_t step = 0; step < max_steps; ++step) {
        size_t idx = scfg.warmup_bars + step;
        uint32_t act = action_tape[step];
        if (fault == ParityMutation::MUTATE_EVENT_ORDER && step + 1 < max_steps) {
            // Deliberately swap bar/action order on step 0 as negative control
            if (step == 0) act = (act + 1) % 3;
        }

        StepOutcome cand = env.step(action_tape[step]);

        int64_t ref_eq_now = ref_cash + ref_shares * ds.bars[idx].close_cents;
        int64_t bar_day = ds.bars[idx].close_epoch_s / 86400;
        if (bar_day != ref_day) {
            ref_day = bar_day;
            ref_day_start = ref_eq_now;
        }
        int64_t px_close = ds.bars[idx].close_cents;
        int64_t target_shares = static_cast<int64_t>(std::floor((static_cast<double>(ref_eq_now) * kTargets[act]) / static_cast<double>(px_close)));
        int64_t delta_shares = target_shares - ref_shares;

        bool ref_rejected = false;
        int64_t ref_exec_delta = 0;
        if (delta_shares != 0) {
            int64_t abs_d = delta_shares > 0 ? delta_shares : -delta_shares;
            int64_t daily_bps = ((ref_eq_now - ref_day_start) * 10000) / std::max<int64_t>(1, ref_day_start);
            int64_t dd_bps = static_cast<int64_t>(std::llround(ref_prev_dd * 10000.0));
            bool is_rr = (delta_shares < 0 && ref_shares >= abs_d);
            bool pre_ok = true;
            if (!is_rr) {
                if (scfg.halted) pre_ok = false;
                if (daily_bps <= -scfg.risk_policy.max_daily_loss_bps) pre_ok = false;
                if (dd_bps >= scfg.risk_policy.max_total_loss_bps) pre_ok = false;
                int64_t pre_notional = abs_d * px_close;
                if (delta_shares > 0 && pre_notional > ref_cash) pre_ok = false;
                if (pre_notional * 10000 > ref_eq_now * scfg.risk_policy.max_order_notional_bps) pre_ok = false;
                int64_t next_sym = (ref_shares * px_close) + pre_notional;
                if (delta_shares > 0 && next_sym * 10000 > ref_eq_now * scfg.risk_policy.max_concentration_bps) pre_ok = false;
                if (delta_shares > 0 && next_sym * 10000 > ref_eq_now * scfg.risk_policy.max_gross_exposure_bps) pre_ok = false;
            }
            if (fault == ParityMutation::MUTATE_VERDICT && !fault_injected) {
                pre_ok = !pre_ok;
                fault_injected = true;
            }
            if (!pre_ok) {
                ref_rejected = true;
            } else {
                int64_t raw_open = ds.bars[idx + 1].open_cents;
                int64_t adv_bps = scfg.half_spread_bps + scfg.slippage_bps;
                int64_t fill_px = delta_shares > 0
                    ? (raw_open * (10000 + adv_bps) + 9999) / 10000
                    : (raw_open * (10000 - adv_bps)) / 10000;
                int64_t notional = abs_d * fill_px;
                int64_t fee = (notional * scfg.commission_bps + 9999) / 10000;
                if (fault == ParityMutation::MUTATE_FEE && !fault_injected) {
                    fee += 100;
                    fault_injected = true;
                }
                if (delta_shares > 0) {
                    bool exec_ok = (notional + fee <= ref_cash) &&
                                   (notional * 10000 <= ref_eq_now * scfg.risk_policy.max_order_notional_bps) &&
                                   (((ref_shares * px_close) + notional) * 10000 <= ref_eq_now * scfg.risk_policy.max_concentration_bps) &&
                                   (((ref_shares * px_close) + notional) * 10000 <= ref_eq_now * scfg.risk_policy.max_gross_exposure_bps);
                    if (exec_ok) {
                        ref_cash -= (notional + fee);
                        ref_shares += abs_d;
                        ref_exec_delta = abs_d;
                    } else {
                        ref_rejected = true;
                    }
                } else {
                    ref_cash += (notional - fee);
                    ref_shares -= abs_d;
                    ref_exec_delta = -abs_d;
                }
            }
        }
        int64_t ref_eq_next = ref_cash + ref_shares * ds.bars[idx + 1].close_cents;
        if (ref_eq_next > ref_peak) ref_peak = ref_eq_next;
        ref_prev_dd = std::max(0.0, static_cast<double>(ref_peak - ref_eq_next) / static_cast<double>(std::max<int64_t>(1, ref_peak)));

        rep.events_compared += 1;
        if (cand.risk_rejected != ref_rejected) {
            rep.first_divergence_step = step;
            rep.first_divergence_field = "risk_rejected";
            rep.detail = "Verdict mismatch";
            rep.ref_final_equity_cents = ref_eq_next;
            rep.native_final_equity_cents = cand.equity_cents;
            return rep;
        }
        if (cand.executed_shares_delta != ref_exec_delta) {
            rep.first_divergence_step = step;
            rep.first_divergence_field = "executed_shares_delta";
            rep.detail = "Executed delta mismatch";
            rep.ref_final_equity_cents = ref_eq_next;
            rep.native_final_equity_cents = cand.equity_cents;
            return rep;
        }
        if (cand.cash_cents != ref_cash || cand.shares != ref_shares || cand.equity_cents != ref_eq_next) {
            rep.first_divergence_step = step;
            rep.first_divergence_field = (cand.cash_cents != ref_cash) ? "cash_cents" : ((cand.shares != ref_shares) ? "shares" : "equity_cents");
            rep.detail = "Ledger accounting mismatch";
            rep.ref_final_equity_cents = ref_eq_next;
            rep.native_final_equity_cents = cand.equity_cents;
            return rep;
        }
        rep.ref_final_equity_cents = ref_eq_next;
        rep.native_final_equity_cents = cand.equity_cents;
    }

    rep.exact_per_event_parity = true;
    return rep;
}

struct NativeHttpServer::NetImpl {
    net::io_context ioc{1};
    tcp::acceptor acceptor{ioc};
    std::thread io_thread;

    explicit NetImpl(const std::string& host, uint16_t port) {
        tcp::endpoint ep(net::ip::make_address(host), port);
        acceptor.open(ep.protocol());
        acceptor.set_option(net::socket_base::reuse_address(true));
        acceptor.bind(ep);
        acceptor.listen(net::socket_base::max_listen_connections);
    }
};

NativeHttpServer::NativeHttpServer(ServiceConfig cfg)
    : cfg_(std::move(cfg)), oms_(cfg_.db_path) {
    policy_ = default_sim_config().risk_policy;
    policy_.max_order_notional_bps = 2500;
    policy_.max_concentration_bps = 3500;
    policy_.max_daily_loss_bps = 500;
    policy_.max_total_loss_bps = 1000;

    // R1: Non-destructive create-if-absent seed. Never overwrites existing balances.
    oms_.seed_account_if_absent(cfg_.env, 10000000, 10000000);

    // R1/R4: Only acquire writer lease when explicitly enabled; read-only observer NEVER revokes another owner's lease!
    if (cfg_.enable_paper_writer) {
        auto info = oms_.writer_lease_info(cfg_.env);
        uint64_t next_epoch = info.authority_epoch + 1;
        oms_.try_acquire_writer_lease(cfg_.env, cfg_.writer_owner_id, info.authority_epoch, next_epoch);
        cfg_.writer_epoch = next_epoch;
    }

    worker_thread_ = std::thread(&NativeHttpServer::worker_loop, this);
}

NativeHttpServer::~NativeHttpServer() {
    stop();
    worker_stop_.store(true);
    jobs_cv_.notify_all();
    if (worker_thread_.joinable()) worker_thread_.join();
}

void NativeHttpServer::worker_loop() {
    while (true) {
        std::shared_ptr<TrainingJobInfo> job;
        {
            std::unique_lock<std::mutex> lk(jobs_mu_);
            jobs_cv_.wait(lk, [&]() { return worker_stop_.load() || !job_queue_.empty(); });
            if (worker_stop_.load() && job_queue_.empty()) break;
            job = job_queue_.front();
            job_queue_.pop_front();
        }
        if (job->cancel_requested.load()) {
            std::lock_guard<std::mutex> lk(jobs_mu_);
            job->status = "CANCELLED";
            continue;
        }
        {
            std::lock_guard<std::mutex> lk(jobs_mu_);
            job->status = "RUNNING";
        }
        try {
            Dataset ds_copy;
            {
                std::lock_guard<std::mutex> lk(mu_);
                ds_copy = dataset_;
            }
            TrainConfig tcfg;
            tcfg.seed = job->seed;
            tcfg.updates = job->updates;
            tcfg.rollout_steps = 32;
            std::string out_dir = cfg_.db_path + ".jobs/" + job->job_id;
            auto sum = train_ppo(ds_copy, out_dir, tcfg);
            std::lock_guard<std::mutex> lk(jobs_mu_);
            job->status = job->cancel_requested.load() ? "CANCELLED" : "COMPLETED";
            job->checkpoint_hash = sum.checkpoint_hash;
            job->eval_equity_usd = sum.final_eval_equity_usd;
        } catch (const std::exception& e) {
            std::lock_guard<std::mutex> lk(jobs_mu_);
            job->status = "FAILED";
            job->error = e.what();
        }
    }
}

void NativeHttpServer::load_dataset(Dataset ds) {
    std::lock_guard<std::mutex> lk(mu_);
    dataset_ = std::move(ds);
    dataset_loaded_ = !dataset_.bars.empty();
}

void NativeHttpServer::set_policy(const aq_risk_policy_t& policy) {
    std::lock_guard<std::mutex> lk(mu_);
    policy_ = policy;
}

void NativeHttpServer::set_override_now_epoch_s(int64_t epoch_s) {
    std::lock_guard<std::mutex> lk(mu_);
    cfg_.override_now_epoch_s = epoch_s;
}

std::string NativeHttpServer::json_escape(const std::string& s) const {
    std::ostringstream oss;
    for (char c : s) {
        if (c == '"') oss << "\\\"";
        else if (c == '\\') oss << "\\\\";
        else if (c == '\n') oss << "\\n";
        else if (c == '\r') oss << "\\r";
        else if (static_cast<unsigned char>(c) < 0x20) oss << ' ';
        else oss << c;
    }
    return oss.str();
}

bool NativeHttpServer::check_auth(const HttpRequest& req, bool require_csrf) const {
    std::string x_tok = get_header(req, "x-aegis-token");
    if (!x_tok.empty() && ct_equal(x_tok, cfg_.auth_token)) return true;

    std::string auth = get_header(req, "authorization");
    if (auth.rfind("Bearer ", 0) == 0 && ct_equal(auth.substr(7), cfg_.auth_token)) {
        return true;
    }

    std::string sid = extract_cookie(get_header(req, "cookie"), "aegis_session");
    if (sid.empty()) return false;
    std::lock_guard<std::mutex> lk(mu_);
    auto it = sessions_.find(sid);
    if (it == sessions_.end() || it->second.expires_epoch_s < now_epoch_s()) return false;
    if (require_csrf) {
        std::string csrf = get_header(req, "x-csrf-token");
        if (!ct_equal(csrf, it->second.csrf)) return false;
    }
    return true;
}

HttpResponse NativeHttpServer::handle_request(const HttpRequest& req) {
    req_count_.fetch_add(1);
    if (req.body.size() > kMaxHttpBodyBytes) {
        return {413, "application/json", {}, "{\"detail\":\"Request body too large\"}"};
    }

    const std::string& m = req.method;
    const std::string& p = req.path;

    if (m == "GET" && p == "/api/health") {
        return {200, "application/json", {},
                "{\"status\":\"ok\",\"version\":\"0.3.0-native\",\"mode\":\"PAPER_ONLY\","
                "\"live_execution\":false,\"backend\":\"native_cpp\"}"};
    }

    if (m == "GET" && p == "/api/ready") {
        bool ds_ok = false;
        {
            std::lock_guard<std::mutex> lk(mu_);
            ds_ok = dataset_loaded_;
        }
        std::string acct_err;
        bool acct_ok = oms_.verify_accounting_consistency(cfg_.env, &acct_err);
        auto lease = oms_.writer_lease_info(cfg_.env);
        auto in_flight = oms_.recover_in_flight(cfg_.env);
        std::ostringstream oss;
        oss << "{\"process\":\"ok\",\"backend\":\"native_cpp\",\"data_mode\":\"" << json_escape(cfg_.data_namespace)
            << "\",\"execution_mode\":\"" << json_escape(cfg_.env)
            << "\",\"live_execution\":false,\"dataset_loaded\":" << (ds_ok ? "true" : "false")
            << ",\"accounting_consistent\":" << (acct_ok ? "true" : "false")
            << ",\"writer_lease_active\":" << (lease.active ? "true" : "false")
            << ",\"in_flight_unknown\":" << in_flight.size()
            << ",\"abi_version\":" << AQ_CORE_ABI_VERSION << "}";
        return {acct_ok ? 200 : 503, "application/json", {}, oss.str()};
    }

    if (m == "POST" && p == "/api/login") {
        JsonValue j;
        try {
            j = parse_json_strict(req.body);
            if (!j.is_object()) return {400, "application/json", {}, "{\"detail\":\"Expected JSON object\"}"};
        } catch (const std::exception& e) {
            return {400, "application/json", {}, "{\"detail\":\"" + json_escape(e.what()) + "\"}"};
        }
        const auto& obj = j.as_object();
        auto it = obj.find("token");
        if (it == obj.end() || !it->second.is_string()) {
            return {400, "application/json", {}, "{\"detail\":\"Missing string token\"}"};
        }

        int64_t now = now_epoch_s();
        {
            std::lock_guard<std::mutex> lk(mu_);
            auto& att = login_attempts_[req.remote_addr];
            att.erase(std::remove_if(att.begin(), att.end(), [&](int64_t t) { return now - t >= 60; }), att.end());
            if (att.size() >= 10) {
                return {429, "application/json", {}, "{\"detail\":\"Too many login attempts\"}"};
            }
            att.push_back(now);
        }
        if (!ct_equal(it->second.as_string(), cfg_.auth_token)) {
            auth_fail_count_.fetch_add(1);
            return {401, "application/json", {}, "{\"detail\":\"Invalid access token\"}"};
        }
        std::string sid = random_hex(24);
        std::string csrf = random_hex(16);
        {
            std::lock_guard<std::mutex> lk(mu_);
            sessions_[sid] = SessionInfo{csrf, now + 8 * 3600};
        }
        std::map<std::string, std::string> hdrs;
        hdrs["Set-Cookie"] = "aegis_session=" + sid + "; HttpOnly; SameSite=Strict; Max-Age=28800; Path=/";
        return {200, "application/json", hdrs, "{\"csrf\":\"" + csrf + "\",\"expires_in\":28800}"};
    }

    if (m == "GET" && p == "/api/session") {
        std::string sid = extract_cookie(get_header(req, "cookie"), "aegis_session");
        std::lock_guard<std::mutex> lk(mu_);
        auto it = sessions_.find(sid);
        if (it == sessions_.end() || it->second.expires_epoch_s < now_epoch_s()) {
            return {401, "application/json", {}, "{\"detail\":\"Unauthorized\"}"};
        }
        return {200, "application/json", {}, "{\"csrf\":\"" + it->second.csrf + "\"}"};
    }

    if (m == "POST" && p == "/api/logout") {
        std::string sid = extract_cookie(get_header(req, "cookie"), "aegis_session");
        {
            std::lock_guard<std::mutex> lk(mu_);
            sessions_.erase(sid);
        }
        std::map<std::string, std::string> hdrs;
        hdrs["Set-Cookie"] = "aegis_session=; HttpOnly; SameSite=Strict; Max-Age=0; Path=/";
        return {200, "application/json", hdrs, "{\"ok\":true}"};
    }

    bool is_post = (m == "POST");
    if (!check_auth(req, is_post)) {
        auth_fail_count_.fetch_add(1);
        return {401, "application/json", {}, "{\"detail\":\"Authentication or CSRF check failed\"}"};
    }

    if (m == "GET" && p == "/api/state") {
        auto snap = oms_.account_snapshot(cfg_.env);
        auto cs = oms_.control_state();
        std::ostringstream oss;
        oss << "{\"mode\":\"" << json_escape(cfg_.data_namespace) << "_MARKET\",\"execution_mode\":\""
            << json_escape(cfg_.env) << "\",\"live_execution\":false,\"halted\":" << (cs.halted ? "true" : "false")
            << ",\"control_state\":{\"halted\":" << (cs.halted ? "true" : "false")
            << ",\"control_epoch\":" << cs.control_epoch
            << ",\"halt_reason\":\"" << json_escape(cs.reason) << "\"}"
            << ",\"account\":{\"cash_cents\":" << snap.cash_cents
            << ",\"equity_cents\":" << snap.equity_cents
            << ",\"reserved_buy_cents\":" << snap.total_reserved_buy_cents
            << ",\"account_revision\":" << snap.account_revision
            << ",\"integrity_ok\":" << (snap.integrity_ok ? "true" : "false") << "}}";
        return {200, "application/json", {}, oss.str()};
    }

    if (m == "POST" && p == "/api/reset") {
        try {
            JsonValue j = parse_json_strict(req.body);
            const auto& obj = j.as_object();
            auto sc_it = obj.find("scope");
            if (sc_it == obj.end() || !sc_it->second.is_string() || sc_it->second.as_string().empty()) {
                return {400, "application/json", {}, "{\"detail\":\"Explicit non-empty scope required for reset\"}"};
            }
            int64_t cash = 10000000;
            if (auto it = obj.find("cash_cents"); it != obj.end()) cash = it->second.as_int64();
            if (cash <= 0) return {400, "application/json", {}, "{\"detail\":\"cash_cents must be > 0\"}"};
            oms_.reset_account(cfg_.env, cash, cash, sc_it->second.as_string());
            return {200, "application/json", {}, "{\"reset\":true}"};
        } catch (const std::exception& e) {
            return {400, "application/json", {}, "{\"detail\":\"" + json_escape(e.what()) + "\"}"};
        }
    }

    if (m == "GET" && p == "/api/market/feed/status") {
        bool ds_ok = false;
        std::string sym, src;
        size_t nbars = 0;
        {
            std::lock_guard<std::mutex> lk(mu_);
            ds_ok = dataset_loaded_;
            sym = dataset_.symbol;
            src = dataset_.source;
            nbars = dataset_.bars.size();
        }
        std::ostringstream oss;
        oss << "{\"state\":\"" << (ds_ok ? "READY" : "UNAVAILABLE") << "\",\"namespace\":\""
            << json_escape(cfg_.data_namespace) << "\",\"source\":\"" << json_escape(src)
            << "\",\"symbol\":\"" << json_escape(sym) << "\",\"bars\":" << nbars << "}";
        return {200, "application/json", {}, oss.str()};
    }

    if (m == "GET" && p.rfind("/api/market/", 0) == 0) {
        std::string sym = p.substr(std::strlen("/api/market/"));
        std::lock_guard<std::mutex> lk(mu_);
        if (!dataset_loaded_ || dataset_.symbol != sym) {
            return {404, "application/json", {}, "{\"detail\":\"Unknown symbol or dataset not loaded\"}"};
        }
        size_t start = dataset_.bars.size() > 240 ? dataset_.bars.size() - 240 : 0;
        std::ostringstream oss;
        oss << std::fixed << std::setprecision(2);
        oss << "{\"symbol\":\"" << json_escape(sym) << "\",\"source\":\"" << json_escape(dataset_.source)
            << "\",\"source_mode\":\"" << json_escape(cfg_.data_namespace) << "\",\"bars\":[";
        for (size_t i = start; i < dataset_.bars.size(); ++i) {
            const auto& b = dataset_.bars[i];
            if (i > start) oss << ",";
            oss << "{\"open_at\":\"" << b.open_at << "\",\"timestamp\":\"" << b.timestamp
                << "\",\"available_at\":\"" << b.available_at << "\",\"open\":" << b.open
                << ",\"high\":" << b.high << ",\"low\":" << b.low << ",\"close\":" << b.close
                << ",\"volume\":" << b.volume << "}";
        }
        oss << "]}";
        return {200, "application/json", {}, oss.str()};
    }

    if (m == "POST" && p == "/api/risk/halt") {
        try {
            JsonValue j = parse_json_strict(req.body);
            const auto& obj = j.as_object();
            std::string action = "halt";
            if (auto it = obj.find("action"); it != obj.end()) action = it->second.as_string();
            std::string reason = "";
            if (auto it = obj.find("reason"); it != obj.end()) reason = it->second.as_string();
            if (action == "resume") {
                auto cs = oms_.resume(reason.empty() ? "operator_resume" : reason);
                std::ostringstream oss;
                oss << "{\"halted\":false,\"control_epoch\":" << cs.control_epoch << "}";
                return {200, "application/json", {}, oss.str()};
            }
            if (action != "halt") {
                return {400, "application/json", {}, "{\"detail\":\"Invalid halt action\"}"};
            }
            auto hr = oms_.halt(cfg_.env, reason.empty() ? "operator_halt" : reason);
            std::ostringstream oss;
            oss << "{\"halted\":true,\"control_epoch\":" << hr.control_epoch
                << ",\"in_flight_unknown\":" << hr.in_flight_unknown_count
                << ",\"note\":\"" << json_escape(hr.note) << "\"}";
            return {200, "application/json", {}, oss.str()};
        } catch (const std::exception& e) {
            return {400, "application/json", {}, "{\"detail\":\"" + json_escape(e.what()) + "\"}"};
        }
    }

    if (m == "GET" && p == "/api/broker/paper-orders") {
        auto orders = oms_.list_orders(cfg_.env);
        std::ostringstream oss;
        oss << "{\"orders\":[";
        for (size_t i = 0; i < orders.size(); ++i) {
            const auto& o = orders[i];
            if (i > 0) oss << ",";
            oss << "{\"client_order_id\":\"" << json_escape(o.client_order_id)
                << "\",\"account_id\":\"" << json_escape(o.account_id)
                << "\",\"symbol\":\"" << json_escape(o.symbol)
                << "\",\"side\":\"" << (o.side == AQ_SIDE_BUY ? "BUY" : "SELL")
                << "\",\"order_type\":\"" << (o.order_type == AQ_ORDER_MARKET ? "MARKET" : "LIMIT")
                << "\",\"qty\":" << o.qty << ",\"filled_qty\":" << o.filled_qty
                << ",\"limit_price_cents\":" << o.limit_price_cents
                << ",\"stop_cents\":" << o.stop_cents
                << ",\"target_cents\":" << o.target_cents
                << ",\"reserved_cash_cents\":" << o.reserved_cash_cents
                << ",\"state\":\"" << order_state_to_str(o.state)
                << "\",\"reason_mask\":" << o.reason_mask << "}";
        }
        oss << "]}";
        return {200, "application/json", {}, oss.str()};
    }

    if (m == "POST" && p == "/api/orders") {
        // R2: Strict bounded JSON parsing & schema validation. Never reinterpret invalid fields!
        try {
            JsonValue j = parse_json_strict(req.body);
            const auto& obj = j.as_object();
            static const std::set<std::string> kAllowed = {
                "client_order_id", "account_id", "symbol", "side", "order_type",
                "qty", "price_cents", "stop_cents", "target_cents", "time_in_force"
            };
            for (const auto& kv : obj) {
                if (!kAllowed.count(kv.first)) {
                    return {400, "application/json", {}, "{\"detail\":\"Unsupported field: " + json_escape(kv.first) + "\"}"};
                }
            }
            auto req_str = [&](const char* k) -> std::string {
                auto it = obj.find(k);
                if (it == obj.end() || !it->second.is_string() || it->second.as_string().empty()) {
                    throw std::runtime_error(std::string("Missing or invalid string field: ") + k);
                }
                return it->second.as_string();
            };
            std::string cid = req_str("client_order_id");
            if (!is_valid_identifier(cid, 128)) {
                return {400, "application/json", {}, "{\"detail\":\"Invalid client_order_id characters or length\"}"};
            }
            std::string sym = req_str("symbol");
            if (!is_valid_identifier(sym, AQ_MAX_SYMBOL_LEN - 1)) {
                return {400, "application/json", {}, "{\"detail\":\"Invalid symbol characters or exceeds AQ_MAX_SYMBOL_LEN\"}"};
            }
            std::string side_s = req_str("side");
            uint8_t side_u = 0;
            if (side_s == "BUY") side_u = AQ_SIDE_BUY;
            else if (side_s == "SELL") side_u = AQ_SIDE_SELL;
            else return {400, "application/json", {}, "{\"detail\":\"Invalid side; must be BUY or SELL\"}"};

            std::string otype_s = "LIMIT";
            if (auto it = obj.find("order_type"); it != obj.end()) otype_s = it->second.as_string();
            uint8_t otype_u = 0;
            if (otype_s == "LIMIT") otype_u = AQ_ORDER_LIMIT;
            else if (otype_s == "MARKET") otype_u = AQ_ORDER_MARKET;
            else return {400, "application/json", {}, "{\"detail\":\"Unsupported order_type\"}"};

            std::string tif = "DAY";
            if (auto it = obj.find("time_in_force"); it != obj.end()) {
                tif = it->second.as_string();
                if (tif != "DAY" && tif != "IOC") {
                    return {400, "application/json", {}, "{\"detail\":\"Unsupported time_in_force\"}"};
                }
            }

            std::string account_id = "default";
            if (auto it = obj.find("account_id"); it != obj.end()) {
                account_id = it->second.as_string();
                if (!is_valid_identifier(account_id, 128)) {
                    return {400, "application/json", {}, "{\"detail\":\"Invalid account_id characters or length\"}"};
                }
            }

            auto it_qty = obj.find("qty");
            if (it_qty == obj.end() || !it_qty->second.is_int64() || it_qty->second.as_int64() <= 0) {
                return {400, "application/json", {}, "{\"detail\":\"qty must be a positive exact integer\"}"};
            }
            int64_t qty = it_qty->second.as_int64();

            auto it_px = obj.find("price_cents");
            if (it_px == obj.end() || !it_px->second.is_int64() || it_px->second.as_int64() <= 0) {
                return {400, "application/json", {}, "{\"detail\":\"price_cents must be a positive exact integer\"}"};
            }
            int64_t price_cents = it_px->second.as_int64();

            int64_t stop_cents = 0;
            if (auto it = obj.find("stop_cents"); it != obj.end()) {
                if (!it->second.is_int64() || it->second.as_int64() < 0) {
                    return {400, "application/json", {}, "{\"detail\":\"stop_cents must be non-negative integer\"}"};
                }
                stop_cents = it->second.as_int64();
            }
            int64_t target_cents = 0;
            if (auto it = obj.find("target_cents"); it != obj.end()) {
                if (!it->second.is_int64() || it->second.as_int64() < 0) {
                    return {400, "application/json", {}, "{\"detail\":\"target_cents must be non-negative integer\"}"};
                }
                target_cents = it->second.as_int64();
            }

            // Server-owned freshness calculation from loaded market snapshot (never literal 0!).
            int64_t data_age_ms = 0;
            aq_risk_policy_t pol{};
            {
                std::lock_guard<std::mutex> lk(mu_);
                pol = policy_;
                if (!dataset_loaded_ || dataset_.symbol != sym || dataset_.bars.empty()) {
                    data_age_ms = pol.max_data_age_ms + 1;  // Missing market snapshot -> stale fail-closed
                } else {
                    int64_t now_s = (cfg_.override_now_epoch_s > 0) ? cfg_.override_now_epoch_s : now_epoch_s();
                    int64_t avail_s = dataset_.bars.back().available_epoch_s;
                    int64_t age_s = std::max<int64_t>(0, now_s - avail_s);
                    data_age_ms = age_s * 1000;
                }
            }

            aq_instrument_spec_t spec{};
            spec.abi_version = AQ_CORE_ABI_VERSION;
            spec.struct_size = sizeof(spec);
            std::snprintf(spec.symbol, sizeof(spec.symbol), "%s", sym.c_str());
            std::snprintf(spec.currency, sizeof(spec.currency), "USD");
            spec.price_scale = 100;
            spec.qty_scale = 1;
            spec.tick_size = 1;

            aq_order_intent_t intent{};
            intent.abi_version = AQ_CORE_ABI_VERSION;
            intent.struct_size = sizeof(intent);
            std::snprintf(intent.symbol, sizeof(intent.symbol), "%s", sym.c_str());
            intent.side = side_u;
            intent.order_type = otype_u;
            intent.qty = qty;
            intent.price_cents = price_cents;
            intent.stop_cents = stop_cents;
            intent.target_cents = target_cents;
            intent.data_age_ms = data_age_ms;

            auto sub = oms_.submit_intent(cfg_.env, cid, spec, intent, pol, 25, account_id, tif);
            if (sub.conflict) {
                return {409, "application/json", {}, "{\"detail\":\"client_order_id conflict with different order economics\"}"};
            }
            std::ostringstream oss;
            oss << "{\"approved\":" << (sub.accepted_for_reservation ? "true" : "false")
                << ",\"duplicate_idempotent\":" << (sub.duplicate_idempotent ? "true" : "false")
                << ",\"client_order_id\":\"" << json_escape(sub.order.client_order_id)
                << "\",\"side\":\"" << side_s << "\",\"order_type\":\"" << otype_s
                << "\",\"qty\":" << sub.order.qty << ",\"stop_cents\":" << sub.order.stop_cents
                << ",\"target_cents\":" << sub.order.target_cents
                << ",\"state\":\"" << order_state_to_str(sub.order.state)
                << "\",\"reason_mask\":" << sub.decision.reason_mask << "}";
            return {sub.accepted_for_reservation ? 200 : 422, "application/json", {}, oss.str()};
        } catch (const std::exception& e) {
            return {400, "application/json", {}, "{\"detail\":\"" + json_escape(e.what()) + "\"}"};
        }
    }

    if (m == "POST" && p == "/api/training/jobs") {
        {
            std::lock_guard<std::mutex> lk(mu_);
            if (!dataset_loaded_) {
                return {400, "application/json", {}, "{\"detail\":\"No dataset loaded for training\"}"};
            }
        }
        uint32_t updates = 2;
        uint64_t seed = 26;
        if (!req.body.empty()) {
            try {
                JsonValue j = parse_json_strict(req.body);
                const auto& obj = j.as_object();
                if (auto it = obj.find("updates"); it != obj.end()) {
                    updates = static_cast<uint32_t>(std::clamp<int64_t>(it->second.as_int64(), 1, 16));
                }
                if (auto it = obj.find("seed"); it != obj.end()) {
                    seed = static_cast<uint64_t>(std::max<int64_t>(1, it->second.as_int64()));
                }
            } catch (const std::exception& e) {
                return {400, "application/json", {}, "{\"detail\":\"" + json_escape(e.what()) + "\"}"};
            }
        }
        auto job = std::make_shared<TrainingJobInfo>();
        job->job_id = "job-" + random_hex(6);
        job->status = "QUEUED";
        job->updates = updates;
        job->seed = seed;
        {
            std::lock_guard<std::mutex> lk(jobs_mu_);
            jobs_.push_back(job);
            job_queue_.push_back(job);
        }
        jobs_cv_.notify_one();

        std::ostringstream oss;
        oss << "{\"job_id\":\"" << job->job_id << "\",\"status\":\"QUEUED\",\"updates\":" << job->updates << "}";
        return {200, "application/json", {}, oss.str()};
    }

    if (m == "POST" && p.rfind("/api/training/jobs/", 0) == 0 && p.size() > 26 && p.substr(p.size() - 7) == "/cancel") {
        std::string jid = p.substr(19, p.size() - 19 - 7);
        std::lock_guard<std::mutex> lk(jobs_mu_);
        for (auto& j : jobs_) {
            if (j->job_id == jid) {
                j->cancel_requested.store(true);
                if (j->status == "QUEUED") j->status = "CANCELLED";
                return {200, "application/json", {}, "{\"job_id\":\"" + jid + "\",\"cancel_requested\":true}"};
            }
        }
        return {404, "application/json", {}, "{\"detail\":\"Job not found\"}"};
    }

    if (m == "GET" && p == "/api/training/jobs") {
        std::lock_guard<std::mutex> lk(jobs_mu_);
        std::ostringstream oss;
        oss << "{\"jobs\":[";
        for (size_t i = 0; i < jobs_.size(); ++i) {
            const auto& j = jobs_[i];
            if (i > 0) oss << ",";
            oss << "{\"job_id\":\"" << j->job_id << "\",\"status\":\"" << j->status
                << "\",\"updates\":" << j->updates
                << ",\"checkpoint_hash\":\"" << j->checkpoint_hash
                << "\",\"eval_equity_usd\":" << j->eval_equity_usd << "}";
        }
        oss << "]}";
        return {200, "application/json", {}, oss.str()};
    }

    if (m == "GET" && p == "/api/metrics") {
        std::ostringstream oss;
        oss << "{\"requests_total\":" << req_count_.load()
            << ",\"auth_failures_total\":" << auth_fail_count_.load() << "}";
        return {200, "application/json", {}, oss.str()};
    }

    return {404, "application/json", {}, "{\"detail\":\"Not found\"}"};
}

void NativeHttpServer::start() {
    if (running_.load()) return;
    net_ = new NetImpl(cfg_.host, cfg_.port);
    bound_port_ = net_->acceptor.local_endpoint().port();
    running_.store(true);

    auto do_accept = std::make_shared<std::function<void()>>();
    std::weak_ptr<std::function<void()>> weak_accept = do_accept;
    *do_accept = [this, weak_accept]() {
        auto strong = weak_accept.lock();
        if (!strong) return;
        net_->acceptor.async_accept([this, strong, weak_accept](beast::error_code ec, tcp::socket sock) {
            if (!ec && running_.load()) {
                auto stream = std::make_shared<beast::tcp_stream>(std::move(sock));
                auto buf = std::make_shared<beast::flat_buffer>();
                auto parser = std::make_shared<http::request_parser<http::string_body>>();
                parser->header_limit(kMaxHttpHeaderBytes);
                parser->body_limit(kMaxHttpBodyBytes);
                stream->expires_after(std::chrono::milliseconds(cfg_.io_timeout_ms));

                http::async_read(*stream, *buf, *parser,
                    [this, stream, buf, parser](beast::error_code rec, std::size_t) {
                        HttpResponse resp;
                        if (rec) {
                            if (rec == http::error::end_of_stream ||
                                rec == beast::error::timeout ||
                                rec == boost::asio::error::operation_aborted ||
                                rec == boost::asio::error::eof ||
                                rec == boost::asio::error::connection_reset ||
                                rec == boost::asio::error::broken_pipe) {
                                beast::error_code ig;
                                stream->socket().shutdown(tcp::socket::shutdown_both, ig);
                                return;
                            }
                            resp = {400, "application/json", {}, "{\"detail\":\"" + json_escape(rec.message()) + "\"}"};
                        } else {
                            try {
                                const auto& breq = parser->get();
                                HttpRequest req;
                                req.method = std::string(breq.method_string());
                                req.path = std::string(breq.target());
                                req.body = breq.body();
                                beast::error_code ep_ec;
                                auto ep = stream->socket().remote_endpoint(ep_ec);
                                if (!ep_ec) req.remote_addr = ep.address().to_string();
                                for (const auto& f : breq) {
                                    req.headers[std::string(f.name_string())] = std::string(f.value());
                                }
                                resp = handle_request(req);
                            } catch (const std::exception& e) {
                                resp = {400, "application/json", {}, "{\"detail\":\"" + json_escape(e.what()) + "\"}"};
                            }
                        }

                        auto bres = std::make_shared<http::response<http::string_body>>(
                            static_cast<http::status>(resp.status_code), 11);
                        bres->reason(reason_text(resp.status_code));
                        bres->set(http::field::content_type, resp.content_type);
                        bres->set(http::field::connection, "close");
                        for (const auto& kv : resp.headers) {
                            bres->set(kv.first, kv.second);
                        }
                        bres->body() = std::move(resp.body);
                        bres->prepare_payload();

                        stream->expires_after(std::chrono::milliseconds(cfg_.io_timeout_ms));
                        http::async_write(*stream, *bres,
                            [stream, bres](beast::error_code, std::size_t) {
                                beast::error_code ig;
                                stream->socket().shutdown(tcp::socket::shutdown_both, ig);
                            });
                    });
            }
            if (running_.load()) {
                if (auto next = weak_accept.lock()) (*next)();
            }
        });
    };
    (*do_accept)();
    net_->io_thread = std::thread([this]() { net_->ioc.run(); });
}

void NativeHttpServer::stop() {
    if (!running_.exchange(false)) return;
    if (net_) {
        net::post(net_->ioc, [this]() {
            beast::error_code ig;
            net_->acceptor.close(ig);
        });
        net_->ioc.stop();
        if (net_->io_thread.joinable()) net_->io_thread.join();
        delete net_;
        net_ = nullptr;
    }
}

}  // namespace aq
