"""F14 PostgreSQL decision store: filters, order, supersession, idempotency, append-only."""

import os
from datetime import timedelta
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from serving_support import (
    READ_AT,
    FakeChecks,
    authenticator,
    bet,
    copy,
    headers,
    no_bet,
    stored,
    watch,
)
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from test_foundation_api import Probe
from test_pricing_decision import AT

from tennis_engine.common.clock import FrozenClock
from tennis_engine.contracts.domain import RecommendationStatus
from tennis_engine.governance.contracts import Decision
from tennis_engine.infrastructure.settings import Settings
from tennis_engine.pricing.decision import prepare_publication
from tennis_engine.serving.api import Serving, create_app
from tennis_engine.serving.checks import StaticRedistribution
from tennis_engine.serving.contracts import StoredDecision
from tennis_engine.serving.postgres import PostgresDecisionStore
from tennis_engine.serving.service import RecommendationService
from tennis_engine.serving.store import DecisionConflict, DecisionQuery, InMemoryDecisionStore


def superseding(record):
    return prepare_publication(
        record,
        now=READ_AT,
        publication=Decision.deny("SOURCE_DISABLED"),
        actionability=None,
        responsible_use=FakeChecks().responsible,
        policy=None,
        rules=None,
        reserve=lambda stake, allowed: UUID(int=1),
    ).record


def fixtures():
    original = bet()
    later = copy(no_bet(), "later")
    items = [stored(original), stored(superseding(original)), stored(watch())]
    items += [stored(copy(no_bet(), f"page-{index}")) for index in range(4)]
    later_context = stored(later).context
    later_match = later_context.match.model_copy(update={"scheduled_start": AT + timedelta(days=2)})
    items.append(
        StoredDecision(
            record=later, context=later_context.model_copy(update={"match": later_match})
        )
    )
    return items


QUERIES = [
    DecisionQuery(limit=50),
    DecisionQuery(limit=50, latest_only=True),
    DecisionQuery(limit=50, statuses=frozenset({RecommendationStatus.WATCH})),
    DecisionQuery(limit=50, bookmaker="synthetic-book", starts_after=AT + timedelta(days=1)),
    DecisionQuery(limit=50, starts_before=AT + timedelta(days=1), active_at=READ_AT),
    DecisionQuery(limit=3),
]


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="isolated PostgreSQL not configured"
)
def test_postgres_decision_store_matches_the_memory_store(monkeypatch):
    database_url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setenv("TENNIS_DATABASE_URL", database_url)
    config = Config("alembic.ini")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    try:
        postgres, memory = PostgresDecisionStore(engine), InMemoryDecisionStore()
        items = fixtures()
        for item in items:
            postgres.add(item)
            memory.add(item)
        postgres.add(items[0])  # an identical retry is a no-op
        with pytest.raises(DecisionConflict):
            postgres.add(stored(bet(), account_scope="other"))

        assert postgres.get(items[0].record.decision_id) == items[0]
        assert postgres.get(UUID(int=9)) is None
        for query in QUERIES:
            assert [i.record.decision_id for i in postgres.query(query)] == [
                i.record.decision_id for i in memory.query(query)
            ], query
        first = memory.query(DecisionQuery(limit=2))[-1]
        after = DecisionQuery(limit=50, after=(first.scheduled_start, first.record.decision_id))
        assert [i.record.decision_id for i in postgres.query(after)] == [
            i.record.decision_id for i in memory.query(after)
        ]
        original = items[0].record.decision_id
        assert list(postgres.successors(original)) == list(memory.successors(original))
        assert postgres.latest_observations() == memory.latest_observations()

        service = RecommendationService(
            store=postgres,
            checks=FakeChecks(),
            clock=FrozenClock(READ_AT),
            redistribution=StaticRedistribution(()),
            bookmakers=frozenset({"synthetic-book"}),
        )
        client = TestClient(
            create_app(Settings(environment="test"), Probe(True), Serving(service, authenticator()))
        )
        body = client.get("/v1/tennis/recommendations", headers=headers()).json()
        assert str(original) not in {r["recommendation_id"] for r in body["recommendations"]}
        audit = client.get(f"/v1/audit/recommendations/{original}", headers=headers()).json()
        assert audit["record"] == items[0].record.model_dump(mode="json")

        for statement in (
            "UPDATE tennis.decision_record SET status = 'BET'",
            "DELETE FROM tennis.decision_record",
        ):
            with pytest.raises(DBAPIError, match="append-only"), engine.begin() as db:
                db.execute(text(statement))
        with pytest.raises(DBAPIError, match="ck_decision_stake_zero"), engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO tennis.decision_record SELECT gen_random_uuid(), decision_key, "
                    "ledger_id, version, supersedes, 'WATCH', bookmaker, market, match_id, "
                    "scheduled_start, decided_at, expires_at, quote_source_id, "
                    "quote_observed_at, 5.00, record, context, now() "
                    "FROM tennis.decision_record WHERE status = 'BET' LIMIT 1"
                )
            )
    finally:
        engine.dispose()
        command.downgrade(config, "base")
