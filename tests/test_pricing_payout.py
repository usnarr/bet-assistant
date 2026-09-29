from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError
from settlement_support import BET_TIME, DOCUMENT_SHA256, allowed, pln, policy, registry

from tennis_engine.governance.contracts import Decision, EvidenceRef
from tennis_engine.governance.service import PolicyLookup
from tennis_engine.pricing.payout import (
    CouponPreview,
    PayoutReason,
    PayoutRequest,
    PayoutSource,
    Reconciliation,
    resolve_payout,
)

PREVIEW_ID = UUID("00000000-0000-4000-8000-0000000000f1")


def request(stake="100.00", odds="2.50", **overrides):
    return PayoutRequest.model_validate(
        {
            "bookmaker": "synthetic-book",
            "selection_key": "match-1:player-a",
            "decimal_odds": Decimal(odds),
            "stake": pln(stake),
            "account_scope": "shadow",
            "bet_time": BET_TIME,
            "known_at": BET_TIME,
        }
        | overrides
    )


def preview(cash, **overrides):
    return CouponPreview.model_validate(
        {
            "preview_id": PREVIEW_ID,
            "bookmaker": "synthetic-book",
            "selection_key": "match-1:player-a",
            "decimal_odds": Decimal("2.50"),
            "stake": pln("100.00"),
            "account_scope": "shadow",
            "payout_policy_version": "synthetic-policy-v1",
            "captured_at": BET_TIME - timedelta(seconds=5),
            "expires_at": BET_TIME + timedelta(seconds=30),
            "cash_return_if_win": pln(cash),
            "evidence": [EvidenceRef(document_id="synthetic-preview", sha256=DOCUMENT_SHA256)],
        }
        | overrides
    )


# Hand calculations with the synthetic regime: effective = S*0.90 (half up), gross =
# effective*odds (down), 20% of gross above PLN 1000.00 (strictly greater), cap 100000.00.
@pytest.mark.parametrize(
    ("stake", "odds", "effective", "gross", "winnings_tax", "cash"),
    [
        ("100.00", "2.50", "90.00", "225.00", "0.00", "225.00"),
        ("3.33", "1.87", "3.00", "5.61", "0.00", "5.61"),
        ("444.44", "2.50", "400.00", "1000.00", "0.00", "1000.00"),
        ("444.45", "2.50", "400.01", "1000.02", "200.00", "800.02"),
        ("500.00", "2.50", "450.00", "1125.00", "225.00", "900.00"),
        ("2.00", "1.01", "1.80", "1.81", "0.00", "1.81"),
    ],
)
def test_jurisdiction_payout_golden_cases(stake, odds, effective, gross, winnings_tax, cash):
    result = resolve_payout(request(stake, odds), allowed(), registry())
    assert result.actionable and result.source == PayoutSource.JURISDICTION_RULE
    assert result.breakdown is not None
    assert result.breakdown.effective_stake == pln(effective)
    assert result.breakdown.gross_return == pln(gross)
    assert result.breakdown.winnings_tax == pln(winnings_tax)
    assert result.cash_return_if_win == pln(cash)
    assert result.cash_return_if_loss == pln("0.00")
    assert result.rule_versions == ("synthetic-book-payout-v1", "synthetic-pl-payout-v1")


def test_maximum_cash_return_caps_the_payout():
    result = resolve_payout(request("5000.00", "30.00"), allowed(), registry())
    assert result.breakdown is not None
    assert result.breakdown.gross_return == pln("135000.00")
    assert result.breakdown.winnings_tax == pln("27000.00")
    assert result.breakdown.cap_reduction == pln("8000.00")
    assert result.cash_return_if_win == pln("100000.00")


def test_bookmaker_own_tax_regime_takes_precedence_over_jurisdiction():
    own = policy(payout_rule_version="synthetic-own-tax-payout-v1")
    result = resolve_payout(request("10.00", "1.91"), allowed(own), registry())
    assert result.source == PayoutSource.BOOKMAKER_RULE
    assert result.cash_return_if_win == pln("19.10")


@pytest.mark.parametrize(
    ("stake", "reason"),
    [
        ("1.99", PayoutReason.STAKE_BELOW_MINIMUM),
        ("5000.01", PayoutReason.STAKE_ABOVE_MAXIMUM),
    ],
)
def test_stake_limits_make_payout_non_actionable(stake, reason):
    result = resolve_payout(request(stake), allowed(), registry())
    assert not result.actionable and reason in result.reasons
    assert result.cash_return_if_win is None
    # The research diagnostic stays available but is never a decision input.
    assert result.research_cash_return is not None


def test_unknown_policy_or_rules_fail_closed():
    denied = PolicyLookup(Decision.deny("PAYOUT_POLICY_NOT_APPROVED", "v"))
    result = resolve_payout(request(), denied, registry())
    assert not result.actionable
    assert result.reasons == ("PAYOUT_POLICY_UNAVAILABLE", "PAYOUT_POLICY_NOT_APPROVED")
    assert result.research_cash_return == pln("250.00")

    draft = resolve_payout(
        request(), allowed(policy(payout_rule_version="synthetic-draft-payout-v1")), registry()
    )
    assert not draft.actionable
    assert draft.reasons == ("PAYOUT_RULE_UNAVAILABLE", "RULE_NOT_REVIEWED")

    other = resolve_payout(request(bookmaker="other-book"), allowed(), registry())
    assert "PAYOUT_POLICY_BOOKMAKER_MISMATCH" in other.reasons


def test_rule_not_known_at_decision_time_is_unavailable():
    early = request(known_at=BET_TIME.replace(month=8), bet_time=BET_TIME)
    result = resolve_payout(early, allowed(), registry())
    assert "RULE_NOT_KNOWN_AT_DECISION" in result.reasons


def test_supported_promotion_waives_stake_tax_only_when_eligible():
    covered = resolve_payout(
        request(promotion_rule_version="synthetic-tax-covered-v1"), allowed(), registry()
    )
    assert covered.actionable and covered.cash_return_if_win == pln("250.00")
    assert "synthetic-tax-covered-v1" in covered.rule_versions

    low_odds = resolve_payout(
        request(odds="1.40", promotion_rule_version="synthetic-tax-covered-v1"),
        allowed(),
        registry(),
    )
    assert PayoutReason.PROMOTION_NOT_ELIGIBLE in low_odds.reasons and not low_odds.actionable

    freebet = resolve_payout(
        request(promotion_rule_version="synthetic-freebet-v1"), allowed(), registry()
    )
    assert PayoutReason.PROMOTION_UNSUPPORTED in freebet.reasons

    disabled = resolve_payout(
        request(promotion_rule_version="synthetic-tax-covered-v1"),
        allowed(policy(promotion_handling="disabled", promotion_rule_version=None)),
        registry(),
    )
    assert disabled.reasons == (PayoutReason.PROMOTION_DISABLED,)


def test_bound_preview_takes_precedence_and_reconciles():
    result = resolve_payout(request(), allowed(), registry(), preview("225.00"))
    assert result.actionable and result.source == PayoutSource.COUPON_PREVIEW
    assert result.reconciliation == Reconciliation.MATCHED
    assert result.preview_id == PREVIEW_ID


def test_mismatched_preview_fails_closed_and_requires_review():
    result = resolve_payout(request(), allowed(), registry(), preview("230.00"))
    assert not result.actionable
    assert result.reasons == (PayoutReason.PREVIEW_MISMATCH,)
    assert result.review_required and result.reconciliation == Reconciliation.MISMATCHED


def test_preview_alone_is_used_when_calculator_is_unavailable():
    draft = allowed(policy(payout_rule_version="synthetic-draft-payout-v1"))
    result = resolve_payout(request(), draft, registry(), preview("225.00"))
    assert result.actionable and result.source == PayoutSource.COUPON_PREVIEW
    assert result.reconciliation == Reconciliation.CALCULATOR_UNAVAILABLE


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"stake": pln("100.01")}, "STAKE"),
        ({"decimal_odds": Decimal("2.51")}, "ODDS"),
        ({"selection_key": "match-1:player-b"}, "SELECTION"),
        ({"account_scope": "other"}, "ACCOUNT_SCOPE"),
        ({"payout_policy_version": "older-policy"}, "POLICY"),
        ({"promotion_rule_version": "synthetic-tax-covered-v1"}, "PROMOTION"),
        (
            {
                "captured_at": BET_TIME + timedelta(seconds=1),
                "expires_at": BET_TIME + timedelta(seconds=9),
            },
            "NOT_YET_CAPTURED",
        ),
        ({"expires_at": BET_TIME}, "EXPIRED"),
    ],
)
def test_preview_binds_only_to_its_exact_inputs(overrides, problem):
    result = resolve_payout(request(), allowed(), registry(), preview("999.99", **overrides))
    # An unbound preview is ignored; the reviewed calculator result is used instead.
    assert result.source == PayoutSource.JURISDICTION_RULE
    assert result.cash_return_if_win == pln("225.00")
    assert f"PREVIEW_NOT_BOUND:{problem}" in result.notes


def test_contracts_reject_floats_and_inconsistent_values():
    with pytest.raises(ValidationError):
        request(decimal_odds=2.5)
    with pytest.raises(ValidationError):
        request(stake="0.00")
    with pytest.raises(ValidationError):
        request(decimal_odds=Decimal("1.00"))
    with pytest.raises(ValidationError):
        preview("1.00", expires_at=BET_TIME - timedelta(seconds=10))
