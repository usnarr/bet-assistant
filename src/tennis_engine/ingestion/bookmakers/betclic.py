"""Betclic Poland parser for the synthetic `betclic-synthetic-v1` fixture shape.

The shape is a placeholder, not a verified Betclic payload. F05.1 requires an approved
access path and a one-event comparison with the source presentation before this parser
may read real data. Betclic-specific assumptions stay in this module:

- starts are ISO 8601 with an explicit offset; a start without an offset is rejected;
- contestants are numbered 1 and 2 in source order;
- statuses are text; a suspended or closed market overrides its selections.
"""

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from tennis_engine.contracts.domain import Market, Tour

from .contracts import (
    EventState,
    Listing,
    QuoteState,
    RawQuote,
    RejectedRecord,
    SourceEvent,
    SourceParticipant,
)

BOOKMAKER = "betclic"
VERSION = "betclic-synthetic-v1"
MARKET_ALIASES = {"Zwycięzca meczu": Market.MATCH_WINNER}
STATUS = {"Open": QuoteState.OPEN, "Suspended": QuoteState.SUSPENDED, "Closed": QuoteState.CLOSED}
GENDER = {"M": Tour.ATP, "F": Tour.WTA}
ODDS = re.compile(r"^[0-9]+\.[0-9]{1,3}$")


class _Raw(BaseModel):
    # Added fields are tolerated; missing or retyped required fields are not.
    model_config = ConfigDict(extra="ignore", strict=True)


class _Selection(_Raw):
    id: str
    name: str
    contestant: int | None
    odds: str
    status: str
    boosted: bool


class _Market(_Raw):
    id: str
    name: str
    status: str
    selections: list[dict[str, Any]]


class _Contestant(_Raw):
    id: str | None
    name: str


class _Competition(_Raw):
    name: str
    gender: str


class _Match(_Raw):
    id: str
    competition: _Competition
    date: str
    live: bool
    cancelled: bool
    doubles: bool
    bestOf: int | None  # noqa: N815 - source field name.
    contestants: list[_Contestant]
    markets: list[dict[str, Any]]


class _Payload(_Raw):
    format: str
    matches: list[dict[str, Any]]


class SchemaDrift(ValueError):
    """The response does not match the parser's shape; F03 dead-letters it."""


def _start(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("start time has no UTC offset")
    return parsed.astimezone(UTC)


def _odds(value: str) -> Decimal:
    if not ODDS.fullmatch(value):
        raise ValueError(f"malformed odds {value!r}")
    odds = Decimal(value)
    if odds <= 1:
        raise ValueError(f"odds not above 1: {value!r}")
    return odds


class BetclicParser:
    bookmaker = BOOKMAKER
    version = VERSION

    def parse_listing(self, body: bytes) -> Listing:
        try:
            payload = _Payload.model_validate(json.loads(body))
        except (ValueError, ValidationError) as error:
            raise SchemaDrift(f"{VERSION}: invalid payload: {error}") from error
        if payload.format != VERSION:
            raise SchemaDrift(f"{VERSION}: unexpected format {payload.format!r}")
        events: list[SourceEvent] = []
        quotes: list[RawQuote] = []
        rejected: list[RejectedRecord] = []
        for raw in payload.matches:
            event_id = raw.get("id") if isinstance(raw.get("id"), str) else None
            try:
                match = _Match.model_validate(raw)
                if len(match.contestants) != 2:
                    raise ValueError("a match needs exactly two contestants")
                if match.bestOf not in (None, 3, 5):
                    raise ValueError(f"unknown best-of {match.bestOf}")
                state = (
                    EventState.CANCELLED
                    if match.cancelled
                    else EventState.STARTED
                    if match.live
                    else EventState.PRE_MATCH
                )
                event = SourceEvent(
                    bookmaker=BOOKMAKER,
                    source_event_id=match.id,
                    competition_name=match.competition.name,
                    tour=GENDER.get(match.competition.gender),
                    participants=(
                        SourceParticipant(
                            source_player_id=match.contestants[0].id,
                            name=match.contestants[0].name,
                        ),
                        SourceParticipant(
                            source_player_id=match.contestants[1].id,
                            name=match.contestants[1].name,
                        ),
                    ),
                    doubles=match.doubles,
                    best_of=match.bestOf,
                    scheduled_start=_start(match.date),
                    state=state,
                )
            except (ValueError, ValidationError) as error:
                rejected.append(
                    RejectedRecord(
                        source_event_id=event_id,
                        source_selection_id=None,
                        reason=f"event: {str(error)[:400]}",
                    )
                )
                continue
            events.append(event)
            for raw_market in match.markets:
                quotes.extend(self._market(match.id, raw_market, rejected))
        return Listing(
            bookmaker=BOOKMAKER,
            parser_version=VERSION,
            events=tuple(events),
            quotes=tuple(quotes),
            rejected=tuple(rejected),
        )

    def _market(
        self, event_id: str, raw: dict[str, Any], rejected: list[RejectedRecord]
    ) -> list[RawQuote]:
        try:
            market = _Market.model_validate(raw)
            market_state = STATUS[market.status]
        except (ValueError, ValidationError, KeyError) as error:
            rejected.append(
                RejectedRecord(
                    source_event_id=event_id,
                    source_selection_id=None,
                    reason=f"market: {str(error)[:400]}",
                )
            )
            return []
        canonical = MARKET_ALIASES.get(market.name)
        quotes = []
        for raw_selection in market.selections:
            selection_id = raw_selection.get("id")
            try:
                selection = _Selection.model_validate(raw_selection)
                state = STATUS[selection.status]
                if market_state != QuoteState.OPEN:
                    state = market_state
                index = None
                if canonical == Market.MATCH_WINNER:
                    if selection.contestant not in (1, 2):
                        raise ValueError(f"unknown contestant {selection.contestant}")
                    index = 0 if selection.contestant == 1 else 1
                quotes.append(
                    RawQuote(
                        bookmaker=BOOKMAKER,
                        source_event_id=event_id,
                        source_market_id=market.id,
                        source_selection_id=selection.id,
                        market_label=market.name,
                        market=canonical,
                        selection_label=selection.name,
                        participant_index=index,
                        line=None,
                        decimal_odds=_odds(selection.odds),
                        state=state,
                        promotion_marker="boosted" if selection.boosted else None,
                    )
                )
            except (ValueError, ValidationError, KeyError) as error:
                rejected.append(
                    RejectedRecord(
                        source_event_id=event_id,
                        source_selection_id=selection_id if isinstance(selection_id, str) else None,
                        reason=f"selection: {str(error)[:400]}",
                    )
                )
        return quotes
