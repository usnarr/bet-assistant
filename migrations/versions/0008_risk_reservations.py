"""Create F12 append-only exposure reservations and reservation events."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008_risk_reservations"
down_revision = "0007_bookmaker_quotes"
branch_labels = None
depends_on = None

APPEND_ONLY_TABLES = ("risk_reservation", "risk_reservation_event")


def upgrade() -> None:
    op.create_table(
        "risk_reservation",
        sa.Column("reservation_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("decision_key", sa.Text(), nullable=False),
        sa.Column("ledger_id", sa.Text(), nullable=False),
        sa.Column("match_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bookmaker", sa.Text(), nullable=False),
        sa.Column("stake", sa.Numeric(14, 2), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.UniqueConstraint("ledger_id", "decision_key", name="uq_risk_reservation_key"),
        sa.CheckConstraint("stake > 0", name="ck_risk_reservation_stake"),
        sa.CheckConstraint("expires_at > reserved_at", name="ck_risk_reservation_expiry"),
        schema="tennis",
    )
    op.create_index(
        "ix_risk_reservation_ledger", "risk_reservation", ["ledger_id"], schema="tennis"
    )
    op.create_table(
        "risk_reservation_event",
        sa.Column("event_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "reservation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tennis.risk_reservation.reservation_id"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.UniqueConstraint("reservation_id", "kind", name="uq_risk_reservation_event_kind"),
        sa.CheckConstraint(
            "kind IN ('RESERVED','COMMITTED','RELEASED')", name="ck_risk_reservation_event_kind"
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
        "INSERT INTO tennis.platform_component(component, version) "
        "VALUES ('risk_reservations', 'F12-v1')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM tennis.platform_component WHERE component = 'risk_reservations'")
    for table in reversed(APPEND_ONLY_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON tennis.{table}")
    op.drop_table("risk_reservation_event", schema="tennis")
    op.drop_index("ix_risk_reservation_ledger", table_name="risk_reservation", schema="tennis")
    op.drop_table("risk_reservation", schema="tennis")
