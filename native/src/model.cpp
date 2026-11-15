#include "aq/model.hpp"
#include "aq/storage.hpp"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <fstream>
#include <sstream>
#include <stdexcept>

namespace aq {

namespace {
constexpr char kMagic[8] = {'A', 'Q', 'C', 'P', 'v', '0', '0', '1'};

void xavier_init(std::mt19937_64& rng, double* dst, size_t fan_in, size_t fan_out, double gain = 1.0) {
    double bound = gain * std::sqrt(6.0 / static_cast<double>(fan_in + fan_out));
    std::uniform_real_distribution<double> dist(-bound, bound);
    for (size_t i = 0; i < fan_in * fan_out; ++i) dst[i] = dist(rng);
}
}  // namespace

ActorCritic::ActorCritic(uint64_t seed)
    : actor_p_(kActorParamCount, 0.0),
      critic_p_(kCriticParamCount, 0.0) {
    actor_adam_.m.assign(kActorParamCount, 0.0);
    actor_adam_.v.assign(kActorParamCount, 0.0);
    critic_adam_.m.assign(kCriticParamCount, 0.0);
    critic_adam_.v.assign(kCriticParamCount, 0.0);

    std::mt19937_64 rng(seed);
    // Actor: W1(8x32), b1(32), W2(32x3), b2(3)
    xavier_init(rng, actor_p_.data(), kObsDim, kHiddenDim, 1.0);
    xavier_init(rng, actor_p_.data() + kObsDim * kHiddenDim + kHiddenDim, kHiddenDim, kActionCount, 0.01);
    // Critic: W1(8x32), b1(32), W2(32x1), b2(1)
    xavier_init(rng, critic_p_.data(), kObsDim, kHiddenDim, 1.0);
    xavier_init(rng, critic_p_.data() + kObsDim * kHiddenDim + kHiddenDim, kHiddenDim, 1, 1.0);
}

ActorForward ActorCritic::forward_actor(const std::array<double, kObsDim>& obs) const {
    ActorForward out;
    const double* w1 = actor_p_.data();
    const double* b1 = w1 + kObsDim * kHiddenDim;
    const double* w2 = b1 + kHiddenDim;
    const double* b2 = w2 + kHiddenDim * kActionCount;

    for (size_t j = 0; j < kHiddenDim; ++j) {
        double z = b1[j];
        for (size_t i = 0; i < kObsDim; ++i) z += obs[i] * w1[i * kHiddenDim + j];
        out.h[j] = std::tanh(z);
    }
    double max_l = -1e300;
    for (size_t a = 0; a < kActionCount; ++a) {
        double z = b2[a];
        for (size_t j = 0; j < kHiddenDim; ++j) z += out.h[j] * w2[j * kActionCount + a];
        out.logits[a] = z;
        if (z > max_l) max_l = z;
    }
    double sum_e = 0.0;
    for (size_t a = 0; a < kActionCount; ++a) {
        out.probs[a] = std::exp(out.logits[a] - max_l);
        sum_e += out.probs[a];
    }
    for (size_t a = 0; a < kActionCount; ++a) out.probs[a] /= sum_e;
    return out;
}

CriticForward ActorCritic::forward_critic(const std::array<double, kObsDim>& obs) const {
    CriticForward out;
    const double* w1 = critic_p_.data();
    const double* b1 = w1 + kObsDim * kHiddenDim;
    const double* w2 = b1 + kHiddenDim;
    const double* b2 = w2 + kHiddenDim;

    for (size_t j = 0; j < kHiddenDim; ++j) {
        double z = b1[j];
        for (size_t i = 0; i < kObsDim; ++i) z += obs[i] * w1[i * kHiddenDim + j];
        out.h[j] = std::tanh(z);
    }
    double v = b2[0];
    for (size_t j = 0; j < kHiddenDim; ++j) v += out.h[j] * w2[j];
    out.value = v;
    return out;
}

void ActorCritic::accumulate_ppo_grad(
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
    double& out_kl) const {
    const ActorForward af = forward_actor(obs);
    const CriticForward cf = forward_critic(obs);

    double log_p[kActionCount]{};
    double entropy = 0.0;
    for (size_t a = 0; a < kActionCount; ++a) {
        double p = std::max(af.probs[a], 1e-12);
        log_p[a] = std::log(p);
        entropy -= af.probs[a] * log_p[a];
    }
    double new_lp = log_p[action];
    double log_ratio = new_lp - old_log_prob;
    double ratio = std::exp(log_ratio);
    out_kl = (ratio - 1.0) - log_ratio;
    out_entropy = entropy;

    double surr1 = ratio * advantage;
    double clipped_r = std::clamp(ratio, 1.0 - clip_eps, 1.0 + clip_eps);
    double surr2 = clipped_r * advantage;
    bool clipped = (surr2 < surr1) && ((advantage >= 0.0 && ratio > 1.0 + clip_eps) || (advantage < 0.0 && ratio < 1.0 - clip_eps));
    out_pi_loss = -std::min(surr1, surr2);

    // d(pi_loss)/d(new_lp): if unclipped -> -ratio * advantage; if clipped -> 0.
    double dL_d_newlp = clipped ? 0.0 : (-ratio * advantage);
    // d(entropy)/d(logits_a) = -p_a * (log_p_a + entropy)
    // d(new_lp)/d(logits_a) = (a == action ? 1 : 0) - p_a
    double d_logits[kActionCount]{};
    for (size_t a = 0; a < kActionCount; ++a) {
        double d_lp_d_z = (a == action ? 1.0 : 0.0) - af.probs[a];
        double d_ent_d_z = -af.probs[a] * (log_p[a] + entropy);
        d_logits[a] = scale * (dL_d_newlp * d_lp_d_z - ent_coef * d_ent_d_z);
    }

    // Backprop through Actor W2, b2, W1, b1
    const size_t off_b1 = kObsDim * kHiddenDim;
    const size_t off_w2 = off_b1 + kHiddenDim;
    const size_t off_b2 = off_w2 + kHiddenDim * kActionCount;
    const double* w2 = actor_p_.data() + off_w2;

    double d_h[kHiddenDim]{};
    for (size_t a = 0; a < kActionCount; ++a) {
        grad_actor[off_b2 + a] += d_logits[a];
        for (size_t j = 0; j < kHiddenDim; ++j) {
            grad_actor[off_w2 + j * kActionCount + a] += af.h[j] * d_logits[a];
            d_h[j] += w2[j * kActionCount + a] * d_logits[a];
        }
    }
    for (size_t j = 0; j < kHiddenDim; ++j) {
        double d_z1 = d_h[j] * (1.0 - af.h[j] * af.h[j]);
        grad_actor[off_b1 + j] += d_z1;
        for (size_t i = 0; i < kObsDim; ++i) {
            grad_actor[i * kHiddenDim + j] += obs[i] * d_z1;
        }
    }

    // Critic: vf_loss = 0.5 * (v - ret)^2 -> scaled by vf_coef
    double diff = cf.value - return_target;
    out_vf_loss = 0.5 * diff * diff;
    double d_v = scale * vf_coef * diff;

    const size_t c_off_b1 = kObsDim * kHiddenDim;
    const size_t c_off_w2 = c_off_b1 + kHiddenDim;
    const size_t c_off_b2 = c_off_w2 + kHiddenDim;
    const double* cw2 = critic_p_.data() + c_off_w2;
    grad_critic[c_off_b2] += d_v;
    for (size_t j = 0; j < kHiddenDim; ++j) {
        grad_critic[c_off_w2 + j] += cf.h[j] * d_v;
        double d_ch = cw2[j] * d_v;
        double d_cz1 = d_ch * (1.0 - cf.h[j] * cf.h[j]);
        grad_critic[c_off_b1 + j] += d_cz1;
        for (size_t i = 0; i < kObsDim; ++i) {
            grad_critic[i * kHiddenDim + j] += obs[i] * d_cz1;
        }
    }
}

void ActorCritic::adam_step(
    const std::vector<double>& grad_actor,
    const std::vector<double>& grad_critic,
    double lr,
    double max_grad_norm) {
    double sq_norm = 0.0;
    for (double g : grad_actor) sq_norm += g * g;
    for (double g : grad_critic) sq_norm += g * g;
    double norm = std::sqrt(sq_norm);
    double clip_scale = (max_grad_norm > 0.0 && norm > max_grad_norm) ? (max_grad_norm / norm) : 1.0;

    constexpr double beta1 = 0.9;
    constexpr double beta2 = 0.999;
    constexpr double eps = 1e-8;

    auto apply = [&](std::vector<double>& params, AdamState& st, const std::vector<double>& grad) {
        st.t += 1;
        double b1_corr = 1.0 - std::pow(beta1, static_cast<double>(st.t));
        double b2_corr = 1.0 - std::pow(beta2, static_cast<double>(st.t));
        for (size_t i = 0; i < params.size(); ++i) {
            double g = grad[i] * clip_scale;
            st.m[i] = beta1 * st.m[i] + (1.0 - beta1) * g;
            st.v[i] = beta2 * st.v[i] + (1.0 - beta2) * g * g;
            double m_hat = st.m[i] / b1_corr;
            double v_hat = st.v[i] / b2_corr;
            params[i] -= lr * m_hat / (std::sqrt(v_hat) + eps);
        }
    };
    apply(actor_p_, actor_adam_, grad_actor);
    apply(critic_p_, critic_adam_, grad_critic);
}

std::string ActorCritic::save_checkpoint(const std::string& path) const {
    std::ostringstream payload(std::ios::binary);
    payload.write(kMagic, 8);
    auto write_u32 = [&](uint32_t v) { payload.write(reinterpret_cast<const char*>(&v), sizeof(v)); };
    auto write_u64 = [&](uint64_t v) { payload.write(reinterpret_cast<const char*>(&v), sizeof(v)); };
    auto write_str = [&](const std::string& s) {
        uint32_t n = static_cast<uint32_t>(s.size());
        write_u32(n);
        if (n) payload.write(s.data(), n);
    };
    auto write_vec = [&](const std::vector<double>& v) {
        uint32_t n = static_cast<uint32_t>(v.size());
        write_u32(n);
        if (n) payload.write(reinterpret_cast<const char*>(v.data()), n * sizeof(double));
    };

    write_u32(feature_version);
    write_u32(action_version);
    write_u64(update_count);
    write_str(dataset_hash);
    write_str(rng_state_serialized);
    write_str(env_state_serialized);
    write_u64(actor_adam_.t);
    write_u64(critic_adam_.t);
    write_vec(actor_p_);
    write_vec(actor_adam_.m);
    write_vec(actor_adam_.v);
    write_vec(critic_p_);
    write_vec(critic_adam_.m);
    write_vec(critic_adam_.v);

    std::string raw = payload.str();
    std::string digest = sha256_hex(raw);

    std::ofstream ofs(path, std::ios::binary);
    if (!ofs) throw std::runtime_error("Failed to open checkpoint for write: " + path);
    ofs.write(raw.data(), static_cast<std::streamsize>(raw.size()));
    ofs.write(digest.data(), 64);
    ofs.close();
    return sha256_hex(raw + digest);
}

ActorCritic ActorCritic::load_checkpoint(const std::string& path, std::string* out_file_hash) {
    std::ifstream ifs(path, std::ios::binary);
    if (!ifs) throw std::runtime_error("Cannot open checkpoint: " + path);
    std::ostringstream oss;
    oss << ifs.rdbuf();
    std::string full = oss.str();
    if (full.size() < 8 + 64 || full.size() > 16 * 1024 * 1024) {
        throw std::runtime_error("Invalid checkpoint size");
    }
    std::string raw = full.substr(0, full.size() - 64);
    std::string tag = full.substr(full.size() - 64);
    if (sha256_hex(raw) != tag) {
        throw std::runtime_error("Checkpoint integrity hash mismatch");
    }
    if (std::memcmp(raw.data(), kMagic, 8) != 0) {
        throw std::runtime_error("Invalid checkpoint magic");
    }
    if (out_file_hash) *out_file_hash = sha256_hex(full);

    size_t pos = 8;
    auto read_bytes = [&](void* dst, size_t n) {
        if (pos + n > raw.size()) throw std::runtime_error("Truncated checkpoint");
        std::memcpy(dst, raw.data() + pos, n);
        pos += n;
    };
    auto read_u32 = [&]() { uint32_t v = 0; read_bytes(&v, sizeof(v)); return v; };
    auto read_u64 = [&]() { uint64_t v = 0; read_bytes(&v, sizeof(v)); return v; };
    auto read_str = [&]() {
        uint32_t n = read_u32();
        if (n > 1024 * 1024 || pos + n > raw.size()) throw std::runtime_error("Invalid checkpoint string length");
        std::string s(raw.data() + pos, n);
        pos += n;
        return s;
    };
    auto read_vec = [&](size_t expected_len) {
        uint32_t n = read_u32();
        if (n != expected_len || pos + n * sizeof(double) > raw.size()) {
            throw std::runtime_error("Invalid checkpoint vector length");
        }
        std::vector<double> v(n);
        read_bytes(v.data(), n * sizeof(double));
        for (double x : v) {
            if (!std::isfinite(x)) throw std::runtime_error("Non-finite weight in checkpoint");
        }
        return v;
    };

    ActorCritic ac(0);
    ac.feature_version = read_u32();
    ac.action_version = read_u32();
    ac.update_count = read_u64();
    ac.dataset_hash = read_str();
    ac.rng_state_serialized = read_str();
    ac.env_state_serialized = read_str();
    ac.actor_adam_.t = read_u64();
    ac.critic_adam_.t = read_u64();
    ac.actor_p_ = read_vec(kActorParamCount);
    ac.actor_adam_.m = read_vec(kActorParamCount);
    ac.actor_adam_.v = read_vec(kActorParamCount);
    ac.critic_p_ = read_vec(kCriticParamCount);
    ac.critic_adam_.m = read_vec(kCriticParamCount);
    ac.critic_adam_.v = read_vec(kCriticParamCount);
    if (pos != raw.size()) throw std::runtime_error("Trailing bytes in checkpoint");
    return ac;
}

}  // namespace aq
