from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
import httpx

from aegis.market import (
    BarAggregator,
    MarketDataSupervisor,
    MarketEvent,
    forward_bar_timestamps,
    is_trading_day,
    market_session_state,
    payload_sha256,
    session_bounds,
)
from aegis.providers.alpaca_market import AlpacaMarketAdapter
from aegis.secrets import SecretResolver
from aegis.settings import AegisSettings

NY = ZoneInfo('America/New_York')


def test_exchange_calendar_holidays_early_close_and_dst():
    # Independence Day 2026 is Saturday -> observed Friday July 3; Christmas 2026 is Friday
    assert not is_trading_day(date(2026, 7, 3))
    assert not is_trading_day(date(2026, 12, 25))
    # Day after Thanksgiving 2026 (Nov 27) is early close 13:00 ET
    bounds = session_bounds(date(2026, 11, 27))
    assert bounds is not None
    assert bounds[1].hour == 13 and bounds[1].minute == 0
    # Open session check during regular hours (Monday Nov 16 2026, 10:00 ET)
    assert market_session_state(datetime(2026, 11, 16, 10, 0, tzinfo=NY)) == 'OPEN'
    # Saturday is MARKET_CLOSED
    assert market_session_state(datetime(2026, 11, 14, 12, 0, tzinfo=NY)) == 'MARKET_CLOSED'


def test_forward_bar_timestamps_crosses_weekend():
    # Friday 15:45 ET -> next 2 15-min bars must be Monday 09:30 ET and 09:45 ET
    fri = datetime(2026, 11, 13, 15, 45, tzinfo=NY).astimezone(timezone.utc)
    fwd = forward_bar_timestamps(fri, horizon=2, bar_minutes=15)
    assert len(fwd) == 2
    assert fwd[0].astimezone(NY) == datetime(2026, 11, 16, 9, 30, tzinfo=NY)
    assert fwd[1].astimezone(NY) == datetime(2026, 11, 16, 9, 45, tzinfo=NY)


def test_bar_aggregator_dedup_revisions_and_gaps():
    agg = BarAggregator(bar_minutes=15)
    t0 = datetime(2026, 11, 16, 14, 30, tzinfo=timezone.utc)
    t1 = datetime(2026, 11, 16, 14, 45, tzinfo=timezone.utc)
    t2 = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)
    ev1 = MarketEvent(
        event_id='e1', instrument_id='US_EQUITY:NVDA', symbol='NVDA', venue='IEX',
        source='alpaca_iex', provider='alpaca', feed='iex', source_mode='live',
        provider_ts=t0, receive_ts=t0, available_at=t0,
        open=140.0, high=141.0, low=139.5, close=140.5, volume=1000,
        raw_payload_hash=payload_sha256({'id': 'e1'}),
    )
    assert agg.ingest(ev1) is not None
    # Duplicate event_id ignored
    assert agg.ingest(ev1) is None
    assert agg.duplicate_count == 1

    # Correction creates revision 2 without overwriting revision 1
    ev_corr = ev1.model_copy(update={'event_id': 'e1_corr', 'event_type': 'correction', 'close': 140.8, 'revision_id': 2})
    agg.ingest(ev_corr)
    revs = agg.revisions_for('NVDA', t0)
    assert len(revs) == 2
    assert revs[0].close == 140.5 and revs[1].close == 140.8
    assert revs[0].coverage == 'single_exchange'

    # Gap detection finds missing t1, t2
    gaps = agg.detect_gaps('NVDA', [t0, t1, t2])
    assert gaps == [t1, t2]


def test_supervisor_ws_and_rest_backfill_and_entitlement_denial():
    settings = AegisSettings.from_env({'AEGIS_DATA_MODE': 'live', 'ALPACA_DATA_FEED': 'iex'})
    sup = MarketDataSupervisor(feed='iex', source_mode='live', stale_after_seconds=10.0)
    resolver = SecretResolver({'ALPACA_API_KEY': 'test_alp_key_1234', 'ALPACA_SECRET_KEY': 'test_alp_sec_1234'})
    adapter = AlpacaMarketAdapter(settings, resolver, sup)

    # Auth + bar ingestion
    open_ts = datetime(2026, 11, 16, 10, 0, tzinfo=NY).astimezone(timezone.utc)
    adapter.handle_ws_messages([{'T': 'success', 'msg': 'authenticated'}], receive_ts=open_ts)
    assert sup.state == 'AUTHENTICATED'
    adapter.handle_ws_messages(
        [{'T': 'b', 'S': 'NVDA', 't': '2026-11-16T15:00:00Z', 'o': 140, 'h': 142, 'l': 139, 'c': 141, 'v': 5000}],
        receive_ts=open_ts,
    )
    assert sup.status(now=open_ts)['state'] == 'RECEIVING'
    # Stale after 20s during open session
    later = datetime(2026, 11, 16, 10, 0, 25, tzinfo=NY).astimezone(timezone.utc)
    assert sup.evaluate_health(now=later) == 'STALE'

    # Entitlement denial must set BLOCKED_ENTITLEMENT, never synthetic
    adapter.handle_ws_messages([{'T': 'error', 'code': 403, 'msg': 'subscription does not permit SIP'}])
    assert sup.state == 'BLOCKED_ENTITLEMENT'
    assert sup.allow_synthetic_fallback is False

    # Missing credentials -> BLOCKED_CREDENTIAL
    sup2 = MarketDataSupervisor(feed='iex', source_mode='live')
    adapter2 = AlpacaMarketAdapter(settings, SecretResolver({}), sup2)
    res = adapter2.backfill_bars(['NVDA'], open_ts, later)
    assert res['status'] == 'BLOCKED_CREDENTIAL'
    assert sup2.state == 'BLOCKED_CREDENTIAL'

    # Mock REST backfill success
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={'bars': {'NVDA': [{'t': '2026-11-16T15:15:00Z', 'o': 141, 'h': 143, 'l': 140, 'c': 142.5, 'v': 3200}]}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sup3 = MarketDataSupervisor(feed='iex', source_mode='live')
    adapter3 = AlpacaMarketAdapter(settings, resolver, sup3)
    b_res = adapter3.backfill_bars(['NVDA'], open_ts, later, client=client)
    assert b_res['status'] == 'OK' and b_res['bars_ingested'] == 1

