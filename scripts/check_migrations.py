"""Validate one migration head and compile the full PostgreSQL upgrade offline."""

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from tennis_engine.infrastructure.database import EXPECTED_ALEMBIC_REVISION


def main() -> int:
    config = Config("alembic.ini")
    heads = ScriptDirectory.from_config(config).get_heads()
    if heads != [EXPECTED_ALEMBIC_REVISION]:
        raise SystemExit(f"Expected one head {EXPECTED_ALEMBIC_REVISION!r}, found {heads!r}")
    command.upgrade(config, "head", sql=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
