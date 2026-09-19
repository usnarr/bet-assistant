"""Create append-only PostgreSQL governance and audit history."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_governance"
down_revision = "0001_extensions"
branch_labels = None
depends_on = None

TABLES = ("governance_revision", "evidence_object", "stop_revision", "audit_event")


def _append_only_trigger(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER {table}_append_only "
        f"BEFORE UPDATE OR DELETE ON tennis.{table} "
        "FOR EACH ROW EXECUTE FUNCTION tennis.reject_history_mutation()"
    )


def upgrade() -> None:
    op.execute(
        "CREATE FUNCTION tennis.reject_history_mutation() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'append-only history'; END; $$"
    )
    op.create_table(
        "governance_revision",
        sa.Column("revision", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("entity_key", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.CheckConstraint("kind IN ('source', 'payout', 'responsible_use')", name="ck_governance_kind"),
        schema="tennis",
    )
    op.create_index(
        "ix_governance_entity_history",
        "governance_revision",
        ["kind", "entity_key", "recorded_at"],
        schema="tennis",
    )
    op.create_table(
        "evidence_object",
        sa.Column("evidence_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False, unique=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_evidence_sha256"),
        schema="tennis",
    )
    op.create_index(
        "ix_evidence_document_history",
        "evidence_object",
        ["document_id", "recorded_at"],
        schema="tennis",
    )
    op.create_table(
        "stop_revision",
        sa.Column("revision", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        schema="tennis",
    )
    op.create_index(
        "ix_stop_scope_history",
        "stop_revision",
        ["scope", "recorded_at"],
        schema="tennis",
    )
    op.create_table(
        "audit_event",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        schema="tennis",
    )
    op.create_index(
        "ix_audit_entity_history",
        "audit_event",
        ["entity_type", "entity_id", "recorded_at"],
        schema="tennis",
    )
    for table in TABLES:
        _append_only_trigger(table)
    op.execute(
        "INSERT INTO tennis.stop_revision(scope, disabled, actor, reason) "
        "VALUES ('global', TRUE, 'migration', 'Fail closed until reviewed configuration is applied')"
    )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON tennis.{table}")
    op.drop_index("ix_audit_entity_history", table_name="audit_event", schema="tennis")
    op.drop_table("audit_event", schema="tennis")
    op.drop_index("ix_stop_scope_history", table_name="stop_revision", schema="tennis")
    op.drop_table("stop_revision", schema="tennis")
    op.drop_index("ix_evidence_document_history", table_name="evidence_object", schema="tennis")
    op.drop_table("evidence_object", schema="tennis")
    op.drop_index("ix_governance_entity_history", table_name="governance_revision", schema="tennis")
    op.drop_table("governance_revision", schema="tennis")
    op.execute("DROP FUNCTION tennis.reject_history_mutation()")
