from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st
from settlement_support import BET_TIME, allowed, registry

from tennis_engine.common.contracts import Money
from tennis_engine.pricing.payout import PayoutRequest, resolve_payout

RULES = registry()
stakes = st.integers(min_value=200, max_value=500_000).map(lambda cents: Decimal(cents) / 100)
odds = st.integers(min_value=101, max_value=5_000).map(lambda value: Decimal(value) / 100)


def resolve(stake: Decimal, price: Decimal, promotion: str | None = None):
    return resolve_payout(
        PayoutRequest(
            bookmaker="synthetic-book",
            selection_key="match-1:player-a",
            decimal_odds=price,
            stake=Money(amount=stake.quantize(Decimal("0.01"))),
            account_scope="shadow",
            promotion_rule_version=promotion,
            bet_time=BET_TIME,
            known_at=BET_TIME,
        ),
        allowed(),
        RULES,
    )


@settings(max_examples=300, deadline=None)
@given(stake=stakes, price=odds)
def test_property_payout_is_cent_exact_and_bounded_by_plain_odds(stake, price):
    result = resolve(stake, price)
    assert result.actionable and result.breakdown is not None
    cash = result.cash_return_if_win
    assert cash is not None and cash.amount == cash.amount.quantize(Decimal("0.01"))
    assert Decimal(0) < cash.amount <= stake * price
    part = result.breakdown
    assert part.stake.amount == part.stake_tax.amount + part.effective_stake.amount
    assert cash.amount == (
        part.gross_return.amount - part.winnings_tax.amount - part.cap_reduction.amount
    )
    assert cash.amount <= Decimal("100000.00")


@settings(max_examples=200, deadline=None)
@given(stake=stakes.filter(lambda value: value <= 500), price=odds.filter(lambda v: v >= 2))
def test_property_supported_promotion_never_lowers_the_return(stake, price):
    plain = resolve(stake, price)
    covered = resolve(stake, price, "synthetic-tax-covered-v1")
    assert covered.actionable and plain.cash_return_if_win and covered.cash_return_if_win
    gross_plain = plain.breakdown.gross_return.amount
    gross_covered = covered.breakdown.gross_return.amount
    assert gross_covered >= gross_plain


@settings(max_examples=200, deadline=None)
@given(stake=stakes, price=odds)
def test_property_resolution_is_deterministic(stake, price):
    assert resolve(stake, price) == resolve(stake, price)
