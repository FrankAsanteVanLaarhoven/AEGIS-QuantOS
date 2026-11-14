# Architecture and integration ownership

## Scope

AEGIS QuantOS is the only master application. QuantForge OS denotes the retained research workstream; QuantShield-FTMO denotes the retained risk workstream. Kronos is a pinned model dependency installed separately. Day Trader Skill is a reference, not a runtime dependency. No original private project code was available to merge.

## Implemented path

```text
CSV or explicit synthetic fixture
  -> Dataset validation + availability timestamps + content hash
  -> Prefix-limited quantitative features
  -> Deterministic research-cycle workers
  -> Proposed long-only order with idempotency key
  -> Aggregate pending reservations + portfolio/account policy
  -> QUEUED or REJECTED
  -> Next replay open: risk revalidation + adverse modeled fill + costs
  -> FILLED / PARTIALLY_FILLED_CANCELLED / REJECTED
  -> Cash + positions + events committed in one SQLite transaction
  -> Reconciliation, audit export and connected browser state
```

Each order includes a stable client key, request hash, submission index/time, quantities, fill result and reason codes. The same key and payload returns the same order; changed payload under the same key fails. Pending buys reserve capital and contribute to symbol and gross exposure. Pending sells reserve owned shares. Shared per-symbol participation caps bound aggregate filled volume across the bar. Orders are immediate-or-cancel at the modeled next open; an unfilled remainder is not silently retained.

Protective stops/targets are evaluated conservatively against the next completed OHLC bar. When both are crossed and ordering cannot be known, the stop wins. Stop gaps use the worse open; targets receive no favorable opening price improvement. Such modeling is approximate, not tick-level microstructure reconstruction. A halt does not prevent long-position reduction.

## Data and clock

Market timestamps, publication/availability timestamps and wall-clock event timestamps are separate. The replay clock never pretends to be a real-time market feed. The scheduler is part of the application process and advances only when explicitly started. A restart pauses it. There is no agent self-promotion, hidden background job on the assistant's infrastructure, or unbounded live execution loop.

## Frontend/API wiring

The UI uses real HTTP endpoints for all controls. The ten views share the active session/dataset. Streaming head-change notifications refresh state while avoiding active input edits. Per-view results are retained across redraws. Browser-native SSE transport still requires target-host verification because the build environment required an HTTP bridge for UI testing.

## Persistence and integrity

SQLite stores datasets, session state, research results and the event chain. WAL and `BEGIN IMMEDIATE` transactions support a single process with coordinated threads. Events hash canonical JSON containing sequence, wall time, type, payload and previous hash. The local saved head detects accidental tail deletion; a privileged actor can rewrite both records and head. Independent anchor retention is necessary for a stronger threat model.

Cash and position quantities can be recomputed from session fills. Manual orders fail closed if this reconciliation or chain verification fails. An SQLite account record is not a bank or broker ledger.

## Deployment

One user, one process, one local database. Bind only to loopback; maintain the long random token. TLS reverse proxy, OIDC/RBAC, tenant segregation, external signing/anchoring, managed secrets, queue-based workers, recovery drills, capacity testing and regulatory assessment are not complete. Do not promote this architecture to a public service merely by changing the bind address.
