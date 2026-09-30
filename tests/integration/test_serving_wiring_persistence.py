"""F14 production wiring against isolated PostgreSQL and a real F01 journal."""

import os

import pytest
from alembic import command
from alembic.config import Config
from conftest import source_policy
from fastapi.testclient import TestClient
from serving_support import BOOK_SOURCE, READ_AT, bet, stored, watch

from tennis_engine.common.clock import FrozenClock
from tennis_engine.governance.contracts import Role
from tennis_engine.infrastructure.database import build_engine
from tennis_engine.infrastructure.settings import Settings
from tennis_engine.serving.api import create_app
from tennis_engine.serving.auth import issue_token
from tennis_engine.serving.postgres import PostgresDecisionStore
from tennis_engine.serving.wiring import build_serving, create_production_app

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="isolated PostgreSQL not configured"
)


@pytest.fixture
def migrated(monkeypatch):
    database_url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setenv("TENNIS_DATABASE_URL", database_url)
    config = Config("alembic.ini")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = build_engine(database_url)
    try:
        yield database_url, engine
    finally:
        engine.dispose()
        command.downgrade(config, "base")


def test_wired_stack_reads_postgres_and_the_journal(migrated, store, enabled, tmp_path):
    database_url, engine = migrated
    store.save(source_policy(store, BOOK_SOURCE), expected_revision=0, reason="Synthetic book")
    credentials = tmp_path / "api-credentials.json"
    token = issue_token(credentials, "fixture-operator", Role.OPERATOR)
    settings = Settings(
        environment="test",
        database_url=database_url,
        serving_enabled=True,
        governance_journal=tmp_path / "governance.sqlite3",
        api_credentials_file=credentials,
    )
    decisions = PostgresDecisionStore(engine)
    for item in (stored(watch()), stored(bet())):
        decisions.add(item)

    # The default stores come from the engine: PostgreSQL decisions and quote history.
    serving = build_serving(settings, engine=engine, clock=FrozenClock(READ_AT))
    client = TestClient(create_app(settings, serving=serving))
    auth = {"Authorization": f"Bearer {token}"}
    rows = client.get("/v1/tennis/recommendations", headers=auth).json()["recommendations"]
    by_recorded = {row["recorded_decision"]: row for row in rows}
    assert by_recorded["WATCH"]["decision"] == "WATCH"
    # No quote observation exists in PostgreSQL, so the BET is served as NO_BET.
    assert by_recorded["BET"]["decision"] == "NO_BET"

    store.set_global_disable(True, reason="Synthetic incident stop")
    rows = client.get("/v1/tennis/recommendations", headers=auth).json()["recommendations"]
    assert {row["decision"] for row in rows} == {"NO_BET"}

    # The process entry point starts with the same settings and reports the journal.
    ready = TestClient(create_production_app(settings)).get("/health/ready").json()
    assert ready["dependencies"]["database"]["ready"] is True
    assert ready["dependencies"]["governance_journal"] == {
        "ready": True,
        "detail": "read-only; global_stop=on",
    }
