"""Global and surface Elo with inactivity shrinkage, and opponent-adjusted form (F08.2–F08.3).

The replay uses only matches that :class:`AsOfView` proves completed before the cutoff,
in its fixed chronological order. All arithmetic is ``Decimal`` in a local context, so a
rebuild gives identical values. Parameters are versioned configuration; fitting them on
training periods only is the job of F09/F13.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Literal
from uuid import UUID

from pydantic import Field

from tennis_engine.common.contracts import Contract, Identifier
from tennis_engine.normalization.contracts import MatchStatus, Surface

from .asof import AsOfView, CompletedMatch

ONE = Decimal(1)
TEN = Decimal(10)
PRECISION = 28


class EloConfig(Contract):
    version: Identifier = "elo-v1-candidate"
    initial: Decimal = Decimal(1500)
    scale: Decimal = Decimal(400)
    k_numerator: Decimal = Decimal(250)
    k_offset: Decimal = Decimal(5)
    k_shape: Decimal = Decimal("0.4")
    inactivity_days: int = Field(default=90, ge=1)
    inactivity_monthly_retention: Decimal = Field(default=Decimal("0.97"), gt=0, le=1)
    retirements: Literal["EXCLUDE", "INCLUDE"] = "EXCLUDE"


class FormConfig(Contract):
    version: Identifier = "form-v1-candidate"
    half_life_days: Decimal = Field(default=Decimal(60), gt=0)
    prior_weight: Decimal = Field(default=Decimal(2), ge=0)
    window_days: int = Field(default=365, ge=1)


def expected_score(rating: Decimal, opponent: Decimal, scale: Decimal) -> Decimal:
    """Elo expectation ``1 / (1 + 10 ** ((opponent - rating) / scale))``."""
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return ONE / (ONE + TEN ** ((opponent - rating) / scale))


def k_factor(matches: int, config: EloConfig) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = PRECISION
        base = Decimal(matches) + config.k_offset
        return config.k_numerator / (base**config.k_shape)


def shrink_for_inactivity(
    rating: Decimal, last: datetime | None, at: datetime, config: EloConfig
) -> Decimal:
    """Pull a rating toward the initial value for each 30 days beyond the threshold."""
    if last is None:
        return rating
    idle = (at - last).days - config.inactivity_days
    if idle <= 0:
        return rating
    with localcontext() as ctx:
        ctx.prec = PRECISION
        retention = config.inactivity_monthly_retention ** (Decimal(idle) / Decimal(30))
        return config.initial + (rating - config.initial) * retention


@dataclass
class RatingEntry:
    rating: Decimal
    matches: int = 0
    last: datetime | None = None


@dataclass
class RatingTable:
    config: EloConfig
    entries: dict[UUID, RatingEntry] = field(default_factory=dict)

    def current(self, player_id: UUID, at: datetime) -> RatingEntry:
        entry = self.entries.get(player_id)
        if entry is None:
            return RatingEntry(self.config.initial)
        rating = shrink_for_inactivity(entry.rating, entry.last, at, self.config)
        return RatingEntry(rating, entry.matches, entry.last)

    def update(self, winner: UUID, loser: UUID, at: datetime) -> tuple[Decimal, Decimal]:
        """Apply one result. Returns the winner's and loser's pre-match expectations."""
        w, lo = self.current(winner, at), self.current(loser, at)
        expected_w = expected_score(w.rating, lo.rating, self.config.scale)
        expected_l = ONE - expected_w
        with localcontext() as ctx:
            ctx.prec = PRECISION
            new_w = w.rating + k_factor(w.matches, self.config) * (ONE - expected_w)
            new_l = lo.rating - k_factor(lo.matches, self.config) * expected_l
        self.entries[winner] = RatingEntry(new_w, w.matches + 1, at)
        self.entries[loser] = RatingEntry(new_l, lo.matches + 1, at)
        return expected_w, expected_l


@dataclass(frozen=True)
class PlayedMatch:
    """One rated match from one player's view, with the pre-match global expectation."""

    ended_at: datetime
    opponent: UUID
    won: bool
    expected: Decimal
    surface: Surface


@dataclass
class RatingState:
    global_table: RatingTable
    surface_tables: dict[Surface, RatingTable]
    played: dict[UUID, list[PlayedMatch]]
    skipped: dict[str, int]
    rated: list[CompletedMatch]


def replay(view: AsOfView, config: EloConfig, *, exclude: UUID | None = None) -> RatingState:
    """Rebuild rating state from all matches completed before the cutoff."""
    state = RatingState(RatingTable(config), {}, {}, {}, [])
    for item in view.all_completed(exclude=exclude):
        status = item.result.status
        reason = None
        if status == MatchStatus.WALKOVER:
            reason = "walkover"
        elif status in {MatchStatus.RETIRED, MatchStatus.DEFAULTED} and (
            config.retirements == "EXCLUDE"
        ):
            reason = "retirement"
        if reason is not None:
            state.skipped[reason] = state.skipped.get(reason, 0) + 1
            continue
        winner = item.result.winner_id
        loser = next(player for player in item.match.player_ids if player != winner)
        at = item.ended_at
        expected_w, expected_l = state.global_table.update(winner, loser, at)
        surface = view.store.edition(item.match.edition_id).surface
        if surface != Surface.UNKNOWN:
            table = state.surface_tables.setdefault(surface, RatingTable(config))
            table.update(winner, loser, at)
        state.played.setdefault(winner, []).append(
            PlayedMatch(at, loser, True, expected_w, surface)
        )
        state.played.setdefault(loser, []).append(
            PlayedMatch(at, winner, False, expected_l, surface)
        )
        state.rated.append(item)
    return state


@dataclass(frozen=True)
class Form:
    value: Decimal | None
    effective_sample_size: Decimal
    matches: int


def form(played: list[PlayedMatch], at: datetime, config: FormConfig) -> Form:
    """Half-life weighted mean of ``won - expected``, shrunk toward zero.

    ``form = sum(w * residual) / (sum(w) + prior_weight)`` and the effective sample size is
    ``sum(w) ** 2 / sum(w ** 2)``. With no matches in the window, form is ``None``.
    """
    with localcontext() as ctx:
        ctx.prec = PRECISION
        total = weighted = squares = Decimal(0)
        count = 0
        for item in played:
            age = at - item.ended_at
            if age.days >= config.window_days:
                continue
            days = Decimal(age.days) + Decimal(age.seconds) / Decimal(86400)
            weight = Decimal("0.5") ** (days / config.half_life_days)
            residual = (ONE if item.won else Decimal(0)) - item.expected
            total += weight
            weighted += weight * residual
            squares += weight * weight
            count += 1
        if count == 0:
            return Form(None, Decimal(0), 0)
        return Form(weighted / (total + config.prior_weight), total * total / squares, count)
