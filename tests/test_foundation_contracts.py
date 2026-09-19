from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tennis_engine.common.contracts import Money, VersionRef
from tennis_engine.common.ids import stable_id
from tennis_engine.contracts.domain import (
    AuditLineage,
    CanonicalMatch,
    GateResult,
    PayoutResult,
    ProbabilityOutput,
    Recommendation,
)

NOW = datetime(2026, 9, 19, 10, tzinfo=UTC)
HASH = "a" * 64


def version(component: str) -> VersionRef:
    return VersionRef(component=component, version="v1", sha256=HASH)


def lineage() -> AuditLineage:
    return AuditLineage(
        raw_hashes=(HASH,),
        parser=version("parser"),
        dataset=version("dataset"),
        feature_set=version("features"),
        model=version("model"),
        policies=(version("policy"),),
        code_revision="abcdef1",
    )


def test_canonical_match_rejects_naive_time_and_duplicate_players():
    player = stable_id("player", "one")
    valid = {
        "match_id": stable_id("match", "one"),
        "tournament_id": stable_id("tournament", "one"),
        "player_ids": (player, stable_id("player", "two")),
        "scheduled_start": NOW,
        "tour": "ATP",
        "status": "SCHEDULED",
    }
    assert CanonicalMatch.model_validate(valid).scheduled_start.tzinfo is UTC
    with pytest.raises(ValidationError, match="Timezone-aware"):
        CanonicalMatch.model_validate(valid | {"scheduled_start": NOW.replace(tzinfo=None)})
    with pytest.raises(ValidationError, match="distinct"):
        CanonicalMatch.model_validate(valid | {"player_ids": (player, player)})


def test_probability_output_requires_exact_binary_distribution_and_no_floats():
    players = (stable_id("player", "one"), stable_id("player", "two"))
    data = {
        "match_id": stable_id("match", "one"),
        "generated_at": NOW,
        "player_probabilities": {players[0]: "0.60", players[1]: "0.40"},
        "conservative_probability": "0.55",
        "model": version("model"),
        "feature_vector_sha256": HASH,
    }
    result = ProbabilityOutput.model_validate(data)
    assert result.player_probabilities[players[0]] == Decimal("0.60")
    with pytest.raises(ValidationError, match="sum exactly"):
        ProbabilityOutput.model_validate(
            data | {"player_probabilities": {players[0]: "0.60", players[1]: "0.39"}}
        )
    with pytest.raises(ValidationError, match="never a float"):
        ProbabilityOutput.model_validate(data | {"conservative_probability": 0.55})


def test_payout_supports_negative_expected_value_but_not_negative_cash_flows():
    payout = PayoutResult(
        stake=Money(amount="2.00"),
        cash_return_if_win=Money(amount="3.50"),
        cash_return_if_loss=Money(amount="0.00"),
        expected_value=Money(amount="-0.10"),
        break_even_probability="0.5714",
        policy=version("payout"),
    )
    assert payout.expected_value.amount == Decimal("-0.10")
    with pytest.raises(ValidationError, match="cannot be negative"):
        PayoutResult.model_validate(payout.model_dump() | {"stake": {"amount": "-1.00"}})


def test_recommendation_enforces_gate_and_stake_invariants():
    base = {
        "recommendation_id": stable_id("recommendation", "one"),
        "match_id": stable_id("match", "one"),
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=1),
        "stake": Money(amount="0.00"),
        "central_probability": "0.55",
        "conservative_probability": "0.51",
        "expected_value": Money(amount="0.00"),
        "gates": (GateResult(gate="identity", passed=False, reason="REVIEW_REQUIRED"),),
        "lineage": lineage(),
    }
    assert Recommendation(status="NO_BET", **base).status == "NO_BET"
    with pytest.raises(ValidationError, match="zero stake"):
        Recommendation(status="WATCH", **(base | {"stake": Money(amount="1.00")}))
    with pytest.raises(ValidationError, match="every gate"):
        Recommendation(status="BET", **(base | {"stake": Money(amount="1.00")}))
    with pytest.raises(ValidationError, match="failed gate"):
        Recommendation(
            status="NO_BET",
            **(base | {"gates": (GateResult(gate="identity", passed=True),)}),
        )
