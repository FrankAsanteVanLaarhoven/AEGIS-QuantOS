#pragma once

#include "aq/core.h"
#include "aq/data.hpp"
#include "aq/model.hpp"

#include <array>
#include <cstdint>
#include <string>
#include <vector>

namespace aq {

struct SimConfig {
    int64_t initial_cash_cents = 10000000;  // $100,000.00
    int64_t half_spread_bps = 2;
    int64_t slippage_bps = 4;
    int64_t commission_bps = 5;
    size_t warmup_bars = 16;
    aq_risk_policy_t risk_policy{};
};

SimConfig default_sim_config();

std::array<double, kObsDim> build_causal_obs(
    const Dataset& ds,
    size_t idx,
    int64_t shares,
    int64_t cash_cents,
    int64_t peak_equity_cents);

struct StepOutcome {
    std::array<double, kObsDim> next_obs{};
    double reward = 0.0;
    bool terminated = false;
    bool truncated = false;
    bool risk_rejected = false;
    int64_t executed_shares_delta = 0;
    int64_t equity_cents = 0;
    int64_t cash_cents = 0;
    int64_t shares = 0;
};

class CausalEnv {
public:
    CausalEnv(const Dataset& ds, SimConfig cfg = default_sim_config());

    std::array<double, kObsDim> reset(size_t start_idx = 16);
    StepOutcome step(uint32_t action);
    std::string serialize_state() const;
    std::array<double, kObsDim> restore_state(const std::string& state);

    int64_t cash_cents() const { return cash_cents_; }
    int64_t shares() const { return shares_; }
    int64_t equity_cents() const;
    size_t index() const { return idx_; }

private:
    const Dataset& ds_;
    SimConfig cfg_;
    aq_instrument_spec_t spec_{};
    size_t idx_ = 16;
    int64_t cash_cents_ = 0;
    int64_t shares_ = 0;
    int64_t peak_equity_cents_ = 0;
    double prev_drawdown_ = 0.0;
    uint64_t acct_rev_ = 1;
};

struct GaeTransition {
    std::array<double, kObsDim> obs{};
    uint32_t action = 0;
    double log_prob = 0.0;
    double value = 0.0;
    double reward = 0.0;
    bool terminated = false;
    bool truncated = false;
};

void compute_gae(
    const std::vector<GaeTransition>& traj,
    double last_bootstrap_value,
    double gamma,
    double gae_lambda,
    std::vector<double>& out_advantages,
    std::vector<double>& out_returns);

struct TrainConfig {
    uint64_t seed = 26;
    uint32_t updates = 32;
    size_t rollout_steps = 128;
    uint32_t ppo_epochs = 4;
    size_t minibatch_size = 32;
    double gamma = 0.99;
    double gae_lambda = 0.95;
    double clip_eps = 0.2;
    double vf_coef = 0.5;
    double ent_coef = 0.01;
    double lr = 3e-4;
    double max_grad_norm = 0.5;
    double target_kl = 0.03;
};

struct TrainSummary {
    uint32_t updates_completed = 0;
    size_t transitions = 0;
    double final_eval_equity_usd = 0.0;
    std::string checkpoint_path;
    std::string checkpoint_hash;
};

TrainSummary train_ppo(
    const Dataset& ds,
    const std::string& out_dir,
    const TrainConfig& tcfg = TrainConfig{},
    const SimConfig& scfg = default_sim_config(),
    const std::string& resume_checkpoint = "");

struct ReplayReceipt {
    std::string dataset_hash;
    std::string model_hash;
    std::string receipt_hash;
    int64_t final_cash_cents = 0;
    int64_t final_shares = 0;
    int64_t final_equity_cents = 0;
    uint64_t steps = 0;
    uint64_t risk_interventions = 0;
    uint64_t trades_executed = 0;
};

ReplayReceipt run_replay_episode(
    const Dataset& ds,
    const ActorCritic* model_or_null,
    const std::string& model_hash,
    const SimConfig& scfg = default_sim_config(),
    int fixed_action = -1);

}  // namespace aq
