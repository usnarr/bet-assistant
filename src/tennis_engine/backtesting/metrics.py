"""Forecast metrics, segments and matched comparisons (F13.6, F13.8).

Log loss is primary, then Brier and calibration intercept/slope. Accuracy and AUC are
secondary and descriptive. Every segment shows its denominators; a segment with fewer
scorable rows than the frozen floor is ``INCONCLUSIVE``, not pooled into success.
"""

from collections.abc import Sequence
from decimal import Decimal, localcontext
from typing import Annotated
from uuid import UUID

from pydantic import Field

from tennis_engine.common.contracts import Contract, Identifier
from tennis_engine.models.baselines.contracts import SupportStatus
from tennis_engine.models.baselines.logit import log_loss_term
from tennis_engine.models.baselines.report import calibration_fit

from .bootstrap import BootstrapInterval, block_bootstrap
from .contracts import GateStatus, ScoredPrediction

QUANTUM = Decimal("1e-6")
EPSILON = Decimal("1e-15")


class ForecastMetrics(Contract):
    model: Identifier
    segment: Annotated[str, Field(min_length=1, max_length=128)]
    rows: Annotated[int, Field(ge=0, strict=True)]
    supported: Annotated[int, Field(ge=0, strict=True)]
    scorable: Annotated[int, Field(ge=0, strict=True)]
    coverage: Decimal | None
    log_loss: Decimal | None
    brier: Decimal | None
    calibration_intercept: Decimal | None
    calibration_slope: Decimal | None
    mean_probability: Decimal | None
    event_rate: Decimal | None
    accuracy: Decimal | None
    auc: Decimal | None
    status: GateStatus


def loss(prediction: ScoredPrediction) -> Decimal:
    if prediction.probability_player_one is None or prediction.outcome is None:
        raise ValueError("Only a scorable prediction has a loss")
    p = min(max(prediction.probability_player_one, EPSILON), Decimal(1) - EPSILON)
    with localcontext() as ctx:
        ctx.prec = 40
        return log_loss_term(p, prediction.outcome)


def auc(probabilities: Sequence[Decimal], outcomes: Sequence[int]) -> Decimal | None:
    """Rank AUC with average ranks for ties; ``None`` without both outcomes."""
    positives = sum(outcomes)
    negatives = len(outcomes) - positives
    if positives == 0 or negatives == 0:
        return None
    order = sorted(range(len(probabilities)), key=lambda i: probabilities[i])
    ranks = [Decimal(0)] * len(order)
    start = 0
    while start < len(order):
        end = start
        while end + 1 < len(order) and probabilities[order[end + 1]] == probabilities[order[start]]:
            end += 1
        average = Decimal(start + end + 2) / 2
        for position in range(start, end + 1):
            ranks[order[position]] = average
        start = end + 1
    rank_sum = sum((ranks[i] for i, y in enumerate(outcomes) if y == 1), Decimal(0))
    score = (rank_sum - Decimal(positives * (positives + 1)) / 2) / (positives * negatives)
    return score.quantize(QUANTUM)


def forecast_metrics(
    model: str, segment: str, predictions: Sequence[ScoredPrediction], *, min_rows: int
) -> ForecastMetrics:
    supported = [item for item in predictions if item.support != SupportStatus.UNSUPPORTED]
    scorable = [item for item in predictions if item.scorable]
    empty: dict[str, Decimal | None] = {
        "log_loss": None,
        "brier": None,
        "calibration_intercept": None,
        "calibration_slope": None,
        "mean_probability": None,
        "event_rate": None,
        "accuracy": None,
        "auc": None,
    }
    values = dict(empty)
    if scorable:
        ps = [item.probability_player_one or Decimal(0) for item in scorable]
        ys = [item.outcome or 0 for item in scorable]
        n = Decimal(len(scorable))
        with localcontext() as ctx:
            ctx.prec = 40
            values["log_loss"] = (sum((loss(item) for item in scorable), Decimal(0)) / n).quantize(
                QUANTUM
            )
            values["brier"] = (
                sum(((p - y) ** 2 for p, y in zip(ps, ys, strict=True)), Decimal(0)) / n
            ).quantize(QUANTUM)
            correct = sum(1 for p, y in zip(ps, ys, strict=True) if (p > Decimal("0.5")) == bool(y))
            values["accuracy"] = (Decimal(correct) / n).quantize(QUANTUM)
            values["mean_probability"] = (sum(ps, Decimal(0)) / n).quantize(QUANTUM)
            values["event_rate"] = (Decimal(sum(ys)) / n).quantize(QUANTUM)
        calibration = calibration_fit(ps, ys)
        if calibration is not None:
            values["calibration_intercept"] = calibration[0].quantize(QUANTUM)
            values["calibration_slope"] = calibration[1].quantize(QUANTUM)
        values["auc"] = auc(ps, ys)
    return ForecastMetrics(
        model=model,
        segment=segment,
        rows=len(predictions),
        supported=len(supported),
        scorable=len(scorable),
        coverage=(Decimal(len(supported)) / len(predictions)).quantize(QUANTUM)
        if predictions
        else None,
        status=GateStatus.PASS if len(scorable) >= min_rows else GateStatus.INCONCLUSIVE,
        **values,
    )


def segments(
    predictions: Sequence[ScoredPrediction], keys: Sequence[str]
) -> dict[str, list[ScoredPrediction]]:
    groups: dict[str, list[ScoredPrediction]] = {"all": list(predictions)}
    for item in predictions:
        groups.setdefault(f"fold={item.fold}", []).append(item)
        for key in keys:
            if key in item.tags:
                groups.setdefault(f"{key}={item.tags[key]}", []).append(item)
    return groups


def segment_report(
    model: str, predictions: Sequence[ScoredPrediction], *, keys: Sequence[str], min_rows: int
) -> tuple[ForecastMetrics, ...]:
    return tuple(
        forecast_metrics(model, name, rows, min_rows=min_rows)
        for name, rows in sorted(segments(predictions, keys).items())
    )


def matched(
    candidate: Sequence[ScoredPrediction], baseline: Sequence[ScoredPrediction]
) -> list[tuple[ScoredPrediction, ScoredPrediction]]:
    """Rows that both models score, keyed by match and cutoff, in chronological order."""
    index: dict[tuple[UUID, str], ScoredPrediction] = {
        item.key: item for item in baseline if item.scorable
    }
    pairs = []
    for item in candidate:
        other = index.get(item.key)
        if item.scorable and other is not None:
            if other.outcome != item.outcome or other.snapshot_sha256 != item.snapshot_sha256:
                raise ValueError("Matched rows must share one snapshot and one label")
            pairs.append((item, other))
    pairs.sort(key=lambda pair: (pair[0].as_of, str(pair[0].match_id)))
    return pairs


def paired_log_loss_difference(
    pairs: Sequence[tuple[ScoredPrediction, ScoredPrediction]],
    *,
    draws: int,
    seed: int,
    level: Decimal,
) -> BootstrapInterval:
    """``candidate - baseline`` mean log loss on matched rows; below zero favours the candidate."""
    return block_bootstrap(
        [(a.block, loss(a) - loss(b), Decimal(1)) for a, b in pairs],
        statistic="log_loss_difference",
        draws=draws,
        seed=seed,
        level=level,
    )
