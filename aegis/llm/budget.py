"""Thread-safe reservation-based token and cost budget ledger."""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class BudgetReservation:
    reservation_id: str
    job_id: str
    reserved_usd: float
    status: str  # RESERVED | SETTLED | RELEASED | UNKNOWN_OUTCOME
    actual_usd: float | None = None


class BudgetExceeded(ValueError):
    pass


class BudgetLedger:
    """Enforces per-job, per-day, and project-wide USD caps atomically."""

    def __init__(
        self,
        *,
        job_budget_usd: float | None = None,
        daily_budget_usd: float | None = None,
        project_budget_usd: float | None = None,
    ) -> None:
        self.job_budget_usd = job_budget_usd
        self.daily_budget_usd = daily_budget_usd
        self.project_budget_usd = project_budget_usd
        self._lock = threading.Lock()
        self._reservations: dict[str, BudgetReservation] = {}
        self._job_spent: dict[str, float] = {}
        self._daily_spent: dict[str, float] = {}
        self._project_spent: float = 0.0

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime('%Y-%m-%d')

    def reserve(self, job_id: str, estimated_usd: float) -> BudgetReservation:
        if estimated_usd <= 0:
            raise ValueError('Estimated reservation must be positive')
        with self._lock:
            day = self._today()
            if self.job_budget_usd is None or self.daily_budget_usd is None or self.project_budget_usd is None:
                raise BudgetExceeded('BLOCKED_BUDGET: Finite job, daily, and project budgets are required')
            if self._job_spent.get(job_id, 0.0) + estimated_usd > self.job_budget_usd + 1e-12:
                raise BudgetExceeded(f'BLOCKED_BUDGET: Job {job_id} budget exceeded')
            if self._daily_spent.get(day, 0.0) + estimated_usd > self.daily_budget_usd + 1e-12:
                raise BudgetExceeded('BLOCKED_BUDGET: Daily budget exceeded')
            if self._project_spent + estimated_usd > self.project_budget_usd + 1e-12:
                raise BudgetExceeded('BLOCKED_BUDGET: Project budget exceeded')

            rid = str(uuid.uuid4())
            res = BudgetReservation(reservation_id=rid, job_id=job_id, reserved_usd=estimated_usd, status='RESERVED')
            self._reservations[rid] = res
            self._job_spent[job_id] = self._job_spent.get(job_id, 0.0) + estimated_usd
            self._daily_spent[day] = self._daily_spent.get(day, 0.0) + estimated_usd
            self._project_spent += estimated_usd
            return res

    def reconcile(self, reservation_id: str, *, actual_usd: float | None, unknown_outcome: bool = False) -> BudgetReservation:
        with self._lock:
            res = self._reservations[reservation_id]
            if res.status != 'RESERVED':
                return res
            day = self._today()
            if unknown_outcome or actual_usd is None:
                # Unknown cost is NOT zero: keep full reservation locked
                res.status = 'UNKNOWN_OUTCOME'
                res.actual_usd = res.reserved_usd
                return res
            delta = actual_usd - res.reserved_usd
            self._job_spent[res.job_id] = max(0.0, self._job_spent.get(res.job_id, 0.0) + delta)
            self._daily_spent[day] = max(0.0, self._daily_spent.get(day, 0.0) + delta)
            self._project_spent = max(0.0, self._project_spent + delta)
            res.status = 'SETTLED'
            res.actual_usd = actual_usd
            return res

    def snapshot(self) -> dict[str, object]:
        with self._lock:
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
