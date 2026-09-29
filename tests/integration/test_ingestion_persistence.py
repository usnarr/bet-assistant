import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from tennis_engine.common.clock import FrozenClock
from tennis_engine.infrastructure.object_store import LocalObjectStore
from tennis_engine.ingestion.contracts import FetchCapture, FetchDisposition, FetchOrigin
from tennis_engine.ingestion.postgres import PostgresIngestionStore
from tennis_engine.ingestion.service import IngestionService
from tennis_engine.ingestion.synthetic import SyntheticSportsParser


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="isolated PostgreSQL not configured"
)
def test_postgres_ingestion_lineage_and_append_only_outputs(tmp_path, monkeypatch):
    database_url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setenv("TENNIS_DATABASE_URL", database_url)
    config = Config("alembic.ini")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    instant = datetime(2026, 9, 19, 10, tzinfo=UTC)
    body = (
        Path(__file__).parents[1] / "fixtures" / "sources" / "synthetic-sports-v1" / "payload.json"
    ).read_bytes()
    service = IngestionService(
        PostgresIngestionStore(engine),
        LocalObjectStore(tmp_path / "objects"),
        FrozenClock(instant),
    )
    try:
        results = []
        for index in (1, 2):
            result = service.archive(
                idempotency_key=f"integration-{index}",
                observation_window=instant,
                capture=FetchCapture(
                    source_id="synthetic-sports",
                    logical_resource_id="synthetic-event-1",
                    request_identity="FILE payload.json",
                    requested_at=instant,
                    completed_at=instant + timedelta(seconds=index),
                    origin=FetchOrigin.FILE_IMPORT,
                    disposition=FetchDisposition.SUCCESS,
                    attempt_number=1,
                    content_type="application/json",
                    body=body,
                ),
                parser_candidate=SyntheticSportsParser.version,
                policy_version="fixture-v1",
                policy_revision=1,
            )
            assert result.observation_id is not None
            results.append(result)
        assert results[0].raw_content_id == results[1].raw_content_id
        parsed = service.parse_observation(
            service.repository.observation(results[0].observation_id), SyntheticSportsParser()
        )
        assert len(parsed.derived_record_ids) == 1
        with engine.connect() as opened:
            assert opened.execute(text("SELECT count(*) FROM tennis.raw_content")).scalar_one() == 1
            assert (
                opened.execute(text("SELECT count(*) FROM tennis.source_observation")).scalar_one()
                == 2
            )
        with pytest.raises(Exception, match="append-only history"):
            with engine.begin() as opened:
                opened.execute(text("DELETE FROM tennis.derived_record"))
    finally:
        engine.dispose()
        command.downgrade(config, "base")
        command.upgrade(config, "head")
