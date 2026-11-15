#include "aq/core.h"
#include "aq/inference.h"
#include "aq/model.hpp"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <memory>
#include <string>

struct aq_model_handle {
    aq::ActorCritic model;
    std::string model_hash;
    explicit aq_model_handle(aq::ActorCritic&& m, std::string h)
        : model(std::move(m)), model_hash(std::move(h)) {}
};

extern "C" {

int aq_model_load(const char* checkpoint_path, aq_model_handle** out_handle) {
    if (!checkpoint_path || !out_handle) return AQ_ERR_NULL;
    *out_handle = nullptr;
    try {
        std::string h;
        aq::ActorCritic m = aq::ActorCritic::load_checkpoint(checkpoint_path, &h);
        auto ptr = std::make_unique<aq_model_handle>(std::move(m), std::move(h));
        *out_handle = ptr.release();
        return AQ_OK;
    } catch (...) {
        return AQ_ERR_CORRUPT;
    }
}

void aq_model_free(aq_model_handle* handle) {
    delete handle;
}

int aq_model_predict_v1(
    const aq_model_handle* handle,
    const aq_obs_v1_t* obs,
    aq_proposal_v1_t* out_proposal) {
    if (!handle || !obs || !out_proposal) return AQ_ERR_NULL;
    if (obs->abi_version != AQ_INFERENCE_ABI_VERSION ||
        out_proposal->abi_version != AQ_INFERENCE_ABI_VERSION) {
        return AQ_ERR_ABI_VERSION;
    }
    if (obs->struct_size != sizeof(aq_obs_v1_t) ||
        out_proposal->struct_size != sizeof(aq_proposal_v1_t)) {
        return AQ_ERR_STRUCT_SIZE;
    }
    if (obs->feature_schema_version != 1u) {
        return AQ_ERR_INVALID_ARG;
    }
    try {
        std::array<double, aq::kObsDim> x{};
        for (size_t i = 0; i < aq::kObsDim; ++i) {
            if (!std::isfinite(obs->features[i])) return AQ_ERR_INVALID_ARG;
            x[i] = obs->features[i];
        }
        const auto af = handle->model.forward_actor(x);
        const auto cf = handle->model.forward_critic(x);
        static constexpr double kTargets[aq::kActionCount] = {0.0, 0.25, 0.50};
        uint32_t best = 0;
        for (uint32_t a = 1; a < aq::kActionCount; ++a) {
            if (af.probs[a] > af.probs[best]) best = a;
        }

        std::memset(out_proposal, 0, sizeof(*out_proposal));
        out_proposal->abi_version = AQ_INFERENCE_ABI_VERSION;
        out_proposal->struct_size = sizeof(aq_proposal_v1_t);
        out_proposal->action_schema_version = 1u;
        out_proposal->chosen_action = best;
        out_proposal->target_exposure_pct = kTargets[best];
        for (size_t a = 0; a < aq::kActionCount; ++a) {
            out_proposal->action_probs[a] = af.probs[a];
        }
        out_proposal->value_estimate = cf.value;
        out_proposal->execution_authorized = 0u;  /* Proposals never authorize execution */
        std::snprintf(out_proposal->model_hash, sizeof(out_proposal->model_hash), "%s", handle->model_hash.c_str());
        return AQ_OK;
    } catch (...) {
        return AQ_ERR_INVALID_ARG;
    }
}

}  // extern "C"

