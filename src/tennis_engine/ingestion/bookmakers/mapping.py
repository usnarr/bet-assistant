"""F05.2, F05.3: map bookmaker events and selections to canonical matches through F04.

Source player order never decides the canonical selection: each selection maps to the
player that F04 resolved for that source participant. Doubles, best-of-five, unresolved
events, unknown starts and unsupported markets stay unmapped with explicit reasons.
"""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from tennis_engine.common.contracts import Contract
from tennis_engine.common.ids import stable_id
from tennis_engine.contracts.domain import Market
from tennis_engine.normalization.contracts import (
    BestOf,
    DrawType,
    EventQuery,
    IdentityResolver,
    Match,
    MatchResolution,
    SourcePlayerRecord,
)

from .contracts import CanonicalQuote, MappingReason, Snapshot, SourceEvent, UnmappedQuote

SUPPORTED_MARKETS = frozenset({Market.MATCH_WINNER})


class MappedSnapshot(Contract):
    quotes: tuple[CanonicalQuote, ...]
    unmapped: tuple[UnmappedQuote, ...]
    resolutions: tuple[MatchResolution, ...]


def event_query(source_id: str, event: SourceEvent) -> EventQuery:
    """Build the F04 query. A missing source player ID gets a per-event key, so a name
    never becomes a reusable alias key."""
    participants = tuple(
        SourcePlayerRecord(
            source_id=source_id,
            source_player_id=participant.source_player_id
            or f"event:{event.source_event_id}:{index}",
            full_name=participant.name,
            tour=event.tour,
        )
        for index, participant in enumerate(event.participants)
    )
    return EventQuery(
        source_id=source_id,
        source_event_id=event.source_event_id,
        tour=event.tour,
        draw_type=DrawType.DOUBLES if event.doubles else DrawType.SINGLES,
        participants=(participants[0], participants[1]),
        scheduled_start=event.scheduled_start,
        tournament_name=event.competition_name,
    )


def _event_problems(
    event: SourceEvent,
    resolution: MatchResolution | None,
    match: Match | None,
) -> tuple[list[MappingReason], list[str]]:
    reasons: list[MappingReason] = []
    detail: list[str] = []
    if event.doubles:
        reasons.append(MappingReason.DOUBLES)
    if event.best_of == 5:
        reasons.append(MappingReason.INCOMPATIBLE_FORMAT)
    if event.scheduled_start is None:
        reasons.append(MappingReason.START_UNKNOWN)
    if resolution is None:
        return reasons, detail
    if resolution.blocks_recommendations or match is None:
        reasons.append(MappingReason.EVENT_UNRESOLVED)
        detail.extend(resolution.reasons)
        return reasons, detail
    if match.draw_type != DrawType.SINGLES or match.best_of != BestOf.THREE:
        reasons.append(MappingReason.INCOMPATIBLE_FORMAT)
        detail.append(f"canonical:{match.draw_type.value}:{match.best_of.value}")
    resolved = {participant.player_id for participant in resolution.participants}
    if resolved != set(match.player_ids):
        reasons.append(MappingReason.PARTICIPANT_MISMATCH)
    return reasons, detail


def map_snapshot(
    snapshot: Snapshot,
    *,
    source_id: str,
    resolver: IdentityResolver,
    match_lookup: Callable[[UUID], Match],
    at: datetime,
    resolution_policy_version: str | None = None,
) -> MappedSnapshot:
    """Map one parsed snapshot. `at` is the decision time for identity evidence."""
    quotes: list[CanonicalQuote] = []
    unmapped: list[UnmappedQuote] = []
    resolutions: list[MatchResolution] = []
    events = {event.source_event_id: event for event in snapshot.events}
    for event in snapshot.events:
        resolution: MatchResolution | None = None
        match: Match | None = None
        # Doubles never reach identity resolution; they are out of scope.
        if not event.doubles:
            resolution = resolver.resolve_event(event_query(source_id, event), at=at)
            resolutions.append(resolution)
            if resolution.match_id is not None and not resolution.blocks_recommendations:
                match = match_lookup(resolution.match_id)
        event_reasons, detail = _event_problems(event, resolution, match)
        for quote in (
            item for item in snapshot.quotes if item.source_event_id == event.source_event_id
        ):
            reasons = list(event_reasons)
            if quote.market not in SUPPORTED_MARKETS:
                reasons.append(MappingReason.UNSUPPORTED_MARKET)
            if quote.participant_index is None:
                reasons.append(MappingReason.SELECTION_UNMAPPED)
            if reasons:
                unmapped.append(
                    UnmappedQuote(
                        bookmaker=quote.bookmaker,
                        source_event_id=quote.source_event_id,
                        source_selection_id=quote.source_selection_id,
                        reasons=tuple(dict.fromkeys(reasons)),
                        detail=tuple(detail),
                        observed_at=snapshot.observed_at,
                    )
                )
                continue
            assert resolution is not None and match is not None
            assert quote.market is not None and quote.participant_index is not None
            assert event.scheduled_start is not None
            player_id = resolution.participants[quote.participant_index].player_id
            assert player_id is not None
            quotes.append(
                CanonicalQuote(
                    quote_id=stable_id(
                        "bookmaker-quote",
                        ":".join(
                            (
                                *quote.key,
                                snapshot.observed_at.isoformat(),
                                snapshot.raw_content_sha256,
                            )
                        ),
                    ),
                    bookmaker=quote.bookmaker,
                    match_id=match.match_id,
                    market=quote.market,
                    selection_player_id=player_id,
                    decimal_odds=quote.decimal_odds,
                    state=quote.state,
                    source_event_id=quote.source_event_id,
                    source_market_id=quote.source_market_id,
                    source_selection_id=quote.source_selection_id,
                    source_order_swapped=bool(resolution.swapped),
                    scheduled_start=events[quote.source_event_id].scheduled_start,
                    promotion_marker=quote.promotion_marker,
                    observed_at=snapshot.observed_at,
                    parser_version=snapshot.parser_version,
                    raw_content_sha256=snapshot.raw_content_sha256,
                    resolution_policy_version=resolution_policy_version
                    or resolution.policy_version,
                )
            )
    return MappedSnapshot(
        quotes=tuple(quotes), unmapped=tuple(unmapped), resolutions=tuple(resolutions)
    )
