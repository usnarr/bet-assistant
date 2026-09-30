"""Add F04 deciding-set rule versions and the player name-key index."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009_identity_formats"
down_revision = "0008_risk_reservations"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)
APPEND_ONLY_TABLES = ("edition_format_version", "player_name_key")


def upgrade() -> None:
    op.create_table(
        "edition_format_version",
        sa.Column(
            "edition_id",
            UUID,
            sa.ForeignKey("tennis.tournament_edition.edition_id"),
            primary_key=True,
        ),
        sa.Column("draw_stage", sa.Text(), primary_key=True),
        sa.Column("best_of", sa.Text(), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("deciding_set", sa.Text(), nullable=False),
        sa.Column("reference", sa.Text(), nullable=False),
        sa.Column("corrects_version", sa.Integer()),
        sa.Column("observed_at", TS, nullable=False),
        sa.Column("ingested_at", TS, nullable=False),
        sa.Column("effective_at", TS),
        sa.Column("source_available_at", TS),
        sa.Column("availability_evidence_id", sa.Text()),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_edition_format_version"),
        sa.CheckConstraint(
            "draw_stage IN ('QUALIFYING','MAIN')", name="ck_edition_format_stage"
        ),
        sa.CheckConstraint(
            "best_of IN ('BEST_OF_3','BEST_OF_5')", name="ck_edition_format_best_of"
        ),
        sa.CheckConstraint(
            "deciding_set IN ('TIEBREAK_7','TIEBREAK_10','MATCH_TIEBREAK_10',"
            "'ADVANTAGE','UNKNOWN')",
            name="ck_edition_format_deciding_set",
        ),
        sa.CheckConstraint(
            "corrects_version IS NULL OR (corrects_version >= 1 AND corrects_version < version)",
            name="ck_edition_format_correction",
        ),
        sa.CheckConstraint("ingested_at >= observed_at", name="ck_edition_format_ingested"),
        sa.CheckConstraint(
            "(source_available_at IS NULL) = (availability_evidence_id IS NULL)",
            name="ck_edition_format_archive_evidence",
        ),
        schema="tennis",
    )
    op.create_table(
        "player_name_key",
        sa.Column("name_key", sa.Text(), primary_key=True),
        sa.Column(
            "player_id", UUID, sa.ForeignKey("tennis.player.player_id"), primary_key=True
        ),
        schema="tennis",
    )
    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON tennis.{table} "
            "FOR EACH ROW EXECUTE FUNCTION tennis.reject_history_mutation()"
        )
    op.execute(
        "UPDATE tennis.platform_component SET version = 'F04-v2' WHERE component = 'identity'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE tennis.platform_component SET version = 'F04-v1' WHERE component = 'identity'"
    )
    for table in reversed(APPEND_ONLY_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON tennis.{table}")
    op.drop_table("player_name_key", schema="tennis")
    op.drop_table("edition_format_version", schema="tennis")
