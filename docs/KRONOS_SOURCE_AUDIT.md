# Kronos — source inspection and reproduction boundary

## Provenance

Review date: 14 November 2026. Upstream: `shiyu-coder/Kronos`. Inspected commit: `67b630e67f6a18c9e9be918d9b4337c960db1e9a`.

Primary references:

- Shi, Y., Fu, Z., Chen, S., Zhao, B., Xu, W., Zhang, C., & Li, J. (2025/2026). *Kronos: A Foundation Model for the Language of Financial Markets*. arXiv:2508.02739. https://arxiv.org/abs/2508.02739
- Pinned implementation: https://github.com/shiyu-coder/Kronos/tree/67b630e67f6a18c9e9be918d9b4337c960db1e9a
- Inference implementation: https://github.com/shiyu-coder/Kronos/blob/67b630e67f6a18c9e9be918d9b4337c960db1e9a/model/kronos.py
- Neural components: https://github.com/shiyu-coder/Kronos/blob/67b630e67f6a18c9e9be918d9b4337c960db1e9a/model/module.py
- License: https://github.com/shiyu-coder/Kronos/blob/67b630e67f6a18c9e9be918d9b4337c960db1e9a/LICENSE

The license inspected is MIT, copyright (c) 2025 ShiYu. Upstream notices remain intact when the installer clones the source. Model artifact terms and source-code licensing are distinct checks; retain and review the downloaded model cards as well.

## What was actually done

The GitHub connector returned the commit, file tree, model implementation and license. Public raw source was also inspected through the web reader. A direct container `git clone` failed because `github.com` could not be resolved; the file-download route also failed. No pretrained checkpoint was downloaded. Consequently this deliverable includes a source-pinned adapter, installer, integration tests and architectural audit, **not a demonstrated reproduction of pretrained outputs or a newly trained Kronos clone**.

## Reconstructed inference path

| Stage | Upstream mechanism | AEGIS treatment |
|---|---|---|
| Input selection | OHLC plus volume and amount | Only historical data prefix is passed |
| Missing volume/amount | Zeros when volume absent; amount proxy from volume × mean OHLC | Preserve behavior; expose the proxy in output provenance |
| Normalisation | Per-channel mean/std on supplied context, epsilon 1e-5, clipping | Use official predictor; never fit on future observations |
| Time features | Minute, hour, weekday, day and month | Declared future timestamps only; no future market values |
| Tokenizer | Linear embedding, Transformer encoder, projection and binary spherical quantisation | Preserve real official tokenizer |
| Token hierarchy | Two token groups s1 and s2 | Preserve both streams, not a single scalar regression output |
| Forecast model | Hierarchical/time embeddings, Transformer blocks, RMS normalisation | Load actual source classes and strict weights |
| Conditional generation | Sample s1; dependency-aware layer conditions s2 on s1 | Preserve official autoregressive generation |
| Context window | Sliding token buffers limited by max_context | Bound input/horizon; use installed model metadata |
| Decode | Reconstruct the six numerical channels | Keep raw outputs; reject non-finite values and flag invalid OHLCV |
| Multi-sample aggregation | Official inference averages samples internally | Run separate sample_count=1 trajectories before computing empirical quantiles |

The source review found that a single averaged prediction returned by a multi-sample call is not a predictive distribution. Therefore this adapter does not label upstream averages as uncertainty intervals. Even preserved sample quantiles remain **uncalibrated** until evaluated against genuinely unseen data.

## Adapter safeguards

`aegis/kronos_adapter.py` verifies the source commit and SHA-256 digests listed in `kronos.lock.json`, loads SafeTensors without pickle fallback, checks import location, calls `eval()`, uses `inference_mode()` and scopes seeded RNG state. All source files and weight/config files are hashed by the installer. Model requests are serialised with a lock. A failed artifact check returns an explicit unavailable state; there is no mock or statistical fallback posing as Kronos.

Predictions cannot submit orders. `ohlcv_valid` records only local output-shape checks. `execution_eligible` is always false: model output is research-only, not a trading approval. Even valid output has no path to bypass account limits.

## Actual tests versus deferred gates

Implemented interface tests use a clearly named `PredictorDouble`. They check that independent paths are retained, the historical cutoff is respected, equal seeds reproduce paths, invalid OHLCV is flagged and missing future calendars block the call. They do not exercise the learned weights or prove compatibility with every upstream checkpoint.

The separate `scripts/kronos_smoke.py` is the real-pretrained gate. It refuses to run without actual verified artifacts and records a distinct `evidence/kronos_pretrained_smoke.json` only after real inference. That file is intentionally absent from this release.

## Next reproduction gates on the target host

1. Install the pinned source and model artifacts; retain immutable model/tokenizer revision IDs and hashes.
2. Execute the real-pretrained smoke test on CPU, then the intended GPU environment. Record versions and inference latency.
3. Compare the adapter's sample_count=1 output with the untouched upstream predictor using identical model, input, calendar, device and seed.
4. Repeat 100 fixed-seed cases; require no unexplained preprocessing difference and no future-price access. Report invalid output rates rather than hiding them.
5. Evaluate forecast utility against no-change, trend and other preregistered baselines using unseen historical data and realistic costs. No performance result is assumed.

These are acceptance gates, not completed results or a promise of trading profitability.
