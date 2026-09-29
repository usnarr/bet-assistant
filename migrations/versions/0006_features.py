"""Create F07 immutable feature snapshots and dataset manifests."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006_features"
down_revision = "0005_settlement"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)
CLASSES = "('PROSPECTIVE','ARCHIVED','RESEARCH_ONLY')"
APPEND_ONLY_TABLES = ("feature_snapshot", "dataset_manifest", "dataset_row")


def upgrade() -> None:
    op.create_table(
        "feature_snapshot",
        sa.Column(
            "match_id", UUID, sa.ForeignKey("tennis.canonical_match.match_id"), primary_key=True
        ),
        sa.Column("as_of", TS, primary_key=True),
        sa.Column("feature_set", sa.Text(), primary_key=True),
        sa.Column("feature_set_sha256", sa.String(64), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("availability", sa.Text(), nullable=False),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("recorded_at", TS, nullable=False),
        sa.CheckConstraint(f"mode IN {CLASSES}", name="ck_feature_snapshot_mode"),
        sa.CheckConstraint(f"availability IN {CLASSES}", name="ck_feature_snapshot_class"),
        sa.CheckConstraint(
            "mode <> 'PROSPECTIVE' OR availability = 'PROSPECTIVE'",
            name="ck_feature_snapshot_prospective",
        ),
        schema="tennis",
    )
    op.create_table(
        "dataset_manifest",
        sa.Column("dataset_id", UUID, primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("availability", sa.Text(), nullable=False),
        sa.Column("research_only", sa.Boolean(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.CheckConstraint(
            "research_only = (availability = 'RESEARCH_ONLY')", name="ck_dataset_research_only"
        ),
        sa.CheckConstraint(
            "mode <> 'PROSPECTIVE' OR availability = 'PROSPECTIVE'",
            name="ck_dataset_prospective",
        ),
        schema="tennis",
    )
    op.create_table(
        "dataset_row",
        sa.Column(
            "dataset_id", UUID, sa.ForeignKey("tennis.dataset_manifest.dataset_id"), primary_key=True
        ),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column(
            "snapshot_sha256",
            sa.String(64),
            sa.ForeignKey("tennis.feature_snapshot.snapshot_sha256"),
            nullable=False,
        ),
        schema="tennis",
    )
    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only "
            f"BEFORE UPDATE OR DELETE ON tennis.{table} "
            "FOR EACH ROW EXECUTE FUNCTION tennis.reject_history_mutation()"
        )
    op.execute(
        "INSERT INTO tennis.platform_component(component, version) VALUES ('features', 'F07-v1')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM tennis.platform_component WHERE component = 'features'")
    for table in reversed(APPEND_ONLY_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON tennis.{table}")
    op.drop_table("dataset_row", schema="tennis")
    op.drop_table("dataset_manifest", schema="tennis")
    op.drop_table("feature_snapshot", schema="tennis")
