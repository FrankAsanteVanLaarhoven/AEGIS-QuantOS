"""Minimal standard-library asyncio RFC 6455 WebSocket client (ws:// and wss://)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import ssl
import struct
from urllib.parse import urlparse

_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


class WebSocketClosed(Exception):
    def __init__(self, code: int = 1000, reason: str = '') -> None:
        super().__init__(f'WebSocket closed ({code}): {reason}')
        self.code = code
        self.reason = reason


class AsyncWebSocket:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self.closed = False

    @classmethod
    async def connect(cls, url: str, *, timeout: float = 10.0) -> 'AsyncWebSocket':
        parsed = urlparse(url)
        if parsed.scheme not in ('ws', 'wss'):
            raise ValueError(f'Unsupported WebSocket scheme: {parsed.scheme}')
        host = parsed.hostname or '127.0.0.1'
        port = parsed.port or (443 if parsed.scheme == 'wss' else 80)
        path = parsed.path or '/'
        if parsed.query:
            path = f'{path}?{parsed.query}'

        ssl_ctx = ssl.create_default_context() if parsed.scheme == 'wss' else None
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=ssl_ctx), timeout=timeout)

        key_bytes = os.urandom(16)
        sec_key = base64.b64encode(key_bytes).decode('ascii')
        host_hdr = f'{host}:{port}' if parsed.port else host
        req = (
            f'GET {path} HTTP/1.1\r\n'
            f'Host: {host_hdr}\r\n'
            f'Upgrade: websocket\r\n'
            f'Connection: Upgrade\r\n'
            f'Sec-WebSocket-Key: {sec_key}\r\n'
            f'Sec-WebSocket-Version: 13\r\n\r\n'
        )
        writer.write(req.encode('ascii'))
        await writer.drain()

        raw_hdr = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), timeout=timeout)
        lines = raw_hdr.decode('latin-1').split('\r\n')
        status_line = lines[0]
        if ' 101 ' not in status_line:
            writer.close()
            raise ConnectionError(f'WebSocket handshake failed: {status_line}')
        headers = {}
        for line in lines[1:]:
            if ':' in line:
                k, v = line.split(':', 1)
                headers[k.strip().lower()] = v.strip()
        expected_accept = base64.b64encode(hashlib.sha1((sec_key + _GUID).encode('ascii')).digest()).decode('ascii')
        if headers.get('sec-websocket-accept') != expected_accept:
            writer.close()
            raise ConnectionError('Invalid Sec-WebSocket-Accept header')
        return cls(reader, writer)

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self.closed:
            raise WebSocketClosed(1000, 'already closed')
        fin_opcode = 0x80 | (opcode & 0x0F)
        length = len(payload)
        mask_key = os.urandom(4)
        header = bytearray([fin_opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack('!H', length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack('!Q', length))
        header.extend(mask_key)
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        self._writer.write(bytes(header) + masked)
        await self._writer.drain()

    async def send_text(self, text: str) -> None:
        await self._send_frame(0x1, text.encode('utf-8'))

    async def ping(self, data: bytes = b'hb') -> None:
        await self._send_frame(0x9, data)

    async def pong(self, data: bytes = b'') -> None:
        await self._send_frame(0xA, data)

    async def close(self, code: int = 1000, reason: str = '') -> None:
        if self.closed:
            return
        self.closed = True
        try:
            payload = struct.pack('!H', code) + reason.encode('utf-8')
            fin_opcode = 0x80 | 0x8
            mask_key = os.urandom(4)
            header = bytes([fin_opcode, 0x80 | len(payload)]) + mask_key
            masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
            self._writer.write(header + masked)
            await self._writer.drain()
        except Exception:
            pass
        finally:
            self._writer.close()

    async def recv_text(self, *, timeout: float | None = None) -> str:
        while True:
            try:
                b1, b2 = await asyncio.wait_for(self._reader.readexactly(2), timeout=timeout)
            except asyncio.IncompleteReadError as exc:
                self.closed = True
                self._writer.close()
                raise WebSocketClosed(1006, 'unexpected EOF') from exc
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            length = b2 & 0x7F
            if length == 126:
                (length,) = struct.unpack('!H', await self._reader.readexactly(2))
            elif length == 127:
                (length,) = struct.unpack('!Q', await self._reader.readexactly(8))
            mask_key = await self._reader.readexactly(4) if masked else b''
            payload = await self._reader.readexactly(length) if length else b''
            if masked:
                payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

            if opcode == 0x8:  # Close
                code = struct.unpack('!H', payload[:2])[0] if len(payload) >= 2 else 1000
                reason = payload[2:].decode('utf-8', errors='replace') if len(payload) > 2 else ''
                self.closed = True
                self._writer.close()
                raise WebSocketClosed(code, reason)
            if opcode == 0x9:  # Ping
                await self.pong(payload)
                continue
            if opcode == 0xA:  # Pong
                continue
            if opcode in (0x1, 0x2):  # Text / Binary
                return payload.decode('utf-8')
