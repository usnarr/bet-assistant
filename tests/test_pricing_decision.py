"""SYS-10 decision matrix: every hard gate, WATCH/NO_BET mapping and stake boundaries."""

from dataclasses import replace
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pricing_support import decision_policy, exposure, responsible
from settlement_support import BET_TIME, MATCH_ID, PLAYER_A, PLAYER_B, allowed, registry

from tennis_engine.common.contracts import VersionRef
from tennis_engine.contracts.domain import AuditLineage, RecommendationStatus
from tennis_engine.features.quality import QualityReport
from tennis_engine.governance.contracts import Decision
from tennis_engine.governance.service import PolicyLookup
from tennis_engine.ingestion.bookmakers.contracts import CanonicalQuote, QuoteState
from tennis_engine.ingestion.bookmakers.quotes import (
    Actionability,
    ActionabilityReason,
)
from tennis_engine.normalization.contracts import (
    MatchResolution,
    PlayerResolution,
    ResolutionAction,
    ResolutionDecision,
)
from tennis_engine.pricing.decision import DecisionInputs, Gate, ModelAssessment, decide
from tennis_engine.pricing.payout import PayoutRequest, resolve_payout
from tennis_engine.pricing.risk import DecisionPolicy, StakeRules
from tennis_engine.settlement.rules import SettlementRule

D = Decimal
AT = BET_TIME
SHA = "c" * 64
RULES = StakeRules(minimum=D("2.00"), increment=D("0.01"), maximum=D("5000.00"))
REGISTRY = registry()


def ref(name):
    return VersionRef(component=name, version=f"{name}-v1", sha256=SHA)


def quote(odds="2.30"):
    return CanonicalQuote(
        quote_id=UUID(int=77),
        bookmaker="synthetic-book",
        match_id=MATCH_ID,
        market="TENNIS_MATCH_WINNER",
        selection_player_id=PLAYER_A,
        decimal_odds=D(odds),
        state=QuoteState.OPEN,
        source_event_id="e-1",
        source_market_id="m-1",
        source_selection_id="s-1",
        source_order_swapped=False,
        scheduled_start=AT + timedelta(hours=3),
        promotion_marker=None,
        observed_at=AT - timedelta(seconds=10),
        parser_version="synthetic-book-v1",
        raw_content_sha256=SHA,
        resolution_policy_version="fixture-policy",
    )


def player(player_id):
    return PlayerResolution(
        source_id="synthetic-book",
        source_player_id=str(player_id),
        source_name="name",
        decision=ResolutionDecision.AUTO_ACCEPT,
        action=ResolutionAction.LINK_EXISTING,
        player_id=player_id,
        candidates=(),
        reasons=(),
        policy_version="fixture-policy",
        resolved_at=AT,
    )


RESOLVED = MatchResolution(
    source_id="synthetic-book",
    source_event_id="e-1",
    decision=ResolutionDecision.AUTO_ACCEPT,
    action=ResolutionAction.LINK_EXISTING,
    match_id=MATCH_ID,
    swapped=False,
    participants=(player(PLAYER_A), player(PLAYER_B)),
    candidates=(),
    reasons=(),
    policy_version="fixture-policy",
    resolved_at=AT,
)


def fresh(**overrides):
    return Actionability.model_validate(
        {
            "actionable": True,
            "reasons": (),
            "policy_version": "quote-actionability-proposed-v1",
            "evaluated_at": AT,
            "observation": None,
            "confirmations": 2,
            "expires_at": AT + timedelta(seconds=40),
        }
        | overrides
    )


def payout_for(odds="2.30", payout_policy=None):
    def resolve(stake):
        return resolve_payout(
            PayoutRequest(
                bookmaker="synthetic-book",
                selection_key="match-1:player-a",
                decimal_odds=D(odds),
                stake=stake,
                account_scope="shadow",
                bet_time=AT,
                known_at=AT,
            ),
            payout_policy or allowed(),
            REGISTRY,
        )

    return resolve


def model(p="0.60", low="0.56", **overrides):
    return ModelAssessment.model_validate(
        {
            "model": ref("synthetic-model"),
            "probability": D(p),
            "conservative_probability": D(low),
            "semantics": "SETTLED_WIN",
            "in_supported_domain": True,
            "calibrated": True,
            "disagreement": D("0.01"),
            "generated_at": AT - timedelta(seconds=5),
            "feature_vector_sha256": SHA,
        }
        | overrides
    )


LINEAGE = AuditLineage(
    raw_hashes=(SHA,),
    parser=ref("parser"),
    dataset=ref("dataset"),
    feature_set=ref("features"),
    model=ref("synthetic-model"),
    policies=(ref("decision-policy"),),
    code_revision="abcdef1",
)
USABLE = QualityReport(components=(), usable=True, soft_score=D("1"), hard_failures=())


def inputs(**overrides):
    base = DecisionInputs(
        decision_key="decision-1",
        decided_at=AT,
        ledger_id="shadow",
        quote=quote(),
        event_resolution=RESOLVED,
        actionability=fresh(),
        settlement_rule=REGISTRY.lookup(
            SettlementRule, "synthetic-book-settlement-v1", effective_at=AT, known_at=AT
        ),
        payout=payout_for(),
        stake_rules=RULES,
        quality=USABLE,
        model=model(),
        consensus_probability=D("0.59"),
        publication=Decision(allowed=True, version="fixture", revision=1),
        responsible_use=PolicyLookup(
            Decision(allowed=True, version="synthetic-responsible-v1", revision=1), responsible()
        ),
        decision_policy=decision_policy(),
        exposure=exposure(),
        lineage=LINEAGE,
    )
    return replace(base, **overrides)


def failed(record):
    return {gate.gate: gate.detail for gate in record.gates if not gate.passed}


def test_bet_passes_every_gate_and_sizes_at_its_own_payout():
    record = decide(inputs())
    assert record.status == RecommendationStatus.BET, failed(record)
    assert [gate.gate for gate in record.gates] == list(Gate)
    value = record.value
    assert value is not None and value.stake == record.stake
    # Stake is within the event cap and the conservative Kelly stake at its payout.
    assert D("2.00") <= record.stake.amount <= D("50.00")
    ratio = value.cash_return_if_win.amount / record.stake.amount
    kelly = (ratio * D("0.56") - 1) / (ratio - 1) * D("0.20") * D("1000.00")
    assert record.stake.amount <= kelly < record.stake.amount + D("0.03")
    assert value.conservative_expected_value > 0 and value.conservative_roi >= D("0.02")
    assert record.expires_at == AT + timedelta(seconds=40)
    assert record.virtual is True
    assert record.to_recommendation().status == RecommendationStatus.BET


def test_watch_has_central_edge_but_fails_conservative_value_with_zero_stake():
    record = decide(inputs(model=model("0.60", "0.47")))
    assert record.status == RecommendationStatus.WATCH, failed(record)
    assert record.stake.amount == 0
    assert set(failed(record)) == {Gate.CONSERVATIVE_EV, Gate.CONSERVATIVE_ROI, Gate.STAKE}
    assert record.to_recommendation().stake.amount == 0


def test_small_conservative_roi_below_threshold_is_watch():
    record = decide(inputs(model=model("0.60", "0.49")))
    assert record.status == RecommendationStatus.WATCH, failed(record)
    assert Gate.CONSERVATIVE_ROI in failed(record)
    assert Gate.CONSERVATIVE_EV not in failed(record)


def test_no_central_edge_is_no_bet():
    record = decide(inputs(model=model("0.45", "0.40")))
    assert record.status == RecommendationStatus.NO_BET
    assert Gate.CENTRAL_EV in failed(record)


def test_zero_edge_is_no_bet():
    # W at the minimum stake is 2.00 * 0.9 * 2.30 = 4.14; break-even = 2.00 / 4.14.
    break_even = (D("2.00") / D("4.14")).quantize(D("0.000001"), rounding=ROUND_DOWN)
    record = decide(inputs(model=model(str(break_even), str(break_even))))
    assert record.status == RecommendationStatus.NO_BET


HARD_CASES = {
    "draft decision policy": (
        {"decision_policy": decision_policy(state="PENDING_REVIEW")},
        Gate.DECISION_POLICY,
    ),
    "unresolved identity": (
        {
            "event_resolution": RESOLVED.model_copy(
                update={
                    "decision": ResolutionDecision.REVIEW_REQUIRED,
                    "action": ResolutionAction.NONE,
                    "match_id": None,
                    "swapped": None,
                    "reasons": ("NAME_ONLY",),
                }
            )
        },
        Gate.IDENTITY,
    ),
    "started event": (
        {
            "actionability": fresh(
                actionable=False,
                reasons=(ActionabilityReason.EVENT_STARTED,),
                expires_at=None,
            )
        },
        Gate.NOT_STARTED,
    ),
    "stale quote": (
        {
            "actionability": fresh(
                actionable=False, reasons=(ActionabilityReason.QUOTE_STALE,), expires_at=None
            )
        },
        Gate.QUOTE_FRESH,
    ),
    "expired actionability": (
        {"decided_at": AT + timedelta(seconds=41)},
        Gate.QUOTE_FRESH,
    ),
    "missing actionability": ({"actionability": None}, Gate.QUOTE_FRESH),
    "selection not in match": (
        {"quote": quote().model_copy(update={"selection_player_id": UUID(int=5)})},
        Gate.MARKET,
    ),
    "unreviewed settlement rule": (
        {
            "settlement_rule": REGISTRY.lookup(
                SettlementRule, "missing-rule", effective_at=AT, known_at=AT
            )
        },
        Gate.RULES,
    ),
    "unknown payout policy": (
        {
            "payout": payout_for(
                payout_policy=PolicyLookup(Decision.deny("PAYOUT_POLICY_NOT_APPROVED"))
            )
        },
        Gate.RULES,
    ),
    "kill switch at publication": (
        {"publication": Decision.deny("SOURCE_DISABLED")},
        Gate.RULES,
    ),
    "unusable quality": (
        {
            "quality": QualityReport(
                components=(
                    {"component": "IDENTITY", "status": "UNKNOWN", "hard": True, "score": "0"},
                ),
                usable=False,
                soft_score=D("0"),
                hard_failures=("identity",),
            )
        },
        Gate.QUALITY,
    ),
    "missing model": ({"model": None}, Gate.MODEL_DOMAIN),
    "out of domain": ({"model": model(in_supported_domain=False)}, Gate.MODEL_DOMAIN),
    "unknown domain": ({"model": model(in_supported_domain=None)}, Gate.MODEL_DOMAIN),
    "sporting win without void stress": (
        {"model": model(semantics="SPORTING_WIN")},
        Gate.MODEL_DOMAIN,
    ),
    "not calibrated": ({"model": model(calibrated=None)}, Gate.MODEL_CALIBRATED),
    "disagreement unknown": ({"model": model(disagreement=None)}, Gate.MODEL_DISAGREEMENT),
    "disagreement too high": (
        {"model": model(disagreement=D("0.20"))},
        Gate.MODEL_DISAGREEMENT,
    ),
    "large edge without consensus": ({"consensus_probability": None}, Gate.OUTLIER),
    "consensus disagrees": ({"consensus_probability": D("0.45")}, Gate.OUTLIER),
    "empty bankroll": ({"exposure": exposure("0.00")}, Gate.RISK_BUDGET),
    "event cap reached": (
        {"exposure": exposure(event_exposure="49.00")},
        Gate.RISK_BUDGET,
    ),
    "missing exposure": ({"exposure": None}, Gate.RISK_BUDGET),
    "cooling off": (
        {"responsible_use": PolicyLookup(Decision.deny("COOLING_OFF"))},
        Gate.RESPONSIBLE_USE,
    ),
}


@pytest.mark.parametrize("case", sorted(HARD_CASES))
def test_every_hard_gate_failure_is_no_bet_with_zero_stake(case):
    overrides, gate = HARD_CASES[case]
    record = decide(inputs(**overrides))
    assert record.status == RecommendationStatus.NO_BET
    assert gate in failed(record), failed(record)
    assert record.stake.amount == 0
    recommendation = record.to_recommendation()
    assert recommendation.stake.amount == 0
    assert any(not gate.passed for gate in recommendation.gates)


def test_every_failed_reason_is_recorded():
    record = decide(inputs(model=model(calibrated=False), exposure=exposure("0.00")))
    assert {Gate.MODEL_CALIBRATED, Gate.RISK_BUDGET} <= set(failed(record))
    assert all(gate.code is not None for gate in record.gates if not gate.passed)


def test_minimum_stake_conflict_abstains():
    # Conservative Kelly on a PLN 60 bankroll is below the PLN 2.00 minimum stake.
    record = decide(
        inputs(model=model("0.55", "0.50"), exposure=exposure("60.00"), consensus_probability=None)
    )
    assert record.status == RecommendationStatus.NO_BET, failed(record)
    assert set(failed(record)) == {Gate.STAKE}
    assert record.stake.amount == 0


def test_void_stress_policy_allows_sporting_win_probabilities_conservatively():
    stressed = decision_policy(void_stress_probability="0.02")
    record = decide(inputs(model=model(semantics="SPORTING_WIN"), decision_policy=stressed))
    assert record.status == RecommendationStatus.BET, failed(record)
    assert record.conservative_probability == D("0.56") * D("0.98")


def test_decisions_are_deterministic_and_keyed():
    first, second = decide(inputs()), decide(inputs())
    assert first == second
    assert decide(inputs(decision_key="decision-2")).decision_id != first.decision_id


def test_repository_decision_policy_is_a_draft_that_blocks_every_bet():
    draft = DecisionPolicy.model_validate_json(
        Path("configs/risk/decision-policy.json").read_text("utf-8")
    )
    record = decide(inputs(decision_policy=draft, decided_at=AT.replace(month=10)))
    assert record.status == RecommendationStatus.NO_BET
    assert Gate.DECISION_POLICY in failed(record)
