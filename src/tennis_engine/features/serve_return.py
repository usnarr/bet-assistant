"""Count-based serve/return rates with Beta-Binomial shrinkage (F08.4).

Three cases stay distinct: no stats recorded (missing), zero attempts recorded, and zero
successes observed. The shrunk rate is always paired with its raw count and a missing
flag, so a model can tell a prior from evidence.
"""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, localcontext
from uuid import UUID

from pydantic import Field

from tennis_engine.common.contracts import Contract, Identifier
from tennis_engine.contracts.domain import Tour
from tennis_engine.normalization.contracts import MatchStatus

from .asof import AsOfView, CompletedMatch
from .contracts import InputRef


class ServeReturnConfig(Contract):
    """Prior means are candidate values; F09/F13 must refit them on training data only."""

    version: Identifier = "serve-return-v1-candidate"
    window_days: int = Field(default=365, ge=1)
    prior_strength: Decimal = Field(default=Decimal(200), gt=0)
    serve_prior: dict[Tour, Decimal] = Field(
        default_factory=lambda: {Tour.ATP: Decimal("0.64"), Tour.WTA: Decimal("0.56")}
    )


@dataclass(frozen=True)
class RateEstimate:
    successes: int
    attempts: int
    matches_with_stats: int
    matches_without_stats: int
    raw: Decimal | None
    shrunk: Decimal
    prior_mean: Decimal

    @property
    def missing(self) -> bool:
        return self.matches_with_stats == 0


def shrink(successes: int, attempts: int, prior_mean: Decimal, strength: Decimal) -> Decimal:
    """Posterior mean ``(s + m*n0) / (n + n0)`` of a Beta-Binomial model."""
    with localcontext() as ctx:
        ctx.prec = 28
        return (Decimal(successes) + prior_mean * strength) / (Decimal(attempts) + strength)


def rates(
    view: AsOfView,
    history: list[CompletedMatch],
    player_id: UUID,
    tour: Tour,
    config: ServeReturnConfig,
) -> tuple[RateEstimate, RateEstimate, list[InputRef]]:
    """Serve and return point-win rates from completed matches in the window.

    Walkovers have no played points, so they count neither as evidence nor as missing.
    """
    since = view.as_of - timedelta(days=config.window_days)
    serve_won = serve_n = return_won = return_n = 0
    with_stats = without = 0
    refs: list[InputRef] = []
    for item in history:
        if item.ended_at < since or item.result.status == MatchStatus.WALKOVER:
            continue
        known = view.stats(item.match.match_id, player_id)
        if known is None:
            without += 1
            continue
        version, ref = known
        counts = version.counts
        if counts.serve_points is None or counts.serve_points_won is None:
            without += 1
            continue
        refs.append(ref)
        with_stats += 1
        serve_won += counts.serve_points_won
        serve_n += counts.serve_points
        if counts.return_points is not None and counts.return_points_won is not None:
            return_won += counts.return_points_won
            return_n += counts.return_points
    serve_prior = config.serve_prior[tour]
    return_prior = Decimal(1) - serve_prior

    def estimate(won: int, attempts: int, prior: Decimal) -> RateEstimate:
        raw = Decimal(won) / Decimal(attempts) if attempts > 0 else None
        return RateEstimate(
            won,
            attempts,
            with_stats,
            without,
            raw,
            shrink(won, attempts, prior, config.prior_strength),
            prior,
        )

    return (
        estimate(serve_won, serve_n, serve_prior),
        estimate(return_won, return_n, return_prior),
        refs,
    )
