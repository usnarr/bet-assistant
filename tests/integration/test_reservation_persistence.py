import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tennis_engine.common.clock import FrozenClock
from tennis_engine.common.contracts import Money
from tennis_engine.pricing.postgres import PostgresReservationStore
from tennis_engine.pricing.reservation import (
    ExposureService,
    ReservationRejected,
    ReservationRequest,
)
from tennis_engine.settlement.ledger import VirtualLedgerService
from tennis_engine.settlement.postgres import PostgresLedgerStore

INSTANT = datetime(2026, 9, 20, 12, tzinfo=UTC)


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="isolated PostgreSQL not configured"
)
def test_postgres_reservations_serialize_and_stay_append_only(monkeypatch):
    database_url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setenv("TENNIS_DATABASE_URL", database_url)
    config = Config("alembic.ini")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    clock = FrozenClock(INSTANT)
    ledger = VirtualLedgerService(PostgresLedgerStore(engine), clock)
    service = ExposureService(PostgresReservationStore(engine), ledger, clock)
    try:
        ledger.open_ledger("shadow-integration", Money(amount=Decimal("100.00")))
        rejected = []

        def attempt(index: int) -> None:
            try:
                service.reserve(
                    ReservationRequest(
                        decision_key=f"decision-{index}",
                        ledger_id="shadow-integration",
                        match_id=UUID(int=1),
                        bookmaker="synthetic-book",
                        selection_player_id=UUID(int=2),
                        stake=Money(amount=Decimal("10.00")),
                        ttl_seconds=60,
                    ),
                    lambda state: Decimal("30.00") - state.event_exposure,
                )
            except ReservationRejected as error:
                rejected.append(error)

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(attempt, range(8)))
        state = service.exposure("shadow-integration", match_id=UUID(int=1), bookmaker="x")
        assert state.event_exposure == Decimal("30.00") and len(rejected) == 5
        with pytest.raises(DBAPIError, match="append-only"), engine.begin() as opened:
            opened.execute(text("DELETE FROM tennis.risk_reservation_event"))
    finally:
        engine.dispose()
        command.downgrade(config, "base")
