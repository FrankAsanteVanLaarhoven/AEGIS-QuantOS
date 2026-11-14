"""External paper-broker OMS adapter (Alpaca Paper ONLY) with SQLite durable outbox and reconciliation."""
from __future__ import annotations

import json
import math
import threading
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict

from aegis.brokers.context import AccountSnapshot
from aegis.risk.policy import IndependentRiskGate, OrderIntent, RiskDecision
from aegis.secrets import SecretResolver
from aegis.settings import AegisSettings

OrderState = Literal[
    'PENDING_SUBMIT',
    'SUBMITTED',
    'ACCEPTED',
    'PARTIALLY_FILLED',
    'FILLED',
    'CANCEL_PENDING',
    'CANCELLED',
    'REJECTED',
    'EXPIRED',
    'UNKNOWN',
]

TERMINAL_STATES: set[str] = {'FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED'}


class OrderOutboxRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    account_id: str = 'paper_default'
    client_order_id: str
    intent_hash: str
    symbol: str
    side: Literal['BUY', 'SELL']
    qty: float
    order_type: Literal['market', 'limit'] = 'market'
    limit_price: float | None = None
    time_in_force: Literal['day', 'gtc', 'ioc'] = 'day'
    environment: Literal['paper_local', 'paper_external']
    state: OrderState = 'PENDING_SUBMIT'
    broker_order_id: str | None = None
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    updated_at: datetime
    last_error: str | None = None


class AlpacaPaperOMS:
    """Strictly paper-only external OMS with SQLite durable outbox, idempotent client_order_id, and restart reconciliation."""

    ALLOWED_HOST = 'https://paper-api.alpaca.markets'

    def __init__(
        self,
        settings: AegisSettings,
        resolver: SecretResolver,
        *,
        client: httpx.Client | None = None,
        store: Any = None,
        risk_gate: IndependentRiskGate | None = None,
    ) -> None:
        if settings.alpaca_paper_base_url.rstrip('/') != self.ALLOWED_HOST:
            raise ValueError('Only Alpaca paper-api host is permitted')
        self.settings = settings
        self.resolver = resolver
        self._client = client
        self._store = store
        self._risk_gate = risk_gate
        self._lock = threading.RLock()
        self._outbox: dict[tuple[str, str, str], OrderOutboxRecord] = {}
        self._ensure_schema()
        self._load_all()

    def _ensure_schema(self) -> None:
        if self._store is None:
            return
        with self._store.lock:
            self._store.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_oms_outbox (
                    account_id TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    client_order_id TEXT NOT NULL,
                    intent_hash TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    PRIMARY KEY (account_id, environment, client_order_id)
                );
                """
            )

    def _load_all(self) -> None:
        if self._store is None:
            return
        with self._store.lock:
            rows = self._store.db.execute('SELECT account_id, environment, client_order_id, record_json FROM paper_oms_outbox').fetchall()
            loaded: dict[tuple[str, str, str], OrderOutboxRecord] = {}
            for r in rows:
                rec = OrderOutboxRecord.model_validate_json(r['record_json'])
                loaded[(r['account_id'], r['environment'], r['client_order_id'])] = rec
            self._outbox = loaded

    def _persist(self, rec: OrderOutboxRecord) -> None:
        key = (rec.account_id, rec.environment, rec.client_order_id)
        self._outbox[key] = rec
        if self._store is None:
            return
        with self._store.tx():
            self._store.db.execute(
                """
                INSERT INTO paper_oms_outbox(account_id, environment, client_order_id, intent_hash, record_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account_id, environment, client_order_id) DO UPDATE SET
                    intent_hash = excluded.intent_hash,
                    record_json = excluded.record_json
                """,
                (rec.account_id, rec.environment, rec.client_order_id, rec.intent_hash, rec.model_dump_json()),
            )

    def _get_record(self, client_order_id: str, account_id: str | None = None, environment: str = 'paper_external') -> OrderOutboxRecord:
        if account_id is not None:
            key = (account_id, environment, client_order_id)
            if self._store is not None:
                with self._store.lock:
                    row = self._store.db.execute(
                        'SELECT record_json FROM paper_oms_outbox WHERE account_id=? AND environment=? AND client_order_id=?',
                        key,
                    ).fetchone()
                    if row is not None:
                        rec = OrderOutboxRecord.model_validate_json(row['record_json'])
                        self._outbox[key] = rec
                        return rec
            if key not in self._outbox:
                raise KeyError(f'Unknown client_order_id: {client_order_id}')
            return self._outbox[key]
        self._load_all()
        matches = [r for (acc, env, cid), r in self._outbox.items() if env == environment and cid == client_order_id]
        if not matches:
            raise KeyError(f'Unknown client_order_id: {client_order_id}')
        if len(matches) > 1:
            raise ValueError(f'Ambiguous client_order_id across accounts: {client_order_id}')
        return matches[0]

    def _release_if_terminal(self, rec: OrderOutboxRecord) -> None:
        if self._risk_gate is not None and rec.state in TERMINAL_STATES:
            self._risk_gate.release_order_reservation(rec.account_id, rec.environment, rec.client_order_id)

    def _headers(self) -> dict[str, str]:
        k = self.resolver.resolve('ALPACA_API_KEY')
        s = self.resolver.resolve('ALPACA_SECRET_KEY')
        if not k or not s:
            raise ValueError('BLOCKED_CREDENTIAL: Alpaca paper credentials missing')
        return {'APCA-API-KEY-ID': k, 'APCA-API-SECRET-KEY': s, 'Content-Type': 'application/json'}

    def submit_order(self, intent: OrderIntent, decision: RiskDecision) -> OrderOutboxRecord:
        i_hash = intent.intent_hash()
        if not decision.approved or decision.intent_hash != i_hash:
            raise ValueError('OrderIntent lacks valid matching RiskDecision')
        if intent.environment != 'paper_external':
            raise ValueError('AlpacaPaperOMS only accepts paper_external intents')
        if intent.order_type == 'limit' and intent.limit_price is None:
            raise ValueError('Limit order requires limit_price')

        key = (intent.account_id, intent.environment, intent.client_order_id)
        with self._lock:
            tx_ctx = self._store.tx() if self._store is not None else nullcontext()
            with tx_ctx:
                self._load_all()
                if key in self._outbox:
                    existing = self._outbox[key]
                    if existing.intent_hash != i_hash:
                        raise ValueError('IDEMPOTENCY_CONFLICT: client_order_id already exists with a different OrderIntent')
                    return existing

                now = datetime.now(timezone.utc)
                rec = OrderOutboxRecord(
                    account_id=intent.account_id,
                    client_order_id=intent.client_order_id,
                    intent_hash=i_hash,
                    symbol=intent.symbol.upper(),
                    side=intent.side,
                    qty=intent.qty,
                    order_type=intent.order_type,
                    limit_price=intent.limit_price,
                    time_in_force=intent.time_in_force,
                    environment='paper_external',
                    state='PENDING_SUBMIT',
                    updated_at=now,
                )
                # Persist intent atomically BEFORE remote send
                self._persist(rec)

        headers = self._headers()
        payload: dict[str, Any] = {
            'symbol': rec.symbol,
            'qty': str(rec.qty),
            'side': rec.side.lower(),
            'type': intent.order_type,
            'time_in_force': intent.time_in_force,
            'client_order_id': rec.client_order_id,
        }
        if intent.order_type == 'limit' and intent.limit_price is not None:
            payload['limit_price'] = str(intent.limit_price)

        url = f'{self.ALLOWED_HOST}/v2/orders'
        own = self._client is None
        http = self._client or httpx.Client(timeout=10.0)
        try:
            try:
                resp = http.post(url, headers=headers, json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                # Ambiguous network outcome stays UNKNOWN until reconciled by client_order_id
                rec.state = 'UNKNOWN'
                rec.last_error = f'{type(exc).__name__} after POST; awaiting client_order_id reconciliation'
                rec.updated_at = datetime.now(timezone.utc)
                with self._lock:
                    self._persist(rec)
                return rec

            if resp.status_code >= 500:
                rec.state = 'UNKNOWN'
                rec.last_error = f'HTTP {resp.status_code} from broker; awaiting reconciliation'
                rec.updated_at = datetime.now(timezone.utc)
                with self._lock:
                    self._persist(rec)
                return rec

            if resp.status_code >= 400:
                rec.state = 'REJECTED'
                rec.last_error = f'HTTP {resp.status_code}'
                rec.updated_at = datetime.now(timezone.utc)
                with self._lock:
                    self._persist(rec)
                    self._release_if_terminal(rec)
                return rec

            body = resp.json()
            rec.broker_order_id = body.get('id')
            st = str(body.get('status', '')).lower()
            if st in ('new', 'accepted', 'pending_new'):
                rec.state = 'ACCEPTED'
            elif st == 'filled':
                rec.state = 'FILLED'
            elif st == 'partially_filled':
                rec.state = 'PARTIALLY_FILLED'
            else:
                rec.state = 'SUBMITTED'
            rec.updated_at = datetime.now(timezone.utc)
            with self._lock:
                self._persist(rec)
                self._release_if_terminal(rec)
            return rec
        finally:
            if own:
                http.close()

    def reconcile_by_client_id(self, client_order_id: str, *, account_id: str | None = None) -> OrderOutboxRecord:
        with self._lock:
            rec = self._get_record(client_order_id, account_id=account_id, environment='paper_external')
        headers = self._headers()
        url = f'{self.ALLOWED_HOST}/v2/orders:by_client_order_id'
        own = self._client is None
        http = self._client or httpx.Client(timeout=10.0)
        try:
            try:
                resp = http.get(url, headers=headers, params={'client_order_id': client_order_id})
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                rec.state = 'UNKNOWN'
                rec.last_error = f'Reconcile transport error: {type(exc).__name__}'
                rec.updated_at = datetime.now(timezone.utc)
                with self._lock:
                    self._persist(rec)
                return rec

            if resp.status_code == 404:
                # Conservative: 404 may reflect eventual consistency or an unsubmitted order; stay UNKNOWN
                rec.state = 'UNKNOWN'
                rec.last_error = 'Not found at broker during reconciliation (404); remains UNKNOWN'
                rec.updated_at = datetime.now(timezone.utc)
                with self._lock:
                    self._persist(rec)
                return rec

            if resp.status_code >= 500:
                rec.state = 'UNKNOWN'
                rec.last_error = f'Broker HTTP {resp.status_code} during reconciliation'
                rec.updated_at = datetime.now(timezone.utc)
                with self._lock:
                    self._persist(rec)
                return rec

            resp.raise_for_status()
            body = resp.json()
            st_map = {
                'new': 'ACCEPTED',
                'accepted': 'ACCEPTED',
                'pending_new': 'ACCEPTED',
                'partially_filled': 'PARTIALLY_FILLED',
                'filled': 'FILLED',
                'pending_cancel': 'CANCEL_PENDING',
                'canceled': 'CANCELLED',
                'cancelled': 'CANCELLED',
                'rejected': 'REJECTED',
                'expired': 'EXPIRED',
            }
            rec.broker_order_id = body.get('id', rec.broker_order_id)
            rec.state = st_map.get(str(body.get('status', '')).lower(), rec.state)  # type: ignore[assignment]
            rec.filled_qty = float(body.get('filled_qty') or 0.0)
            if body.get('filled_avg_price'):
                rec.filled_avg_price = float(body['filled_avg_price'])
            rec.last_error = None
            rec.updated_at = datetime.now(timezone.utc)
            with self._lock:
                self._persist(rec)
                self._release_if_terminal(rec)
            return rec
        finally:
            if own:
                http.close()

    def reconcile_open_orders(self) -> list[OrderOutboxRecord]:
        with self._lock:
            self._load_all()
            open_recs = [r for r in self._outbox.values() if r.state not in TERMINAL_STATES]
        updated: list[OrderOutboxRecord] = []
        for rec in open_recs:
            updated.append(self.reconcile_by_client_id(rec.client_order_id, account_id=rec.account_id))
        return updated

    def cancel_order(self, client_order_id: str, *, account_id: str | None = None) -> OrderOutboxRecord:
        with self._lock:
            rec = self._get_record(client_order_id, account_id=account_id, environment='paper_external')
        if not rec.broker_order_id:
            rec = self.reconcile_by_client_id(client_order_id, account_id=account_id)
        if not rec.broker_order_id or rec.state in TERMINAL_STATES:
            return rec
        headers = self._headers()
        url = f'{self.ALLOWED_HOST}/v2/orders/{rec.broker_order_id}'
        own = self._client is None
        http = self._client or httpx.Client(timeout=10.0)
        try:
            resp = http.delete(url, headers=headers)
            if resp.status_code in (200, 204):
                rec.state = 'CANCEL_PENDING'
                rec.updated_at = datetime.now(timezone.utc)
                with self._lock:
                    self._persist(rec)
            return rec
        finally:
            if own:
                http.close()

    def outbox_snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            self._load_all()
            return [r.model_dump(mode='json') for r in self._outbox.values()]

    def fetch_account_snapshot(self, *, client: httpx.Client | None = None) -> AccountSnapshot:
        """Fetch authoritative Alpaca paper account, positions, and open orders; fail closed on any error."""
        headers = self._headers()
        http = client or self._client or httpx.Client(timeout=10.0)
        own = client is None and self._client is None
        try:
            try:
                acc_resp = http.get(f'{self.ALLOWED_HOST}/v2/account', headers=headers)
                pos_resp = http.get(f'{self.ALLOWED_HOST}/v2/positions', headers=headers)
                ord_resp = http.get(f'{self.ALLOWED_HOST}/v2/orders', headers=headers, params={'status': 'open'})
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise ValueError(f'EXTERNAL_ACCOUNT_UNAVAILABLE: {type(exc).__name__}') from exc

            for label, r in [('account', acc_resp), ('positions', pos_resp), ('orders', ord_resp)]:
                if r.status_code >= 400:
                    raise ValueError(f'EXTERNAL_ACCOUNT_UNAVAILABLE: {label} HTTP {r.status_code}')

            acc = acc_resp.json() or {}
            pos_list = pos_resp.json() or []
            ord_list = ord_resp.json() or []

            account_id = str(acc.get('id') or acc.get('account_number') or '').strip()
            status = str(acc.get('status') or '').upper()
            if not account_id:
                raise ValueError('EXTERNAL_ACCOUNT_UNAVAILABLE: missing broker account_id')

            cash = float(acc.get('cash', 'nan'))
            equity = float(acc.get('equity', 'nan'))
            buying_power = float(acc.get('buying_power', cash))
            if not (math.isfinite(cash) and math.isfinite(equity) and math.isfinite(buying_power)) or equity <= 0 or cash < 0:
                raise ValueError('EXTERNAL_ACCOUNT_UNAVAILABLE: invalid account balances')

            positions: dict[str, float] = {}
            for p in pos_list:
                sym = str(p.get('symbol') or '').upper()
                qty = float(p.get('qty', 'nan'))
                if not sym or not math.isfinite(qty):
                    raise ValueError('EXTERNAL_ACCOUNT_UNAVAILABLE: invalid position entry')
                positions[sym] = qty

            integrity_ok = status == 'ACTIVE' and not bool(acc.get('trading_blocked', False))
            with self._lock:
                if self._store is not None:
                    with self._store.tx():
                        rev = int(self._store.get(f'acct_rev:{account_id}', 0)) + 1
                        self._store.put(f'acct_rev:{account_id}', rev)
                else:
                    rev = 1

            return AccountSnapshot(
                account_id=account_id,
                environment='paper_external',
                status=status,
                currency=str(acc.get('currency') or 'USD'),
                cash=cash,
                equity=equity,
                buying_power=buying_power,
                positions=positions,
                open_orders_count=len(ord_list),
                account_revision=rev,
                reconciled_at=datetime.now(timezone.utc),
                integrity_ok=integrity_ok,
            )
        finally:
            if own:
                http.close()
