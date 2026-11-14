"""US Equity exchange calendar with holidays, early closes, DST, and forward timestamps."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo('America/New_York')
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        d = date(year, 12, 31)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    offset = (d.weekday() - weekday) % 7
    return d - timedelta(days=offset)


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _easter_sunday(year: int) -> date:
    # Anonymous Gregorian algorithm
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def nyse_holidays(year: int) -> set[date]:
    good_friday = _easter_sunday(year) - timedelta(days=2)
    holidays = {
        _observed(date(year, 1, 1)),      # New Year's Day
        _nth_weekday(year, 1, 0, 3),      # MLK Day
        _nth_weekday(year, 2, 0, 3),      # Presidents' Day
        good_friday,                      # Good Friday
        _last_weekday(year, 5, 0),        # Memorial Day
        _observed(date(year, 7, 4)),      # Independence Day
        _nth_weekday(year, 9, 0, 1),      # Labor Day
        _nth_weekday(year, 11, 3, 4),     # Thanksgiving
        _observed(date(year, 12, 25)),    # Christmas
    }
    if year >= 2022:
        holidays.add(_observed(date(year, 6, 19)))  # Juneteenth
    return holidays


def nyse_early_closes(year: int) -> set[date]:
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    day_after_thanksgiving = thanksgiving + timedelta(days=1)
    early = {day_after_thanksgiving}
    july3 = date(year, 7, 3)
    if july3.weekday() < 5 and july3 not in nyse_holidays(year):
        early.add(july3)
    xmas_eve = date(year, 12, 24)
    if xmas_eve.weekday() < 5 and xmas_eve not in nyse_holidays(year):
        early.add(xmas_eve)
    return early


def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    return d not in nyse_holidays(d.year)


def session_bounds(d: date) -> tuple[datetime, datetime] | None:
    if not is_trading_day(d):
        return None
    close_t = EARLY_CLOSE if d in nyse_early_closes(d.year) else REGULAR_CLOSE
    start = datetime.combine(d, REGULAR_OPEN, tzinfo=NY_TZ)
    end = datetime.combine(d, close_t, tzinfo=NY_TZ)
    return start, end


def market_session_state(ts: datetime | None = None) -> Literal['OPEN', 'PRE_MARKET', 'AFTER_HOURS', 'MARKET_CLOSED']:
    now_utc = ts.astimezone(timezone.utc) if ts else datetime.now(timezone.utc)
    now_ny = now_utc.astimezone(NY_TZ)
    d = now_ny.date()
    bounds = session_bounds(d)
    if bounds is None:
        return 'MARKET_CLOSED'
    start, end = bounds
    if start <= now_ny < end:
        return 'OPEN'
    if datetime.combine(d, time(4, 0), tzinfo=NY_TZ) <= now_ny < start:
        return 'PRE_MARKET'
    if end <= now_ny < datetime.combine(d, time(20, 0), tzinfo=NY_TZ):
        return 'AFTER_HOURS'
    return 'MARKET_CLOSED'


def forward_bar_timestamps(
    last_ts: datetime,
    horizon: int,
    bar_minutes: int = 15,
) -> list[datetime]:
    """Generate future exchange-calendar timestamps strictly after last_ts.

    Contains calendar information only — never peeks at future price rows.
    """
    if horizon <= 0:
        return []
    cur = last_ts.astimezone(NY_TZ) if last_ts.tzinfo else last_ts.replace(tzinfo=timezone.utc).astimezone(NY_TZ)
    step = timedelta(minutes=bar_minutes)
    out: list[datetime] = []
    candidate = cur + step
    safety = 0
    while len(out) < horizon and safety < horizon * 500:
        safety += 1
        d = candidate.date()
        bounds = session_bounds(d)
        if bounds is None:
            # Advance to next day's open
            next_d = d + timedelta(days=1)
            candidate = datetime.combine(next_d, REGULAR_OPEN, tzinfo=NY_TZ)
            continue
        start, end = bounds
        if candidate < start:
            candidate = start
        if candidate >= end:
            next_d = d + timedelta(days=1)
            candidate = datetime.combine(next_d, REGULAR_OPEN, tzinfo=NY_TZ)
            continue
        out.append(candidate.astimezone(timezone.utc))
        candidate = candidate + step
    return out
