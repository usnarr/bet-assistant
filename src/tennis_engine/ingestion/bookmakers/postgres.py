"""PostgreSQL persistence for the F05 append-only quote history."""

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from tennis_engine.normalization.contracts import MatchResolution

from .history import PollRecord, QuoteKey
from .quotes import QuoteObservation


def _json(model: Any) -> str:
    return json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


class PostgresHistoryStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def add_poll(self, poll: PollRecord) -> bool:
        with self.engine.begin() as db:
            inserted = db.execute(
                text(
                    "INSERT INTO tennis.bookmaker_poll (poll_id, bookmaker, parser_version, "
                    "observed_at, raw_content_sha256, source_event_ids) VALUES (:poll_id, "
                    ":bookmaker, :parser, :observed_at, :sha, CAST(:events AS JSONB)) "
                    "ON CONFLICT (poll_id) DO NOTHING"
                ),
                {
                    "poll_id": poll.poll_id,
                    "bookmaker": poll.bookmaker,
                    "parser": poll.parser_version,
                    "observed_at": poll.observed_at,
                    "sha": poll.raw_content_sha256,
                    "events": json.dumps(list(poll.source_event_ids)),
                },
            )
            return bool(inserted.rowcount)

    def add_observation(self, observation_id: UUID, observation: QuoteObservation) -> bool:
        quote = observation.quote
        with self.engine.begin() as db:
            inserted = db.execute(
                text(
                    "INSERT INTO tennis.bookmaker_quote_observation (observation_id, bookmaker, "
                    "source_event_id, source_market_id, source_selection_id, market, state, "
                    "decimal_odds, observed_at, raw_content_sha256, payload) VALUES (:id, "
                    ":bookmaker, :event, :market_id, :selection, :market, :state, :odds, "
                    ":observed_at, :sha, CAST(:payload AS JSONB)) "
                    "ON CONFLICT (observation_id) DO NOTHING"
                ),
                {
                    "id": observation_id,
                    "bookmaker": quote.bookmaker,
                    "event": quote.source_event_id,
                    "market_id": quote.source_market_id,
                    "selection": quote.source_selection_id,
                    "market": quote.market.value if quote.market else None,
                    "state": quote.state.value,
                    "odds": quote.decimal_odds,
                    "observed_at": observation.observed_at,
                    "sha": observation.raw_content_sha256,
                    "payload": _json(observation),
                },
            )
            if inserted.rowcount:
                return True
            stored = db.execute(
                text(
                    "SELECT payload FROM tennis.bookmaker_quote_observation "
                    "WHERE observation_id = :id"
                ),
                {"id": observation_id},
            ).scalar_one()
            if QuoteObservation.model_validate(stored) != observation:
                raise ValueError("An observation ID was reused with other content")
            return False

    def add_mapping(self, mapping_id: UUID, bookmaker: str, resolution: MatchResolution) -> bool:
        with self.engine.begin() as db:
            inserted = db.execute(
                text(
                    "INSERT INTO tennis.bookmaker_event_mapping (mapping_id, bookmaker, "
                    "source_event_id, match_id, decision, resolved_at, payload) VALUES (:id, "
                    ":bookmaker, :event, :match_id, :decision, :resolved_at, "
                    "CAST(:payload AS JSONB)) ON CONFLICT (mapping_id) DO NOTHING"
                ),
                {
                    "id": mapping_id,
                    "bookmaker": bookmaker,
                    "event": resolution.source_event_id,
                    "match_id": resolution.match_id,
                    "decision": resolution.decision.value,
                    "resolved_at": resolution.resolved_at,
                    "payload": _json(resolution),
                },
            )
            return bool(inserted.rowcount)

    def observations(self, key: QuoteKey, until: datetime) -> Sequence[QuoteObservation]:
        with self.engine.connect() as db:
            rows = db.execute(
                text(
                    "SELECT payload FROM tennis.bookmaker_quote_observation WHERE bookmaker = :b "
                    "AND source_event_id = :e AND source_market_id = :m "
                    "AND source_selection_id = :s AND observed_at <= :until "
                    "ORDER BY observed_at, raw_content_sha256"
                ),
                {"b": key[0], "e": key[1], "m": key[2], "s": key[3], "until": until},
            )
            return [QuoteObservation.model_validate(row[0]) for row in rows]

    def event_polls(
        self, bookmaker: str, source_event_id: str, until: datetime
    ) -> Sequence[datetime]:
        with self.engine.connect() as db:
            rows = db.execute(
                text(
                    "SELECT observed_at FROM tennis.bookmaker_poll WHERE bookmaker = :b "
                    "AND source_event_ids @> CAST(:e AS JSONB) AND observed_at <= :until "
                    "ORDER BY observed_at"
                ),
                {"b": bookmaker, "e": json.dumps([source_event_id]), "until": until},
            )
            return [row[0] for row in rows]

    def mappings(
        self, bookmaker: str, source_event_id: str, until: datetime
    ) -> Sequence[MatchResolution]:
        with self.engine.connect() as db:
            rows = db.execute(
                text(
                    "SELECT payload FROM tennis.bookmaker_event_mapping WHERE bookmaker = :b "
                    "AND source_event_id = :e AND resolved_at <= :until ORDER BY resolved_at"
                ),
                {"b": bookmaker, "e": source_event_id, "until": until},
            )
            return [MatchResolution.model_validate(row[0]) for row in rows]
