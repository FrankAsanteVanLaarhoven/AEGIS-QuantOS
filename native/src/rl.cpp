#include "aq/rl.hpp"
#include "aq/storage.hpp"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <numeric>
#include <random>
#include <sstream>
#include <stdexcept>

namespace aq {

SimConfig default_sim_config() {
    SimConfig c;
    c.risk_policy.abi_version = AQ_CORE_ABI_VERSION;
    c.risk_policy.struct_size = sizeof(aq_risk_policy_t);
    c.risk_policy.max_order_notional_bps = 5500;
    c.risk_policy.max_concentration_bps = 6000;
    c.risk_policy.max_gross_exposure_bps = 9000;
    c.risk_policy.max_daily_loss_bps = 1500;
    c.risk_policy.max_total_loss_bps = 2500;
    c.risk_policy.max_data_age_ms = 30000;
    c.risk_policy.no_short_selling = 1;
    c.risk_policy.allow_protective_brackets = 0;
    return c;
}

std::array<double, kObsDim> build_causal_obs(
    const Dataset& ds,
    size_t idx,
    int64_t shares,
    int64_t cash_cents,
    int64_t peak_equity_cents) {
    if (idx < 16 || idx >= ds.bars.size()) {
        throw std::out_of_range("build_causal_obs index out of range");
    }
    auto log_ret = [&](size_t lag) {
        double c_now = static_cast<double>(ds.bars[idx].close_cents);
        double c_prev = static_cast<double>(ds.bars[idx - lag].close_cents);
        return std::log(c_now / c_prev);
    };
    double r1 = log_ret(1);
    double r4 = log_ret(4);
    double r16 = log_ret(16);

    double mean_r = 0.0;
    double rets[16]{};
    for (size_t k = 0; k < 16; ++k) {
        double c1 = static_cast<double>(ds.bars[idx - k].close_cents);
        double c0 = static_cast<double>(ds.bars[idx - k - 1].close_cents);
        rets[k] = std::log(c1 / c0);
        mean_r += rets[k];
    }
    mean_r /= 16.0;
    double var_r = 0.0;
    for (size_t k = 0; k < 16; ++k) {
        double d = rets[k] - mean_r;
        var_r += d * d;
    }
    double vol16 = std::sqrt(var_r / 16.0);

    double pos_val = static_cast<double>(shares * ds.bars[idx].close_cents);
    double eq = std::max(1.0, static_cast<double>(cash_cents) + pos_val);
    double pos_frac = pos_val / eq;
    double cash_frac = static_cast<double>(cash_cents) / eq;
    double dd = std::max(0.0, (static_cast<double>(peak_equity_cents) - eq) / std::max(1.0, static_cast<double>(peak_equity_cents)));

    return {
        std::clamp(r1 * 100.0, -10.0, 10.0),
        std::clamp(r4 * 50.0, -10.0, 10.0),
        std::clamp(r16 * 25.0, -10.0, 10.0),
        std::clamp(vol16 * 100.0, 0.0, 10.0),
        std::clamp(pos_frac, 0.0, 1.0),
        std::clamp(cash_frac, 0.0, 1.0),
        std::clamp(dd, 0.0, 1.0),
        1.0,
    };
}

CausalEnv::CausalEnv(const Dataset& ds, SimConfig cfg) : ds_(ds), cfg_(cfg) {
    if (ds_.bars.size() < cfg_.warmup_bars + 2) {
        throw std::invalid_argument("Dataset too short for CausalEnv");
    }
    std::memset(&spec_, 0, sizeof(spec_));
    spec_.abi_version = AQ_CORE_ABI_VERSION;
    spec_.struct_size = sizeof(aq_instrument_spec_t);
    std::snprintf(spec_.symbol, sizeof(spec_.symbol), "%s", ds_.symbol.c_str());
    std::snprintf(spec_.currency, sizeof(spec_.currency), "USD");
    spec_.price_scale = 100;
    spec_.qty_scale = 1;
    spec_.tick_size = 1;
    reset(cfg_.warmup_bars);
}

std::array<double, kObsDim> CausalEnv::reset(size_t start_idx) {
    idx_ = std::max(start_idx, cfg_.warmup_bars);
    cash_cents_ = cfg_.initial_cash_cents;
    shares_ = 0;
    peak_equity_cents_ = cfg_.initial_cash_cents;
    session_day_ = ds_.bars[idx_].close_epoch_s / 86400;
    day_start_equity_cents_ = cfg_.initial_cash_cents;
    prev_drawdown_ = 0.0;
    acct_rev_ = 1;
    halted_ = cfg_.halted;
    control_epoch_ = cfg_.control_epoch;
    return build_causal_obs(ds_, idx_, shares_, cash_cents_, peak_equity_cents_);
}

void CausalEnv::set_control_state(bool halted, uint64_t control_epoch) {
    halted_ = halted;
    control_epoch_ = control_epoch;
}

int64_t CausalEnv::equity_cents() const {
    int64_t pos_val = 0;
    int64_t eq = 0;
    if (aq_checked_mul_i64(shares_, ds_.bars[idx_].close_cents, &pos_val) != AQ_OK ||
        aq_checked_add_i64(cash_cents_, pos_val, &eq) != AQ_OK) {
        throw std::overflow_error("CausalEnv equity_cents overflow");
    }
    return eq;
}

StepOutcome CausalEnv::step(uint32_t action) {
    if (action >= kActionCount) throw std::invalid_argument("Invalid action");
    static constexpr double kTargets[kActionCount] = {0.0, 0.25, 0.50};

    int64_t eq_now = equity_cents();
    int64_t bar_day = ds_.bars[idx_].close_epoch_s / 86400;
    if (bar_day != session_day_) {
        session_day_ = bar_day;
        day_start_equity_cents_ = eq_now;
    }
    int64_t px_close = ds_.bars[idx_].close_cents;
    double target_notional = static_cast<double>(eq_now) * kTargets[action];
    int64_t target_shares = static_cast<int64_t>(std::floor(target_notional / static_cast<double>(px_close)));
    int64_t delta_shares = 0;
    if (aq_checked_sub_i64(target_shares, shares_, &delta_shares) != AQ_OK) {
        throw std::overflow_error("delta_shares overflow");
    }

    bool risk_rejected = false;
    int64_t executed_delta = 0;

    if (delta_shares != 0) {
        int64_t abs_delta = 0;
        if (aq_checked_abs_i64(delta_shares, &abs_delta) != AQ_OK) {
            throw std::overflow_error("abs_delta overflow");
        }
        int64_t pnl_diff = 0;
        int64_t pnl_scaled = 0;
        if (aq_checked_sub_i64(eq_now, day_start_equity_cents_, &pnl_diff) != AQ_OK ||
            aq_checked_mul_i64(pnl_diff, 10000, &pnl_scaled) != AQ_OK) {
            throw std::overflow_error("daily_pnl_bps overflow");
        }
        int64_t daily_pnl_bps = pnl_scaled / std::max<int64_t>(1, day_start_equity_cents_);

        aq_position_mark_t pm{};
        std::snprintf(pm.symbol, sizeof(pm.symbol), "%s", ds_.symbol.c_str());
        pm.qty = shares_;
        pm.mark_cents = px_close;

        aq_account_snapshot_t acct{};
        acct.abi_version = AQ_CORE_ABI_VERSION;
        acct.struct_size = sizeof(aq_account_snapshot_t);
        acct.cash_cents = cash_cents_;
        acct.equity_cents = eq_now;
        acct.reserved_buy_cents = 0;
        acct.daily_pnl_bps = daily_pnl_bps;
        acct.total_drawdown_bps = static_cast<int64_t>(std::llround(prev_drawdown_ * 10000.0));
        acct.account_revision = acct_rev_;
        acct.control_epoch = control_epoch_;
        acct.halted = halted_ ? 1u : 0u;
        acct.integrity_ok = 1;
        acct.position_count = shares_ > 0 ? 1u : 0u;
        acct.positions = shares_ > 0 ? &pm : nullptr;
        acct.reservation_count = 0;
        acct.reservations = nullptr;

        aq_order_intent_t intent{};
        intent.abi_version = AQ_CORE_ABI_VERSION;
        intent.struct_size = sizeof(aq_order_intent_t);
        std::snprintf(intent.symbol, sizeof(intent.symbol), "%s", ds_.symbol.c_str());
        intent.side = delta_shares > 0 ? AQ_SIDE_BUY : AQ_SIDE_SELL;
        intent.order_type = AQ_ORDER_MARKET;
        intent.qty = abs_delta;
        intent.price_cents = px_close;  // Causal pre-trade check uses decision-time close, never future open!
        intent.data_age_ms = 0;

        aq_risk_decision_t dec{};
        dec.abi_version = AQ_CORE_ABI_VERSION;
        dec.struct_size = sizeof(aq_risk_decision_t);
        aq_status_t st = aq_evaluate_risk(&spec_, &acct, &cfg_.risk_policy, &intent, &dec);
        if (st != AQ_OK || !dec.approved) {
            risk_rejected = true;
        } else {
            int64_t raw_open = ds_.bars[idx_ + 1].open_cents;
            int64_t adv_bps = cfg_.half_spread_bps + cfg_.slippage_bps;
            int64_t mult = delta_shares > 0 ? (10000 + adv_bps) : (10000 - adv_bps);
            int64_t px_num = 0;
            if (aq_checked_mul_i64(raw_open, mult, &px_num) != AQ_OK) {
                throw std::overflow_error("fill_px overflow");
            }
            int64_t fill_px = delta_shares > 0 ? (px_num + 9999) / 10000 : px_num / 10000;
            int64_t notional = 0;
            int64_t fee_num = 0;
            if (aq_checked_mul_i64(intent.qty, fill_px, &notional) != AQ_OK ||
                aq_checked_mul_i64(notional, cfg_.commission_bps, &fee_num) != AQ_OK) {
                throw std::overflow_error("execution notional/fee overflow");
            }
            int64_t fee = (fee_num + 9999) / 10000;
            if (delta_shares > 0) {
                int64_t total_cost = 0;
                if (aq_checked_add_i64(notional, fee, &total_cost) != AQ_OK) {
                    throw std::overflow_error("total_cost overflow");
                }
                // Execution-time check against gapped open fill price.
                intent.price_cents = fill_px;
                aq_risk_decision_t exec_dec{};
                exec_dec.abi_version = AQ_CORE_ABI_VERSION;
                exec_dec.struct_size = sizeof(aq_risk_decision_t);
                aq_status_t exec_st = aq_evaluate_risk(&spec_, &acct, &cfg_.risk_policy, &intent, &exec_dec);
                if (exec_st == AQ_OK && exec_dec.approved && total_cost <= cash_cents_) {
                    cash_cents_ -= total_cost;
                    shares_ += intent.qty;
                    executed_delta = intent.qty;
                    acct_rev_ += 1;
                } else {
                    risk_rejected = true;
                }
            } else {
                int64_t net_proceeds = 0;
                if (aq_checked_sub_i64(notional, fee, &net_proceeds) != AQ_OK) {
                    throw std::overflow_error("net_proceeds overflow");
                }
                cash_cents_ += net_proceeds;
                shares_ -= intent.qty;
                executed_delta = -intent.qty;
                acct_rev_ += 1;
            }
        }
    }

    idx_ += 1;
    int64_t eq_next = equity_cents();
    if (eq_next > peak_equity_cents_) peak_equity_cents_ = eq_next;
    double dd_next = std::max(0.0, static_cast<double>(peak_equity_cents_ - eq_next) / static_cast<double>(std::max<int64_t>(1, peak_equity_cents_)));
    double reward = 100.0 * std::log(static_cast<double>(std::max<int64_t>(1, eq_next)) / static_cast<double>(std::max<int64_t>(1, eq_now)))
                  - 2.0 * std::max(0.0, dd_next - prev_drawdown_);
    prev_drawdown_ = dd_next;

    bool at_end = (idx_ + 1 >= ds_.bars.size());
    StepOutcome out;
    out.next_obs = build_causal_obs(ds_, idx_, shares_, cash_cents_, peak_equity_cents_);
    out.reward = reward;
    out.terminated = false;
    out.truncated = at_end;
    out.risk_rejected = risk_rejected;
    out.executed_shares_delta = executed_delta;
    out.equity_cents = eq_next;
    out.cash_cents = cash_cents_;
    out.shares = shares_;
    return out;
}

std::string CausalEnv::serialize_state() const {
    uint64_t dd_bits = 0;
    std::memcpy(&dd_bits, &prev_drawdown_, sizeof(dd_bits));
    std::ostringstream oss;
    oss << idx_ << " " << cash_cents_ << " " << shares_ << " " << peak_equity_cents_ << " "
        << session_day_ << " " << day_start_equity_cents_ << " " << dd_bits << " "
        << acct_rev_ << " " << (halted_ ? 1 : 0) << " " << control_epoch_;
    return oss.str();
}

std::array<double, kObsDim> CausalEnv::restore_state(const std::string& state) {
    std::istringstream iss(state);
    uint64_t dd_bits = 0;
    int halted_int = 0;
    if (!(iss >> idx_ >> cash_cents_ >> shares_ >> peak_equity_cents_ >>
          session_day_ >> day_start_equity_cents_ >> dd_bits >> acct_rev_ >>
          halted_int >> control_epoch_)) {
        throw std::runtime_error("Invalid CausalEnv serialized state");
    }
    halted_ = (halted_int != 0);
    std::memcpy(&prev_drawdown_, &dd_bits, sizeof(prev_drawdown_));
    return build_causal_obs(ds_, idx_, shares_, cash_cents_, peak_equity_cents_);
}

void compute_gae(
    const std::vector<GaeTransition>& traj,
    double last_bootstrap_value,
    double gamma,
    double gae_lambda,
    std::vector<double>& out_advantages,
    std::vector<double>& out_returns) {
    const size_t n = traj.size();
    out_advantages.assign(n, 0.0);
    out_returns.assign(n, 0.0);
    double gae = 0.0;
    for (size_t rev = 0; rev < n; ++rev) {
        size_t t = n - 1 - rev;
        const auto& tr = traj[t];
        double next_val = 0.0;
        if (tr.terminated) {
            next_val = 0.0;
        } else if (tr.truncated) {
            next_val = tr.bootstrap_value;
        } else if (t + 1 < n) {
            next_val = traj[t + 1].value;
        } else {
            next_val = last_bootstrap_value;
        }
        double non_terminal = tr.terminated ? 0.0 : 1.0;
        double delta = tr.reward + gamma * next_val * non_terminal - tr.value;
        double carry = (tr.terminated || tr.truncated) ? 0.0 : (gamma * gae_lambda);
        gae = delta + carry * gae;
        out_advantages[t] = gae;
        out_returns[t] = gae + tr.value;
    }
}

std::string sim_config_hash(const SimConfig& scfg, const TrainConfig& tcfg) {
    std::ostringstream oss;
    oss << scfg.initial_cash_cents << "|" << scfg.half_spread_bps << "|" << scfg.slippage_bps << "|"
        << scfg.commission_bps << "|" << scfg.warmup_bars << "|"
        << scfg.risk_policy.max_order_notional_bps << "|"
        << scfg.risk_policy.max_concentration_bps << "|"
        << scfg.risk_policy.max_gross_exposure_bps << "|"
        << scfg.risk_policy.max_daily_loss_bps << "|"
        << scfg.risk_policy.max_total_loss_bps << "|"
        << scfg.risk_policy.max_data_age_ms << "|"
        << static_cast<int>(scfg.risk_policy.no_short_selling) << "|"
        << static_cast<int>(scfg.risk_policy.allow_protective_brackets) << "|"
        << tcfg.gamma << "|" << tcfg.gae_lambda << "|" << tcfg.clip_eps << "|"
        << tcfg.vf_coef << "|" << tcfg.ent_coef << "|" << tcfg.lr << "|"
        << tcfg.rollout_steps << "|" << tcfg.ppo_epochs << "|" << tcfg.minibatch_size;
    return sha256_hex(oss.str());
}

TrainSummary train_ppo(
    const Dataset& ds,
    const std::string& out_dir,
    const TrainConfig& tcfg,
    const SimConfig& scfg,
    const std::string& resume_checkpoint,
    const std::string& split_tag) {
    std::filesystem::create_directories(out_dir);
    std::string expected_cfg_hash = sim_config_hash(scfg, tcfg);
    ActorCritic ac = resume_checkpoint.empty() ? ActorCritic(tcfg.seed) : ActorCritic::load_checkpoint(resume_checkpoint);
    if (!resume_checkpoint.empty()) {
        if (ac.feature_version != 1 || ac.action_version != 1 || ac.optimizer_format != 1) {
            throw std::runtime_error("Incompatible checkpoint schema/optimizer format for resume");
        }
        if (ac.dataset_hash != ds.dataset_hash) {
            throw std::runtime_error("Checkpoint dataset_hash mismatch on resume");
        }
        if (ac.split_tag != split_tag) {
            throw std::runtime_error("Checkpoint split_tag mismatch on resume");
        }
        if (ac.config_hash != expected_cfg_hash) {
            throw std::runtime_error("Checkpoint reward/cost config_hash mismatch on resume");
        }
    } else {
        ac.dataset_hash = ds.dataset_hash;
        ac.split_tag = split_tag;
        ac.config_hash = expected_cfg_hash;
    }

    std::mt19937_64 rng(tcfg.seed + 1000);
    if (!ac.rng_state_serialized.empty()) {
        std::istringstream iss(ac.rng_state_serialized);
        iss >> rng;
    }

    CausalEnv env(ds, scfg);
    auto obs = ac.env_state_serialized.empty()
                   ? env.reset(scfg.warmup_bars)
                   : env.restore_state(ac.env_state_serialized);
    size_t total_transitions = 0;

    for (uint32_t upd = 0; upd < tcfg.updates; ++upd) {
        std::vector<GaeTransition> traj;
        traj.reserve(tcfg.rollout_steps);
        for (size_t step = 0; step < tcfg.rollout_steps; ++step) {
            auto af = ac.forward_actor(obs);
            auto cf = ac.forward_critic(obs);
            std::discrete_distribution<uint32_t> dist(af.probs.begin(), af.probs.end());
            uint32_t act = dist(rng);
            double lp = std::log(std::max(af.probs[act], 1e-12));

            StepOutcome so = env.step(act);
            double bootstrap_val = 0.0;
            if (so.truncated && !so.terminated) {
                bootstrap_val = ac.forward_critic(so.next_obs).value;
            }
            traj.push_back(GaeTransition{obs, act, lp, cf.value, so.reward, bootstrap_val, so.terminated, so.truncated});
            total_transitions += 1;
            if (so.terminated || so.truncated) {
                obs = env.reset(scfg.warmup_bars);
            } else {
                obs = so.next_obs;
            }
        }
        double last_val = ac.forward_critic(obs).value;
        std::vector<double> adv, ret;
        compute_gae(traj, last_val, tcfg.gamma, tcfg.gae_lambda, adv, ret);

        double mean_a = std::accumulate(adv.begin(), adv.end(), 0.0) / static_cast<double>(adv.size());
        double var_a = 0.0;
        for (double a : adv) var_a += (a - mean_a) * (a - mean_a);
        double std_a = std::sqrt(var_a / static_cast<double>(adv.size()) + 1e-8);
        for (double& a : adv) a = (a - mean_a) / std_a;

        std::vector<size_t> indices(traj.size());
        std::iota(indices.begin(), indices.end(), 0);
        for (uint32_t ep = 0; ep < tcfg.ppo_epochs; ++ep) {
            std::shuffle(indices.begin(), indices.end(), rng);
            double ep_kl = 0.0;
            size_t ep_cnt = 0;
            for (size_t start = 0; start < indices.size(); start += tcfg.minibatch_size) {
                size_t end = std::min(indices.size(), start + tcfg.minibatch_size);
                double scale = 1.0 / static_cast<double>(end - start);
                std::vector<double> ga(kActorParamCount, 0.0);
                std::vector<double> gc(kCriticParamCount, 0.0);
                for (size_t k = start; k < end; ++k) {
                    const auto& tr = traj[indices[k]];
                    double pi_l = 0, vf_l = 0, ent = 0, kl = 0;
                    ac.accumulate_ppo_grad(
                        tr.obs, tr.action, tr.log_prob, adv[indices[k]], ret[indices[k]],
                        tcfg.clip_eps, tcfg.vf_coef, tcfg.ent_coef, scale, ga, gc, pi_l, vf_l, ent, kl);
                    ep_kl += kl;
                    ep_cnt += 1;
                }
                ac.adam_step(ga, gc, tcfg.lr, tcfg.max_grad_norm);
            }
            if (ep_cnt > 0 && (ep_kl / static_cast<double>(ep_cnt)) > tcfg.target_kl) break;
        }
        ac.update_count += 1;
    }

    std::ostringstream rng_oss;
    rng_oss << rng;
    ac.rng_state_serialized = rng_oss.str();
    ac.env_state_serialized = env.serialize_state();

    std::string ckpt_path = out_dir + "/policy.aq";
    std::string ckpt_hash = ac.save_checkpoint(ckpt_path);
    auto receipt = run_replay_episode(ds, &ac, ckpt_hash, scfg);

    TrainSummary sum;
    sum.updates_completed = tcfg.updates;
    sum.transitions = total_transitions;
    sum.final_eval_equity_usd = static_cast<double>(receipt.final_equity_cents) / 100.0;
    sum.checkpoint_path = ckpt_path;
    sum.checkpoint_hash = ckpt_hash;
    return sum;
}

ReplayReceipt run_replay_episode(
    const Dataset& ds,
    const ActorCritic* model_or_null,
    const std::string& model_hash,
    const SimConfig& scfg,
    int fixed_action) {
    CausalEnv env(ds, scfg);
    auto obs = env.reset(scfg.warmup_bars);
    ReplayReceipt rec;
    rec.dataset_hash = ds.dataset_hash;
    rec.model_hash = model_hash;

    while (true) {
        uint32_t act = 0;
        if (fixed_action >= 0) {
            act = static_cast<uint32_t>(fixed_action);
        } else if (model_or_null != nullptr) {
            auto af = model_or_null->forward_actor(obs);
            act = static_cast<uint32_t>(std::distance(af.probs.begin(), std::max_element(af.probs.begin(), af.probs.end())));
        }
        StepOutcome so = env.step(act);
        rec.steps += 1;
        if (so.risk_rejected) rec.risk_interventions += 1;
        if (so.executed_shares_delta != 0) rec.trades_executed += 1;
        obs = so.next_obs;
        if (so.terminated || so.truncated) {
            rec.final_cash_cents = so.cash_cents;
            rec.final_shares = so.shares;
            rec.final_equity_cents = so.equity_cents;
            break;
        }
    }
    std::ostringstream oss;
    oss << rec.dataset_hash << "|" << rec.model_hash << "|" << rec.final_cash_cents << "|"
        << rec.final_shares << "|" << rec.final_equity_cents << "|" << rec.steps << "|"
        << rec.risk_interventions << "|" << rec.trades_executed;
    rec.receipt_hash = sha256_hex(oss.str());
    return rec;
}

}  // namespace aq
