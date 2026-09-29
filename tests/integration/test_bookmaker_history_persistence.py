import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tennis_engine.ingestion.bookmakers.contracts import Snapshot
from tennis_engine.ingestion.bookmakers.history import QuoteHistory
from tennis_engine.ingestion.bookmakers.postgres import PostgresHistoryStore
from tennis_engine.ingestion.bookmakers.quotes import ActionabilityPolicy
from tennis_engine.ingestion.bookmakers.registry import BETCLIC

FIXTURES = Path(__file__).parents[1] / "fixtures" / "bookmakers" / "betclic"
POLL_1 = datetime(2026, 9, 21, 8, tzinfo=UTC)


def snapshot(poll, observed_at):
    body = (FIXTURES / f"payload-{poll}.json").read_bytes()
    return Snapshot.observe(
        BETCLIC.parser.parse_listing(body),
        observed_at=observed_at,
        raw_content_sha256=hashlib.sha256(body).hexdigest(),
    )


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="isolated PostgreSQL not configured"
)
def test_postgres_quote_history_is_append_only_and_idempotent(monkeypatch):
    database_url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setenv("TENNIS_DATABASE_URL", database_url)
    config = Config("alembic.ini")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    history = QuoteHistory(PostgresHistoryStore(engine))
    try:
        first = snapshot(1, POLL_1)
        assert history.record(first) == len(first.quotes)
        assert history.record(first) == 0
        history.record(snapshot(2, POLL_1 + timedelta(seconds=30)))
        key = ("betclic", "ev-01", "ev-01-mw", "ev-01-s1")
        result = history.actionability(
            key, at=POLL_1 + timedelta(seconds=35), policy=ActionabilityPolicy()
        )
        assert result.actionable
        assert str(result.observation.quote.decimal_odds) == "1.62"
        with pytest.raises(DBAPIError, match="append-only"), engine.begin() as opened:
            opened.execute(text("UPDATE tennis.bookmaker_quote_observation SET state = 'CLOSED'"))
    finally:
        engine.dispose()
        command.downgrade(config, "base")
