"""SYS-04 identity cases against independently written labels (synthetic fixtures)."""

from datetime import timedelta

import pytest
from identity_support import SECOND_OBSERVATION, archive_payload, labels, world

from tennis_engine.normalization.contracts import (
    EventQuery,
    ResolutionAction,
    ResolutionDecision,
    SourcePlayerRecord,
)
from tennis_engine.normalization.names import NameMatch, blocking_keys, compare, fold
from tennis_engine.normalization.resolver import EvidenceResolver
from tennis_engine.normalization.store import MemoryIdentityStore

LABELS = labels()


@pytest.fixture
def loaded(tmp_path):
    state = world(tmp_path)
    report = state.run()
    assert report.review_required.get("sports-player", 0) == 0
    return state


def test_name_folding_and_candidate_keys_keep_source_text_out_of_identity():
    assert fold("Łukasz Wójcik") == "lukasz wojcik"
    assert fold("Zoran Đurić") == "zoran djuric"
    assert fold("María-José  Núñez") == "maria jose nunez"
    assert compare("Djuric Zoran", "Zoran Đurić") == NameMatch.EXACT
    assert compare("Z. Djuric", "Zoran Đurić") == NameMatch.INITIALS
    assert compare("Djuric Z", "Zoran Đurić") == NameMatch.INITIALS
    assert compare("Kowalski", "Jan Kowalski") == NameMatch.PARTIAL
    assert compare("J. Kowalski", "Piotr Kowalski") == NameMatch.NONE
    assert compare("J. K.", "Jan Kowalski") == NameMatch.NONE
    assert compare("", "Jan Kowalski") == NameMatch.NONE
    assert blocking_keys("J. Kowalski") == frozenset({"kowalski"})


@pytest.mark.parametrize("case", LABELS["players"], ids=lambda case: case["case_id"])
def test_player_labels(loaded, case):
    record = SourcePlayerRecord.model_validate(case["record"])
    resolution = loaded.resolver.resolve_player(record, at=loaded.clock.now())
    assert resolution.decision == ResolutionDecision(case["expected_decision"]), resolution.reasons
    expected = case["expected_player"]
    assert resolution.player_id == (loaded.player_id(expected) if expected else None)


@pytest.mark.parametrize("case", LABELS["events"], ids=lambda case: case["case_id"])
def test_event_labels(loaded, case):
    query = EventQuery.model_validate(case["query"])
    resolution = loaded.resolver.resolve_event(query, at=loaded.clock.now())
    assert resolution.decision == ResolutionDecision(case["expected_decision"]), resolution.reasons
    expected = case["expected_match"]
    assert resolution.match_id == (loaded.match_id(expected) if expected else None)
    assert resolution.blocks_recommendations == (expected is None)


def test_reversed_bookmaker_order_reports_orientation(loaded):
    forward, reverse = (
        loaded.resolver.resolve_event(
            EventQuery.model_validate(case["query"]), at=loaded.clock.now()
        )
        for case in LABELS["events"][:2]
    )
    assert forward.match_id == reverse.match_id
    assert forward.swapped is not None and reverse.swapped is not None
    assert forward.swapped != reverse.swapped
    match = loaded.store.match(forward.match_id)
    first = forward.participants[0].player_id
    assert (match.player_ids[0] != first) == forward.swapped


def test_label_set_contains_no_false_merge(loaded):
    """Zero known incorrect merges: every accepted label maps to its labelled player."""
    accepted = 0
    for case in LABELS["players"]:
        record = SourcePlayerRecord.model_validate(case["record"])
        resolution = loaded.resolver.resolve_player(record, at=loaded.clock.now())
        if resolution.decision == ResolutionDecision.AUTO_ACCEPT:
            accepted += 1
            assert case["expected_player"] is not None
            assert resolution.player_id == loaded.player_id(case["expected_player"])
    assert accepted == 3


def test_name_only_can_never_auto_accept_even_with_many_name_forms(loaded):
    store = loaded.store
    djuric = loaded.player_id("p-djuric")
    for name in ("Zoran Djuric", "Djuric Zoran", "Z. Djuric", "ZORAN ĐURIĆ"):
        record = SourcePlayerRecord(
            source_id="synthetic-stats", source_player_id=name, full_name=name
        )
        resolution = loaded.resolver.resolve_player(record, at=loaded.clock.now())
        assert resolution.decision == ResolutionDecision.REVIEW_REQUIRED
        assert all(
            candidate.score < loaded.resolver.policy.review_threshold
            for candidate in resolution.candidates
        )
    assert store.player(djuric).display_name == "Zoran Đurić"


def test_rescheduled_match_keeps_identity_and_old_cutoff_sees_old_time(tmp_path):
    state = world(tmp_path)
    state.run()
    match_id = state.match_id("m-3")
    archive_payload(state.service, state.clock, "payload-2.json", SECOND_OBSERVATION)
    state.clock.instant = SECOND_OBSERVATION + timedelta(hours=1)
    state.run()
    assert state.match_id("m-3") == match_id
    schedules = state.store.schedules(match_id)
    assert [item.scheduled_start.hour for item in schedules] == [10, 14]
    before = SECOND_OBSERVATION - timedelta(hours=1)
    assert state.resolver.known_start(match_id, before).hour == 10
    assert state.resolver.known_start(match_id, state.clock.now()).hour == 14


def test_bookmaker_source_without_create_permission_cannot_create_players():
    store = MemoryIdentityStore()
    resolver = EvidenceResolver(store)
    from datetime import UTC, datetime

    record = SourcePlayerRecord(
        source_id="synthetic-book", source_player_id="x", full_name="New Player", tour="ATP"
    )
    resolution = resolver.resolve_player(record, at=datetime(2026, 9, 19, tzinfo=UTC))
    assert resolution.decision == ResolutionDecision.REVIEW_REQUIRED
    assert resolution.action == ResolutionAction.NONE
