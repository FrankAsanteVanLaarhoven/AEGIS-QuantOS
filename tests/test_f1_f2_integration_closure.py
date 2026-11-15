from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from aegis.api import create_app
from aegis.market.contracts import MarketEvent, payload_sha256

_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


def _authed_headers(client: TestClient, token: str = 'tok-f1') -> dict[str, str]:
    r = client.post('/api/login', json={'token': token})
    assert r.status_code == 200
    return {'X-Aegis-CSRF': r.json()['csrf']}


def _seed_live_bar(app, *, symbol: str = 'NVDA', price: float = 321.45, volume: float = 98765.0, age_seconds: float = 0.5, mark_healthy: bool = True) -> MarketEvent:
    now = datetime.now(timezone.utc)
    pts = now - timedelta(seconds=age_seconds)
    raw = {'S': symbol, 't': pts.isoformat(), 'o': price - 1.0, 'h': price + 1.0, 'l': price - 2.0, 'c': price, 'v': volume}
    ev = MarketEvent(
        event_id=f'ws_live:{symbol}:{pts.isoformat()}',
        instrument_id=f'US_EQUITY:{symbol}',
        symbol=symbol,
        venue='IEX',
        event_type='bar',
        source='alpaca_ws_iex',
        provider='alpaca',
        feed='iex',
        source_mode='live',
        provider_ts=pts,
        receive_ts=now,
        available_at=now,
        open=raw['o'],
        high=raw['h'],
        low=raw['l'],
        close=price,
        volume=volume,
        entitlement_ref='alpaca:iex',
        raw_payload_hash=payload_sha256(raw),
    )
    app.state.market_read_model.ingest_event(ev)
    if mark_healthy:
        app.state.market_supervisor.push_event(ev)
    return ev


def _mock_broker_transport(
    *,
    account_status: str = 'ACTIVE',
    trading_blocked: bool = False,
    cash: str = '50000.00',
    equity: str = '100000.00',
    positions: list[dict] | None = None,
    post_status: int = 200,
    reconcile_status: str = 'filled',
    filled_qty: str = '5',
    account_http_status: int = 200,
):
    calls: dict[str, int] = {'post': 0, 'get_account': 0, 'get_order': 0, 'delete': 0}
    lock = threading.Lock()
    pos_payload = positions if positions is not None else []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == 'GET' and path == '/v2/account':
            with lock:
                calls['get_account'] += 1
            if account_http_status >= 400:
                return httpx.Response(account_http_status, json={'message': 'unavailable'})
            return httpx.Response(200, json={
                'id': 'acct-ext-paper-1',
                'status': account_status,
                'trading_blocked': trading_blocked,
                'currency': 'USD',
                'cash': cash,
                'equity': equity,
                'last_equity': equity,
                'buying_power': cash,
            })
        if req.method == 'GET' and path == '/v2/positions':
            return httpx.Response(200, json=pos_payload)
        if req.method == 'GET' and path == '/v2/orders':
            return httpx.Response(200, json=[])
        if req.method == 'POST' and path == '/v2/orders':
            with lock:
                calls['post'] += 1
            body = json.loads(req.content.decode('utf-8'))
            if post_status >= 400:
                return httpx.Response(post_status, json={'message': 'rejected'})
            return httpx.Response(200, json={'id': 'brk-ext-100', 'client_order_id': body['client_order_id'], 'status': 'accepted'})
        if req.method == 'GET' and path == '/v2/orders:by_client_order_id':
            with lock:
                calls['get_order'] += 1
            return httpx.Response(200, json={
                'id': 'brk-ext-100',
                'status': reconcile_status,
                'filled_qty': filled_qty,
                'filled_avg_price': '321.45',
            })
        if req.method == 'DELETE' and path.startswith('/v2/orders/'):
            with lock:
                calls['delete'] += 1
            return httpx.Response(204)
        return httpx.Response(404)

    return httpx.MockTransport(handler), calls


def test_f1_external_order_positive_lifecycle_idempotency_partial_fill_cancel_restart(tmp_path: Path, monkeypatch):
    import aegis.market.supervisor as sup_mod
    monkeypatch.setattr(sup_mod, 'market_session_state', lambda _now=None: 'OPEN')
    db = tmp_path / 'f1_pos.sqlite'
    monkeypatch.setenv('AEGIS_EXECUTION_MODE', 'paper_external')
    monkeypatch.setenv('AEGIS_DATA_MODE', 'live')
    monkeypatch.setenv('ALPACA_API_KEY', 'test_alp_key_1234')
    monkeypatch.setenv('ALPACA_SECRET_KEY', 'test_alp_sec_1234')

    transport, calls = _mock_broker_transport(
        positions=[{'symbol': 'NVDA', 'qty': '10'}],
        reconcile_status='partially_filled',
        filled_qty='2',
    )
    app = create_app(str(db), token='tok-f1')
    app.state.paper_oms._client = httpx.Client(transport=transport)

    with TestClient(app) as client:
        hdrs = _authed_headers(client)
        _seed_live_bar(app, symbol='NVDA', price=321.45)

        # Positive BUY reaches exactly 1 stub external submit
        r1 = client.post('/api/orders', json={'symbol': 'NVDA', 'side': 'BUY', 'qty': 5, 'client_id': 'ext-buy-1'}, headers=hdrs)
        assert r1.status_code == 200, r1.text
        data1 = r1.json()
        assert data1['mode'] == 'paper_external'
        assert data1['authorization_context']['account']['account_id'] == 'acct-ext-paper-1'
        assert data1['decision']['approved'] is True
        assert data1['order']['state'] == 'ACCEPTED'
        assert calls['post'] == 1

        # Idempotent replay with same client_id & payload -> 0 additional POSTs
        r1_dup = client.post('/api/orders', json={'symbol': 'NVDA', 'side': 'BUY', 'qty': 5, 'client_id': 'ext-buy-1'}, headers=hdrs)
        assert r1_dup.status_code == 200
        assert calls['post'] == 1

        # Different payload with same client_id -> rejected with 0 additional POSTs
        r1_conflict = client.post('/api/orders', json={'symbol': 'NVDA', 'side': 'BUY', 'qty': 6, 'client_id': 'ext-buy-1'}, headers=hdrs)
        assert r1_conflict.status_code == 400
        assert calls['post'] == 1

        # Partial fill reconciliation via API
        r_rec = client.post('/api/broker/paper-orders/ext-buy-1/reconcile', headers=hdrs)
        assert r_rec.status_code == 200
        assert r_rec.json()['state'] == 'PARTIALLY_FILLED'
        assert r_rec.json()['filled_qty'] == 2.0

        # Cancel via API
        r_can = client.post('/api/broker/paper-orders/ext-buy-1/cancel', headers=hdrs)
        assert r_can.status_code == 200
        assert r_can.json()['state'] == 'CANCEL_PENDING'
        assert calls['delete'] == 1

        # Positive SELL against reconciled 10-share broker position
        r_sell = client.post('/api/orders', json={'symbol': 'NVDA', 'side': 'SELL', 'qty': 4, 'client_id': 'ext-sell-1'}, headers=hdrs)
        assert r_sell.status_code == 200
        assert calls['post'] == 2

    # Restart app against same DB and verify outbox persisted
    app2 = create_app(str(db), token='tok-f1')
    with TestClient(app2) as client2:
        _authed_headers(client2)
        outbox = client2.get('/api/broker/paper-orders').json()['outbox']
        assert {o['client_order_id'] for o in outbox} == {'ext-buy-1', 'ext-sell-1'}


def test_f1_negative_cases_cause_zero_broker_submissions(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('AEGIS_EXECUTION_MODE', 'paper_external')
    monkeypatch.setenv('ALPACA_API_KEY', 'test_alp_key_1234')
    monkeypatch.setenv('ALPACA_SECRET_KEY', 'test_alp_sec_1234')

    # 1. Synthetic data mode blocked -> 0 submissions
    monkeypatch.setenv('AEGIS_DATA_MODE', 'synthetic')
    transport, calls = _mock_broker_transport()
    app_syn = create_app(str(tmp_path / 'syn.sqlite'), token='tok-f1')
    app_syn.state.paper_oms._client = httpx.Client(transport=transport)
    with TestClient(app_syn) as c:
        hdrs = _authed_headers(c)
        assert c.post('/api/orders', json={'symbol': 'NVDA', 'side': 'BUY', 'qty': 1, 'client_id': 'neg-syn'}, headers=hdrs).status_code == 400
    assert calls['post'] == 0

    # 2. Missing mark / disconnected feed / stale mark -> 0 submissions
    monkeypatch.setenv('AEGIS_DATA_MODE', 'live')
    transport2, calls2 = _mock_broker_transport()
    app_live = create_app(str(tmp_path / 'live.sqlite'), token='tok-f1')
    app_live.state.paper_oms._client = httpx.Client(transport=transport2)
    with TestClient(app_live) as c:
        hdrs = _authed_headers(c)
        # Disconnected / no marks
        assert c.post('/api/orders', json={'symbol': 'NVDA', 'side': 'BUY', 'qty': 1, 'client_id': 'neg-nomark'}, headers=hdrs).status_code == 400
        # Stale mark (provider_ts 120s old even though receive_ts is now!)
        _seed_live_bar(app_live, symbol='NVDA', price=100.0, age_seconds=120.0, mark_healthy=True)
        assert c.post('/api/orders', json={'symbol': 'NVDA', 'side': 'BUY', 'qty': 1, 'client_id': 'neg-stale'}, headers=hdrs).status_code == 400
        # Fresh mark for NVDA, but feed marked disconnected
        _seed_live_bar(app_live, symbol='NVDA', price=100.0, age_seconds=0.5, mark_healthy=True)
        app_live.state.market_supervisor.mark_disconnected('feed stopped')
        assert c.post('/api/orders', json={'symbol': 'NVDA', 'side': 'BUY', 'qty': 1, 'client_id': 'neg-disc'}, headers=hdrs).status_code == 400
    assert calls2['post'] == 0

    # 3. Broker trading_blocked=True or 503 account unavailable -> 0 submissions
    transport_blk, calls_blk = _mock_broker_transport(trading_blocked=True)
    app_blk = create_app(str(tmp_path / 'blk.sqlite'), token='tok-f1')
    app_blk.state.paper_oms._client = httpx.Client(transport=transport_blk)
    with TestClient(app_blk) as c:
        hdrs = _authed_headers(c)
        _seed_live_bar(app_blk, symbol='NVDA', price=100.0)
        assert c.post('/api/orders', json={'symbol': 'NVDA', 'side': 'BUY', 'qty': 1, 'client_id': 'neg-blk'}, headers=hdrs).status_code == 400
    assert calls_blk['post'] == 0

    transport_503, calls_503 = _mock_broker_transport(account_http_status=503)
    app_503 = create_app(str(tmp_path / '503.sqlite'), token='tok-f1')
    app_503.state.paper_oms._client = httpx.Client(transport=transport_503)
    with TestClient(app_503) as c:
        hdrs = _authed_headers(c)
        _seed_live_bar(app_503, symbol='NVDA', price=100.0)
        assert c.post('/api/orders', json={'symbol': 'NVDA', 'side': 'BUY', 'qty': 1, 'client_id': 'neg-503'}, headers=hdrs).status_code == 400
    assert calls_503['post'] == 0


def test_f1_concurrent_orders_and_single_worker_guard(tmp_path: Path, monkeypatch):
    import aegis.market.supervisor as sup_mod
    monkeypatch.setattr(sup_mod, 'market_session_state', lambda _now=None: 'OPEN')
    monkeypatch.setenv('WEB_CONCURRENCY', '2')
    with pytest.raises(RuntimeError, match='MULTIWORKER_BLOCKED'):
        create_app(str(tmp_path / 'mw.sqlite'), token='tok-mw')
    monkeypatch.delenv('WEB_CONCURRENCY')

    monkeypatch.setenv('AEGIS_EXECUTION_MODE', 'paper_external')
    monkeypatch.setenv('AEGIS_DATA_MODE', 'live')
    monkeypatch.setenv('ALPACA_API_KEY', 'test_alp_key_1234')
    monkeypatch.setenv('ALPACA_SECRET_KEY', 'test_alp_sec_1234')
    transport, calls = _mock_broker_transport(cash='1000.00', equity='10000.00')
    app = create_app(str(tmp_path / 'conc.sqlite'), token='tok-f1')
    app.state.paper_oms._client = httpx.Client(transport=transport)

    with TestClient(app) as c:
        hdrs = _authed_headers(c)
        _seed_live_bar(app, symbol='NVDA', price=100.0)

        def submit(i: int) -> int:
            return c.post('/api/orders', json={'symbol': 'NVDA', 'side': 'BUY', 'qty': 6, 'client_id': f'conc-{i}'}, headers=hdrs).status_code

        with ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(submit, range(4)))
        # Cash is $1000 and each order is $600 -> at most 1 order can be approved and submitted!
        assert statuses.count(200) == 1
        assert calls['post'] == 1


def test_f2_live_ws_event_propagates_to_bars_scanner_state_and_forecast_without_synthetic_leak(tmp_path: Path, monkeypatch):
    async def run_ws_and_verify():
        stop = asyncio.Event()

        async def handle_ws(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                raw = await reader.readuntil(b'\r\n\r\n')
                key = ''
                for line in raw.decode('latin-1').split('\r\n'):
                    if line.lower().startswith('sec-websocket-key:'):
                        key = line.split(':', 1)[1].strip()
                accept = base64.b64encode(hashlib.sha1((key + _GUID).encode('ascii')).digest()).decode('ascii')
                writer.write((
                    'HTTP/1.1 101 Switching Protocols\r\n'
                    'Upgrade: websocket\r\n'
                    'Connection: Upgrade\r\n'
                    f'Sec-WebSocket-Accept: {accept}\r\n\r\n'
                ).encode('ascii'))
                await writer.drain()

                def enc(txt: str) -> bytes:
                    b = txt.encode('utf-8')
                    hdr = bytearray([0x81])
                    if len(b) < 126:
                        hdr.append(len(b))
                    else:
                        hdr.append(126)
                        hdr.extend(struct.pack('!H', len(b)))
                    return bytes(hdr) + b

                async def read_frame():
                    b1, b2 = await reader.readexactly(2)
                    ln = b2 & 0x7F
                    if ln == 126:
                        (ln,) = struct.unpack('!H', await reader.readexactly(2))
                    _m = await reader.readexactly(4)
                    return await reader.readexactly(ln)

                # Read auth, reply authenticated, then read subscribe, then emit bar
                await read_frame()
                writer.write(enc(json.dumps([{'T': 'success', 'msg': 'authenticated'}])))
                await writer.drain()
                await read_frame()
                # Emit uniquely recognizable price 777.88 and volume 54321
                now_iso = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
                writer.write(enc(json.dumps([
                    {'T': 'b', 'S': 'NVDA', 't': now_iso, 'o': 776.0, 'h': 778.5, 'l': 775.5, 'c': 777.88, 'v': 54321}
                ])))
                await writer.drain()
                await stop.wait()
            finally:
                writer.close()

        server = await asyncio.start_server(handle_ws, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]

        monkeypatch.setenv('AEGIS_DATA_MODE', 'live')
        monkeypatch.setenv('AEGIS_ALPACA_STREAM_URL', f'ws://127.0.0.1:{port}/v2')
        monkeypatch.setenv('ALPACA_API_KEY', 'test_alp_key_1234')
        monkeypatch.setenv('ALPACA_SECRET_KEY', 'test_alp_sec_1234')

        app = create_app(str(tmp_path / 'f2_live.sqlite'), token='tok-f2')
        async with server:
            with TestClient(app) as client:
                hdrs = _authed_headers(client, 'tok-f2')
                # Wait briefly for WS frame to ingest into MarketReadModel
                for _ in range(30):
                    mkt = client.get('/api/market/NVDA').json()
                    if mkt['bars']:
                        break
                    await asyncio.sleep(0.05)

                assert mkt['source_mode'] == 'live'
                assert mkt['bars'][-1]['close'] == 777.88
                assert mkt['bars'][-1]['volume'] == 54321.0
                expected_hash = mkt['bars'][-1]['raw_payload_hash']

                st = client.get('/api/state').json()
                assert st['mode'] == 'LIVE_MARKET'
                assert st['scanner'][0]['symbol'] == 'NVDA'
                assert st['scanner'][0]['close'] == 777.88
                assert st['scanner'][0]['raw_payload_hash'] == expected_hash

                # Unseen symbol AAPL must NOT fall back to synthetic replay bars
                aapl = client.get('/api/market/AAPL').json()
                assert aapl['bars'] == []

                # Forecast with < 24 live bars must refuse cleanly rather than using synthetic prices
                fc_fail = client.post('/api/forecasts', json={'symbol': 'NVDA', 'horizon': 6}, headers=hdrs)
                assert fc_fail.status_code == 400
                stop.set()

    asyncio.run(run_ws_and_verify())
