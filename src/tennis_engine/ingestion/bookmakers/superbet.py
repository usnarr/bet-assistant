"""Superbet Poland parser for the synthetic `superbet-synthetic-v1` fixture shape.

The shape is a placeholder, not a verified Superbet payload (see F05.1). Superbet-specific
assumptions stay in this module:

- starts are epoch milliseconds in UTC; any other start type is rejected;
- participants come from one "A - B" name; exactly one separator is required;
- prices are bare JSON numbers read as exact decimals; a quoted price is rejected;
- statuses are numeric codes; an unknown code is rejected, never guessed.
"""

import json
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

BOOKMAKER = "superbet"
VERSION = "superbet-synthetic-v1"
SEPARATOR = " - "
MARKET_ALIASES = {"Zwycięzca": Market.MATCH_WINNER}
QUOTE_STATUS = {1: QuoteState.OPEN, 2: QuoteState.SUSPENDED, 3: QuoteState.CLOSED}
EVENT_STATUS = {0: EventState.PRE_MATCH, 1: EventState.STARTED, 9: EventState.CANCELLED}
OUTCOME_SIDE = {"1": 0, "2": 1}


class SchemaDrift(ValueError):
    """The response does not match the parser's shape; F03 dead-letters it."""


class _Raw(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, arbitrary_types_allowed=True)


class _Odds(_Raw):
    marketId: str  # noqa: N815 - source field names.
    marketName: str  # noqa: N815
    marketStatus: int  # noqa: N815
    outcomeId: str  # noqa: N815
    outcomeName: str  # noqa: N815
    price: Decimal
    status: int
    boost: bool


class _Event(_Raw):
    matchId: str  # noqa: N815
    matchName: str  # noqa: N815
    matchTimestamp: int  # noqa: N815
    tournamentName: str  # noqa: N815
    sportCategory: str  # noqa: N815
    bestOfSets: int | None  # noqa: N815
    status: int
    odds: list[dict[str, Any]]


class _Payload(_Raw):
    schema_: str
    data: list[dict[str, Any]]


def _participants(name: str) -> tuple[SourceParticipant, SourceParticipant]:
    parts = name.split(SEPARATOR)
    if len(parts) != 2 or not all(part.strip() for part in parts):
        raise ValueError(f"cannot split participants from {name!r}")
    return (
        SourceParticipant(source_player_id=None, name=parts[0].strip()),
        SourceParticipant(source_player_id=None, name=parts[1].strip()),
    )


def _category(value: str) -> tuple[Tour | None, bool]:
    tour_text, _, draw = value.partition(" ")
    if draw not in ("Singles", "Doubles"):
        raise ValueError(f"unknown sport category {value!r}")
    tour = Tour(tour_text) if tour_text in Tour.__members__ else None
    return tour, draw == "Doubles"


class SuperbetParser:
    bookmaker = BOOKMAKER
    version = VERSION

    def parse_listing(self, body: bytes) -> Listing:
        try:
            decoded = json.loads(body, parse_float=Decimal)
            if not isinstance(decoded, dict):
                raise ValueError("payload is not an object")
            payload = _Payload.model_validate(
                {"schema_": decoded.get("schema"), "data": decoded.get("data")}
            )
        except (ValueError, ValidationError) as error:
            raise SchemaDrift(f"{VERSION}: invalid payload: {error}") from error
        if payload.schema_ != VERSION:
            raise SchemaDrift(f"{VERSION}: unexpected schema {payload.schema_!r}")
        events: list[SourceEvent] = []
        quotes: list[RawQuote] = []
        rejected: list[RejectedRecord] = []
        for raw in payload.data:
            event_id = raw.get("matchId") if isinstance(raw.get("matchId"), str) else None
            try:
                item = _Event.model_validate(raw)
                tour, doubles = _category(item.sportCategory)
                if item.bestOfSets not in (None, 3, 5):
                    raise ValueError(f"unknown best-of {item.bestOfSets}")
                event = SourceEvent(
                    bookmaker=BOOKMAKER,
                    source_event_id=item.matchId,
                    competition_name=item.tournamentName,
                    tour=tour,
                    participants=_participants(item.matchName),
                    doubles=doubles,
                    best_of=item.bestOfSets,
                    scheduled_start=datetime.fromtimestamp(item.matchTimestamp / 1000, UTC),
                    state=EVENT_STATUS[item.status],
                )
            except (ValueError, ValidationError, KeyError) as error:
                rejected.append(
                    RejectedRecord(
                        source_event_id=event_id,
                        source_selection_id=None,
                        reason=f"event: {str(error)[:400]}",
                    )
                )
                continue
            events.append(event)
            for raw_odds in item.odds:
                quote = self._quote(item.matchId, raw_odds, rejected)
                if quote is not None:
                    quotes.append(quote)
        return Listing(
            bookmaker=BOOKMAKER,
            parser_version=VERSION,
            events=tuple(events),
            quotes=tuple(quotes),
            rejected=tuple(rejected),
        )

    def _quote(
        self, event_id: str, raw: dict[str, Any], rejected: list[RejectedRecord]
    ) -> RawQuote | None:
        selection_id = raw.get("outcomeId")
        try:
            odds = _Odds.model_validate(raw)
            if not odds.price.is_finite() or odds.price <= 1:
                raise ValueError(f"odds not above 1: {odds.price}")
            market = MARKET_ALIASES.get(odds.marketName)
            market_state = QUOTE_STATUS[odds.marketStatus]
            state = QUOTE_STATUS[odds.status] if market_state == QuoteState.OPEN else market_state
            index = None
            if market == Market.MATCH_WINNER:
                index = OUTCOME_SIDE[odds.outcomeName]
            return RawQuote(
                bookmaker=BOOKMAKER,
                source_event_id=event_id,
                source_market_id=odds.marketId,
                source_selection_id=odds.outcomeId,
                market_label=odds.marketName,
                market=market,
                selection_label=odds.outcomeName,
                participant_index=index,
                line=None,
                decimal_odds=odds.price,
                state=state,
                promotion_marker="boost" if odds.boost else None,
            )
        except (ValueError, ValidationError, KeyError) as error:
            rejected.append(
                RejectedRecord(
                    source_event_id=event_id,
                    source_selection_id=selection_id if isinstance(selection_id, str) else None,
                    reason=f"selection: {str(error)[:400]}",
                )
            )
            return None
