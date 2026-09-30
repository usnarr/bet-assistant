"""F11.8/F13.9 registry on PostgreSQL: append-only events, one use per decision, races."""

import getpass
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from registry_support import FAMILY, OPERATOR, REVIEWER, bundle, drill_pass_decision
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tennis_engine.common.clock import FrozenClock
from tennis_engine.infrastructure.database import (
    EXPECTED_ALEMBIC_REVISION,
    build_engine,
    current_revision,
)
from tennis_engine.models.registry import ModelRegistry, RegistryRefused, StaleChampion
from tennis_engine.models.registry_postgres import PostgresRegistryStore
from tennis_engine.operations import cli as ops_cli

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="isolated PostgreSQL not configured"
)
NOW = datetime(2026, 9, 30, 13, tzinfo=UTC)


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


@pytest.fixture(scope="module")
def bundles(tmp_path_factory):
    root = tmp_path_factory.mktemp("registry-artifacts")
    first = bundle(root, "v1", seed=1)
    second = bundle(root, "v2", seed=2, rollback_target=first.bundle_id)
    third = bundle(root, "v3", seed=3, rollback_target=first.bundle_id)
    return root, first, second, third


def test_switch_and_rollback_on_postgres(engine, bundles):
    root, first, second, third = bundles
    registry = ModelRegistry(PostgresRegistryStore(engine), root, FrozenClock(NOW))
    for item in (first, second, third):
        assert registry.register(item) is True
    assert registry.register(first) is False
    supported = frozenset({first.feature_set_sha256})

    first_decision = drill_pass_decision(first.candidate, first.rollback_reference)
    registry.promote(
        first.bundle_id,
        first_decision,
        actor=REVIEWER,
        reason="Drill switch",
        supported_feature_sets=supported,
    )

    # Two reviewers checked the same champion and switch at the same time: one wins.
    template = registry.store.events(FAMILY)[0]
    racing = [
        template.model_copy(
            update={
                "event_id": uuid4(),
                "sequence": 2,
                "bundle_id": target.bundle_id,
                "previous_bundle_id": first.bundle_id,
                "decision_id": uuid4(),
            }
        )
        for target in (second, third)
    ]

    def append(event):
        try:
            registry.store.append(event, first.bundle_id)
        except StaleChampion:
            return "stale"
        return "won"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(append, racing))
    assert outcomes == ["stale", "won"]
    events = registry.store.events(FAMILY)
    assert [item.sequence for item in events] == [1, 2]
    winner = events[-1].bundle_id
    assert winner in (second.bundle_id, third.bundle_id)

    back = registry.rollback(
        FAMILY, actor=OPERATOR, reason="Drill rollback", supported_feature_sets=supported
    )
    assert back.bundle_id == first.bundle_id and back.previous_bundle_id == winner
    loaded = registry.active(FAMILY, supported)
    assert loaded is not None and loaded.bundle == first

    # A decision promotes once, also against the database index.
    with pytest.raises(RegistryRefused, match="DECISION_ALREADY_USED"):
        registry.promote(
            first.bundle_id,
            first_decision,
            actor=REVIEWER,
            reason="Replay",
            supported_feature_sets=supported,
        )
    for statement in (
        "UPDATE tennis.champion_event SET kind = 'PROMOTE'",
        "DELETE FROM tennis.champion_event",
        "UPDATE tennis.model_bundle SET version = 'x'",
        "DELETE FROM tennis.model_bundle",
    ):
        with pytest.raises(DBAPIError, match="append-only"), engine.begin() as db:
            db.execute(text(statement))
    with pytest.raises(DBAPIError, match="ck_champion_promote_decision"), engine.begin() as db:
        db.execute(
            text(
                "INSERT INTO tennis.champion_event (family, sequence, event_id, kind, bundle_id, "
                "event, recorded_at) VALUES ('x', 1, gen_random_uuid(), 'PROMOTE', NULL, "
                "'{}'::jsonb, now())"
            )
        )


def test_registry_migration_downgrades_and_upgrades(engine):
    config = Config("alembic.ini")
    command.downgrade(config, "0012_agent_records")
    with engine.connect() as db:
        assert db.execute(text("SELECT to_regclass('tennis.model_bundle')")).scalar_one() is None
    command.upgrade(config, "head")
    assert current_revision(engine) == EXPECTED_ALEMBIC_REVISION == "0013_model_registry"


def test_registry_commands(engine, bundles, tmp_path, monkeypatch, capsys):
    root, first, *_ = bundles
    monkeypatch.setenv("TENNIS_ARTIFACT_ROOT", str(root))
    access = tmp_path / "access.json"
    access.write_text(json.dumps({getpass.getuser(): "policy_reviewer"}), encoding="utf-8")
    base = ["registry", "--access-file", str(access), "--runtime-lock", str(tmp_path / "none")]
    model_dir = first.model.resolve(root).parent
    calibrator_dir = first.calibrator.resolve(root).parent
    arguments = base + ["register", "--family", FAMILY, "--version", "cli-v1"]
    arguments += ["--model-dir", str(model_dir), "--calibrator-dir", str(calibrator_dir)]
    arguments += ["--evaluation-report", str(first.evaluation_report.resolve(root))]
    assert ops_cli.main(arguments) == 0
    bundle_id = json.loads(capsys.readouterr().out)["bundle_id"]
    assert ops_cli.main(base + ["verify", "--bundle", bundle_id]) == 0
    assert json.loads(capsys.readouterr().out)["problems"] == []

    decision = tmp_path / "release-decision.json"
    decision.write_text(
        drill_pass_decision(first.candidate, "no-champion", salt="cli").model_dump_json(),
        encoding="utf-8",
    )
    promote = base + ["promote", "--bundle", bundle_id, "--decision", str(decision)]
    assert ops_cli.main(promote + ["--reason", "CLI drill"]) == 0
    capsys.readouterr()
    assert ops_cli.main(promote + ["--reason", "CLI replay"]) == 1
    assert "DECISION_ALREADY_USED" in capsys.readouterr().out
    assert ops_cli.main(base + ["show", "--family", FAMILY]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["champion"] == bundle_id and len(shown["events"]) == 1
    assert ops_cli.main(base + ["rollback", "--family", FAMILY, "--reason", "CLI rollback"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["bundle_id"] is None
    assert str(root) not in output and str(tmp_path) not in output
