"""PostgreSQL agent traces and proposals (migration 0012). Both tables are append-only.

A proposal insert uses `ON CONFLICT (idempotency_key) DO NOTHING`, then reads the stored
row. So two concurrent retries of one proposal store it once. A trace insert with an
existing ID is a no-op when the content is equal and a `TraceConflict` otherwise.
"""

import json

from sqlalchemy import Engine, text

from .contracts import sha256
from .proposals import Proposal, TraceConflict
from .trace import AgentTrace

PROPOSAL_COLUMNS = (
    "proposal_id, idempotency_key, role, tool, subject_id, kind, fields, evidence_ids, "
    "rationale, trace_id, created_at, state"
)


class PostgresAgentStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def propose(self, proposal: Proposal) -> tuple[Proposal, bool]:
        with self.engine.begin() as db:
            inserted = db.execute(
                text(
                    f"INSERT INTO tennis.agent_proposal ({PROPOSAL_COLUMNS}) VALUES "
                    "(:proposal_id, :idempotency_key, :role, :tool, :subject_id, :kind, "
                    "CAST(:fields AS JSONB), CAST(:evidence_ids AS JSONB), :rationale, "
                    ":trace_id, :created_at, :state) "
                    "ON CONFLICT (idempotency_key) DO NOTHING RETURNING proposal_id"
                ),
                {
                    **proposal.model_dump(mode="python"),
                    "role": proposal.role.value,
                    "fields": json.dumps(proposal.fields, sort_keys=True),
                    "evidence_ids": json.dumps(list(proposal.evidence_ids)),
                },
            ).first()
            row = db.execute(
                text(
                    f"SELECT {PROPOSAL_COLUMNS} FROM tennis.agent_proposal "
                    "WHERE idempotency_key = :key"
                ),
                {"key": proposal.idempotency_key},
            ).one()
        data = dict(row._mapping)
        data["evidence_ids"] = tuple(data["evidence_ids"])
        return Proposal.model_validate(data), inserted is not None

    def proposals(self, subject_id: str) -> tuple[Proposal, ...]:
        with self.engine.connect() as db:
            rows = db.execute(
                text(
                    f"SELECT {PROPOSAL_COLUMNS} FROM tennis.agent_proposal "
                    "WHERE subject_id = :subject ORDER BY created_at, proposal_id"
                ),
                {"subject": subject_id},
            ).all()
        found = []
        for row in rows:
            data = dict(row._mapping)
            data["evidence_ids"] = tuple(data["evidence_ids"])
            found.append(Proposal.model_validate(data))
        return tuple(found)

    def record_trace(self, trace: AgentTrace) -> bool:
        body = trace.model_dump(mode="json")
        digest = sha256(body)
        with self.engine.begin() as db:
            inserted = db.execute(
                text(
                    "INSERT INTO tennis.agent_trace (trace_id, run_id, role, role_version, "
                    "model_id, status, started_at, finished_at, critical_attempts, trace, "
                    "trace_sha256) VALUES (:trace_id, :run_id, :role, :role_version, :model_id, "
                    ":status, :started_at, :finished_at, :critical, CAST(:trace AS JSONB), "
                    ":sha) ON CONFLICT (trace_id) DO NOTHING RETURNING trace_id"
                ),
                {
                    "trace_id": trace.trace_id,
                    "run_id": trace.run_id,
                    "role": trace.role.value,
                    "role_version": trace.role_version,
                    "model_id": trace.model.model_id,
                    "status": trace.status,
                    "started_at": trace.started_at,
                    "finished_at": trace.finished_at,
                    "critical": len(trace.critical_attempts),
                    "trace": json.dumps(body, sort_keys=True),
                    "sha": digest,
                },
            ).first()
            if inserted is not None:
                return True
            stored = db.execute(
                text("SELECT trace_sha256 FROM tennis.agent_trace WHERE trace_id = :id"),
                {"id": trace.trace_id},
            ).scalar_one()
        if stored != digest:
            raise TraceConflict("A trace is immutable")
        return False

    def trace(self, trace_id: object) -> AgentTrace | None:
        with self.engine.connect() as db:
            body = db.execute(
                text("SELECT trace FROM tennis.agent_trace WHERE trace_id = :id"),
                {"id": trace_id},
            ).scalar_one_or_none()
        return None if body is None else AgentTrace.model_validate(body)
