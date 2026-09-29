import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tennis_engine.common.clock import FrozenClock
from tennis_engine.common.contracts import Money
from tennis_engine.settlement.engine import SettlementResult, SettlementStatus
from tennis_engine.settlement.ledger import InsufficientFunds, VirtualBet, VirtualLedgerService
from tennis_engine.settlement.postgres import PostgresLedgerStore

INSTANT = datetime(2026, 9, 20, 12, tzinfo=UTC)


def pln(value: str) -> Money:
    return Money(amount=Decimal(value))


def bet(index: int, stake: str = "30.00") -> VirtualBet:
    return VirtualBet(
        bet_id=UUID(int=index),
        ledger_id="shadow-integration",
        decision_id=UUID(int=1000 + index),
        bookmaker="synthetic-book",
        match_id=UUID(int=99),
        selection_player_id=UUID(int=100),
        decimal_odds=Decimal("2.00"),
        stake=pln(stake),
        cash_return_if_win=pln(str(Decimal(stake) * 2)),
        payout_policy_version="synthetic-policy-v1",
        settlement_rule_version="synthetic-book-settlement-v1",
        struck_at=INSTANT,
    )


def won(index: int, cash: str) -> SettlementResult:
    return SettlementResult(
        bet_id=UUID(int=index),
        status=SettlementStatus.WON,
        rule_version="synthetic-book-settlement-v1",
        stake_deducted=pln("30.00"),
        cash_return=pln(cash),
        net_profit=pln(str(Decimal(cash) - Decimal("30.00"))),
        tax_amount=None,
        applied_rules=("fixture",),
        evidence=("raw:result",),
        outcome_observed_at=INSTANT + timedelta(hours=3),
        settled_at=INSTANT + timedelta(hours=4),
    )


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="isolated PostgreSQL not configured"
)
def test_postgres_ledger_is_append_only_idempotent_and_serialized(monkeypatch):
    database_url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setenv("TENNIS_DATABASE_URL", database_url)
    config = Config("alembic.ini")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    service = VirtualLedgerService(PostgresLedgerStore(engine), FrozenClock(INSTANT))
    try:
        service.open_ledger("shadow-integration", pln("100.00"))
        failures = []

        def attempt(index: int) -> None:
            try:
                service.record_virtual_bet(bet(index))
            except InsufficientFunds as error:
                failures.append(error)

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(attempt, range(1, 7)))
        assert len(failures) == 3
        with engine.connect() as opened:
            first = (
                opened.execute(
                    text("SELECT bet_id FROM tennis.settlement_virtual_bet ORDER BY bet_id LIMIT 1")
                )
                .scalar_one()
                .int
            )
        effect = service.apply_settlement("shadow-integration", won(first, "60.00"))
        assert service.apply_settlement("shadow-integration", won(first, "60.00")) == effect
        service.apply_settlement(
            "shadow-integration", won(first, "55.00"), correction_reason="Payout correction"
        )
        report = service.reconcile("shadow-integration")
        assert report.balanced and report.closing_balance == pln("65.00")
        assert report.open_exposure == pln("60.00")
        with pytest.raises(DBAPIError, match="append-only"), engine.begin() as opened:
            opened.execute(text("UPDATE tennis.settlement_ledger_entry SET reason = 'edited'"))
        with pytest.raises(DBAPIError, match="append-only"), engine.begin() as opened:
            opened.execute(text("DELETE FROM tennis.settlement_record"))
    finally:
        engine.dispose()
        command.downgrade(config, "base")
