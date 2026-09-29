from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from identity_support import labels, world

from tennis_engine.ingestion.bookmakers.contracts import (
    EventState,
    MappingReason,
    QuoteState,
    RawQuote,
    Snapshot,
    SourceEvent,
    SourceParticipant,
)
from tennis_engine.ingestion.bookmakers.mapping import map_snapshot
from tennis_engine.normalization.contracts import (
    BestOf,
    DrawStage,
    DrawType,
    Match,
    MatchResolution,
    PlayerResolution,
    ResolutionAction,
    ResolutionDecision,
    Round,
)

OBSERVED = datetime(2026, 9, 21, 8, tzinfo=UTC)
LOW, HIGH = sorted((UUID(int=11), UUID(int=12)), key=str)
MATCH_ID = UUID(int=500)


def event(event_id="e-1", names=("A Player", "B Player"), **overrides):
    return SourceEvent.model_validate(
        {
            "bookmaker": "synthetic-book",
            "source_event_id": event_id,
            "competition_name": "Synthetic Open",
            "tour": "ATP",
            "participants": [{"source_player_id": None, "name": name} for name in names],
            "doubles": False,
            "best_of": 3,
            "scheduled_start": OBSERVED + timedelta(hours=3),
            "state": EventState.PRE_MATCH,
        }
        | overrides
    )


def quotes(event_id="e-1", market="TENNIS_MATCH_WINNER"):
    return tuple(
        RawQuote(
            bookmaker="synthetic-book",
            source_event_id=event_id,
            source_market_id=f"{event_id}-mw",
            source_selection_id=f"{event_id}-{index}",
            market_label="Winner",
            market=market,
            selection_label=f"side {index}",
            participant_index=index,
            line=None,
            decimal_odds=Decimal("1.80") + index,
            state=QuoteState.OPEN,
        )
        for index in (0, 1)
    )


def snapshot(events, extra_quotes=()):
    return Snapshot(
        bookmaker="synthetic-book",
        parser_version="synthetic-book-v1",
        observed_at=OBSERVED,
        raw_content_sha256="b" * 64,
        events=tuple(events),
        quotes=tuple(q for item in events for q in quotes(item.source_event_id)) + extra_quotes,
    )


def player_resolution(index, player_id, accepted=True):
    return PlayerResolution(
        source_id="synthetic-book",
        source_player_id=f"p-{index}",
        source_name=f"name {index}",
        decision=ResolutionDecision.AUTO_ACCEPT if accepted else ResolutionDecision.REVIEW_REQUIRED,
        action=ResolutionAction.LINK_EXISTING if accepted else ResolutionAction.NONE,
        player_id=player_id if accepted else None,
        candidates=(),
        reasons=() if accepted else ("NAME_ONLY",),
        policy_version="fixture-policy",
        resolved_at=OBSERVED,
    )


class FakeResolver:
    """Returns a fixed resolution per source event; source order HIGH, LOW means swapped."""

    def __init__(self, orders):
        self.orders = orders
        self.queries = []

    def resolve_player(self, record, context=None, *, at):
        raise NotImplementedError

    def resolve_event(self, query, *, at):
        self.queries.append(query)
        order = self.orders.get(query.source_event_id)
        if order is None:
            return MatchResolution(
                source_id=query.source_id,
                source_event_id=query.source_event_id,
                decision=ResolutionDecision.REVIEW_REQUIRED,
                action=ResolutionAction.NONE,
                match_id=None,
                swapped=None,
                participants=(player_resolution(0, None, False), player_resolution(1, None, False)),
                candidates=(),
                reasons=("PARTICIPANT_UNRESOLVED",),
                policy_version="fixture-policy",
                resolved_at=at,
            )
        return MatchResolution(
            source_id=query.source_id,
            source_event_id=query.source_event_id,
            decision=ResolutionDecision.AUTO_ACCEPT,
            action=ResolutionAction.LINK_EXISTING,
            match_id=MATCH_ID,
            swapped=order[0] != LOW,
            participants=(player_resolution(0, order[0]), player_resolution(1, order[1])),
            candidates=(),
            reasons=(),
            policy_version="fixture-policy",
            resolved_at=at,
        )


def canonical(best_of=BestOf.THREE, draw_type=DrawType.SINGLES):
    return Match(
        match_id=MATCH_ID,
        edition_id=UUID(int=900),
        tour="ATP",
        draw_type=draw_type,
        draw_stage=DrawStage.MAIN,
        round=Round.R128,
        best_of=best_of,
        player_ids=(LOW, HIGH),
        created_at=OBSERVED - timedelta(days=1),
    )


def run(events, orders, match=None, extra_quotes=()):
    return map_snapshot(
        snapshot(events, extra_quotes),
        source_id="synthetic-book",
        resolver=FakeResolver(orders),
        match_lookup=lambda _: match or canonical(),
        at=OBSERVED,
    )


def test_selection_follows_resolved_player_not_source_order():
    forward = run([event("e-1")], {"e-1": (LOW, HIGH)})
    reverse = run([event("e-1")], {"e-1": (HIGH, LOW)})
    by_odds = lambda result: {q.decimal_odds: q.selection_player_id for q in result.quotes}  # noqa: E731
    assert by_odds(forward) == {Decimal("1.80"): LOW, Decimal("2.80"): HIGH}
    assert by_odds(reverse) == {Decimal("1.80"): HIGH, Decimal("2.80"): LOW}
    assert {q.source_order_swapped for q in reverse.quotes} == {True}


def test_unresolved_doubles_and_best_of_five_stay_unmapped():
    result = run(
        [
            event("e-unresolved"),
            event("e-doubles", doubles=True),
            event("e-bo5", best_of=5),
            event("e-nostart", scheduled_start=None),
        ],
        {"e-bo5": (LOW, HIGH), "e-nostart": (LOW, HIGH)},
    )
    assert not result.quotes
    reasons = {item.source_event_id: item.reasons for item in result.unmapped}
    assert reasons["e-unresolved"] == (MappingReason.EVENT_UNRESOLVED,)
    assert reasons["e-doubles"] == (MappingReason.DOUBLES,)
    assert MappingReason.INCOMPATIBLE_FORMAT in reasons["e-bo5"]
    assert MappingReason.START_UNKNOWN in reasons["e-nostart"]
    assert len(result.resolutions) == 3  # Doubles never reach identity resolution.


def test_canonical_format_and_participants_are_checked():
    bo5 = run([event()], {"e-1": (LOW, HIGH)}, canonical(best_of=BestOf.FIVE))
    assert {r for item in bo5.unmapped for r in item.reasons} == {MappingReason.INCOMPATIBLE_FORMAT}
    stranger = run([event()], {"e-1": (LOW, UUID(int=77))})
    assert MappingReason.PARTICIPANT_MISMATCH in stranger.unmapped[0].reasons


def test_unknown_markets_are_kept_for_diagnostics_only():
    extra = (
        RawQuote(
            bookmaker="synthetic-book",
            source_event_id="e-1",
            source_market_id="e-1-games",
            source_selection_id="e-1-over",
            market_label="Total games",
            market=None,
            selection_label="Over 22.5",
            participant_index=None,
            line=Decimal("22.5"),
            decimal_odds=Decimal("1.90"),
            state=QuoteState.OPEN,
        ),
    )
    result = run([event()], {"e-1": (LOW, HIGH)}, extra_quotes=extra)
    assert len(result.quotes) == 2
    assert result.unmapped[0].reasons == (
        MappingReason.UNSUPPORTED_MARKET,
        MappingReason.SELECTION_UNMAPPED,
    )


def test_missing_source_player_ids_never_become_name_alias_keys():
    resolver = FakeResolver({"e-1": (LOW, HIGH)})
    map_snapshot(
        snapshot([event()]),
        source_id="synthetic-book",
        resolver=resolver,
        match_lookup=lambda _: canonical(),
        at=OBSERVED,
    )
    keys = [item.source_player_id for item in resolver.queries[0].participants]
    assert keys == ["event:e-1:0", "event:e-1:1"]


def test_real_f04_resolver_maps_reversed_bookmaker_listings(tmp_path):
    state = world(tmp_path)
    state.run()
    cases = labels()["events"][:2]  # Same match, source order reversed.
    results = []
    for case in cases:
        query = case["query"]
        source_event = SourceEvent(
            bookmaker="synthetic-book",
            source_event_id=query["source_event_id"],
            competition_name=None,
            tour=query["tour"],
            participants=tuple(
                SourceParticipant(source_player_id=item["source_player_id"], name=item["full_name"])
                for item in query["participants"]
            ),
            doubles=False,
            best_of=None,
            scheduled_start=query["scheduled_start"],
            state=EventState.PRE_MATCH,
        )
        results.append(
            map_snapshot(
                snapshot([source_event]),
                source_id="synthetic-book",
                resolver=state.resolver,
                match_lookup=state.store.match,
                at=state.clock.now(),
            )
        )
    forward, reverse = results
    assert len(forward.quotes) == len(reverse.quotes) == 2, (forward.unmapped, reverse.unmapped)
    # The first source participant of each listing is a different canonical player.
    assert forward.quotes[0].selection_player_id == reverse.quotes[1].selection_player_id
    assert forward.quotes[0].match_id == reverse.quotes[0].match_id
