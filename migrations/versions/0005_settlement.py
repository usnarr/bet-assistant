"""Create F06 append-only virtual ledger, positions and settlement records."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005_settlement"
down_revision = "0004_identity"
branch_labels = None
depends_on = None

APPEND_ONLY_TABLES = (
    "settlement_ledger",
    "settlement_virtual_bet",
    "settlement_record",
    "settlement_ledger_entry",
)
MONEY = sa.Numeric(14, 2)


def upgrade() -> None:
    op.create_table(
        "settlement_ledger",
        sa.Column("ledger_id", sa.Text(), primary_key=True),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("opening_balance", MONEY, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("scope IN ('VIRTUAL','ACTUAL')", name="ck_settlement_ledger_scope"),
        sa.CheckConstraint("opening_balance >= 0", name="ck_settlement_ledger_opening"),
        schema="tennis",
    )
    op.create_table(
        "settlement_virtual_bet",
        sa.Column("bet_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "ledger_id",
            sa.Text(),
            sa.ForeignKey("tennis.settlement_ledger.ledger_id"),
            nullable=False,
        ),
        sa.Column("decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stake", MONEY, nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("stake > 0", name="ck_settlement_virtual_bet_stake"),
        schema="tennis",
    )
    op.create_index(
        "ix_settlement_virtual_bet_ledger",
        "settlement_virtual_bet",
        ["ledger_id"],
        schema="tennis",
    )
    op.create_table(
        "settlement_record",
        sa.Column("record_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "bet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tennis.settlement_virtual_bet.bet_id"),
            nullable=False,
        ),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("bet_id", "digest", name="uq_settlement_record_effect"),
        sa.CheckConstraint(
            "status IN ('WON','LOST','VOID','PENDING')", name="ck_settlement_record_status"
        ),
        schema="tennis",
    )
    op.create_table(
        "settlement_ledger_entry",
        sa.Column("entry_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "ledger_id",
            sa.Text(),
            sa.ForeignKey("tennis.settlement_ledger.ledger_id"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("entry_type", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("balance_after", MONEY, nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column(
            "bet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tennis.settlement_virtual_bet.bet_id"),
        ),
        sa.Column("settlement_digest", sa.String(64)),
        sa.Column("settlement_status", sa.Text()),
        sa.Column(
            "reverses_entry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tennis.settlement_ledger_entry.entry_id"),
            unique=True,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("ledger_id", "sequence", name="uq_settlement_entry_sequence"),
        sa.UniqueConstraint("ledger_id", "idempotency_key", name="uq_settlement_entry_key"),
        sa.CheckConstraint("sequence >= 1", name="ck_settlement_entry_sequence"),
        sa.CheckConstraint(
            "entry_type IN ('OPENING_BALANCE','STAKE_DEBIT','SETTLEMENT_CREDIT','REVERSAL')",
            name="ck_settlement_entry_type",
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
        "INSERT INTO tennis.platform_component(component, version) VALUES ('settlement', 'F06-v1')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM tennis.platform_component WHERE component = 'settlement'")
    for table in reversed(APPEND_ONLY_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON tennis.{table}")
    op.drop_table("settlement_ledger_entry", schema="tennis")
    op.drop_table("settlement_record", schema="tennis")
    op.drop_index(
        "ix_settlement_virtual_bet_ledger", table_name="settlement_virtual_bet", schema="tennis"
    )
    op.drop_table("settlement_virtual_bet", schema="tennis")
    op.drop_table("settlement_ledger", schema="tennis")
