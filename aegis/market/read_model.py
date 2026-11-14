"""Durable SQLite-backed bar and mark read model isolating live/delayed/historical from synthetic replay."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from aegis.market.contracts import MarketEvent
from aegis.store import Store


class InstrumentMark(BaseModel):
    model_config = ConfigDict(extra='forbid')

    symbol: str
    price: float
    bid: float | None = None
    ask: float | None = None
    volume: float = 0.0
    provider: str
    feed: str
    source_mode: Literal['live', 'delayed', 'historical', 'synthetic']
    timeframe: str = '15Min'
    provider_ts: datetime
    receive_ts: datetime
    available_at: datetime
    raw_payload_hash: str
    event_id: str
    age_seconds: float | None = None
    stale: bool = False


class MarketSnapshot(BaseModel):
    model_config = ConfigDict(extra='forbid')

    source_mode: Literal['live', 'delayed', 'historical', 'synthetic']
    feed: str
    feed_state: str
    marks: dict[str, InstrumentMark]
    integrity_ok: bool
    as_of: datetime


class MarketReadModel:
    """Durable completed-bar and per-instrument mark store scoped by source_mode."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._lock = threading.RLock()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._store.tx():
            self._store.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS market_bars (
                    source_mode TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    bar_ts TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    provider TEXT NOT NULL,
                    feed TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    raw_payload_hash TEXT NOT NULL,
                    provider_ts TEXT NOT NULL,
                    receive_ts TEXT NOT NULL,
                    PRIMARY KEY (source_mode, timeframe, symbol, bar_ts)
                );
                CREATE TABLE IF NOT EXISTS market_marks (
                    source_mode TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    mark_json TEXT NOT NULL,
                    PRIMARY KEY (source_mode, symbol)
                );
                """
            )

    def ingest_event(self, ev: MarketEvent, *, timeframe: str = '15Min') -> InstrumentMark:
        sym = ev.symbol.upper()
        pts = ev.provider_ts.astimezone(timezone.utc)
        rts = ev.receive_ts.astimezone(timezone.utc)
        ats = ev.available_at.astimezone(timezone.utc)
        price = ev.close if ev.close is not None else (ev.price if ev.price is not None else 0.0)
        mark = InstrumentMark(
            symbol=sym,
            price=float(price),
            bid=getattr(ev, 'bid', None),
            ask=getattr(ev, 'ask', None),
            volume=float(ev.volume or 0.0),
            provider=ev.provider,
            feed=ev.feed,
            source_mode=ev.source_mode,
            timeframe=timeframe,
            provider_ts=pts,
            receive_ts=rts,
            available_at=ats,
            raw_payload_hash=ev.raw_payload_hash,
            event_id=ev.event_id,
        )
        with self._lock:
            with self._store.tx():
                if ev.event_type == 'bar' and ev.close is not None:
                    self._store.db.execute(
                        """
                        INSERT INTO market_bars(
                            source_mode, timeframe, symbol, bar_ts,
                            open, high, low, close, volume,
                            provider, feed, event_id, raw_payload_hash, provider_ts, receive_ts
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source_mode, timeframe, symbol, bar_ts) DO UPDATE SET
                            open = excluded.open,
                            high = excluded.high,
                            low = excluded.low,
                            close = excluded.close,
                            volume = excluded.volume,
                            event_id = excluded.event_id,
                            raw_payload_hash = excluded.raw_payload_hash,
                            provider_ts = excluded.provider_ts,
                            receive_ts = excluded.receive_ts
                        """,
                        (
                            ev.source_mode,
                            timeframe,
                            sym,
                            pts.isoformat(),
                            float(ev.open if ev.open is not None else price),
                            float(ev.high if ev.high is not None else price),
                            float(ev.low if ev.low is not None else price),
                            float(ev.close),
                            float(ev.volume or 0.0),
                            ev.provider,
                            ev.feed,
                            ev.event_id,
                            ev.raw_payload_hash,
                            pts.isoformat(),
                            rts.isoformat(),
                        ),
                    )
                self._store.db.execute(
                    """
                    INSERT INTO market_marks(source_mode, symbol, mark_json)
                    VALUES (?, ?, ?)
                    ON CONFLICT(source_mode, symbol) DO UPDATE SET mark_json = excluded.mark_json
                    """,
                    (ev.source_mode, sym, mark.model_dump_json()),
                )
        return mark

    def bars(self, symbol: str, *, source_mode: str, timeframe: str = '15Min', limit: int = 240) -> list[dict[str, Any]]:
        with self._lock:
            with self._store.lock:
                rows = self._store.db.execute(
                    """
                    SELECT * FROM (
                        SELECT * FROM market_bars
                        WHERE source_mode = ? AND timeframe = ? AND symbol = ?
                        ORDER BY bar_ts DESC
                        LIMIT ?
                    ) sub ORDER BY bar_ts ASC
                    """,
                    (source_mode, timeframe, symbol.upper(), limit),
                ).fetchall()
                return [
                    {
                        'time': r['bar_ts'],
                        'open': float(r['open']),
                        'high': float(r['high']),
                        'low': float(r['low']),
                        'close': float(r['close']),
                        'volume': float(r['volume']),
                        'provider': r['provider'],
                        'feed': r['feed'],
                        'source_mode': r['source_mode'],
                        'event_id': r['event_id'],
                        'raw_payload_hash': r['raw_payload_hash'],
                        'provider_ts': r['provider_ts'],
                    }
                    for r in rows
                ]

    def scanner(self, *, source_mode: str, timeframe: str = '15Min') -> list[dict[str, Any]]:
        with self._lock:
            with self._store.lock:
                symbols = [
                    r['symbol']
                    for r in self._store.db.execute(
                        'SELECT DISTINCT symbol FROM market_bars WHERE source_mode = ? AND timeframe = ? ORDER BY symbol',
                        (source_mode, timeframe),
                    ).fetchall()
                ]
                out: list[dict[str, Any]] = []
                for sym in symbols:
                    b = self.bars(sym, source_mode=source_mode, timeframe=timeframe, limit=24)
                    if not b:
                        continue
                    last = b[-1]
                    prev = b[-2] if len(b) >= 2 else last
                    chg = ((last['close'] - prev['close']) / prev['close']) if prev['close'] else 0.0
                    avg_vol = sum(x['volume'] for x in b) / len(b) if b else 1.0
                    rel_vol = (last['volume'] / avg_vol) if avg_vol else 1.0
                    out.append({
                        'symbol': sym,
                        'price': last['close'],
                        'close': last['close'],
                        'volume': last['volume'],
                        'relative_volume': round(rel_vol, 3),
                        'change_pct': round(chg * 100.0, 4),
                        'series': [x['close'] for x in b],
                        'signal': {'action': 'OBSERVE', 'score': 0.0},
                        'provider': last['provider'],
                        'feed': last['feed'],
                        'source_mode': source_mode,
                        'event_id': last['event_id'],
                        'raw_payload_hash': last['raw_payload_hash'],
                        'provider_ts': last['provider_ts'],
                    })
                return out

    def snapshot(
        self,
        *,
        source_mode: Literal['live', 'delayed', 'historical', 'synthetic'],
        feed: str,
        feed_state: str,
        max_age_seconds: float,
        now: datetime | None = None,
    ) -> MarketSnapshot:
        now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        marks: dict[str, InstrumentMark] = {}
        with self._lock:
            with self._store.lock:
                rows = self._store.db.execute(
                    'SELECT symbol, mark_json FROM market_marks WHERE source_mode = ?',
                    (source_mode,),
                ).fetchall()
                for r in rows:
                    m = InstrumentMark.model_validate_json(r['mark_json'])
                    # Freshness MUST be evaluated against provider_ts, never receive_ts
                    age = max(0.0, (now_utc - m.provider_ts.astimezone(timezone.utc)).total_seconds())
                    stale = age > max_age_seconds
                    marks[r['symbol']] = m.model_copy(update={'age_seconds': round(age, 3), 'stale': stale})

        healthy_feed = feed_state in ('LIVE', 'DELAYED', 'RECEIVING') and source_mode in ('live', 'delayed')
        integrity_ok = healthy_feed and len(marks) > 0 and all(not m.stale and m.price > 0 for m in marks.values())
        return MarketSnapshot(
            source_mode=source_mode,
            feed=feed,
            feed_state=feed_state,
            marks=marks,
            integrity_ok=integrity_ok,
            as_of=now_utc,
        )
