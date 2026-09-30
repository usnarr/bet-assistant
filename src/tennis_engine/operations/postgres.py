"""PostgreSQL job runs and attempts (migration 0011). Both tables are append-only.

A fenced attempt locks the lease row and inserts the attempt in one transaction. A unique
partial index allows one success per run, so a duplicate delivery cannot record a second
effect even when two workers race.
"""

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from .jobs import JobAttempt, JobRun
from .leases import Lease, LeaseLost, PostgresLeaseStore


class PostgresJobStore:
    def __init__(self, engine: Engine, leases: PostgresLeaseStore) -> None:
        self.engine = engine
        self.leases = leases

    def register(self, run: JobRun) -> JobRun:
        with self.engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO tennis.job_run (job_run_id, idempotency_key, job, capacity, "
                    "resource, cutoff, input_versions, max_attempts, created_at) VALUES (:id, "
                    ":key, :job, :capacity, :resource, :cutoff, CAST(:inputs AS JSONB), "
                    ":max_attempts, :created_at) ON CONFLICT (idempotency_key) DO NOTHING"
                ),
                {
                    "id": run.job_run_id,
                    "key": run.idempotency_key,
                    "job": run.job.value,
                    "capacity": run.capacity.value,
                    "resource": run.resource,
                    "cutoff": run.cutoff,
                    "inputs": json.dumps(run.input_versions, sort_keys=True),
                    "max_attempts": run.max_attempts,
                    "created_at": run.created_at,
                },
            )
            row = db.execute(
                text(
                    "SELECT job_run_id, idempotency_key, job, capacity, resource, cutoff, "
                    "input_versions, max_attempts, created_at FROM tennis.job_run "
                    "WHERE idempotency_key = :key"
                ),
                {"key": run.idempotency_key},
            ).one()
        return JobRun.model_validate(dict(row._mapping))

    def attempts(self, job_run_id: UUID) -> tuple[JobAttempt, ...]:
        with self.engine.connect() as db:
            rows = db.execute(
                text(
                    "SELECT job_run_id, sequence, status, lease_owner, fencing_token, "
                    "dependency_status, output_versions, detail, recorded_at "
                    "FROM tennis.job_attempt WHERE job_run_id = :id ORDER BY sequence"
                ),
                {"id": job_run_id},
            ).all()
        return tuple(JobAttempt.model_validate(dict(row._mapping)) for row in rows)

    def append(self, attempt: JobAttempt, lease: Lease | None, now: datetime) -> None:
        params: dict[str, Any] = attempt.model_dump(mode="python")
        params["dependency_status"] = json.dumps(attempt.dependency_status, sort_keys=True)
        params["output_versions"] = json.dumps(attempt.output_versions, sort_keys=True)
        with self.engine.begin() as db:
            if attempt.status in ("RUNNING", "SUCCEEDED"):
                if lease is None:
                    raise LeaseLost("A fenced attempt needs a lease")
                self.leases.lock_current(db, lease, now)
            db.execute(
                text(
                    "INSERT INTO tennis.job_attempt (job_run_id, sequence, status, lease_owner, "
                    "fencing_token, dependency_status, output_versions, detail, recorded_at) "
                    "VALUES (:job_run_id, :sequence, :status, :lease_owner, :fencing_token, "
                    "CAST(:dependency_status AS JSONB), CAST(:output_versions AS JSONB), "
                    ":detail, :recorded_at)"
                ),
                params,
            )
