"""Create the F14 append-only store of F12 decision records and their display context."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010_decision_records"
down_revision = "0009_identity_formats"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "decision_record",
        sa.Column("decision_id", UUID, primary_key=True),
        sa.Column("decision_key", sa.Text(), nullable=False),
        sa.Column("ledger_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "supersedes",
            UUID,
            sa.ForeignKey("tennis.decision_record.decision_id"),
            nullable=True,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("bookmaker", sa.Text(), nullable=True),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("match_id", UUID, nullable=False),
        sa.Column("scheduled_start", TS, nullable=False),
        sa.Column("decided_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("quote_source_id", sa.Text(), nullable=True),
        sa.Column("quote_observed_at", TS, nullable=True),
        sa.Column("stake", sa.Numeric(14, 2), nullable=False),
        sa.Column("record", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("stored_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('BET','WATCH','NO_BET')", name="ck_decision_status"),
        sa.CheckConstraint("status = 'BET' OR stake = 0", name="ck_decision_stake_zero"),
        sa.CheckConstraint("stake >= 0", name="ck_decision_stake_nonnegative"),
        sa.CheckConstraint("expires_at > decided_at", name="ck_decision_expiry"),
        sa.CheckConstraint("version >= 1", name="ck_decision_version"),
        sa.CheckConstraint(
            "(quote_source_id IS NULL) = (quote_observed_at IS NULL)",
            name="ck_decision_quote_observation",
        ),
        sa.CheckConstraint("supersedes IS NULL OR version > 1", name="ck_decision_supersedes"),
        schema="tennis",
    )
    op.create_index(
        "ix_decision_record_order",
        "decision_record",
        ["scheduled_start", "decision_id"],
        schema="tennis",
    )
    op.create_index("ix_decision_record_match", "decision_record", ["match_id"], schema="tennis")
    op.create_index(
        "ix_decision_record_supersedes", "decision_record", ["supersedes"], schema="tennis"
    )
    op.execute(
        "CREATE TRIGGER decision_record_append_only "
        "BEFORE UPDATE OR DELETE ON tennis.decision_record "
        "FOR EACH ROW EXECUTE FUNCTION tennis.reject_history_mutation()"
    )
    op.execute(
        "INSERT INTO tennis.platform_component(component, version) "
        "VALUES ('decision_records', 'F14-v1')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM tennis.platform_component WHERE component = 'decision_records'")
    op.execute("DROP TRIGGER IF EXISTS decision_record_append_only ON tennis.decision_record")
    op.drop_index(
        "ix_decision_record_supersedes", table_name="decision_record", schema="tennis"
    )
    op.drop_index("ix_decision_record_match", table_name="decision_record", schema="tennis")
    op.drop_index("ix_decision_record_order", table_name="decision_record", schema="tennis")
    op.drop_table("decision_record", schema="tennis")
