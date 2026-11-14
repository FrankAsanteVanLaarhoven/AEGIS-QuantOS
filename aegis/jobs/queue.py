"""Durable research job state, step limits, cancellation, and redacted traces."""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class JobRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    job_id: str
    kind: str
    symbol: str
    status: Literal['PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED', 'ABSTAINED', 'BLOCKED'] = 'PENDING'
    max_steps: int = Field(default=6, ge=1, le=20)
    steps_completed: int = 0
    created_at: datetime
    updated_at: datetime
    trace: list[dict[str, Any]] = Field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None


class DurableJobManager:
    def __init__(self, store: Any = None) -> None:
        self._store = store
        self._lock = threading.RLock()
        self._jobs: dict[str, JobRecord] = {}
        self._ensure_schema()
        self._load_all()

    def _ensure_schema(self) -> None:
        if self._store is None:
            return
        with self._store.lock:
            self._store.db.executescript(
                'CREATE TABLE IF NOT EXISTS research_jobs (job_id TEXT PRIMARY KEY, record_json TEXT NOT NULL);'
            )

    def _load_all(self) -> None:
        if self._store is None:
            return
        with self._store.lock:
            rows = self._store.db.execute('SELECT job_id, record_json FROM research_jobs').fetchall()
            self._jobs = {r['job_id']: JobRecord.model_validate_json(r['record_json']) for r in rows}

    def _persist(self, rec: JobRecord) -> None:
        self._jobs[rec.job_id] = rec
        if self._store is None:
            return
        with self._store.tx():
            self._store.db.execute(
                'INSERT INTO research_jobs(job_id, record_json) VALUES (?, ?) '
                'ON CONFLICT(job_id) DO UPDATE SET record_json = excluded.record_json',
                (rec.job_id, rec.model_dump_json()),
            )

    def create(self, kind: str, symbol: str, max_steps: int = 6) -> JobRecord:
        with self._lock:
            now = datetime.now(timezone.utc)
            jid = f'job_{uuid.uuid4().hex[:12]}'
            rec = JobRecord(job_id=jid, kind=kind, symbol=symbol.upper(), max_steps=max_steps, created_at=now, updated_at=now)
            self._persist(rec)
            return rec

    def record_step(self, job_id: str, stage: str, detail: dict[str, Any]) -> JobRecord:
        with self._lock:
            self._load_all()
            rec = self._jobs[job_id]
            if rec.status == 'CANCELLED':
                raise RuntimeError('Job cancelled')
            if rec.steps_completed >= rec.max_steps:
                rec.status = 'FAILED'
                rec.error = 'Max job steps exceeded'
                self._persist(rec)
                raise RuntimeError(rec.error)
            rec.steps_completed += 1
            rec.updated_at = datetime.now(timezone.utc)
            rec.trace.append({'step': rec.steps_completed, 'stage': stage, 'ts': rec.updated_at.isoformat(), **detail})
            self._persist(rec)
            return rec

    def finish(
        self,
        job_id: str,
        status: Literal['COMPLETED', 'FAILED', 'CANCELLED', 'ABSTAINED', 'BLOCKED'],
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> JobRecord:
        with self._lock:
            self._load_all()
            rec = self._jobs[job_id]
            rec.status = status
            rec.result = result
            rec.error = error
            rec.updated_at = datetime.now(timezone.utc)
            self._persist(rec)
            return rec

    def cancel(self, job_id: str) -> JobRecord:
        return self.finish(job_id, 'CANCELLED', error='Cancelled by operator')

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            self._load_all()
            return [j.model_dump(mode='json') for j in sorted(self._jobs.values(), key=lambda x: x.created_at, reverse=True)]

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            self._load_all()
            return self._jobs.get(job_id)
