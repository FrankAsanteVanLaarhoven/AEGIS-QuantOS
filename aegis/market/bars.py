"""Bar aggregation, deduplication, revision versioning, and gap accounting."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from .contracts import MarketEvent, NormalizedBar


def _floor_bar_ts(ts: datetime, bar_minutes: int) -> datetime:
    utc = ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    minute = (utc.minute // bar_minutes) * bar_minutes
    return utc.replace(minute=minute, second=0, microsecond=0)


class BarAggregator:
    """Builds versioned NormalizedBars from MarketEvents with watermark & deduplication."""

    def __init__(self, bar_minutes: int = 15) -> None:
        self.bar_minutes = bar_minutes
        self._seen_events: set[str] = set()
        self._revisions: dict[tuple[str, datetime], list[NormalizedBar]] = {}
        self.watermark_ts: datetime | None = None
        self.duplicate_count: int = 0

    def ingest(self, event: MarketEvent) -> NormalizedBar | None:
        if event.event_id in self._seen_events:
            self.duplicate_count += 1
            return None
        self._seen_events.add(event.event_id)

        bar_ts = _floor_bar_ts(event.provider_ts, self.bar_minutes)
        key = (event.symbol, bar_ts)
        coverage = 'single_exchange' if event.feed == 'iex' else ('consolidated' if event.feed == 'sip' else event.feed)

        price = event.close or event.price
        if price is None:
            return None
        o = event.open or price
        h = max(event.high or price, o, price)
        l = min(event.low or price, o, price)
        c = price
        vol = event.volume if event.volume is not None else (event.quantity or 0.0)

        if self.watermark_ts is None or event.provider_ts > self.watermark_ts:
            self.watermark_ts = event.provider_ts

        prior_list = self._revisions.get(key, [])
        if not prior_list or event.event_type == 'correction' or event.revision_id > prior_list[-1].revision_id:
            rev = (prior_list[-1].revision_id + 1) if (prior_list and event.event_type == 'correction' and event.revision_id <= prior_list[-1].revision_id) else event.revision_id
            if prior_list and event.event_type == 'bar' and event.revision_id == prior_list[-1].revision_id:
                # Aggregate intrabar trade/bar into current revision
                prev = prior_list[-1]
                bar = NormalizedBar(
                    symbol=event.symbol,
                    instrument_id=event.instrument_id,
                    bar_ts=bar_ts,
                    open=prev.open,
                    high=max(prev.high, h),
                    low=min(prev.low, l),
                    close=c,
                    volume=prev.volume + vol,
                    feed=event.feed,
                    coverage=coverage,
                    source_mode=event.source_mode,
                    revision_id=prev.revision_id,
                    watermark_ts=self.watermark_ts,
                    event_count=prev.event_count + 1,
                )
                prior_list[-1] = bar
            else:
                bar = NormalizedBar(
                    symbol=event.symbol,
                    instrument_id=event.instrument_id,
                    bar_ts=bar_ts,
                    open=o,
                    high=h,
                    low=l,
                    close=c,
                    volume=vol,
                    feed=event.feed,
                    coverage=coverage,
                    source_mode=event.source_mode,
                    revision_id=rev,
                    watermark_ts=self.watermark_ts,
                    event_count=1,
                )
                prior_list.append(bar)
                self._revisions[key] = prior_list
            return prior_list[-1]
        return prior_list[-1]

    def latest_bars(self, symbol: str) -> list[NormalizedBar]:
        items = [revs[-1] for (sym, _), revs in self._revisions.items() if sym == symbol]
        return sorted(items, key=lambda b: b.bar_ts)

    def revisions_for(self, symbol: str, bar_ts: datetime) -> list[NormalizedBar]:
        return list(self._revisions.get((symbol, _floor_bar_ts(bar_ts, self.bar_minutes)), []))

    def detect_gaps(self, symbol: str, expected_timestamps: Iterable[datetime]) -> list[datetime]:
        present = {ts for (sym, ts) in self._revisions if sym == symbol}
        return [ts for ts in expected_timestamps if _floor_bar_ts(ts, self.bar_minutes) not in present]

