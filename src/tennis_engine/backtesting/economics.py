"""Economic summary of settled virtual bets (F13.6, F13.7).

Profit, ROI, drawdown and losing streaks come from a sequential equity path in settlement
order. CLV is ``odds / closing_odds - 1`` and exists only when a comparable closing quote
was observed; a missing closing price is counted as missing, never as zero. Closing
prices are evaluation data only and never a prediction input.
"""

from collections import Counter
from collections.abc import Sequence
from decimal import Decimal, localcontext
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from tennis_engine.common.contracts import Amount, Contract, Identifier, Timestamp

from .bootstrap import BootstrapInterval, block_bootstrap

QUANTUM = Decimal("1e-6")
CENT = Decimal("0.01")


class ReplayBet(Contract):
    bet_id: UUID
    block: Identifier
    decided_at: Timestamp
    settled_at: Timestamp | None
    stake: Amount
    cash_return: Amount | None
    odds: Decimal
    closing_odds: Decimal | None = None
    closing_policy: Identifier | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.stake <= 0 or self.odds <= 1:
            raise ValueError("A bet needs a positive stake and decimal odds above one")
        if (self.cash_return is None) != (self.settled_at is None):
            raise ValueError("A settled bet has both a return and a settlement time")
        if self.closing_odds is not None and (self.closing_odds <= 1 or not self.closing_policy):
            raise ValueError("A closing price needs valid odds and a comparability policy")
        return self

    @property
    def profit(self) -> Decimal | None:
        return None if self.cash_return is None else self.cash_return - self.stake


class EconomicSummary(Contract):
    execution_grade: bool
    assumptions: tuple[str, ...]
    decisions: dict[Literal["BET", "WATCH", "NO_BET"], int]
    abstention_rate: Decimal | None
    actionable_decisions: Annotated[int, Field(ge=0, strict=True)]
    actionability_coverage: Decimal | None
    bets: int
    pending: int
    total_stake: Decimal
    total_return: Decimal
    profit: Decimal
    roi: Decimal | None
    max_drawdown: Decimal
    max_drawdown_fraction: Decimal | None
    longest_losing_streak: int
    clv_observed: int
    clv_missing: int
    mean_clv: Decimal | None
    roi_interval: BootstrapInterval | None


def summarize(
    bets: Sequence[ReplayBet],
    *,
    decisions: Sequence[Literal["BET", "WATCH", "NO_BET"]],
    actionable_decisions: int,
    starting_bankroll: Decimal,
    execution_grade: bool,
    assumptions: Sequence[str],
    draws: int,
    seed: int,
    level: Decimal,
) -> EconomicSummary:
    if starting_bankroll <= 0:
        raise ValueError("The starting bankroll must be positive")
    if not execution_grade and not assumptions:
        raise ValueError("A non-execution-grade replay must name its assumptions")
    counts = Counter(decisions)
    settled = sorted(
        (item for item in bets if item.cash_return is not None),
        key=lambda item: (item.settled_at, str(item.bet_id)),
    )
    stake = sum((item.stake for item in settled), Decimal(0))
    returned = sum((item.cash_return or Decimal(0) for item in settled), Decimal(0))
    equity = peak = starting_bankroll
    drawdown = Decimal(0)
    drawdown_fraction = Decimal(0)
    streak = longest = 0
    for item in settled:
        profit = item.profit or Decimal(0)
        equity += profit
        peak = max(peak, equity)
        if peak - equity > drawdown:
            drawdown = peak - equity
            drawdown_fraction = (drawdown / peak).quantize(QUANTUM)
        if profit < 0:
            streak += 1
            longest = max(longest, streak)
        elif profit > 0:
            streak = 0
    clv = [item.odds / item.closing_odds - 1 for item in bets if item.closing_odds is not None]
    total = len(decisions)
    with localcontext() as ctx:
        ctx.prec = 34
        interval = (
            block_bootstrap(
                [(item.block, item.profit or Decimal(0), item.stake) for item in settled],
                statistic="net_roi",
                draws=draws,
                seed=seed,
                level=level,
            )
            if settled
            else None
        )
        return EconomicSummary(
            execution_grade=execution_grade,
            assumptions=tuple(assumptions),
            decisions={key: counts.get(key, 0) for key in ("BET", "WATCH", "NO_BET")},
            abstention_rate=(Decimal(total - counts["BET"]) / total).quantize(QUANTUM)
            if total
            else None,
            actionable_decisions=actionable_decisions,
            actionability_coverage=(Decimal(actionable_decisions) / total).quantize(QUANTUM)
            if total
            else None,
            bets=len(bets),
            pending=len(bets) - len(settled),
            total_stake=stake,
            total_return=returned,
            profit=returned - stake,
            roi=((returned - stake) / stake).quantize(QUANTUM) if stake else None,
            max_drawdown=drawdown.quantize(CENT),
            max_drawdown_fraction=drawdown_fraction if settled else None,
            longest_losing_streak=longest,
            clv_observed=len(clv),
            clv_missing=len(bets) - len(clv),
            mean_clv=(sum(clv, Decimal(0)) / len(clv)).quantize(QUANTUM) if clv else None,
            roi_interval=interval,
        )
