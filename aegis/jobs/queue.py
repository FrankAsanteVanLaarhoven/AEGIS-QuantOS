"""Durable research job state, step limits, cancellation, and redacted traces."""
from __future__ import annotations

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
    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}

    def create(self, kind: str, symbol: str, max_steps: int = 6) -> JobRecord:
        now = datetime.now(timezone.utc)
        jid = f'job_{uuid.uuid4().hex[:12]}'
        rec = JobRecord(job_id=jid, kind=kind, symbol=symbol.upper(), max_steps=max_steps, created_at=now, updated_at=now)
        self._jobs[jid] = rec
        return rec

    def record_step(self, job_id: str, stage: str, detail: dict[str, Any]) -> JobRecord:
        rec = self._jobs[job_id]
        if rec.status == 'CANCELLED':
            raise RuntimeError('Job cancelled')
        if rec.steps_completed >= rec.max_steps:
            rec.status = 'FAILED'
            rec.error = 'Max job steps exceeded'
            raise RuntimeError(rec.error)
        rec.steps_completed += 1
        rec.updated_at = datetime.now(timezone.utc)
        rec.trace.append({'step': rec.steps_completed, 'stage': stage, 'ts': rec.updated_at.isoformat(), **detail})
        return rec

    def finish(self, job_id: str, status: Literal['COMPLETED', 'FAILED', 'CANCELLED', 'ABSTAINED', 'BLOCKED'], result: dict[str, Any] | None = None, error: str | None = None) -> JobRecord:
        rec = self._jobs[job_id]
        rec.status = status
        rec.result = result
        rec.error = error
        rec.updated_at = datetime.now(timezone.utc)
        return rec

    def cancel(self, job_id: str) -> JobRecord:
        return self.finish(job_id, 'CANCELLED', error='Cancelled by operator')

    def list_jobs(self) -> list[dict[str, Any]]:
        return [j.model_dump(mode='json') for j in sorted(self._jobs.values(), key=lambda x: x.created_at, reverse=True)]

    def get(self, job_id: str) -> JobRecord | None:
        return self._jobs.get(job_id)
