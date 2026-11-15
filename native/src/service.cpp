#include "aq/service.hpp"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <openssl/crypto.h>
#include <openssl/rand.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <iomanip>
#include <sstream>
#include <stdexcept>

namespace aq {

namespace {

constexpr size_t kMaxHttpBodyBytes = 1024 * 1024;

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

std::string json_str_field(const std::string& body, const std::string& key) {
    std::string pat = "\"" + key + "\"";
    size_t pos = body.find(pat);
    if (pos == std::string::npos) return "";
    pos = body.find(':', pos + pat.size());
    if (pos == std::string::npos) return "";
    size_t q1 = body.find('"', pos + 1);
    if (q1 == std::string::npos) return "";
    size_t q2 = body.find('"', q1 + 1);
    if (q2 == std::string::npos) return "";
    return body.substr(q1 + 1, q2 - q1 - 1);
}

int64_t json_i64_field(const std::string& body, const std::string& key, int64_t fallback) {
    std::string pat = "\"" + key + "\"";
    size_t pos = body.find(pat);
    if (pos == std::string::npos) return fallback;
    pos = body.find(':', pos + pat.size());
    if (pos == std::string::npos) return fallback;
    pos += 1;
    while (pos < body.size() && (body[pos] == ' ' || body[pos] == '\t')) pos++;
    try {
        size_t idx = 0;
        long long v = std::stoll(body.substr(pos), &idx, 10);
        if (idx == 0) return fallback;
        return static_cast<int64_t>(v);
    } catch (...) {
        return fallback;
    }
}

int64_t now_epoch_s() {
    return std::chrono::duration_cast<std::chrono::seconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

}  // namespace

ShadowComparisonReport run_shadow_parity_check(
    const Dataset& ds,
    const SimConfig& scfg,
    const ActorCritic* model_or_null,
    const std::string& model_hash) {
    auto r1 = run_replay_episode(ds, model_or_null, model_hash, scfg);
    auto r2 = run_replay_episode(ds, model_or_null, model_hash, scfg);
    ShadowComparisonReport rep;
    rep.ref_final_equity_cents = r1.final_equity_cents;
    rep.native_final_equity_cents = r2.final_equity_cents;
    rep.ref_risk_interventions = r1.risk_interventions;
    rep.native_risk_interventions = r2.risk_interventions;
    rep.steps_compared = r1.steps;
    rep.ref_receipt_hash = r1.receipt_hash;
    rep.native_receipt_hash = r2.receipt_hash;
    rep.exact_accounting_parity = (r1.final_cash_cents == r2.final_cash_cents &&
                                   r1.final_shares == r2.final_shares &&
                                   r1.final_equity_cents == r2.final_equity_cents);
    rep.exact_verdict_parity = (r1.risk_interventions == r2.risk_interventions &&
                                r1.trades_executed == r2.trades_executed &&
                                r1.receipt_hash == r2.receipt_hash);
    return rep;
}

NativeHttpServer::NativeHttpServer(ServiceConfig cfg)
    : cfg_(std::move(cfg)), oms_(cfg_.db_path) {
    policy_ = default_sim_config().risk_policy;
    policy_.max_order_notional_bps = 2500;
    policy_.max_concentration_bps = 3500;
    policy_.max_daily_loss_bps = 500;
    policy_.max_total_loss_bps = 1000;
    oms_.init_account(cfg_.env, 10000000, 10000000);
    if (cfg_.enable_paper_writer) {
        oms_.acquire_writer_lease(cfg_.env, cfg_.writer_owner_id, cfg_.writer_epoch);
    } else {
        oms_.revoke_writer_lease(cfg_.env);
    }
}

NativeHttpServer::~NativeHttpServer() {
    stop();
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
        bool lease_ok = oms_.has_active_writer_lease(cfg_.env, cfg_.writer_owner_id, cfg_.writer_epoch);
        auto in_flight = oms_.recover_in_flight(cfg_.env);
        std::ostringstream oss;
        oss << "{\"process\":\"ok\",\"backend\":\"native_cpp\",\"data_mode\":\"" << json_escape(cfg_.data_namespace)
            << "\",\"execution_mode\":\"" << json_escape(cfg_.env)
            << "\",\"live_execution\":false,\"dataset_loaded\":" << (ds_ok ? "true" : "false")
            << ",\"writer_lease_active\":" << (lease_ok ? "true" : "false")
            << ",\"in_flight_unknown\":" << in_flight.size()
            << ",\"abi_version\":" << AQ_CORE_ABI_VERSION << "}";
        return {200, "application/json", {}, oss.str()};
    }

    if (m == "POST" && p == "/api/login") {
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
        std::string tok = json_str_field(req.body, "token");
        if (!ct_equal(tok, cfg_.auth_token)) {
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

    // All remaining endpoints require authentication.
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

    if (m == "GET" && p == "/api/market/feed/status") {
        bool ds_ok = false;
        std::string sym;
        std::string src;
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
        std::string action = json_str_field(req.body, "action");
        std::string reason = json_str_field(req.body, "reason");
        if (action == "resume") {
            auto cs = oms_.resume(reason.empty() ? "operator_resume" : reason);
            std::ostringstream oss;
            oss << "{\"halted\":false,\"control_epoch\":" << cs.control_epoch << "}";
            return {200, "application/json", {}, oss.str()};
        }
        auto hr = oms_.halt(cfg_.env, reason.empty() ? "operator_halt" : reason);
        std::ostringstream oss;
        oss << "{\"halted\":true,\"control_epoch\":" << hr.control_epoch
            << ",\"in_flight_unknown\":" << hr.in_flight_unknown_count
            << ",\"note\":\"" << json_escape(hr.note) << "\"}";
        return {200, "application/json", {}, oss.str()};
    }

    if (m == "GET" && p == "/api/broker/paper-orders") {
        auto orders = oms_.list_orders(cfg_.env);
        std::ostringstream oss;
        oss << "{\"orders\":[";
        for (size_t i = 0; i < orders.size(); ++i) {
            const auto& o = orders[i];
            if (i > 0) oss << ",";
            oss << "{\"client_order_id\":\"" << json_escape(o.client_order_id)
                << "\",\"symbol\":\"" << json_escape(o.symbol)
                << "\",\"side\":\"" << (o.side == AQ_SIDE_BUY ? "BUY" : "SELL")
                << "\",\"qty\":" << o.qty << ",\"filled_qty\":" << o.filled_qty
                << ",\"limit_price_cents\":" << o.limit_price_cents
                << ",\"reserved_cash_cents\":" << o.reserved_cash_cents
                << ",\"state\":\"" << order_state_to_str(o.state)
                << "\",\"reason_mask\":" << o.reason_mask << "}";
        }
        oss << "]}";
        return {200, "application/json", {}, oss.str()};
    }

    if (m == "POST" && p == "/api/orders") {
        std::string cid = json_str_field(req.body, "client_order_id");
        std::string sym = json_str_field(req.body, "symbol");
        std::string side_s = json_str_field(req.body, "side");
        int64_t qty = json_i64_field(req.body, "qty", 0);
        int64_t price_cents = json_i64_field(req.body, "price_cents", 0);
        if (cid.empty() || sym.empty() || qty <= 0 || price_cents <= 0) {
            return {400, "application/json", {}, "{\"detail\":\"Missing or invalid order fields\"}"};
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
        intent.side = (side_s == "SELL") ? AQ_SIDE_SELL : AQ_SIDE_BUY;
        intent.order_type = AQ_ORDER_LIMIT;
        intent.qty = qty;
        intent.price_cents = price_cents;
        intent.data_age_ms = 0;

        aq_risk_policy_t pol{};
        {
            std::lock_guard<std::mutex> lk(mu_);
            pol = policy_;
        }
        auto sub = oms_.submit_intent(cfg_.env, cid, spec, intent, pol);
        std::ostringstream oss;
        oss << "{\"approved\":" << (sub.accepted_for_reservation ? "true" : "false")
            << ",\"duplicate_idempotent\":" << (sub.duplicate_idempotent ? "true" : "false")
            << ",\"client_order_id\":\"" << json_escape(sub.order.client_order_id)
            << "\",\"state\":\"" << order_state_to_str(sub.order.state)
            << "\",\"reason_mask\":" << sub.decision.reason_mask << "}";
        return {sub.accepted_for_reservation ? 200 : 422, "application/json", {}, oss.str()};
    }

    if (m == "POST" && p == "/api/training/jobs") {
        Dataset ds_copy;
        {
            std::lock_guard<std::mutex> lk(mu_);
            if (!dataset_loaded_) {
                return {400, "application/json", {}, "{\"detail\":\"No dataset loaded for training\"}"};
            }
            ds_copy = dataset_;
        }
        uint32_t updates = static_cast<uint32_t>(std::clamp<int64_t>(json_i64_field(req.body, "updates", 2), 1, 16));
        TrainConfig tcfg;
        tcfg.seed = static_cast<uint64_t>(std::max<int64_t>(1, json_i64_field(req.body, "seed", 26)));
        tcfg.updates = updates;
        tcfg.rollout_steps = 32;
        std::string job_id = "job-" + random_hex(6);
        std::string out_dir = cfg_.db_path + ".jobs/" + job_id;
        auto sum = train_ppo(ds_copy, out_dir, tcfg);
        {
            std::lock_guard<std::mutex> lk(mu_);
            jobs_.push_back(TrainingJobInfo{job_id, "COMPLETED", sum.updates_completed, sum.checkpoint_hash, sum.final_eval_equity_usd});
        }
        std::ostringstream oss;
        oss << "{\"job_id\":\"" << job_id << "\",\"status\":\"COMPLETED\",\"updates\":" << sum.updates_completed
            << ",\"checkpoint_hash\":\"" << sum.checkpoint_hash << "\",\"eval_equity_usd\":" << sum.final_eval_equity_usd << "}";
        return {200, "application/json", {}, oss.str()};
    }

    if (m == "GET" && p == "/api/training/jobs") {
        std::lock_guard<std::mutex> lk(mu_);
        std::ostringstream oss;
        oss << "{\"jobs\":[";
        for (size_t i = 0; i < jobs_.size(); ++i) {
            if (i > 0) oss << ",";
            oss << "{\"job_id\":\"" << jobs_[i].job_id << "\",\"status\":\"" << jobs_[i].status
                << "\",\"updates\":" << jobs_[i].updates
                << ",\"checkpoint_hash\":\"" << jobs_[i].checkpoint_hash
                << "\",\"eval_equity_usd\":" << jobs_[i].eval_equity_usd << "}";
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
    listen_fd_ = ::socket(AF_INET, SOCK_STREAM, 0);
    if (listen_fd_ < 0) throw std::runtime_error("socket() failed");

    int opt = 1;
    ::setsockopt(listen_fd_, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(cfg_.port);
    if (::inet_pton(AF_INET, cfg_.host.c_str(), &addr.sin_addr) != 1) {
        ::close(listen_fd_);
        listen_fd_ = -1;
        throw std::runtime_error("Invalid bind host");
    }
    if (::bind(listen_fd_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0 ||
        ::listen(listen_fd_, 32) != 0) {
        ::close(listen_fd_);
        listen_fd_ = -1;
        throw std::runtime_error("bind/listen failed");
    }

    sockaddr_in bound{};
    socklen_t blen = sizeof(bound);
    if (::getsockname(listen_fd_, reinterpret_cast<sockaddr*>(&bound), &blen) == 0) {
        bound_port_ = ntohs(bound.sin_port);
    }

    running_.store(true);
    accept_thread_ = std::thread([this]() {
        while (running_.load()) {
            sockaddr_in cli{};
            socklen_t clen = sizeof(cli);
            int cfd = ::accept(listen_fd_, reinterpret_cast<sockaddr*>(&cli), &clen);
            if (cfd < 0) {
                if (!running_.load()) break;
                continue;
            }
            char buf[8192];
            ssize_t n = ::recv(cfd, buf, sizeof(buf) - 1, 0);
            if (n > 0) {
                buf[n] = '\0';
                std::string raw(buf, static_cast<size_t>(n));
                size_t hdr_end = raw.find("\r\n\r\n");
                HttpRequest req;
                std::string hdr_part = (hdr_end == std::string::npos) ? raw : raw.substr(0, hdr_end);
                if (hdr_end != std::string::npos) req.body = raw.substr(hdr_end + 4);

                std::istringstream hss(hdr_part);
                std::string req_line;
                std::getline(hss, req_line);
                if (!req_line.empty() && req_line.back() == '\r') req_line.pop_back();
                std::istringstream rl(req_line);
                rl >> req.method >> req.path;

                std::string line;
                while (std::getline(hss, line)) {
                    if (!line.empty() && line.back() == '\r') line.pop_back();
                    size_t col = line.find(':');
                    if (col != std::string::npos) {
                        std::string k = line.substr(0, col);
                        std::string v = line.substr(col + 1);
                        while (!v.empty() && v.front() == ' ') v.erase(v.begin());
                        req.headers[k] = v;
                    }
                }
                char ipbuf[INET_ADDRSTRLEN]{};
                ::inet_ntop(AF_INET, &cli.sin_addr, ipbuf, sizeof(ipbuf));
                req.remote_addr = ipbuf;

                HttpResponse resp = handle_request(req);
                std::ostringstream out;
                out << "HTTP/1.1 " << resp.status_code << " OK\r\n"
                    << "Content-Type: " << resp.content_type << "\r\n"
                    << "Content-Length: " << resp.body.size() << "\r\n"
                    << "Connection: close\r\n";
                for (const auto& kv : resp.headers) {
                    out << kv.first << ": " << kv.second << "\r\n";
                }
                out << "\r\n" << resp.body;
                std::string wire = out.str();
                ::send(cfd, wire.data(), wire.size(), 0);
            }
            ::close(cfd);
        }
    });
}

void NativeHttpServer::stop() {
    if (!running_.exchange(false)) return;
    if (listen_fd_ >= 0) {
        ::shutdown(listen_fd_, SHUT_RDWR);
        ::close(listen_fd_);
        listen_fd_ = -1;
    }
    if (accept_thread_.joinable()) accept_thread_.join();
}

}  // namespace aq
