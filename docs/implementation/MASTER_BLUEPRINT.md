# AEGIS QuantOS — autonomous implementation and qualification blueprint

**Issued:** 14 November 2026. **Scope:** approved v5. **Target repository:** `https://github.com/FrankAsanteVanLaarhoven/AEGIS-QuantOS`.
**Audited baseline:** `b0ec80cbea1e2a78155525fed2b13542d523e9e7` on `main`.
**Deliverable type:** executable engineering handoff and acceptance specification, not a statement that the requested integrations are already operating.

## 1. Binding objective and authority

Complete the existing AEGIS QuantOS application in place: real entitled market feeds, verified local Kronos inference, OpenRouter-backed financial research, deterministic quantitative/risk engines, external paper-broker execution, complete UI wiring, reproducible qualification, and deployable release artifacts. Do not replace the repository with another demonstration, scaffold, or unrelated platform.

Approved names: AEGIS QuantOS, QuantForge OS (research responsibility), QuantShield-FTMO (trading-risk responsibility), Kronos (official model implementation), Day Trader Skill (functional reference only). The original QuantForge and QuantShield repositories have not been verified or imported. Keep their responsibilities native unless their actual sources become explicitly available. Do not claim integration based on labels.

Excluded from imports, dependencies, services, architecture and integrations: VerdictPlane, ATLAS, FinMesh-AI, AEGIS RiskGuard and FAVL TradeProof. They may be named only in exclusion-policy documentation/tests. Do not introduce replacement named platforms without approval.

Engineering authority covers repository-local edits, tests, isolated dependency installation, schema migrations on disposable/test data, local services, permitted provider health checks, approved-budget model calls, and integration-branch commits. It does not cover real-money trading, fund transfers, subscription purchases, new account registration/KYC, accepting legal agreements, bypassing MFA, disabling host security, production-destructive migrations, or exposing credentials. Missing authorization must not be bypassed.

This is an autonomous **implementation** mandate, not an unrestricted capital-management mandate. Use live market data while keeping execution in local or authenticated external **paper** mode. Design a future live-execution adapter behind a hard-disabled release boundary; do not activate it from this mission.

## 2. What was actually inspected

Sources R1–R8 in `SOURCES.md` are current repository files read at the pinned commit, not inferred from previous conversation summaries.

| Existing item | Inspected state | Required action |
|---|---|---|
| `aegis/api.py` | FastAPI routes, local session actions and SSE; integrations explicitly report `external_order_submission=False` | Extend incrementally, preserving current authentication/CSRF and API contracts |
| `aegis/connectors.py` | SEC filing metadata; Alpaca paper account/orders via GET only | Implement real feed supervisor and separate external paper OMS adapter |
| `.env.example` | Local port/token, SEC user agent, Alpaca keys, Kronos paths/device | Add typed secret references, OpenRouter settings, independent feed/execution modes |
| `scripts/run.py` | Simple `.env` loader; local token printed at startup; single loopback process | Add safe secret-file resolution, redacted noninteractive startup and configurable local/deployment profiles |
| `aegis/kronos_adapter.py` | Source hash verification, official class loading, individual sampled paths; future timestamps borrowed from the replay dataset | Preserve adapter; add market-calendar forecast scheduling and real-model qualification |
| `aegis/engine.py`, `store.py`, `quant.py`, `research.py` | Present native engine/research/state implementations; full behavioral audit belongs to W00 | Retain and regression-test before changing interfaces |
| `web/` | Existing connected ten-view interface; not a React/Next.js scaffold | Preserve functional interactions and upgrade tested design; do not gratuitously rewrite the stack |
| README / saved evidence | Reports 100 previous tests and 19 browser checks; real provider/Kronos tests not established there | Rerun on current host and record new results; do not copy old counts as new evidence |

Repository metadata showed a first v0.2 import and an unprotected `main` branch at inspection. The combined commit-status endpoint returned no statuses; this is not proof that CI passed or that no workflow run exists. Inspect Actions separately and establish fresh checks. Repository metadata size alone is not reliable evidence of emptiness.

## 3. Non-negotiable data and execution modes

Maintain **two independent axes**:

- `data_mode`: `synthetic`, `historical`, `delayed`, `live`.
- `execution_mode`: `paper_local`, `paper_external`, `live_disabled`.

A `live` feed must never toggle real-money execution. Source mode, source venue/coverage, provider time, receive time, delay, entitlement and connection state appear in every applicable UI panel and API response.

No automatic substitution of synthetic prices for failed live feeds. Provider fallback changes provenance and may change strategy eligibility. IEX is a single-venue feed; SIP is consolidated coverage. Never label an IEX price as a consolidated NBBO. Entitlement denial is `BLOCKED_ENTITLEMENT`, not a reason to relabel delayed data as live. Official Alpaca documentation describes feed-dependent subscriptions and authentication errors (S6–S8).

Distinguish `MARKET_CLOSED` from `STALE` and `DISCONNECTED`. A Saturday or an exchange holiday is not a successful live-trading session. Live qualification must include actual open-market observations; protocol test streams are not market evidence.

## 4. Autonomous execution protocol

Use `configs/autonomous_delivery.json` as the task DAG. Each work package has an owner role, dependencies, required outputs and exit evidence. Start by recording the current `git rev-parse HEAD`; if HEAD differs from the audited baseline, inspect the diff and update the audit rather than resetting it.

For each task:

1. Inspect exact source; write a small implementation plan and tests for the acceptance criterion.
2. Implement an additive vertical slice and wire backend, persistence, UI, configuration and monitoring together.
3. Run unit, integration, security and applicable browser/provider checks. Fix failures without weakening the gate or deleting tests.
4. Record command, exit code, timestamp, commit, dependency versions and evidence hashes. Separate mocks, replay, real credentials and real-market evidence.
5. Commit a coherent checkpoint on `feat/autonomous-completion`; update the machine-readable state and continue without seeking routine approval.

State values: `NOT_STARTED`, `IN_PROGRESS`, `IMPLEMENTED_UNVERIFIED`, `VERIFIED_LOCAL`, `VERIFIED_EXTERNAL`, `FAILED`, `BLOCKED_CREDENTIAL`, `BLOCKED_ENTITLEMENT`, `BLOCKED_PERMISSION`, `BLOCKED_ENVIRONMENT`, `AWAITING_MARKET_SESSION`, `NOT_APPLICABLE`.

Try at most three materially different repairs for the same failure; record attempts and move to an independent task. Do not loop indefinitely, burn unapproved tokens, reissue an ambiguous POST, or wait on a market session while other work is available. Resume from the last checkpoint when a blocker is resolved. Stop only when all eligible work is complete or genuinely blocked; do not stop after producing a plan or mock UI.

A blocked task is **not complete**. A skipped real-provider test is **not a pass**. The agent must not mark the release “fully wired”, “production ready” or “SOTA” while any required external gate is blocked.

Prefer Antigravity workspace skills and root project instructions. Current documentation supports `.agents/skills/<name>/SKILL.md` and `AGENTS.md`; older `.agent/skills` is a compatibility path (S1–S3). Use the provided skill. Inspect the installed application's version and help before changing settings. Do not install global unrestricted execution rules or guess obsolete CLI flags. Apply bounded permissions to this workspace and approved provider domains, not the whole workstation. Prompt-level deny text is not a security boundary; enforce actual OS/agent permissions.

## 5. Target implementation boundaries

Retain FastAPI and the working UI initially. Add ordinary modules, not separately branded systems:

```text
aegis/
  settings.py                  typed public configuration and secret references
  secrets.py                   isolated runtime secret resolver; never API output
  market/                      normalisation, calendars, streaming, backfill, health
  providers/                   Alpaca market data, SEC and licensed news adapters
  llm/                         OpenRouter gateway, schemas, cost controls, model registry
  agents/                      finance-specific bounded research workflows
  brokers/                     broker interfaces, external paper OMS, reconciliation
  jobs/                        durable job state, retries, scheduling and stop conditions
  risk/                        native policy checks and account-rule profiles
  persistence/                 migrations, transactions and outbox
  api.py                       compatibility routes plus versioned new endpoints
  kronos_adapter.py            verified official-model adapter
  engine.py / quant.py / research.py / store.py
web/                           existing screens extended with actual services
scripts/                       setup, doctor, smoke, qualification, backup and restore
configs/                       provider registry, role/model locks, policies, release gates
```

Names above are a **proposed target**, not files already present. Avoid module/package name collisions: for example, migrate `connectors.py` into a package only with import compatibility and tests. Do not start a sprawling microservice/Kubernetes rewrite before a functioning vertical slice exists. Separate GPU inference and provider I/O from the critical risk/OMS path. LLM latency must not hold an order transaction or a risk lock.

Use a durable relational system for deployed concurrent operations; retain SQLite for explicitly single-operator local profiles. Introduce PostgreSQL with tested migrations and a transactional outbox before declaring multi-worker support. Cache/bus components are conditional on a demonstrated requirement, not cosmetic architecture.

## 6. Typed contracts and provenance

Implement and test these logical contracts; use Pydantic/JSON Schema and strict enum/numeric/time validation.

**MarketEvent:** `schema_version`, `event_id`, stable `instrument_id`, symbol/venue, asset class, currency, event type, source/provider/feed, `source_mode`, provider timestamp, receive timestamp, `available_at`, sequence or provider-specific duplicate key, revision ID, price/quantity/volume, adjustment basis, entitlement reference, raw-payload hash. Do not invent sequence IDs for providers that do not supply them. Deduplication must respect provider correction/cancellation semantics.

**SourceDocument:** original URI, provider, content hash, first observed/published/accepted timestamps, extracted text, licensing tags, entity resolution and certainty, point-in-time eligibility. A SEC transaction date is not its disclosure availability date. Amendments are new versions, not retrospective replacements.

**ForecastEnvelope:** model/tokenizer/source revisions, device/software, dataset/input hash, observation cutoff, future calendar, horizon, seed, retained paths, calibration version/status, validity flags and `execution_eligible` policy. An empirical interval is not automatically calibrated.

**ResearchAssessment:** job/role/model identifiers, input evidence IDs, as-of time, extracted claims, cited evidence IDs, conflicts, numerical-engine outputs, `abstain` plus reason, uncertainties, token/cost usage and policy status. A cited ID must exist and be accessible to the caller. LLM self-confidence is not a calibrated probability.

**OrderIntent:** account and environment, instrument, exact side/quantity/type/price/time-in-force, strategy version, decision evidence IDs, unique client order ID, expiry, data freshness and policy version. **RiskDecision:** exact intent hash, account-state revision, quote/event reference, checks and decision. Approval expires on material state change; revalidate immediately before submission.

**OrderEvent:** submitted/accepted/partially-filled/filled/cancel-pending/cancelled/rejected/expired/unknown, broker order/fill IDs, quantity/price/fees/currency, original provider timestamp and receive time. Persist intent before remote send; after a timeout query by client ID before any retry. Do not promise network-level exactly-once delivery; implement at-least-once processing with idempotent effects and reconciliation.

## 7. Work packages and executable acceptance

### W00 — preserve, audit, reproduce

Inspect the whole repository, current branch/worktree, CI, dependencies, database schema and current UI. Never delete local uncommitted work. Record baseline commands and failures; run the actual test suite including optional model-contract dependencies where available. Classify skips explicitly. Inspect the uploaded screenshots as visual references, not verified current screenshots. Record Python, OS, GPU/driver, available disk/memory and browser versions.

Exit: `evidence/current/baseline.json`, fresh tests, API/UI map, dependency/license inventory and reproducible local start. No unsupported claim that old 100/19 counts were reproduced.

### W01 — configuration, credentials and authorization

Create typed configuration and a startup doctor. Support `NAME_FILE` secret references with explicit precedence and ambiguity rejection. Protect secrets outside Git or in an actual secret manager; never expose them in frontend variables, logs, analytics, screenshots, LLM messages, MCP arguments or issue bodies. A secret's presence is not connectivity verification.

Separate application login tokens, OpenRouter runtime keys, OpenRouter management keys, Alpaca data/paper credentials, optional Hugging Face token, GitHub delivery identity and optional enterprise OIDC secrets. Do not put a management key into the application. Do not scan the home directory for other projects' keys. Only the explicitly configured environment or secret paths are eligible.

Remove unattended startup token printing from log collectors; retain a deliberate local secure enrolment path. Add expiry, role checks, rotation/revocation and secret-redaction tests. Harden `.gitignore`/`.dockerignore`; scan Git history and build contexts before push. Never modify subscription/billing settings automatically.

Exit: doctor reports redacted presence and real auth status separately; invalid or absent secrets fail closed for that feature and do not block offline work.

### W02 — real market-data supervisor

Implement Alpaca WebSocket authentication/subscription, heartbeats, bounded queues, exponential reconnect with jitter, health states, REST backfill, provider revisions and gap accounting. Reuse credential settings; keep data and broker endpoints distinct. Initial universe preserves `NVDA`, `AMAT`, `LRCX`, `TER`, `SPY` as a technical coverage set, not investment advice. Use entitled IEX or SIP explicitly. No unofficial Yahoo endpoint as a production SLA substitute.

Implement exchange calendars, halts, early closes and daylight-saving changes. Store raw events and normalised bars with provenance. Form completed bars from events using a documented watermark; keep revised bars versioned. Historical adjustments, corporate actions and survivorship rules must be explicit. Do not use final revised data as though available earlier.

Exit: credentialed connection receipt; source labels; two real market sessions of observation before release qualification; forced reconnect/gap recovery; zero unexplained loss in a bounded recorded sequence; freshness checks based on active sessions. REST/static fixtures alone cannot satisfy this gate.

### W03 — current financial evidence and research ingestion

Extend SEC submissions with company facts and official filing retrieval. Honour provider fair-access rules and identifying User-Agent. Add licensed news using verified provider endpoints and entitlements. Separate filings, earnings, news, congressional disclosures and prediction-market commentary; do not substitute one for another. SEC read APIs are not trading feeds and do not require an invented API token (S9).

Implement entity/instrument resolution, bitemporal availability, deduplication, corrections, source licensing and hostile-document sanitisation. Add optional macro-vintage and on-chain research adapters only under the provider registry with real permissions and separate tests. Do not enable crypto/derivative trading merely to demonstrate those feeds. Preserve original Day Trader Skill concepts—scanner/news/entry/exit/outcome replay/risk/budget/executor/alerts/earnings/disclosures—without asserting that all came from an identical video or repository version.

Exit: each stored claim and metric has source/time/hash; malicious news cannot issue tools; delayed filings cannot enter a historical decision before publication; amended facts retain earlier versions.

### W04 — actual OpenRouter runtime gateway

Implement `aegis/llm/` and backend-only authenticated routes. Use the official API and scoped runtime key; the setup utility in this pack is not itself the application gateway. Endpoints: `/api/v1/key` for credential checks, `/models` for discovery, `/chat/completions` for calls. Resolve actual installed provider schemas at implementation time. Use finite timeouts, cancellable jobs, concurrency limits, circuit breakers and capped retries. Treat ambiguous inference timeouts as potentially billable; do not blindly duplicate requests.

For each research role, choose models from a saved current catalog based on required context length, tool/structured-output support, privacy, measured finance-evaluation quality, latency and approved cost. Store exact returned model IDs and approved fallback list in a model lock. Do not use guessed IDs, unrestricted `auto`, a generic cheapest model, or silent automatic upgrades. No model is called “best” without a scoped benchmark.

Use strict JSON-schema output and server-side validation. Set provider parameter requirements and appropriate privacy constraints; deny collection and require ZDR where the account supports it. Current docs note account Safety Retention settings and that tool backends are not covered by inference ZDR filters; inspect those settings and disable external server tools unless separately approved (S5). Never loosen privacy silently to get a response.

Implement reservation-based token/cost budgeting before dispatch; include retries, input/output/reasoning tokens, tool costs and concurrent requests. Reconcile provider usage afterward; unknown cost is not zero. Enforce per-job, per-day and project totals plus provider key caps; no auto-top-up. Account key creation requires pre-existing authorized management access and a user-approved finite cap (S4). Missing billing authorization blocks paid calls, not code implementation.

Exit: real credential check; real structured response with requested/resolved model, provider and usage; rejected schema, 401/402/429, outage, fallback exhaustion, cost-race and injection tests. Do not store prompt contents by default when confidential. Record exact provider retention policy and legal constraints rather than claiming end-to-end privacy from one flag.

### W05 — native finance-specific agents

Implement bounded roles: market/evidence researcher, filings/earnings extractor, quantitative hypothesis author, independent evidence checker, and risk/report explainer. A deterministic coordinator routes typed tasks. Research outputs propose hypotheses or parameter candidates; numerical engines calculate valuation, portfolio weights, P&L and risk. No LLM can alter a hard risk limit, submit broker orders, grant itself tools or read credentials.

Every role has a tool allowlist, model assignment, max steps, time/token budget, evidence schema and abstention criteria. Tool results and retrieved documents are data, never instruction authority. Enforce source time cutoffs and user access before retrieval. Persist tool inputs/results only after redaction. A schema-valid hallucination still fails evidence checking.

Exit: three-arm evaluation (deterministic baseline, retrieval-assisted LLM, verified multi-role workflow), stable numerical oracles, source attribution audit, prompt-injection suite and real end-to-end OpenRouter job. Record failures and cost; no “agent swarm” claim for deterministic stages.

### W06 — official Kronos reproduction

Retain commit `67b630e67f6a18c9e9be918d9b4337c960db1e9a` unless a documented compatibility/security decision changes it. Install the actual source/tokenizer/weights and retain MIT attribution. Prefer mini first; move to small/base only after host resource/latency measurements. No training-from-scratch claim from a checkpoint download.

Verify source/model/tokenizer hashes and immutable model revisions. Run `scripts/kronos_smoke.py` on actual weights. Add same-input direct-upstream versus adapter conformance tests under recorded seeds and numerical tolerance. Keep single-path samples separate before aggregating bands; do not treat upstream average as distribution. Test max-context truncation, missing volume/amount, dtype/device, nonfinite/invalid OHLCV, concurrent inference, cache invalidation and corrupted artifacts.

Replace dependence on future rows in a finite replay CSV with a tested exchange-calendar timestamp provider for forward inference. Future inputs must contain calendar information only. Document amount imputation and pretraining cutoff uncertainty. Block out-of-sample claims where checkpoint training contamination cannot be excluded.

Exit: real local smoke and saved source parity results; measured inference latency/memory on the actual host; UI displays real forecasts or explicit unavailability; forecast quality assessed prospectively. Small sample quantiles stay labelled uncalibrated until calibration experiments pass.

### W07 — quantitative, simulation and portfolio engines

Preserve baseline algorithms; specify train/validation/untouched test chronology, causal availability, purging where labels overlap and parameter fitting within each training fold. Compare buy-and-hold/cash as applicable, simple trend/reversion, volatility targeting, model-only and combined strategies on identical execution/cost assumptions. Parameter search includes all tried configurations and negative results; an untouched test period is not a tuning set.

Implement cost-aware optimisation, exposure/factor/correlation constraints, cash/fees/currency accounting, realistic fills and capacity assumptions. Use numerical oracles for DCF, signal math, accounting and portfolio constraints. Do not hard-code prior 0.35/0.25/0.40 ensemble weights as an established optimum. No invented “Sharpe improvement >0.2” pass without uncertainty and preregistration.

Time Machine must disclose method, window, seed and regime. Model gaps, jumps and execution costs; same-bar stop/target order is ambiguous without adequate intrabar data, so use declared conservative handling. Bootstrap outcomes are not automatically causal effects or calibrated probabilities.

Exit: deterministic replay, statistical/forecast calibration report, trading-cost sensitivity, multiple-testing assessment and reproducible negative-control results. A zero-return/no-trade strategy is allowed when no candidate clears the research gate.

### W08 — independent native risk and external paper OMS

Keep risk outside LLM control. Validate exact order/state, data freshness and entitlement, account/instrument permissions, cash/margin policy, notional/size/gross/net/concentration, loss/drawdown, liquidity and order-rate limits. Reserve outstanding exposure atomically across symbols. Recheck after fills, revisions and account changes.

Add separate generic and versioned account-rule profiles under the approved QuantShield responsibility. Do not call generic UTC reset logic a verified current FTMO rules implementation. Retrieve the relevant official account type/rule version when applicable and test its timezone/reset/equity semantics; no fabricated current rule thresholds.

External paper broker: fixed paper-domain allowlist, durable intent/outbox, client order IDs, broker acknowledgement, trade-update processing, terminal state reconciliation, cancellation/replacement race tests and restart recovery. Local-paper fills and external-paper fills must never be double-counted. A submitted/cancel-requested order is not a filled/cancelled order. Ambiguous network outcomes stay UNKNOWN until reconciled.

When stale/integrity-failed, block new risk. Risk reduction/cancellations use a separately defined emergency policy: an indiscriminate order denial must not trap unsafe exposure. HALT must stop new submissions and request permitted cancels; do not claim it can reverse trades or guarantee liquidation during outages.

Exit: credentialed external paper submission/cancel/fill/restart proof where the broker environment permits; exact cash/position/fill reconciliation; every injected prohibited intent rejected; no real-money API host reachable from this build's execution worker.

### W09 — premium, fully connected UI

Keep the ten existing workspaces and their useful visual language. Improve typography, hierarchy, spacing, contrast, keyboard navigation, responsive density and rendering performance. Add layout persistence, resizable panels, honest source badges, command palette, tooltips, empty/error/loading states and accessible reduced motion. Avoid decorative 3D obstructing risk, orders or prices. Never market an unmeasured style as the most advanced available.

Connect every visible action to a real backend capability and permission. Integrations page reports separate `configured`, `authenticated`, `entitled`, `receiving`, `fresh` and `qualified` states. Expose OpenRouter model/job/cost status without keys. Draw actual retained Kronos paths and calibrated status. Show agent/tool event traces, not fabricated neuron activity. Display quote/bar age, venue coverage, session state, held positions and order lifecycle accurately.

Exit: real native Chromium/Playwright login, cookies, CSRF, SSE/WebSocket, all views/actions; no HTTP bridge substitution for release qualification; desktop/tablet/mobile screenshots; no uncaught JS errors; keyboard/focus tests and performance budgets. Failed live feed must visibly degrade without replay fallback.

### W10 — deployment, persistence and operations

Implement health/readiness separating process health from provider readiness. Freeze dependencies, SBOM and third-party notices. CI includes offline tests, optional real-model gate, static/security checks, secret scan and native-browser suite. External tests require explicit secrets and budget; do not expose them to untrusted PR jobs. Checksum artifacts and verify installed release content.

Use least-privilege containers without a Docker socket or root host mount. Add TLS ingress and OIDC/RBAC before exposing beyond loopback. Role separation: viewer/researcher/operator/admin; server enforces every route. Run no tenant-sensitive data through unapproved external models. If multi-tenancy is introduced, test isolation at data/query/object-store/agent-tool boundaries before claiming it.

Use migrations with backups and restore checks; external anchor storage for evidence integrity; encrypted backups, retention and deletion procedures; telemetry redaction; cost/provider outage dashboards. Test rollback of application and schema-compatible release. A single hash chain under the same administrator is not immutable proof.

Exit: reproducible container launch, backup and restore, outage/restart tests, observability, signed/hashed release manifest and verified operational runbook. Cloud deployment is conditional on an existing authorized target, not automatic cloud-account creation or spending.

### W11 — qualification and unattended paper soak

Run all required local and external gates. Start a supervised-by-policy paper soak only after credentials, data and execution permissions pass. Observe two genuine regular market sessions plus a 24-hour process/recovery window; these are minimum engineering observations, not profitability evidence. A closed market cannot satisfy open-session checks. The agent may configure the local worker/scheduler with these limits, but must not claim elapsed results before they occur.

Run injected feed dropout, duplicate/out-of-order/corrected messages, broker timeout, partial fill, restart, API quota exhaustion, model timeout, corrupt model file, ledger corruption, unauthorized user, prompt injection and budget-exhaustion scenarios. Record both expected and observed results.

Exit: completion matrix with evidence hashes; only verified capabilities enabled. A release candidate with explicit blockers is useful but is not a fully qualified release. Produce a PR/checkpoint and final factual report; do not force-push or bypass branch checks.

## 8. Provider and secret registry

| Service | Required for | Credential/config | Qualification |
|---|---|---|---|
| Alpaca market data | US equity trades/quotes/bars and historical backfill | Existing `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` compatibility; add `_FILE`; explicit `ALPACA_DATA_FEED=iex` or entitled `sip` | Real authentication, venue/entitlement identity, open-session stream and reconnect proof |
| Alpaca external paper | Broker paper lifecycle | Separate paper-scoped key pair when available; fixed `https://paper-api.alpaca.markets` | Submit/ack/fill/cancel and reconciliation, no live host |
| SEC EDGAR | Filings/fundamental evidence | `SEC_USER_AGENT` with actual contact, no invented SEC market-data key | Real retrieval, rate control and source availability |
| Licensed news | Timestamped news evidence | Approved provider account/entitlement, not assumed included in an equity feed | Provider/time/content provenance and licence check |
| OpenRouter | Native financial LLM research | `OPENROUTER_API_KEY_FILE`; optional management key available only to bootstrap | Auth + catalog + allowed model + structured inference + usage + role evaluation |
| Hugging Face/Kronos | Model artifacts | Public checkpoint access or explicitly supplied `HF_TOKEN_FILE` if needed | Immutable revisions, hash checks, real model smoke |
| GitHub delivery | Code/CI artifacts | Existing scoped CLI/app identity | Branch push/checks without broad account-admin access |
| Optional macro/on-chain | Extended research only | Provider-specific supplied rights | Own evidence and acceptance gates; never marks core feed complete |
| OIDC/TLS/cloud | Networked deployment | Existing approved identity provider, domain and target | Login/isolation/cert/backup tests; otherwise remain loopback |

There is no universal “all tokens” credential. Application access tokens, provider API keys, OAuth/OIDC tokens, model download tokens and broker account credentials have different authorities. Never interchange them or place all of them in a generic agent context.

## 9. OpenRouter provisioning versus runtime wiring

The provided `scripts/aegis_openrouter_setup.py` can: check local presence without printing values, verify a runtime key, securely import an existing key, create a capped expiring runtime key using existing management authorization, fetch candidate model metadata, and run one explicitly authorized structured-output smoke call. It never purchases credits or registers an account. It uses HTTPS, rejects redirects, caps responses and does not automatically retry key-creation POSTs.

Default secret destination is `~/.config/aegis-quantos/secrets/openrouter.key`, owner-only, outside Git. On non-POSIX systems use the OS secret manager instead. Missing secrets yield a redacted blocker code. Do not pass a secret as a shell argument or paste it into Antigravity chat.

Runtime integration still requires W01/W04. The existing `.env` loader does not implement `_FILE` resolution. The setup utility alone does not enable any LLM UI button. See `OPENROUTER_RUNBOOK.md` for exact commands, flags and proof requirements.

## 10. Scientific process and bounded claims

**Hypotheses:** H1, local official Kronos can reproduce direct-upstream behavior under matched artifacts/inputs. H2, verified multi-role research reduces material numerical/attribution errors relative to the retrieval-only arm at an acceptable cost. H3, the engineering pipeline enforces declared order/data/account invariants under the specified fault model. No hypothesis presupposes profitable trading.

**Design:** pre-register datasets, timestamps, market coverage, evaluation windows, horizons, model/strategy candidates, costs, confidence intervals and acceptance thresholds. Freeze holdouts; use time-respecting resampling and multiple-testing controls appropriate to the selected experiment. Forward-paper results are distinct from historical evaluation and live economic execution.

**Initial results:** this handoff contains an inspected repository and offline tests of its new setup utilities; it does not contain new credentialed market, model or broker results.

**Failures/challenges:** credentials and entitlements may be absent; market may be closed; the existing adapter assumes future replay rows; offline test doubles do not prove pretrained inference; autoregressive samples are not calibrated; hosted models can change behind a named ID.

**Refinement:** retain the original baseline, isolate one change, rerun the paired experiment and explain changed failure modes. Never tune on the test data or remove failed runs from the record.

**Further experiments:** prospective post-checkpoint-cutoff evaluation; input-source ablations; latency/cost stress; independent oracle/accounting comparison; multiple market regimes and corporate-action cases.

**New insights/final conclusions:** fill these fields only from measured results. Distinguish bitwise replay of recorded artifacts from approximate model regeneration. A “SOTA” statement requires an independent contemporary benchmark; completing software alone does not provide it.

## 11. Seven measurable release KPIs

All values below are proposed gates, not achieved performance claims. Measure before the release-candidate promotion, with the observation windows stated.

| KPI | Target and tracking |
|---|---|
| Traceability | 100% of submitted paper orders have intent, risk state/version, source and broker evidence; audit every order during the two-session soak |
| Forbidden-action control | 0 accepted prohibited orders across at least 1,000 deterministic/property/fault cases; test every change to risk/OMS |
| Accounting/recovery | 0 unexplained cash, position or fill mismatches; reconcile after every provider update, every restart and at soak end |
| Data truthfulness | 0 silent source-mode downgrades; 100% of scripted disconnect/entitlement/stale cases correctly classified; measure per active session |
| Model/research integrity | Real Kronos parity under preregistered tolerance; 100% schema/evidence validation before accepting LLM output; report rejected/abstained outputs separately |
| UI/runtime performance | Initial target: p95 observed-event-to-UI <=2 s on the documented local workload, no uncaught browser errors or mobile page overflow; capture native-browser evidence |
| Cost/security | 0 secret exposures or cap overruns in tests; 100% billable jobs have budget reservation and actual/unknown-cost reconciliation; verify backup restore within a proposed 30-minute recovery target |

Do not report statistical evidence of perfect safety from zero failures. Record test coverage, assumptions and uncertainty. A stricter feed latency target is defined per chosen provider and strategy rather than invented globally.

## 12. SWOT and delivery decision

Strength: existing functioning local workflow and auditable interfaces. Weakness: external identity/data/model qualification is not established. Opportunity: verified source-to-decision financial research and paper automation. Threats: provider changes, credential leakage, prompt injection, look-ahead bias, cost runaway and false readiness claims.

Mitigations are the task gates above. The definition of done is a tested integration matrix, not the number of screens, models or agents.

**Required final agent report:** current commit; changes and migrations; exact commands/results; model/source locks; redacted provider evidence; native-browser screenshots; unresolved blockers; cost usage; paper-only mode; known failure modes; startup/stop/rollback commands; paths to all artifacts. State unambiguously which integrations were tested with mocks versus real providers. Never report account/key creation or live-feed availability without actual successful receipts.
