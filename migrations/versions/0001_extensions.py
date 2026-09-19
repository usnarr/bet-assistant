"""Create the platform schema and artifact manifest convention."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_extensions"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS tennis")
    op.create_table(
        "platform_component",
        sa.Column("component", sa.Text(), primary_key=True),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("installed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="tennis",
    )
    op.create_table(
        "artifact_manifest",
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("manifest_sha256 ~ '^[0-9a-f]{64}$'", name="ck_artifact_sha256"),
        schema="tennis",
    )
    op.create_index(
        "ix_artifact_kind_created_at",
        "artifact_manifest",
        ["kind", "created_at"],
        schema="tennis",
    )
    op.execute(
        "INSERT INTO tennis.platform_component(component, version) VALUES ('foundation', 'F02-v1')"
    )


def downgrade() -> None:
    op.drop_index("ix_artifact_kind_created_at", table_name="artifact_manifest", schema="tennis")
    op.drop_table("artifact_manifest", schema="tennis")
    op.drop_table("platform_component", schema="tennis")
    op.execute("DROP SCHEMA IF EXISTS tennis")
