# AEGIS QuantOS project instructions

The authoritative implementation mission is `docs/implementation/MASTER_BLUEPRINT.md` and the task DAG is `configs/autonomous_delivery.json`. Read these before changing the platform. Invoke the workspace skill `aegis-complete`.

Extend the existing repository; do not replace it with a new scaffold. Preserve approved scope v5 and the exclusion list in the blueprint. Do not import unrelated projects. The QuantForge and QuantShield responsibilities are native until actual source integrations are independently verified.

Work autonomously on implementation, tests and isolated local/paper services. Record assumptions and continue independent tasks rather than asking for routine decisions. Stop only at actual authorization/credential/entitlement/environment boundaries; report a machine-readable blocker, not a fake pass. No real-money execution, account registration, paid subscription purchase, security bypass, secret disclosure, force-push or production-destructive operation is authorized by this mission.

No secrets in chat, logs, browser bundles, Git, screenshots or model messages. Read only explicitly designated secret sources; never hunt for credentials across the host. Do not change global agent permissions to unrestricted mode.

Preserve tests. Add regression tests for each defect. Rerun real external and native-browser tests before claiming external readiness. Mocks and archived results cannot satisfy those gates. Use the checked-out repository's documented test commands, not fabricated tools.

Complete each eligible work package end-to-end: settings -> provider/engine -> state -> API -> UI -> security/observability -> evidence. Persist progress after each coherent commit on an integration branch. Do not mark complete with pending required gates.
