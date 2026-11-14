# OpenRouter setup and runtime integration runbook

## What is prepared and what is not

The handoff includes a runnable standard-library bootstrap helper. It has been designed for offline unit testing and for execution on the user's authorized host. It does not contain a real API key, does not register an OpenRouter account, does not buy credits, and does not by itself add LLM calls to the application. Runtime gateway implementation is W04 in the master blueprint.

Antigravity's own editor/CLI model login is separate from the application's OpenRouter credentials. Kronos is a local numerical model with separately downloaded artifacts, not an OpenRouter chat model. AEGIS login/session tokens are a third separate credential category.

## Already-authorized runtime key

Provide credentials through the authorized runtime environment or a private file, not chat or command-line arguments. The helper accepts `OPENROUTER_API_KEY` **or** `OPENROUTER_API_KEY_FILE`, never both. With a file, owner-only permissions are required.

```bash
# The path below contains a real key already supplied by the account owner.
export OPENROUTER_API_KEY_FILE="$HOME/.config/aegis-quantos/secrets/openrouter.key"
python scripts/aegis_openrouter_setup.py status
python scripts/aegis_openrouter_setup.py verify
python scripts/aegis_openrouter_setup.py catalog \
  --output runtime/provider-evidence/openrouter-catalog.json
```

`status` checks only local presence/format, not authenticity. `verify` calls `GET /api/v1/key`; it prints only allowed budget/expiry metadata, never plaintext key, provider account identifiers or key labels. `catalog` fetches model metadata, filters candidates with both `tools` and `structured_outputs`, and does not call inference. Metadata is not a benchmark and endpoint support must still be tested.

To securely import an already-authorized key held in the environment:

```bash
python scripts/aegis_openrouter_setup.py import-key
```

An optional masked terminal prompt is available for one-time owner setup:

```bash
python scripts/aegis_openrouter_setup.py import-key --prompt
```

The autonomous mission must not invoke `--prompt`; it records a credential blocker and continues other tasks instead. Do not print shell environments or use `set -x` with secrets.

## Programmatic creation using existing management authorization

OpenRouter requires a management credential for key creation; a regular inference key cannot mint other keys. If the account owner has already supplied an authorized management key file, and an explicit finite budget, run:

```bash
export OPENROUTER_MANAGEMENT_KEY_FILE="/absolute/private/path/to/authorized-management-key"
# APPROVED_LIFETIME_USD must be an actual owner-approved positive amount.
python scripts/aegis_openrouter_setup.py create-key \
  --limit-usd "$APPROVED_LIFETIME_USD" --expires-days 30
```

This makes one `POST /api/v1/keys`, stores the returned runtime secret at the owner-only default path outside Git, and creates a lifetime cap with no automatic reset. It does not buy credits or increase the account's spending budget. The management key is never copied into the application configuration.

Creation refuses an existing target; it does not rotate keys silently. A timed-out POST can have succeeded remotely. Do not rerun blind: reconcile the provider's key list first. An explicit partial-provisioning error also requires key reconciliation/revocation. Avoid concurrent provisioning, unsupported secret directories and symlinks.

After provisioning, remove management authorization from the runtime process/environment. The account owner or a separate restricted bootstrap identity can retain it in an approved secret manager; the financial research service must never receive it.

## One real inference smoke test

First choose an actual candidate model ID from the retrieved catalog and verify provider capability/privacy under the account. Then run:

```bash
python scripts/aegis_openrouter_setup.py smoke \
  --model "$APPROVED_OPENROUTER_MODEL" --allow-billable
```

This sends only a small test request for `{"ok": true}`, with a 256-output-token ceiling, structured JSON schema, parameter-support requirement, collection denied, ZDR requested and provider fallback disabled. It requires a positive finite provider key cap. It may consume credits; explicit authorization and an existing capped key are prerequisites. Output-token limits alone do not replace application-wide cost accounting.

An auth check is not model inference. A successful test request is not a financial research benchmark. Do not mark W04 or the release complete until the in-application role/tool/evidence/cost paths are tested.

## Required application environment contract (to implement in W01/W04)

The following names are proposed new settings, not already consumed by the current `scripts/run.py`:

```dotenv
AEGIS_LLM_PROVIDER=openrouter
OPENROUTER_API_KEY_FILE=/absolute/private/path/to/openrouter.key
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
AEGIS_OPENROUTER_POLICY=configs/openrouter.policy.json
AEGIS_OPENROUTER_MODELS_LOCK=configs/openrouter.models.lock.json
AEGIS_ENABLE_PAID_LLM=false
```

The runtime must read a secret file only in the provider process, not expose it through `/api/integrations`, and avoid forwarding it into tool results, telemetry or agent context. Model configuration contains **IDs and limits**, never key values. Keep management-key variables outside the runtime environment file.

## Model selection and capability routing

Implement roles for extraction, market research, quantitative hypothesis drafting, verification and report explanation. Use the catalog as discovery; evaluate candidates on a fixed financial task suite and select by required quality/privacy/latency/cost, not by name or recency alone. Pin selected IDs and approved fallbacks. Hosted model IDs may not imply immutable weights; record provider, timestamps and returned model identifiers and preserve outputs for replay.

Requests should include a role-specific JSON Schema and a validated tool allowlist. Privacy preferences cannot establish legal permission to transmit licensed feeds or confidential data. Redact/minimize content; check account retention controls, provider terms and downstream tool policies. If a provider cannot meet requirements, block that route instead of weakening them silently.

## Useful failure classification

- Missing secret: `BLOCKED_CREDENTIAL`.
- HTTP 401: invalid or revoked authorization; no repeated key guessing.
- HTTP 402 / exhausted cap: `BLOCKED_BUDGET`; no auto-top-up.
- HTTP 403: permission or management-versus-runtime mismatch; do not bypass.
- HTTP 429: bounded backoff and rate scheduling; account for already-dispatched requests.
- Unknown model/no compatible private endpoint: `BLOCKED_MODEL_CONFIGURATION`.
- Network failure after POST: outcome unknown; avoid unbounded potentially billable retry.
- Invalid/schema-violating output: reject; retain redacted evidence; never convert it to a trade.

## Release evidence

Save redacted authentication result, dated model catalog, role model lock, real smoke result, cost record, schema/injection/fallback tests, runtime UI proof and secret-scanning report. Never store a provider response wholesale from key creation: it contains the one-time plaintext key.
