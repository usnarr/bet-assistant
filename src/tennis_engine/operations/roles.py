"""F15.5 least-privilege PostgreSQL roles, one per Compose component.

The owner role (the Compose `POSTGRES_USER`) owns the schema. Only one-shot services use
it: `migrate`, `db-roles` and `admin`. Every long-running component has its own login
role. `provision_roles` creates each role when it is missing, sets its password from a
secret file, revokes every table privilege and then grants only the privileges in
`COMPONENT_ROLES`. It then reads the effective privileges back and fails when a role has
more or less than its specification. Compose runs it after each migration, so a new
table never stays readable or writable by a role that must not use it.

The component list comes from `compose.yaml`. Prometheus and Grafana do not connect to
PostgreSQL, so there is no monitoring role. Compose has no worker service yet.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from tennis_engine.infrastructure.settings import PLACEHOLDER_SECRETS

ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
ALL_TABLES = "*"
ALEMBIC = "public.alembic_version"
MIN_PASSWORD_LENGTH = 16


@dataclass(frozen=True)
class ComponentRole:
    component: str
    role: str
    # Secret file name (from `scripts/init_local_secrets.py`) that holds the password.
    secret: str
    # Tables in the `tennis` schema with SELECT. `*` means every table in the schema.
    reads: tuple[str, ...] = ()
    # Table -> write privileges. Nothing else is granted.
    writes: Mapping[str, tuple[str, ...]] | None = None
    # SELECT on `public.alembic_version` for the readiness check.
    alembic: bool = False
    replication: bool = False


API = ComponentRole(
    component="api",
    role="tennis_api",
    secret="postgres-api-password",
    reads=(ALL_TABLES,),
    alembic=True,
)
SCHEDULER = ComponentRole(
    component="scheduler",
    role="tennis_scheduler",
    secret="postgres-scheduler-password",
    reads=(ALL_TABLES,),
    # Job runs and attempts are append-only. A lease is current state (migration 0011).
    # `SELECT ... FOR UPDATE` on a lease needs UPDATE.
    writes={
        "job_run": ("INSERT",),
        "job_attempt": ("INSERT",),
        "resource_lease": ("INSERT", "UPDATE"),
    },
    alembic=True,
)
AGENT = ComponentRole(
    component="agent",
    role="tennis_agent",
    secret="postgres-agent-password",
    # F18 traces and proposals only. INSERT without UPDATE or DELETE keeps them append-only
    # in addition to the migration 0012 triggers.
    reads=("agent_trace", "agent_proposal"),
    writes={"agent_trace": ("INSERT",), "agent_proposal": ("INSERT",)},
)
BACKUP = ComponentRole(
    component="backup",
    role="tennis_backup",
    secret="postgres-backup-password",
    # Streams a base backup only (F15.7). No table privilege.
    replication=True,
)
COMPONENT_ROLES: tuple[ComponentRole, ...] = (API, SCHEDULER, AGENT, BACKUP)


class RolePrivilegeMismatch(ValueError):
    """A role has more, or less, privilege than its specification."""


def _check_name(role: str) -> str:
    if not ROLE_NAME.match(role):
        raise ValueError("A role name has lowercase letters, digits and underscores only")
    return f'"{role}"'


def read_passwords(directory: Path, roles: Iterable[ComponentRole]) -> dict[str, str]:
    """Role -> password from the secret files. A missing or weak password fails closed."""
    passwords: dict[str, str] = {}
    for spec in roles:
        path = directory / spec.secret.replace("-", "_")
        if not path.exists():
            path = directory / spec.secret
        value = path.read_text(encoding="utf-8").strip()
        if len(value) < MIN_PASSWORD_LENGTH or value in PLACEHOLDER_SECRETS:
            raise ValueError(f"The password for {spec.component} is too weak")
        passwords[spec.role] = value
    return passwords


def _tables(db: Connection) -> tuple[str, ...]:
    rows = db.execute(
        text(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'tennis' AND c.relkind IN ('r', 'p', 'v', 'm') "
            "ORDER BY c.relname"
        )
    )
    return tuple(row[0] for row in rows)


def expected_privileges(spec: ComponentRole, tables: Iterable[str]) -> set[tuple[str, str]]:
    names = tuple(tables)
    reads = names if ALL_TABLES in spec.reads else spec.reads
    wanted = {(f"tennis.{table}", "SELECT") for table in reads if table in names}
    for table, privileges in (spec.writes or {}).items():
        if table in names:
            wanted |= {(f"tennis.{table}", privilege) for privilege in privileges}
    if spec.alembic:
        wanted.add((ALEMBIC, "SELECT"))
    return wanted


def effective_privileges(db: Connection, role: str) -> set[tuple[str, str]]:
    """Every table privilege the role has, directly, through PUBLIC or membership."""
    rows = db.execute(
        text(
            "SELECT n.nspname || '.' || c.relname, p.privilege FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "CROSS JOIN unnest(CAST(:privileges AS text[])) AS p(privilege) "
            "WHERE c.relkind IN ('r', 'p', 'v', 'm') AND (n.nspname = 'tennis' "
            "OR (n.nspname = 'public' AND c.relname = 'alembic_version')) "
            "AND has_table_privilege(:role, c.oid, p.privilege)"
        ),
        {"role": role, "privileges": list(TABLE_PRIVILEGES)},
    )
    return {(row[0], row[1]) for row in rows}


def verify_role(db: Connection, spec: ComponentRole) -> None:
    attributes = db.execute(
        text(
            "SELECT rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolreplication, "
            "has_schema_privilege(rolname, 'tennis', 'CREATE'), "
            "has_schema_privilege(rolname, 'public', 'CREATE') "
            "OR has_database_privilege(rolname, current_database(), 'CREATE'), "
            "EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member = r.oid) "
            "FROM pg_roles r WHERE rolname = :role"
        ),
        {"role": spec.role},
    ).first()
    if attributes is None:
        raise RolePrivilegeMismatch(f"{spec.role}: the role does not exist")
    superuser, create_role, create_db, bypass_rls, replication, *rest = attributes
    tennis_create, public_create, member = rest
    problems = []
    if superuser or create_role or create_db or bypass_rls:
        problems.append("administrative attribute")
    if replication != spec.replication:
        problems.append("replication attribute")
    if tennis_create or public_create:
        problems.append("schema CREATE")
    if member:
        problems.append("role membership")
    actual = effective_privileges(db, spec.role)
    wanted = expected_privileges(spec, _tables(db))
    if actual - wanted:
        problems.append(f"extra privileges {sorted(actual - wanted)}")
    if wanted - actual:
        problems.append(f"missing privileges {sorted(wanted - actual)}")
    if problems:
        raise RolePrivilegeMismatch(f"{spec.role}: {'; '.join(problems)}")


def _apply(db: Connection, spec: ComponentRole, password: str | None) -> None:
    quoted = _check_name(spec.role)
    exists = db.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": spec.role})
    if exists.first() is None:
        if password is None:
            raise ValueError("The role does not exist; an administrator creates it first")
        db.execute(text(f"CREATE ROLE {quoted} LOGIN"))
    replication = "REPLICATION" if spec.replication else "NOREPLICATION"
    db.execute(
        text(
            f"ALTER ROLE {quoted} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            f"NOBYPASSRLS {replication}"
        )
    )
    if password is not None:
        # The server quotes the password (`%L`). A failure hides the statement and the
        # parameters, because both hold the password.
        try:
            statement = db.execute(
                text(
                    "SELECT format('ALTER ROLE %I PASSWORD %L', "
                    "CAST(:role AS text), CAST(:password AS text))"
                ),
                {"role": spec.role, "password": password},
            ).scalar_one()
            db.exec_driver_sql(statement)
        except DBAPIError:
            raise ValueError(f"{spec.role}: the password change failed") from None
    for statement in (
        f"REVOKE ALL ON ALL TABLES IN SCHEMA tennis FROM {quoted}",
        f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA tennis FROM {quoted}",
        f"REVOKE ALL ON SCHEMA tennis FROM {quoted}",
        f"REVOKE ALL ON TABLE {ALEMBIC} FROM {quoted}",
    ):
        db.execute(text(statement))
    tables = _tables(db)
    wanted = expected_privileges(spec, tables)
    if any(table.startswith("tennis.") for table, _ in wanted):
        db.execute(text(f"GRANT USAGE ON SCHEMA tennis TO {quoted}"))
    for table, privilege in sorted(wanted):
        db.execute(text(f"GRANT {privilege} ON TABLE {table} TO {quoted}"))


def provision_roles(
    engine: Engine,
    passwords: Mapping[str, str],
    roles: Iterable[ComponentRole] = COMPONENT_ROLES,
) -> tuple[str, ...]:
    """Create or update each role, grant its privileges and verify them. Run as the owner."""
    specs = tuple(roles)
    with engine.begin() as db:
        for spec in specs:
            _apply(db, spec, passwords[spec.role])
        for spec in specs:
            verify_role(db, spec)
    return tuple(spec.role for spec in specs)


def grant_serving_reader(engine: Engine, role: str) -> None:
    """Give an existing role the API privileges (SELECT only) without a password change."""
    _check_name(role)
    spec = ComponentRole(component="api", role=role, secret="", reads=(ALL_TABLES,), alembic=True)
    with engine.begin() as db:
        _apply(db, spec, None)
        verify_role(db, spec)


def revoke_serving_reader(engine: Engine, role: str) -> None:
    quoted = _check_name(role)
    with engine.begin() as db:
        for statement in (
            f"REVOKE ALL ON ALL TABLES IN SCHEMA tennis FROM {quoted}",
            f"REVOKE USAGE ON SCHEMA tennis FROM {quoted}",
            f"REVOKE ALL ON TABLE {ALEMBIC} FROM {quoted}",
        ):
            db.execute(text(statement))
