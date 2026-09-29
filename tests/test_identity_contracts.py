from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from tennis_engine.common.ids import stable_id
from tennis_engine.contracts.domain import Tour
from tennis_engine.normalization.contracts import (
    BestOf,
    DrawStage,
    DrawType,
    EvidenceKind,
    Match,
    MatchStatus,
    PlayerAlias,
    PlayerResolution,
    ResolutionAction,
    ResolutionDecision,
    ResolutionPolicy,
    Round,
    ServeReturnCounts,
)

NOW = datetime(2026, 9, 19, 10, tzinfo=UTC)


def ordered_pair() -> tuple[UUID, UUID]:
    first, second = stable_id("player", "a"), stable_id("player", "b")
    return (first, second) if str(first) < str(second) else (second, first)


def match(player_ids: tuple[UUID, UUID]) -> Match:
    return Match(
        match_id=stable_id("match", "m"),
        edition_id=stable_id("edition", "e"),
        tour=Tour.ATP,
        draw_type=DrawType.SINGLES,
        draw_stage=DrawStage.MAIN,
        round=Round.R32,
        best_of=BestOf.THREE,
        player_ids=player_ids,
        created_at=NOW,
    )


def test_match_requires_distinct_players_in_canonical_orientation():
    first, second = ordered_pair()
    assert match((first, second)).player_ids == (first, second)
    with pytest.raises(ValidationError, match="ascending"):
        match((second, first))
    with pytest.raises(ValidationError, match="distinct"):
        match((first, first))


def test_counts_keep_missing_distinct_from_zero_and_check_denominators():
    missing = ServeReturnCounts()
    assert missing.serve_points is None
    zero = ServeReturnCounts(serve_points=0, serve_points_won=0)
    assert zero.serve_points == 0
    ServeReturnCounts(
        serve_points=80,
        serve_points_won=52,
        first_serves_in=50,
        first_serve_points_won=38,
        second_serve_points_won=14,
    )
    with pytest.raises(ValidationError, match="denominator"):
        ServeReturnCounts(serve_points=10, serve_points_won=11)
    with pytest.raises(ValidationError, match="second-serve"):
        ServeReturnCounts(serve_points=10, first_serves_in=8, second_serve_points_won=3)
    with pytest.raises(ValidationError, match="sum"):
        ServeReturnCounts(serve_points_won=10, first_serve_points_won=6, second_serve_points_won=3)
    with pytest.raises(ValidationError):
        ServeReturnCounts(aces=-1)


def test_rejected_or_unreviewed_decisions_cannot_become_aliases():
    common = {
        "alias_id": stable_id("alias", "1"),
        "player_id": stable_id("player", "a"),
        "source_id": "synthetic-sports",
        "source_player_id": "p-1",
        "source_name": "Fixture Player",
        "version": 1,
        "active": True,
        "evidence": (EvidenceKind.STABLE_SOURCE_ID,),
        "recorded_at": NOW,
    }
    PlayerAlias(**common, decision=ResolutionDecision.AUTO_ACCEPT)
    PlayerAlias(**common, decision=ResolutionDecision.REVIEW_REQUIRED, reviewed_by="reviewer")
    with pytest.raises(ValidationError, match="rejected"):
        PlayerAlias(**common, decision=ResolutionDecision.REJECT)
    with pytest.raises(ValidationError, match="reviewer"):
        PlayerAlias(**common, decision=ResolutionDecision.REVIEW_REQUIRED)


def test_only_accepted_resolutions_name_a_player():
    common = {
        "source_id": "synthetic-sports",
        "source_player_id": "p-1",
        "source_name": "Fixture Player",
        "candidates": (),
        "reasons": (),
        "policy_version": "identity-v1",
        "resolved_at": NOW,
    }
    with pytest.raises(ValidationError):
        PlayerResolution(
            **common,
            decision=ResolutionDecision.REVIEW_REQUIRED,
            action=ResolutionAction.NONE,
            player_id=stable_id("player", "a"),
        )
    with pytest.raises(ValidationError):
        PlayerResolution(
            **common,
            decision=ResolutionDecision.AUTO_ACCEPT,
            action=ResolutionAction.NONE,
            player_id=stable_id("player", "a"),
        )


def test_policy_rejects_weights_that_let_names_alone_reach_review():
    with pytest.raises(ValidationError, match="Name evidence alone"):
        ResolutionPolicy(
            version="unsafe",
            weights={EvidenceKind.NAME_EXACT: Decimal("0.95")},
        )
    with pytest.raises(ValidationError, match="below"):
        ResolutionPolicy(
            version="unsafe",
            review_threshold=Decimal("0.995"),
            weights={},
        )


def test_status_winner_semantics_are_explicit():
    assert MatchStatus.RETIRED.has_winner
    assert not MatchStatus.CANCELLED.has_winner
    assert BestOf.UNKNOWN.sets_to_win is None
    assert BestOf.THREE.sets_to_win == 2
