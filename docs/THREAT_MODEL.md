# Threat model and residual risks

Boundary: one trusted operator on a local machine. Browser inputs, CSV contents and external provider responses are untrusted. Broker credentials remain in environment variables. Real-money execution is not implemented.

Implemented mitigations: loopback binding, trusted Host validation, cross-origin rejection, session/CSRF checks, fixed provider domains, read-only broker access, typed/finite/bounded inputs, HTML escaping, CSP, transactional writes, idempotency, long-only policy, risk-reducing exits, source/weight digests and SafeTensors loading.

Residual risks: operator or administrator can read secrets and alter the database or model manifest; the local ledger anchor is not an external trust root; SQLite and Python are not a high-availability multi-tenant architecture; public network exposure, dependency supply-chain scanning and a security penetration test have not been qualified; user-provided historical availability/provenance may be false; coarse OHLC simulation is not real execution; market gaps can exceed configured trade-loss estimates.

Operational rules: do not expose the service publicly; do not use real broker credentials; do not add arbitrary connector URLs; do not run more than one application worker against the session scheduler; back up the closed database and retain evidence anchors independently; require a separate review for any expansion of account or execution authority.
