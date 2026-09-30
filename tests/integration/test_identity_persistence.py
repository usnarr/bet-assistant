"""F04 PostgreSQL identity store: same results as the reference store, append-only history."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from identity_support import FIRST_OBSERVATION, check_operations_are_atomic, world
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tennis_engine.contracts.domain import Availability
from tennis_engine.models.point.formats import BEST_OF_3_FINAL_TB10, match_format
from tennis_engine.normalization.contracts import (
    BestOf,
    DecidingSetRule,
    DrawStage,
    PlayerAlias,
    ResolutionDecision,
    SourceFormatRecord,
)
from tennis_engine.normalization.postgres import PostgresIdentityStore
from tennis_engine.normalization.store import IdentityStore

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="isolated PostgreSQL not configured"
)
RULE_TIME = FIRST_OBSERVATION + timedelta(hours=2)


@pytest.fixture
def engine(monkeypatch):
    database_url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setenv("TENNIS_DATABASE_URL", database_url)
    config = Config("alembic.ini")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    try:
        yield engine
    finally:
        engine.dispose()
        command.downgrade(config, "base")
        command.upgrade(config, "head")


def _dump(store: IdentityStore) -> dict[str, list[str]]:
    def rows(items) -> list[str]:
        return sorted(
            json.dumps(
                item.model_dump(mode="json") if hasattr(item, "model_dump") else vars(item),
                sort_keys=True,
                default=str,
            )
            for item in items
        )

    matches = store.matches()
    players = store.players()
    return {
        "players": rows(players),
        "aliases": rows(
            alias for player in players for alias in store.aliases_for_player(player.player_id)
        ),
        "rankings": rows(item for player in players for item in store.rankings(player.player_id)),
        "matches": rows(matches),
        "schedules": rows(item for match in matches for item in store.schedules(match.match_id)),
        "statuses": rows(item for match in matches for item in store.statuses(match.match_id)),
        "results": rows(item for match in matches for item in store.results(match.match_id)),
        "stats": rows(
            item
            for match in matches
            for player_id in match.player_ids
            for item in store.stats(match.match_id, player_id)
        ),
        "formats": rows(
            item
            for match in matches
            for item in store.edition_formats(match.edition_id, match.draw_stage, match.best_of)
        ),
        "reviews": rows(store.reviews()),
        "audit": rows(store.audit_log()),
        "checkpoint": [str(store.checkpoint("sys-04"))],
    }


def _load(tmp_path, store=None):
    w = world(tmp_path, payloads=("payload-1.json", "payload-2.json"), store=store)
    w.run()
    w.run()
    w.warehouse.ingest_format(
        SourceFormatRecord(
            source_id="synthetic-sports",
            source_tournament_id="t-atp",
            season=2026,
            draw_stage=DrawStage.MAIN,
            best_of=BestOf.THREE,
            deciding_set=DecidingSetRule.TIEBREAK_10,
            reference="synthetic regulations section 3",
        ),
        Availability(observed_at=RULE_TIME, ingested_at=RULE_TIME),
    )
    return w


def test_postgres_store_matches_the_reference_store(engine, tmp_path):
    memory = _load(tmp_path / "memory")
    postgres = _load(tmp_path / "postgres", PostgresIdentityStore(engine))
    stored = _dump(postgres.store)
    assert stored == _dump(memory.store)
    for part in ("players", "aliases", "matches", "results", "stats", "formats", "reviews"):
        assert stored[part], part
    with engine.connect() as db:
        rows = db.execute(text("SELECT count(*) FROM tennis.canonical_match")).scalar_one()
    assert rows == len(stored["matches"])
    main = postgres.match_id("m-2")
    assert match_format(postgres.store, main, RULE_TIME) == BEST_OF_3_FINAL_TB10
    alias = postgres.store.player_alias("synthetic-sports", "p-kowalski-j")
    assert alias is not None
    assert postgres.store.candidate_player_ids(alias.source_name) >= {alias.player_id}


def test_history_is_append_only_and_versions_cannot_skip(engine, tmp_path):
    w = _load(tmp_path, PostgresIdentityStore(engine))
    for table in ("player_alias", "edition_format_version", "player_name_key", "match_alias"):
        with pytest.raises(DBAPIError, match="append-only history"):
            with engine.begin() as db:
                db.execute(text(f"DELETE FROM tennis.{table}"))
    alias = w.store.player_alias("synthetic-sports", "p-kowalski-j")
    assert alias is not None
    with pytest.raises(ValueError, match="Version must be"):
        w.store.append_player_alias(alias.model_copy(update={"version": alias.version + 2}))
    with pytest.raises(ValueError, match="cannot move backwards"):
        w.store.save_checkpoint("sys-04", 0)


def test_concurrent_alias_writers_create_one_version(engine, tmp_path):
    w = _load(tmp_path, PostgresIdentityStore(engine))
    current = w.store.player_alias("synthetic-sports", "p-kowalski-j")
    assert current is not None

    def attempt(index: int) -> bool:
        proposed = PlayerAlias(
            alias_id=UUID(int=900 + index),
            player_id=current.player_id,
            source_id=current.source_id,
            source_player_id=current.source_player_id,
            source_name=current.source_name,
            version=current.version + 1,
            active=True,
            decision=ResolutionDecision.REVIEW_REQUIRED,
            reviewed_by="reviewer-one",
            evidence=current.evidence,
            recorded_at=current.recorded_at + timedelta(minutes=1),
            supersedes=current.alias_id,
        )
        try:
            w.store.append_player_alias(proposed)
        except ValueError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(attempt, range(6)))
    assert outcomes.count(True) == 1
    history = w.store.player_alias_history(current.source_id, current.source_player_id)
    assert [item.version for item in history] == list(range(1, current.version + 2))


def test_failed_operations_and_batches_write_nothing(engine, tmp_path, monkeypatch):
    check_operations_are_atomic(world(tmp_path, store=PostgresIdentityStore(engine)), monkeypatch)
