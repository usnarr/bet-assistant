"""Database engine creation and schema readiness checks."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import Connection

EXPECTED_ALEMBIC_REVISION = "0002_governance"


def build_engine(database_url: str, *, pool_pre_ping: bool = True) -> Engine:
    return create_engine(database_url, pool_pre_ping=pool_pre_ping)


@contextmanager
def connection(engine: Engine) -> Iterator[Connection]:
    with engine.begin() as opened:
        yield opened


def current_revision(engine: Engine) -> str | None:
    try:
        with engine.connect() as opened:
            return opened.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
    except Exception:
        return None


def database_ready(engine: Engine) -> tuple[bool, str]:
    try:
        with engine.connect() as opened:
            opened.execute(text("SELECT 1"))
            revision = opened.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    except Exception as error:
        return False, f"{type(error).__name__}: database unavailable or not migrated"
    if revision != EXPECTED_ALEMBIC_REVISION:
        return False, f"schema revision {revision!r}, expected {EXPECTED_ALEMBIC_REVISION!r}"
    return True, revision
