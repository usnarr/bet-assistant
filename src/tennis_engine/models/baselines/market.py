"""Proportional de-vig and market-consensus baseline (F09.3–F09.5, SYS-09).

A pair is two OPEN quotes from one bookmaker, for one match and market, one per player,
both observed by the cutoff and within ``max_pair_gap`` of each other. Quotes from
different bookmakers are never combined into a synthetic pair. Displayed odds are kept
unchanged in the components; consensus probabilities are separate derived values.
"""

from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from tennis_engine.common.contracts import Contract, Identifier, Probability, Timestamp
from tennis_engine.contracts.domain import Market
from tennis_engine.ingestion.bookmakers.contracts import CanonicalQuote, QuoteState

from .contracts import SupportStatus
from .logit import sigmoid

ONE = Decimal(1)
QUANTUM = Decimal("1e-9")


class ConsensusConfig(Contract):
    version: Identifier = "consensus-v1-candidate"
    max_pair_gap: timedelta = timedelta(seconds=60)
    max_age: timedelta = timedelta(minutes=30)
    freshness_half_life: timedelta = timedelta(minutes=10)
    bookmaker_weights: dict[Identifier, Decimal] = Field(default_factory=dict)
    default_weight: Decimal = Decimal(1)
    normalization_tolerance: Decimal = Decimal("1e-18")


class DevigPair(Contract):
    bookmaker: Identifier
    match_id: UUID
    odds: tuple[Decimal, Decimal]
    probabilities: tuple[Probability, Probability]
    overround: Decimal
    observed_at: Timestamp
    quote_ids: tuple[UUID, UUID]


class ConsensusOutput(Contract):
    schema_version: Literal["1.0"] = "1.0"
    match_id: UUID
    player_ids: tuple[UUID, UUID]
    as_of: Timestamp
    method: Identifier
    support: SupportStatus
    reasons: tuple[Identifier, ...] = ()
    probability_player_one: Probability | None
    pairs: tuple[DevigPair, ...]
    weights: tuple[Decimal, ...]
    rejected: dict[Identifier, int]

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.probability_player_one is None) != (self.support == SupportStatus.UNSUPPORTED):
            raise ValueError("Only an unsupported consensus lacks a probability")
        if len(self.weights) != len(self.pairs):
            raise ValueError("Every pair needs one weight")
        return self


class PairError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def devig(
    first: CanonicalQuote,
    second: CanonicalQuote,
    player_ids: tuple[UUID, UUID],
    config: ConsensusConfig,
) -> DevigPair:
    """Proportional de-vig of one bookmaker pair, oriented to ``player_ids``."""
    if first.bookmaker != second.bookmaker:
        raise PairError("cross_bookmaker_pair")
    if first.match_id != second.match_id or first.market != second.market:
        raise PairError("mismatched_pair")
    if first.market != Market.MATCH_WINNER:
        raise PairError("unsupported_market")
    if first.state != QuoteState.OPEN or second.state != QuoteState.OPEN:
        raise PairError("suspended_or_closed")
    by_player = {first.selection_player_id: first, second.selection_player_id: second}
    if set(by_player) != set(player_ids):
        raise PairError("incomplete_pair")
    if abs(first.observed_at - second.observed_at) > config.max_pair_gap:
        raise PairError("pair_not_contemporaneous")
    one, two = by_player[player_ids[0]], by_player[player_ids[1]]
    if min(one.decimal_odds, two.decimal_odds) <= ONE:
        raise PairError("invalid_odds")
    with localcontext() as ctx:
        ctx.prec = 40
        implied = (ONE / one.decimal_odds, ONE / two.decimal_odds)
        total = implied[0] + implied[1]
        p1 = implied[0] / total
        p2 = ONE - p1
    if abs(p1 + p2 - ONE) > config.normalization_tolerance:
        raise PairError("normalization_failed")
    return DevigPair(
        bookmaker=one.bookmaker,
        match_id=one.match_id,
        odds=(one.decimal_odds, two.decimal_odds),
        probabilities=(p1, p2),
        overround=total - ONE,
        observed_at=max(one.observed_at, two.observed_at),
        quote_ids=(one.quote_id, two.quote_id),
    )


def _latest(
    quotes: Iterable[CanonicalQuote], match_id: UUID, as_of: datetime
) -> dict[tuple[str, UUID], CanonicalQuote]:
    latest: dict[tuple[str, UUID], CanonicalQuote] = {}
    for quote in quotes:
        if quote.match_id != match_id or quote.observed_at > as_of:
            continue
        key = (quote.bookmaker, quote.selection_player_id)
        current = latest.get(key)
        if current is None or (quote.observed_at, str(quote.quote_id)) > (
            current.observed_at,
            str(current.quote_id),
        ):
            latest[key] = quote
    return latest


def consensus(
    quotes: Iterable[CanonicalQuote],
    *,
    match_id: UUID,
    player_ids: tuple[UUID, UUID],
    as_of: datetime,
    config: ConsensusConfig | None = None,
) -> ConsensusOutput:
    """Freshness-weighted mean of de-vig probabilities in logit space."""
    config = config or ConsensusConfig()
    latest = _latest(quotes, match_id, as_of)
    rejected: dict[str, int] = {}
    pairs: list[DevigPair] = []
    weights: list[Decimal] = []
    for bookmaker in sorted({key[0] for key in latest}):
        first = latest.get((bookmaker, player_ids[0]))
        second = latest.get((bookmaker, player_ids[1]))
        try:
            if first is None or second is None:
                raise PairError("incomplete_pair")
            pair = devig(first, second, player_ids, config)
            if as_of - pair.observed_at > config.max_age:
                raise PairError("stale_pair")
        except PairError as error:
            rejected[error.reason] = rejected.get(error.reason, 0) + 1
            continue
        with localcontext() as ctx:
            ctx.prec = 40
            age = Decimal((as_of - pair.observed_at).total_seconds())
            half = Decimal(config.freshness_half_life.total_seconds())
            base = config.bookmaker_weights.get(bookmaker, config.default_weight)
            weights.append(base * Decimal("0.5") ** (age / half))
        pairs.append(pair)
    probability = None
    reasons: tuple[str, ...] = ()
    support = SupportStatus.SUPPORTED
    total_weight = sum(weights, Decimal(0))
    if not pairs or total_weight <= 0:
        support, reasons = SupportStatus.UNSUPPORTED, ("no_valid_pair",)
    else:
        with localcontext() as ctx:
            ctx.prec = 40
            mean = (
                sum(
                    (
                        w * (p.probabilities[0] / p.probabilities[1]).ln()
                        for w, p in zip(weights, pairs, strict=True)
                    ),
                    Decimal(0),
                )
                / total_weight
            )
            probability = sigmoid(mean).quantize(QUANTUM)
        if len(pairs) == 1:
            support, reasons = SupportStatus.SPARSE, ("single_bookmaker",)
    return ConsensusOutput(
        match_id=match_id,
        player_ids=player_ids,
        as_of=as_of,
        method=config.version,
        support=support,
        reasons=reasons,
        probability_player_one=probability,
        pairs=tuple(pairs),
        weights=tuple(item.quantize(QUANTUM) for item in weights),
        rejected=dict(sorted(rejected.items())),
    )
