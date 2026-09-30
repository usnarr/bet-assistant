"""Fortuna Poland parser for the synthetic `fortuna-synthetic-v1` fixture shape.

The shape is a placeholder, not a verified Fortuna payload (see F05.1). Fortuna-specific
assumptions stay in this module:

- starts are naive local times in Europe/Warsaw; a time that is ambiguous or does not
  exist around a daylight-saving change is rejected, never guessed;
- odds are text with a comma decimal separator;
- the `home`/`away` position decides the participant, not the list order;
- states are booleans; a market that is not open closes all its outcomes.
"""

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

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

BOOKMAKER = "fortuna"
VERSION = "fortuna-synthetic-v1"
WARSAW = ZoneInfo("Europe/Warsaw")
MARKET_ALIASES = {"MATCH_RESULT": Market.MATCH_WINNER}
EVENT_STATE = {
    "NOT_STARTED": EventState.PRE_MATCH,
    "IN_PLAY": EventState.STARTED,
    "CANCELLED": EventState.CANCELLED,
}
FORMAT = {"BO3": 3, "BO5": 5}
POSITION = {"home": 0, "away": 1}
ODDS = re.compile(r"^[0-9]+,[0-9]{1,3}$")


class SchemaDrift(ValueError):
    """The response does not match the parser's shape; F03 dead-letters it."""


class _Raw(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class _Outcome(_Raw):
    code: str
    label: str
    position: str | None
    odds: str
    active: bool
    suspended: bool
    superOdds: bool  # noqa: N815 - source field name.


class _Market(_Raw):
    code: str
    type: str
    open: bool
    suspended: bool
    outcomes: list[dict[str, Any]]


class _Participant(_Raw):
    position: str
    id: str | None
    name: str


class _Event(_Raw):
    code: str
    state: str
    format: str | None
    pair: bool
    participants: list[_Participant]
    markets: list[dict[str, Any]]
    startLocal: str  # noqa: N815


class _Competition(_Raw):
    name: str
    category: str
    events: list[dict[str, Any]]


class _Sport(_Raw):
    sport: str
    competitions: list[_Competition]


class _Payload(_Raw):
    version: str
    sports: list[_Sport]


def local_start(value: str) -> datetime:
    """Convert a Warsaw wall-clock time to UTC; reject gaps and repeated hours."""
    naive = datetime.strptime(value, "%Y-%m-%d %H:%M")
    first = naive.replace(tzinfo=WARSAW, fold=0)
    second = naive.replace(tzinfo=WARSAW, fold=1)
    if first.astimezone(UTC).astimezone(WARSAW).replace(tzinfo=None) != naive:
        raise ValueError(f"nonexistent local start {value!r}")
    if first.utcoffset() != second.utcoffset():
        raise ValueError(f"ambiguous local start {value!r}")
    return first.astimezone(UTC)


def _odds(value: str) -> Decimal:
    if not ODDS.fullmatch(value):
        raise ValueError(f"malformed odds {value!r}")
    odds = Decimal(value.replace(",", "."))
    if odds <= 1:
        raise ValueError(f"odds not above 1: {value!r}")
    return odds


def _outcome_state(market: _Market, outcome: _Outcome) -> QuoteState:
    if not market.open or not outcome.active:
        return QuoteState.CLOSED
    if market.suspended or outcome.suspended:
        return QuoteState.SUSPENDED
    return QuoteState.OPEN


class FortunaParser:
    bookmaker = BOOKMAKER
    version = VERSION

    def parse_listing(self, body: bytes) -> Listing:
        try:
            payload = _Payload.model_validate(json.loads(body))
        except (ValueError, ValidationError) as error:
            raise SchemaDrift(f"{VERSION}: invalid payload: {error}") from error
        if payload.version != VERSION:
            raise SchemaDrift(f"{VERSION}: unexpected version {payload.version!r}")
        events: list[SourceEvent] = []
        quotes: list[RawQuote] = []
        rejected: list[RejectedRecord] = []
        for sport in payload.sports:
            if sport.sport != "tennis":
                continue
            for competition in sport.competitions:
                tour = (
                    Tour(competition.category) if competition.category in Tour.__members__ else None
                )
                for raw in competition.events:
                    event_id = raw.get("code") if isinstance(raw.get("code"), str) else None
                    try:
                        item = _Event.model_validate(raw)
                        event = self._event(item, competition.name, tour)
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
                    for raw_market in item.markets:
                        quotes.extend(self._market(item.code, raw_market, rejected))
        return Listing(
            bookmaker=BOOKMAKER,
            parser_version=VERSION,
            events=tuple(events),
            quotes=tuple(quotes),
            rejected=tuple(rejected),
        )

    @staticmethod
    def _event(item: _Event, competition: str, tour: Tour | None) -> SourceEvent:
        positions = {participant.position: participant for participant in item.participants}
        if len(item.participants) != 2 or set(positions) != {"home", "away"}:
            raise ValueError("an event needs one home and one away participant")
        if item.format is not None and item.format not in FORMAT:
            raise ValueError(f"unknown format {item.format!r}")
        home, away = positions["home"], positions["away"]
        return SourceEvent(
            bookmaker=BOOKMAKER,
            source_event_id=item.code,
            competition_name=competition,
            tour=tour,
            participants=(
                SourceParticipant(source_player_id=home.id, name=home.name),
                SourceParticipant(source_player_id=away.id, name=away.name),
            ),
            doubles=item.pair,
            best_of=FORMAT.get(item.format) if item.format else None,
            scheduled_start=local_start(item.startLocal),
            state=EVENT_STATE[item.state],
        )

    @staticmethod
    def _market(
        event_id: str, raw: dict[str, Any], rejected: list[RejectedRecord]
    ) -> list[RawQuote]:
        try:
            market = _Market.model_validate(raw)
        except ValidationError as error:
            rejected.append(
                RejectedRecord(
                    source_event_id=event_id,
                    source_selection_id=None,
                    reason=f"market: {str(error)[:400]}",
                )
            )
            return []
        canonical = MARKET_ALIASES.get(market.type)
        quotes = []
        for raw_outcome in market.outcomes:
            selection_id = raw_outcome.get("code")
            try:
                outcome = _Outcome.model_validate(raw_outcome)
                index = None
                if canonical == Market.MATCH_WINNER:
                    if outcome.position not in POSITION:
                        raise ValueError(f"unknown position {outcome.position!r}")
                    index = POSITION[outcome.position]
                quotes.append(
                    RawQuote(
                        bookmaker=BOOKMAKER,
                        source_event_id=event_id,
                        source_market_id=market.code,
                        source_selection_id=outcome.code,
                        market_label=market.type,
                        market=canonical,
                        selection_label=outcome.label,
                        participant_index=index,
                        line=None,
                        decimal_odds=_odds(outcome.odds),
                        state=_outcome_state(market, outcome),
                        promotion_marker="superOdds" if outcome.superOdds else None,
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
