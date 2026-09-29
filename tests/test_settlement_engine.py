from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError
from settlement_support import BET_TIME, MATCH_ID, PLAYER_A, PLAYER_B, pln, registry

from tennis_engine.settlement.engine import (
    MatchOutcome,
    MatchStatus,
    PendingReason,
    SettlementContext,
    SettlementStatus,
    settle,
)
from tennis_engine.settlement.rules import RuleRegistry

BET_ID = UUID("00000000-0000-4000-8000-0000000000b1")
START = BET_TIME + timedelta(hours=2)
SETTLED = START + timedelta(hours=4)


def outcome(status=MatchStatus.COMPLETED, winner=PLAYER_A, sets=2, **overrides):
    return MatchOutcome.model_validate(
        {
            "match_id": MATCH_ID,
            "player_ids": (PLAYER_A, PLAYER_B),
            "status": status,
            "scheduled_start": START,
            "completed_at": START + timedelta(hours=2) if status == MatchStatus.COMPLETED else None,
            "winner_player_id": winner,
            "completed_sets": sets,
            "sets": [{"games": (6, 4)}, {"games": (6, 3)}][:sets],
            "postponed": False,
            "venue_changed": False,
            "surface_changed": False,
            "format_changed": False,
            "wrong_listing": False,
            "palpable_error": False,
            "disputed": False,
            "observed_at": SETTLED - timedelta(minutes=5),
            "evidence_ids": ["raw:result-1"],
        }
        | overrides
    )


def context(result=None, rule="synthetic-book-settlement-v1", **overrides):
    return SettlementContext.model_validate(
        {
            "bet_id": BET_ID,
            "bookmaker": "synthetic-book",
            "rule_version": rule,
            "match_id": MATCH_ID,
            "selection_player_id": PLAYER_A,
            "stake": pln("100.00"),
            "displayed_odds": Decimal("2.50"),
            "cash_return_if_win": pln("225.00"),
            "stake_tax": pln("10.00"),
            "winnings_tax_if_win": pln("0.00"),
            "bet_time": BET_TIME,
            "settled_at": SETTLED,
            "outcome": result or outcome(),
        }
        | overrides
    )


def run(result=None, rule="synthetic-book-settlement-v1", **overrides):
    return settle(context(result, rule, **overrides), registry())


def test_completed_match_settles_on_the_official_winner():
    won = run()
    assert won.status == SettlementStatus.WON
    assert won.cash_return == pln("225.00") and won.net_profit == pln("125.00")
    assert won.tax_amount == pln("10.00")
    assert won.applied_rules == ("synthetic-book-settlement-v1:completed:synthetic-rules-doc:s-1",)
    assert won.evidence == ("raw:result-1",)
    lost = run(outcome(winner=PLAYER_B))
    assert lost.status == SettlementStatus.LOST
    assert lost.cash_return == pln("0.00") and lost.net_profit == pln("-100.00")
    assert lost.tax_amount == pln("10.00")


@pytest.mark.parametrize(
    ("result", "status", "reason"),
    [
        # Retirement before one completed set voids; after it, the advancing player wins.
        (outcome(MatchStatus.RETIRED, sets=0), SettlementStatus.VOID, None),
        (outcome(MatchStatus.RETIRED, sets=1), SettlementStatus.WON, None),
        (outcome(MatchStatus.RETIRED, winner=PLAYER_B, sets=1), SettlementStatus.LOST, None),
        (
            outcome(MatchStatus.RETIRED, winner=None, sets=1),
            SettlementStatus.PENDING,
            PendingReason.WINNER_UNKNOWN,
        ),
        (outcome(MatchStatus.WALKOVER, sets=0), SettlementStatus.VOID, None),
        (outcome(MatchStatus.DISQUALIFIED, sets=0), SettlementStatus.WON, None),
        (outcome(MatchStatus.ABANDONED, winner=None, sets=1), SettlementStatus.VOID, None),
        (outcome(venue_changed=True), SettlementStatus.WON, None),
        (outcome(surface_changed=True), SettlementStatus.VOID, None),
        (outcome(format_changed=True), SettlementStatus.VOID, None),
        (outcome(wrong_listing=True), SettlementStatus.PENDING, PendingReason.MANUAL_REVIEW),
        (outcome(palpable_error=True), SettlementStatus.PENDING, PendingReason.MANUAL_REVIEW),
        (outcome(disputed=True), SettlementStatus.PENDING, PendingReason.RESULT_DISPUTED),
        (
            outcome(MatchStatus.NOT_STARTED, winner=None, sets=0),
            SettlementStatus.PENDING,
            PendingReason.MATCH_NOT_FINISHED,
        ),
        (
            outcome(MatchStatus.IN_PROGRESS, winner=None, sets=1),
            SettlementStatus.PENDING,
            PendingReason.MATCH_NOT_FINISHED,
        ),
        (
            outcome(MatchStatus.CANCELLED, winner=None, sets=0),
            SettlementStatus.PENDING,
            PendingReason.CANCELLATION_REVIEW,
        ),
    ],
)
def test_each_reviewed_rule_branch(result, status, reason):
    settled = run(result)
    assert settled.status == status
    assert settled.pending_reason == reason
    if status == SettlementStatus.VOID:
        assert settled.cash_return == pln("100.00") and settled.net_profit == pln("0.00")
    if status == SettlementStatus.PENDING:
        assert settled.cash_return is None and settled.net_profit is None


def test_postponement_window_decides_between_pending_void_and_result():
    postponed = outcome(MatchStatus.POSTPONED, winner=None, sets=0)
    assert run(postponed).pending_reason == PendingReason.POSTPONEMENT_WINDOW_OPEN
    late = START + timedelta(hours=49)
    assert run(postponed, settled_at=late).status == SettlementStatus.VOID
    resumed_in_time = outcome(postponed=True, completed_at=START + timedelta(hours=30))
    settled = run(resumed_in_time, settled_at=START + timedelta(hours=31))
    assert settled.status == SettlementStatus.WON
    assert "synthetic-book-settlement-v1:postponement:synthetic-rules-doc:s-6" in (
        settled.applied_rules
    )
    resumed_late = outcome(postponed=True, completed_at=START + timedelta(hours=50))
    assert run(resumed_late, settled_at=START + timedelta(hours=51)).status == (
        SettlementStatus.VOID
    )


def test_missing_rule_branches_stay_pending_and_rule_differences_are_respected():
    sparse = "synthetic-book-settlement-sparse-v1"
    walkover = run(outcome(MatchStatus.WALKOVER, sets=0), sparse)
    assert walkover.pending_reason == PendingReason.RULE_BRANCH_MISSING
    assert run(outcome(surface_changed=True), sparse).pending_reason == (
        PendingReason.RULE_BRANCH_MISSING
    )
    assert run(outcome(MatchStatus.POSTPONED, winner=None, sets=0), sparse).pending_reason == (
        PendingReason.RULE_BRANCH_MISSING
    )
    # This rule voids every retirement, even after completed sets.
    assert run(outcome(MatchStatus.RETIRED, sets=1), sparse).status == SettlementStatus.VOID


def test_unreviewed_or_not_yet_effective_rules_keep_bets_pending():
    drafts = RuleRegistry.from_directory(Path("configs/settlement/rules"))
    for bookmaker in ("betclic", "superbet", "fortuna"):
        result = settle(
            context(
                rule=f"{bookmaker}-settlement-draft-2026-09-29",
                bookmaker=bookmaker,
                bet_time=BET_TIME.replace(month=10),
                settled_at=SETTLED.replace(month=10),
                outcome=outcome(observed_at=SETTLED.replace(month=10)),
            ),
            drafts,
        )
        assert result.status == SettlementStatus.PENDING
        assert result.pending_reason == PendingReason.RULE_UNAVAILABLE
        assert "RULE_NOT_REVIEWED" in result.applied_rules[0]
    early = run(bet_time=BET_TIME.replace(month=8, day=1))
    assert early.pending_reason == PendingReason.RULE_UNAVAILABLE


def test_settlement_is_deterministic_and_rejects_inconsistent_inputs():
    assert run() == run()
    assert run().financial_digest() == run().financial_digest()
    assert run().financial_digest() != run(outcome(winner=PLAYER_B)).financial_digest()
    with pytest.raises(ValidationError, match="observed later"):
        context(outcome(observed_at=SETTLED + timedelta(seconds=1)))
    with pytest.raises(ValidationError, match="participant"):
        context(selection_player_id=UUID(int=7))
    with pytest.raises(ValidationError, match="another match"):
        context(outcome(match_id=UUID(int=8)))
    with pytest.raises(ValidationError, match="completion time"):
        outcome(completed_at=None)


def test_preview_payouts_without_tax_breakdown_report_unknown_tax():
    settled = run(stake_tax=None, winnings_tax_if_win=None)
    assert settled.status == SettlementStatus.WON and settled.tax_amount is None
