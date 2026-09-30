"""F15 scheduler on PostgreSQL and a temporary F01 journal (synthetic, isolated services)."""

import json
import os
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from tennis_engine.common.clock import FrozenClock
from tennis_engine.governance.contracts import Principal, Role
from tennis_engine.governance.store import GovernanceStore
from tennis_engine.infrastructure.database import build_engine
from tennis_engine.infrastructure.settings import Settings
from tennis_engine.operations import cli as ops_cli
from tennis_engine.operations.runtime import SchedulerOptions, build_scheduler

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="isolated PostgreSQL not configured"
)
START = datetime(2026, 9, 20, 8, 0, 30, tzinfo=UTC)


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


@pytest.fixture
def journal(tmp_path):
    path = tmp_path / "governance.sqlite3"
    GovernanceStore(path, Principal(identity="fixture", role=Role.OPERATOR)).close()
    return path


def test_scheduler_ticks_are_idempotent_and_publish_nothing(engine, journal, tmp_path):
    settings = Settings(
        environment="test",
        database_url=os.environ["TEST_DATABASE_URL"],
        governance_journal=journal,
    )
    clock = FrozenClock(START)
    options = SchedulerOptions(metrics_host="127.0.0.1", metrics_port=0, signal_inbox=tmp_path)
    process = build_scheduler(settings, options, clock=clock, engine=engine)
    process.server.start()
    try:
        first = process.scheduler.tick()
        assert first.jobs["sync_source_registry"] == "SUCCEEDED"
        assert first.jobs["publish_recommendations"] == "NOT_CONFIGURED"
        assert first.tasks == {"evaluate_alerts": "SUCCEEDED"}
        clock.advance(timedelta(minutes=1))
        second = process.scheduler.tick()
        assert second.jobs["sync_source_registry"] == "ALREADY_SUCCEEDED"
        with engine.connect() as db:
            runs = db.execute(text("SELECT count(*) FROM tennis.job_run")).scalar_one()
            succeeded = db.execute(
                text("SELECT count(*) FROM tennis.job_attempt WHERE status = 'SUCCEEDED'")
            ).scalar_one()
            decisions = db.execute(text("SELECT count(*) FROM tennis.decision_record")).scalar_one()
        assert (runs, succeeded, decisions) == (1, 1, 0)

        url = f"http://127.0.0.1:{process.server.port}"
        with urllib.request.urlopen(url + "/metrics", timeout=5) as response:
            body = response.read().decode()
        assert 'tennis_signal_value{signal="settlement_mismatch_count",scope="global"} 0' in body
        assert 'tennis_signal_expected{signal="leakage_test_failures",scope="global"} 1' in body
        assert 'rule_id="future-leakage"' in body  # missing leakage telemetry is not healthy
        with urllib.request.urlopen(url + "/health", timeout=5) as response:
            assert response.status == 200
    finally:
        process.close()

    # An empty journal has the global stop on; missing global telemetry applies no control.
    store = GovernanceStore(journal, Principal(identity="fixture", role=Role.OPERATOR))
    try:
        assert store.global_disabled(datetime.now(UTC))
        assert store.records("source_stop") == []
    finally:
        store.close()


def test_scheduler_once_command(engine, journal, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TENNIS_GOVERNANCE_JOURNAL", str(journal))
    assert ops_cli.main(["scheduler", "--once", "--signal-inbox", str(tmp_path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["jobs"]["sync_source_registry"] == "SUCCEEDED"
    assert report["tasks"]["evaluate_alerts"] == "SUCCEEDED"
