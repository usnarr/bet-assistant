"""Fit, compare and apply symmetric calibrators (F11.5, F11.6, F11.7).

Time rules:

- The calibration window starts after the base model's training cutoff, so the base model
  never saw a calibration row.
- The fit part ends at ``validation_start``. A fit row counts only if its label was
  observed by then. Validation rows use labels observed by ``window_end``.
- The method with the lowest validation log loss is refitted on the whole window.
- A prediction can use the calibrator only if its cutoff is at or after ``window_end``.

Missing samples, a hash mismatch or an in-window prediction raise ``ValueError``. The
caller must then abstain; it must not fall back to the raw probability silently.

An optional week-block bootstrap refits the selected method on resampled weeks of the
window. ``calibrate`` then maps the base bounds through every draw, so the spread also
reflects calibrator fit uncertainty (F11.6).
"""

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from math import ceil, floor

from tennis_engine.common.contracts import VersionRef
from tennis_engine.common.ids import stable_id
from tennis_engine.features.contracts import digest
from tennis_engine.features.labels import MatchLabel
from tennis_engine.models.baselines.contracts import (
    BaselinePrediction,
    SupportStatus,
    Uncertainty,
    UncertaintyMethod,
)
from tennis_engine.models.baselines.logit import fit, log_loss_term, sigmoid

from .contracts import (
    CalibratedPrediction,
    CalibrationMethod,
    CalibrationTrial,
    CalibratorArtifact,
    CalibratorBootstrap,
    CalibratorDraw,
    CalibratorSet,
    Knot,
)

PRECISION = 40
ONE = Decimal(1)
PROBABILITY_QUANTUM = Decimal("1e-9")
METRIC_QUANTUM = Decimal("1e-6")
SLOPE_QUANTUM = Decimal("1e-12")
PLATT_L2 = Decimal("1e-6")
CALIBRATED_LIMITATIONS = (
    "Base bootstrap spread mapped through the calibrator",
    "Excludes calibrator fit uncertainty",
    "Not a confidence interval for the true win probability",
)
BOOTSTRAPPED_LIMITATIONS = (
    "Base bootstrap bounds mapped through each calibrator week-block draw",
    "The level is the calibrator bootstrap level; the base spread has its own level",
    "Not a confidence interval for the true win probability",
)


@dataclass(frozen=True)
class Row:
    probability: Decimal
    outcome: int
    label_observed_at: datetime


def _logit(p: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return (p / (ONE - p)).ln()


def _clip(p: Decimal, floor: Decimal) -> Decimal:
    return min(max(p, floor), ONE - floor)


def fit_platt(rows: Sequence[Row], floor: Decimal) -> Decimal:
    """Slope ``a`` of ``sigmoid(a * logit(p))``; there is no intercept, so it is symmetric."""
    xs = [_logit(_clip(row.probability, floor)) for row in rows]
    result = fit(xs, [row.outcome for row in rows], l2=PLATT_L2, initial=ONE)
    if not result.converged or result.coefficient <= 0:
        raise ValueError("The Platt fit did not converge to a positive slope; BLOCKED")
    return result.coefficient.quantize(SLOPE_QUANTUM)


def fit_isotonic(rows: Sequence[Row], floor: Decimal) -> tuple[Knot, ...]:
    """Pool-adjacent-violators on the rows and their mirror images ``(1 - p, 1 - y)``.

    The mirrored data are symmetric, and the isotonic solution is unique, so the fitted
    step function is symmetric too. Equal raw probabilities are pooled first.
    """
    points: dict[Decimal, list[int]] = {}
    for row in rows:
        for p, y in ((row.probability, row.outcome), (ONE - row.probability, 1 - row.outcome)):
            points.setdefault(p, []).append(y)
    blocks: list[tuple[Decimal, Decimal, int]] = []  # weighted raw sum, outcome sum, weight
    with localcontext() as ctx:
        ctx.prec = PRECISION
        for p in sorted(points):
            outcomes = points[p]
            blocks.append((p * len(outcomes), Decimal(sum(outcomes)), len(outcomes)))
            while len(blocks) > 1 and (
                blocks[-2][1] / blocks[-2][2] >= blocks[-1][1] / blocks[-1][2]
            ):
                right = blocks.pop()
                left = blocks.pop()
                blocks.append((left[0] + right[0], left[1] + right[1], left[2] + right[2]))
        knots = [
            Knot(
                raw=(raw / weight).quantize(PROBABILITY_QUANTUM),
                calibrated=_clip(outcome / weight, floor).quantize(PROBABILITY_QUANTUM),
            )
            for raw, outcome, weight in blocks
        ]
    unique: list[Knot] = []
    for knot in knots:
        if unique and knot.raw <= unique[-1].raw:
            continue
        unique.append(knot)
    if len(unique) < 2:
        raise ValueError("The isotonic fit has fewer than two knots; BLOCKED")
    return tuple(unique)


def _apply_isotonic(knots: Sequence[Knot], p: Decimal) -> Decimal:
    if p <= knots[0].raw:
        return knots[0].calibrated
    if p >= knots[-1].raw:
        return knots[-1].calibrated
    with localcontext() as ctx:
        ctx.prec = PRECISION
        for left, right in zip(knots, knots[1:], strict=False):
            if left.raw <= p <= right.raw:
                share = (p - left.raw) / (right.raw - left.raw)
                return left.calibrated + share * (right.calibrated - left.calibrated)
    raise AssertionError("unreachable")


def _map(
    method: CalibrationMethod,
    slope: Decimal | None,
    knots: Sequence[Knot],
    floor: Decimal,
    p: Decimal,
) -> Decimal:
    if method == CalibrationMethod.PLATT_SYMMETRIC:
        if slope is None:
            raise ValueError("A Platt calibrator needs a slope")
        with localcontext() as ctx:
            ctx.prec = PRECISION
            value = sigmoid(slope * _logit(_clip(p, floor)))
    else:
        value = _apply_isotonic(knots, p)
    return _clip(value, floor).quantize(PROBABILITY_QUANTUM)


def apply(artifact: CalibratorArtifact, p: Decimal) -> Decimal:
    return _map(artifact.method, artifact.slope, artifact.knots, artifact.min_probability, p)


def _metrics(pairs: Sequence[tuple[Decimal, int]], floor: Decimal) -> tuple[Decimal, Decimal]:
    with localcontext() as ctx:
        ctx.prec = PRECISION
        n = Decimal(len(pairs))
        loss = sum((log_loss_term(_clip(p, floor), y) for p, y in pairs), Decimal(0)) / n
        brier = sum(((p - y) ** 2 for p, y in pairs), Decimal(0)) / n
    return loss.quantize(METRIC_QUANTUM), brier.quantize(METRIC_QUANTUM)


def _rows(
    pairs: Sequence[tuple[BaselinePrediction, MatchLabel]],
    *,
    window_start: datetime,
    window_end: datetime,
) -> tuple[list[tuple[datetime, Row]], BaselinePrediction]:
    if not pairs:
        raise ValueError("No calibration rows; the calibrator is BLOCKED")
    reference = pairs[0][0]
    seen: set[object] = set()
    rows: list[tuple[datetime, Row]] = []
    for prediction, label in pairs:
        if prediction.match_id != label.match_id:
            raise ValueError("Prediction and label refer to different matches")
        if prediction.match_id in seen:
            raise ValueError("A match appears twice; orientations must stay grouped")
        seen.add(prediction.match_id)
        if (prediction.model, prediction.artifact_sha256, prediction.feature_set) != (
            reference.model,
            reference.artifact_sha256,
            reference.feature_set,
        ):
            raise ValueError("Calibration rows must come from one base artifact")
        if not window_start <= prediction.as_of < window_end:
            raise ValueError("A calibration row lies outside the calibration window")
        if label.observed_at <= prediction.as_of:
            raise ValueError("A label known at prediction time is not out-of-sample")
        if prediction.support != SupportStatus.SUPPORTED:
            continue
        p = prediction.probability_player_one
        if p is None:
            raise ValueError("A supported prediction lacks a probability")
        rows.append(
            (prediction.as_of, Row(p, int(label.player_one_won), label.observed_at)),
        )
    if reference.training_cutoff >= window_start:
        raise ValueError("The calibration window overlaps the base model's training data")
    return sorted(rows, key=lambda item: item[0]), reference


def _fit(
    method: CalibrationMethod, rows: Sequence[Row], floor: Decimal
) -> tuple[Decimal | None, tuple[Knot, ...]]:
    if method == CalibrationMethod.PLATT_SYMMETRIC:
        return fit_platt(rows, floor), ()
    return None, fit_isotonic(rows, floor)


def _quantile(values: Sequence[Decimal], level: Decimal) -> Decimal:
    ordered = sorted(values)
    position = level * (len(ordered) - 1)
    return ordered[floor(position)] if level < Decimal("0.5") else ordered[ceil(position)]


def _bootstrap(
    method: CalibrationMethod,
    rows: Sequence[tuple[datetime, Row]],
    floor_probability: Decimal,
    *,
    draws: int,
    seed: int,
    level: Decimal,
) -> CalibratorBootstrap:
    """Refit ``method`` on resampled ISO weeks of the prediction cutoffs."""
    blocks: dict[tuple[int, int], list[Row]] = {}
    for at, row in rows:
        year, week, _ = at.isocalendar()
        blocks.setdefault((year, week), []).append(row)
    keys = sorted(blocks)
    if len(keys) < 2 or draws < 2:
        raise ValueError("A calibrator bootstrap needs two weeks and two draws; BLOCKED")
    generator = random.Random(seed)
    kept: list[CalibratorDraw] = []
    for _ in range(draws):
        chosen = [keys[generator.randrange(len(keys))] for _ in keys]
        sample = [row for key in chosen for row in blocks[key]]
        try:
            slope, knots = _fit(method, sample, floor_probability)
        except ValueError:
            continue  # Counted below as a failed draw.
        kept.append(CalibratorDraw(slope=slope, knots=knots))
    if len(kept) < 2:
        raise ValueError("Fewer than two calibrator bootstrap draws converged; BLOCKED")
    return CalibratorBootstrap(
        draws=tuple(kept),
        requested=draws,
        failed=draws - len(kept),
        blocks=len(keys),
        seed=seed,
        level=level,
    )


def fit_calibrator(
    pairs: Sequence[tuple[BaselinePrediction, MatchLabel]],
    *,
    window_start: datetime,
    validation_start: datetime,
    window_end: datetime,
    version: str,
    min_fit_rows: int = 50,
    min_validation_rows: int = 30,
    min_probability: Decimal = Decimal("0.01"),
    methods: Sequence[CalibrationMethod] = tuple(CalibrationMethod),
    segment: str = "all",
    bootstrap_draws: int = 0,
    bootstrap_seed: int = 20261001,
    bootstrap_level: Decimal = Decimal("0.8"),
) -> CalibratorArtifact:
    """Compare the methods on the validation part, then refit the best on the window.

    ``bootstrap_draws`` above zero adds a week-block bootstrap of the selected method.
    """
    if not window_start < validation_start < window_end:
        raise ValueError("The validation part must lie inside the window, after the fit part")
    ordered, reference = _rows(pairs, window_start=window_start, window_end=window_end)
    fit_rows = [
        row
        for at, row in ordered
        if at < validation_start and row.label_observed_at <= validation_start
    ]
    validation_rows = [
        row for at, row in ordered if at >= validation_start and row.label_observed_at <= window_end
    ]
    if len(fit_rows) < min_fit_rows or len(validation_rows) < min_validation_rows:
        raise ValueError(
            f"Calibration needs {min_fit_rows} fit and {min_validation_rows} validation "
            f"rows, got {len(fit_rows)} and {len(validation_rows)}; BLOCKED"
        )
    raw = _metrics([(row.probability, row.outcome) for row in validation_rows], min_probability)
    trials: list[CalibrationTrial] = []
    order = sorted(methods, key=lambda item: item != CalibrationMethod.PLATT_SYMMETRIC)
    for method in order:
        slope, knots = _fit(method, fit_rows, min_probability)
        scored = [
            (_map(method, slope, knots, min_probability, row.probability), row.outcome)
            for row in validation_rows
        ]
        loss, brier = _metrics(scored, min_probability)
        trials.append(
            CalibrationTrial(
                method=method,
                fit_rows=len(fit_rows),
                validation_rows=len(validation_rows),
                raw_log_loss=raw[0],
                raw_brier=raw[1],
                log_loss=loss,
                brier=brier,
            )
        )
    best = min(trials, key=lambda trial: trial.log_loss)
    final = [(at, row) for at, row in ordered if row.label_observed_at <= window_end]
    final_rows = [row for _, row in final]
    slope, knots = _fit(best.method, final_rows, min_probability)
    bootstrap = (
        _bootstrap(
            best.method,
            final,
            min_probability,
            draws=bootstrap_draws,
            seed=bootstrap_seed,
            level=bootstrap_level,
        )
        if bootstrap_draws
        else None
    )
    body: dict[str, object] = {
        "version": version,
        "method": best.method.value,
        "base": reference.artifact_sha256,
        "window": [window_start.isoformat(), validation_start.isoformat(), window_end.isoformat()],
        "slope": None if slope is None else str(slope),
        "knots": [[str(knot.raw), str(knot.calibrated)] for knot in knots],
        "floor": str(min_probability),
        "trials": [trial.model_dump(mode="json") for trial in trials],
        "rows": len(final_rows),
    }
    # Optional parts enter the hash only when used, so older artifacts keep their hash.
    if segment != "all":
        body["segment"] = segment
    if bootstrap is not None:
        body["bootstrap"] = bootstrap.model_dump(mode="json")
    sha = digest(body)
    suffix = "" if segment == "all" else f"-{segment}"
    return CalibratorArtifact(
        calibrator_id=stable_id("calibrator", sha),
        name=f"{reference.model}-calibrator{suffix}",
        version=version,
        method=best.method,
        base_model=reference.model,
        base_model_version=reference.model_version,
        base_artifact_sha256=reference.artifact_sha256,
        base_training_cutoff=reference.training_cutoff,
        window_start=window_start,
        validation_start=validation_start,
        window_end=window_end,
        rows=len(final_rows),
        slope=slope,
        knots=knots,
        min_probability=min_probability,
        trials=tuple(trials),
        segment=segment,
        bootstrap=bootstrap,
        artifact_sha256=sha,
    )


def fit_calibrator_set(
    pairs: Sequence[tuple[BaselinePrediction, MatchLabel]],
    *,
    segment_of: Callable[[BaselinePrediction], str],
    window_start: datetime,
    validation_start: datetime,
    window_end: datetime,
    version: str,
    min_fit_rows: int = 50,
    min_validation_rows: int = 30,
    min_probability: Decimal = Decimal("0.01"),
    methods: Sequence[CalibrationMethod] = tuple(CalibrationMethod),
    bootstrap_draws: int = 0,
    bootstrap_seed: int = 20261001,
    bootstrap_level: Decimal = Decimal("0.8"),
) -> CalibratorSet:
    """Fit a pooled calibrator and a calibrator per tour that has enough rows (F11.5).

    ``segment_of`` gives the tour of a prediction, known before the match. A tour below
    the row minimums, or whose fit fails, is skipped with its reason and uses the pooled
    calibrator. The pooled calibrator must fit, or the set is BLOCKED.
    """

    def fit_one(
        rows: Sequence[tuple[BaselinePrediction, MatchLabel]], segment: str
    ) -> CalibratorArtifact:
        return fit_calibrator(
            rows,
            window_start=window_start,
            validation_start=validation_start,
            window_end=window_end,
            version=version,
            min_fit_rows=min_fit_rows,
            min_validation_rows=min_validation_rows,
            min_probability=min_probability,
            methods=methods,
            segment=segment,
            bootstrap_draws=bootstrap_draws,
            bootstrap_seed=bootstrap_seed,
            bootstrap_level=bootstrap_level,
        )

    pooled = fit_one(pairs, "all")
    by_segment: dict[str, list[tuple[BaselinePrediction, MatchLabel]]] = {}
    for pair in pairs:
        by_segment.setdefault(segment_of(pair[0]), []).append(pair)
    segments: dict[str, CalibratorArtifact] = {}
    skipped: dict[str, str] = {}
    for segment in sorted(by_segment):
        if segment == "all":
            raise ValueError('"all" is reserved for the pooled calibrator')
        try:
            segments[segment] = fit_one(by_segment[segment], segment)
        except ValueError as error:
            skipped[segment] = str(error)
    body = {
        "version": version,
        "pooled": pooled.artifact_sha256,
        "segments": {key: value.artifact_sha256 for key, value in segments.items()},
        "skipped": skipped,
    }
    return CalibratorSet(
        name=f"{pooled.base_model}-calibrator-set",
        version=version,
        pooled=pooled,
        segments=segments,
        skipped=skipped,
        artifact_sha256=digest(body),
    )


def calibrate_with_set(
    prediction: BaselinePrediction,
    calibrators: CalibratorSet,
    *,
    segment: str,
    predicted_at: datetime,
) -> CalibratedPrediction:
    """Use the tour calibrator when the set has one, otherwise the pooled calibrator."""
    return calibrate(prediction, calibrators.for_segment(segment), predicted_at=predicted_at)


def calibrate(
    prediction: BaselinePrediction, artifact: CalibratorArtifact, *, predicted_at: datetime
) -> CalibratedPrediction:
    """Map a raw prediction and its spread. Raise if the bundle does not match."""
    if (prediction.model, prediction.artifact_sha256, prediction.training_cutoff) != (
        artifact.base_model,
        artifact.base_artifact_sha256,
        artifact.base_training_cutoff,
    ):
        raise ValueError("The calibrator belongs to a different base artifact")
    if prediction.as_of < artifact.window_end:
        raise ValueError("The prediction lies inside the calibration window")
    raw = prediction.probability_player_one
    central = None if raw is None else apply(artifact, raw)
    spread = prediction.uncertainty
    uncertainty = Uncertainty(method=UncertaintyMethod.NONE, limitations=CALIBRATED_LIMITATIONS)
    bootstrap = artifact.bootstrap
    if central is not None and raw is not None and bootstrap is not None:
        low, high = raw, raw
        if spread.method != UncertaintyMethod.NONE:
            if spread.lower is None or spread.upper is None:
                raise ValueError("A bootstrap spread lacks its bounds")
            low, high = spread.lower, spread.upper
        floor_probability = artifact.min_probability
        lows = [
            _map(artifact.method, draw.slope, draw.knots, floor_probability, low)
            for draw in bootstrap.draws
        ]
        highs = [
            _map(artifact.method, draw.slope, draw.knots, floor_probability, high)
            for draw in bootstrap.draws
        ]
        tail = (ONE - bootstrap.level) / 2
        uncertainty = Uncertainty(
            method=UncertaintyMethod.WEEK_BLOCK_BOOTSTRAP,
            level=bootstrap.level,
            lower=_quantile(lows, tail),
            upper=_quantile(highs, ONE - tail),
            draws=len(bootstrap.draws),
            limitations=BOOTSTRAPPED_LIMITATIONS,
        )
    elif central is not None and spread.method != UncertaintyMethod.NONE:
        if spread.lower is None or spread.upper is None:
            raise ValueError("A bootstrap spread lacks its bounds")
        uncertainty = Uncertainty(
            method=spread.method,
            level=spread.level,
            lower=apply(artifact, spread.lower),
            upper=apply(artifact, spread.upper),
            draws=spread.draws,
            limitations=CALIBRATED_LIMITATIONS,
        )
    return CalibratedPrediction(
        match_id=prediction.match_id,
        player_ids=prediction.player_ids,
        model=prediction.model,
        model_version=prediction.model_version,
        artifact_sha256=prediction.artifact_sha256,
        calibrator=VersionRef(
            component=artifact.name, version=artifact.version, sha256=artifact.artifact_sha256
        ),
        feature_set=prediction.feature_set,
        snapshot_sha256=prediction.snapshot_sha256,
        as_of=prediction.as_of,
        predicted_at=predicted_at,
        training_cutoff=prediction.training_cutoff,
        calibration_cutoff=artifact.window_end,
        support=prediction.support,
        reasons=prediction.reasons,
        raw_probability_player_one=raw,
        probability_player_one=central,
        uncertainty=uncertainty,
    )


def is_symmetric(artifact: CalibratorArtifact, p: Decimal, tolerance: Decimal) -> bool:
    return abs(apply(artifact, p) + apply(artifact, ONE - p) - ONE) <= tolerance
