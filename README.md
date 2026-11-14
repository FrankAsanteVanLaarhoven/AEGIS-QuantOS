# AEGIS QuantOS 0.2
## Integrated local research and paper-trading workstation — approved scope v5

This release replaces the small M0 demonstration with a runnable application: FastAPI, a persistent SQLite engine, ten connected browser views, reproducible quantitative research, cost-aware paper execution, native risk controls and a hash-linked evidence ledger.

**Release status:** working single-operator research/paper platform. **Not a fully qualified enterprise or live-money release.** Default prices are a seeded synthetic fixture. No real orders can be submitted. No claim of profitability or state-of-the-art performance is made.

## Start on Ubuntu or macOS

Requires Python 3.11–3.13. Initial dependency installation needs Internet access.

```bash
cd AEGIS_QuantOS_v0_2
bash scripts/start.sh
```

Open `http://127.0.0.1:8765` in your browser. Paste the **workspace token printed by the launcher** into the login screen. The token is generated locally and retained with restricted file permissions in `runtime/access.token`. No shared default password is included.

Already using an environment with `requirements.txt` installed:

```bash
python scripts/run.py
```

Windows: run `powershell -File scripts/start.ps1` if your device policy permits local scripts; otherwise create a Python virtual environment, install `requirements.txt`, and run `python scripts/run.py` directly. Do not weaken an organisation's execution policy. The PowerShell helper and Docker image have not been executed in this build environment.

## First complete workflow

1. Open **Overview**, inspect the synthetic market chart and scanner, then select **Run research cycle**. Deterministic strategy workers propose paper orders; policy checks may reject some proposals.
2. Select **Advance bar**. Queued orders are revalidated and filled at the next modeled open, with costs and participation limits. Inspect **Execution** and **Portfolio**.
3. In **Research lab**, run a chronological experiment. In **Time machine**, generate reproducible scenario paths. Both results are retained in SQLite and referenced by evidence events.
4. Use **Risk & controls** to configure the strategy and enable automated local paper orders; then **Start replay** on Overview. The replay stops at the end of the dataset. **Halt new exposure** cancels queued buys; risk-reducing sells remain possible.
5. Open **Evidence ledger**, verify the hash chain and accounting reconciliation, and export the evidence JSON. Independently verify it with `python scripts/verify_evidence.py aegis_evidence.json`.

The term "paper" in the core application means a **local historical/synthetic replay simulator**, not an authenticated external broker's paper account. The two are kept separate.

## Connected workspaces

| Workspace | Backend behavior |
|---|---|
| Overview | Actual session equity, scanner, candlesticks, workflow trace, replay controls and risk state |
| Market scanner | Moving-average/reversion indicators, cross-symbol watchlist, timestamped price alerts |
| Research lab | Next-open chronological backtest; fixed-strategy windows; costs, turnover, drawdown and DCF calculator |
| Kronos intelligence | Real upstream inference adapter; artifact checks; independent sampled paths; honest missing-artifact error |
| Time machine | Seeded moving-block bootstrap, empirical bands, close-only stop/target frequencies and tail summary |
| Portfolio | Cash, positions, unrealised P&L, equity curve and correlation matrix |
| Execution | Idempotent tickets; pending reservations; next-open IOC fills; cancellations and protective exits |
| Risk & controls | Long-only checks, per-order/concentration/gross exposure, loss/drawdown limits, automation policy and halt |
| Evidence ledger | Persistent event chain; hash recomputation; local-head truncation check; cash/position reconciliation; export |
| Integrations | CSV import/export, source manifests, SEC filings, read-only Alpaca paper account and model status |

The interface includes responsive dark styling, canvas charts with crosshairs and wheel zoom, a command palette, keyboard focus states, reduced-motion support and no externally hosted frontend dependencies.

## Approved components and separation

AEGIS QuantOS is the master application. QuantForge OS is the approved research workstream; QuantShield-FTMO is the approved trading-risk workstream. **Their original repositories were unavailable**, so this package contains new native implementations of the stated responsibilities, not imported or verified integrations with private codebases. Day Trader Skill is a functional reference only; none of its source is copied. Kronos is integrated through its pinned official implementation.

No additional named platform is introduced. Previously removed projects are not dependencies or integration targets.

## Real Kronos installation and source audit

The adapter is not a regression-model imitation. It calls the actual official `KronosTokenizer`, `Kronos` and `KronosPredictor` interfaces from:

```text
Repository: shiyu-coder/Kronos
Commit: 67b630e67f6a18c9e9be918d9b4337c960db1e9a
Default model: NeoQuasar/Kronos-mini
Tokenizer: NeoQuasar/Kronos-Tokenizer-2k
```

On your Internet-connected host, activate the same application virtual environment and run:

```bash
source .venv/bin/activate
python scripts/install_kronos.py --model mini
python scripts/kronos_smoke.py
```

The installer clones the pinned source, preserves upstream licensing, resolves and records immutable model/tokenizer revisions, downloads SafeTensors files, and writes SHA-256 checksums. It refuses to overwrite modified upstream source. Model loading uses local SafeTensors and strict state-dictionary checks, not pickle fallback. Set `AEGIS_MODEL_DEVICE=cuda:0` after installing a compatible CUDA PyTorch build on your GPU host; CPU is the default.

The inspected upstream averages sampled paths internally. This adapter calls it with `sample_count=1` for each independent path, then computes empirical quantiles across those paths. Raw non-finite output is rejected; invalid OHLCV envelopes are flagged rather than silently repaired. Future inputs contain calendar timestamps only, never future prices.

**Build-environment limitation:** direct Git/Hugging Face downloads were unavailable. The official source was inspected through the GitHub connector, but a complete local upstream clone and pretrained weights were not obtained. The pretrained smoke test therefore has **not been run**. Adapter interface tests use explicitly identified test doubles and do not establish model reproduction. The UI reports `NOT_INSTALLED` and returns an explicit error until you install actual artifacts; it never substitutes synthetic forecasts.

See `docs/KRONOS_SOURCE_AUDIT.md` for the reverse-engineering findings and exact verification gates.

## External connectors

Copy `.env.example` to `.env` and provide only the credentials required for the connector you intend to use. Secrets are never sent to the UI.

- SEC EDGAR needs `SEC_USER_AGENT` containing a real name/contact email. Filing metadata is for current research only; it is not silently joined to past backtests.
- Alpaca needs `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` for a paper account. Only `GET` requests to the fixed paper domain are implemented. No external order-submission endpoint exists.
- CSV import is functional without credentials. Importing creates a new local session while preserving the evidence history.

External connector request contracts were tested with mocked transports. Actual provider connectivity, entitlements and credentials were **not tested**.

## Dataset contract

Required CSV columns:

```text
symbol,timestamp,available_at,open,high,low,close,volume
```

An optional `open_at` is recommended. Without it the importer assumes the open occurred 299 seconds before `timestamp`; this default is suitable only for the included five-minute fixture. Always provide real `open_at` values for other timeframes.

All timestamps must have timezones and are normalised to UTC. `available_at` must not precede the completed-bar timestamp. Symbols must have aligned calendars and at least 65 bars; interactive backtests need at least 100 and permit at most 1,000 bars per instrument. The CSV input cap is 8 MB; no more than 20 symbols are accepted.

The importer rejects invalid OHLC ranges, non-finite numbers, duplicate symbol/time observations, fractional/negative volume, calendar misalignment and explicit corporate-action events. It **does not** independently verify historical availability, price adjustment, survivorship, licenses or the uploader's provenance claims. Do not treat these checks as a complete leakage audit.

## Tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

The included build run passed **100 tests**. Six Kronos contract tests require the optional model libraries and explicitly use predictor doubles; those tests skip when optional libraries are absent. See `evidence/pytest.txt` and `evidence/pytest.xml`.

The Chromium UI harness passed **19 checks**, including all ten views, order entry/fill, halt/resume, simulation, backtesting, DCF, unavailable-model handling, command navigation and a 430-pixel layout with no page overflow. In this environment managed Chromium blocked direct local navigation, so real HTML/CSS/JS was rendered and its `fetch` calls forwarded over an HTTP bridge to the running FastAPI server. Native browser cookie transport and SSE were not exercised by that bridge. API authentication/CSRF were separately tested. See `evidence/browser_smoke.json` and screenshots.

To run native browser verification on your host, start the server and use:

```bash
AEGIS_TEST_TOKEN='<the launcher token>' python scripts/browser_smoke.py
```

Set `CHROMIUM_PATH` if Chromium is installed somewhere other than `/usr/bin/chromium`.

## Security and deployment boundary

Default binding is loopback only. Session cookies are HTTP-only and SameSite Strict. Write APIs require a CSRF token; foreign origins and unapproved Host headers are rejected. Inputs are typed and bounded; responses have CSP, frame denial and content-type headers. Database writes use transactions and a single-process lock. Manual orders, automated cycles, replay advancement and replay activation are blocked when evidence integrity or account reconciliation fails.

This is **not** an OIDC/RBAC multi-tenant service, independently audited security product, certified financial system, high-availability deployment or licensed data terminal. SQLite and a local hash anchor do not resist a database administrator rewriting both the chain and its head. Export anchors to independent storage for stronger assurance. The Docker/Compose and CI files are supplied but were not executed remotely.

## Model and market limitations

There is no trained LLM agent swarm, trained reinforcement-learning strategy, live exchange matching, tick-level queue model, options margin engine, market-making agent or automatic model promotion in this release. The six traced workers are deterministic software stages. The risk workstream has generic user-configured limits and a UTC reset; it is not a certified implementation of any provider's current challenge rules. Stops can gap or fail to fill under the participation model. Replay simulation is not causal-inference evidence, and bootstrap paths do not establish calibrated predictive probabilities.

## Repository map

```text
aegis/        API, state/ledger, execution/risk, data, quantitative research, Kronos and connectors
web/          Connected responsive interface, chart rendering and interaction code
scripts/      Launchers, pinned Kronos installer, real-model smoke gate, browser test, evidence verifier
tests/        Engine, data, API, security, connector, accounting and model-interface tests
configs/      Approved-scope and feature-status registries
docs/         Source audit, architecture, research protocol, threat model and release qualification
evidence/     Actual test reports, source audit metadata, screenshots and verification outputs
```
