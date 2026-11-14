# Research protocol and evidence boundaries

## 5W1H

**Who:** a local research operator; no client funds or external managed account. **What:** deterministic quantitative strategies, path simulations, valuation arithmetic and an independently installed foundation-model adapter. **Why:** test whether an evidence-linked workflow can execute and explain its decisions without violating its declared software contracts. **Where:** the user's local host and SQLite workspace. **When:** the explicit dataset replay clock, with separate wall-clock evidence timestamps. **How:** point-in-time prefixes, next-open simulation, fixed parameters, measured costs, negative controls and archived test artifacts.

## Hypotheses

H1 (software): given valid inputs, every filled paper order is traceable, cash and positions reconcile, and configured hard pre-trade limits block prohibited proposals in the specified test cases.

H2 (research, untested on real financial data): a foundation-model/quantitative ensemble may improve prespecified unseen-data forecast or decision metrics over strong simple baselines after costs. The current strategies are deterministic baselines; no result supports H2 yet.

## Experimental design

Use a fixed instrument universe and point-in-time licensed snapshots. Freeze data, code and policy hashes. The included experiment uses the first 65% of observations as context and the last 35% as the main evaluation window, with no test-set parameter tuning. Three additional fixed-rule chronological windows provide descriptive robustness checks; they are not a substitute for nested cross-validation of a fitted model.

Fees, half-spread and adverse slippage are charged in the execution model. Capital and volume-participation limits apply. Open positions are marked at the final close and are not secretly liquidated. The comparator is a frictionless first-symbol buy-and-hold series: it is descriptive and neither cost-matched nor exposure-matched. Improve that comparator before making an alpha claim.

## Initial results

The included qualification suite and browser report establish behavior of the software on synthetic/fixture data. They do not establish forecast accuracy, profitable trading, external provider reliability or production security. See `evidence/pytest.txt` and `evidence/browser_smoke.json` rather than copied performance claims.

## Failures and refinements

The legacy prototype lacked cross-symbol valuation, durable accounting and an interface. These were replaced with aggregate risk checks, transactional persistence and an integrated workspace. During testing, incorrect numeric-input step offsets prevented scenario submission; the form was corrected. Valuation results were initially lost during redraw; retained result state fixed that. Narrow-screen table minimum widths overflowed the viewport; bounded grid/flex sizing and scroll containers corrected it.

Direct Git cloning/model download was unavailable in the build environment. Source inspection and adapter tests proceeded, while pretrained reproduction remained blocked rather than faked. Managed Chromium disallowed direct local navigation; rendering tests used actual app assets with an HTTP bridge. This means native cookie/SSE browser transport remains a separate host-level check.

## Further experiments

Once real model artifacts and point-in-time data are installed, freeze a 100-case inference corpus and independently rerun preprocessing, fixed-seed single-path parity, output validity and latency tests. Subsequently conduct holdout forecast evaluation with no-change and classical models. Calibrate uncertainty on validation data before evaluating on test data. Track failed trials and the complete experiment budget to avoid selective strategy reporting.

## New insights

A path average is not an uncertainty distribution. A high-resolution UI is not evidence of live data. A hash chain is not immutable storage. Passing risk tests is bounded assurance under the tested model, not a guarantee that a strategy cannot lose money.

## SMART acceptance register

| KPI | Target | Measurement / gate |
|---|---|---|
| Qualified tests | 100% of the release's declared tests pass | At each release candidate, archive pytest output and environment |
| Order reconciliation | Zero unexplained cash/quantity discrepancies in 1,000 replayed order cases | Before upgrading execution scope, export reconciliation evidence |
| Pretrained adapter parity | 100 fixed-seed cases with documented tolerance against unchanged upstream | Before claiming Kronos reproduction |
| UI journeys | Every shipped view and critical action passes automated checks on desktop and 430px viewport | At each UI release |
| Evidence consistency | 100% of exported events rehash; independently retained anchor matches | At each release and operator export |
| Real-data utility | Prespecified unseen-data results versus strong baselines, with costs and uncertainty | Before any trading-performance/SOTA claim |

The 1,000-order and 100-model-case gates are future qualification targets, not achieved counts.

## SWOT

Strength: one traceable workflow and local reproducibility. Weakness: synthetic data and incomplete external/model qualification. Opportunity: measured real-data research and independently reviewed risk controls. Threat: regime change, data leakage, licensing limits, poor fill models and overclaiming validation.

## Final conclusion

This is a verified local engineering release for research and paper replay. It provides the foundation for controlled experiments; it is not a proved profitable autonomous trading system.
