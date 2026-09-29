"""Create F05 append-only bookmaker polls, quote observations and event mappings."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0007_bookmaker_quotes"
down_revision = "0006_features"
branch_labels = None
depends_on = None

APPEND_ONLY_TABLES = ("bookmaker_poll", "bookmaker_quote_observation", "bookmaker_event_mapping")


def upgrade() -> None:
    op.create_table(
        "bookmaker_poll",
        sa.Column("poll_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("bookmaker", sa.Text(), nullable=False),
        sa.Column("parser_version", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_content_sha256", sa.String(64), nullable=False),
        sa.Column("source_event_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "raw_content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_bookmaker_poll_sha256"
        ),
        schema="tennis",
    )
    op.create_index(
        "ix_bookmaker_poll_time", "bookmaker_poll", ["bookmaker", "observed_at"], schema="tennis"
    )
    op.create_table(
        "bookmaker_quote_observation",
        sa.Column("observation_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("bookmaker", sa.Text(), nullable=False),
        sa.Column("source_event_id", sa.Text(), nullable=False),
        sa.Column("source_market_id", sa.Text(), nullable=False),
        sa.Column("source_selection_id", sa.Text(), nullable=False),
        sa.Column("market", sa.Text()),
        sa.Column("state", sa.Text(), nullable=False),
        # Exact source decimal text is kept in the payload; numeric supports queries.
        sa.Column("decimal_odds", sa.Numeric(12, 4), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_content_sha256", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint("decimal_odds > 1", name="ck_bookmaker_quote_odds"),
        sa.CheckConstraint(
            "state IN ('OPEN','SUSPENDED','CLOSED')", name="ck_bookmaker_quote_state"
        ),
        schema="tennis",
    )
    op.create_index(
        "ix_bookmaker_quote_key_time",
        "bookmaker_quote_observation",
        ["bookmaker", "source_event_id", "source_market_id", "source_selection_id", "observed_at"],
        schema="tennis",
    )
    op.create_table(
        "bookmaker_event_mapping",
        sa.Column("mapping_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("bookmaker", sa.Text(), nullable=False),
        sa.Column("source_event_id", sa.Text(), nullable=False),
        sa.Column("match_id", postgresql.UUID(as_uuid=True)),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        schema="tennis",
    )
    op.create_index(
        "ix_bookmaker_event_mapping_event",
        "bookmaker_event_mapping",
        ["bookmaker", "source_event_id", "resolved_at"],
        schema="tennis",
    )
    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only "
            f"BEFORE UPDATE OR DELETE ON tennis.{table} "
            "FOR EACH ROW EXECUTE FUNCTION tennis.reject_history_mutation()"
        )
    op.execute(
        "INSERT INTO tennis.platform_component(component, version) "
        "VALUES ('bookmaker_quotes', 'F05-v1')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM tennis.platform_component WHERE component = 'bookmaker_quotes'")
    for table in reversed(APPEND_ONLY_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON tennis.{table}")
    op.drop_index(
        "ix_bookmaker_event_mapping_event", table_name="bookmaker_event_mapping", schema="tennis"
    )
    op.drop_table("bookmaker_event_mapping", schema="tennis")
    op.drop_index(
        "ix_bookmaker_quote_key_time", table_name="bookmaker_quote_observation", schema="tennis"
    )
    op.drop_table("bookmaker_quote_observation", schema="tennis")
    op.drop_index("ix_bookmaker_poll_time", table_name="bookmaker_poll", schema="tennis")
    op.drop_table("bookmaker_poll", schema="tennis")
