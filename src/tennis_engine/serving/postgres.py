"""PostgreSQL implementation of the F14 `DecisionStore` (migration 0010).

The table is append-only. A retry with identical content is a no-op; other content under
the same decision ID raises `DecisionConflict`.
"""

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from .contracts import StoredDecision
from .store import DecisionConflict, DecisionQuery

COLUMNS = "record, context"


def _load(row: Any) -> StoredDecision:
    return StoredDecision.model_validate({"record": row.record, "context": row.context})


class PostgresDecisionStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def add(self, stored: StoredDecision) -> None:
        record, context = stored.record, stored.context
        with self.engine.begin() as db:
            inserted = db.execute(
                text(
                    "INSERT INTO tennis.decision_record (decision_id, decision_key, ledger_id, "
                    "version, supersedes, status, bookmaker, market, match_id, "
                    "scheduled_start, decided_at, expires_at, quote_source_id, "
                    "quote_observed_at, stake, record, context) VALUES (:id, :key, :ledger, "
                    ":version, :supersedes, :status, :bookmaker, :market, :match_id, :start, "
                    ":decided_at, :expires_at, :quote_source, :quote_observed_at, :stake, "
                    "CAST(:record AS JSONB), CAST(:context AS JSONB)) "
                    "ON CONFLICT (decision_id) DO NOTHING RETURNING decision_id"
                ),
                {
                    "id": record.decision_id,
                    "key": record.decision_key,
                    "ledger": record.ledger_id,
                    "version": record.version,
                    "supersedes": record.supersedes,
                    "status": record.status.value,
                    "bookmaker": record.bookmaker,
                    "market": context.market.value,
                    "match_id": context.match.match_id,
                    "start": context.match.scheduled_start,
                    "decided_at": record.decided_at,
                    "expires_at": record.expires_at,
                    "quote_source": context.quote_source_id,
                    "quote_observed_at": context.quote_observed_at,
                    "stake": record.stake.amount,
                    "record": json.dumps(record.model_dump(mode="json"), sort_keys=True),
                    "context": json.dumps(context.model_dump(mode="json"), sort_keys=True),
                },
            ).first()
        if inserted is None:
            existing = self.get(record.decision_id)
            if existing != stored:
                raise DecisionConflict(str(record.decision_id))

    def get(self, decision_id: UUID) -> StoredDecision | None:
        with self.engine.connect() as db:
            row = db.execute(
                text(f"SELECT {COLUMNS} FROM tennis.decision_record WHERE decision_id = :id"),
                {"id": decision_id},
            ).first()
        return _load(row) if row is not None else None

    def query(self, query: DecisionQuery) -> Sequence[StoredDecision]:
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": query.limit}
        if query.bookmaker is not None:
            clauses.append("d.bookmaker = :bookmaker")
            params["bookmaker"] = query.bookmaker
        if query.statuses:
            clauses.append("d.status = ANY(:statuses)")
            params["statuses"] = sorted(status.value for status in query.statuses)
        if query.starts_after is not None:
            clauses.append("d.scheduled_start >= :starts_after")
            params["starts_after"] = query.starts_after
        if query.starts_before is not None:
            clauses.append("d.scheduled_start < :starts_before")
            params["starts_before"] = query.starts_before
        if query.match_id is not None:
            clauses.append("d.match_id = :match_id")
            params["match_id"] = query.match_id
        if query.active_at is not None:
            clauses.append("d.expires_at > :active_at")
            params["active_at"] = query.active_at
        if query.latest_only:
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM tennis.decision_record s "
                "WHERE s.supersedes = d.decision_id)"
            )
        if query.after is not None:
            clauses.append("(d.scheduled_start, d.decision_id) > (:after_start, :after_id)")
            params["after_start"], params["after_id"] = query.after
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        statement = (
            f"SELECT {COLUMNS} FROM tennis.decision_record d {where} "
            "ORDER BY d.scheduled_start, d.decision_id LIMIT :limit"
        )
        with self.engine.connect() as db:
            rows = db.execute(text(statement), params).all()
        return [_load(row) for row in rows]

    def successors(self, decision_id: UUID) -> Sequence[UUID]:
        with self.engine.connect() as db:
            rows = db.execute(
                text(
                    "SELECT decision_id FROM tennis.decision_record WHERE supersedes = :id "
                    "ORDER BY decision_id"
                ),
                {"id": decision_id},
            ).all()
        return [row.decision_id for row in rows]

    def latest_observations(self) -> dict[str, datetime | None]:
        with self.engine.connect() as db:
            rows = db.execute(
                text(
                    "SELECT s.source_id, max(CASE WHEN s.source_id = d.quote_source_id "
                    "THEN d.quote_observed_at END) AS latest FROM tennis.decision_record d "
                    "CROSS JOIN LATERAL jsonb_array_elements_text(d.context -> 'source_ids') "
                    "AS s(source_id) GROUP BY s.source_id ORDER BY s.source_id"
                )
            ).all()
        return {row.source_id: row.latest for row in rows}
