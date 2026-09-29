"""SYS-05 golden and end-to-end checks for each bookmaker adapter (synthetic shapes)."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from tennis_engine.common.clock import FrozenClock
from tennis_engine.common.ids import stable_id
from tennis_engine.governance.contracts import Principal, Purpose, Role, SourcePolicy
from tennis_engine.governance.service import GovernanceService, PermissionDenied
from tennis_engine.governance.store import GovernanceStore
from tennis_engine.infrastructure.object_store import LocalObjectStore
from tennis_engine.ingestion.bookmakers.adapter import (
    DriftAlert,
    DriftThresholds,
    drift_alerts,
    snapshot_metrics,
)
from tennis_engine.ingestion.bookmakers.contracts import Snapshot
from tennis_engine.ingestion.bookmakers.mapping import map_snapshot
from tennis_engine.ingestion.bookmakers.quotes import (
    ActionabilityPolicy,
    ActionabilityReason,
    QuoteObservation,
    evaluate_actionability,
)
from tennis_engine.ingestion.bookmakers.registry import ADAPTERS
from tennis_engine.ingestion.contracts import (
    FetchCapture,
    FetchDisposition,
    FetchOrigin,
    ParseStatus,
)
from tennis_engine.ingestion.service import IngestionService, ParseRejected
from tennis_engine.ingestion.store import MemoryIngestionStore
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

FIXTURES = Path(__file__).parent / "fixtures" / "bookmakers"
BOOKMAKERS = sorted(ADAPTERS)
POLL_1 = datetime(2026, 9, 21, 8, 0, tzinfo=UTC)
POLL_2 = POLL_1 + timedelta(seconds=30)


def load(bookmaker, poll):
    folder = FIXTURES / bookmaker
    body = (folder / f"payload-{poll}.json").read_bytes()
    return body, json.loads((folder / f"expected-{poll}.json").read_text("utf-8"))


def snapshot(bookmaker, poll):
    body, _ = load(bookmaker, poll)
    listing = ADAPTERS[bookmaker].parser.parse_listing(body)
    return Snapshot.observe(
        listing,
        observed_at=POLL_1 if poll == 1 else POLL_2,
        raw_content_sha256=hashlib.sha256(body).hexdigest(),
    )


@pytest.mark.parametrize("bookmaker", BOOKMAKERS)
@pytest.mark.parametrize("poll", [1, 2])
def test_golden_fixture_matches_independent_expectations(bookmaker, poll):
    _, expected = load(bookmaker, poll)
    parsed = snapshot(bookmaker, poll)
    assert len(parsed.events) + len(expected["rejected_events"]) >= 20
    events = {event.source_event_id: event for event in parsed.events}
    assert set(events) == set(expected["events"])
    for event_id, truth in expected["events"].items():
        event = events[event_id]
        assert event.scheduled_start == datetime.fromisoformat(truth["start"])
        assert event.state == truth["state"]
        assert [item.name for item in event.participants] == truth["participants"]
        assert event.doubles == truth["doubles"] and event.best_of == truth["best_of"]
        assert event.tour == truth["tour"]
    quotes = {quote.source_selection_id: quote for quote in parsed.quotes}
    assert set(quotes) == set(expected["quotes"])
    for selection_id, truth in expected["quotes"].items():
        quote = quotes[selection_id]
        # Exact decimal text is preserved, including trailing zeros.
        assert str(quote.decimal_odds) == truth["odds"]
        assert quote.state == truth["state"]
        assert quote.participant_index == truth["participant_index"]
        assert (quote.market is not None) == truth["supported"]
        assert (quote.promotion_marker is not None) == truth["promotion"]
    rejected_selections = {item.source_selection_id for item in parsed.rejected} - {None}
    rejected_events = {
        item.source_event_id for item in parsed.rejected if item.source_selection_id is None
    }
    assert rejected_selections == set(expected["rejected_selections"])
    assert rejected_events == set(expected["rejected_events"])


@pytest.mark.parametrize("bookmaker", BOOKMAKERS)
def test_schema_drift_raises_and_is_dead_lettered(bookmaker, tmp_path):
    adapter = ADAPTERS[bookmaker]
    body, _ = load(bookmaker, 1)
    for broken in (b"not json", b"{}", json.dumps({"format": "other-v9"}).encode()):
        with pytest.raises(ValueError):
            adapter.parser.parse_listing(broken)
    service = IngestionService(
        MemoryIngestionStore(), LocalObjectStore(tmp_path / "objects"), FrozenClock(POLL_1)
    )
    ok = archive(service, adapter.source_id, body, "ok")
    drift = archive(service, adapter.source_id, b'{"unexpected": true}', "drift")
    assert service.parse_observation(ok, adapter).status == ParseStatus.ACCEPTED
    with pytest.raises(ParseRejected):
        service.parse_observation(drift, adapter)
    assert service.repository.open_dead_letters(drift.content.content_id)


def archive(service, source_id, body, key):
    result = service.archive(
        idempotency_key=f"sys-05:{source_id}:{key}",
        observation_window=POLL_1,
        capture=FetchCapture(
            source_id=source_id,
            logical_resource_id="tennis-listing",
            request_identity="FILE synthetic",
            requested_at=POLL_1,
            completed_at=POLL_1 + timedelta(seconds=1),
            origin=FetchOrigin.FILE_IMPORT,
            disposition=FetchDisposition.SUCCESS,
            attempt_number=1,
            status_code=200,
            content_type="application/json",
            body=body,
        ),
        parser_candidate=ADAPTERS_BY_SOURCE[source_id].version,
        policy_version="fixture-v1",
        policy_revision=1,
    )
    return service.repository.observation(result.observation_id)


ADAPTERS_BY_SOURCE = {adapter.source_id: adapter for adapter in ADAPTERS.values()}


@pytest.mark.parametrize("bookmaker", BOOKMAKERS)
def test_drift_metrics_flag_invalid_records_and_volume_changes(bookmaker):
    metrics = snapshot_metrics(snapshot(bookmaker, 1))
    assert metrics.events == 20 and metrics.rejected_records == 4
    assert metrics.unknown_market_labels == 2
    alerts = drift_alerts(metrics, None, DriftThresholds())
    # Two malformed selections out of about fifty: above the proposed 1% limit.
    assert DriftAlert.INVALID_RECORDS in alerts
    assert DriftAlert.UNKNOWN_LABELS in alerts
    shrunk = metrics.model_copy(update={"events": 5})
    assert DriftAlert.VOLUME_CHANGE in drift_alerts(shrunk, metrics, DriftThresholds())
    empty = metrics.model_copy(update={"events": 0})
    assert DriftAlert.ZERO_EVENTS in drift_alerts(empty, None, DriftThresholds())


@pytest.mark.parametrize("bookmaker", BOOKMAKERS)
def test_repository_source_policy_keeps_the_adapter_disabled(bookmaker, tmp_path):
    adapter = ADAPTERS[bookmaker]
    store = GovernanceStore(
        tmp_path / "governance.sqlite3",
        Principal(identity="fixture-reviewer", role=Role.POLICY_REVIEWER),
        lambda: datetime(2026, 9, 21, tzinfo=UTC),
    )
    try:
        service = GovernanceService(store)
        with pytest.raises(PermissionDenied, match="SOURCE_UNKNOWN"):
            adapter.healthcheck(service, Purpose.PROTOTYPE)
        config = Path("configs/governance/sources") / f"{adapter.source_id}.json"
        store.save(
            SourcePolicy.model_validate_json(config.read_text("utf-8")),
            expected_revision=0,
            reason="Load repository draft",
        )
        with pytest.raises(PermissionDenied, match="SOURCE_DISABLED"):
            adapter.healthcheck(service, Purpose.PROTOTYPE)
    finally:
        store.close()


LOW, HIGH = sorted((UUID(int=21), UUID(int=22)), key=str)


class OneMatchResolver:
    """Resolves every listing to one canonical match, keyed by the first participant."""

    def __init__(self, first_names_low):
        self.first_names_low = first_names_low

    def resolve_player(self, record, context=None, *, at):
        raise NotImplementedError

    def resolve_event(self, query, *, at):
        low_first = query.participants[0].full_name in self.first_names_low
        order = (LOW, HIGH) if low_first else (HIGH, LOW)
        players = tuple(
            PlayerResolution(
                source_id=query.source_id,
                source_player_id=item.source_player_id,
                source_name=item.full_name,
                decision=ResolutionDecision.AUTO_ACCEPT,
                action=ResolutionAction.LINK_EXISTING,
                player_id=player,
                candidates=(),
                reasons=(),
                policy_version="fixture-policy",
                resolved_at=at,
            )
            for item, player in zip(query.participants, order, strict=True)
        )
        return MatchResolution(
            source_id=query.source_id,
            source_event_id=query.source_event_id,
            decision=ResolutionDecision.AUTO_ACCEPT,
            action=ResolutionAction.LINK_EXISTING,
            match_id=stable_id("fixture-match", query.source_event_id),
            swapped=not low_first,
            participants=(players[0], players[1]),
            candidates=(),
            reasons=(),
            policy_version="fixture-policy",
            resolved_at=at,
        )


def canonical(match_id):
    return Match(
        match_id=match_id,
        edition_id=UUID(int=1),
        tour="ATP",
        draw_type=DrawType.SINGLES,
        draw_stage=DrawStage.MAIN,
        round=Round.R128,
        best_of=BestOf.THREE,
        player_ids=(LOW, HIGH),
        created_at=POLL_1 - timedelta(days=1),
    )


@pytest.mark.parametrize("bookmaker", BOOKMAKERS)
def test_end_to_end_parse_map_and_actionability(bookmaker):
    adapter = ADAPTERS[bookmaker]
    resolver = OneMatchResolver({"Jan Kowalski", "Anna Nowak", "Piotr Lewandowski"})
    history: dict[str, list[QuoteObservation]] = {}
    mapped_polls = []
    for poll in (1, 2):
        parsed = snapshot(bookmaker, poll)
        mapped = map_snapshot(
            parsed,
            source_id=adapter.source_id,
            resolver=resolver,
            match_lookup=canonical,
            at=parsed.observed_at,
        )
        mapped_polls.append(mapped)
        events = {event.source_event_id: event for event in parsed.events}
        for quote in parsed.quotes:
            event = events[quote.source_event_id]
            history.setdefault(quote.source_selection_id, []).append(
                QuoteObservation(
                    quote=quote,
                    observed_at=parsed.observed_at,
                    parser_version=parsed.parser_version,
                    raw_content_sha256=parsed.raw_content_sha256,
                    scheduled_start=event.scheduled_start,
                    event_state=event.state,
                )
            )
    mapped = mapped_polls[0]
    unmapped = {item.source_event_id: item.reasons for item in mapped.unmapped}
    assert unmapped["ev-16"] == ("DOUBLES",) and "INCOMPATIBLE_FORMAT" in unmapped["ev-17"]
    by_selection = {quote.source_selection_id: quote for quote in mapped.quotes}
    # ev-01 lists Kowalski first, ev-09 lists him second: the same player either way.
    assert by_selection["ev-01-s1"].selection_player_id == LOW
    assert by_selection["ev-09-s2"].selection_player_id == LOW
    assert by_selection["ev-09-s1"].decimal_odds == Decimal("2.30")

    policy = ActionabilityPolicy()
    at = POLL_2 + timedelta(seconds=5)

    def decide(selection_id):
        return evaluate_actionability(history[selection_id], at=at, policy=policy)

    assert decide("ev-01-s1").actionable
    assert ActionabilityReason.QUOTE_UNCONFIRMED in decide("ev-03-s1").reasons  # Start moved.
    assert ActionabilityReason.QUOTE_UNCONFIRMED in decide("ev-04-s2").reasons  # Price moved.
    assert decide("ev-04-s1").actionable
    assert ActionabilityReason.QUOTE_SUSPENDED in decide("ev-10-s1").reasons
    assert ActionabilityReason.QUOTE_SUSPENDED in decide("ev-11-s2").reasons
    assert ActionabilityReason.EVENT_STARTED in decide("ev-18-s1").reasons
    assert ActionabilityReason.EVENT_CANCELLED in decide("ev-19-s1").reasons
    assert ActionabilityReason.QUOTE_CLOSED in decide("ev-20-s1").reasons
    # Polling stops helping once the quote ages past the limit.
    late = evaluate_actionability(
        history["ev-01-s1"], at=POLL_2 + timedelta(seconds=60), policy=policy
    )
    assert ActionabilityReason.QUOTE_STALE in late.reasons
