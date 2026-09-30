"""F15.5/F15.7 on PostgreSQL: fingerprints, raw-object and ledger checks, the reader role."""

import os
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from recovery_support import seed
from serving_support import READ_AT, copy, no_bet, stored
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from tennis_engine.common.clock import FrozenClock
from tennis_engine.governance.contracts import Principal, Role
from tennis_engine.governance.store import GovernanceStore
from tennis_engine.infrastructure.database import build_engine, database_ready
from tennis_engine.infrastructure.object_store import LocalObjectStore
from tennis_engine.infrastructure.settings import Settings
from tennis_engine.operations.recovery import (
    compare,
    database_fingerprint,
    reconcile_ledgers,
    reconcile_raw_objects,
)
from tennis_engine.operations.roles import grant_serving_reader, revoke_serving_reader
from tennis_engine.serving.api import create_app
from tennis_engine.serving.auth import issue_token
from tennis_engine.serving.postgres import PostgresDecisionStore
from tennis_engine.serving.wiring import build_serving

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="isolated PostgreSQL not configured"
)
READER = "f15_reader"
# Synthetic test-only password for an isolated test database.
READER_PASSWORD = "synthetic-reader-pass"
NOW = datetime(2026, 9, 20, 13, tzinfo=UTC)


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


def test_fingerprint_and_reconciliation_detect_changes(engine, tmp_path):
    objects = LocalObjectStore(tmp_path / "objects")
    seeded = seed(engine, objects)
    first = database_fingerprint(engine, NOW)
    assert first == database_fingerprint(engine, NOW)
    assert first.revision == "0011_operations"
    assert first.tables["decision_record"].rows == seeded["decisions"]
    assert compare(first, database_fingerprint(engine, NOW)).integrity == "PASS"

    raw = reconcile_raw_objects(engine, objects)
    assert (raw.contents, raw.verified, raw.problems) == (1, 1, ())
    ledgers = reconcile_ledgers(engine, NOW)
    assert ledgers.ledgers == 1 and ledgers.unbalanced == ()
    assert ledgers.closing_balances == {"ops02-shadow": "80.00"}

    PostgresDecisionStore(engine).add(stored(copy(no_bet(), "ops02-late")))
    changed = compare(first, database_fingerprint(engine, NOW))
    assert changed.findings == ("TABLE:decision_record",)

    for path in (tmp_path / "objects").rglob("*"):
        if path.is_file():
            path.chmod(0o666)
            path.unlink()
    missing = reconcile_raw_objects(engine, objects)
    assert missing.verified == 0 and missing.problems[0].startswith("OBJECT_MISSING:")


def test_reader_role_can_read_but_not_write_and_serves_the_api(engine, tmp_path, store, enabled):
    with engine.begin() as db:
        db.execute(text(f"DROP ROLE IF EXISTS {READER}"))
        db.execute(text(f"CREATE ROLE {READER} LOGIN PASSWORD '{READER_PASSWORD}'"))
    reader_url = (
        make_url(os.environ["TEST_DATABASE_URL"])
        .set(username=READER, password=READER_PASSWORD)
        .render_as_string(hide_password=False)
    )
    reader = build_engine(reader_url)
    try:
        with pytest.raises(ValueError):
            grant_serving_reader(engine, "bad name; DROP")
        grant_serving_reader(engine, READER)
        seed(engine, LocalObjectStore(tmp_path / "objects"))
        assert database_ready(reader) == (True, "0011_operations")
        with reader.connect() as db:
            assert db.execute(text("SELECT count(*) FROM tennis.decision_record")).scalar_one()
        for statement in (
            "DELETE FROM tennis.resource_lease",
            "UPDATE tennis.resource_lease SET owner = 'x'",
            "INSERT INTO tennis.job_run SELECT * FROM tennis.job_run",
            "CREATE TABLE tennis.intruder (id int)",
        ):
            with pytest.raises(DBAPIError, match="permission denied"), reader.begin() as db:
                db.execute(text(statement))

        credentials = tmp_path / "api-credentials.json"
        token = issue_token(credentials, "fixture-operator", Role.OPERATOR)
        journal = tmp_path / "governance.sqlite3"
        settings = Settings(
            environment="test",
            database_url=os.environ["TEST_DATABASE_URL"],
            serving_database_url=reader_url,
            serving_enabled=True,
            governance_journal=journal,
            api_credentials_file=credentials,
        )
        GovernanceStore(journal, Principal(identity="x", role=Role.OPERATOR)).close()
        client = TestClient(
            create_app(settings, serving=build_serving(settings, clock=FrozenClock(READ_AT)))
        )
        response = client.get(
            "/v1/tennis/recommendations?view=history", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200, response.text
        assert len(response.json()["recommendations"]) == 8
    finally:
        reader.dispose()
        revoke_serving_reader(engine, READER)
        with engine.begin() as db:
            db.execute(text(f"DROP OWNED BY {READER}"))
            db.execute(text(f"DROP ROLE {READER}"))
