"""Database-transactional reservation-based token and cost budget ledger (exact nano-USD accounting)."""
from __future__ import annotations

import math
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from aegis.store import Store

NANO_PER_USD = 1_000_000_000


def _to_nano_positive(value: float, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f'{label} must be a finite positive number')
    f = float(value)
    if not math.isfinite(f) or f <= 0.0:
        raise ValueError(f'{label} must be finite and positive')
    nano = int((Decimal(str(f)) * NANO_PER_USD).to_integral_value(rounding=ROUND_HALF_UP))
    if nano <= 0:
        raise ValueError(f'{label} must be at least 1e-9 USD')
    return nano


def _to_nano_non_negative(value: float, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f'{label} must be a finite non-negative number')
    f = float(value)
    if not math.isfinite(f) or f < 0.0:
        raise ValueError(f'{label} must be finite and non-negative')
    return int((Decimal(str(f)) * NANO_PER_USD).to_integral_value(rounding=ROUND_HALF_UP))


def _nano_to_usd(nano: int) -> float:
    return float(Decimal(nano) / Decimal(NANO_PER_USD))


@dataclass
class BudgetReservation:
    reservation_id: str
    job_id: str
    reserved_usd: float
    status: str  # RESERVED | SETTLED | RELEASED | UNKNOWN_OUTCOME
    originating_day: str
    actual_usd: float | None = None


class BudgetExceeded(ValueError):
    pass


class BudgetLedger:
    """Enforces per-job, per-day, and project-wide USD caps atomically inside SQLite transactions."""

    def __init__(
        self,
        *,
        job_budget_usd: float | None = None,
        daily_budget_usd: float | None = None,
        project_budget_usd: float | None = None,
        store: Store | None = None,
    ) -> None:
        self._job_cap_nano = _to_nano_positive(job_budget_usd, 'job_budget_usd') if job_budget_usd is not None else None
        self._daily_cap_nano = _to_nano_positive(daily_budget_usd, 'daily_budget_usd') if daily_budget_usd is not None else None
        self._project_cap_nano = _to_nano_positive(project_budget_usd, 'project_budget_usd') if project_budget_usd is not None else None
        self.job_budget_usd = _nano_to_usd(self._job_cap_nano) if self._job_cap_nano is not None else None
        self.daily_budget_usd = _nano_to_usd(self._daily_cap_nano) if self._daily_cap_nano is not None else None
        self.project_budget_usd = _nano_to_usd(self._project_cap_nano) if self._project_cap_nano is not None else None
        self._store = store if store is not None else Store(':memory:')
        self._lock = threading.RLock()
        self._ensure_schema()

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime('%Y-%m-%d')

    def _ensure_schema(self) -> None:
        with self._store.tx():
            self._store.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS budget_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    originating_day TEXT NOT NULL,
                    reserved_nano INTEGER NOT NULL,
                    actual_nano INTEGER,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS budget_job_spent (
                    job_id TEXT PRIMARY KEY,
                    spent_nano INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS budget_daily_spent (
                    day TEXT PRIMARY KEY,
                    spent_nano INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS budget_project_spent (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    spent_nano INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO budget_project_spent(id, spent_nano) VALUES (1, 0);
                """
            )

    def reserve(self, job_id: str, estimated_usd: float) -> BudgetReservation:
        est_nano = _to_nano_positive(estimated_usd, 'estimated_usd')
        with self._lock:
            day = self._today()
            if self._job_cap_nano is None or self._daily_cap_nano is None or self._project_cap_nano is None:
                raise BudgetExceeded('BLOCKED_BUDGET: Finite job, daily, and project budgets are required')

            with self._store.tx():
                job_row = self._store.db.execute(
                    'SELECT spent_nano FROM budget_job_spent WHERE job_id = ?', (job_id,)
                ).fetchone()
                day_row = self._store.db.execute(
                    'SELECT spent_nano FROM budget_daily_spent WHERE day = ?', (day,)
                ).fetchone()
                proj_row = self._store.db.execute(
                    'SELECT spent_nano FROM budget_project_spent WHERE id = 1'
                ).fetchone()

                job_spent = int(job_row['spent_nano']) if job_row else 0
                day_spent = int(day_row['spent_nano']) if day_row else 0
                proj_spent = int(proj_row['spent_nano']) if proj_row else 0

                if job_spent + est_nano > self._job_cap_nano:
                    raise BudgetExceeded(f'BLOCKED_BUDGET: Job {job_id} budget exceeded')
                if day_spent + est_nano > self._daily_cap_nano:
                    raise BudgetExceeded('BLOCKED_BUDGET: Daily budget exceeded')
                if proj_spent + est_nano > self._project_cap_nano:
                    raise BudgetExceeded('BLOCKED_BUDGET: Project budget exceeded')

                rid = str(uuid.uuid4())
                self._store.db.execute(
                    'INSERT INTO budget_reservations(reservation_id, job_id, originating_day, reserved_nano, actual_nano, status) '
                    'VALUES (?, ?, ?, ?, NULL, ?)',
                    (rid, job_id, day, est_nano, 'RESERVED'),
                )
                self._store.db.execute(
                    'INSERT INTO budget_job_spent(job_id, spent_nano) VALUES (?, ?) '
                    'ON CONFLICT(job_id) DO UPDATE SET spent_nano = spent_nano + excluded.spent_nano',
                    (job_id, est_nano),
                )
                self._store.db.execute(
                    'INSERT INTO budget_daily_spent(day, spent_nano) VALUES (?, ?) '
                    'ON CONFLICT(day) DO UPDATE SET spent_nano = spent_nano + excluded.spent_nano',
                    (day, est_nano),
                )
                self._store.db.execute(
                    'UPDATE budget_project_spent SET spent_nano = spent_nano + ? WHERE id = 1',
                    (est_nano,),
                )
                return BudgetReservation(
                    reservation_id=rid,
                    job_id=job_id,
                    reserved_usd=_nano_to_usd(est_nano),
                    status='RESERVED',
                    originating_day=day,
                )

    def reconcile(self, reservation_id: str, *, actual_usd: float | None, unknown_outcome: bool = False) -> BudgetReservation:
        actual_nano = _to_nano_non_negative(actual_usd, 'actual_usd') if actual_usd is not None else None
        with self._lock:
            with self._store.tx():
                row = self._store.db.execute(
                    'SELECT * FROM budget_reservations WHERE reservation_id = ?',
                    (reservation_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(reservation_id)
                reserved_nano = int(row['reserved_nano'])
                job_id = str(row['job_id'])
                orig_day = str(row['originating_day'])
                status = str(row['status'])

                if status in ('SETTLED', 'RELEASED'):
                    return BudgetReservation(
                        reservation_id=reservation_id,
                        job_id=job_id,
                        reserved_usd=_nano_to_usd(reserved_nano),
                        status=status,
                        originating_day=orig_day,
                        actual_usd=_nano_to_usd(int(row['actual_nano'])) if row['actual_nano'] is not None else None,
                    )

                if unknown_outcome or actual_nano is None:
                    self._store.db.execute(
                        'UPDATE budget_reservations SET status = ?, actual_nano = ? WHERE reservation_id = ?',
                        ('UNKNOWN_OUTCOME', reserved_nano, reservation_id),
                    )
                    return BudgetReservation(
                        reservation_id=reservation_id,
                        job_id=job_id,
                        reserved_usd=_nano_to_usd(reserved_nano),
                        status='UNKNOWN_OUTCOME',
                        originating_day=orig_day,
                        actual_usd=_nano_to_usd(reserved_nano),
                    )

                delta_nano = actual_nano - reserved_nano
                self._store.db.execute(
                    'UPDATE budget_job_spent SET spent_nano = MAX(0, spent_nano + ?) WHERE job_id = ?',
                    (delta_nano, job_id),
                )
                self._store.db.execute(
                    'UPDATE budget_daily_spent SET spent_nano = MAX(0, spent_nano + ?) WHERE day = ?',
                    (delta_nano, orig_day),
                )
                self._store.db.execute(
                    'UPDATE budget_project_spent SET spent_nano = MAX(0, spent_nano + ?) WHERE id = 1',
                    (delta_nano,),
                )
                self._store.db.execute(
                    'UPDATE budget_reservations SET status = ?, actual_nano = ? WHERE reservation_id = ?',
                    ('SETTLED', actual_nano, reservation_id),
                )
                return BudgetReservation(
                    reservation_id=reservation_id,
                    job_id=job_id,
                    reserved_usd=_nano_to_usd(reserved_nano),
                    status='SETTLED',
                    originating_day=orig_day,
                    actual_usd=_nano_to_usd(actual_nano),
                )

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            day = self._today()
            with self._store.lock:
                day_row = self._store.db.execute('SELECT spent_nano FROM budget_daily_spent WHERE day = ?', (day,)).fetchone()
                proj_row = self._store.db.execute('SELECT spent_nano FROM budget_project_spent WHERE id = 1').fetchone()
                active_row = self._store.db.execute("SELECT COUNT(*) AS c FROM budget_reservations WHERE status = 'RESERVED'").fetchone()
                unk_row = self._store.db.execute("SELECT COUNT(*) AS c FROM budget_reservations WHERE status = 'UNKNOWN_OUTCOME'").fetchone()
                total_row = self._store.db.execute('SELECT COUNT(*) AS c FROM budget_reservations').fetchone()
                return {
                    'job_budget_usd': self.job_budget_usd,
                    'daily_budget_usd': self.daily_budget_usd,
                    'project_budget_usd': self.project_budget_usd,
                    'daily_spent_usd': round(_nano_to_usd(int(day_row['spent_nano']) if day_row else 0), 9),
                    'project_spent_usd': round(_nano_to_usd(int(proj_row['spent_nano']) if proj_row else 0), 9),
                    'active_reservations': int(active_row['c']) if active_row else 0,
                    'unknown_outcomes': int(unk_row['c']) if unk_row else 0,
                    'reservation_count': int(total_row['c']) if total_row else 0,
                }
