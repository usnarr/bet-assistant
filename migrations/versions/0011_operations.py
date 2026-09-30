"""Create F15 job runs, append-only job attempts and fenced resource leases."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0011_operations"
down_revision = "0010_decision_records"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    # A lease is current state, not history: it is the one mutable table here. The fencing
    # token only grows, so a stale holder can always be detected.
    op.create_table(
        "resource_lease",
        sa.Column("resource", sa.Text(), primary_key=True),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("acquired_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.CheckConstraint("fencing_token >= 1", name="ck_lease_token"),
        sa.CheckConstraint("expires_at > acquired_at", name="ck_lease_expiry"),
        schema="tennis",
    )
    op.execute(
        "CREATE FUNCTION tennis.reject_lease_token_decrease() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        "IF NEW.fencing_token < OLD.fencing_token THEN "
        "RAISE EXCEPTION 'fencing token cannot decrease'; END IF; "
        "RETURN NEW; END; $$"
    )
    op.execute(
        "CREATE TRIGGER resource_lease_token_monotonic BEFORE UPDATE ON tennis.resource_lease "
        "FOR EACH ROW EXECUTE FUNCTION tennis.reject_lease_token_decrease()"
    )
    op.create_table(
        "job_run",
        sa.Column("job_run_id", UUID, primary_key=True),
        sa.Column("idempotency_key", sa.String(64), nullable=False, unique=True),
        sa.Column("job", sa.Text(), nullable=False),
        sa.Column("capacity", sa.Text(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("cutoff", TS, nullable=False),
        sa.Column("input_versions", JSONB, nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.CheckConstraint("capacity IN ('PROSPECTIVE','BACKFILL')", name="ck_job_capacity"),
        sa.CheckConstraint("max_attempts BETWEEN 1 AND 20", name="ck_job_attempts"),
        sa.CheckConstraint("idempotency_key ~ '^[0-9a-f]{64}$'", name="ck_job_key"),
        schema="tennis",
    )
    op.create_table(
        "job_attempt",
        sa.Column("job_run_id", UUID, sa.ForeignKey("tennis.job_run.job_run_id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("fencing_token", sa.BigInteger(), nullable=True),
        sa.Column("dependency_status", JSONB, nullable=False),
        sa.Column("output_versions", JSONB, nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("recorded_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("job_run_id", "sequence", name="pk_job_attempt"),
        sa.CheckConstraint(
            "status IN ('BLOCKED','RUNNING','SUCCEEDED','FAILED')", name="ck_attempt_status"
        ),
        sa.CheckConstraint(
            "status NOT IN ('RUNNING','SUCCEEDED') OR fencing_token IS NOT NULL",
            name="ck_attempt_fenced",
        ),
        schema="tennis",
    )
    # One success per run: a duplicate delivery cannot record a second effect.
    op.create_index(
        "ux_job_attempt_one_success",
        "job_attempt",
        ["job_run_id"],
        unique=True,
        schema="tennis",
        postgresql_where=sa.text("status = 'SUCCEEDED'"),
    )
    for table in ("job_run", "job_attempt"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON tennis.{table} "
            "FOR EACH ROW EXECUTE FUNCTION tennis.reject_history_mutation()"
        )
    op.execute(
        "INSERT INTO tennis.platform_component(component, version) VALUES ('operations', 'F15-v1')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM tennis.platform_component WHERE component = 'operations'")
    for table in ("job_attempt", "job_run"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON tennis.{table}")
    op.drop_index("ux_job_attempt_one_success", table_name="job_attempt", schema="tennis")
    op.drop_table("job_attempt", schema="tennis")
    op.drop_table("job_run", schema="tennis")
    op.execute("DROP TRIGGER IF EXISTS resource_lease_token_monotonic ON tennis.resource_lease")
    op.execute("DROP FUNCTION tennis.reject_lease_token_decrease()")
    op.drop_table("resource_lease", schema="tennis")
