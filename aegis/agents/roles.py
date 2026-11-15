"""Bounded finance research role specifications."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoleSpec:
    name: str
    allowed_tools: tuple[str, ...]
    max_steps: int
    budget_usd: float
    abstain_on_empty_evidence: bool = True


FINANCE_ROLES: dict[str, RoleSpec] = {
    'filing_extractor': RoleSpec('filing_extractor', ('sec_submissions', 'sec_company_facts'), 2, 0.05),
    'market_researcher': RoleSpec('market_researcher', ('market_bars', 'evidence_as_of'), 2, 0.05),
    'hypothesis_author': RoleSpec('hypothesis_author', ('quant_signals', 'evidence_as_of'), 2, 0.05),
    'evidence_checker': RoleSpec('evidence_checker', ('evidence_as_of', 'numerical_oracle'), 2, 0.05),
    'report_explainer': RoleSpec('report_explainer', ('evidence_as_of', 'quant_summary'), 2, 0.05, abstain_on_empty_evidence=False),
}

