"""Independent native risk engine and QuantShield-FTMO account-rule profiles."""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class OrderIntent(BaseModel):
    model_config = ConfigDict(extra='forbid')

    account_id: str = 'paper_default'
    environment: Literal['paper_local', 'paper_external'] = 'paper_local'
    symbol: str = Field(min_length=1, max_length=12)
    side: Literal['BUY', 'SELL']
    qty: float = Field(gt=0)
    order_type: Literal['market', 'limit'] = 'market'
    limit_price: float | None = Field(default=None, gt=0)
    time_in_force: Literal['day', 'gtc', 'ioc'] = 'day'
    strategy_version: str = 'v0.2'
    decision_evidence_ids: list[str] = Field(default_factory=list)
    client_order_id: str = Field(min_length=1, max_length=100)
    expires_at: datetime
    data_age_seconds: float = Field(ge=0.0)
    policy_version: str = 'quantshield_native_v1'

    def intent_hash(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode('utf-8')).hexdigest()


class RiskProfile(BaseModel):
    model_config = ConfigDict(extra='forbid')

    profile_id: str
    label: str
    timezone: str = 'UTC'
    max_order_notional_pct: float = Field(default=0.25, gt=0, le=1.0)
    max_concentration_pct: float = Field(default=0.35, gt=0, le=1.0)
    max_gross_exposure_pct: float = Field(default=0.90, gt=0, le=1.0)
    max_daily_loss_pct: float = Field(default=0.05, gt=0, le=0.5)
    max_total_loss_pct: float = Field(default=0.10, gt=0, le=0.5)
    max_data_age_seconds: float = Field(default=30.0, gt=0)
    no_short_selling: bool = True


DEFAULT_PROFILES: dict[str, RiskProfile] = {
    'generic_paper_v1': RiskProfile(
        profile_id='generic_paper_v1',
        label='Generic Paper Trading Profile (Native)',
        timezone='America/New_York',
    ),
    'quantshield_ftmo_style_v1': RiskProfile(
        profile_id='quantshield_ftmo_style_v1',
        label='QuantShield-FTMO Native Evaluation Rules (Unverified against external FTMO portal)',
        timezone='Europe/Prague',
        max_daily_loss_pct=0.05,
        max_total_loss_pct=0.10,
        max_order_notional_pct=0.20,
        max_concentration_pct=0.30,
        max_gross_exposure_pct=0.80,
    ),
}


class RiskDecision(BaseModel):
    model_config = ConfigDict(extra='forbid')

    approved: bool
    intent_hash: str
    account_revision: int
    checks: list[str]
    reasons: list[str]
    risk_reducing: bool = False
    policy_version: str


class IndependentRiskGate:
    """Thread-safe risk evaluator with atomic cross-symbol exposure reservations."""

    def __init__(self, profile: RiskProfile | None = None) -> None:
        self.profile = profile or DEFAULT_PROFILES['generic_paper_v1']
        self._lock = threading.Lock()
        self._reserved_notional: dict[str, float] = {}

    def evaluate(
        self,
        intent: OrderIntent,
        *,
        price: float,
        equity: float,
        cash: float,
        positions: dict[str, float],
        daily_pnl_pct: float = 0.0,
        total_drawdown_pct: float = 0.0,
        halted: bool = False,
        integrity_ok: bool = True,
        account_revision: int = 1,
        now: datetime | None = None,
    ) -> RiskDecision:
        now_utc = now or datetime.now(timezone.utc)
        sym = intent.symbol.upper()
        held_qty = float(positions.get(sym, 0.0))
        is_risk_reducing = intent.side == 'SELL' and held_qty >= intent.qty
        checks: list[str] = []
        reasons: list[str] = []

        with self._lock:
            if intent.expires_at.astimezone(timezone.utc) <= now_utc.astimezone(timezone.utc):
                reasons.append('INTENT_EXPIRED')
            checks.append('expiry')

            if not integrity_ok and not is_risk_reducing:
                reasons.append('INTEGRITY_FAILED')
            checks.append('integrity')

            if halted and not is_risk_reducing:
                reasons.append('EMERGENCY_HALT_ACTIVE')
            checks.append('halt')

            if intent.data_age_seconds > self.profile.max_data_age_seconds and not is_risk_reducing:
                reasons.append('STALE_MARKET_DATA')
            checks.append('data_freshness')

            if price <= 0 or equity <= 0:
                reasons.append('INVALID_VALUATION')
            checks.append('valuation')

            if intent.side == 'SELL' and self.profile.no_short_selling and intent.qty > held_qty + 1e-9:
                reasons.append('SHORT_SELLING_PROHIBITED')
            checks.append('no_short_selling')

            if not is_risk_reducing:
                if daily_pnl_pct <= -self.profile.max_daily_loss_pct:
                    reasons.append('MAX_DAILY_LOSS_BREACHED')
                if total_drawdown_pct >= self.profile.max_total_loss_pct:
                    reasons.append('MAX_TOTAL_DRAWDOWN_BREACHED')
                checks.append('loss_limits')

                notional = intent.qty * price
                if notional > cash + 1e-9:
                    reasons.append('INSUFFICIENT_CASH')
                if equity > 0 and (notional / equity) > self.profile.max_order_notional_pct + 1e-9:
                    reasons.append('ORDER_NOTIONAL_LIMIT')

                current_sym_notional = held_qty * price + self._reserved_notional.get(sym, 0.0)
                if equity > 0 and ((current_sym_notional + notional) / equity) > self.profile.max_concentration_pct + 1e-9:
                    reasons.append('CONCENTRATION_LIMIT')

                gross = sum(abs(q) * price for q in positions.values()) + sum(self._reserved_notional.values()) + notional
                if equity > 0 and (gross / equity) > self.profile.max_gross_exposure_pct + 1e-9:
                    reasons.append('GROSS_EXPOSURE_LIMIT')
                checks.append('exposure_limits')

                if not reasons:
                    self._reserved_notional[sym] = self._reserved_notional.get(sym, 0.0) + notional

        return RiskDecision(
            approved=len(reasons) == 0,
            intent_hash=intent.intent_hash(),
            account_revision=account_revision,
            checks=checks,
            reasons=reasons,
            risk_reducing=is_risk_reducing,
            policy_version=self.profile.profile_id,
        )

    def release_reservation(self, symbol: str, notional: float) -> None:
        with self._lock:
            sym = symbol.upper()
            cur = self._reserved_notional.get(sym, 0.0)
            self._reserved_notional[sym] = max(0.0, cur - notional)
