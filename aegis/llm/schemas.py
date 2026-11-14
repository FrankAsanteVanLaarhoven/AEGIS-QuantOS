"""Role-specific structured-output schemas and ResearchAssessment contract."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class FilingExtractionOutput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    symbol: str
    period: str
    claims: list[str]
    cited_evidence_ids: list[str]
    abstain: bool = False
    abstain_reason: str | None = None


class MarketResearchOutput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    symbol: str
    regime_summary: str
    catalysts: list[str]
    conflicts: list[str] = Field(default_factory=list)
    cited_evidence_ids: list[str]
    abstain: bool = False
    abstain_reason: str | None = None


class HypothesisOutput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    symbol: str
    hypothesis: str
    candidate_strategy: Literal['trend', 'reversion', 'ensemble']
    expected_horizon_bars: int = Field(ge=1, le=240)
    cited_evidence_ids: list[str]
    abstain: bool = False
    abstain_reason: str | None = None


class EvidenceCheckOutput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    approved: bool
    missing_citations: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    cited_evidence_ids: list[str]
    abstain: bool = False
    abstain_reason: str | None = None


class ReportExplanationOutput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    summary: str
    uncertainties: list[str]
    cited_evidence_ids: list[str]
    abstain: bool = False
    abstain_reason: str | None = None


ROLE_SCHEMAS: dict[str, type[BaseModel]] = {
    'filing_extractor': FilingExtractionOutput,
    'market_researcher': MarketResearchOutput,
    'hypothesis_author': HypothesisOutput,
    'evidence_checker': EvidenceCheckOutput,
    'report_explainer': ReportExplanationOutput,
}


class ResearchAssessment(BaseModel):
    model_config = ConfigDict(extra='forbid')

    job_id: str
    role: Literal['filing_extractor', 'market_researcher', 'hypothesis_author', 'evidence_checker', 'report_explainer']
    model_id: str
    resolved_model: str | None = None
    provider: str = 'openrouter'
    input_evidence_ids: list[str]
    as_of: datetime
    extracted_claims: list[str] = Field(default_factory=list)
    cited_evidence_ids: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    numerical_outputs: dict[str, Any] = Field(default_factory=dict)
    abstain: bool = False
    abstain_reason: str | None = None
    uncertainties: list[str] = Field(default_factory=list)
    token_usage: dict[str, Any] = Field(default_factory=dict)
    cost_usd: float = Field(default=0.0, ge=0.0)
    policy_status: Literal['APPROVED', 'REJECTED_CITATION', 'REJECTED_SCHEMA', 'ABSTAINED', 'BLOCKED'] = 'APPROVED'

    def validate_citations(self) -> bool:
        allowed = set(self.input_evidence_ids)
        return all(cid in allowed for cid in self.cited_evidence_ids)
