"""F14 production wiring: typed settings, the app factory and fail-closed start-up."""

import json
import sqlite3

import pytest
from conftest import source_policy
from fastapi.testclient import TestClient
from pydantic import ValidationError
from serving_support import BOOK_SOURCE, READ_AT, bet, stored, watch
from sqlalchemy.engine import make_url

from tennis_engine.common.clock import FrozenClock
from tennis_engine.governance.contracts import Principal, Role
from tennis_engine.governance.store import GovernanceStore
from tennis_engine.infrastructure.cli import main
from tennis_engine.infrastructure.settings import Settings
from tennis_engine.ingestion.bookmakers.history import MemoryHistoryStore
from tennis_engine.serving.api import create_app
from tennis_engine.serving.auth import (
    ApiCredential,
    issue_token,
    load_credentials,
    revoke_token,
    token_digest,
)
from tennis_engine.serving.store import InMemoryDecisionStore
from tennis_engine.serving.wiring import (
    ServingConfigurationError,
    build_serving,
    create_production_app,
    journal_check,
)

# Synthetic values for tests only. They are not real credentials.
REAL_LOOKING = {
    "object_store_access_key": "synthetic-access-key-1",
    "object_store_secret_key": "synthetic-secret-key-1",
    "object_store_secure": True,
    # Port 1 refuses connections, so a database read fails fast.
    "database_url": "postgresql+psycopg://tennis:synthetic-db-pass@127.0.0.1:1/tennis",
}


@pytest.fixture
def journal(tmp_path):
    path = tmp_path / "governance.sqlite3"
    GovernanceStore(path, Principal(identity="fixture-reviewer", role=Role.POLICY_REVIEWER)).close()
    return path


@pytest.fixture
def credentials(tmp_path):
    path = tmp_path / "api-credentials.json"
    token = issue_token(path, "fixture-operator", Role.OPERATOR)
    return path, token


def production(journal, credentials_file, **overrides):
    values = REAL_LOOKING | {
        "environment": "production",
        "serving_connect_timeout_seconds": 1,
        "serving_enabled": True,
        "governance_journal": journal,
        "api_credentials_file": credentials_file,
    }
    return Settings(**(values | overrides))


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


# Settings ------------------------------------------------------------------------------


def test_serving_settings_need_the_credential_file_and_the_journal(tmp_path):
    with pytest.raises(ValidationError, match="TENNIS_API_CREDENTIALS_FILE"):
        Settings(environment="test", serving_enabled=True, governance_journal=tmp_path / "g")
    with pytest.raises(ValidationError, match="TENNIS_GOVERNANCE_JOURNAL"):
        Settings(environment="test", serving_enabled=True, api_credentials_file=tmp_path / "c")


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+psycopg://tennis:tennis@localhost:5432/tennis",
        "postgresql+psycopg://tennis:local-only@postgres:5432/tennis",
        "postgresql+psycopg://tennis@localhost:5432/tennis",
        "not a url",
    ],
)
def test_production_refuses_placeholder_or_missing_database_passwords(database_url):
    with pytest.raises(ValidationError, match="database password"):
        Settings(environment="production", **(REAL_LOOKING | {"database_url": database_url}))
    with pytest.raises(ValidationError, match="database password"):
        Settings(environment="production", **REAL_LOOKING, serving_database_url=database_url)


def test_public_summary_hides_paths_and_secrets(journal, credentials):
    settings = production(
        journal, credentials[0], serving_database_url=REAL_LOOKING["database_url"]
    )
    rendered = json.dumps(settings.public_summary())
    for hidden in ("synthetic-db-pass", "synthetic-secret-key-1", str(journal.parent)):
        assert hidden not in rendered
    assert settings.public_summary()["serving_database_role"] == "separate"


def test_role_password_files_complete_each_url_and_fail_closed(tmp_path):
    secret = tmp_path / "role-password"
    secret.write_text("synthetic-role-pass-1\n", encoding="utf-8")
    bare = "postgresql+psycopg://tennis_api@postgres:5432/tennis"
    settings = Settings(
        environment="production",
        **(REAL_LOOKING | {"database_url": bare}),
        database_password_file=secret,
        agent_database_url="postgresql+psycopg://tennis_agent@postgres:5432/tennis",
        agent_database_password_file=secret,
    )
    assert make_url(settings.database_url).password == "synthetic-role-pass-1"
    assert settings.agent_database_url is not None
    assert make_url(settings.agent_database_url.get_secret_value()).username == "tennis_agent"
    assert "synthetic-role-pass-1" not in json.dumps(settings.public_summary())
    assert settings.public_summary()["agent_database_role"] == "separate"
    # A password file without its URL, an empty file and a missing file all refuse.
    with pytest.raises(ValidationError, match="needs a serving database URL"):
        Settings(environment="test", serving_database_password_file=secret)
    (tmp_path / "empty").write_text("\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="empty"):
        Settings(environment="test", database_password_file=tmp_path / "empty")
    with pytest.raises(OSError):
        Settings(environment="test", database_password_file=tmp_path / "missing")


def test_agent_store_never_falls_back_to_the_main_role():
    from tennis_engine.agents.postgres import build_agent_store

    with pytest.raises(ValueError, match="TENNIS_AGENT_DATABASE_URL"):
        build_agent_store(Settings(environment="test"))


# App factory ---------------------------------------------------------------------------


def test_production_app_starts_with_valid_settings_and_fails_closed_on_the_store(
    journal, credentials
):
    path, token = credentials
    client = TestClient(create_production_app(production(journal, path)))
    response = client.get("/v1/tennis/recommendations", headers=bearer(token))
    # The configured database is unreachable: HTTP 503, never a stale record.
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/v1/tennis/recommendations").status_code == 401
    assert client.get("/docs").status_code == 404


def test_serving_off_returns_503_on_every_route(tmp_path):
    client = TestClient(create_production_app(Settings(environment="test")))
    response = client.get("/v1/tennis/recommendations")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_NOT_CONFIGURED"


def test_production_refuses_unsafe_serving_configuration(journal, credentials, tmp_path):
    path, _ = credentials
    missing = tmp_path / "missing.json"
    with pytest.raises(ServingConfigurationError, match="does not exist"):
        create_production_app(production(journal, missing))

    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    with pytest.raises(ServingConfigurationError, match="no credentials"):
        create_production_app(production(journal, empty))

    broken = tmp_path / "broken.json"
    broken.write_text('[{"identity": "x", "role": "operator", "token_sha256": "zz"}]')
    with pytest.raises(ServingConfigurationError, match="not valid") as caught:
        create_production_app(production(journal, broken))
    assert "zz" not in str(caught.value)

    placeholder = tmp_path / "placeholder.json"
    credential = ApiCredential(
        identity="x", role=Role.OPERATOR, token_sha256=token_digest("change-me")
    )
    placeholder.write_text(json.dumps([credential.model_dump(mode="json")]))
    with pytest.raises(ServingConfigurationError, match="placeholder"):
        create_production_app(production(journal, placeholder))

    with pytest.raises(ServingConfigurationError, match="Governance journal"):
        create_production_app(production(tmp_path / "no-journal.sqlite3", path))
    uninitialized = tmp_path / "uninitialized.sqlite3"
    sqlite3.connect(uninitialized).close()
    with pytest.raises(ServingConfigurationError, match="Governance journal"):
        create_production_app(production(uninitialized, path))


def test_development_without_credentials_refuses_every_request(journal, tmp_path):
    settings = Settings(
        environment="development",
        serving_enabled=True,
        governance_journal=journal,
        api_credentials_file=tmp_path / "not-created-yet.json",
    )
    client = TestClient(create_production_app(settings))
    response = client.get("/v1/tennis/recommendations", headers=bearer("anything"))
    assert response.status_code == 401


# Real governance journal, opened read-only ---------------------------------------------


@pytest.fixture
def approved(store, enabled):
    store.save(source_policy(store, BOOK_SOURCE), expected_revision=0, reason="Synthetic book")
    return store


def served(settings, *items):
    decisions = InMemoryDecisionStore()
    for item in items:
        decisions.add(item)
    serving = build_serving(
        settings,
        decision_store=decisions,
        history_store=MemoryHistoryStore(),
        clock=FrozenClock(READ_AT),
    )
    return TestClient(create_app(settings, serving=serving))


def test_wired_service_reads_the_real_journal_and_follows_a_global_stop(
    approved, tmp_path, credentials
):
    path, token = credentials
    settings = Settings(
        environment="test",
        serving_enabled=True,
        governance_journal=tmp_path / "governance.sqlite3",
        api_credentials_file=path,
    )
    client = served(settings, stored(watch()), stored(bet()))
    rows = client.get("/v1/tennis/recommendations", headers=bearer(token)).json()
    by_recorded = {row["recorded_decision"]: row for row in rows["recommendations"]}
    assert by_recorded["WATCH"]["decision"] == "WATCH", by_recorded["WATCH"]["read_time_reasons"]
    # The quote store has no observation for the BET, so the BET is not actionable.
    assert by_recorded["BET"]["decision"] == "NO_BET"
    assert by_recorded["BET"]["actionable"] is False

    approved.set_global_disable(True, reason="Synthetic incident stop")
    rows = client.get("/v1/tennis/recommendations", headers=bearer(token)).json()
    assert {row["decision"] for row in rows["recommendations"]} == {"NO_BET"}
    assert rows["responsible_use"]["reason"] == "GLOBAL_DISABLE"


def test_missing_journal_serves_no_bet_and_fails_readiness(tmp_path, credentials):
    path, token = credentials
    settings = Settings(
        environment="development",
        serving_enabled=True,
        governance_journal=tmp_path / "absent.sqlite3",
        api_credentials_file=path,
    )
    client = served(settings, stored(watch()))
    row = client.get("/v1/tennis/recommendations", headers=bearer(token)).json()["recommendations"][
        0
    ]
    assert row["recorded_decision"] == "WATCH" and row["decision"] == "NO_BET"
    assert row["read_time_reasons"] == ["READ_CHECK_UNAVAILABLE"]
    assert journal_check(tmp_path / "absent.sqlite3").ready is False
    assert not (tmp_path / "absent.sqlite3").exists()


def test_read_only_journal_rejects_writes(journal):
    reader = GovernanceStore(journal, Principal(identity="x", role=Role.OPERATOR), read_only=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.set_global_disable(True, reason="must not be written")
    finally:
        reader.close()
    assert journal_check(journal).ready is True


# Token administration ------------------------------------------------------------------


def test_token_commands_store_digests_only(tmp_path, capsys):
    path = tmp_path / "creds.json"
    assert (
        main(["create-api-token", "--identity", "ops", "--role", "operator", "--file", str(path)])
        == 0
    )
    token = json.loads(capsys.readouterr().out)["token"]
    assert token not in path.read_text(encoding="utf-8")
    assert [item.identity for item in load_credentials(path)] == ["ops"]
    assert (
        main(["create-api-token", "--identity", "ops", "--role", "operator", "--file", str(path)])
        == 2
    )
    capsys.readouterr()
    rotated = issue_token(path, "ops", Role.OPERATOR, rotate=True)
    assert rotated != token and len(load_credentials(path)) == 1
    assert revoke_token(path, "ops") is True
    assert main(["revoke-api-token", "--identity", "ops", "--file", str(path)]) == 2
    assert load_credentials(path) == ()
