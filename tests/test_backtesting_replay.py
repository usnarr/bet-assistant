"""F13.2 to F13.5 execution replay on synthetic quote history, rules and results."""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pricing_support import decision_policy, responsible
from settlement_support import BET_TIME, MATCH_ID, PLAYER_A, PLAYER_B, allowed, registry

from tennis_engine.backtesting.replay import (
    CLOSING_POLICY,
    AcceptedStakeCap,
    ExecutionScenario,
    ExecutionStatus,
    ReplayCase,
    ReplayServices,
    replay,
)
from tennis_engine.common.contracts import Money, VersionRef
from tennis_engine.common.ids import stable_id
from tennis_engine.contracts.domain import AuditLineage, RecommendationStatus
from tennis_engine.features.quality import QualityReport
from tennis_engine.governance.contracts import Decision
from tennis_engine.governance.service import PolicyLookup
from tennis_engine.ingestion.bookmakers.contracts import EventState, QuoteState, RawQuote
from tennis_engine.ingestion.bookmakers.history import MemoryHistoryStore, QuoteHistory
from tennis_engine.ingestion.bookmakers.quotes import ActionabilityPolicy, QuoteObservation
from tennis_engine.normalization.contracts import (
    MatchResolution,
    PlayerResolution,
    ResolutionAction,
    ResolutionDecision,
)
from tennis_engine.pricing.decision import ModelAssessment
from tennis_engine.pricing.payout import PayoutRequest, resolve_payout
from tennis_engine.pricing.risk import StakeRules
from tennis_engine.settlement.engine import MatchOutcome, MatchStatus, SettlementStatus
from tennis_engine.settlement.rules import SettlementRule

D = Decimal
SHA = "d" * 64
BOOK = "synthetic-book"
KEY = (BOOK, "e-1", "m-1", "s-1")
T0 = BET_TIME
START = T0 + timedelta(hours=3)
CUTOFF = T0 + timedelta(seconds=65)
REGISTRY = registry()
RULES = StakeRules(minimum=D("2.00"), increment=D("0.01"), maximum=D("5000.00"))


def ref(name):
    return VersionRef(component=name, version=f"{name}-v1", sha256=SHA)


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


def player(player_id, at):
    return PlayerResolution(
        source_id=BOOK,
        source_player_id=str(player_id),
        source_name="name",
        decision=ResolutionDecision.AUTO_ACCEPT,
        action=ResolutionAction.LINK_EXISTING,
        player_id=player_id,
        candidates=(),
        reasons=(),
        policy_version="fixture-policy",
        resolved_at=at,
    )


def resolution(at):
    return MatchResolution(
        source_id=BOOK,
        source_event_id="e-1",
        decision=ResolutionDecision.AUTO_ACCEPT,
        action=ResolutionAction.LINK_EXISTING,
        match_id=MATCH_ID,
        swapped=False,
        participants=(player(PLAYER_A, at), player(PLAYER_B, at)),
        candidates=(),
        reasons=(),
        policy_version="fixture-policy",
        resolved_at=at,
    )


def observation(at, odds, state=QuoteState.OPEN):
    return QuoteObservation(
        quote=RawQuote(
            bookmaker=BOOK,
            source_event_id="e-1",
            source_market_id="m-1",
            source_selection_id="s-1",
            market_label="Winner",
            market="TENNIS_MATCH_WINNER",
            selection_label="Player A",
            participant_index=0,
            line=None,
            decimal_odds=D(odds),
            state=state,
        ),
        observed_at=at,
        parser_version="synthetic-book-v1",
        raw_content_sha256=SHA,
        scheduled_start=START,
        event_state=EventState.PRE_MATCH,
    )


def history(prices, *, mapped_at=T0 - timedelta(hours=1)):
    """Observations every 30 seconds from T0. `prices` maps a second offset to odds."""
    store = MemoryHistoryStore()
    store.add_mapping(UUID(int=1), BOOK, resolution(mapped_at))
    odds = "2.30"
    for second in range(0, 600, 30):
        odds = prices.get(second, odds)
        at = T0 + timedelta(seconds=second)
        store.add_observation(stable_id("test-observation", at.isoformat()), observation(at, odds))
    # The last pre-start quote, used only for CLV.
    last = START - timedelta(minutes=1)
    store.add_observation(UUID(int=2), observation(last, "2.10"))
    return QuoteHistory(store)


def outcome(winner=PLAYER_A, observed=START + timedelta(hours=2), evidence="raw:result-1"):
    return MatchOutcome(
        match_id=MATCH_ID,
        player_ids=(PLAYER_A, PLAYER_B),
        status=MatchStatus.COMPLETED,
        scheduled_start=START,
        completed_at=START + timedelta(hours=2),
        winner_player_id=winner,
        completed_sets=2,
        sets=(),
        postponed=False,
        venue_changed=False,
        surface_changed=False,
        format_changed=False,
        wrong_listing=False,
        palpable_error=False,
        disputed=False,
        observed_at=observed,
        evidence_ids=(evidence,),
    )


def payout(quote, at):
    def resolve(stake):
        return resolve_payout(
            PayoutRequest(
                bookmaker=quote.bookmaker,
                selection_key=f"{quote.match_id}:{quote.selection_player_id}",
                decimal_odds=quote.decimal_odds,
                stake=stake,
                account_scope="shadow",
                bet_time=at,
                known_at=at,
            ),
            allowed(),
            REGISTRY,
        )

    return resolve


def services(prices=None, *, caps=None, outcomes=None, origin="PROSPECTIVE", **overrides):
    if caps is None:
        caps = (
            AcceptedStakeCap(
                bookmaker=BOOK,
                match_id=None,
                maximum_stake=D("500.00"),
                observed_at=T0 - timedelta(days=1),
                evidence_id="cap:observed-1",
            ),
        )
    values = {
        "history": history(prices or {}),
        "actionability_policy": ActionabilityPolicy(),
        "quote_origin": origin,
        "settlement_rule": lambda book, at: REGISTRY.lookup(
            SettlementRule, "synthetic-book-settlement-v1", effective_at=at, known_at=at
        ),
        "payout": payout,
        "stake_rules": lambda book: RULES,
        "publication": lambda at: Decision(allowed=True, version="fixture", revision=1),
        "responsible_use": lambda at: PolicyLookup(
            Decision(allowed=True, version="synthetic-responsible-v1", revision=1), responsible()
        ),
        "decision_policy": lambda at: decision_policy(),
        "registry": REGISTRY,
        "outcomes": lambda match_id: outcomes if outcomes is not None else (outcome(),),
        "stake_caps": caps,
    }
    return ReplayServices(**(values | overrides))


def model(generated_at=CUTOFF - timedelta(seconds=5)):
    return ModelAssessment(
        model=ref("synthetic-model"),
        probability=D("0.60"),
        conservative_probability=D("0.56"),
        semantics="SETTLED_WIN",
        in_supported_domain=True,
        calibrated=True,
        disagreement=D("0.01"),
        generated_at=generated_at,
        feature_vector_sha256=SHA,
    )


def case(case_id="c-1", decided_at=CUTOFF, **overrides):
    values = {
        "case_id": case_id,
        "block": "2026-w38",
        "decided_at": decided_at,
        "quote_key": KEY,
        "model": model(),
        "consensus_probability": D("0.59"),
        "quality": USABLE,
        "lineage": LINEAGE,
    }
    return ReplayCase.model_validate(values | overrides)


BASE = ExecutionScenario(name="base", latency_seconds=20)


def run(cases=None, service=None, scenario=BASE):
    return replay(
        cases or [case()],
        service or services(),
        scenario,
        run_id="replay-test",
        opening_balance=Money(amount=D("1000.00")),
    )


def test_a_bet_is_struck_settled_and_execution_grade():
    result = run()
    (outcome_,) = result.cases
    assert outcome_.decision.status == RecommendationStatus.BET
    assert outcome_.status == ExecutionStatus.STRUCK
    # Latency 20 s: the first observation at or after 85 s is at 90 s.
    assert outcome_.executed_at == T0 + timedelta(seconds=90)
    assert outcome_.cap_evidence_id == "cap:observed-1"
    assert result.execution_grade and result.assumptions == ()
    (settled,) = result.settlements
    assert settled.status == SettlementStatus.WON
    (bet,) = result.bets
    assert bet.cash_return == outcome_.bet.cash_return_if_win.amount
    assert bet.closing_odds == D("2.10") and bet.closing_policy == CLOSING_POLICY
    assert result.reconciliation.balanced and result.reconciliation.open_bets == 0
    summary = result.summary(starting_bankroll=D("1000.00"), draws=200, seed=7, level=D("0.9"))
    assert summary.execution_grade and summary.bets == 1
    assert summary.profit == bet.cash_return - bet.stake > 0
    assert run() == result  # Deterministic.


def test_the_cutoff_state_ignores_later_quotes_and_mappings():
    at = T0 - timedelta(seconds=30)
    early = case("early", decided_at=at, model=model(generated_at=at))
    unmapped = services(history=history({}, mapped_at=CUTOFF + timedelta(seconds=1)))
    first = run([early]).cases[0]
    assert first.decision.status == RecommendationStatus.NO_BET
    assert "QUOTE_MISSING" in dict(
        (gate.gate.value, gate.detail) for gate in first.decision.gates
    ).get("identity_resolved", ())
    second = run(service=unmapped).cases[0]
    assert second.decision.status == RecommendationStatus.NO_BET
    assert second.status == ExecutionStatus.NOT_BET


def test_a_price_drop_during_latency_cancels_with_the_same_policy():
    result = run(service=services({90: "1.50"}))
    (outcome_,) = result.cases
    assert outcome_.decision.status == RecommendationStatus.BET
    assert outcome_.status == ExecutionStatus.CANCELLED
    assert outcome_.execution is not None and outcome_.execution.decimal_odds == D("1.50")
    assert "EXECUTION:central_ev_positive" in outcome_.reasons
    assert result.bets == () and result.reconciliation.balanced


def test_no_quote_after_the_action_time_cancels():
    late = case("late", decided_at=T0 + timedelta(seconds=575))
    # The only later observations are after the scheduled start.
    delay = BASE.model_copy(update={"latency_seconds": 4 * 3600})
    outcome_ = run([late], scenario=delay).cases[0]
    assert outcome_.status == ExecutionStatus.CANCELLED
    assert outcome_.reasons == ("NO_QUOTE_AT_ACTION_TIME",)


def test_observed_caps_resize_or_reject_the_stake():
    def cap(amount):
        return (
            AcceptedStakeCap(
                bookmaker=BOOK,
                match_id=MATCH_ID,
                maximum_stake=D(amount),
                observed_at=T0,
                evidence_id=f"cap:{amount}",
            ),
        )

    resized = run(service=services(caps=cap("10.00"))).cases[0]
    assert resized.status == ExecutionStatus.STRUCK
    assert resized.decision.stake.amount > D("10.00") >= resized.bet.stake.amount
    rejected = run(service=services(caps=cap("0.00"))).cases[0]
    assert rejected.status == ExecutionStatus.CANCELLED
    assert "EXECUTION:risk_budget_available" in rejected.reasons
    assert rejected.execution.capacity.limiting_cap == "bookmaker_maximum_stake"


def test_missing_caps_and_stress_settings_are_named_assumptions():
    uncapped = run(service=services(caps=()))
    assert not uncapped.execution_grade
    assert uncapped.assumptions == ("1 bets: no observed accepted-stake cap; full stake accepted",)
    summary = uncapped.summary(starting_bankroll=D("1000.00"), draws=50, seed=1, level=D("0.9"))
    assert not summary.execution_grade and summary.assumptions == uncapped.assumptions

    haircut = run(scenario=BASE.model_copy(update={"odds_haircut": D("0.10")}))
    assert haircut.cases[0].bet.decimal_odds == D("2.17")
    assert haircut.assumptions == ("odds haircut 0.10",)
    suspended = run(scenario=BASE.model_copy(update={"suspension_rate": D("1")}))
    assert suspended.cases[0].reasons == ("SCENARIO_SUSPENDED",)
    reduced = run(
        service=services(caps=()),
        scenario=BASE.model_copy(update={"assumed_cap": D("20.00"), "capacity_fraction": D("0.5")}),
    )
    assert reduced.cases[0].bet.stake.amount <= D("10.00")
    assert reduced.assumptions == (
        "capacity fraction 0.5",
        "1 bets: assumed accepted-stake cap",
    )
    reconstructed = run(service=services(origin="RECONSTRUCTED"))
    assert "quote history is reconstructed, not observed" in reconstructed.assumptions
    research = run([case(research_only=True)])
    assert research.assumptions == ("1 cases use research-only snapshots",)


def test_a_corrected_result_is_a_ledger_correction():
    corrected = (
        outcome(),
        outcome(winner=PLAYER_B, observed=START + timedelta(hours=5), evidence="raw:result-2"),
    )
    result = run(service=services(outcomes=corrected))
    assert [item.status for item in result.settlements] == [
        SettlementStatus.WON,
        SettlementStatus.LOST,
    ]
    (bet,) = result.bets
    assert bet.cash_return == 0 and bet.settled_at == START + timedelta(hours=5)
    assert result.reconciliation.balanced
    assert result.reconciliation.closing_balance.amount == D("1000.00") - bet.stake


def test_exposure_carries_between_cases_and_settlements_free_it():
    second = case("c-2", decided_at=CUTOFF + timedelta(seconds=60))
    result = run([case(), second])
    first, other = result.cases
    assert first.status == other.status == ExecutionStatus.STRUCK
    # The event cap is 50.00 across both bets on the same match.
    assert first.bet.stake.amount + other.bet.stake.amount <= D("50.00")
    assert other.decision.capacity.maximum_stake < first.decision.capacity.maximum_stake


def test_invalid_cases_are_rejected():
    with pytest.raises(ValueError, match="after its cutoff"):
        run([case(model=model(generated_at=CUTOFF + timedelta(seconds=1)))])
    with pytest.raises(ValueError, match="unique"):
        run([case(), case()])
