"""Independent native risk engine and QuantShield-FTMO account-rule profiles."""
from __future__ import annotations

import hashlib
import math
import threading
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _is_finite_positive(value: float | int | None) -> bool:
    if value is None or isinstance(value, bool):
        return False
    f = float(value)
    return math.isfinite(f) and f > 0.0


def _is_finite_non_negative(value: float | int | None) -> bool:
    if value is None or isinstance(value, bool):
        return False
    f = float(value)
    return math.isfinite(f) and f >= 0.0


def _is_finite(value: float | int | None) -> bool:
    if value is None or isinstance(value, bool):
        return False
    return math.isfinite(float(value))


class OrderIntent(BaseModel):
    model_config = ConfigDict(extra='forbid')

    account_id: str = 'paper_default'
    environment: Literal['paper_local', 'paper_external'] = 'paper_local'
    symbol: str = Field(min_length=1, max_length=12)
    side: Literal['BUY', 'SELL']
    qty: float = Field(gt=0)
    order_type: Literal['market', 'limit'] = 'market'
    limit_price: float | None = Field(default=None, gt=0)
    stop_price: float | None = Field(default=None, gt=0)
    take_profit_price: float | None = Field(default=None, gt=0)
    time_in_force: Literal['day', 'gtc', 'ioc'] = 'day'
    strategy_version: str = 'v0.2'
    decision_evidence_ids: list[str] = Field(default_factory=list)
    client_order_id: str = Field(min_length=1, max_length=100)
    expires_at: datetime
    data_age_seconds: float = Field(ge=0.0)
    policy_version: str = 'quantshield_native_v1'

    @field_validator('qty', 'limit_price', 'stop_price', 'take_profit_price', 'data_age_seconds')
    @classmethod
    def _require_finite_float(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(float(value)):
            raise ValueError('Numeric order field must be finite')
        return value

    def intent_hash(self) -> str:
        payload = self.model_dump(
            mode='json',
            exclude={'expires_at', 'data_age_seconds'},
        )
        return hashlib.sha256(
            __import__('json').dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
        ).hexdigest()


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
    control_version: int = 1
    profile_revision: int = 1


class IndependentRiskGate:
    """Thread-safe risk evaluator with atomic BUY notional and SELL quantity reservations."""

    _STORE_KEY = 'risk_gate_reservations_v1'

    def __init__(self, profile: RiskProfile | None = None, store: Any = None) -> None:
        self.profile = profile or DEFAULT_PROFILES['generic_paper_v1']
        self._store = store
        self._lock = threading.RLock()
        # Keyed by f"{account_id}:{environment}:{client_order_id}"
        self._buy_reservations: dict[str, dict[str, Any]] = {}
        self._sell_reservations: dict[str, dict[str, Any]] = {}
        self._legacy_symbol_notional: dict[str, float] = {}
        self._mem_control_state: dict[str, Any] = {
            'halted': False,
            'halt_reason': None,
            'profile_id': self.profile.profile_id,
            'profile_overrides': {},
            'profile_revision': 1,
            'control_version': 1,
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }
        self._ensure_schema()
        self._load_from_store()

    def _ensure_schema(self) -> None:
        if self._store is None:
            return
        with self._store.tx():
            self._store.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS risk_control_state (
                    scope_key TEXT PRIMARY KEY,
                    halted INTEGER NOT NULL,
                    halt_reason TEXT,
                    profile_id TEXT NOT NULL,
                    profile_overrides_json TEXT NOT NULL,
                    profile_revision INTEGER NOT NULL,
                    control_version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._store.db.execute(
                """
                INSERT OR IGNORE INTO risk_control_state(
                    scope_key, halted, halt_reason, profile_id, profile_overrides_json,
                    profile_revision, control_version, updated_at
                ) VALUES (?, 0, NULL, ?, '{}', 1, 1, ?)
                """,
                ('global', self.profile.profile_id, datetime.now(timezone.utc).isoformat()),
            )

    def _read_control_state_locked(self, scope_key: str = 'global') -> dict[str, Any]:
        if self._store is None:
            return dict(self._mem_control_state)
        row = self._store.db.execute(
            'SELECT * FROM risk_control_state WHERE scope_key = ?',
            (scope_key,),
        ).fetchone()
        if row is None and scope_key != 'global':
            row = self._store.db.execute(
                'SELECT * FROM risk_control_state WHERE scope_key = ?',
                ('global',),
            ).fetchone()
        if row is None:
            return dict(self._mem_control_state)
        import json as _json
        overrides = _json.loads(row['profile_overrides_json']) if row['profile_overrides_json'] else {}
        if overrides:
            self.profile = self.profile.model_copy(update=overrides)
        return {
            'halted': bool(row['halted']),
            'halt_reason': row['halt_reason'],
            'profile_id': str(row['profile_id']),
            'profile_overrides': overrides,
            'profile_revision': int(row['profile_revision']),
            'control_version': int(row['control_version']),
            'updated_at': str(row['updated_at']),
        }

    def get_control_state(self, *, environment: str = '*', account_id: str = '*') -> dict[str, Any]:
        with self._lock:
            if self._store is None:
                return dict(self._mem_control_state)
            with self._store.lock:
                return self._read_control_state_locked('global')

    def set_halt(self, enabled: bool, *, reason: str | None = None, environment: str = '*', account_id: str = '*') -> dict[str, Any]:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            if self._store is None:
                self._mem_control_state['halted'] = bool(enabled)
                self._mem_control_state['halt_reason'] = reason if enabled else None
                self._mem_control_state['control_version'] += 1
                self._mem_control_state['updated_at'] = now_iso
                return dict(self._mem_control_state)
            with self._store.tx():
                cur = self._read_control_state_locked('global')
                new_ver = cur['control_version'] + 1
                self._store.db.execute(
                    """
                    UPDATE risk_control_state
                    SET halted = ?, halt_reason = ?, control_version = ?, updated_at = ?
                    WHERE scope_key = 'global'
                    """,
                    (1 if enabled else 0, reason if enabled else None, new_ver, now_iso),
                )
                return self._read_control_state_locked('global')

    def update_profile(self, changes: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            'max_order_notional_pct',
            'max_concentration_pct',
            'max_gross_exposure_pct',
            'max_daily_loss_pct',
            'max_total_loss_pct',
            'max_data_age_seconds',
            'no_short_selling',
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f'UNSUPPORTED_POLICY_FIELD: {sorted(unknown)} cannot be applied to authoritative risk gate')
        candidate = self.profile.model_copy(update=changes)
        RiskProfile.model_validate(candidate.model_dump())
        now_iso = datetime.now(timezone.utc).isoformat()
        import json as _json
        with self._lock:
            self.profile = candidate
            if self._store is None:
                self._mem_control_state['profile_overrides'] = {**self._mem_control_state['profile_overrides'], **changes}
                self._mem_control_state['profile_revision'] += 1
                self._mem_control_state['control_version'] += 1
                self._mem_control_state['updated_at'] = now_iso
                return dict(self._mem_control_state)
            with self._store.tx():
                cur = self._read_control_state_locked('global')
                merged = {**cur['profile_overrides'], **changes}
                self._store.db.execute(
                    """
                    UPDATE risk_control_state
                    SET profile_overrides_json = ?, profile_revision = ?, control_version = ?, updated_at = ?
                    WHERE scope_key = 'global'
                    """,
                    (_json.dumps(merged, sort_keys=True), cur['profile_revision'] + 1, cur['control_version'] + 1, now_iso),
                )
                return self._read_control_state_locked('global')

    def is_dispatch_allowed(self, intent: OrderIntent, decision: RiskDecision) -> tuple[bool, str | None]:
        """Last-moment versioned dispatch fence checked inside DB transaction right before broker POST."""
        if not decision.approved or decision.intent_hash != intent.intent_hash():
            return False, 'INVALID_RISK_DECISION'
        with self._lock:
            ctrl = self._read_control_state_locked('global')
            if ctrl['halted'] and not decision.risk_reducing:
                return False, 'EMERGENCY_HALT_ACTIVE'
            if ctrl['control_version'] != decision.control_version:
                return False, 'CONTROL_VERSION_STALE'
            if ctrl['profile_revision'] != decision.profile_revision:
                return False, 'POLICY_REVISION_STALE'
            return True, None

    @staticmethod
    def _order_key(account_id: str, environment: str, client_order_id: str) -> str:
        return f'{account_id}:{environment}:{client_order_id}'

    def _load_from_store(self) -> None:
        if self._store is None:
            return
        raw = self._store.get(self._STORE_KEY)
        if not isinstance(raw, dict):
            return
        self._buy_reservations = dict(raw.get('buy', {}))
        self._sell_reservations = dict(raw.get('sell', {}))
        self._legacy_symbol_notional = dict(raw.get('legacy', {}))

    def _persist_to_store(self) -> None:
        if self._store is None:
            return
        self._store.put(
            self._STORE_KEY,
            {
                'buy': self._buy_reservations,
                'sell': self._sell_reservations,
                'legacy': self._legacy_symbol_notional,
            },
        )

    def _reserved_buy_notional_by_symbol(self, account_id: str, environment: str, symbol: str) -> float:
        total = self._legacy_symbol_notional.get(symbol, 0.0)
        for rec in self._buy_reservations.values():
            if rec['account_id'] == account_id and rec['environment'] == environment and rec['symbol'] == symbol:
                total += float(rec['notional'])
        return total

    def _reserved_buy_total(self, account_id: str, environment: str) -> float:
        total = sum(self._legacy_symbol_notional.values())
        for rec in self._buy_reservations.values():
            if rec['account_id'] == account_id and rec['environment'] == environment:
                total += float(rec['notional'])
        return total

    def _reserved_sell_qty(self, account_id: str, environment: str, symbol: str) -> float:
        total = 0.0
        for rec in self._sell_reservations.values():
            if rec['account_id'] == account_id and rec['environment'] == environment and rec['symbol'] == symbol:
                total += float(rec['qty'])
        return total

    def evaluate(
        self,
        intent: OrderIntent,
        *,
        price: float,
        equity: float,
        cash: float,
        positions: dict[str, float],
        marks: dict[str, float] | None = None,
        daily_pnl_pct: float | None = 0.0,
        total_drawdown_pct: float | None = 0.0,
        halted: bool | None = None,
        integrity_ok: bool = True,
        account_revision: int = 1,
        now: datetime | None = None,
    ) -> RiskDecision:
        now_utc = now or datetime.now(timezone.utc)
        sym = intent.symbol.upper()
        i_hash = intent.intent_hash()
        order_key = self._order_key(intent.account_id, intent.environment, intent.client_order_id)
        checks: list[str] = []
        reasons: list[str] = []

        with self._lock:
            tx_ctx = self._store.tx() if self._store is not None else nullcontext()
            with tx_ctx:
                return self._evaluate_locked(
                    intent,
                    price=price,
                    equity=equity,
                    cash=cash,
                    positions=positions,
                    marks=marks,
                    daily_pnl_pct=daily_pnl_pct,
                    total_drawdown_pct=total_drawdown_pct,
                    halted=halted,
                    integrity_ok=integrity_ok,
                    account_revision=account_revision,
                    now_utc=now_utc,
                    sym=sym,
                    i_hash=i_hash,
                    order_key=order_key,
                    checks=checks,
                    reasons=reasons,
                )

    def _evaluate_locked(
        self,
        intent: OrderIntent,
        *,
        price: float,
        equity: float,
        cash: float,
        positions: dict[str, float],
        marks: dict[str, float] | None,
        daily_pnl_pct: float | None,
        total_drawdown_pct: float | None,
        halted: bool | None,
        integrity_ok: bool,
        account_revision: int,
        now_utc: datetime,
        sym: str,
        i_hash: str,
        order_key: str,
        checks: list[str],
        reasons: list[str],
    ) -> RiskDecision:
        if True:
            self._load_from_store()
            ctrl = self._read_control_state_locked('global')
            effective_halted = bool(halted) or bool(ctrl['halted'])
            ctrl_ver = int(ctrl['control_version'])
            prof_rev = int(ctrl['profile_revision'])

            existing = self._buy_reservations.get(order_key) or self._sell_reservations.get(order_key)
            if existing is not None:
                if existing.get('intent_hash') == i_hash:
                    return RiskDecision(
                        approved=True,
                        intent_hash=i_hash,
                        account_revision=account_revision,
                        checks=['idempotent_reservation'],
                        reasons=[],
                        risk_reducing=bool(existing.get('risk_reducing', False)),
                        policy_version=self.profile.profile_id,
                        control_version=ctrl_ver,
                        profile_revision=prof_rev,
                    )
                return RiskDecision(
                    approved=False,
                    intent_hash=i_hash,
                    account_revision=account_revision,
                    checks=['idempotency'],
                    reasons=['DUPLICATE_CLIENT_ORDER_ID'],
                    risk_reducing=False,
                    policy_version=self.profile.profile_id,
                    control_version=ctrl_ver,
                    profile_revision=prof_rev,
                )

            held_raw = positions.get(sym, positions.get(intent.symbol, 0.0))
            held_qty = float(held_raw) if _is_finite(held_raw) else math.nan
            reserved_sell = self._reserved_sell_qty(intent.account_id, intent.environment, sym)
            available_sell_qty = held_qty - reserved_sell if math.isfinite(held_qty) else math.nan
            is_risk_reducing = (
                intent.side == 'SELL'
                and math.isfinite(available_sell_qty)
                and available_sell_qty >= intent.qty - 1e-9
            )

            if intent.expires_at.astimezone(timezone.utc) <= now_utc.astimezone(timezone.utc):
                reasons.append('INTENT_EXPIRED')
            checks.append('expiry')

            if not integrity_ok and not is_risk_reducing:
                reasons.append('INTEGRITY_FAILED')
            checks.append('integrity')

            if effective_halted and not is_risk_reducing:
                reasons.append('EMERGENCY_HALT_ACTIVE')
            checks.append('halt')

            if not _is_finite_non_negative(intent.data_age_seconds):
                reasons.append('INVALID_VALUATION')
            elif intent.data_age_seconds > self.profile.max_data_age_seconds and not is_risk_reducing:
                reasons.append('STALE_MARKET_DATA')
            checks.append('data_freshness')

            loss_state_present = daily_pnl_pct is not None and total_drawdown_pct is not None
            if not loss_state_present and not is_risk_reducing:
                reasons.append('MISSING_ACCOUNT_LOSS_STATE')

            valuation_ok = (
                _is_finite_positive(price)
                and _is_finite_positive(equity)
                and _is_finite_non_negative(cash)
                and _is_finite_positive(intent.qty)
                and (is_risk_reducing or (_is_finite(daily_pnl_pct) and _is_finite(total_drawdown_pct)))
                and math.isfinite(held_qty)
            )
            if not valuation_ok and (loss_state_present or is_risk_reducing):
                reasons.append('INVALID_VALUATION')
            checks.append('valuation')

            if intent.side == 'SELL' and self.profile.no_short_selling:
                if not math.isfinite(available_sell_qty) or intent.qty > available_sell_qty + 1e-9:
                    reasons.append('SHORT_SELLING_PROHIBITED')
            checks.append('no_short_selling')

            if not is_risk_reducing:
                if _is_finite(daily_pnl_pct) and daily_pnl_pct <= -self.profile.max_daily_loss_pct:
                    reasons.append('MAX_DAILY_LOSS_BREACHED')
                if _is_finite(total_drawdown_pct) and total_drawdown_pct >= self.profile.max_total_loss_pct:
                    reasons.append('MAX_TOTAL_DRAWDOWN_BREACHED')
                checks.append('loss_limits')

                if valuation_ok:
                    notional = float(intent.qty) * float(price)
                    reserved_cash = self._reserved_buy_total(intent.account_id, intent.environment)
                    if notional > (float(cash) - reserved_cash) + 1e-9:
                        reasons.append('INSUFFICIENT_CASH')
                    if (notional / float(equity)) > self.profile.max_order_notional_pct + 1e-9:
                        reasons.append('ORDER_NOTIONAL_LIMIT')

                    sym_mark = float(price)
                    if marks is not None:
                        m_val = marks.get(sym, marks.get(intent.symbol))
                        if m_val is not None:
                            if not _is_finite_positive(m_val):
                                reasons.append('INVALID_VALUATION')
                            else:
                                sym_mark = float(m_val)

                    current_sym_notional = abs(held_qty) * sym_mark + self._reserved_buy_notional_by_symbol(
                        intent.account_id, intent.environment, sym
                    )
                    if ((current_sym_notional + notional) / float(equity)) > self.profile.max_concentration_pct + 1e-9:
                        reasons.append('CONCENTRATION_LIMIT')

                    # Per-instrument valuation for all held positions; fail closed if any non-order symbol lacks a mark.
                    norm_marks: dict[str, float] = {}
                    if marks is not None:
                        for mk_sym, mk_val in marks.items():
                            norm_marks[mk_sym.upper()] = mk_val

                    held_gross = 0.0
                    marks_valid = True
                    for pos_sym, pos_qty_raw in positions.items():
                        if not _is_finite(pos_qty_raw):
                            reasons.append('INVALID_VALUATION')
                            marks_valid = False
                            break
                        pos_qty = abs(float(pos_qty_raw))
                        if pos_qty <= 1e-9:
                            continue
                        u_sym = pos_sym.upper()
                        if u_sym == sym:
                            pos_mark = sym_mark
                        elif u_sym in norm_marks:
                            m_other = norm_marks[u_sym]
                            if not _is_finite_positive(m_other):
                                reasons.append('INVALID_VALUATION')
                                marks_valid = False
                                break
                            pos_mark = float(m_other)
                        else:
                            reasons.append('MISSING_POSITION_MARKS')
                            marks_valid = False
                            break
                        held_gross += pos_qty * pos_mark

                    if marks_valid:
                        gross = held_gross + reserved_cash + notional
                        if (gross / float(equity)) > self.profile.max_gross_exposure_pct + 1e-9:
                            reasons.append('GROSS_EXPOSURE_LIMIT')
                checks.append('exposure_limits')

            if not reasons:
                if intent.side == 'BUY':
                    self._buy_reservations[order_key] = {
                        'account_id': intent.account_id,
                        'environment': intent.environment,
                        'client_order_id': intent.client_order_id,
                        'symbol': sym,
                        'notional': float(intent.qty) * float(price),
                        'intent_hash': i_hash,
                        'risk_reducing': False,
                    }
                else:
                    self._sell_reservations[order_key] = {
                        'account_id': intent.account_id,
                        'environment': intent.environment,
                        'client_order_id': intent.client_order_id,
                        'symbol': sym,
                        'qty': float(intent.qty),
                        'intent_hash': i_hash,
                        'risk_reducing': is_risk_reducing,
                    }
                self._persist_to_store()

        return RiskDecision(
            approved=len(reasons) == 0,
            intent_hash=i_hash,
            account_revision=account_revision,
            checks=checks,
            reasons=reasons,
            risk_reducing=is_risk_reducing,
            policy_version=self.profile.profile_id,
            control_version=ctrl_ver,
            profile_revision=prof_rev,
        )

    def release_order_reservation(self, account_id: str, environment: str, client_order_id: str) -> None:
        key = self._order_key(account_id, environment, client_order_id)
        with self._lock:
            tx_ctx = self._store.tx() if self._store is not None else nullcontext()
            with tx_ctx:
                self._load_from_store()
                changed = False
                if key in self._buy_reservations:
                    del self._buy_reservations[key]
                    changed = True
                if key in self._sell_reservations:
                    del self._sell_reservations[key]
                    changed = True
                if changed:
                    self._persist_to_store()

    def release_reservation(self, symbol: str, notional: float) -> None:
        with self._lock:
            tx_ctx = self._store.tx() if self._store is not None else nullcontext()
            with tx_ctx:
                self._load_from_store()
                sym = symbol.upper()
                cur = self._legacy_symbol_notional.get(sym, 0.0)
                self._legacy_symbol_notional[sym] = max(0.0, cur - float(notional))
                self._persist_to_store()
