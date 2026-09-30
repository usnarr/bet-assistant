from decimal import Decimal

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from settlement_support import reviewed

from tennis_engine.governance.contracts import ResponsibleUsePolicy
from tennis_engine.pricing.risk import (
    CapacityReason,
    DecisionPolicy,
    ExposureState,
    PeriodUsage,
    SizingReason,
    StakeRules,
    capacity,
    size_stake,
)
from tennis_engine.pricing.value import (
    binary_value,
    conservative_kelly_fraction,
    outcome_expected_value,
    proportional_devig,
)

D = Decimal
RULES = StakeRules(minimum=D("2.00"), increment=D("0.01"), maximum=D("5000.00"))


def decision_policy(**overrides):
    return DecisionPolicy.model_validate(
        reviewed("synthetic-decision-v1")
        | {
            "state": "APPROVED",
            "kelly_fraction": "0.20",
            "minimum_conservative_roi": "0.02",
            "max_model_disagreement": "0.05",
            "max_unconfirmed_edge": "0.10",
            "consensus_tolerance": "0.03",
            "max_bookmaker_exposure": "80.00",
            "max_open_bets": 20,
            "reservation_ttl_seconds": 120,
            "no_bet_ttl_seconds": 60,
        }
        | overrides
    )


def responsible(**overrides):
    return ResponsibleUsePolicy.model_validate(
        reviewed("synthetic-responsible-v1")
        | {
            "account_scope": "shadow",
            "ledger_scope": "virtual",
            "state": "APPROVED",
            "daily": {"stake": "60.00", "count": 5},
            "weekly": {"stake": "200.00", "count": 20},
            "monthly": {"stake": "500.00", "count": 60},
            "max_event_exposure": "50.00",
            "max_open_exposure": "150.00",
            "max_bankroll_fraction": "0.05",
            "drawdown_stop": "0.20",
            "disable_recommendations": False,
        }
        | overrides
    )


def exposure(bankroll="1000.00", **overrides):
    usage = {"stake": "0.00", "count": 0}
    return ExposureState.model_validate(
        {
            "bankroll": bankroll,
            "equity": bankroll,
            "peak_bankroll": bankroll,
            "open_exposure": "0.00",
            "event_exposure": "0.00",
            "bookmaker_exposure": "0.00",
            "open_bets": 0,
            "daily": usage,
            "weekly": usage,
            "monthly": usage,
        }
        | overrides
    )


def linear(odds):
    return lambda stake: (stake * odds).quantize(D("0.01"))


# --- SYS-10 arithmetic cases -----------------------------------------------------------


def test_zero_edge_has_zero_ev_and_no_kelly_stake():
    value = binary_value(
        probability=D("0.5"), conservative_probability=D("0.5"), stake=D("10"), cash_return=D("20")
    )
    assert value.expected_value == 0 and value.break_even_probability == D("0.5")
    assert (
        conservative_kelly_fraction(
            conservative_probability=D("0.5"), payout_ratio=D("2"), kelly_fraction=D("1")
        )
        == 0
    )


def test_small_positive_central_negative_conservative():
    value = binary_value(
        probability=D("0.52"),
        conservative_probability=D("0.48"),
        stake=D("10"),
        cash_return=D("20"),
    )
    assert value.expected_value == D("0.40") and value.expected_roi == D("0.04")
    assert value.conservative_expected_value == D("-0.40")
    assert value.probability_edge > 0 > value.conservative_edge


def test_outcome_model_ev_includes_void_returns_and_requires_a_complete_model():
    ev = outcome_expected_value(
        [(D("0.55"), D("19.00")), (D("0.40"), D("0")), (D("0.05"), D("10"))], D("10")
    )
    assert ev == D("0.95")
    with pytest.raises(ValueError, match="sum exactly"):
        outcome_expected_value([(D("0.5"), D("19"))], D("10"))


def test_value_contract_rejects_inconsistent_probabilities():
    with pytest.raises(ValueError):
        binary_value(
            probability=D("0.4"),
            conservative_probability=D("0.5"),
            stake=D("1"),
            cash_return=D("2"),
        )
    with pytest.raises(ValueError):
        binary_value(
            probability=D("1.2"),
            conservative_probability=D("0.5"),
            stake=D("1"),
            cash_return=D("2"),
        )


def test_proportional_devig_removes_overround():
    result = proportional_devig([D("1.80"), D("2.00")])
    assert sum(result.fair) == 1
    assert result.overround > 0
    assert result.fair[0] > result.fair[1]
    with pytest.raises(ValueError):
        proportional_devig([D("1.00"), D("3.00")])


def test_sizing_uses_conservative_kelly_and_every_cap():
    cap = capacity(decision_policy(), responsible(), exposure(), RULES)
    assert cap.available and cap.maximum_stake == D("50.00")  # Event cap binds.
    sizing = size_stake(
        conservative_probability=D("0.55"),
        bankroll=D("1000.00"),
        maximum_stake=cap.maximum_stake,
        rules=RULES,
        kelly_fraction=D("0.20"),
        payout=linear(D("2.00")),
        max_steps=100_000,
    )
    # Full Kelly at r=2, p=0.55 is 0.10; 20% of that on PLN 1000 is PLN 20.00.
    assert sizing.stake == D("20.00") and sizing.kelly_stake == D("20.00")


def test_minimum_stake_conflict_abstains_instead_of_rounding_up():
    sizing = size_stake(
        conservative_probability=D("0.51"),
        bankroll=D("100.00"),
        maximum_stake=D("50.00"),
        rules=RULES,
        kelly_fraction=D("0.20"),
        payout=linear(D("2.00")),
        max_steps=100_000,
    )
    # Kelly stake PLN 0.40 is below the PLN 2.00 minimum.
    assert sizing.stake is None and sizing.reason == SizingReason.STAKE_BELOW_MINIMUM


def test_nonlinear_payout_is_recomputed_at_each_stake():
    def taxed(stake):
        gross = (stake * D("0.9") * D("3.00")).quantize(D("0.01"))
        return gross - (gross * D("0.2")).quantize(D("0.01")) if gross > 100 else gross

    sizing = size_stake(
        conservative_probability=D("0.45"),
        bankroll=D("5000.00"),
        maximum_stake=D("500.00"),
        rules=RULES,
        kelly_fraction=D("0.25"),
        payout=taxed,
        max_steps=100_000,
    )
    # Above a gross of PLN 100 the tax makes r = 2.16 and conservative EV negative,
    # so the largest valid stake keeps the gross at or below the threshold.
    assert sizing.stake == D("37.03")
    assert sizing.cash_return_if_win == D("99.98")


def test_empty_bankroll_drawdown_and_count_limits_remove_capacity():
    empty = capacity(decision_policy(), responsible(), exposure("0.00"), RULES)
    assert CapacityReason.EMPTY_BANKROLL in empty.reasons
    drawdown = capacity(
        decision_policy(),
        responsible(),
        exposure("790.00", equity="790.00", peak_bankroll="1000.00"),
        RULES,
    )
    assert CapacityReason.DRAWDOWN_STOP in drawdown.reasons
    counted = capacity(
        decision_policy(),
        responsible(),
        exposure(daily=PeriodUsage(stake=D("10.00"), count=5)),
        RULES,
    )
    assert CapacityReason.DAILY_COUNT_LIMIT in counted.reasons
    full = capacity(decision_policy(), responsible(), exposure(event_exposure="49.00"), RULES)
    assert CapacityReason.BELOW_MINIMUM_STAKE in full.reasons and full.maximum_stake == D("1.00")


# --- Properties ------------------------------------------------------------------------

probabilities = st.integers(min_value=1, max_value=999).map(lambda value: D(value) / 1000)
odds = st.integers(min_value=101, max_value=1000).map(lambda value: D(value) / 100)
bankrolls = st.integers(min_value=0, max_value=1_000_000).map(lambda cents: D(cents) / 100)


@settings(max_examples=300, deadline=None)
@given(
    p=probabilities,
    stake=st.integers(1, 10_000),
    cash=st.integers(1, 100_000),
    bump=st.integers(0, 1000),
)
def test_property_higher_cash_return_never_lowers_binary_ev(p, stake, cash, bump):
    low = binary_value(
        probability=p, conservative_probability=p, stake=D(stake), cash_return=D(cash)
    )
    high = binary_value(
        probability=p, conservative_probability=p, stake=D(stake), cash_return=D(cash + bump)
    )
    assert high.expected_value >= low.expected_value


@settings(max_examples=300, deadline=None)
@given(p=probabilities, price=odds, bankroll=bankrolls)
def test_property_no_stake_at_or_below_break_even(p, price, bankroll):
    assume(p * price <= 1)
    sizing = size_stake(
        conservative_probability=p,
        bankroll=bankroll,
        maximum_stake=bankroll,
        rules=RULES,
        kelly_fraction=D("1"),
        payout=linear(price),
        max_steps=100_000,
    )
    assert sizing.stake is None


@settings(max_examples=200, deadline=None)
@given(p=probabilities, price=odds, bankroll=bankrolls, event=st.integers(0, 6000))
def test_property_sized_stake_respects_every_cap(p, price, bankroll, event):
    state = exposure(str(bankroll), event_exposure=str(D(event) / 100))
    cap = capacity(decision_policy(), responsible(), state, RULES)
    sizing = size_stake(
        conservative_probability=p,
        bankroll=bankroll,
        maximum_stake=cap.maximum_stake,
        rules=RULES,
        kelly_fraction=D("0.20"),
        payout=linear(price),
        max_steps=100_000,
    )
    if sizing.stake is not None:
        assert RULES.minimum <= sizing.stake <= cap.maximum_stake
        assert sizing.stake == sizing.stake.quantize(D("0.01"))
        assert sizing.stake <= bankroll * D("0.05")
        assert p * sizing.cash_return_if_win - sizing.stake > 0


@settings(max_examples=200, deadline=None)
@given(p=probabilities, price=odds, bankroll=bankrolls, loss=st.integers(0, 100_000))
def test_property_losses_never_increase_the_stake(p, price, bankroll, loss):
    after_loss = max(D(0), bankroll - D(loss) / 100)

    def stake_for(balance):
        return size_stake(
            conservative_probability=p,
            bankroll=balance,
            maximum_stake=balance * D("0.05"),
            rules=RULES,
            kelly_fraction=D("0.20"),
            payout=linear(price),
            max_steps=100_000,
        ).stake or D(0)

    assert stake_for(after_loss) <= stake_for(bankroll)
