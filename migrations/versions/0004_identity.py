"""Create F04 canonical sports entities, versioned facts and identity review history."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_identity"
down_revision = "0003_ingestion"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)

APPEND_ONLY_TABLES = (
    "player",
    "player_alias",
    "tournament",
    "tournament_edition",
    "tournament_alias",
    "canonical_match",
    "match_alias",
    "match_schedule_version",
    "match_status_version",
    "match_result_version",
    "match_stats_version",
    "ranking_snapshot",
    "identity_review_revision",
    "identity_audit",
)
TOURS = "('ATP','WTA')"


def _availability(table: str) -> list:
    """Separate time axes for one fact; see contracts.domain.Availability."""
    return [
        sa.Column("observed_at", TS, nullable=False),
        sa.Column("ingested_at", TS, nullable=False),
        sa.Column("effective_at", TS),
        sa.Column("source_available_at", TS),
        sa.Column("availability_evidence_id", sa.Text()),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.CheckConstraint("ingested_at >= observed_at", name=f"ck_{table}_ingested"),
        sa.CheckConstraint(
            "(source_available_at IS NULL) = (availability_evidence_id IS NULL)",
            name=f"ck_{table}_archive_evidence",
        ),
        sa.CheckConstraint(
            "source_available_at IS NULL OR source_available_at <= observed_at",
            name=f"ck_{table}_archive_order",
        ),
    ]


def _append_only_trigger(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER {table}_append_only "
        f"BEFORE UPDATE OR DELETE ON tennis.{table} "
        "FOR EACH ROW EXECUTE FUNCTION tennis.reject_history_mutation()"
    )


def upgrade() -> None:
    op.create_table(
        "player",
        sa.Column("player_id", UUID, primary_key=True),
        sa.Column("tour", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("birth_date", sa.Date()),
        sa.Column("nationality", sa.String(3)),
        sa.Column("handedness", sa.Text(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.CheckConstraint(f"tour IN {TOURS}", name="ck_player_tour"),
        sa.CheckConstraint(
            "handedness IN ('RIGHT','LEFT','UNKNOWN')", name="ck_player_handedness"
        ),
        schema="tennis",
    )
    op.create_table(
        "player_alias",
        sa.Column("alias_id", UUID, primary_key=True),
        sa.Column(
            "player_id", UUID, sa.ForeignKey("tennis.player.player_id"), nullable=False
        ),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("source_player_id", sa.Text(), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("reviewed_by", sa.Text()),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("recorded_at", TS, nullable=False),
        sa.Column("supersedes", UUID, sa.ForeignKey("tennis.player_alias.alias_id")),
        sa.UniqueConstraint(
            "source_id", "source_player_id", "version", name="uq_player_alias_version"
        ),
        sa.CheckConstraint("version >= 1", name="ck_player_alias_version"),
        sa.CheckConstraint(
            "decision IN ('AUTO_ACCEPT','REVIEW_REQUIRED')", name="ck_player_alias_decision"
        ),
        sa.CheckConstraint(
            "decision <> 'REVIEW_REQUIRED' OR reviewed_by IS NOT NULL",
            name="ck_player_alias_reviewer",
        ),
        schema="tennis",
    )
    op.create_index(
        "ix_player_alias_player", "player_alias", ["player_id"], schema="tennis"
    )
    op.create_table(
        "tournament",
        sa.Column("tournament_id", UUID, primary_key=True),
        sa.Column("tour", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.CheckConstraint(f"tour IN {TOURS}", name="ck_tournament_tour"),
        schema="tennis",
    )
    op.create_table(
        "tournament_edition",
        sa.Column("edition_id", UUID, primary_key=True),
        sa.Column(
            "tournament_id",
            UUID,
            sa.ForeignKey("tennis.tournament.tournament_id"),
            nullable=False,
        ),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("surface", sa.Text(), nullable=False),
        sa.Column("environment", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text()),
        sa.Column("start_date", sa.Date()),
        sa.Column("end_date", sa.Date()),
        sa.UniqueConstraint("tournament_id", "season", name="uq_tournament_edition"),
        sa.CheckConstraint(
            "surface IN ('HARD','CLAY','GRASS','CARPET','UNKNOWN')", name="ck_edition_surface"
        ),
        sa.CheckConstraint(
            "environment IN ('INDOOR','OUTDOOR','UNKNOWN')", name="ck_edition_environment"
        ),
        sa.CheckConstraint(
            "end_date IS NULL OR start_date IS NULL OR end_date >= start_date",
            name="ck_edition_dates",
        ),
        schema="tennis",
    )
    op.create_table(
        "tournament_alias",
        sa.Column("source_id", sa.Text(), primary_key=True),
        sa.Column("source_tournament_id", sa.Text(), primary_key=True),
        sa.Column("season", sa.Integer(), primary_key=True),
        sa.Column(
            "edition_id",
            UUID,
            sa.ForeignKey("tennis.tournament_edition.edition_id"),
            nullable=False,
        ),
        sa.Column("recorded_at", TS, nullable=False),
        schema="tennis",
    )
    op.create_table(
        "canonical_match",
        sa.Column("match_id", UUID, primary_key=True),
        sa.Column(
            "edition_id",
            UUID,
            sa.ForeignKey("tennis.tournament_edition.edition_id"),
            nullable=False,
        ),
        sa.Column("tour", sa.Text(), nullable=False),
        sa.Column("draw_type", sa.Text(), nullable=False),
        sa.Column("draw_stage", sa.Text(), nullable=False),
        sa.Column("round", sa.Text(), nullable=False),
        sa.Column("best_of", sa.Text(), nullable=False),
        sa.Column(
            "player_one_id", UUID, sa.ForeignKey("tennis.player.player_id"), nullable=False
        ),
        sa.Column(
            "player_two_id", UUID, sa.ForeignKey("tennis.player.player_id"), nullable=False
        ),
        sa.Column("created_at", TS, nullable=False),
        sa.CheckConstraint(f"tour IN {TOURS}", name="ck_match_tour"),
        sa.CheckConstraint("draw_type IN ('SINGLES','DOUBLES')", name="ck_match_draw_type"),
        sa.CheckConstraint(
            "best_of IN ('BEST_OF_3','BEST_OF_5','UNKNOWN')", name="ck_match_best_of"
        ),
        sa.CheckConstraint(
            "player_one_id::text < player_two_id::text", name="ck_match_orientation"
        ),
        schema="tennis",
    )
    op.create_table(
        "match_alias",
        sa.Column("alias_id", UUID, primary_key=True),
        sa.Column(
            "match_id", UUID, sa.ForeignKey("tennis.canonical_match.match_id"), nullable=False
        ),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("source_match_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("reviewed_by", sa.Text()),
        sa.Column("swapped", sa.Boolean(), nullable=False),
        sa.Column("recorded_at", TS, nullable=False),
        sa.Column("supersedes", UUID, sa.ForeignKey("tennis.match_alias.alias_id")),
        sa.UniqueConstraint(
            "source_id", "source_match_id", "version", name="uq_match_alias_version"
        ),
        sa.CheckConstraint(
            "decision IN ('AUTO_ACCEPT','REVIEW_REQUIRED')", name="ck_match_alias_decision"
        ),
        schema="tennis",
    )
    versioned = {
        "match_schedule_version": [sa.Column("scheduled_start", TS)],
        "match_status_version": [
            sa.Column("status", sa.Text(), nullable=False),
            sa.Column("actual_start", TS),
            sa.Column("actual_end", TS),
        ],
        "match_result_version": [
            sa.Column("status", sa.Text(), nullable=False),
            sa.Column(
                "winner_id", UUID, sa.ForeignKey("tennis.player.player_id"), nullable=False
            ),
            sa.Column("sets", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column("corrects_version", sa.Integer()),
        ],
    }
    for table, columns in versioned.items():
        op.create_table(
            table,
            sa.Column(
                "match_id",
                UUID,
                sa.ForeignKey("tennis.canonical_match.match_id"),
                primary_key=True,
            ),
            sa.Column("version", sa.Integer(), primary_key=True),
            *columns,
            *_availability(table),
            sa.CheckConstraint("version >= 1", name=f"ck_{table}_version"),
            schema="tennis",
        )
    op.create_table(
        "match_stats_version",
        sa.Column(
            "match_id", UUID, sa.ForeignKey("tennis.canonical_match.match_id"), primary_key=True
        ),
        sa.Column("player_id", UUID, sa.ForeignKey("tennis.player.player_id"), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("counts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        *_availability("match_stats_version"),
        schema="tennis",
    )
    op.create_table(
        "ranking_snapshot",
        sa.Column("snapshot_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("player_id", UUID, sa.ForeignKey("tennis.player.player_id"), nullable=False),
        sa.Column("tour", sa.Text(), nullable=False),
        sa.Column("ranking_date", sa.Date(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("points", sa.Integer()),
        *_availability("ranking_snapshot"),
        sa.CheckConstraint("rank >= 1", name="ck_ranking_rank"),
        sa.CheckConstraint("points IS NULL OR points >= 0", name="ck_ranking_points"),
        sa.UniqueConstraint(
            "player_id", "source_id", "ranking_date", "rank", "points", name="uq_ranking_value"
        ),
        schema="tennis",
    )
    op.create_table(
        "identity_review_revision",
        sa.Column("review_id", UUID, primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("recorded_at", TS, nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("resolution_note", sa.Text()),
        sa.CheckConstraint(
            "state IN ('OPEN','APPROVED','REJECTED')", name="ck_identity_review_state"
        ),
        sa.CheckConstraint(
            "state = 'OPEN' OR (actor NOT LIKE 'system:%' AND actor NOT LIKE 'agent:%')",
            name="ck_identity_review_human",
        ),
        schema="tennis",
    )
    op.create_table(
        "identity_audit",
        sa.Column("audit_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("recorded_at", TS, nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        schema="tennis",
    )
    op.create_table(
        "backfill_checkpoint",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("position", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_backfill_position"),
        schema="tennis",
    )
    op.execute(
        "CREATE FUNCTION tennis.reject_checkpoint_rewind() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN IF NEW.position < OLD.position THEN "
        "RAISE EXCEPTION 'checkpoint cannot move backwards'; END IF; RETURN NEW; END; $$"
    )
    op.execute(
        "CREATE TRIGGER backfill_checkpoint_monotonic BEFORE UPDATE ON "
        "tennis.backfill_checkpoint FOR EACH ROW EXECUTE FUNCTION "
        "tennis.reject_checkpoint_rewind()"
    )
    for table in APPEND_ONLY_TABLES:
        _append_only_trigger(table)
    op.execute(
        "INSERT INTO tennis.platform_component(component, version) VALUES ('identity', 'F04-v1')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM tennis.platform_component WHERE component = 'identity'")
    for table in reversed(APPEND_ONLY_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON tennis.{table}")
    op.execute("DROP TRIGGER IF EXISTS backfill_checkpoint_monotonic ON tennis.backfill_checkpoint")
    op.execute("DROP FUNCTION IF EXISTS tennis.reject_checkpoint_rewind()")
    op.drop_table("backfill_checkpoint", schema="tennis")
    op.drop_table("identity_audit", schema="tennis")
    op.drop_table("identity_review_revision", schema="tennis")
    op.drop_table("ranking_snapshot", schema="tennis")
    op.drop_table("match_stats_version", schema="tennis")
    op.drop_table("match_result_version", schema="tennis")
    op.drop_table("match_status_version", schema="tennis")
    op.drop_table("match_schedule_version", schema="tennis")
    op.drop_table("match_alias", schema="tennis")
    op.drop_table("canonical_match", schema="tennis")
    op.drop_table("tournament_alias", schema="tennis")
    op.drop_table("tournament_edition", schema="tennis")
    op.drop_table("tournament", schema="tennis")
    op.drop_index("ix_player_alias_player", table_name="player_alias", schema="tennis")
    op.drop_table("player_alias", schema="tennis")
    op.drop_table("player", schema="tennis")
