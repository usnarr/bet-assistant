"""F18/F15.6 on PostgreSQL: idempotent proposals, immutable traces, append-only tables."""

import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tennis_engine.agents.contracts import AgentRole
from tennis_engine.agents.postgres import PostgresAgentStore
from tennis_engine.agents.proposals import TraceConflict, new_proposal
from tennis_engine.common.ids import stable_id
from tennis_engine.infrastructure.database import (
    EXPECTED_ALEMBIC_REVISION,
    build_engine,
    current_revision,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="isolated PostgreSQL not configured"
)


@pytest.fixture
def engine(monkeypatch):
    database_url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setenv("TENNIS_DATABASE_URL", database_url)
    config = Config("alembic.ini")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    instance = build_engine(database_url)
    try:
        yield instance
    finally:
        instance.dispose()
        command.downgrade(config, "base")


def proposal(trace_seed="t1", rationale="The alert breached its rule."):
    from agent_support import AS_OF

    return new_proposal(
        role=AgentRole.MONITORING,
        tool="propose_incident_triage",
        subject_id="synthetic-scope",
        kind="TRIAGE",
        fields={"severity": "CRITICAL"},
        evidence_ids=("synthetic-alert-1",),
        rationale=rationale,
        trace_id=stable_id("trace", trace_seed),
        created_at=AS_OF,
    )


def test_proposals_are_stored_once_under_concurrent_retries(engine):
    store = PostgresAgentStore(engine)
    candidates = [proposal(f"t{index}", f"Retry {index}.") for index in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(store.propose, candidates))
    assert sum(created for _, created in results) == 1
    assert len({stored.proposal_id for stored, _ in results}) == 1
    (only,) = store.proposals("synthetic-scope")
    assert only.state == "PROPOSED" and only.evidence_ids == ("synthetic-alert-1",)
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as db:
        db.execute(text("DELETE FROM tennis.agent_proposal"))
    with pytest.raises(IntegrityError), engine.begin() as db:
        db.execute(
            text(
                "INSERT INTO tennis.agent_proposal (proposal_id, idempotency_key, role, tool, "
                "subject_id, kind, fields, evidence_ids, rationale, trace_id, created_at, state) "
                "SELECT gen_random_uuid(), repeat('a', 64), role, tool, subject_id, kind, fields, "
                "evidence_ids, rationale, trace_id, created_at, 'APPLIED' "
                "FROM tennis.agent_proposal"
            )
        )


def test_traces_are_immutable_and_audited(engine):
    from agent_support import call, final, run, turn

    store = PostgresAgentStore(engine)
    result, _ = run(
        [turn(call("evaluate_quote"), call("place_bet")), turn(final=final())], store=store
    )
    assert result.trace_recorded and result.status == "REJECTED"
    assert store.trace(result.trace.trace_id) == result.trace
    assert store.record_trace(result.trace) is False
    with pytest.raises(TraceConflict):
        store.record_trace(result.trace.model_copy(update={"status": "COMPLETED"}))
    with engine.connect() as db:
        critical = db.execute(text("SELECT critical_attempts FROM tennis.agent_trace")).scalar_one()
    assert critical == 1
    for statement in (
        "UPDATE tennis.agent_trace SET status = 'COMPLETED'",
        "DELETE FROM tennis.agent_trace",
    ):
        with pytest.raises(DBAPIError, match="append-only"), engine.begin() as db:
            db.execute(text(statement))


def test_agent_migration_downgrades_and_upgrades(engine):
    config = Config("alembic.ini")
    command.downgrade(config, "0011_operations")
    with engine.connect() as db:
        assert db.execute(text("SELECT to_regclass('tennis.agent_trace')")).scalar_one() is None
    command.upgrade(config, "head")
    assert current_revision(engine) == EXPECTED_ALEMBIC_REVISION
