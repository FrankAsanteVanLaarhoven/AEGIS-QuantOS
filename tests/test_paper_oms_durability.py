from __future__ import annotations

from datetime import datetime, timedelta, timezone
import httpx
import pytest

from aegis.brokers.alpaca_paper import AlpacaPaperOMS
from aegis.risk import IndependentRiskGate, OrderIntent
from aegis.secrets import SecretResolver
from aegis.settings import AegisSettings
from aegis.store import Store


def _creds() -> tuple[AegisSettings, SecretResolver]:
    settings = AegisSettings.from_env({'AEGIS_EXECUTION_MODE': 'paper_external'})
    resolver = SecretResolver({'ALPACA_API_KEY': 'test_alp_key_1234', 'ALPACA_SECRET_KEY': 'test_alp_sec_1234'})
    return settings, resolver


def _intent(
    cid: str = 'cid-1',
    *,
    account_id: str = 'acct-a',
    symbol: str = 'NVDA',
    side: str = 'BUY',
    qty: float = 2.0,
    order_type: str = 'market',
    limit_price: float | None = None,
) -> OrderIntent:
    now = datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc)
    return OrderIntent(
        account_id=account_id,
        environment='paper_external',
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        qty=qty,
        order_type=order_type,  # type: ignore[arg-type]
        limit_price=limit_price,
        client_order_id=cid,
        expires_at=now + timedelta(minutes=5),
        data_age_seconds=0.5,
    )


def test_oms_persists_across_restart_and_reconciles_open_orders(tmp_path):
    store = Store(tmp_path / 'oms.sqlite')
    settings, resolver = _creds()
    gate = IndependentRiskGate(store=store)
    it = _intent('restart-1')
    dec = gate.evaluate(it, price=100.0, equity=10000.0, cash=10000.0, positions={}, now=it.expires_at - timedelta(minutes=4))
    assert dec.approved

    def timeout_post(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout('dropped')

    oms1 = AlpacaPaperOMS(settings, resolver, client=httpx.Client(transport=httpx.MockTransport(timeout_post)), store=store, risk_gate=gate)
    rec = oms1.submit_order(it, dec)
    assert rec.state == 'UNKNOWN'

    # Simulate process restart with new Store/Gate/OMS instances
    store2 = Store(tmp_path / 'oms.sqlite')
    gate2 = IndependentRiskGate(store=store2)

    def filled_get(req: httpx.Request) -> httpx.Response:
        assert req.method == 'GET'
        return httpx.Response(200, json={'id': 'brk-101', 'status': 'filled', 'filled_qty': '2', 'filled_avg_price': '100.25'})

    oms2 = AlpacaPaperOMS(settings, resolver, client=httpx.Client(transport=httpx.MockTransport(filled_get)), store=store2, risk_gate=gate2)
    snap = oms2.outbox_snapshot()
    assert len(snap) == 1
    assert snap[0]['client_order_id'] == 'restart-1'

    # Reservation still active before reconcile; released after reconcile to FILLED
    reconciled = oms2.reconcile_open_orders()
    assert len(reconciled) == 1
    assert reconciled[0].state == 'FILLED'
    assert reconciled[0].filled_avg_price == 100.25

    # After terminal fill, same account reservation is released
    it2 = _intent('restart-2', qty=10.0)
    dec2 = gate2.evaluate(it2, price=100.0, equity=10000.0, cash=1000.0, positions={}, now=it.expires_at - timedelta(minutes=4))
    assert dec2.approved


@pytest.mark.parametrize('status_code,expected_state', [
    (500, 'UNKNOWN'),
    (502, 'UNKNOWN'),
    (503, 'UNKNOWN'),
    (400, 'REJECTED'),
    (422, 'REJECTED'),
])
def test_oms_http_status_classification(tmp_path, status_code: int, expected_state: str):
    store = Store(tmp_path / f'oms_{status_code}.sqlite')
    settings, resolver = _creds()
    gate = IndependentRiskGate(store=store)
    it = _intent(f'cid-{status_code}')
    dec = gate.evaluate(it, price=50.0, equity=10000.0, cash=10000.0, positions={}, now=it.expires_at - timedelta(minutes=4))
    oms = AlpacaPaperOMS(
        settings, resolver,
        client=httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(status_code, json={'message': 'err'}))),
        store=store, risk_gate=gate,
    )
    rec = oms.submit_order(it, dec)
    assert rec.state == expected_state


def test_oms_reconcile_404_stays_unknown_and_prevents_resubmit(tmp_path):
    store = Store(tmp_path / 'oms_404.sqlite')
    settings, resolver = _creds()
    gate = IndependentRiskGate(store=store)
    it = _intent('cid-404')
    dec = gate.evaluate(it, price=50.0, equity=10000.0, cash=10000.0, positions={}, now=it.expires_at - timedelta(minutes=4))

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == 'POST':
            raise httpx.ConnectTimeout('lost')
        return httpx.Response(404, json={'message': 'not found'})

    oms = AlpacaPaperOMS(settings, resolver, client=httpx.Client(transport=httpx.MockTransport(handler)), store=store, risk_gate=gate)
    assert oms.submit_order(it, dec).state == 'UNKNOWN'
    assert oms.reconcile_by_client_id('cid-404', account_id='acct-a').state == 'UNKNOWN'
    # Re-submitting same client_order_id returns existing UNKNOWN record without hitting broker
    assert oms.submit_order(it, dec).state == 'UNKNOWN'


def test_oms_idempotency_conflict_and_multi_account_isolation(tmp_path):
    store = Store(tmp_path / 'oms_multi.sqlite')
    settings, resolver = _creds()
    gate = IndependentRiskGate(store=store)
    now = datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc)

    it_a = _intent('shared-cid', account_id='acct-a', qty=1.0)
    it_b = _intent('shared-cid', account_id='acct-b', qty=1.0)
    it_a_diff = _intent('shared-cid', account_id='acct-a', qty=2.0)

    dec_a = gate.evaluate(it_a, price=50.0, equity=10000.0, cash=10000.0, positions={}, now=now)
    dec_b = gate.evaluate(it_b, price=50.0, equity=10000.0, cash=10000.0, positions={}, now=now)
    dec_a_diff = gate.evaluate(it_a_diff, price=50.0, equity=10000.0, cash=10000.0, positions={}, now=now)
    assert dec_a.approved and dec_b.approved and not dec_a_diff.approved

    oms = AlpacaPaperOMS(
        settings, resolver,
        client=httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={'id': 'b1', 'status': 'accepted'}))),
        store=store, risk_gate=gate,
    )
    assert oms.submit_order(it_a, dec_a).account_id == 'acct-a'
    assert oms.submit_order(it_b, dec_b).account_id == 'acct-b'

    # Different intent with same (account_id, client_order_id) raises conflict
    fake_dec = dec_a.model_copy(update={'intent_hash': it_a_diff.intent_hash()})
    with pytest.raises(ValueError, match='IDEMPOTENCY_CONFLICT'):
        oms.submit_order(it_a_diff, fake_dec)


def test_oms_limit_order_includes_limit_price_and_cancel_persists(tmp_path):
    store = Store(tmp_path / 'oms_limit.sqlite')
    settings, resolver = _creds()
    gate = IndependentRiskGate(store=store)
    it = _intent('limit-1', order_type='limit', limit_price=123.45)
    dec = gate.evaluate(it, price=123.45, equity=10000.0, cash=10000.0, positions={}, now=it.expires_at - timedelta(minutes=4))
    seen_payloads: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == 'POST':
            import json
            seen_payloads.append(json.loads(req.content.decode('utf-8')))
            return httpx.Response(200, json={'id': 'brk-lim-1', 'status': 'accepted'})
        if req.method == 'DELETE':
            return httpx.Response(204)
        return httpx.Response(200, json={'id': 'brk-lim-1', 'status': 'canceled', 'filled_qty': '0'})

    oms = AlpacaPaperOMS(settings, resolver, client=httpx.Client(transport=httpx.MockTransport(handler)), store=store, risk_gate=gate)
    rec = oms.submit_order(it, dec)
    assert rec.state == 'ACCEPTED'
    assert seen_payloads[0]['limit_price'] == '123.45'

    canceled = oms.cancel_order('limit-1', account_id='acct-a')
    assert canceled.state == 'CANCEL_PENDING'

    # Restart and verify CANCEL_PENDING persisted, then reconcile to CANCELLED
    oms2 = AlpacaPaperOMS(settings, resolver, client=httpx.Client(transport=httpx.MockTransport(handler)), store=Store(tmp_path / 'oms_limit.sqlite'), risk_gate=gate)
    assert oms2.outbox_snapshot()[0]['state'] == 'CANCEL_PENDING'
    assert oms2.reconcile_by_client_id('limit-1', account_id='acct-a').state == 'CANCELLED'


@pytest.mark.parametrize('broker_status,expected', [
    ('new', 'ACCEPTED'),
    ('accepted', 'ACCEPTED'),
    ('pending_new', 'ACCEPTED'),
    ('partially_filled', 'PARTIALLY_FILLED'),
    ('filled', 'FILLED'),
    ('pending_cancel', 'CANCEL_PENDING'),
    ('canceled', 'CANCELLED'),
    ('cancelled', 'CANCELLED'),
    ('rejected', 'REJECTED'),
    ('expired', 'EXPIRED'),
])
def test_oms_reconcile_status_mapping(tmp_path, broker_status: str, expected: str):
    store = Store(tmp_path / f'oms_map_{broker_status}.sqlite')
    settings, resolver = _creds()
    gate = IndependentRiskGate(store=store)
    it = _intent(f'map-{broker_status}')
    dec = gate.evaluate(it, price=10.0, equity=10000.0, cash=10000.0, positions={}, now=it.expires_at - timedelta(minutes=4))

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == 'POST':
            return httpx.Response(200, json={'id': 'brk-m', 'status': 'new'})
        return httpx.Response(200, json={'id': 'brk-m', 'status': broker_status, 'filled_qty': '1'})

    oms = AlpacaPaperOMS(settings, resolver, client=httpx.Client(transport=httpx.MockTransport(handler)), store=store, risk_gate=gate)
    oms.submit_order(it, dec)
    rec = oms.reconcile_by_client_id(f'map-{broker_status}', account_id='acct-a')
    assert rec.state == expected

