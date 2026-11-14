"""Alpaca market-data WebSocket protocol and REST backfill adapter.

Separates data endpoints from broker paper execution endpoints.
Never substitutes synthetic data on failure or entitlement denial.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Callable, Literal

import httpx

from aegis.market.contracts import MarketEvent, payload_sha256
from aegis.market.supervisor import MarketDataSupervisor
from aegis.providers.ws_client import AsyncWebSocket, WebSocketClosed
from aegis.secrets import SecretResolver
from aegis.settings import AegisSettings


class AlpacaMarketAdapter:
    def __init__(
        self,
        settings: AegisSettings,
        resolver: SecretResolver,
        supervisor: MarketDataSupervisor,
        *,
        stream_url: str | None = None,
    ) -> None:
        self.settings = settings
        self.resolver = resolver
        self.supervisor = supervisor
        self._stream_url_override = stream_url

    def credentials(self) -> tuple[str, str] | None:
        key = self.resolver.resolve('ALPACA_API_KEY')
        sec = self.resolver.resolve('ALPACA_SECRET_KEY')
        if not key or not sec:
            self.supervisor.mark_blocked_credential('ALPACA_API_KEY/ALPACA_SECRET_KEY not configured')
            return None
        return key, sec

    def stream_endpoint(self) -> str:
        base = (self._stream_url_override or self.settings.alpaca_stream_url).rstrip('/')
        return f'{base}/{self.settings.alpaca_data_feed}'

    def build_auth_message(self) -> dict[str, str]:
        creds = self.credentials()
        if not creds:
            raise ValueError('BLOCKED_CREDENTIAL: Alpaca market data credentials missing')
        return {'action': 'auth', 'key': creds[0], 'secret': creds[1]}

    def build_subscribe_message(self, symbols: list[str] | None = None) -> dict[str, Any]:
        syms = symbols or self.supervisor.symbols
        return {'action': 'subscribe', 'trades': syms, 'bars': syms}

    def handle_ws_messages(self, raw_payload: str | list[dict[str, Any]], receive_ts: datetime | None = None) -> list[MarketEvent]:
        rx = receive_ts or datetime.now(timezone.utc)
        items = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
        events: list[MarketEvent] = []
        for item in items:
            t = item.get('T')
            if t == 'error':
                code = item.get('code')
                msg = item.get('msg', 'Alpaca stream error')
                if code in (401, 402, 403):
                    self.supervisor.mark_blocked_entitlement(f'Alpaca stream error {code}: {msg}')
                else:
                    self.supervisor.mark_disconnected(f'Alpaca stream error {code}: {msg}')
                continue
            if t == 'success' and item.get('msg') == 'authenticated':
                self.supervisor.mark_authenticated()
                continue
            if t in ('b', 't'):
                sym = str(item.get('S', '')).upper()
                if not sym:
                    continue
                pts = datetime.fromisoformat(str(item['t']).replace('Z', '+00:00'))
                ev = MarketEvent(
                    event_id=f"alpaca:{self.settings.alpaca_data_feed}:{t}:{sym}:{item['t']}:{item.get('i', '')}",
                    instrument_id=f'US_EQUITY:{sym}',
                    symbol=sym,
                    venue='IEX' if self.settings.alpaca_data_feed == 'iex' else 'SIP',
                    event_type='bar' if t == 'b' else 'trade',
                    source=f'alpaca_{self.settings.alpaca_data_feed}',
                    provider='alpaca',
                    feed=self.settings.alpaca_data_feed,
                    source_mode='live',
                    provider_ts=pts,
                    receive_ts=rx,
                    available_at=rx,
                    sequence=str(item['i']) if 'i' in item else None,
                    price=float(item['p']) if t == 't' else float(item['c']),
                    open=float(item['o']) if t == 'b' else None,
                    high=float(item['h']) if t == 'b' else None,
                    low=float(item['l']) if t == 'b' else None,
                    close=float(item['c']) if t == 'b' else None,
                    quantity=float(item['s']) if t == 't' else None,
                    volume=float(item['v']) if t == 'b' else None,
                    entitlement_ref=f'alpaca:{self.settings.alpaca_data_feed}',
                    raw_payload_hash=payload_sha256(item),
                )
                self.supervisor.push_event(ev)
                events.append(ev)
        return events

    def backfill_bars(
        self,
        symbols: list[str],
        start: datetime,
        end: datetime,
        *,
        timeframe: str = '15Min',
        client: httpx.Client | None = None,
    ) -> dict[str, Any]:
        creds = self.credentials()
        if not creds:
            return {'status': 'BLOCKED_CREDENTIAL', 'bars_ingested': 0}
        headers = {'APCA-API-KEY-ID': creds[0], 'APCA-API-SECRET-KEY': creds[1]}
        params = {
            'symbols': ','.join(symbols),
            'timeframe': timeframe,
            'start': start.astimezone(timezone.utc).isoformat(),
            'end': end.astimezone(timezone.utc).isoformat(),
            'feed': self.settings.alpaca_data_feed,
            'adjustment': 'raw',
        }
        url = f"{self.settings.alpaca_data_base_url.rstrip('/')}/v2/stocks/bars"
        own_client = client is None
        http = client or httpx.Client(timeout=15.0)
        pages_bars: list[tuple[str, dict[str, Any]]] = []
        pages = 0
        page_token: str | None = None
        try:
            while True:
                req_params = dict(params)
                if page_token:
                    req_params['page_token'] = page_token
                resp = http.get(url, headers=headers, params=req_params)
                if resp.status_code in (401, 403):
                    self.supervisor.mark_blocked_entitlement(f'Alpaca REST backfill {resp.status_code}')
                    return {'status': 'BLOCKED_ENTITLEMENT', 'http_status': resp.status_code, 'bars_ingested': 0}
                if resp.status_code == 409:
                    self.supervisor.mark_disconnected('Alpaca REST conflict (409)')
                    return {'status': 'CONFLICT_409', 'http_status': 409, 'bars_ingested': 0}
                if resp.status_code == 429:
                    self.supervisor.mark_disconnected('Alpaca REST rate limited (429)')
                    return {'status': 'RATE_LIMITED', 'http_status': 429, 'bars_ingested': 0}
                resp.raise_for_status()
                data = resp.json()
                pages += 1
                for sym, rows in (data.get('bars') or {}).items():
                    for row in rows:
                        pages_bars.append((sym, row))
                page_token = data.get('next_page_token')
                if not page_token:
                    break
        finally:
            if own_client:
                http.close()

        rx = datetime.now(timezone.utc)
        count = 0
        for sym, row in pages_bars:
                pts = datetime.fromisoformat(str(row['t']).replace('Z', '+00:00'))
                ev = MarketEvent(
                    event_id=f"alpaca_rest:{self.settings.alpaca_data_feed}:{sym}:{row['t']}",
                    instrument_id=f'US_EQUITY:{sym}',
                    symbol=sym,
                    venue='IEX' if self.settings.alpaca_data_feed == 'iex' else 'SIP',
                    event_type='bar',
                    source=f'alpaca_rest_{self.settings.alpaca_data_feed}',
                    provider='alpaca',
                    feed=self.settings.alpaca_data_feed,
                    source_mode='historical',
                    provider_ts=pts,
                    receive_ts=rx,
                    available_at=rx,
                    open=float(row['o']),
                    high=float(row['h']),
                    low=float(row['l']),
                    close=float(row['c']),
                    volume=float(row['v']),
                    entitlement_ref=f'alpaca:{self.settings.alpaca_data_feed}',
                    raw_payload_hash=payload_sha256(row),
                )
                self.supervisor.push_event(ev)
                count += 1
        return {'status': 'OK', 'bars_ingested': count, 'pages': pages, 'feed': self.settings.alpaca_data_feed}

    async def run_stream(
        self,
        symbols: list[str],
        *,
        on_event: Callable[[MarketEvent], Any] | None = None,
        stop_event: asyncio.Event | None = None,
        max_reconnects: int | None = None,
        heartbeat_seconds: float = 20.0,
        base_backoff_seconds: float = 0.25,
    ) -> None:
        """Maintain a supervised WebSocket stream with auth, subscribe, heartbeat, and reconnect."""
        creds = self.credentials()
        if not creds:
            return
        attempts = 0
        while True:
            if stop_event and stop_event.is_set():
                return
            ws: AsyncWebSocket | None = None
            try:
                ws = await AsyncWebSocket.connect(self.stream_endpoint(), timeout=10.0)
                await ws.send_text(json.dumps(self.build_auth_message()))
                # Read up to 2 frames for connected + auth status
                authed = False
                for _ in range(3):
                    raw = await ws.recv_text(timeout=10.0)
                    items = json.loads(raw) if raw.strip().startswith('[') else [json.loads(raw)]
                    for item in items:
                        if item.get('T') == 'error':
                            code = int(item.get('code') or 0)
                            msg = str(item.get('msg') or 'stream error')
                            if code in (401, 403):
                                self.supervisor.mark_blocked_entitlement(f'Alpaca WS {code}: {msg}')
                                await ws.close()
                                return
                            if code == 406 or code == 409:
                                raise ConnectionError(f'Alpaca WS connection limit ({code}): {msg}')
                            raise ConnectionError(f'Alpaca WS error ({code}): {msg}')
                        if item.get('T') == 'success' and 'authenticated' in str(item.get('msg', '')).lower():
                            authed = True
                    if authed:
                        break
                if not authed:
                    self.supervisor.mark_authenticated()
                await ws.send_text(json.dumps(self.build_subscribe_message(symbols)))

                while True:
                    if stop_event and stop_event.is_set():
                        await ws.close()
                        return
                    try:
                        frame = await ws.recv_text(timeout=heartbeat_seconds)
                    except asyncio.TimeoutError:
                        await ws.ping()
                        continue
                    events = self.handle_ws_messages(frame)
                    for ev in events:
                        if on_event is not None:
                            res = on_event(ev)
                            if asyncio.iscoroutine(res):
                                await res
            except (ConnectionError, OSError, asyncio.TimeoutError, WebSocketClosed) as exc:
                self.supervisor.mark_disconnected(str(exc))
                attempts += 1
                if max_reconnects is not None and attempts > max_reconnects:
                    return
                if stop_event and stop_event.is_set():
                    return
                delay = self.supervisor.next_backoff_seconds(base=base_backoff_seconds, cap=30.0)
                await asyncio.sleep(delay)
            finally:
                if ws is not None and not ws.closed:
                    await ws.close()


