"""Create F18/F15.6 append-only agent traces and review-queue proposals."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0012_agent_records"
down_revision = "0011_operations"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
ROLES = (
    "'data_intake_analyst','identity_review_assistant','research_feature_assistant',"
    "'model_evaluation_analyst','value_risk_reviewer','explanation_assistant',"
    "'monitoring_incident_assistant'"
)
STATUSES = (
    "'COMPLETED','ABSTAINED','REVIEW_REQUIRED','REJECTED','TIMEOUT','BUDGET_EXHAUSTED',"
    "'EXPIRED','DISABLED','MODEL_UNAVAILABLE'"
)


def upgrade() -> None:
    op.create_table(
        "agent_trace",
        sa.Column("trace_id", UUID, primary_key=True),
        sa.Column("run_id", UUID, nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("role_version", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("finished_at", TS, nullable=False),
        sa.Column("critical_attempts", sa.Integer(), nullable=False),
        sa.Column("trace", JSONB, nullable=False),
        sa.Column("trace_sha256", sa.String(64), nullable=False),
        sa.CheckConstraint(f"role IN ({ROLES})", name="ck_agent_trace_role"),
        sa.CheckConstraint(f"status IN ({STATUSES})", name="ck_agent_trace_status"),
        sa.CheckConstraint("finished_at >= started_at", name="ck_agent_trace_time"),
        sa.CheckConstraint("critical_attempts >= 0", name="ck_agent_trace_critical"),
        sa.CheckConstraint("trace_sha256 ~ '^[0-9a-f]{64}$'", name="ck_agent_trace_sha"),
        schema="tennis",
    )
    op.create_index("ix_agent_trace_role_time", "agent_trace", ["role", "started_at"], schema="tennis")
    # A proposal is only a review-queue item. There is no column to apply or approve it.
    op.create_table(
        "agent_proposal",
        sa.Column("proposal_id", UUID, primary_key=True),
        sa.Column("idempotency_key", sa.String(64), nullable=False, unique=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("fields", JSONB, nullable=False),
        sa.Column("evidence_ids", JSONB, nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("trace_id", UUID, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.CheckConstraint(f"role IN ({ROLES})", name="ck_agent_proposal_role"),
        sa.CheckConstraint("state = 'PROPOSED'", name="ck_agent_proposal_state"),
        sa.CheckConstraint("idempotency_key ~ '^[0-9a-f]{64}$'", name="ck_agent_proposal_key"),
        sa.CheckConstraint(
            "jsonb_array_length(evidence_ids) >= 1", name="ck_agent_proposal_evidence"
        ),
        schema="tennis",
    )
    op.create_index("ix_agent_proposal_subject", "agent_proposal", ["subject_id"], schema="tennis")
    for table in ("agent_trace", "agent_proposal"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON tennis.{table} "
            "FOR EACH ROW EXECUTE FUNCTION tennis.reject_history_mutation()"
        )
    op.execute(
        "INSERT INTO tennis.platform_component(component, version) VALUES ('agents', 'F18-v1')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM tennis.platform_component WHERE component = 'agents'")
    for table in ("agent_proposal", "agent_trace"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON tennis.{table}")
    op.drop_index("ix_agent_proposal_subject", table_name="agent_proposal", schema="tennis")
    op.drop_table("agent_proposal", schema="tennis")
    op.drop_index("ix_agent_trace_role_time", table_name="agent_trace", schema="tennis")
    op.drop_table("agent_trace", schema="tennis")
