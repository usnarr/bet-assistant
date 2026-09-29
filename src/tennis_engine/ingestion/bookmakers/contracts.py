"""F05 bookmaker contracts shared by all adapters.

Adapters share these transport and domain contracts, not assumptions about labels, player
order, timezones or settlement. Each parser owns its status map and market aliases.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import Field, model_validator

from tennis_engine.common.contracts import Contract, ExactDecimal, Identifier, Timestamp
from tennis_engine.contracts.domain import Market, Tour

SourceKey = Annotated[str, Field(min_length=1, max_length=256)]
SourceText = Annotated[str, Field(min_length=1, max_length=512)]


class QuoteState(StrEnum):
    OPEN = "OPEN"
    SUSPENDED = "SUSPENDED"
    CLOSED = "CLOSED"


class EventState(StrEnum):
    PRE_MATCH = "PRE_MATCH"
    STARTED = "STARTED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class SourceParticipant(Contract):
    source_player_id: SourceKey | None
    name: SourceText


class SourceEvent(Contract):
    """One bookmaker event as listed. Participant order is the source order."""

    bookmaker: Identifier
    source_event_id: SourceKey
    competition_name: SourceText | None
    tour: Tour | None
    participants: tuple[SourceParticipant, SourceParticipant]
    doubles: bool
    best_of: Literal[3, 5] | None
    scheduled_start: Timestamp | None
    state: EventState


class RawQuote(Contract):
    """One source selection price. `participant_index` refers to the source order."""

    bookmaker: Identifier
    source_event_id: SourceKey
    source_market_id: SourceKey
    source_selection_id: SourceKey
    market_label: SourceText
    market: Market | None  # None: an unknown or unsupported market, kept for diagnostics.
    selection_label: SourceText
    participant_index: Literal[0, 1] | None
    line: ExactDecimal | None
    decimal_odds: ExactDecimal
    state: QuoteState
    promotion_marker: SourceText | None = None

    @model_validator(mode="after")
    def exact_odds(self) -> Self:
        if not self.decimal_odds.is_finite() or self.decimal_odds <= 1:
            raise ValueError("Decimal odds must be finite and above 1")
        return self

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (
            self.bookmaker,
            self.source_event_id,
            self.source_market_id,
            self.source_selection_id,
        )

    def fingerprint(self) -> tuple[object, ...]:
        """Values whose change starts a new quote interval (blueprint section 10.7)."""
        return (*self.key, self.line, self.decimal_odds, self.state)


class RejectedRecord(Contract):
    """A record-level parse failure. It is counted and kept, never silently dropped."""

    source_event_id: SourceKey | None
    source_selection_id: SourceKey | None
    reason: Annotated[str, Field(min_length=1, max_length=512)]


class Listing(Contract):
    """Everything a parser reads from one response body, in source form."""

    bookmaker: Identifier
    parser_version: Identifier
    events: tuple[SourceEvent, ...]
    quotes: tuple[RawQuote, ...]
    rejected: tuple[RejectedRecord, ...] = ()

    @model_validator(mode="after")
    def consistent(self) -> Self:
        event_ids = [event.source_event_id for event in self.events]
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("Duplicate source event IDs in one listing")
        known = set(event_ids)
        if any(quote.source_event_id not in known for quote in self.quotes):
            raise ValueError("A quote references an event not in the listing")
        books = {self.bookmaker}
        if {event.bookmaker for event in self.events} - books or {
            quote.bookmaker for quote in self.quotes
        } - books:
            raise ValueError("A listing contains one bookmaker only")
        return self


class Snapshot(Listing):
    """One successful poll. Observation time comes from F03, never from the payload."""

    observed_at: Timestamp
    raw_content_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @classmethod
    def observe(cls, listing: Listing, *, observed_at: datetime, raw_content_sha256: str) -> Self:
        return cls(
            **dict(listing),
            observed_at=observed_at,
            raw_content_sha256=raw_content_sha256,
        )


class BookmakerParser(Protocol):
    """Strict per-bookmaker parser. A schema mismatch raises; F03 dead-letters it."""

    @property
    def bookmaker(self) -> str: ...

    @property
    def version(self) -> str: ...

    def parse_listing(self, body: bytes) -> Listing: ...


class MappingReason(StrEnum):
    UNSUPPORTED_MARKET = "UNSUPPORTED_MARKET"
    DOUBLES = "DOUBLES"
    INCOMPATIBLE_FORMAT = "INCOMPATIBLE_FORMAT"
    EVENT_UNRESOLVED = "EVENT_UNRESOLVED"
    PARTICIPANT_MISMATCH = "PARTICIPANT_MISMATCH"
    SELECTION_UNMAPPED = "SELECTION_UNMAPPED"
    START_UNKNOWN = "START_UNKNOWN"


class CanonicalQuote(Contract):
    """A quote mapped to a canonical match and player through F04 identity."""

    schema_version: Literal["1.0"] = "1.0"
    quote_id: UUID
    bookmaker: Identifier
    match_id: UUID
    market: Market
    selection_player_id: UUID
    decimal_odds: ExactDecimal
    state: QuoteState
    source_event_id: SourceKey
    source_market_id: SourceKey
    source_selection_id: SourceKey
    source_order_swapped: bool
    scheduled_start: Timestamp
    promotion_marker: SourceText | None
    observed_at: Timestamp
    parser_version: Identifier
    raw_content_sha256: str
    resolution_policy_version: Identifier


class UnmappedQuote(Contract):
    bookmaker: Identifier
    source_event_id: SourceKey
    source_selection_id: SourceKey
    reasons: tuple[MappingReason, ...] = Field(min_length=1)
    detail: tuple[str, ...] = ()
    observed_at: Timestamp
