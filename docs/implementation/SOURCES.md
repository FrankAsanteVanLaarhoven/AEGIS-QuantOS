# Source register — inspected 14 November 2026

Repository findings are source-derived. Target modules, implementation tasks, budgets and acceptance thresholds elsewhere in this package are proposed engineering requirements. External documentation can change; Antigravity must preserve a dated source snapshot when implementing provider-specific behavior.

## Repository sources (pinned)

Base: `https://github.com/FrankAsanteVanLaarhoven/AEGIS-QuantOS/blob/b0ec80cbea1e2a78155525fed2b13542d523e9e7/`

- R1 `README.md`: local/replay release boundary, prior evidence, integration limitations.
- R2 `aegis/connectors.py`: real implemented SEC and read-only Alpaca interfaces.
- R3 `.env.example`: actual existing environment variable contract.
- R4 `scripts/run.py`: actual `.env` handling and local access-token behavior.
- R5 `aegis/kronos_adapter.py`: source pin, artifact validation, samples, replay-calendar dependency and research-only flag.
- R6 `aegis/api.py` lines 160–300: integrations, broker read, ledger, streaming and UI routes.
- R7 repository tree `101bff7edcd2ed67670fd700b024154319efa565`: actual file inventory.
- R8 branch metadata for `main` at inspection: first-import commit, branch protection state. These are observations at the audit time, not persistent claims.

The uploaded v0.2 archive was also inspected programmatically for packaging comparison. Git blob hashes of the archived API, connector, model adapter, launcher and environment template exactly matched the above GitHub source responses. This does not mean every file was audited or that the package was fully retested in this turn.

## Official technical references

- S1 Antigravity skills: `https://antigravity.google/docs/skills` — workspace skill layout and invocation.
- S2 Antigravity CLI best practices: `https://antigravity.google/docs/cli/best-practices` — project instructions, verification loops and noninteractive invocation. Installed-version help remains authoritative for flags.
- S3 Antigravity permissions: `https://antigravity.google/docs/permissions` — actual permission enforcement and sandbox boundaries. Do not replace this with prompt-only security.
- S4 OpenRouter key creation: `https://openrouter.ai/docs/api/api-reference/api-keys/create-a-new-api-key` — management authentication, lifetime/reset limits, one-time plaintext response and expiry.
- S5 OpenRouter provider routing: `https://openrouter.ai/docs/guides/routing/provider-selection` — parameter support, fallback and privacy controls; inspect safety-retention and tool-backend qualifications.
- S6 Alpaca stock stream: `https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data` — real-time feed types and events.
- S7 Alpaca WebSocket guide: `https://docs.alpaca.markets/us/docs/streaming-market-data` — authentication, subscriptions and entitlement errors.
- S8 Alpaca market data FAQ: `https://docs.alpaca.markets/us/docs/market-data-faq` — single-exchange IEX versus consolidated SIP.
- S9 SEC EDGAR API: `https://www.sec.gov/search-filings/edgar-application-programming-interfaces` — submissions and XBRL resources.
- S10 OpenRouter runtime key inspection: `https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key` — `GET /api/v1/key`.
- S11 OpenRouter model catalog: `https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties` — actual model identifiers, parameter support and pricing metadata.
- S12 OpenRouter structured output: `https://openrouter.ai/docs/guides/features/structured-outputs` — schema requests and endpoint-specific support. Always validate output locally.
- S13 Alpaca paper environment: `https://docs.alpaca.markets/us/docs/paper-trading` — distinct paper environment and simulation limitations.
- S14 OpenRouter workspace separation: `https://openrouter.ai/docs/guides/features/workspaces/overview` — environment boundaries and account/plan limitations.
- S15 Kronos source reference: `https://github.com/shiyu-coder/Kronos/tree/67b630e67f6a18c9e9be918d9b4337c960db1e9a` — reviewed official implementation pin carried forward by the actual application.

No peer-reviewed profitability claim is made by this handoff. Current API/IDE behavior is grounded in official technical documentation rather than academic papers that cannot establish current service contracts.
