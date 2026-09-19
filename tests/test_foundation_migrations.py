from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from tennis_engine.infrastructure.database import EXPECTED_ALEMBIC_REVISION


def test_migrations_have_one_expected_head_and_compile_offline(capsys):
    config = Config("alembic.ini")
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [EXPECTED_ALEMBIC_REVISION]
    command.upgrade(config, "head", sql=True)
    sql = capsys.readouterr().out
    assert "CREATE TABLE tennis.artifact_manifest" in sql
    assert "CREATE TABLE tennis.governance_revision" in sql
    assert "INSERT INTO alembic_version" in sql
