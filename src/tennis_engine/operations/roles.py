"""F15.5 least-privilege database role for the read-only API.

An administrator creates the login role and its password outside the repository. This
module grants it only `SELECT` on the `tennis` schema and on `alembic_version` (for the
readiness check). It grants no write privilege. The API then uses the role through
`TENNIS_SERVING_DATABASE_URL`.
"""

import re

from sqlalchemy import Engine, text

ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def grant_serving_reader(engine: Engine, role: str) -> None:
    if not ROLE_NAME.match(role):
        raise ValueError("A role name has lowercase letters, digits and underscores only")
    quoted = f'"{role}"'
    with engine.begin() as db:
        exists = db.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role})
        if exists.first() is None:
            raise ValueError("The role does not exist; an administrator creates it first")
        for statement in (
            f"REVOKE ALL ON ALL TABLES IN SCHEMA tennis FROM {quoted}",
            f"GRANT USAGE ON SCHEMA tennis TO {quoted}",
            f"GRANT SELECT ON ALL TABLES IN SCHEMA tennis TO {quoted}",
            f"GRANT SELECT ON TABLE public.alembic_version TO {quoted}",
        ):
            db.execute(text(statement))


def revoke_serving_reader(engine: Engine, role: str) -> None:
    if not ROLE_NAME.match(role):
        raise ValueError("A role name has lowercase letters, digits and underscores only")
    quoted = f'"{role}"'
    with engine.begin() as db:
        for statement in (
            f"REVOKE ALL ON ALL TABLES IN SCHEMA tennis FROM {quoted}",
            f"REVOKE USAGE ON SCHEMA tennis FROM {quoted}",
            f"REVOKE ALL ON TABLE public.alembic_version FROM {quoted}",
        ):
            db.execute(text(statement))
