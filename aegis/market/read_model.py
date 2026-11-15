"""Durable SQLite-backed bar and mark read model isolating live/delayed/historical from synthetic replay."""
from __future__ import annotations

import hashlib
import json
import math
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from aegis.market.contracts import MarketEvent
from aegis.store import Store

MAX_FUTURE_CLOCK_SKEW_SECONDS = 5.0
MIN_LIVE_FORECAST_BARS = 65

_TIMEFRAME_MINUTES: dict[str, int] = {
    '1Min': 1,
    '5Min': 5,
    '15Min': 15,
    '30Min': 30,
    '1Hour': 60,
    '1Day': 1440,
}


def timeframe_minutes(timeframe: str) -> int:
    if timeframe not in _TIMEFRAME_MINUTES:
        raise ValueError(f'UNSUPPORTED_TIMEFRAME: {timeframe}')
    return _TIMEFRAME_MINUTES[timeframe]


def timeframe_delta(timeframe: str) -> timedelta:
    return timedelta(minutes=timeframe_minutes(timeframe))


def _parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt_val = value
    else:
        dt_val = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if dt_val.tzinfo is None:
        raise ValueError('Timezone-aware timestamps required')
    return dt_val.astimezone(timezone.utc)


class LiveForecastSeries:
    """Validated live/delayed/historical bar series for Kronos inference (65-bar minimum, availability-gated)."""

    def __init__(
        self,
        *,
        symbol: str,
        bars: list[dict[str, Any]],
        source_mode: str,
        timeframe: str,
        as_of: datetime,
        min_bars: int = MIN_LIVE_FORECAST_BARS,
    ) -> None:
        sym = symbol.upper()
        as_of_utc = as_of.astimezone(timezone.utc)
        eligible: list[dict[str, Any]] = []
        seen_ts: set[str] = set()
        for raw in bars:
            o, h, l, c, v = (
                float(raw['open']),
                float(raw['high']),
                float(raw['low']),
                float(raw['close']),
                float(raw['volume']),
            )
            if not all(math.isfinite(x) for x in (o, h, l, c, v)) or min(o, h, l, c) <= 0 or v < 0:
                raise ValueError('Nonfinite or invalid OHLCV in live forecast input')
            if h < max(o, c, l) or l > min(o, c, h):
                raise ValueError('Invalid OHLC envelope in live forecast input')
            bar_start = _parse_utc(raw.get('open_at') or raw['time'])
            bar_end = _parse_utc(raw.get('timestamp') or (bar_start + timeframe_delta(timeframe)))
            avail = _parse_utc(raw.get('available_at') or bar_end)
            if bar_start >= bar_end or avail < bar_end:
                raise ValueError('Invalid bar interval or availability chronology')
            if avail > as_of_utc + timedelta(seconds=MAX_FUTURE_CLOCK_SKEW_SECONDS):
                continue
            ts_iso = bar_end.isoformat().replace('+00:00', 'Z')
            if ts_iso in seen_ts:
                raise ValueError('Duplicate bar timestamp in live forecast input')
            seen_ts.add(ts_iso)
            eligible.append({
                'symbol': sym,
                'open_at': bar_start.isoformat().replace('+00:00', 'Z'),
                'timestamp': ts_iso,
                'available_at': avail.isoformat().replace('+00:00', 'Z'),
                'open': o,
                'high': h,
                'low': l,
                'close': c,
                'volume': v,
                'timeframe': raw.get('timeframe', timeframe),
                'provider': raw.get('provider'),
                'feed': raw.get('feed'),
                'source_mode': raw.get('source_mode', source_mode),
                'raw_payload_hash': raw.get('raw_payload_hash'),
            })
        eligible.sort(key=lambda r: r['timestamp'])
        if len(eligible) < min_bars:
            raise ValueError(f'INSUFFICIENT_LIVE_BARS: need at least {min_bars} bars for {sym}, have {len(eligible)}')
        self.symbols = [sym]
        self.symbol = sym
        self.timeframe = timeframe
        self.bar_minutes = timeframe_minutes(timeframe)
        self.source = f'market_read_model:{source_mode}'
        self.groups = {sym: eligible}
        self.length = len(eligible)
        self.id = hashlib.sha256(
            json.dumps({'source': self.source, 'timeframe': timeframe, 'rows': eligible}, sort_keys=True, separators=(',', ':')).encode()
        ).hexdigest()[:20]

    def prefix(self, symbol: str, index: int) -> list[dict[str, Any]]:
        if symbol.upper() != self.symbol:
            raise ValueError('Unknown symbol')
        return self.groups[self.symbol][: index + 1]

    def row(self, symbol: str, index: int) -> dict[str, Any]:
        if symbol.upper() != self.symbol:
            raise ValueError('Unknown symbol')
        return self.groups[self.symbol][index]

    def time(self, index: int) -> str:
        return self.groups[self.symbol][index]['available_at']


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
                    bar_end TEXT,
                    available_at TEXT,
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
            cols = {
                r['name']
                for r in self._store.db.execute('PRAGMA table_info(market_bars)').fetchall()
            }
            if 'bar_end' not in cols:
                self._store.db.execute('ALTER TABLE market_bars ADD COLUMN bar_end TEXT')
            if 'available_at' not in cols:
                self._store.db.execute('ALTER TABLE market_bars ADD COLUMN available_at TEXT')

    def ingest_event(
        self,
        ev: MarketEvent,
        *,
        timeframe: str | None = None,
        now: datetime | None = None,
    ) -> InstrumentMark:
        sym = ev.symbol.upper()
        tf = timeframe or getattr(ev, 'timeframe', None) or '1Min'
        delta = timeframe_delta(tf)
        pts = ev.provider_ts.astimezone(timezone.utc)
        rts = ev.receive_ts.astimezone(timezone.utc)
        ats = ev.available_at.astimezone(timezone.utc)
        check_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if pts > check_now + timedelta(seconds=MAX_FUTURE_CLOCK_SKEW_SECONDS):
            raise ValueError(f'FUTURE_TIMESTAMP_REJECTED: provider_ts {pts.isoformat()} exceeds clock skew tolerance')
        bar_end = pts + delta
        effective_avail = max(ats, bar_end) if ev.event_type == 'bar' else ats
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
            timeframe=tf,
            provider_ts=pts,
            receive_ts=rts,
            available_at=effective_avail,
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
                            provider, feed, event_id, raw_payload_hash, provider_ts, receive_ts,
                            bar_end, available_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source_mode, timeframe, symbol, bar_ts) DO UPDATE SET
                            open = excluded.open,
                            high = excluded.high,
                            low = excluded.low,
                            close = excluded.close,
                            volume = excluded.volume,
                            provider = excluded.provider,
                            feed = excluded.feed,
                            event_id = excluded.event_id,
                            raw_payload_hash = excluded.raw_payload_hash,
                            provider_ts = excluded.provider_ts,
                            receive_ts = excluded.receive_ts,
                            bar_end = excluded.bar_end,
                            available_at = excluded.available_at
                        WHERE excluded.receive_ts >= market_bars.receive_ts
                        """,
                        (
                            ev.source_mode,
                            tf,
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
                            bar_end.isoformat(),
                            effective_avail.isoformat(),
                        ),
                    )
                existing_row = self._store.db.execute(
                    'SELECT mark_json FROM market_marks WHERE source_mode = ? AND symbol = ?',
                    (ev.source_mode, sym),
                ).fetchone()
                should_update_mark = True
                if existing_row is not None:
                    existing_mark = InstrumentMark.model_validate_json(existing_row['mark_json'])
                    if (
                        existing_mark.provider == ev.provider
                        and existing_mark.feed == ev.feed
                        and pts < existing_mark.provider_ts.astimezone(timezone.utc)
                    ):
                        should_update_mark = False
                if should_update_mark:
                    self._store.db.execute(
                        """
                        INSERT INTO market_marks(source_mode, symbol, mark_json)
                        VALUES (?, ?, ?)
                        ON CONFLICT(source_mode, symbol) DO UPDATE SET mark_json = excluded.mark_json
                        """,
                        (ev.source_mode, sym, mark.model_dump_json()),
                    )
        return mark

    def bars(
        self,
        symbol: str,
        *,
        source_mode: str,
        timeframe: str = '1Min',
        provider: str | None = None,
        feed: str | None = None,
        as_of: datetime | None = None,
        limit: int = 240,
    ) -> list[dict[str, Any]]:
        delta = timeframe_delta(timeframe)
        clauses = ['source_mode = ?', 'timeframe = ?', 'symbol = ?']
        params: list[Any] = [source_mode, timeframe, symbol.upper()]
        if provider is not None:
            clauses.append('provider = ?')
            params.append(provider)
        if feed is not None:
            clauses.append('feed = ?')
            params.append(feed)
        where_sql = ' AND '.join(clauses)
        with self._lock:
            with self._store.lock:
                rows = self._store.db.execute(
                    f"""
                    SELECT * FROM (
                        SELECT * FROM market_bars
                        WHERE {where_sql}
                        ORDER BY bar_ts DESC
                        LIMIT ?
                    ) sub ORDER BY bar_ts ASC
                    """,
                    (*params, limit),
                ).fetchall()
                out: list[dict[str, Any]] = []
                as_of_utc = as_of.astimezone(timezone.utc) if as_of is not None else None
                for r in rows:
                    pts = _parse_utc(r['bar_ts'])
                    bar_end = _parse_utc(r['bar_end']) if r['bar_end'] else (pts + delta)
                    avail = _parse_utc(r['available_at']) if r['available_at'] else bar_end
                    if as_of_utc is not None and avail > as_of_utc + timedelta(seconds=MAX_FUTURE_CLOCK_SKEW_SECONDS):
                        continue
                    out.append({
                        'time': r['bar_ts'],
                        'open_at': pts.isoformat(),
                        'timestamp': bar_end.isoformat(),
                        'available_at': avail.isoformat(),
                        'timeframe': r['timeframe'],
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
                        'receive_ts': r['receive_ts'],
                    })
                return out

    def scanner(
        self,
        *,
        source_mode: str,
        timeframe: str = '1Min',
        provider: str | None = None,
        feed: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ['source_mode = ?', 'timeframe = ?']
        params: list[Any] = [source_mode, timeframe]
        if provider is not None:
            clauses.append('provider = ?')
            params.append(provider)
        if feed is not None:
            clauses.append('feed = ?')
            params.append(feed)
        where_sql = ' AND '.join(clauses)
        with self._lock:
            with self._store.lock:
                symbols = [
                    r['symbol']
                    for r in self._store.db.execute(
                        f'SELECT DISTINCT symbol FROM market_bars WHERE {where_sql} ORDER BY symbol',
                        tuple(params),
                    ).fetchall()
                ]
                out: list[dict[str, Any]] = []
                for sym in symbols:
                    b = self.bars(sym, source_mode=source_mode, timeframe=timeframe, provider=provider, feed=feed, limit=24)
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
                        'timeframe': timeframe,
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
        provider: str | None = None,
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
                    if feed and m.feed != feed:
                        continue
                    if provider and m.provider != provider:
                        continue
                    # Freshness MUST be evaluated against provider_ts, never receive_ts
                    raw_delta = (now_utc - m.provider_ts.astimezone(timezone.utc)).total_seconds()
                    if raw_delta < -MAX_FUTURE_CLOCK_SKEW_SECONDS:
                        marks[r['symbol']] = m.model_copy(update={'age_seconds': None, 'stale': True})
                    else:
                        age = max(0.0, raw_delta)
                        stale = age > max_age_seconds
                        marks[r['symbol']] = m.model_copy(update={'age_seconds': round(age, 3), 'stale': stale})

        healthy_feed = feed_state in ('LIVE', 'DELAYED', 'RECEIVING') and source_mode in ('live', 'delayed')
        integrity_ok = healthy_feed and len(marks) > 0 and all(
            (m.age_seconds is not None and not m.stale and m.price > 0) for m in marks.values()
        )
        return MarketSnapshot(
            source_mode=source_mode,
            feed=feed,
            feed_state=feed_state,
            marks=marks,
            integrity_ok=integrity_ok,
            as_of=now_utc,
        )
