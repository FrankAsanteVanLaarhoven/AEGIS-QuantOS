#pragma once

#include <array>
#include <cstdint>
#include <random>
#include <string>
#include <vector>

namespace aq {

inline constexpr size_t kObsDim = 8;
inline constexpr size_t kHiddenDim = 32;
inline constexpr size_t kActionCount = 3;
inline constexpr size_t kActorParamCount = kObsDim * kHiddenDim + kHiddenDim + kHiddenDim * kActionCount + kActionCount;  // 387
inline constexpr size_t kCriticParamCount = kObsDim * kHiddenDim + kHiddenDim + kHiddenDim * 1 + 1;                      // 321
inline constexpr size_t kTotalParamCount = kActorParamCount + kCriticParamCount;                                         // 708

struct ActorForward {
    std::array<double, kHiddenDim> h{};
    std::array<double, kActionCount> logits{};
    std::array<double, kActionCount> probs{};
};

struct CriticForward {
    std::array<double, kHiddenDim> h{};
    double value = 0.0;
};

struct AdamState {
    uint64_t t = 0;
    std::vector<double> m;
    std::vector<double> v;
};

class ActorCritic {
public:
    explicit ActorCritic(uint64_t seed = 26);

    ActorForward forward_actor(const std::array<double, kObsDim>& obs) const;
    CriticForward forward_critic(const std::array<double, kObsDim>& obs) const;

    /* Accumulate analytic gradients of PPO loss:
       loss = -min(ratio*adv, clip(ratio, 1-eps, 1+eps)*adv) - ent_coef*entropy + vf_coef*0.5*(V - ret)^2
       Returns {policy_loss, value_loss, entropy, approx_kl}. */
    void accumulate_ppo_grad(
        const std::array<double, kObsDim>& obs,
        uint32_t action,
        double old_log_prob,
        double advantage,
        double return_target,
        double clip_eps,
        double vf_coef,
        double ent_coef,
        double scale,
        std::vector<double>& grad_actor,
        std::vector<double>& grad_critic,
        double& out_pi_loss,
        double& out_vf_loss,
        double& out_entropy,
        double& out_kl) const;

    void adam_step(
        const std::vector<double>& grad_actor,
        const std::vector<double>& grad_critic,
        double lr,
        double max_grad_norm);

    std::vector<double>& actor_params() { return actor_p_; }
    const std::vector<double>& actor_params() const { return actor_p_; }
    std::vector<double>& critic_params() { return critic_p_; }
    const std::vector<double>& critic_params() const { return critic_p_; }

    uint64_t update_count = 0;
    uint32_t feature_version = 1;
    uint32_t action_version = 1;
    uint32_t optimizer_format = 1;
    std::string dataset_hash;
    std::string split_tag = "train";
    std::string config_hash;
    std::string rng_state_serialized;
    std::string env_state_serialized;

    static ActorCritic init_transfer_from(
        const ActorCritic& source,
        uint64_t new_seed,
        const std::string& new_dataset_hash,
        const std::string& new_config_hash,
        const std::string& new_split_tag = "train");

    std::string save_checkpoint(const std::string& path) const;
    static ActorCritic load_checkpoint(const std::string& path, std::string* out_file_hash = nullptr);

private:
    std::vector<double> actor_p_;
    std::vector<double> critic_p_;
    AdamState actor_adam_;
    AdamState critic_adam_;
};

}  // namespace aq
