---
name: aegis-complete
description: Completes and qualifies the existing AEGIS QuantOS application with real data providers, local Kronos, OpenRouter financial research, paper execution and tested UI wiring. Use for the approved autonomous AEGIS completion mission.
---

# Execute the AEGIS completion mission

Read `AGENTS.md`, `docs/implementation/MASTER_BLUEPRINT.md`, `docs/implementation/OPENROUTER_RUNBOOK.md` and `configs/autonomous_delivery.json`.

Run `python scripts/aegis_delivery_preflight.py --help`, then its preflight on the repository. It performs local inspection and prints redacted results; it does not activate providers. Audit the current HEAD against the documented baseline and preserve user changes.

Execute W00–W11 in dependency order. Write implementation state and per-task evidence to `runtime/implementation/`. Evidence may be copied to committed release records only after secret/licensing review. Use a bounded test/fix/checkpoint loop; after three distinct failed repairs, record the cause and continue an independent task.

Discover provider credentials only from the explicitly configured environment or secret-file paths. Use `python scripts/aegis_openrouter_setup.py --help` for the provided bootstrap. Do not invoke its interactive prompt in an unattended mission. A management credential and a finite user-approved limit are required for key creation; a successful utility run does not replace runtime LLM integration.

Preserve the existing UI and numerical engine while wiring real services. Live data is independent of execution mode. Real-money execution stays disabled. Show unsupported/unavailable/blocked states truthfully.

Before final completion: run fresh unit/integration/security/native-browser/provider/model/recovery gates; generate the readiness matrix; verify every green cell has appropriate evidence. Do not ask the owner to implement code that you can implement within the granted scope. Return the exact completed work, evidence, remaining external blockers, cost and startup/recovery commands.
