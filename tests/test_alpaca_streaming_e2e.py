from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct
from datetime import datetime, timezone

import httpx
import pytest

from aegis.market.supervisor import MarketDataSupervisor
from aegis.providers.alpaca_market import AlpacaMarketAdapter
from aegis.secrets import SecretResolver
from aegis.settings import AegisSettings

_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


async def _ws_read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    b1, b2 = await reader.readexactly(2)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        (length,) = struct.unpack('!H', await reader.readexactly(2))
    elif length == 127:
        (length,) = struct.unpack('!Q', await reader.readexactly(8))
    mask = await reader.readexactly(4) if masked else b''
    payload = await reader.readexactly(length) if length else b''
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def _ws_encode_text(text: str) -> bytes:
    data = text.encode('utf-8')
    hdr = bytearray([0x81])
    if len(data) < 126:
        hdr.append(len(data))
    else:
        hdr.append(126)
        hdr.extend(struct.pack('!H', len(data)))
    return bytes(hdr) + data


async def _handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    raw = await reader.readuntil(b'\r\n\r\n')
    key = ''
    for line in raw.decode('latin-1').split('\r\n'):
        if line.lower().startswith('sec-websocket-key:'):
            key = line.split(':', 1)[1].strip()
    accept = base64.b64encode(hashlib.sha1((key + _GUID).encode('ascii')).digest()).decode('ascii')
    writer.write(
        (
            'HTTP/1.1 101 Switching Protocols\r\n'
            'Upgrade: websocket\r\n'
            'Connection: Upgrade\r\n'
            f'Sec-WebSocket-Accept: {accept}\r\n\r\n'
        ).encode('ascii')
    )
    await writer.drain()


def test_alpaca_ws_stream_reconnects_resubscribes_and_shuts_down():
    asyncio.run(_run_reconnect_test())


async def _run_reconnect_test():
    connections = 0
    subscribes: list[list[str]] = []
    received_symbols: list[str] = []
    stop_event = asyncio.Event()

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal connections
        connections += 1
        conn_idx = connections
        try:
            await _handshake(reader, writer)
            writer.write(_ws_encode_text(json.dumps([{'T': 'success', 'msg': 'connected'}])))
            await writer.drain()

            _op, auth_raw = await _ws_read_frame(reader)
            auth_msg = json.loads(auth_raw.decode('utf-8'))
            assert auth_msg['action'] == 'auth'
            writer.write(_ws_encode_text(json.dumps([{'T': 'success', 'msg': 'authenticated'}])))
            await writer.drain()

            _op, sub_raw = await _ws_read_frame(reader)
            sub_msg = json.loads(sub_raw.decode('utf-8'))
            subscribes.append(sub_msg['bars'])

            bar_ts = '2026-11-14T15:00:00Z' if conn_idx == 1 else '2026-11-14T15:01:00Z'
            bar_close = 120.5 if conn_idx == 1 else 121.25
            writer.write(
                _ws_encode_text(
                    json.dumps([
                        {'T': 'b', 'S': 'NVDA', 't': bar_ts, 'o': 120.0, 'h': 121.5, 'l': 119.5, 'c': bar_close, 'v': 5000}
                    ])
                )
            )
            await writer.drain()

            if conn_idx == 1:
                # Force abrupt disconnect on first connection to verify reconnect + resubscribe
                writer.close()
                return
            await asyncio.sleep(0.05)
            stop_event.set()
        finally:
            writer.close()

    server = await asyncio.start_server(handle_client, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]

    settings = AegisSettings.from_env({'AEGIS_DATA_MODE': 'live', 'AEGIS_ALPACA_DATA_FEED': 'iex'})
    resolver = SecretResolver({'ALPACA_API_KEY': 'test_key_1234', 'ALPACA_SECRET_KEY': 'test_sec_1234'})
    supervisor = MarketDataSupervisor(feed='iex', source_mode='live', allow_synthetic_fallback=False)
    adapter = AlpacaMarketAdapter(settings, resolver, supervisor, stream_url=f'ws://127.0.0.1:{port}/v2')

    def on_event(ev):
        received_symbols.append(f'{ev.symbol}:{ev.close}')

    async with server:
        await asyncio.wait_for(
            adapter.run_stream(['NVDA'], on_event=on_event, stop_event=stop_event, base_backoff_seconds=0.05, heartbeat_seconds=1.0),
            timeout=5.0,
        )

    assert connections >= 2
    assert subscribes == [['NVDA'], ['NVDA']]
    assert received_symbols == ['NVDA:120.5', 'NVDA:121.25']
    assert supervisor.status()['reconnect_attempts'] >= 1


def test_alpaca_ws_auth_403_marks_entitlement_blocked():
    asyncio.run(_run_auth_403_test())


async def _run_auth_403_test():
    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await _handshake(reader, writer)
            _op, _auth = await _ws_read_frame(reader)
            writer.write(_ws_encode_text(json.dumps([{'T': 'error', 'code': 403, 'msg': 'not entitled to sip'}])))
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle_client, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    settings = AegisSettings.from_env({'AEGIS_DATA_MODE': 'live', 'AEGIS_ALPACA_DATA_FEED': 'sip'})
    resolver = SecretResolver({'ALPACA_API_KEY': 'test_key_1234', 'ALPACA_SECRET_KEY': 'test_sec_1234'})
    supervisor = MarketDataSupervisor(feed='sip', source_mode='live', allow_synthetic_fallback=False)
    adapter = AlpacaMarketAdapter(settings, resolver, supervisor, stream_url=f'ws://127.0.0.1:{port}/v2')

    async with server:
        await asyncio.wait_for(adapter.run_stream(['NVDA']), timeout=3.0)

    assert supervisor.status()['state'] == 'BLOCKED_ENTITLEMENT'


def test_alpaca_rest_backfill_paginates_and_classifies_409():
    settings = AegisSettings.from_env({'AEGIS_DATA_MODE': 'historical'})
    resolver = SecretResolver({'ALPACA_API_KEY': 'test_key_1234', 'ALPACA_SECRET_KEY': 'test_sec_1234'})
    supervisor = MarketDataSupervisor(feed='iex', source_mode='historical', allow_synthetic_fallback=False)
    adapter = AlpacaMarketAdapter(settings, resolver, supervisor)

    pages_seen: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        tok = req.url.params.get('page_token')
        pages_seen.append(tok)
        if tok is None:
            return httpx.Response(200, json={
                'bars': {'NVDA': [{'t': '2026-11-14T14:00:00Z', 'o': 100, 'h': 101, 'l': 99, 'c': 100.5, 'v': 10}]},
                'next_page_token': 'page-2',
            })
        return httpx.Response(200, json={
            'bars': {'NVDA': [{'t': '2026-11-14T14:15:00Z', 'o': 100.5, 'h': 102, 'l': 100, 'c': 101.5, 'v': 15}]},
            'next_page_token': None,
        })

    res = adapter.backfill_bars(
        ['NVDA'],
        datetime(2026, 11, 14, 14, 0, tzinfo=timezone.utc),
        datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert res['status'] == 'OK'
    assert res['pages'] == 2
    assert res['bars_ingested'] == 2
    assert pages_seen == [None, 'page-2']

    # 409 conflict classification
    res_409 = adapter.backfill_bars(
        ['NVDA'],
        datetime(2026, 11, 14, 14, 0, tzinfo=timezone.utc),
        datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc),
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(409, json={'message': 'conflict'}))),
    )
    assert res_409['status'] == 'CONFLICT_409'
