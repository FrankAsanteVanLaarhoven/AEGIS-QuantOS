"""Market-data supervisor with bounded queues, backoff, gap accounting, and honest health states."""
from __future__ import annotations

import random
from collections import deque
from datetime import datetime, timezone
from typing import Any, Literal

from .bars import BarAggregator
from .calendar import market_session_state
from .contracts import MarketEvent

FeedHealthState = Literal[
    'DISCONNECTED',
    'CONNECTING',
    'AUTHENTICATED',
    'RECEIVING',
    'STALE',
    'MARKET_CLOSED',
    'BLOCKED_CREDENTIAL',
    'BLOCKED_ENTITLEMENT',
]


class MarketDataSupervisor:
    """Tracks feed connection, bounded event queue, staleness, and backfill gaps."""

    def __init__(
        self,
        *,
        symbols: list[str] | None = None,
        feed: Literal['iex', 'sip'] = 'iex',
        source_mode: Literal['live', 'delayed', 'historical', 'synthetic'] = 'live',
        max_queue: int = 4096,
        stale_after_seconds: float = 30.0,
        allow_synthetic_fallback: bool = False,
    ) -> None:
        self.symbols = list(symbols or ['NVDA', 'AMAT', 'LRCX', 'TER', 'SPY'])
        self.feed = feed
        self.source_mode = source_mode
        self.allow_synthetic_fallback = allow_synthetic_fallback
        self.stale_after_seconds = stale_after_seconds
        self.queue: deque[MarketEvent] = deque(maxlen=max_queue)
        self.dropped_events: int = 0
        self.aggregator = BarAggregator(bar_minutes=15)
        self.state: FeedHealthState = 'DISCONNECTED'
        self.reconnect_attempts: int = 0
        self.last_event_ts: datetime | None = None
        self.last_receive_ts: datetime | None = None
        self.last_error: str | None = None
        self.gaps_detected: list[dict[str, Any]] = []

    def mark_blocked_credential(self, reason: str = 'Alpaca credentials missing') -> None:
        self.state = 'BLOCKED_CREDENTIAL'
        self.last_error = reason

    def mark_blocked_entitlement(self, reason: str = 'Feed entitlement denied') -> None:
        self.state = 'BLOCKED_ENTITLEMENT'
        self.last_error = reason

    def mark_disconnected(self, reason: str | None = None) -> None:
        self.state = 'DISCONNECTED'
        self.reconnect_attempts += 1
        if reason:
            self.last_error = reason

    def mark_authenticated(self) -> None:
        self.state = 'AUTHENTICATED'
        self.reconnect_attempts = 0
        self.last_error = None

    def next_backoff_seconds(self, base: float = 1.0, cap: float = 60.0, rng: random.Random | None = None) -> float:
        r = rng or random.Random(self.reconnect_attempts)
        exp = min(cap, base * (2 ** min(self.reconnect_attempts, 10)))
        return round(exp * (0.5 + 0.5 * r.random()), 3)

    def push_event(self, event: MarketEvent) -> None:
        if len(self.queue) == self.queue.maxlen:
            self.dropped_events += 1
        self.queue.append(event)
        self.aggregator.ingest(event)
        self.last_event_ts = event.provider_ts
        self.last_receive_ts = event.receive_ts
        self.state = 'RECEIVING'
        self.last_error = None

    def evaluate_health(self, now: datetime | None = None) -> FeedHealthState:
        now_utc = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
        if self.state in ('BLOCKED_CREDENTIAL', 'BLOCKED_ENTITLEMENT'):
            return self.state
        session = market_session_state(now_utc)
        if session == 'MARKET_CLOSED' and self.state in ('DISCONNECTED', 'STALE', 'AUTHENTICATED', 'RECEIVING'):
            return 'MARKET_CLOSED'
        if self.last_receive_ts is not None:
            age = (now_utc - self.last_receive_ts.astimezone(timezone.utc)).total_seconds()
            if age > self.stale_after_seconds and session == 'OPEN':
                self.state = 'STALE'
        return self.state

    def record_gaps(self, symbol: str, missing: list[datetime]) -> None:
        for ts in missing:
            self.gaps_detected.append({'symbol': symbol, 'bar_ts': ts.isoformat(), 'recovered': False})

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        now_utc = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
        health = self.evaluate_health(now_utc)
        age = None
        if self.last_receive_ts is not None:
            age = round((now_utc - self.last_receive_ts.astimezone(timezone.utc)).total_seconds(), 3)
        return {
            'state': health,
            'session': market_session_state(now_utc),
            'source_mode': self.source_mode,
            'feed': self.feed,
            'coverage': 'single_exchange' if self.feed == 'iex' else 'consolidated',
            'symbols': self.symbols,
            'allow_synthetic_fallback': self.allow_synthetic_fallback,
            'queue_depth': len(self.queue),
            'dropped_events': self.dropped_events,
            'duplicate_events': self.aggregator.duplicate_count,
            'reconnect_attempts': self.reconnect_attempts,
            'last_event_ts': self.last_event_ts.isoformat() if self.last_event_ts else None,
            'last_receive_ts': self.last_receive_ts.isoformat() if self.last_receive_ts else None,
            'age_seconds': age,
            'unrecovered_gaps': sum(1 for g in self.gaps_detected if not g['recovered']),
            'last_error': self.last_error,
        }

