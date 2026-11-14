from __future__ import annotations

import multiprocessing
import threading
from pathlib import Path

import pytest

from aegis.llm.budget import BudgetExceeded, BudgetLedger
from aegis.store import Store


def _mp_worker(db_path: str, worker_id: int, amount: float, barrier_ready, queue) -> None:
    ledger = BudgetLedger(job_budget_usd=1.0, daily_budget_usd=1.0, project_budget_usd=1.0, store=Store(db_path))
    barrier_ready.wait(timeout=5)
    try:
        res = ledger.reserve(f'job_mp_{worker_id}', amount)
        queue.put(('APPROVED', res.reservation_id, res.reserved_usd))
    except BudgetExceeded:
        queue.put(('DENIED', None, 0.0))


def test_f3_120_synchronized_two_connection_schedules_never_over_approve(tmp_path: Path):
    for i in range(120):
        db = tmp_path / f'sched_{i}.sqlite'
        b1 = BudgetLedger(job_budget_usd=1.0, daily_budget_usd=1.0, project_budget_usd=1.0, store=Store(db))
        b2 = BudgetLedger(job_budget_usd=1.0, daily_budget_usd=1.0, project_budget_usd=1.0, store=Store(db))
        barrier = threading.Barrier(2)
        outcomes: list[tuple[str, float]] = []

        def run(ledger: BudgetLedger, jid: str) -> None:
            barrier.wait(timeout=2)
            try:
                res = ledger.reserve(jid, 0.75)
                outcomes.append(('APPROVED', res.reserved_usd))
            except BudgetExceeded:
                outcomes.append(('DENIED', 0.0))

        t1 = threading.Thread(target=run, args=(b1, 'j1'))
        t2 = threading.Thread(target=run, args=(b2, 'j2'))
        t1.start(); t2.start(); t1.join(); t2.join()

        approved = [amt for st, amt in outcomes if st == 'APPROVED']
        denied = [st for st, _ in outcomes if st == 'DENIED']
        assert len(approved) == 1, f'Schedule {i} over-approved: {outcomes}'
        assert len(denied) == 1
        snap = BudgetLedger(job_budget_usd=1.0, daily_budget_usd=1.0, project_budget_usd=1.0, store=Store(db)).snapshot()
        assert snap['project_spent_usd'] == pytest.approx(0.75)
        assert snap['reservation_count'] == 1


def test_f3_separate_processes_atomic_reservation(tmp_path: Path):
    db = tmp_path / 'mp_budget.sqlite'
    Store(db)  # pre-initialize WAL schema
    ctx = multiprocessing.get_context('spawn')
    barrier = ctx.Barrier(3)
    q = ctx.Queue()
    procs = [ctx.Process(target=_mp_worker, args=(str(db), idx, 0.40, barrier, q)) for idx in range(3)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=10)
        assert p.exitcode == 0

    results = [q.get() for _ in range(3)]
    approved = [r for r in results if r[0] == 'APPROVED']
    denied = [r for r in results if r[0] == 'DENIED']
    # Two 0.40 reservations fit in 1.00 cap (0.80); the third 0.40 must be denied
    assert len(approved) == 2
    assert len(denied) == 1
    snap = BudgetLedger(job_budget_usd=1.0, daily_budget_usd=1.0, project_budget_usd=1.0, store=Store(db)).snapshot()
    assert snap['project_spent_usd'] == pytest.approx(0.80)
    assert snap['reservation_count'] == 2


def test_f3_25_forced_restart_and_originating_day_settlement(tmp_path: Path):
    for i in range(25):
        db = tmp_path / f'restart_{i}.sqlite'
        b1 = BudgetLedger(job_budget_usd=0.50, daily_budget_usd=0.50, project_budget_usd=0.50, store=Store(db))
        b1._today = lambda: '2026-11-14'
        res = b1.reserve(f'job_{i}', 0.25)
        b1.reconcile(res.reservation_id, actual_usd=None, unknown_outcome=True)

        # Restart on next UTC day
        b2 = BudgetLedger(job_budget_usd=0.50, daily_budget_usd=0.50, project_budget_usd=0.50, store=Store(db))
        b2._today = lambda: '2026-11-15'
        assert b2.snapshot()['unknown_outcomes'] == 1
        assert b2.snapshot()['project_spent_usd'] == pytest.approx(0.25)

        settled = b2.reconcile(res.reservation_id, actual_usd=0.10)
        assert settled.status == 'SETTLED'
        assert b2.snapshot()['project_spent_usd'] == pytest.approx(0.10)
        assert b2.snapshot()['daily_spent_usd'] == pytest.approx(0.0)
        b2._today = lambda: '2026-11-14'
        assert b2.snapshot()['daily_spent_usd'] == pytest.approx(0.10)


def test_f3_sequential_controls(tmp_path: Path):
    b = BudgetLedger(job_budget_usd=0.10, daily_budget_usd=0.10, project_budget_usd=0.10, store=Store(tmp_path / 'seq.sqlite'))
    r1 = b.reserve('j', 0.06)
    with pytest.raises(BudgetExceeded):
        b.reserve('j', 0.06)
    b.reconcile(r1.reservation_id, actual_usd=0.02)
    r2 = b.reserve('j', 0.06)
    assert r2.status == 'RESERVED'
    assert b.snapshot()['project_spent_usd'] == pytest.approx(0.08)

