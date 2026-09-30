"""A representative synthetic bundle for OPS-02: raw object, decisions, ledger and jobs."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from serving_support import bet, copy, no_bet, stored, watch

from tennis_engine.common.clock import FrozenClock
from tennis_engine.common.contracts import Money
from tennis_engine.infrastructure.object_store import ImmutableObjectStore
from tennis_engine.ingestion.contracts import FetchCapture, FetchDisposition, FetchOrigin
from tennis_engine.ingestion.postgres import PostgresIngestionStore
from tennis_engine.ingestion.service import IngestionService
from tennis_engine.operations.jobs import CapacityLimits, CapacityPools, JobName, JobRunner
from tennis_engine.operations.leases import PostgresLeaseStore
from tennis_engine.operations.postgres import PostgresJobStore
from tennis_engine.serving.postgres import PostgresDecisionStore
from tennis_engine.settlement.ledger import VirtualBet, VirtualLedgerService
from tennis_engine.settlement.postgres import PostgresLedgerStore

INSTANT = datetime(2026, 9, 20, 12, tzinfo=UTC)
PAYLOAD = Path(__file__).parent / "fixtures" / "sources" / "synthetic-sports-v1" / "payload.json"


def _pln(value: str) -> Money:
    return Money(amount=Decimal(value))


def seed(engine, objects: ImmutableObjectStore) -> dict[str, int]:
    """Write synthetic rows through the real services. Nothing here is real data."""
    clock = FrozenClock(INSTANT)
    ingestion = IngestionService(PostgresIngestionStore(engine), objects, clock)
    ingestion.archive(
        idempotency_key="ops02-raw-1",
        observation_window=INSTANT,
        capture=FetchCapture(
            source_id="synthetic-sports",
            logical_resource_id="synthetic-event-1",
            request_identity="FILE payload.json",
            requested_at=INSTANT,
            completed_at=INSTANT + timedelta(seconds=1),
            origin=FetchOrigin.FILE_IMPORT,
            disposition=FetchDisposition.SUCCESS,
            attempt_number=1,
            content_type="application/json",
            body=PAYLOAD.read_bytes(),
        ),
        parser_candidate="synthetic-sports-v1",
        policy_version="fixture-v1",
        policy_revision=1,
    )
    decisions = PostgresDecisionStore(engine)
    items = [stored(bet()), stored(watch()), stored(no_bet())]
    items += [stored(copy(no_bet(), f"ops02-{index}")) for index in range(5)]
    for item in items:
        decisions.add(item)
    ledger = VirtualLedgerService(PostgresLedgerStore(engine), clock)
    ledger.open_ledger("ops02-shadow", _pln("100.00"))
    for index in range(1, 3):
        ledger.record_virtual_bet(
            VirtualBet(
                bet_id=UUID(int=500 + index),
                ledger_id="ops02-shadow",
                decision_id=UUID(int=600 + index),
                bookmaker="synthetic-book",
                match_id=UUID(int=99),
                selection_player_id=UUID(int=100),
                decimal_odds=Decimal("2.00"),
                stake=_pln("10.00"),
                cash_return_if_win=_pln("20.00"),
                payout_policy_version="synthetic-policy-v1",
                settlement_rule_version="synthetic-book-settlement-v1",
                struck_at=INSTANT,
            )
        )
    leases = PostgresLeaseStore(engine)
    runner = JobRunner(
        PostgresJobStore(engine, leases), leases, CapacityPools(CapacityLimits()), clock, "ops02"
    )
    runner.run(
        JobName.SYNC_SOURCE_REGISTRY,
        resource="job:sync_source_registry:all",
        cutoff=INSTANT,
        input_versions={"register": "synthetic-v1"},
        dependencies={},
        work=lambda lease: {"register": "synthetic-v1"},
    )
    return {"decisions": len(items), "ledger_bets": 2, "raw_contents": 1, "job_runs": 1}
