"""SYS-04 warehouse, backfill, correction, review and validator behavior."""

from datetime import UTC, datetime, timedelta

import pytest
from identity_support import FIRST_OBSERVATION, SECOND_OBSERVATION, archive_payload, world

from tennis_engine.contracts.domain import AvailabilityClass
from tennis_engine.normalization.contracts import (
    BestOf,
    DrawType,
    MatchStatus,
    ResolutionDecision,
    ReviewState,
    ServeReturnCounts,
    SetScore,
    SourceMatchRecord,
    SourcePlayerRecord,
)
from tennis_engine.normalization.store import ReviewKind
from tennis_engine.normalization.validation import errors, set_winner, validate_match


def match_record(**overrides):
    data = {
        "source_id": "synthetic-sports",
        "source_match_id": "v-1",
        "source_tournament_id": "t-atp",
        "season": 2026,
        "tour": "ATP",
        "draw_type": "SINGLES",
        "best_of": "BEST_OF_3",
        "participant_ids": ("a", "b"),
        "status": "COMPLETED",
        "winner_id": "a",
        "sets": ({"games": (6, 4)}, {"games": (7, 6), "tiebreak_points": (9, 7)}),
    }
    return SourceMatchRecord.model_validate(data | overrides)


def codes(record):
    return {issue.code for issue in errors(validate_match(record))}


def test_set_legality():
    assert set_winner(SetScore(games=(6, 4))) == 0
    assert set_winner(SetScore(games=(5, 7))) == 1
    assert set_winner(SetScore(games=(12, 10))) == 0
    assert set_winner(SetScore(games=(6, 5))) is None
    assert set_winner(SetScore(games=(7, 6))) is None
    assert set_winner(SetScore(games=(7, 6), tiebreak_points=(7, 6))) is None
    assert set_winner(SetScore(games=(7, 6), tiebreak_points=(5, 7))) is None
    assert set_winner(SetScore(games=(6, 7), tiebreak_points=(10, 12))) == 1
    assert set_winner(SetScore(games=(1, 0), tiebreak_points=(10, 8))) == 0
    assert set_winner(SetScore(games=(1, 0), tiebreak_points=(7, 5))) is None
    assert set_winner(SetScore(games=(8, 4))) is None


def test_match_validation_rejects_illegal_state_and_flags_unsupported_scope():
    assert codes(match_record()) == set()
    assert "SAME_PARTICIPANT" in codes(match_record(participant_ids=("a", "a"), winner_id="a"))
    assert "INVALID_WINNER" in codes(match_record(winner_id="c"))
    assert "MISSING_WINNER" in codes(match_record(winner_id=None))
    assert "SCORE_WINNER_MISMATCH" in codes(match_record(winner_id="b"))
    assert "SCORE_WINNER_MISMATCH" in codes(match_record(sets=({"games": (6, 4)},)))
    assert "SETS_AFTER_END" in codes(
        match_record(sets=({"games": (6, 4)}, {"games": (6, 4)}, {"games": (6, 4)}))
    )
    assert "UNEXPECTED_SCORE" in codes(
        match_record(status="SCHEDULED", winner_id=None, sets=({"games": (1, 0)},))
    )
    assert "UNEXPECTED_WINNER" in codes(match_record(status="CANCELLED", sets=()))
    assert "UNEXPECTED_SCORE" in codes(match_record(status="WALKOVER"))
    assert "RETIREMENT_AFTER_END" in codes(match_record(status="RETIRED"))
    assert (
        codes(match_record(status="RETIRED", sets=({"games": (6, 4)}, {"games": (3, 2)}))) == set()
    )
    assert "UNKNOWN_TOUR" in codes(match_record(tour=None))
    assert "INCONSISTENT_STATS" in codes(
        match_record(
            stats=(ServeReturnCounts(serve_points=50), ServeReturnCounts(return_points=49))
        )
    )
    flags = {issue.code for issue in validate_match(match_record(best_of="UNKNOWN"))}
    assert "UNKNOWN_FORMAT" in flags
    assert codes(match_record(best_of="UNKNOWN")) == set()
    flags = {issue.code for issue in validate_match(match_record(draw_type=DrawType.DOUBLES))}
    assert "UNSUPPORTED_DRAW" in flags


def test_backfill_builds_canonical_entities_with_lineage_and_coverage(tmp_path):
    state = world(tmp_path)
    report = state.run()
    assert report.accepted == {
        "sports-match": 4,
        "sports-player": 7,
        "sports-ranking": 2,
        "sports-tournament": 2,
    }
    assert report.review_required == {"sports-match": 1}
    assert report.report_sha256 is not None
    (review,) = state.store.reviews(ReviewState.OPEN)
    assert review.kind == ReviewKind.RECORD and review.source_key == "m-5"
    assert "SCORE_WINNER_MISMATCH" in review.reasons

    m2 = state.store.match(state.match_id("m-2"))
    djuric, ruiz = state.player_id("p-djuric"), state.player_id("p-ruiz")
    assert set(m2.player_ids) == {djuric, ruiz}
    (result,) = state.store.results(m2.match_id)
    assert result.winner_id == djuric
    swapped = m2.player_ids[0] != djuric
    first_set = result.sets[0].games
    assert first_set == ((4, 6) if swapped else (6, 4))
    assert result.availability.observed_at == FIRST_OBSERVATION + timedelta(seconds=1)
    assert result.availability.ingested_at > result.availability.observed_at
    assert result.availability.effective_at == datetime(2026, 9, 15, 14, 30, tzinfo=UTC)

    m4 = state.match_id("m-4")
    novak, nunez = state.player_id("p-novak"), state.player_id("p-nunez")
    assert state.store.stats(m4, novak)[0].counts.serve_points == 40
    assert state.store.stats(m4, novak)[0].counts.first_serves_in is None
    assert state.store.stats(m4, nunez) == ()

    by_tour = {row.tour: row for row in report.coverage}
    assert by_tour["atp"].matches == 4 and by_tour["atp"].unresolved == 1
    assert by_tour["atp"].with_both_stats == 1
    assert by_tour["wta"].missing_stats == 1
    assert report.facts_with_archive_evidence == 1
    assert report.facts_without_archive_evidence == 6

    (ranking,) = state.store.rankings(djuric)
    early = datetime(2026, 9, 15, tzinfo=UTC)
    assert ranking.availability.available_by(early, allow_archived=True) == (
        AvailabilityClass.ARCHIVED
    )
    (ruiz_rank,) = state.store.rankings(ruiz)
    assert ruiz_rank.availability.available_by(early, allow_archived=True) is None


def test_reprocessing_is_idempotent_and_checkpoint_resumes(tmp_path):
    state = world(tmp_path)
    partial = state.run(batch_size=5, max_batches=1)
    assert (partial.start_position, partial.end_position) == (0, 5)
    rest = state.run(batch_size=5)
    assert (rest.start_position, rest.end_position) == (5, len(state.facts()))
    snapshot = (
        len(state.store.players()),
        len(state.store.matches()),
        sum(len(state.store.results(m.match_id)) for m in state.store.matches()),
        len(state.store.audit_log()),
    )
    again = state.run(checkpoint="sys-04-rerun")
    assert again.changed == 0
    assert set(again.review_ids) == set(partial.review_ids) | set(rest.review_ids)
    assert snapshot == (
        len(state.store.players()),
        len(state.store.matches()),
        sum(len(state.store.results(m.match_id)) for m in state.store.matches()),
        len(state.store.audit_log()),
    )


def test_correction_appends_a_version_and_preserves_the_known_result(tmp_path):
    state = world(tmp_path)
    state.run()
    m3 = state.match_id("m-3")
    archive_payload(state.service, state.clock, "payload-2.json", SECOND_OBSERVATION)
    state.clock.instant = SECOND_OBSERVATION + timedelta(hours=1)
    state.run()
    m2 = state.match_id("m-2")
    first, second = state.store.results(m2)
    assert first.version == 1 and second.version == 2 and second.corrects_version == 1
    assert {max(first.sets[2].tiebreak_points), max(second.sets[2].tiebreak_points)} == {7, 8}
    assert first.availability.observed_at < second.availability.observed_at
    # Reversed participant order from the same source maps to the same match.
    assert state.match_id("m-3") == m3
    assert len(state.store.matches()) == 4
    # An unchanged player record does not create a new alias version.
    assert len(state.store.player_alias_history("synthetic-sports", "p-djuric")) == 1


def test_manual_review_is_human_only_audited_and_remap_lists_revalidation(tmp_path):
    state = world(tmp_path)
    state.run()
    outcome = state.warehouse.ingest_player(
        SourcePlayerRecord(
            source_id="synthetic-stats",
            source_player_id="s-10",
            full_name="Kowalski",
            tour="ATP",
        )
    )
    assert not outcome.accepted and outcome.review_id is not None
    with pytest.raises(PermissionError):
        state.warehouse.approve_player(
            outcome.review_id,
            reviewer="agent:identity-review",
            reason="looks right",
            player_id=state.player_id("p-kowalski-j"),
        )
    with pytest.raises(PermissionError):
        state.warehouse.approve_player(
            outcome.review_id, reviewer="system:auto", reason="x", create=True
        )
    alias = state.warehouse.approve_player(
        outcome.review_id,
        reviewer="fixture-reviewer",
        reason="Draw sheet evidence fixture-1 identifies Jan",
        player_id=state.player_id("p-kowalski-j"),
    )
    assert alias.decision == ResolutionDecision.REVIEW_REQUIRED
    assert alias.reviewed_by == "fixture-reviewer"
    assert state.store.review(outcome.review_id).state == ReviewState.APPROVED
    with pytest.raises(ValueError, match="already"):
        state.warehouse.reject_review(outcome.review_id, reviewer="fixture-reviewer", reason="x")

    state.clock.advance(timedelta(minutes=1))
    corrected, affected = state.warehouse.remap_player_alias(
        "synthetic-stats",
        "s-10",
        state.player_id("p-kowalski-p"),
        reviewer="fixture-reviewer",
        reason="Correction: evidence fixture-2 identifies Piotr",
    )
    history = state.store.player_alias_history("synthetic-stats", "s-10")
    assert [item.version for item in history] == [1, 2]
    assert history[0].player_id == state.player_id("p-kowalski-j")
    assert corrected.supersedes == history[0].alias_id
    assert set(affected) >= {state.match_id("m-1"), state.match_id("m-3")}
    old = state.store.player_alias("synthetic-stats", "s-10", as_of=history[0].recorded_at)
    assert old is not None and old.version == 1
    assert old.player_id == state.player_id("p-kowalski-j")
    actions = [entry.action for entry in state.store.audit_log()]
    assert "review.approved" in actions and "alias.remapped" in actions


def test_unresolved_participant_blocks_match(tmp_path):
    state = world(tmp_path)
    state.run()
    outcome = state.warehouse.ingest_match(
        match_record(
            source_match_id="m-x",
            participant_ids=("p-djuric", "p-unknown"),
            winner_id="p-djuric",
        ),
        state.store.results(state.match_id("m-2"))[0].availability,
    )
    assert not outcome.accepted
    assert state.store.review(outcome.review_id).kind == ReviewKind.MATCH
    assert state.store.match_alias("synthetic-sports", "m-x") is None


def test_statuses_include_walkover_without_score(tmp_path):
    state = world(tmp_path)
    state.run()
    m1 = state.match_id("m-1")
    (status,) = state.store.statuses(m1)
    assert status.status == MatchStatus.WALKOVER
    (result,) = state.store.results(m1)
    assert result.sets == () and result.winner_id == state.player_id("p-kowalski-j")
    assert state.store.match(m1).best_of == BestOf.THREE
