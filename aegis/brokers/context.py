"""Typed authoritative external execution context (AccountSnapshot + MarketSnapshot + OrderAuthorizationContext)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from aegis.market.read_model import MarketSnapshot


class AccountSnapshot(BaseModel):
    model_config = ConfigDict(extra='forbid')

    account_id: str = Field(min_length=1)
    environment: Literal['paper_external'] = 'paper_external'
    status: str
    currency: str = 'USD'
    cash: float = Field(ge=0.0)
    equity: float = Field(gt=0.0)
    buying_power: float = Field(ge=0.0)
    positions: dict[str, float] = Field(default_factory=dict)
    open_orders_count: int = Field(default=0, ge=0)
    account_revision: int = Field(ge=1)
    last_equity: float | None = Field(default=None, gt=0.0)
    peak_equity: float | None = Field(default=None, gt=0.0)
    daily_pnl_pct: float | None = None
    total_drawdown_pct: float | None = Field(default=None, ge=0.0)
    reconciled_at: datetime
    integrity_ok: bool


class OrderAuthorizationContext(BaseModel):
    model_config = ConfigDict(extra='forbid')

    account: AccountSnapshot
    market: MarketSnapshot
    policy_version: str
    authorized_by: Literal['authoritative_risk_gate'] = 'authoritative_risk_gate'

