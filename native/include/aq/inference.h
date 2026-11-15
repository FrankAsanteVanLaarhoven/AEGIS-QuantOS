#ifndef AQ_INFERENCE_H
#define AQ_INFERENCE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define AQ_INFERENCE_ABI_VERSION 1u
#define AQ_OBS_DIM_V1 8u
#define AQ_ACTION_COUNT_V1 3u

typedef struct aq_model_handle aq_model_handle;

typedef struct aq_obs_v1_t {
    uint32_t abi_version;
    uint32_t struct_size;
    uint32_t feature_schema_version; /* Must be 1 */
    double features[AQ_OBS_DIM_V1];
} aq_obs_v1_t;

typedef struct aq_proposal_v1_t {
    uint32_t abi_version;
    uint32_t struct_size;
    uint32_t action_schema_version;  /* 1 */
    uint32_t chosen_action;          /* 0 = 0%, 1 = 25%, 2 = 50% */
    double target_exposure_pct;      /* 0.0, 0.25, or 0.50 */
    double action_probs[AQ_ACTION_COUNT_V1];
    double value_estimate;
    uint8_t execution_authorized;    /* ALWAYS 0: proposal only */
    char model_hash[65];             /* Hex SHA-256 of loaded checkpoint */
} aq_proposal_v1_t;

int aq_model_load(const char* checkpoint_path, aq_model_handle** out_handle);
void aq_model_free(aq_model_handle* handle);
int aq_model_predict_v1(
    const aq_model_handle* handle,
    const aq_obs_v1_t* obs,
    aq_proposal_v1_t* out_proposal);

#ifdef __cplusplus
}
#endif

#endif /* AQ_INFERENCE_H */
