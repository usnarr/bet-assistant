"""F15.5 per-component PostgreSQL roles: each role does its work and nothing else."""

import dataclasses
import os
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from operations_support import drills
from recovery_support import seed
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from tennis_engine.agents.contracts import AgentRole
from tennis_engine.agents.postgres import build_agent_store
from tennis_engine.agents.proposals import new_proposal
from tennis_engine.common.ids import stable_id
from tennis_engine.infrastructure.database import (
    EXPECTED_ALEMBIC_REVISION,
    build_engine,
    database_ready,
)
from tennis_engine.infrastructure.object_store import LocalObjectStore
from tennis_engine.infrastructure.settings import Settings
from tennis_engine.operations.leases import PostgresLeaseStore
from tennis_engine.operations.postgres import PostgresJobStore
from tennis_engine.operations.recovery import reconcile_ledgers
from tennis_engine.operations.roles import (
    COMPONENT_ROLES,
    RolePrivilegeMismatch,
    provision_roles,
    read_passwords,
    verify_role,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="isolated PostgreSQL not configured"
)
NOW = datetime(2026, 9, 20, 13, tzinfo=UTC)
# Test-only role names, so the test never changes a role that another database uses.
ROLES = {
    spec.component: dataclasses.replace(spec, role=f"f15t_{spec.role}") for spec in COMPONENT_ROLES
}
# Synthetic test-only passwords for an isolated test database.
PASSWORDS = {spec.role: f"synthetic-{spec.component}-password" for spec in ROLES.values()}


def role_url(component: str) -> str:
    spec = ROLES[component]
    return (
        make_url(os.environ["TEST_DATABASE_URL"])
        .set(username=spec.role, password=PASSWORDS[spec.role])
        .render_as_string(hide_password=False)
    )


def drop_roles(engine) -> None:
    with engine.begin() as db:
        for spec in ROLES.values():
            exists = db.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": spec.role}
            ).first()
            if exists is not None:
                db.execute(text(f'DROP OWNED BY "{spec.role}"'))
                db.execute(text(f'DROP ROLE "{spec.role}"'))


@pytest.fixture
def engine(monkeypatch):
    database_url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setenv("TENNIS_DATABASE_URL", database_url)
    config = Config("alembic.ini")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    instance = build_engine(database_url)
    drop_roles(instance)
    provision_roles(instance, PASSWORDS, ROLES.values())
    try:
        yield instance
    finally:
        drop_roles(instance)
        instance.dispose()
        command.downgrade(config, "base")


@pytest.fixture
def connect():
    engines = []

    def open_role(component: str):
        engines.append(build_engine(role_url(component)))
        return engines[-1]

    yield open_role
    for item in engines:
        item.dispose()


def job_stores(role_engine):
    leases = PostgresLeaseStore(role_engine)
    return PostgresJobStore(role_engine, leases), leases


def proposal():
    from agent_support import AS_OF

    return new_proposal(
        role=AgentRole.MONITORING,
        tool="propose_incident_triage",
        subject_id="synthetic-scope",
        kind="TRIAGE",
        fields={"severity": "CRITICAL"},
        evidence_ids=("synthetic-alert-1",),
        rationale="The alert breached its rule.",
        trace_id=stable_id("trace", "roles"),
        created_at=AS_OF,
    )


def refused(role_engine, statements) -> None:
    for statement in statements:
        with pytest.raises(DBAPIError, match="permission denied"), role_engine.begin() as db:
            db.execute(text(statement))


def test_provisioning_is_repeatable_and_repairs_extra_privilege(engine):
    provision_roles(engine, PASSWORDS, ROLES.values())
    api = ROLES["api"]
    with engine.begin() as db:
        db.execute(text(f"GRANT DELETE ON tennis.decision_record TO {api.role}"))
    with pytest.raises(RolePrivilegeMismatch, match="extra privileges"), engine.connect() as db:
        verify_role(db, api)
    with engine.begin() as db:
        db.execute(text(f"ALTER ROLE {api.role} CREATEDB"))
    with pytest.raises(RolePrivilegeMismatch, match="administrative"), engine.connect() as db:
        verify_role(db, api)
    provision_roles(engine, PASSWORDS, ROLES.values())
    with engine.connect() as db:
        for spec in ROLES.values():
            verify_role(db, spec)
        # A table that a later migration adds is caught until provisioning runs again.
        db.execute(text("CREATE TABLE tennis.f15t_new_table (id int)"))
        with pytest.raises(RolePrivilegeMismatch, match="missing privileges"):
            verify_role(db, ROLES["api"])
        verify_role(db, ROLES["agent"])
        db.rollback()


def test_api_role_reads_and_cannot_write(engine, connect, tmp_path):
    seed(engine, LocalObjectStore(tmp_path / "objects"))
    api = connect("api")
    assert database_ready(api) == (True, EXPECTED_ALEMBIC_REVISION)
    with api.connect() as db:
        assert db.execute(text("SELECT count(*) FROM tennis.decision_record")).scalar_one()
    refused(
        api,
        (
            "DELETE FROM tennis.decision_record",
            "UPDATE tennis.resource_lease SET owner = 'x'",
            "INSERT INTO tennis.agent_trace SELECT * FROM tennis.agent_trace",
            "TRUNCATE tennis.settlement_ledger CASCADE",
            "CREATE TABLE tennis.intruder (id int)",
            "CREATE TABLE public.intruder (id int)",
        ),
    )


def test_scheduler_role_runs_jobs_and_leases_only(engine, connect, tmp_path):
    seed(engine, LocalObjectStore(tmp_path / "objects"))
    scheduler = connect("scheduler")
    drills(lambda: job_stores(scheduler))
    assert reconcile_ledgers(scheduler, NOW).unbalanced == ()
    assert database_ready(scheduler) == (True, EXPECTED_ALEMBIC_REVISION)
    refused(
        scheduler,
        (
            "DELETE FROM tennis.resource_lease",
            "INSERT INTO tennis.decision_record SELECT * FROM tennis.decision_record",
            "DELETE FROM tennis.settlement_ledger",
            "INSERT INTO tennis.model_bundle SELECT * FROM tennis.model_bundle",
            "INSERT INTO tennis.agent_proposal SELECT * FROM tennis.agent_proposal",
            "TRUNCATE tennis.job_run CASCADE",
        ),
    )


def test_agent_role_writes_only_its_append_only_tables(engine, tmp_path):
    from agent_support import call, final, run, turn

    password_file = tmp_path / "agent-password"
    password_file.write_text(PASSWORDS[ROLES["agent"].role] + "\n", encoding="utf-8")
    bare = make_url(role_url("agent")).set(password=None).render_as_string(hide_password=False)
    settings = Settings(
        environment="test",
        agent_database_url=bare,
        agent_database_password_file=password_file,
    )
    store = build_agent_store(settings)
    try:
        stored, created = store.propose(proposal())
        assert created and store.propose(proposal()) == (stored, False)
        result, _ = run([turn(call("evaluate_quote")), turn(final=final())], store=store)
        assert result.trace_recorded
        assert store.trace(result.trace.trace_id) == result.trace
        refused(
            store.engine,
            (
                "UPDATE tennis.agent_trace SET status = 'COMPLETED'",
                "DELETE FROM tennis.agent_proposal",
                "TRUNCATE tennis.agent_trace",
                "SELECT count(*) FROM tennis.settlement_ledger",
                "SELECT count(*) FROM tennis.settlement_ledger_entry",
                "SELECT count(*) FROM tennis.decision_record",
                "SELECT count(*) FROM tennis.model_bundle",
                "INSERT INTO tennis.champion_event SELECT * FROM tennis.champion_event",
                "SELECT count(*) FROM tennis.resource_lease",
                "SELECT version_num FROM public.alembic_version",
            ),
        )
    finally:
        store.engine.dispose()


def test_backup_role_has_no_table_access(engine, connect):
    backup = connect("backup")
    with engine.connect() as db:
        replication = db.execute(
            text("SELECT rolreplication FROM pg_roles WHERE rolname = :role"),
            {"role": ROLES["backup"].role},
        ).scalar_one()
    assert replication is True
    refused(
        backup,
        (
            "SELECT count(*) FROM tennis.decision_record",
            "SELECT version_num FROM public.alembic_version",
            "INSERT INTO tennis.job_run SELECT * FROM tennis.job_run",
        ),
    )


def test_password_files_fail_closed(tmp_path):
    specs = (ROLES["api"],)
    with pytest.raises(OSError):
        read_passwords(tmp_path, specs)
    (tmp_path / "postgres-api-password").write_text("short\n", encoding="utf-8")
    with pytest.raises(ValueError, match="too weak"):
        read_passwords(tmp_path, specs)
    (tmp_path / "postgres_api_password").write_text("a-long-synthetic-secret\n", encoding="utf-8")
    assert read_passwords(tmp_path, specs) == {ROLES["api"].role: "a-long-synthetic-secret"}
