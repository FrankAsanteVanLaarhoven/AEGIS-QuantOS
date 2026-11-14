"""External paper-broker OMS adapter (Alpaca Paper ONLY) with durable outbox and reconciliation."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict

from aegis.risk.policy import OrderIntent, RiskDecision
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


class OrderOutboxRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    client_order_id: str
    intent_hash: str
    symbol: str
    side: Literal['BUY', 'SELL']
    qty: float
    environment: Literal['paper_local', 'paper_external']
    state: OrderState = 'PENDING_SUBMIT'
    broker_order_id: str | None = None
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    updated_at: datetime
    last_error: str | None = None


class AlpacaPaperOMS:
    """Strictly paper-only external OMS with idempotent client_order_id and restart reconciliation."""

    ALLOWED_HOST = 'https://paper-api.alpaca.markets'

    def __init__(self, settings: AegisSettings, resolver: SecretResolver, *, client: httpx.Client | None = None) -> None:
        if settings.alpaca_paper_base_url.rstrip('/') != self.ALLOWED_HOST:
            raise ValueError('Only Alpaca paper-api host is permitted')
        self.settings = settings
        self.resolver = resolver
        self._client = client
        self._outbox: dict[str, OrderOutboxRecord] = {}

    def _headers(self) -> dict[str, str]:
        k = self.resolver.resolve('ALPACA_API_KEY')
        s = self.resolver.resolve('ALPACA_SECRET_KEY')
        if not k or not s:
            raise ValueError('BLOCKED_CREDENTIAL: Alpaca paper credentials missing')
        return {'APCA-API-KEY-ID': k, 'APCA-API-SECRET-KEY': s, 'Content-Type': 'application/json'}

    def submit_order(self, intent: OrderIntent, decision: RiskDecision) -> OrderOutboxRecord:
        if not decision.approved or decision.intent_hash != intent.intent_hash():
            raise ValueError('OrderIntent lacks valid matching RiskDecision')
        if intent.environment != 'paper_external':
            raise ValueError('AlpacaPaperOMS only accepts paper_external intents')

        # Idempotent: if already persisted in outbox, return existing record rather than duplicating POST
        if intent.client_order_id in self._outbox:
            return self._outbox[intent.client_order_id]

        now = datetime.now(timezone.utc)
        rec = OrderOutboxRecord(
            client_order_id=intent.client_order_id,
            intent_hash=intent.intent_hash(),
            symbol=intent.symbol.upper(),
            side=intent.side,
            qty=intent.qty,
            environment='paper_external',
            state='PENDING_SUBMIT',
            updated_at=now,
        )
        # Persist intent BEFORE remote send
        self._outbox[intent.client_order_id] = rec

        headers = self._headers()
        payload = {
            'symbol': rec.symbol,
            'qty': str(rec.qty),
            'side': rec.side.lower(),
            'type': intent.order_type,
            'time_in_force': intent.time_in_force,
            'client_order_id': rec.client_order_id,
        }
        url = f'{self.ALLOWED_HOST}/v2/orders'
        own = self._client is None
        http = self._client or httpx.Client(timeout=10.0)
        try:
            try:
                resp = http.post(url, headers=headers, json=payload)
            except httpx.TimeoutException:
                # Ambiguous network outcome stays UNKNOWN until reconciled by client_order_id
                rec.state = 'UNKNOWN'
                rec.last_error = 'Timeout after POST; awaiting client_order_id reconciliation'
                rec.updated_at = datetime.now(timezone.utc)
                return rec
            if resp.status_code >= 400:
                rec.state = 'REJECTED'
                rec.last_error = f'HTTP {resp.status_code}'
                rec.updated_at = datetime.now(timezone.utc)
                return rec
            body = resp.json()
            rec.broker_order_id = body.get('id')
            rec.state = 'ACCEPTED' if body.get('status') in ('new', 'accepted', 'pending_new') else 'SUBMITTED'
            rec.updated_at = datetime.now(timezone.utc)
            return rec
        finally:
            if own:
                http.close()

    def reconcile_by_client_id(self, client_order_id: str) -> OrderOutboxRecord:
        rec = self._outbox[client_order_id]
        headers = self._headers()
        url = f'{self.ALLOWED_HOST}/v2/orders:by_client_order_id'
        own = self._client is None
        http = self._client or httpx.Client(timeout=10.0)
        try:
            resp = http.get(url, headers=headers, params={'client_order_id': client_order_id})
            if resp.status_code == 404:
                rec.state = 'REJECTED'
                rec.last_error = 'Not found at broker during reconciliation'
                rec.updated_at = datetime.now(timezone.utc)
                return rec
            resp.raise_for_status()
            body = resp.json()
            st_map = {
                'new': 'ACCEPTED',
                'accepted': 'ACCEPTED',
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
            rec.updated_at = datetime.now(timezone.utc)
            return rec
        finally:
            if own:
                http.close()

    def cancel_order(self, client_order_id: str) -> OrderOutboxRecord:
        rec = self._outbox[client_order_id]
        if not rec.broker_order_id:
            rec = self.reconcile_by_client_id(client_order_id)
        if not rec.broker_order_id:
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
            return rec
        finally:
            if own:
                http.close()

    def outbox_snapshot(self) -> list[dict[str, Any]]:
        return [r.model_dump(mode='json') for r in self._outbox.values()]
