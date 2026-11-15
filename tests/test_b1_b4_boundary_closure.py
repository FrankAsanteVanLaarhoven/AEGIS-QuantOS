from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import httpx
import pytest
from fastapi.testclient import TestClient

from aegis.api import create_app
from aegis.llm import BudgetLedger, OpenRouterError, OpenRouterGateway
from aegis.market.contracts import MarketEvent
from aegis.risk.policy import OrderIntent
from aegis.secrets import SecretResolver
from aegis.settings import AegisSettings
from aegis.store import Store


def _mock_broker(
    *,
    positions: list[dict] | None = None,
    equity: str = '100000',
    last_equity: str | None = '100000',
    cash: str = '100000',
    trading_blocked: bool = False,
):
    calls: dict[str, list] = {'post_orders': [], 'cancel_orders': []}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == 'GET' and path == '/v2/account':
            acct = {
                'id': 'acct-b1-1',
                'status': 'ACTIVE',
                'trading_blocked': trading_blocked,
                'currency': 'USD',
                'cash': cash,
                'equity': equity,
                'buying_power': cash,
            }
            if last_equity is not None:
                acct['last_equity'] = last_equity
            return httpx.Response(200, json=acct)
        if req.method == 'GET' and path == '/v2/positions':
            return httpx.Response(200, json=positions or [])
        if req.method == 'GET' and path == '/v2/orders':
            return httpx.Response(200, json=[])
        if req.method == 'POST' and path == '/v2/orders':
            payload = json.loads(req.content)
            calls['post_orders'].append(payload)
            return httpx.Response(200, json={'id': f"brk-{len(calls['post_orders'])}", 'status': 'accepted'})
        if req.method == 'DELETE' and path.startswith('/v2/orders/'):
            calls['cancel_orders'].append(path)
            return httpx.Response(204)
        return httpx.Response(404)

    return httpx.MockTransport(handler), calls


def _seed_live_bars(app, *, symbol: str = 'NVDA', count: int = 65, feed: str = 'iex', timeframe: str = '1Min') -> datetime:
    now = datetime.now(timezone.utc)
    for i in range(count):
        t = now - timedelta(minutes=count - i)
        ev = MarketEvent(
            event_id=f'ev-{symbol}-{i}',
            instrument_id=f'US_EQUITY:{symbol}',
            symbol=symbol,
            venue='IEX',
            event_type='bar',
            source=f'alpaca_{feed}',
            provider='alpaca',
            feed=feed,
            source_mode='live',
            timeframe=timeframe,
            provider_ts=t,
            receive_ts=t + timedelta(minutes=1),
            available_at=t + timedelta(minutes=1),
            open=120.0 + i * 0.1,
            high=120.5 + i * 0.1,
            low=119.5 + i * 0.1,
            close=120.2 + i * 0.1,
            volume=1000 + i,
            raw_payload_hash=f'{i:032x}',
        )
        app.state.market_read_model.ingest_event(ev, now=now + timedelta(seconds=10))
        app.state.market_supervisor.push_event(ev)
    tick = MarketEvent(
        event_id=f'ev-{symbol}-tick',
        instrument_id=f'US_EQUITY:{symbol}',
        symbol=symbol,
        venue='IEX',
        event_type='trade',
        source=f'alpaca_{feed}',
        provider='alpaca',
        feed=feed,
        source_mode='live',
        timeframe=timeframe,
        provider_ts=now,
        receive_ts=now,
        available_at=now,
        price=120.2 + (count - 1) * 0.1,
        quantity=10.0,
        raw_payload_hash='t' * 32,
    )
    app.state.market_read_model.ingest_event(tick, now=now + timedelta(seconds=10))
    app.state.market_supervisor.push_event(tick)
    return now


def _login(client: TestClient, token: str = 'tok-b') -> dict[str, str]:
    r = client.post('/api/login', json={'token': token})
    assert r.status_code == 200
    return {'X-Aegis-CSRF': r.json()['csrf']}


def test_b1_unified_halt_loss_state_dispatch_fence_and_restart(tmp_path: Path, monkeypatch):
    import aegis.market.supervisor as sup_mod
    monkeypatch.setattr(sup_mod, 'market_session_state', lambda _now=None: 'OPEN')
    monkeypatch.setenv('AEGIS_EXECUTION_MODE', 'paper_external')
    monkeypatch.setenv('AEGIS_DATA_MODE', 'live')
    monkeypatch.setenv('ALPACA_API_KEY', 'test_alp_key_1234')
    monkeypatch.setenv('ALPACA_SECRET_KEY', 'test_alp_sec_1234')
    db = tmp_path / 'b1.sqlite'

    transport, calls = _mock_broker(positions=[{'symbol': 'NVDA', 'qty': '5'}])
    app = create_app(str(db), token='tok-b')
    app.state.paper_oms._client = httpx.Client(transport=transport)
    app.state.market_supervisor.mark_authenticated()
    _seed_live_bars(app, count=65)

    client = TestClient(app)
    hdrs = _login(client)

    # 1. Halt via /api/risk/halt -> blocks BUY with 0 outbound submissions
    rh = client.post('/api/risk/halt', json={'enabled': True}, headers=hdrs)
    assert rh.status_code == 200
    assert rh.json()['control_state']['halted'] is True

    rb = client.post('/api/orders', json={'symbol': 'NVDA', 'side': 'BUY', 'qty': 1, 'client_id': 'b1-buy-halted'}, headers=hdrs)
    assert rb.status_code == 400
    assert 'EMERGENCY_HALT_ACTIVE' in rb.text
    assert len(calls['post_orders']) == 0

    # 2. Risk-reducing SELL during active halt IS allowed (backed by acct.positions)
    rs = client.post('/api/orders', json={'symbol': 'NVDA', 'side': 'SELL', 'qty': 2, 'client_id': 'b1-sell-reduce'}, headers=hdrs)
    assert rs.status_code == 200
    assert len(calls['post_orders']) == 1

    # 3. Cancel in paper_external routes to paper_oms
    rc = client.post('/api/orders/b1-sell-reduce/cancel', headers=hdrs)
    assert rc.status_code == 200
    assert len(calls['cancel_orders']) == 1

    # 4. Restart with active halt -> halt persists in SQLite
    app2 = create_app(str(db), token='tok-b')
    app2.state.paper_oms._client = httpx.Client(transport=transport)
    app2.state.market_supervisor.mark_authenticated()
    _seed_live_bars(app2, count=65)
    client2 = TestClient(app2)
    hdrs2 = _login(client2)
    rb2 = client2.post('/api/orders', json={'symbol': 'NVDA', 'side': 'BUY', 'qty': 1, 'client_id': 'b1-buy-restart'}, headers=hdrs2)
    assert rb2.status_code == 400
    assert 'EMERGENCY_HALT_ACTIVE' in rb2.text
    assert len(calls['post_orders']) == 1

    # 5. Safe resume + policy update -> normal BUY allowed
    assert client2.post('/api/risk/halt', json={'enabled': False}, headers=hdrs2).status_code == 200
    rp = client2.post('/api/risk/policy', json={'max_order_notional_pct': 0.2}, headers=hdrs2)
    assert rp.status_code == 200
    assert rp.json()['control_state']['profile_revision'] >= 2
    rb3 = client2.post('/api/orders', json={'symbol': 'NVDA', 'side': 'BUY', 'qty': 1, 'client_id': 'b1-buy-ok'}, headers=hdrs2)
    assert rb3.status_code == 200
    assert len(calls['post_orders']) == 2

    # 6. Dispatch fence: halt between evaluate() and submit_order() blocks outbound POST
    now = datetime.now(timezone.utc)
    intent = OrderIntent(
        account_id='acct-b1-1',
        environment='paper_external',
        symbol='NVDA',
        side='BUY',
        qty=1.0,
        client_order_id='b1-fence-test',
        expires_at=now + timedelta(minutes=5),
        data_age_seconds=1.0,
        policy_version=app2.state.risk_gate.profile.profile_id,
    )
    decision = app2.state.risk_gate.evaluate(
        intent,
        price=125.0,
        equity=100000.0,
        cash=100000.0,
        positions={'NVDA': 5.0},
        marks={'NVDA': 125.0},
        daily_pnl_pct=0.0,
        total_drawdown_pct=0.0,
        account_revision=1,
        now=now,
    )
    assert decision.approved
    app2.state.risk_gate.set_halt(True, reason='CONCURRENT_HALT')
    with pytest.raises(ValueError, match='DISPATCH_FENCED: EMERGENCY_HALT_ACTIVE'):
        app2.state.paper_oms.submit_order(intent, decision)
    assert len(calls['post_orders']) == 2

    # 7. Missing last_equity -> MISSING_ACCOUNT_LOSS_STATE -> 0 submissions
    assert client2.post('/api/risk/halt', json={'enabled': False}, headers=hdrs2).status_code == 200
    t_noloss, calls_noloss = _mock_broker(last_equity=None)
    app2.state.paper_oms._client = httpx.Client(transport=t_noloss)
    r_noloss = client2.post('/api/orders', json={'symbol': 'NVDA', 'side': 'BUY', 'qty': 1, 'client_id': 'b1-noloss'}, headers=hdrs2)
    assert r_noloss.status_code == 400
    assert 'MISSING_ACCOUNT_LOSS_STATE' in r_noloss.text
    assert len(calls_noloss['post_orders']) == 0


def test_b2_rejects_unsupported_protective_and_local_limit_orders(tmp_path: Path, monkeypatch):
    import aegis.market.supervisor as sup_mod
    monkeypatch.setattr(sup_mod, 'market_session_state', lambda _now=None: 'OPEN')
    monkeypatch.setenv('AEGIS_EXECUTION_MODE', 'paper_external')
    monkeypatch.setenv('AEGIS_DATA_MODE', 'live')
    monkeypatch.setenv('ALPACA_API_KEY', 'test_alp_key_1234')
    monkeypatch.setenv('ALPACA_SECRET_KEY', 'test_alp_sec_1234')

    transport, calls = _mock_broker()
    app = create_app(str(tmp_path / 'b2.sqlite'), token='tok-b')
    app.state.paper_oms._client = httpx.Client(transport=transport)
    app.state.market_supervisor.mark_authenticated()
    _seed_live_bars(app, count=65)
    client = TestClient(app)
    hdrs = _login(client)

    # stop=90, target=120 must be explicitly rejected with 0 submissions
    r_prot = client.post('/api/orders', json={
        'symbol': 'NVDA', 'side': 'BUY', 'qty': 1, 'client_id': 'b2-prot', 'stop': 90.0, 'target': 120.0,
    }, headers=hdrs)
    assert r_prot.status_code == 400
    assert 'UNSUPPORTED_PROTECTIVE_PARAMETERS' in r_prot.text
    assert len(calls['post_orders']) == 0

    # Supported limit order preserves order_type and limit_price
    r_lim = client.post('/api/orders', json={
        'symbol': 'NVDA', 'side': 'BUY', 'qty': 1, 'client_id': 'b2-lim', 'order_type': 'limit', 'limit_price': 124.5,
    }, headers=hdrs)
    assert r_lim.status_code == 200
    assert calls['post_orders'][0]['type'] == 'limit'
    assert calls['post_orders'][0]['limit_price'] == '124.5'

    # Local mode must not silently strip order_type='limit' / limit_price
    monkeypatch.setenv('AEGIS_EXECUTION_MODE', 'paper_local')
    monkeypatch.setenv('AEGIS_DATA_MODE', 'synthetic')
    app_loc = create_app(str(tmp_path / 'b2_loc.sqlite'), token='tok-b')
    c_loc = TestClient(app_loc)
    h_loc = _login(c_loc)
    r_loc_lim = c_loc.post('/api/orders', json={
        'symbol': 'NVDA', 'side': 'BUY', 'qty': 1, 'client_id': 'b2-loc-lim', 'order_type': 'limit', 'limit_price': 100.0,
    }, headers=h_loc)
    assert r_loc_lim.status_code == 400
    assert 'UNSUPPORTED_ORDER_TYPE' in r_loc_lim.text


def test_b3_live_forecast_boundaries_timestamps_and_positive_path(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('AEGIS_DATA_MODE', 'live')
    monkeypatch.setenv('AEGIS_EXECUTION_MODE', 'paper_local')
    app = create_app(str(tmp_path / 'b3.sqlite'), token='tok-b')
    client = TestClient(app)
    hdrs = _login(client)

    # Future timestamp beyond 5s skew rejected
    future_t = datetime.now(timezone.utc) + timedelta(minutes=5)
    with pytest.raises(ValueError, match='FUTURE_TIMESTAMP_REJECTED'):
        app.state.market_read_model.ingest_event(MarketEvent(
            event_id='fut-1', instrument_id='US_EQUITY:NVDA', symbol='NVDA', venue='IEX',
            event_type='bar', source='alpaca_iex', provider='alpaca', feed='iex', source_mode='live',
            provider_ts=future_t, receive_ts=datetime.now(timezone.utc), available_at=future_t,
            open=100.0, high=101.0, low=99.0, close=100.5, volume=100, raw_payload_hash='f' * 32,
        ))

    # 23, 24, 64 bars -> INSUFFICIENT_LIVE_BARS
    for n in (23, 1, 40):  # cumulative: 23, 24, 64
        app_sub = create_app(str(tmp_path / f'b3_{n}.sqlite'), token='tok-b')
        c_sub = TestClient(app_sub)
        h_sub = _login(c_sub)
        cum = 23 if n == 23 else (24 if n == 1 else 64)
        _seed_live_bars(app_sub, count=cum)
        rf = c_sub.post('/api/forecasts', json={'symbol': 'NVDA', 'horizon': 4, 'samples': 2, 'seed': 26}, headers=h_sub)
        assert rf.status_code == 400
        assert 'INSUFFICIENT_LIVE_BARS' in rf.text

    # 65 bars -> positive path reaches kronos.forecast cleanly with 1Min provenance
    import aegis.kronos_adapter as kronos
    _seed_live_bars(app, count=65, timeframe='1Min')
    rf_ok = client.post('/api/forecasts', json={'symbol': 'NVDA', 'horizon': 3, 'samples': 2, 'seed': 26}, headers=hdrs)
    if kronos.status().get('state') == 'READY':
        assert rf_ok.status_code == 200
        body = rf_ok.json()
        assert body['status'] == 'REAL_PRETRAINED_KRONOS'
        assert body['timeframe'] == '1Min'
        assert body['source_mode'] == 'live'
        assert len(body['input_bar_hashes']) == 10
        assert len(body['bands']) == 3
    else:
        assert rf_ok.status_code == 503
        assert rf_ok.json().get('state') == 'UNAVAILABLE'


def test_b4_cost_qualification_unresolved_usage_and_legacy_migration(tmp_path: Path):
    now_iso = datetime.now(timezone.utc).isoformat()
    lock = tmp_path / 'lock.json'
    lock.write_text(json.dumps({
        'schema_version': 1,
        'roles': {'filing_extractor': {
            'model_id': 'provider/model-test',
            'approved_fallbacks': [],
            'context_length': 4096,
            'pricing': {'prompt': '0.000002', 'completion': '0.000006', 'updated_at': now_iso},
        }},
    }), encoding='utf-8')
    settings = AegisSettings.from_env({'AEGIS_ENABLE_PAID_LLM': 'true', 'AEGIS_OPENROUTER_MODELS_LOCK': str(lock)})
    resolver = SecretResolver({'OPENROUTER_API_KEY': 'sk-or-v1-test-key'})

    # Missing usage.cost -> UNRESOLVED, never settled as observed
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            'id': 'gen-unresolved-123',
            'model': 'provider/model-test',
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5},
            'choices': [{'message': {'content': json.dumps({
                'symbol': 'NVDA', 'period': '2026Q3', 'claims': ['ok'],
                'cited_evidence_ids': ['sec:1'], 'abstain': False, 'abstain_reason': None,
            })}}],
        })

    store = Store(str(tmp_path / 'b4.sqlite'))
    ledger = BudgetLedger(job_budget_usd=1.0, daily_budget_usd=1.0, project_budget_usd=1.0, store=store)
    gw = OpenRouterGateway(settings, resolver, budget=ledger, client=httpx.Client(transport=httpx.MockTransport(handler)))

    res = gw.complete_structured(job_id='j_unres', role='filing_extractor', messages=[{'role': 'user', 'content': 'Extract'}])
    assert res['cost_status'] == 'UNRESOLVED'
    assert res['cost_usd'] is None
    assert res['generation_id'] == 'gen-unresolved-123'
    assert ledger.snapshot()['unknown_outcomes'] == 1

    # Reconcile later with observed cost -> settles exactly once
    settled = ledger.reconcile(res['reservation_id'], actual_usd=0.0005)
    assert settled.status == 'SETTLED'
    assert ledger.snapshot()['unknown_outcomes'] == 0
    assert ledger.snapshot()['project_spent_usd'] == pytest.approx(0.0005)

    # Non-text/multimodal content -> BLOCKED_INPUT_UNBOUNDED
    with pytest.raises(OpenRouterError) as exc_mm:
        gw.complete_structured(job_id='j_mm', role='filing_extractor', messages=[{'role': 'user', 'content': [{'type': 'image_url'}]}])  # type: ignore[list-item]
    assert exc_mm.value.code == 'BLOCKED_INPUT_UNBOUNDED'

    # Legacy kv budget migration preserves prior spending
    store_mig = Store(str(tmp_path / 'b4_mig.sqlite'))
    with store_mig.tx():
        store_mig.put('llm_budget_v1', {'project_spent_usd': 0.42, 'daily_spent': {'2026-11-15': 0.42}})
    mig_ledger = BudgetLedger(job_budget_usd=1.0, daily_budget_usd=1.0, project_budget_usd=1.0, store=store_mig)
    assert mig_ledger.snapshot()['project_spent_usd'] == pytest.approx(0.42)
