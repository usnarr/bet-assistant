"""Create F03 immutable ingestion, replay, and recovery records."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_ingestion"
down_revision = "0002_governance"
branch_labels = None
depends_on = None

APPEND_ONLY_TABLES = (
    "ingestion_command",
    "raw_archive_transition",
    "fetch_attempt",
    "source_observation",
    "parsing_attempt",
    "derived_record",
    "ingestion_dead_letter_revision",
    "replay_job_revision",
    "orphan_object",
    "source_runtime_event",
)


def _append_only_trigger(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER {table}_append_only "
        f"BEFORE UPDATE OR DELETE ON tennis.{table} "
        "FOR EACH ROW EXECUTE FUNCTION tennis.reject_history_mutation()"
    )


def upgrade() -> None:
    op.create_table(
        "ingestion_command",
        sa.Column("command_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("logical_resource_id", sa.Text(), nullable=False),
        sa.Column("observation_window", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="tennis",
    )
    op.create_table(
        "raw_content",
        sa.Column("content_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False, unique=True),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("archive_state", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_id", "content_sha256", name="uq_raw_content_identity"),
        sa.CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_raw_content_sha256"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_raw_content_size"),
        sa.CheckConstraint(
            "archive_state IN ('PENDING','ARCHIVED','FAILED')", name="ck_raw_content_state"
        ),
        schema="tennis",
    )
    op.create_table(
        "raw_archive_transition",
        sa.Column("transition_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "content_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tennis.raw_content.content_id"),
            nullable=False,
        ),
        sa.Column("previous_state", sa.Text()),
        sa.Column("archive_state", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        schema="tennis",
    )
    op.create_table(
        "fetch_attempt",
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "command_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tennis.ingestion_command.command_id"),
            nullable=False,
        ),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("logical_resource_id", sa.Text(), nullable=False),
        sa.Column("request_identity", sa.Text(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("disposition", sa.Text(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status_code", sa.Integer()),
        sa.Column("content_type", sa.Text()),
        sa.Column(
            "raw_content_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tennis.raw_content.content_id"),
        ),
        sa.Column("etag", sa.Text()),
        sa.Column("last_modified", sa.Text()),
        sa.Column("cache_control", sa.Text()),
        sa.Column("provider_request_id", sa.Text()),
        sa.Column("retry_after_seconds", sa.Integer()),
        sa.Column("parser_candidate", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text()),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("policy_revision", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("command_id", "attempt_number", name="uq_fetch_command_attempt"),
        schema="tennis",
    )
    op.create_index(
        "ix_fetch_source_completed",
        "fetch_attempt",
        ["source_id", "completed_at"],
        schema="tennis",
    )
    op.create_table(
        "source_observation",
        sa.Column("observation_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "attempt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tennis.fetch_attempt.attempt_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "command_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tennis.ingestion_command.command_id"),
            nullable=False,
        ),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("logical_resource_id", sa.Text(), nullable=False),
        sa.Column(
            "raw_content_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tennis.raw_content.content_id"),
            nullable=False,
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True)),
        schema="tennis",
    )
    op.create_index(
        "ix_observation_replay",
        "source_observation",
        ["source_id", "logical_resource_id", "observed_at"],
        schema="tennis",
    )
    op.create_table(
        "parsing_attempt",
        sa.Column("parse_attempt_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "observation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tennis.source_observation.observation_id"),
            nullable=False,
        ),
        sa.Column("parser_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("derived_record_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("errors", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        schema="tennis",
    )
    op.create_table(
        "derived_record",
        sa.Column("record_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("record_type", sa.Text(), nullable=False),
        sa.Column("natural_key", sa.Text(), nullable=False),
        sa.Column("parser_version", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column(
            "raw_content_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tennis.raw_content.content_id"),
            nullable=False,
        ),
        sa.Column(
            "observation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tennis.source_observation.observation_id"),
            nullable=False,
        ),
        sa.Column(
            "parse_attempt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "tennis.parsing_attempt.parse_attempt_id",
                deferrable=True,
                initially="DEFERRED",
            ),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "source_id",
            "record_type",
            "natural_key",
            "parser_version",
            "payload_sha256",
            name="uq_derived_version",
        ),
        sa.CheckConstraint("payload_sha256 ~ '^[0-9a-f]{64}$'", name="ck_derived_sha256"),
        schema="tennis",
    )
    op.create_index(
        "ix_derived_natural_history",
        "derived_record",
        ["source_id", "record_type", "natural_key", "created_at"],
        schema="tennis",
    )
    op.create_table(
        "replay_job_revision",
        sa.Column("revision", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("replay_job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("selected_observations", sa.Integer()),
        sa.Column("accepted", sa.Integer()),
        sa.Column("rejected", sa.Integer()),
        sa.Column("derived_records", sa.Integer()),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('RUNNING','COMPLETED')", name="ck_replay_status"),
        schema="tennis",
    )
    op.create_index(
        "ix_replay_job_history",
        "replay_job_revision",
        ["replay_job_id", "recorded_at"],
        schema="tennis",
    )
    op.create_table(
        "ingestion_dead_letter_revision",
        sa.Column("revision", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("dead_letter_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column(
            "raw_content_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tennis.raw_content.content_id"),
        ),
        sa.Column("parser_version", sa.Text()),
        sa.Column("errors", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("retry_history", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("resolution_status", sa.Text(), nullable=False),
        sa.Column("replay_job_id", postgresql.UUID(as_uuid=True)),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        schema="tennis",
    )
    op.create_index(
        "ix_dead_letter_history",
        "ingestion_dead_letter_revision",
        ["dead_letter_id", "recorded_at"],
        schema="tennis",
    )
    op.create_table(
        "orphan_object",
        sa.Column("object_key", sa.Text(), primary_key=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        schema="tennis",
    )
    op.create_table(
        "source_runtime_event",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("until", sa.DateTime(timezone=True)),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        schema="tennis",
    )
    for table in APPEND_ONLY_TABLES:
        _append_only_trigger(table)
    op.execute(
        "INSERT INTO tennis.platform_component(component, version) VALUES ('ingestion', 'F03-v1')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM tennis.platform_component WHERE component = 'ingestion'")
    for table in reversed(APPEND_ONLY_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON tennis.{table}")
    op.drop_table("source_runtime_event", schema="tennis")
    op.drop_table("orphan_object", schema="tennis")
    op.drop_index(
        "ix_dead_letter_history", table_name="ingestion_dead_letter_revision", schema="tennis"
    )
    op.drop_table("ingestion_dead_letter_revision", schema="tennis")
    op.drop_index("ix_replay_job_history", table_name="replay_job_revision", schema="tennis")
    op.drop_table("replay_job_revision", schema="tennis")
    op.drop_index("ix_derived_natural_history", table_name="derived_record", schema="tennis")
    op.drop_table("derived_record", schema="tennis")
    op.drop_table("parsing_attempt", schema="tennis")
    op.drop_index("ix_observation_replay", table_name="source_observation", schema="tennis")
    op.drop_table("source_observation", schema="tennis")
    op.drop_index("ix_fetch_source_completed", table_name="fetch_attempt", schema="tennis")
    op.drop_table("fetch_attempt", schema="tennis")
    op.drop_table("raw_archive_transition", schema="tennis")
    op.drop_table("raw_content", schema="tennis")
    op.drop_table("ingestion_command", schema="tennis")
