"""Thread-safe, optionally SQLite-persistent reservation-based token and cost budget ledger."""
from __future__ import annotations

import math
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def _require_finite_positive(value: float, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f'{label} must be a finite positive number')
    f = float(value)
    if not math.isfinite(f) or f <= 0.0:
        raise ValueError(f'{label} must be finite and positive')
    return f


def _require_finite_non_negative(value: float, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f'{label} must be a finite non-negative number')
    f = float(value)
    if not math.isfinite(f) or f < 0.0:
        raise ValueError(f'{label} must be finite and non-negative')
    return f


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
    """Enforces per-job, per-day, and project-wide USD caps atomically with originating-day accounting."""

    _STORE_KEY = 'llm_budget_ledger_v1'

    def __init__(
        self,
        *,
        job_budget_usd: float | None = None,
        daily_budget_usd: float | None = None,
        project_budget_usd: float | None = None,
        store: Any = None,
    ) -> None:
        self.job_budget_usd = _require_finite_positive(job_budget_usd, 'job_budget_usd') if job_budget_usd is not None else None
        self.daily_budget_usd = _require_finite_positive(daily_budget_usd, 'daily_budget_usd') if daily_budget_usd is not None else None
        self.project_budget_usd = _require_finite_positive(project_budget_usd, 'project_budget_usd') if project_budget_usd is not None else None
        self._store = store
        self._lock = threading.RLock()
        self._reservations: dict[str, BudgetReservation] = {}
        self._job_spent: dict[str, float] = {}
        self._daily_spent: dict[str, float] = {}
        self._project_spent: float = 0.0
        self._load_from_store()

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime('%Y-%m-%d')

    def _load_from_store(self) -> None:
        if self._store is None:
            return
        raw = self._store.get(self._STORE_KEY)
        if not isinstance(raw, dict):
            return
        self._job_spent = {k: float(v) for k, v in (raw.get('job_spent') or {}).items()}
        self._daily_spent = {k: float(v) for k, v in (raw.get('daily_spent') or {}).items()}
        self._project_spent = float(raw.get('project_spent') or 0.0)
        loaded_res: dict[str, BudgetReservation] = {}
        for rid, rec in (raw.get('reservations') or {}).items():
            loaded_res[rid] = BudgetReservation(
                reservation_id=rec['reservation_id'],
                job_id=rec['job_id'],
                reserved_usd=float(rec['reserved_usd']),
                status=rec['status'],
                originating_day=rec['originating_day'],
                actual_usd=float(rec['actual_usd']) if rec.get('actual_usd') is not None else None,
            )
        self._reservations = loaded_res

    def _persist_to_store(self) -> None:
        if self._store is None:
            return
        self._store.put(
            self._STORE_KEY,
            {
                'job_spent': self._job_spent,
                'daily_spent': self._daily_spent,
                'project_spent': self._project_spent,
                'reservations': {
                    rid: {
                        'reservation_id': r.reservation_id,
                        'job_id': r.job_id,
                        'reserved_usd': r.reserved_usd,
                        'status': r.status,
                        'originating_day': r.originating_day,
                        'actual_usd': r.actual_usd,
                    }
                    for rid, r in self._reservations.items()
                },
            },
        )

    def reserve(self, job_id: str, estimated_usd: float) -> BudgetReservation:
        est = _require_finite_positive(estimated_usd, 'estimated_usd')
        with self._lock:
            self._load_from_store()
            day = self._today()
            if self.job_budget_usd is None or self.daily_budget_usd is None or self.project_budget_usd is None:
                raise BudgetExceeded('BLOCKED_BUDGET: Finite job, daily, and project budgets are required')
            if self._job_spent.get(job_id, 0.0) + est > self.job_budget_usd + 1e-12:
                raise BudgetExceeded(f'BLOCKED_BUDGET: Job {job_id} budget exceeded')
            if self._daily_spent.get(day, 0.0) + est > self.daily_budget_usd + 1e-12:
                raise BudgetExceeded('BLOCKED_BUDGET: Daily budget exceeded')
            if self._project_spent + est > self.project_budget_usd + 1e-12:
                raise BudgetExceeded('BLOCKED_BUDGET: Project budget exceeded')

            rid = str(uuid.uuid4())
            res = BudgetReservation(
                reservation_id=rid,
                job_id=job_id,
                reserved_usd=est,
                status='RESERVED',
                originating_day=day,
            )
            self._reservations[rid] = res
            self._job_spent[job_id] = self._job_spent.get(job_id, 0.0) + est
            self._daily_spent[day] = self._daily_spent.get(day, 0.0) + est
            self._project_spent += est
            self._persist_to_store()
            return res

    def reconcile(self, reservation_id: str, *, actual_usd: float | None, unknown_outcome: bool = False) -> BudgetReservation:
        if actual_usd is not None:
            actual_usd = _require_finite_non_negative(actual_usd, 'actual_usd')
        with self._lock:
            self._load_from_store()
            res = self._reservations[reservation_id]
            if res.status in ('SETTLED', 'RELEASED'):
                return res
            if unknown_outcome or actual_usd is None:
                # Unknown cost is NOT zero: keep full reservation locked until resolved
                res.status = 'UNKNOWN_OUTCOME'
                res.actual_usd = res.reserved_usd
                self._persist_to_store()
                return res
            # Booked amount before settlement is res.reserved_usd (for both RESERVED and UNKNOWN_OUTCOME)
            delta = actual_usd - res.reserved_usd
            orig_day = res.originating_day
            self._job_spent[res.job_id] = max(0.0, self._job_spent.get(res.job_id, 0.0) + delta)
            self._daily_spent[orig_day] = max(0.0, self._daily_spent.get(orig_day, 0.0) + delta)
            self._project_spent = max(0.0, self._project_spent + delta)
            res.status = 'SETTLED'
            res.actual_usd = actual_usd
            self._persist_to_store()
            return res

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            self._load_from_store()
            day = self._today()
            return {
                'job_budget_usd': self.job_budget_usd,
                'daily_budget_usd': self.daily_budget_usd,
                'project_budget_usd': self.project_budget_usd,
                'daily_spent_usd': round(self._daily_spent.get(day, 0.0), 6),
                'project_spent_usd': round(self._project_spent, 6),
                'active_reservations': sum(1 for r in self._reservations.values() if r.status == 'RESERVED'),
                'unknown_outcomes': sum(1 for r in self._reservations.values() if r.status == 'UNKNOWN_OUTCOME'),
            }
