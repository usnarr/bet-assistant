"""Create the F11.8 model bundle registry and the F13.9 append-only champion events."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0013_model_registry"
down_revision = "0012_agent_records"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "model_bundle",
        sa.Column("bundle_id", UUID, primary_key=True),
        sa.Column("family", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("bundle", JSONB, nullable=False),
        sa.Column("registered_at", TS, nullable=False),
        sa.UniqueConstraint("family", "version", name="uq_model_bundle_version"),
        sa.CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_model_bundle_sha"),
        schema="tennis",
    )
    # The current champion of a family is the bundle of its last event. NULL: no champion.
    op.create_table(
        "champion_event",
        sa.Column("family", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_id", UUID, nullable=False, unique=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "bundle_id", UUID, sa.ForeignKey("tennis.model_bundle.bundle_id"), nullable=True
        ),
        sa.Column(
            "previous_bundle_id",
            UUID,
            sa.ForeignKey("tennis.model_bundle.bundle_id"),
            nullable=True,
        ),
        sa.Column("decision_id", UUID, nullable=True),
        sa.Column("decision_sha256", sa.String(64), nullable=True),
        sa.Column("event", JSONB, nullable=False),
        sa.Column("recorded_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("family", "sequence", name="pk_champion_event"),
        sa.CheckConstraint("sequence >= 1", name="ck_champion_sequence"),
        sa.CheckConstraint("kind IN ('PROMOTE','ROLLBACK')", name="ck_champion_kind"),
        # A promotion always names a bundle and its decision; a rollback never has one.
        sa.CheckConstraint(
            "(kind = 'PROMOTE' AND bundle_id IS NOT NULL AND decision_id IS NOT NULL "
            "AND decision_sha256 IS NOT NULL) OR (kind = 'ROLLBACK' AND decision_id IS NULL)",
            name="ck_champion_promote_decision",
        ),
        schema="tennis",
    )
    # A promotion decision is used once.
    op.create_index(
        "ux_champion_event_decision",
        "champion_event",
        ["decision_id"],
        unique=True,
        schema="tennis",
        postgresql_where=sa.text("decision_id IS NOT NULL"),
    )
    for table in ("model_bundle", "champion_event"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON tennis.{table} "
            "FOR EACH ROW EXECUTE FUNCTION tennis.reject_history_mutation()"
        )
    op.execute(
        "INSERT INTO tennis.platform_component(component, version) "
        "VALUES ('model_registry', 'F11.8-v1')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM tennis.platform_component WHERE component = 'model_registry'")
    for table in ("champion_event", "model_bundle"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON tennis.{table}")
    op.drop_index("ux_champion_event_decision", table_name="champion_event", schema="tennis")
    op.drop_table("champion_event", schema="tennis")
    op.drop_table("model_bundle", schema="tennis")
