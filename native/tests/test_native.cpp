#include "aq/core.h"
#include "aq/data.hpp"
#include "aq/inference.h"
#include "aq/model.hpp"
#include "aq/oms.hpp"
#include "aq/rl.hpp"
#include "aq/service.hpp"
#include "aq/storage.hpp"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <cstdlib>

#define AQ_CHECK(cond) do { if (!(cond)) { std::fprintf(stderr, "AQ_CHECK failed at %s:%d: %s\n", __FILE__, __LINE__, #cond); std::abort(); } } while (0)
#include <cmath>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <limits>
#include <string>
#include <thread>
#include <vector>

namespace {

int g_passed = 0;

void run_check(const char* name, const std::function<void()>& fn) {
    fn();
    ++g_passed;
    std::cout << "[PASS] " << g_passed << ". " << name << "\n";
}

aq_instrument_spec_t make_spec(const char* sym = "NVDA") {
    aq_instrument_spec_t s{};
    s.abi_version = AQ_CORE_ABI_VERSION;
    s.struct_size = sizeof(s);
    std::snprintf(s.symbol, sizeof(s.symbol), "%s", sym);
    std::snprintf(s.currency, sizeof(s.currency), "USD");
    s.price_scale = 100;
    s.qty_scale = 1;
    s.tick_size = 1;
    return s;
}

aq_account_snapshot_t make_acct() {
    aq_account_snapshot_t a{};
    a.abi_version = AQ_CORE_ABI_VERSION;
    a.struct_size = sizeof(a);
    a.cash_cents = 10000000;
    a.equity_cents = 10000000;
    a.daily_pnl_bps = 0;
    a.total_drawdown_bps = 0;
    a.account_revision = 1;
    a.control_epoch = 1;
    a.integrity_ok = 1;
    return a;
}

aq_risk_policy_t make_policy() {
    aq_risk_policy_t p{};
    p.abi_version = AQ_CORE_ABI_VERSION;
    p.struct_size = sizeof(p);
    p.max_order_notional_bps = 2500;
    p.max_concentration_bps = 3500;
    p.max_gross_exposure_bps = 9000;
    p.max_daily_loss_bps = 500;
    p.max_total_loss_bps = 1000;
    p.max_data_age_ms = 30000;
    p.no_short_selling = 1;
    p.allow_protective_brackets = 0;
    return p;
}

aq_order_intent_t make_intent(const char* sym = "NVDA", uint8_t side = AQ_SIDE_BUY, int64_t qty = 10, int64_t px = 12000) {
    aq_order_intent_t i{};
    i.abi_version = AQ_CORE_ABI_VERSION;
    i.struct_size = sizeof(i);
    std::snprintf(i.symbol, sizeof(i.symbol), "%s", sym);
    i.side = side;
    i.order_type = AQ_ORDER_MARKET;
    i.qty = qty;
    i.price_cents = px;
    i.data_age_ms = 100;
    return i;
}

aq_risk_decision_t eval_risk(const aq_instrument_spec_t& s, const aq_account_snapshot_t& a, const aq_risk_policy_t& p, const aq_order_intent_t& i) {
    aq_risk_decision_t d{};
    d.abi_version = AQ_CORE_ABI_VERSION;
    d.struct_size = sizeof(d);
    AQ_CHECK(aq_evaluate_risk(&s, &a, &p, &i, &d) == AQ_OK);
    return d;
}

}  // namespace

int main() {
    const std::string tmp_dir = "/tmp/aegis_native_tests";
    std::filesystem::remove_all(tmp_dir);
    std::filesystem::create_directories(tmp_dir);

    // 1-6: Checked arithmetic & ABI guards
    run_check("checked_add_overflow", [] {
        int64_t out = 0;
        AQ_CHECK(aq_checked_add_i64(INT64_MAX, 1, &out) == AQ_ERR_OVERFLOW);
        AQ_CHECK(aq_checked_add_i64(INT64_MIN, -1, &out) == AQ_ERR_OVERFLOW);
    });
    run_check("checked_sub_overflow", [] {
        int64_t out = 0;
        AQ_CHECK(aq_checked_sub_i64(INT64_MIN, 1, &out) == AQ_ERR_OVERFLOW);
        AQ_CHECK(aq_checked_sub_i64(INT64_MAX, -1, &out) == AQ_ERR_OVERFLOW);
    });
    run_check("checked_mul_overflow", [] {
        int64_t out = 0;
        AQ_CHECK(aq_checked_mul_i64(INT64_MAX, 2, &out) == AQ_ERR_OVERFLOW);
        AQ_CHECK(aq_checked_mul_i64(INT64_MIN, -1, &out) == AQ_ERR_OVERFLOW);
    });
    run_check("abi_null_and_size_checks", [] {
        auto s = make_spec(); auto a = make_acct(); auto p = make_policy(); auto i = make_intent();
        aq_risk_decision_t d{}; d.abi_version = AQ_CORE_ABI_VERSION; d.struct_size = sizeof(d);
        AQ_CHECK(aq_evaluate_risk(nullptr, &a, &p, &i, &d) == AQ_ERR_NULL);
        i.struct_size = 8;
        AQ_CHECK(aq_evaluate_risk(&s, &a, &p, &i, &d) == AQ_ERR_STRUCT_SIZE);
    });
    run_check("instrument_currency_rejected", [] {
        auto s = make_spec();
        std::snprintf(s.currency, sizeof(s.currency), "EUR");
        AQ_CHECK(aq_validate_instrument(&s) == AQ_ERR_UNSUPPORTED_UNIT);
    });
    run_check("instrument_fractional_scale_rejected", [] {
        auto s = make_spec();
        s.qty_scale = 1000;
        AQ_CHECK(aq_validate_instrument(&s) == AQ_ERR_UNSUPPORTED_UNIT);
    });

    // 7-18: Risk evaluation invariants
    run_check("risk_valid_buy_approved", [] {
        auto d = eval_risk(make_spec(), make_acct(), make_policy(), make_intent());
        AQ_CHECK(d.approved == 1 && d.reason_mask == AQ_REJ_NONE);
    });
    run_check("risk_halt_blocks_new_buy", [] {
        auto a = make_acct(); a.halted = 1;
        auto d = eval_risk(make_spec(), a, make_policy(), make_intent());
        AQ_CHECK(d.approved == 0 && (d.reason_mask & AQ_REJ_HALTED));
    });
    run_check("risk_halt_allows_risk_reducing_sell", [] {
        aq_position_mark_t pm{}; std::snprintf(pm.symbol, sizeof(pm.symbol), "NVDA"); pm.qty = 20; pm.mark_cents = 12000;
        auto a = make_acct(); a.halted = 1; a.position_count = 1; a.positions = &pm;
        auto d = eval_risk(make_spec(), a, make_policy(), make_intent("NVDA", AQ_SIDE_SELL, 10, 12000));
        AQ_CHECK(d.approved == 1 && d.risk_reducing == 1);
    });
    run_check("risk_untrusted_integrity_blocks_sell_too", [] {
        aq_position_mark_t pm{}; std::snprintf(pm.symbol, sizeof(pm.symbol), "NVDA"); pm.qty = 20; pm.mark_cents = 12000;
        auto a = make_acct(); a.integrity_ok = 0; a.position_count = 1; a.positions = &pm;
        auto d = eval_risk(make_spec(), a, make_policy(), make_intent("NVDA", AQ_SIDE_SELL, 10, 12000));
        AQ_CHECK(d.approved == 0 && (d.reason_mask & AQ_REJ_INTEGRITY));
    });
    run_check("risk_stale_data_blocks_buy", [] {
        auto i = make_intent(); i.data_age_ms = 31000;
        auto d = eval_risk(make_spec(), make_acct(), make_policy(), i);
        AQ_CHECK(d.approved == 0 && (d.reason_mask & AQ_REJ_STALE_DATA));
    });
    run_check("risk_missing_loss_state_fails_closed", [] {
        auto a = make_acct(); a.daily_pnl_bps = INT64_MIN;
        auto d = eval_risk(make_spec(), a, make_policy(), make_intent());
        AQ_CHECK(d.approved == 0 && (d.reason_mask & AQ_REJ_MISSING_LOSS_STATE));
    });
    run_check("risk_daily_loss_breach_blocks", [] {
        auto a = make_acct(); a.daily_pnl_bps = -500;
        auto d = eval_risk(make_spec(), a, make_policy(), make_intent());
        AQ_CHECK(d.approved == 0 && (d.reason_mask & AQ_REJ_DAILY_LOSS));
    });
    run_check("risk_total_drawdown_breach_blocks", [] {
        auto a = make_acct(); a.total_drawdown_bps = 1000;
        auto d = eval_risk(make_spec(), a, make_policy(), make_intent());
        AQ_CHECK(d.approved == 0 && (d.reason_mask & AQ_REJ_TOTAL_DRAWDOWN));
    });
    run_check("risk_unsupported_stop_target_rejected", [] {
        auto i = make_intent(); i.stop_cents = 9000; i.target_cents = 15000;
        auto d = eval_risk(make_spec(), make_acct(), make_policy(), i);
        AQ_CHECK(d.approved == 0 && (d.reason_mask & AQ_REJ_UNSUPPORTED_PROTECTION));
    });
    run_check("risk_insufficient_cash_blocks", [] {
        auto a = make_acct(); a.reserved_buy_cents = 9990000;
        auto d = eval_risk(make_spec(), a, make_policy(), make_intent());
        AQ_CHECK(d.approved == 0 && (d.reason_mask & AQ_REJ_INSUFFICIENT_CASH));
    });
    run_check("risk_short_selling_prohibited", [] {
        auto d = eval_risk(make_spec(), make_acct(), make_policy(), make_intent("NVDA", AQ_SIDE_SELL, 5, 12000));
        AQ_CHECK(d.approved == 0 && (d.reason_mask & AQ_REJ_SHORT_SELLING));
    });
    run_check("risk_missing_held_position_mark_rejected", [] {
        aq_position_mark_t pm{}; std::snprintf(pm.symbol, sizeof(pm.symbol), "AMAT"); pm.qty = 5; pm.mark_cents = 0;
        auto a = make_acct(); a.position_count = 1; a.positions = &pm;
        auto d = eval_risk(make_spec(), a, make_policy(), make_intent());
        AQ_CHECK(d.approved == 0 && (d.reason_mask & AQ_REJ_MISSING_MARK));
    });

    // 19-23: SQLite budget atomicity & ControlJournal
    run_check("sqlite_budget_single_reserve_reconcile", [&] {
        aq::SqliteStore st(tmp_dir + "/b1.sqlite");
        aq::BudgetLedgerNative ledger(st, aq::kNanoPerUsd, aq::kNanoPerUsd, aq::kNanoPerUsd);
        AQ_CHECK(ledger.reserve("r1", "j1", "2026-11-15", 600000000LL));
        AQ_CHECK(!ledger.reserve("r2", "j1", "2026-11-15", 500000000LL));
        AQ_CHECK(ledger.reconcile("r1", 200000000LL));
        AQ_CHECK(ledger.snapshot("2026-11-15").project_spent_nano == 200000000LL);
    });
    run_check("sqlite_budget_unknown_outcome_preserved", [&] {
        aq::SqliteStore st(tmp_dir + "/b2.sqlite");
        aq::BudgetLedgerNative ledger(st, aq::kNanoPerUsd, aq::kNanoPerUsd, aq::kNanoPerUsd);
        AQ_CHECK(ledger.reserve("r1", "j1", "2026-11-15", 400000000LL));
        AQ_CHECK(ledger.reconcile("r1", std::nullopt, true));
        AQ_CHECK(ledger.snapshot("2026-11-15").unknown_outcomes == 1);
    });
    run_check("sqlite_budget_concurrent_threads_no_over_cap", [&] {
        const std::string db = tmp_dir + "/b_race.sqlite";
        {
            aq::SqliteStore st(db);
            aq::BudgetLedgerNative ledger(st, 2 * aq::kNanoPerUsd, 2 * aq::kNanoPerUsd, aq::kNanoPerUsd);
        }
        std::atomic<int> ok_count{0};
        std::vector<std::thread> ths;
        for (int t = 0; t < 8; ++t) {
            ths.emplace_back([&, t] {
                aq::SqliteStore st(db);
                aq::BudgetLedgerNative ledger(st, 2 * aq::kNanoPerUsd, 2 * aq::kNanoPerUsd, aq::kNanoPerUsd);
                if (ledger.reserve("r-" + std::to_string(t), "j1", "2026-11-15", 750000000LL)) {
                    ok_count.fetch_add(1);
                }
            });
        }
        for (auto& th : ths) th.join();
        AQ_CHECK(ok_count.load() == 1);
    });
    run_check("control_journal_halt_invalidates_queued_epoch", [&] {
        aq::SqliteStore st(tmp_dir + "/cj.sqlite");
        aq::ControlJournal cj(st);
        uint64_t ep = cj.current_epoch();
        AQ_CHECK(cj.enqueue_intent("cid-1", "hash-1", ep));
        uint64_t ep2 = cj.set_halt(true, "OPERATOR_HALT");
        AQ_CHECK(ep2 > ep);
        AQ_CHECK(!cj.claim_for_dispatch("cid-1", ep));
        AQ_CHECK(!cj.claim_for_dispatch("cid-1", ep2));
    });
    run_check("control_journal_resume_allows_new_epoch", [&] {
        aq::SqliteStore st(tmp_dir + "/cj2.sqlite");
        aq::ControlJournal cj(st);
        uint64_t ep1 = cj.set_halt(true, "TEST");
        uint64_t ep2 = cj.set_halt(false, "");
        AQ_CHECK(ep2 > ep1);
        AQ_CHECK(cj.enqueue_intent("cid-2", "hash-2", ep2));
        AQ_CHECK(cj.claim_for_dispatch("cid-2", ep2));
    });

    // 24-29: Strict CSV & synthetic dataset
    run_check("synthetic_dataset_deterministic", [] {
        auto d1 = aq::generate_synthetic_dataset(120, 26);
        auto d2 = aq::generate_synthetic_dataset(120, 26);
        AQ_CHECK(d1.dataset_hash == d2.dataset_hash && d1.bars.size() == 120);
    });
    run_check("csv_roundtrip_strict", [&] {
        auto d1 = aq::generate_synthetic_dataset(80, 11);
        auto d2 = aq::parse_csv_strict(aq::to_csv(d1), "SYNTHETIC");
        AQ_CHECK(d1.dataset_hash == d2.dataset_hash);
    });
    run_check("csv_rejects_naive_timestamp", [] {
        bool threw = false;
        try { aq::parse_iso8601_utc("2026-11-02T14:30:00"); } catch (...) { threw = true; }
        AQ_CHECK(threw);
    });
    run_check("csv_rejects_under_65_bars", [] {
        auto d1 = aq::generate_synthetic_dataset(65, 26);
        d1.bars.pop_back();
        bool threw = false;
        try { aq::parse_csv_strict(aq::to_csv(d1)); } catch (...) { threw = true; }
        AQ_CHECK(threw);
    });
    run_check("csv_rejects_lookahead_open_before_available", [] {
        auto d1 = aq::generate_synthetic_dataset(66, 26);
        d1.bars[10].open_at = d1.bars[9].open_at;
        bool threw = false;
        try { aq::parse_csv_strict(aq::to_csv(d1)); } catch (...) { threw = true; }
        AQ_CHECK(threw);
    });
    run_check("csv_rejects_corporate_action", [] {
        std::string csv = "symbol,open_at,timestamp,available_at,open,high,low,close,volume,split_factor\n"
                          "NVDA,2026-11-02T14:30:00Z,2026-11-02T14:34:59Z,2026-11-02T14:35:00Z,100,101,99,100.5,1000,2.0\n";
        bool threw = false;
        try { aq::parse_csv_strict(csv); } catch (...) { threw = true; }
        AQ_CHECK(threw);
    });

    // 30-35: Causal features, GAE terminal vs truncation, PPO finite-difference gradients
    run_check("causal_obs_invariant_to_future_bars", [] {
        auto d1 = aq::generate_synthetic_dataset(100, 26);
        auto d2 = d1;
        d2.bars[50].close_cents = 999999;
        auto o1 = aq::build_causal_obs(d1, 40, 0, 10000000, 10000000);
        auto o2 = aq::build_causal_obs(d2, 40, 0, 10000000, 10000000);
        AQ_CHECK(o1 == o2);
    });
    run_check("lookahead_negative_control_detected", [] {
        auto d1 = aq::generate_synthetic_dataset(100, 26);
        auto d2 = d1;
        d2.bars[40].close_cents = d2.bars[40].close_cents * 2;
        auto o1 = aq::build_causal_obs(d1, 40, 0, 10000000, 10000000);
        auto o2 = aq::build_causal_obs(d2, 40, 0, 10000000, 10000000);
        AQ_CHECK(o1 != o2);
    });
    run_check("gae_truncation_bootstraps_and_resets_carry", [] {
        std::vector<aq::GaeTransition> tr(2);
        tr[0].reward = 1.0; tr[0].value = 0.5; tr[0].bootstrap_value = 0.4; tr[0].truncated = true;
        tr[1].reward = 2.0; tr[1].value = 99.0; tr[1].truncated = false;
        std::vector<double> adv, ret;
        aq::compute_gae(tr, 1.0, 0.99, 0.95, adv, ret);
        // At t=0 (truncated), carry from t=1 is 0, bootstrap uses tr[0].bootstrap_value (never reset tr[1].value)
        double expected_a0 = 1.0 + 0.99 * 0.4 - 0.5;
        AQ_CHECK(std::abs(adv[0] - expected_a0) < 1e-12);
    });
    run_check("gae_terminal_does_not_bootstrap", [] {
        std::vector<aq::GaeTransition> tr(1);
        tr[0].reward = 1.5; tr[0].value = 0.5; tr[0].terminated = true;
        std::vector<double> adv, ret;
        aq::compute_gae(tr, 99.0, 0.99, 0.95, adv, ret);
        AQ_CHECK(std::abs(adv[0] - (1.5 - 0.5)) < 1e-12);
    });
    run_check("ppo_finite_difference_gradients_708_params", [] {
        // Test all 708 parameters across 4 (unclipped pos, unclipped neg, clipped pos, clipped neg) cases
        struct Case { double lp_offset; double adv; };
        Case cases[4] = {{0.0, 1.2}, {0.0, -1.2}, {-0.5, 1.2}, {0.5, -1.2}};
        std::array<double, aq::kObsDim> obs{0.3, -0.2, 0.1, 0.4, 0.25, 0.75, 0.02, 1.0};
        for (const auto& c : cases) {
            aq::ActorCritic ac(26);
            auto af0 = ac.forward_actor(obs);
            uint32_t act = 1;
            double old_lp = std::log(af0.probs[act]) + c.lp_offset;
            std::vector<double> ga(aq::kActorParamCount, 0.0), gc(aq::kCriticParamCount, 0.0);
            double pi_l = 0, vf_l = 0, ent = 0, kl = 0;
            ac.accumulate_ppo_grad(obs, act, old_lp, c.adv, 0.7, 0.2, 0.5, 0.01, 1.0, ga, gc, pi_l, vf_l, ent, kl);

            auto eval_total_loss = [&](const aq::ActorCritic& m) {
                std::vector<double> d1(aq::kActorParamCount, 0.0), d2(aq::kCriticParamCount, 0.0);
                double pl = 0, vl = 0, e = 0, k = 0;
                m.accumulate_ppo_grad(obs, act, old_lp, c.adv, 0.7, 0.2, 0.5, 0.01, 1.0, d1, d2, pl, vl, e, k);
                return pl - 0.01 * e + 0.5 * vl;
            };
            constexpr double h = 1e-6;
            for (size_t i = 0; i < aq::kActorParamCount; ++i) {
                aq::ActorCritic mp = ac, mm = ac;
                mp.actor_params()[i] += h;
                mm.actor_params()[i] -= h;
                double fd = (eval_total_loss(mp) - eval_total_loss(mm)) / (2.0 * h);
                AQ_CHECK(std::abs(fd - ga[i]) < 1e-6);
            }
            for (size_t i = 0; i < aq::kCriticParamCount; ++i) {
                aq::ActorCritic mp = ac, mm = ac;
                mp.critic_params()[i] += h;
                mm.critic_params()[i] -= h;
                double fd = (eval_total_loss(mp) - eval_total_loss(mm)) / (2.0 * h);
                AQ_CHECK(std::abs(fd - gc[i]) < 1e-6);
            }
        }
    });
    run_check("param_counts_match_387_and_321", [] {
        AQ_CHECK(aq::kActorParamCount == 387);
        AQ_CHECK(aq::kCriticParamCount == 321);
        AQ_CHECK(aq::kTotalParamCount == 708);
    });

    // 36-45: Checkpoints, C inference ABI, concurrency, resume equivalence, N2 replay & cash baseline
    const std::string ckpt = tmp_dir + "/test.aq";
    run_check("checkpoint_save_load_hash_verified", [&] {
        aq::ActorCritic ac(11);
        ac.dataset_hash = "abc";
        std::string h1 = ac.save_checkpoint(ckpt);
        std::string h2;
        auto ac2 = aq::ActorCritic::load_checkpoint(ckpt, &h2);
        AQ_CHECK(h1 == h2 && ac.actor_params() == ac2.actor_params() && ac.critic_params() == ac2.critic_params());
    });
    run_check("checkpoint_tampered_rejected", [&] {
        std::string bad = tmp_dir + "/bad.aq";
        std::filesystem::copy_file(ckpt, bad, std::filesystem::copy_options::overwrite_existing);
        std::fstream fs(bad, std::ios::in | std::ios::out | std::ios::binary);
        fs.seekp(10);
        char z = 'X';
        fs.write(&z, 1);
        fs.close();
        bool threw = false;
        try { aq::ActorCritic::load_checkpoint(bad); } catch (...) { threw = true; }
        AQ_CHECK(threw);
    });
    run_check("c_inference_abi_rejects_bad_schema_and_nan", [&] {
        aq_model_handle* h = nullptr;
        AQ_CHECK(aq_model_load(ckpt.c_str(), &h) == AQ_OK && h != NULL);
        aq_obs_v1_t obs{};
        obs.abi_version = AQ_INFERENCE_ABI_VERSION;
        obs.struct_size = sizeof(obs);
        obs.feature_schema_version = 99;
        aq_proposal_v1_t prop{};
        prop.abi_version = AQ_INFERENCE_ABI_VERSION;
        prop.struct_size = sizeof(prop);
        AQ_CHECK(aq_model_predict_v1(h, &obs, &prop) == AQ_ERR_INVALID_ARG);
        obs.feature_schema_version = 1;
        obs.features[0] = std::numeric_limits<double>::quiet_NaN();
        AQ_CHECK(aq_model_predict_v1(h, &obs, &prop) == AQ_ERR_INVALID_ARG);
        aq_model_free(h);
    });
    run_check("c_inference_abi_never_authorizes_execution", [&] {
        aq_model_handle* h = nullptr;
        AQ_CHECK(aq_model_load(ckpt.c_str(), &h) == AQ_OK);
        aq_obs_v1_t obs{};
        obs.abi_version = AQ_INFERENCE_ABI_VERSION;
        obs.struct_size = sizeof(obs);
        obs.feature_schema_version = 1;
        obs.features[7] = 1.0;
        aq_proposal_v1_t prop{};
        prop.abi_version = AQ_INFERENCE_ABI_VERSION;
        prop.struct_size = sizeof(prop);
        AQ_CHECK(aq_model_predict_v1(h, &obs, &prop) == AQ_OK);
        AQ_CHECK(prop.execution_authorized == 0);
        aq_model_free(h);
    });
    run_check("c_inference_concurrent_readonly_threads", [&] {
        aq_model_handle* h = nullptr;
        AQ_CHECK(aq_model_load(ckpt.c_str(), &h) == AQ_OK);
        std::vector<std::thread> ths;
        for (int t = 0; t < 8; ++t) {
            ths.emplace_back([&] {
                aq_obs_v1_t obs{};
                obs.abi_version = AQ_INFERENCE_ABI_VERSION;
                obs.struct_size = sizeof(obs);
                obs.feature_schema_version = 1;
                obs.features[7] = 1.0;
                aq_proposal_v1_t prop{};
                prop.abi_version = AQ_INFERENCE_ABI_VERSION;
                prop.struct_size = sizeof(prop);
                for (int k = 0; k < 100; ++k) {
                    AQ_CHECK(aq_model_predict_v1(h, &obs, &prop) == AQ_OK);
                    AQ_CHECK(prop.execution_authorized == 0);
                }
            });
        }
        for (auto& th : ths) th.join();
        aq_model_free(h);
    });
    run_check("ppo_train_and_resume_exact_equivalence", [&] {
        auto ds = aq::generate_synthetic_dataset(300, 26);
        aq::TrainConfig full_cfg;
        full_cfg.seed = 26;
        full_cfg.updates = 8;
        full_cfg.rollout_steps = 64;
        auto s_full = aq::train_ppo(ds, tmp_dir + "/ppo_full", full_cfg);

        aq::TrainConfig p1_cfg = full_cfg;
        p1_cfg.updates = 4;
        auto s_p1 = aq::train_ppo(ds, tmp_dir + "/ppo_p1", p1_cfg);
        auto s_p2 = aq::train_ppo(ds, tmp_dir + "/ppo_p2", p1_cfg, aq::default_sim_config(), s_p1.checkpoint_path);
        AQ_CHECK(s_full.checkpoint_hash == s_p2.checkpoint_hash);
    });
    run_check("cash_baseline_exact_zero_return", [] {
        auto ds = aq::generate_synthetic_dataset(100, 26);
        auto rec = aq::run_replay_episode(ds, nullptr, "cash_baseline", aq::default_sim_config(), 0);
        AQ_CHECK(rec.final_cash_cents == 10000000 && rec.final_shares == 0 && rec.final_equity_cents == 10000000);
    });
    run_check("execution_costs_reduce_equity_on_turnover", [] {
        auto ds = aq::generate_synthetic_dataset(100, 26);
        auto cfg0 = aq::default_sim_config();
        cfg0.half_spread_bps = 0; cfg0.slippage_bps = 0; cfg0.commission_bps = 0;
        auto cfg1 = aq::default_sim_config();
        cfg1.half_spread_bps = 10; cfg1.slippage_bps = 10; cfg1.commission_bps = 20;
        auto r0 = aq::run_replay_episode(ds, nullptr, "a2", cfg0, 2);
        auto r1 = aq::run_replay_episode(ds, nullptr, "a2", cfg1, 2);
        AQ_CHECK(r1.final_equity_cents < r0.final_equity_cents);
    });
    run_check("n2_replay_receipt_deterministic", [] {
        auto ds = aq::generate_synthetic_dataset(200, 26);
        aq::ActorCritic ac(26);
        auto r1 = aq::run_replay_episode(ds, &ac, "m26");
        auto r2 = aq::run_replay_episode(ds, &ac, "m26");
        AQ_CHECK(r1.receipt_hash == r2.receipt_hash && r1.steps > 0);
    });
    run_check("env_rejects_insufficient_bars", [] {
        auto ds = aq::generate_synthetic_dataset(65, 26);
        ds.bars.resize(17);
        bool threw = false;
        try { aq::CausalEnv env(ds); } catch (...) { threw = true; }
        AQ_CHECK(threw);
    });
    run_check("b1_int64_min_position_rejected_without_ubsan", [] {
        auto spec = make_spec("NVDA");
        aq_position_mark_t pos{};
        std::snprintf(pos.symbol, sizeof(pos.symbol), "NVDA");
        pos.qty = std::numeric_limits<int64_t>::min();
        pos.mark_cents = 10000;
        auto acct = make_acct();
        acct.cash_cents = 100000;
        acct.equity_cents = 100000;
        acct.position_count = 1;
        acct.positions = &pos;
        auto pol = make_policy();
        auto intent = make_intent("NVDA", AQ_SIDE_BUY, 1, 10000);
        aq_risk_decision_t dec{};
        dec.abi_version = AQ_CORE_ABI_VERSION;
        dec.struct_size = sizeof(dec);
        AQ_CHECK(aq_evaluate_risk(&spec, &acct, &pol, &intent, &dec) == AQ_ERR_OVERFLOW);
        AQ_CHECK(dec.approved == 0);
    });
    run_check("b1_policy_tick_duplicate_position_and_symbol_bounds", [] {
        auto spec = make_spec("NVDA");
        spec.tick_size = 5;
        auto acct = make_acct();
        acct.cash_cents = 100000;
        acct.equity_cents = 100000;
        auto pol = make_policy();
        auto bad_tick = make_intent("NVDA", AQ_SIDE_BUY, 1, 10002);
        auto dec = eval_risk(spec, acct, pol, bad_tick);
        AQ_CHECK(dec.approved == 0 && (dec.reason_mask & AQ_REJ_INVALID_VALUATION));

        aq_position_mark_t dups[2]{};
        std::snprintf(dups[0].symbol, sizeof(dups[0].symbol), "NVDA");
        dups[0].qty = 1; dups[0].mark_cents = 10000;
        std::snprintf(dups[1].symbol, sizeof(dups[1].symbol), "NVDA");
        dups[1].qty = 2; dups[1].mark_cents = 10000;
        acct.position_count = 2;
        acct.positions = dups;
        auto ok_tick = make_intent("NVDA", AQ_SIDE_BUY, 1, 10000);
        aq_risk_decision_t out{};
        out.struct_size = sizeof(out);
        AQ_CHECK(aq_evaluate_risk(&spec, &acct, &pol, &ok_tick, &out) == AQ_ERR_INVALID_ARG);
        AQ_CHECK(out.approved == 0 && (out.reason_mask & AQ_REJ_DUPLICATE_POSITION));
    });
    run_check("b2_pending_same_symbol_concentration_and_cross_symbol_control", [] {
        auto pol = make_policy();
        pol.max_order_notional_bps = 2500;  // 25%
        pol.max_concentration_bps = 3500;   // 35%
        pol.max_gross_exposure_bps = 9000;  // 90%

        aq_symbol_reservation_t res{};
        std::snprintf(res.symbol, sizeof(res.symbol), "NVDA");
        res.reserved_buy_cents = 25000;  // $250 pending buy in NVDA
        res.reserved_buy_qty = 25;

        auto acct = make_acct();
        acct.cash_cents = 100000;
        acct.equity_cents = 100000;  // $1,000 equity
        acct.reserved_buy_cents = 25000;
        acct.reservation_count = 1;
        acct.reservations = &res;

        // Same symbol: $250 pending + $250 proposed = 50% > 35% -> REJECTED
        auto dec_same = eval_risk(make_spec("NVDA"), acct, pol, make_intent("NVDA", AQ_SIDE_BUY, 25, 1000));
        AQ_CHECK(dec_same.approved == 0 && (dec_same.reason_mask & AQ_REJ_CONCENTRATION_LIMIT));

        // Different symbol control: $250 pending NVDA + $250 proposed AAPL = 25% AAPL <= 35%, 50% gross <= 90% -> APPROVED
        auto dec_other = eval_risk(make_spec("AAPL"), acct, pol, make_intent("AAPL", AQ_SIDE_BUY, 25, 1000));
        AQ_CHECK(dec_other.approved == 1 && dec_other.reason_mask == AQ_REJ_NONE);
    });
    run_check("b3_gae_truncation_bootstraps_pre_reset_final_obs", [] {
        // r=1, V(current)=2, gamma=0.99, V(final)=10, V(reset)=100 -> adv=8.9 (never 98)
        std::vector<aq::GaeTransition> traj(2);
        traj[0].value = 2.0;
        traj[0].reward = 1.0;
        traj[0].bootstrap_value = 10.0;
        traj[0].terminated = false;
        traj[0].truncated = true;
        traj[1].value = 100.0;  // Next episode reset observation
        traj[1].reward = 0.0;
        traj[1].terminated = true;
        traj[1].truncated = false;
        std::vector<double> adv, ret;
        aq::compute_gae(traj, 999.0, 0.99, 0.95, adv, ret);
        AQ_CHECK(std::abs(adv[0] - 8.9) < 1e-12);
        AQ_CHECK(std::abs(ret[0] - 10.9) < 1e-12);

        // Last-row truncation case
        std::vector<aq::GaeTransition> last_row(1);
        last_row[0].value = 2.0;
        last_row[0].reward = 1.0;
        last_row[0].bootstrap_value = 10.0;
        last_row[0].terminated = false;
        last_row[0].truncated = true;
        aq::compute_gae(last_row, 100.0, 0.99, 0.95, adv, ret);
        AQ_CHECK(std::abs(adv[0] - 8.9) < 1e-12);
    });
    run_check("b4_resume_compatibility_and_transfer_init", [&] {
        auto ds = aq::generate_synthetic_dataset(240, 26);
        auto split = aq::split_dataset_chronological(ds, 0.7);
        AQ_CHECK(split.train.bars.back().close_epoch_s < split.eval.bars.front().open_epoch_s);
        AQ_CHECK(split.train.dataset_hash != split.eval.dataset_hash);

        aq::TrainConfig cfg;
        cfg.seed = 26;
        cfg.updates = 2;
        cfg.rollout_steps = 32;
        auto sum = aq::train_ppo(split.train, tmp_dir + "/b4_train", cfg, aq::default_sim_config(), "", "train");

        // Incompatible dataset must fail closed on resume
        bool threw_ds = false;
        try {
            aq::train_ppo(split.eval, tmp_dir + "/b4_bad_ds", cfg, aq::default_sim_config(), sum.checkpoint_path, "train");
        } catch (...) {
            threw_ds = true;
        }
        AQ_CHECK(threw_ds);

        // Incompatible cost/reward config must fail closed on resume
        auto bad_scfg = aq::default_sim_config();
        bad_scfg.commission_bps += 10;
        bool threw_cfg = false;
        try {
            aq::train_ppo(split.train, tmp_dir + "/b4_bad_cfg", cfg, bad_scfg, sum.checkpoint_path, "train");
        } catch (...) {
            threw_cfg = true;
        }
        AQ_CHECK(threw_cfg);

        // Explicit transfer-learning init preserves weights and resets Adam/env
        auto loaded = aq::ActorCritic::load_checkpoint(sum.checkpoint_path);
        auto xfer = aq::ActorCritic::init_transfer_from(
            loaded, 99, split.eval.dataset_hash, aq::sim_config_hash(bad_scfg, cfg), "train");
        AQ_CHECK(xfer.actor_params() == loaded.actor_params());
        AQ_CHECK(xfer.update_count == 0 && xfer.env_state_serialized.empty());
    });
    run_check("b5_causal_env_halt_epoch_and_gapped_fill", [] {
        auto ds = aq::generate_synthetic_dataset(100, 26);
        aq::CausalEnv env(ds);
        env.set_control_state(true, 7);
        auto so = env.step(2);  // Try to buy 50% while halted
        AQ_CHECK(so.risk_rejected && so.executed_shares_delta == 0);

        // Gapped open price exceeding cash/notional is rejected at execution time
        auto gapped = ds;
        gapped.bars[17].open_cents = gapped.bars[16].close_cents * 10;
        aq::CausalEnv env_gap(gapped);
        auto so_gap = env_gap.step(2);
        AQ_CHECK(so_gap.risk_rejected && so_gap.executed_shares_delta == 0);
    });
    run_check("c1_native_http_service_auth_csrf_and_tcp_socket", [&] {
        aq::ServiceConfig scfg;
        scfg.db_path = tmp_dir + "/n3_srv.sqlite";
        scfg.auth_token = "test-secret-token";
        scfg.enable_paper_writer = false;
        aq::NativeHttpServer srv(scfg);
        srv.load_dataset(aq::generate_synthetic_dataset(100, 26));

        // Unauthenticated /api/state -> 401
        auto r_unauth = srv.handle_request({"GET", "/api/state", {}, ""});
        AQ_CHECK(r_unauth.status_code == 401);

        // /api/health and /api/ready
        auto r_health = srv.handle_request({"GET", "/api/health", {}, ""});
        AQ_CHECK(r_health.status_code == 200 && r_health.body.find("native_cpp") != std::string::npos);
        auto r_ready = srv.handle_request({"GET", "/api/ready", {}, ""});
        AQ_CHECK(r_ready.status_code == 200 && r_ready.body.find("\"dataset_loaded\":true") != std::string::npos);

        // /api/login invalid -> 401, valid -> 200 + Set-Cookie + csrf
        auto r_bad_login = srv.handle_request({"POST", "/api/login", {}, "{\"token\":\"wrong\"}"});
        AQ_CHECK(r_bad_login.status_code == 401);
        auto r_login = srv.handle_request({"POST", "/api/login", {}, "{\"token\":\"test-secret-token\"}"});
        AQ_CHECK(r_login.status_code == 200 && r_login.headers.count("Set-Cookie"));
        std::string cookie = r_login.headers["Set-Cookie"];
        std::string sid = cookie.substr(0, cookie.find(';'));
        size_t cpos = r_login.body.find("\"csrf\":\"") + 8;
        std::string csrf = r_login.body.substr(cpos, r_login.body.find('"', cpos) - cpos);

        // Cookie without CSRF on POST /api/risk/halt -> 401; with CSRF -> 200
        auto r_nocsrf = srv.handle_request({"POST", "/api/risk/halt", {{"Cookie", sid}}, "{\"action\":\"halt\"}"});
        AQ_CHECK(r_nocsrf.status_code == 401);
        auto r_halt = srv.handle_request({"POST", "/api/risk/halt", {{"Cookie", sid}, {"X-CSRF-Token", csrf}}, "{\"action\":\"halt\",\"reason\":\"test\"}"});
        AQ_CHECK(r_halt.status_code == 200 && r_halt.body.find("\"halted\":true") != std::string::npos);

        // Authenticated /api/market/NVDA and /api/training/jobs
        auto r_mkt = srv.handle_request({"GET", "/api/market/NVDA", {{"Cookie", sid}}, ""});
        AQ_CHECK(r_mkt.status_code == 200 && r_mkt.body.find("available_at") != std::string::npos);
        auto r_job = srv.handle_request({"POST", "/api/training/jobs", {{"Cookie", sid}, {"X-CSRF-Token", csrf}}, "{\"updates\":1}"});
        AQ_CHECK(r_job.status_code == 200 && r_job.body.find("\"status\":\"COMPLETED\"") != std::string::npos);

        // Real POSIX TCP socket roundtrip
        srv.start();
        AQ_CHECK(srv.bound_port() > 0);
        int fd = ::socket(AF_INET, SOCK_STREAM, 0);
        AQ_CHECK(fd >= 0);
        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_port = htons(srv.bound_port());
        ::inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);
        AQ_CHECK(::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) == 0);
        const char* req_wire = "GET /api/health HTTP/1.1\r\nHost: localhost\r\n\r\n";
        AQ_CHECK(::send(fd, req_wire, std::strlen(req_wire), 0) > 0);
        char buf[2048]{};
        ssize_t n = ::recv(fd, buf, sizeof(buf) - 1, 0);
        ::close(fd);
        srv.stop();
        AQ_CHECK(n > 0 && std::string(buf, static_cast<size_t>(n)).find("200 OK") != std::string::npos);
    });
    run_check("c2_oms_idempotency_crash_recovery_partial_and_fill", [&] {
        aq::NativeOms oms(tmp_dir + "/n3_oms.sqlite");
        oms.init_account("paper_native", 100000, 100000);
        oms.acquire_writer_lease("paper_native", "w1", 1);
        auto spec = make_spec("NVDA");
        auto pol = make_policy();
        auto intent = make_intent("NVDA", AQ_SIDE_BUY, 10, 2000);  // $200

        auto s1 = oms.submit_intent("paper_native", "cid-1", spec, intent, pol);
        AQ_CHECK(s1.accepted_for_reservation && !s1.duplicate_idempotent);
        auto s2 = oms.submit_intent("paper_native", "cid-1", spec, intent, pol);
        AQ_CHECK(s2.duplicate_idempotent && s2.order.client_order_id == "cid-1");

        // Crash before transport -> DISPATCHED_UNKNOWN with reservation intact
        auto crashed = oms.dispatch_reserved("paper_native", "cid-1", "w1", 1, pol,
            [](const aq::OmsOrderRecord&) { return aq::DispatchOutcome{}; }, true);
        AQ_CHECK(crashed.state == aq::OrderState::DISPATCHED_UNKNOWN && crashed.reserved_cash_cents > 0);
        AQ_CHECK(oms.recover_in_flight("paper_native").size() == 1);

        // Partial fill 5 of 10, then full fill 10 of 10
        auto p1 = oms.reconcile_order("cid-1", {aq::OrderState::PARTIAL, "brk-1", 5, 2000, 10, "partial_5"});
        AQ_CHECK(p1.state == aq::OrderState::PARTIAL && p1.filled_qty == 5 && p1.reserved_cash_cents > 0);
        auto p2 = oms.reconcile_order("cid-1", {aq::OrderState::FILLED, "brk-1", 10, 2000, 10, "filled_10"});
        AQ_CHECK(p2.state == aq::OrderState::FILLED && p2.filled_qty == 10 && p2.reserved_cash_cents == 0);
        auto snap = oms.account_snapshot("paper_native");
        AQ_CHECK(snap.cash_cents == 100000 - 20000 - 20);
        AQ_CHECK(snap.positions.size() == 1 && snap.positions[0].qty == 10);
    });
    run_check("c2_c5_halt_race_and_writer_lease_fencing", [&] {
        aq::NativeOms oms(tmp_dir + "/n3_fence.sqlite");
        oms.init_account("paper_native", 100000, 100000);
        oms.acquire_writer_lease("paper_native", "w1", 1);
        auto spec = make_spec("NVDA");
        auto pol = make_policy();
        auto sub = oms.submit_intent("paper_native", "cid-halt", spec, make_intent("NVDA", AQ_SIDE_BUY, 10, 2000), pol);
        AQ_CHECK(sub.accepted_for_reservation);

        // Stale writer epoch blocked
        bool blocked = false;
        try {
            oms.dispatch_reserved("paper_native", "cid-halt", "w1", 99, pol,
                [](const aq::OmsOrderRecord&) { return aq::DispatchOutcome{}; });
        } catch (...) {
            blocked = true;
        }
        AQ_CHECK(blocked);

        // Halt before dispatch -> REJECTED without transport
        oms.halt("paper_native", "manual_halt");
        bool transport_called = false;
        auto rec = oms.dispatch_reserved("paper_native", "cid-halt", "w1", 1, pol,
            [&](const aq::OmsOrderRecord&) {
                transport_called = true;
                return aq::DispatchOutcome{aq::OrderState::ACCEPTED, "b", 0, 0, 0, ""};
            });
        AQ_CHECK(!transport_called && rec.state == aq::OrderState::REJECTED && rec.reserved_cash_cents == 0);
    });
    run_check("gate4_oms_100_concurrent_reservations_no_concentration_breach", [&] {
        aq::NativeOms oms(tmp_dir + "/n3_conc.sqlite");
        oms.init_account("paper_native", 100000, 100000);  // $1,000
        auto spec = make_spec("NVDA");
        auto pol = make_policy();
        pol.max_order_notional_bps = 2500;
        pol.max_concentration_bps = 3500;  // Max $350 in NVDA

        std::atomic<int> approved{0};
        std::vector<std::thread> ths;
        for (int t = 0; t < 4; ++t) {
            ths.emplace_back([&, t]() {
                for (int k = 0; k < 25; ++k) {
                    std::string cid = "c-" + std::to_string(t) + "-" + std::to_string(k);
                    auto res = oms.submit_intent("paper_native", cid, spec, make_intent("NVDA", AQ_SIDE_BUY, 25, 1000), pol);
                    if (res.accepted_for_reservation) approved.fetch_add(1);
                }
            });
        }
        for (auto& th : ths) th.join();
        AQ_CHECK(approved.load() == 1);  // Only 1x $250 fits under 35% ($350) concentration cap!
    });
    run_check("c4_shadow_parity_exact_accounting_and_verdict", [] {
        auto ds = aq::generate_synthetic_dataset(160, 26);
        aq::ActorCritic ac(26);
        auto rep = aq::run_shadow_parity_check(ds, aq::default_sim_config(), &ac, "shadow26");
        AQ_CHECK(rep.exact_accounting_parity && rep.exact_verdict_parity && rep.steps_compared > 0);
    });

    std::cout << "ALL_NATIVE_CHECKS_PASSED: " << g_passed << "\n";
    return 0;
}
