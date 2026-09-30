"""F15 OPS-01 drills on PostgreSQL: idempotent runs, fenced leases, append-only history."""

import os
from datetime import timedelta

import pytest
from alembic import command
from alembic.config import Config
from operations_support import START, capacity_drill, drills
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tennis_engine.infrastructure.database import (
    EXPECTED_ALEMBIC_REVISION,
    build_engine,
    current_revision,
)
from tennis_engine.operations.leases import PostgresLeaseStore
from tennis_engine.operations.postgres import PostgresJobStore

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


def stores(engine):
    leases = PostgresLeaseStore(engine)
    return PostgresJobStore(engine, leases), leases


def test_ops01_drills_on_postgres(engine):
    store = drills(lambda: stores(engine))
    with engine.connect() as db:
        attempts = db.execute(text("SELECT count(*) FROM tennis.job_attempt")).scalar_one()
    assert attempts > 0
    # History is append-only.
    for statement in ("UPDATE tennis.job_attempt SET detail = 'x'", "DELETE FROM tennis.job_run"):
        with pytest.raises(DBAPIError, match="append-only"), engine.begin() as db:
            db.execute(text(statement))
    # At most one success per run, even when a writer bypasses the runner.
    with engine.connect() as db:
        row = db.execute(
            text(
                "SELECT job_run_id, max(sequence) AS last FROM tennis.job_attempt "
                "WHERE status = 'SUCCEEDED' GROUP BY job_run_id LIMIT 1"
            )
        ).one()
    with pytest.raises(IntegrityError), engine.begin() as db:
        db.execute(
            text(
                "INSERT INTO tennis.job_attempt VALUES (:id, :seq, 'SUCCEEDED', 'x', 99, "
                "'{}', '{}', '', now())"
            ),
            {"id": row.job_run_id, "seq": row.last + 100},
        )
    assert store is not None


def test_backfill_capacity_is_separate_on_postgres(engine):
    capacity_drill(*stores(engine))


def test_fencing_token_never_decreases(engine):
    leases = PostgresLeaseStore(engine)
    lease = leases.acquire("source:synthetic-book:events", "a", timedelta(seconds=5), START)
    assert lease is not None
    with pytest.raises(DBAPIError, match="fencing token cannot decrease"), engine.begin() as db:
        db.execute(text("UPDATE tennis.resource_lease SET fencing_token = 0"))


def test_operations_migration_downgrades_and_upgrades(engine, monkeypatch):
    config = Config("alembic.ini")
    command.downgrade(config, "0010_decision_records")
    assert current_revision(engine) == "0010_decision_records"
    with engine.connect() as db:
        assert db.execute(text("SELECT to_regclass('tennis.job_run')")).scalar_one() is None
    command.upgrade(config, "head")
    assert current_revision(engine) == EXPECTED_ALEMBIC_REVISION
