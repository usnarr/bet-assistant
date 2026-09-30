"""F12.4, F12.5, F12.7 exposure capacity and bounded stake search.

The stake is a deterministic function of the current bankroll, quote, probability,
uncertainty and exposure. Past losses are not an input, so they cannot raise it. The
search walks down the permitted stake grid and returns the largest stake that is within
every cap, has positive conservative EV at its own payout and does not exceed the
conservative fractional Kelly stake at that payout. Payout is recomputed at each stake,
so nonlinear taxes and caps are never extrapolated.
"""

from collections.abc import Callable
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from tennis_engine.common.contracts import Contract
from tennis_engine.governance.contracts import (
    Amount,
    Fraction,
    ResponsibleUsePolicy,
    ReviewedPolicy,
)

from .value import conservative_kelly_fraction

CENT = Decimal("0.01")


class DecisionPolicy(ReviewedPolicy):
    """Versioned decision and sizing settings. Values are product-risk choices."""

    state: Literal["PENDING_REVIEW", "APPROVED", "SUSPENDED"] = "PENDING_REVIEW"
    kelly_fraction: Fraction
    minimum_conservative_roi: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    max_model_disagreement: Fraction
    # A central edge above this needs corroborating consensus before it can be acted on.
    max_unconfirmed_edge: Fraction
    consensus_tolerance: Fraction
    max_bookmaker_exposure: Amount
    max_open_bets: Annotated[int, Field(ge=0, strict=True)]
    reservation_ttl_seconds: Annotated[int, Field(gt=0, strict=True)]
    no_bet_ttl_seconds: Annotated[int, Field(gt=0, strict=True)]
    # Probability of a void used to stress sporting-win probabilities. None blocks them.
    void_stress_probability: Fraction | None = None
    max_search_steps: Annotated[int, Field(gt=0, le=1_000_000, strict=True)] = 100_000

    @model_validator(mode="after")
    def approval_complete(self) -> Self:
        if self.state == "APPROVED":
            self.require_review()
        return self


class StakeRules(Contract):
    minimum: Amount
    increment: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    maximum: Amount | None


class PeriodUsage(Contract):
    stake: Amount
    count: Annotated[int, Field(ge=0, strict=True)]


class ExposureState(Contract):
    """Current virtual bankroll and exposure, including active reservations."""

    bankroll: Amount  # Available cash after open stakes and active reservations.
    equity: Amount  # Cash plus open stakes at cost; drawdown is measured on equity.
    peak_bankroll: Amount  # Highest equity so far.
    open_exposure: Amount
    event_exposure: Amount
    bookmaker_exposure: Amount
    open_bets: Annotated[int, Field(ge=0, strict=True)]
    daily: PeriodUsage
    weekly: PeriodUsage
    monthly: PeriodUsage


class CapacityReason(StrEnum):
    DRAWDOWN_STOP = "DRAWDOWN_STOP"
    EMPTY_BANKROLL = "EMPTY_BANKROLL"
    OPEN_BET_LIMIT = "OPEN_BET_LIMIT"
    DAILY_COUNT_LIMIT = "DAILY_COUNT_LIMIT"
    WEEKLY_COUNT_LIMIT = "WEEKLY_COUNT_LIMIT"
    MONTHLY_COUNT_LIMIT = "MONTHLY_COUNT_LIMIT"
    BELOW_MINIMUM_STAKE = "BELOW_MINIMUM_STAKE"


class Capacity(Contract):
    maximum_stake: Decimal
    limiting_cap: str
    reasons: tuple[CapacityReason, ...]

    @property
    def available(self) -> bool:
        return not self.reasons


def capacity(
    policy: DecisionPolicy,
    responsible: ResponsibleUsePolicy,
    state: ExposureState,
    rules: StakeRules,
) -> Capacity:
    """The largest stake every cap allows now; each cap is named for the audit record."""
    reasons: list[CapacityReason] = []
    if state.bankroll <= 0:
        reasons.append(CapacityReason.EMPTY_BANKROLL)
    if state.peak_bankroll > 0:
        drawdown = (state.peak_bankroll - state.equity) / state.peak_bankroll
        if responsible.drawdown_stop <= 0 or drawdown >= responsible.drawdown_stop:
            reasons.append(CapacityReason.DRAWDOWN_STOP)
    if state.open_bets >= policy.max_open_bets:
        reasons.append(CapacityReason.OPEN_BET_LIMIT)
    for used, limit, reason in (
        (state.daily, responsible.daily, CapacityReason.DAILY_COUNT_LIMIT),
        (state.weekly, responsible.weekly, CapacityReason.WEEKLY_COUNT_LIMIT),
        (state.monthly, responsible.monthly, CapacityReason.MONTHLY_COUNT_LIMIT),
    ):
        if used.count >= limit.count:
            reasons.append(reason)
    caps = {
        "bankroll": state.bankroll,
        "single_bet_fraction": state.bankroll * responsible.max_bankroll_fraction,
        "event_exposure": responsible.max_event_exposure - state.event_exposure,
        "bookmaker_exposure": policy.max_bookmaker_exposure - state.bookmaker_exposure,
        "open_exposure": responsible.max_open_exposure - state.open_exposure,
        "daily_stake": responsible.daily.stake - state.daily.stake,
        "weekly_stake": responsible.weekly.stake - state.weekly.stake,
        "monthly_stake": responsible.monthly.stake - state.monthly.stake,
    }
    if rules.maximum is not None:
        caps["bookmaker_maximum_stake"] = rules.maximum
    limiting = min(caps, key=lambda key: (caps[key], key))
    maximum = max(Decimal(0), caps[limiting]).quantize(CENT, rounding=ROUND_FLOOR)
    if maximum < rules.minimum:
        reasons.append(CapacityReason.BELOW_MINIMUM_STAKE)
    return Capacity(maximum_stake=maximum, limiting_cap=limiting, reasons=tuple(reasons))


class SizingReason(StrEnum):
    STAKE_BELOW_MINIMUM = "STAKE_BELOW_MINIMUM"
    SEARCH_EXHAUSTED = "SEARCH_EXHAUSTED"


class StakeSizing(Contract):
    stake: Decimal | None
    cash_return_if_win: Decimal | None
    kelly_stake: Decimal | None
    steps: int
    reason: SizingReason | None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.stake is None) == (self.reason is None):
            raise ValueError("A sizing has either a stake or a reason")
        return self


def _floor_to_grid(value: Decimal, rules: StakeRules) -> Decimal:
    if value < rules.minimum:
        return value.quantize(CENT, rounding=ROUND_FLOOR)
    steps = ((value - rules.minimum) / rules.increment).to_integral_value(rounding=ROUND_FLOOR)
    return rules.minimum + steps * rules.increment


def size_stake(
    *,
    conservative_probability: Decimal,
    bankroll: Decimal,
    maximum_stake: Decimal,
    rules: StakeRules,
    kelly_fraction: Decimal,
    payout: Callable[[Decimal], Decimal | None],
    max_steps: int,
) -> StakeSizing:
    """Largest grid stake <= every cap with positive conservative EV and <= its Kelly stake.

    `payout(stake)` returns the exact cash return on a win for that stake, or None when no
    reviewed payout exists for it. Every returned stake has been checked at its own payout.
    """
    # Full Kelly (r*p - 1)/(r - 1) never exceeds p, so no stake above this bound can pass.
    # The bound does not depend on the stake, so the search stays monotone in the bankroll.
    kelly_bound = bankroll * kelly_fraction * conservative_probability
    stake = _floor_to_grid(min(maximum_stake, kelly_bound), rules)
    steps = 0
    while stake >= rules.minimum:
        if steps >= max_steps:
            return StakeSizing(
                stake=None,
                cash_return_if_win=None,
                kelly_stake=None,
                steps=steps,
                reason=SizingReason.SEARCH_EXHAUSTED,
            )
        steps += 1
        cash = payout(stake)
        if cash is not None and cash > stake:
            fraction = conservative_kelly_fraction(
                conservative_probability=conservative_probability,
                payout_ratio=cash / stake,
                kelly_fraction=kelly_fraction,
            )
            kelly_stake = (bankroll * fraction).quantize(CENT, rounding=ROUND_FLOOR)
            if conservative_probability * cash - stake > 0 and stake <= kelly_stake:
                return StakeSizing(
                    stake=stake,
                    cash_return_if_win=cash,
                    kelly_stake=kelly_stake,
                    steps=steps,
                    reason=None,
                )
        stake -= rules.increment
    return StakeSizing(
        stake=None,
        cash_return_if_win=None,
        kelly_stake=None,
        steps=steps,
        reason=SizingReason.STAKE_BELOW_MINIMUM,
    )
