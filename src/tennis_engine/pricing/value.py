"""F12.1 exact value arithmetic for binary cash bets, de-vig and conservative Kelly.

`S` is the stake deducted and `W` the cash returned on a win, including stake. The binary
formulas assume a loss returns zero. Other outcomes need `outcome_expected_value`.
"""

from collections.abc import Sequence
from decimal import Decimal, localcontext

from tennis_engine.common.contracts import Contract

PRECISION = 34


def _check_probability(value: Decimal, name: str) -> None:
    if not value.is_finite() or not Decimal(0) <= value <= Decimal(1):
        raise ValueError(f"{name} must be a probability in [0, 1]")


class BinaryValue(Contract):
    stake: Decimal
    cash_return_if_win: Decimal
    break_even_probability: Decimal
    expected_value: Decimal
    expected_roi: Decimal
    conservative_expected_value: Decimal
    conservative_roi: Decimal
    probability_edge: Decimal
    conservative_edge: Decimal


def binary_value(
    *, probability: Decimal, conservative_probability: Decimal, stake: Decimal, cash_return: Decimal
) -> BinaryValue:
    """EV = p*W - S, ROI = EV/S, break-even = S/W, and the same with p_low."""
    _check_probability(probability, "probability")
    _check_probability(conservative_probability, "conservative_probability")
    if conservative_probability > probability:
        raise ValueError("The conservative probability cannot exceed the central probability")
    if stake <= 0 or cash_return <= 0:
        raise ValueError("Stake and cash return must be positive")
    with localcontext() as context:
        context.prec = PRECISION
        break_even = stake / cash_return
        ev = probability * cash_return - stake
        conservative_ev = conservative_probability * cash_return - stake
        return BinaryValue(
            stake=stake,
            cash_return_if_win=cash_return,
            break_even_probability=break_even,
            expected_value=ev,
            expected_roi=ev / stake,
            conservative_expected_value=conservative_ev,
            conservative_roi=conservative_ev / stake,
            probability_edge=probability - break_even,
            conservative_edge=conservative_probability - break_even,
        )


def outcome_expected_value(outcomes: Sequence[tuple[Decimal, Decimal]], stake: Decimal) -> Decimal:
    """EV = sum(p_outcome * cash_return_outcome) - S for a complete outcome model."""
    total = sum((probability for probability, _ in outcomes), Decimal(0))
    if total != 1:
        raise ValueError("Outcome probabilities must sum exactly to one")
    for probability, cash in outcomes:
        _check_probability(probability, "outcome probability")
        if cash < 0:
            raise ValueError("A cash return cannot be negative")
    with localcontext() as context:
        context.prec = PRECISION
        return sum((probability * cash for probability, cash in outcomes), Decimal(0)) - stake


def conservative_kelly_fraction(
    *, conservative_probability: Decimal, payout_ratio: Decimal, kelly_fraction: Decimal
) -> Decimal:
    """max(0, (r*p_low - 1)/(r - 1)) * lambda, with r = W/S."""
    _check_probability(conservative_probability, "conservative_probability")
    if payout_ratio <= 1:
        return Decimal(0)
    if not Decimal(0) <= kelly_fraction <= Decimal(1):
        raise ValueError("The Kelly fraction must be in [0, 1]")
    with localcontext() as context:
        context.prec = PRECISION
        full = (payout_ratio * conservative_probability - 1) / (payout_ratio - 1)
        return max(Decimal(0), full) * kelly_fraction


class DeVig(Contract):
    implied: tuple[Decimal, ...]
    overround: Decimal
    fair: tuple[Decimal, ...]


def proportional_devig(decimal_odds: Sequence[Decimal]) -> DeVig:
    """Proportional normalization of one complete market (blueprint section 25.2)."""
    if len(decimal_odds) < 2:
        raise ValueError("A complete market needs at least two outcomes")
    if any(not odds.is_finite() or odds <= 1 for odds in decimal_odds):
        raise ValueError("Decimal odds must be finite and above 1")
    with localcontext() as context:
        context.prec = PRECISION
        implied = tuple(Decimal(1) / odds for odds in decimal_odds)
        total = sum(implied, Decimal(0))
        return DeVig(
            implied=implied,
            overround=total - 1,
            fair=tuple(item / total for item in implied),
        )
