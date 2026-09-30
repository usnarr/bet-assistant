"""Baseline report metrics and model cards (F09.7, MOD-01 inputs).

Primary metrics are log loss, Brier and calibration intercept/slope; accuracy is
secondary. Unsupported rows are counted, never dropped silently. F13 owns the full
chronological harness; this module only summarises stored out-of-sample predictions.
"""

from collections.abc import Sequence
from decimal import Decimal, localcontext

from tennis_engine.features.labels import MatchLabel

from .contracts import (
    BaselineArtifact,
    BaselinePrediction,
    ModelCard,
    SegmentMetrics,
    SupportStatus,
)
from .logit import log_loss_term, sigmoid

QUANTUM = Decimal("1e-6")
EPSILON = Decimal("1e-15")


def _logit(p: Decimal) -> Decimal:
    clipped = min(max(p, EPSILON), Decimal(1) - EPSILON)
    return (clipped / (Decimal(1) - clipped)).ln()


def calibration_fit(
    probabilities: Sequence[Decimal], outcomes: Sequence[int], iterations: int = 50
) -> tuple[Decimal, Decimal] | None:
    """Fit ``y ~ sigmoid(a + b * logit(p))`` by Newton-Raphson; ``None`` if degenerate."""
    if len(set(outcomes)) < 2 or len(set(probabilities)) < 2:
        return None
    with localcontext() as ctx:
        ctx.prec = 40
        xs = [_logit(p) for p in probabilities]
        a, b = Decimal(0), Decimal(1)
        for _ in range(iterations):
            ga = gb = haa = hab = hbb = Decimal(0)
            for x, y in zip(xs, outcomes, strict=True):
                p = sigmoid(a + b * x)
                w = p * (Decimal(1) - p)
                ga += p - y
                gb += (p - y) * x
                haa += w
                hab += w * x
                hbb += w * x * x
            det = haa * hbb - hab * hab
            if det == 0:
                return None
            da = (hbb * ga - hab * gb) / det
            db = (haa * gb - hab * ga) / det
            a, b = a - da, b - db
            if abs(da) < Decimal("1e-12") and abs(db) < Decimal("1e-12"):
                return a, b
    return None


def segment_metrics(
    segment: str, pairs: Sequence[tuple[BaselinePrediction, MatchLabel]]
) -> SegmentMetrics:
    rows = [
        (prediction.probability_player_one, int(label.player_one_won))
        for prediction, label in pairs
        if prediction.probability_player_one is not None
    ]
    if not rows:
        return SegmentMetrics(
            segment=segment,
            count=0,
            log_loss=None,
            brier=None,
            accuracy=None,
            calibration_intercept=None,
            calibration_slope=None,
            mean_probability=None,
            event_rate=None,
        )
    probabilities = [p for p, _ in rows if p is not None]
    outcomes = [y for _, y in rows]
    n = Decimal(len(rows))
    with localcontext() as ctx:
        ctx.prec = 40
        log_loss = (
            sum(
                (
                    log_loss_term(min(max(p, EPSILON), 1 - EPSILON), y)
                    for p, y in zip(probabilities, outcomes, strict=True)
                ),
                Decimal(0),
            )
            / n
        )
        brier = (
            sum(((p - y) ** 2 for p, y in zip(probabilities, outcomes, strict=True)), Decimal(0))
            / n
        )
        correct = sum(
            1
            for p, y in zip(probabilities, outcomes, strict=True)
            if (p > Decimal("0.5")) == bool(y)
        )
    calibration = calibration_fit(probabilities, outcomes)
    return SegmentMetrics(
        segment=segment,
        count=len(rows),
        log_loss=log_loss.quantize(QUANTUM),
        brier=brier.quantize(QUANTUM),
        accuracy=(Decimal(correct) / n).quantize(QUANTUM),
        calibration_intercept=calibration[0].quantize(QUANTUM) if calibration else None,
        calibration_slope=calibration[1].quantize(QUANTUM) if calibration else None,
        mean_probability=(sum(probabilities, Decimal(0)) / n).quantize(QUANTUM),
        event_rate=(Decimal(sum(outcomes)) / n).quantize(QUANTUM),
    )


def evaluate(
    pairs: Sequence[tuple[BaselinePrediction, MatchLabel, dict[str, str]]],
) -> tuple[tuple[SegmentMetrics, ...], int]:
    """Metrics overall and per ``tour``/``surface`` segment; also the unsupported count."""
    for prediction, label, _ in pairs:
        if prediction.match_id != label.match_id:
            raise ValueError("Prediction and label refer to different matches")
        if label.observed_at <= prediction.as_of:
            raise ValueError("A label known at prediction time is not out-of-sample")
    unsupported = sum(1 for p, _, _ in pairs if p.support == SupportStatus.UNSUPPORTED)
    segments: dict[str, list[tuple[BaselinePrediction, MatchLabel]]] = {"all": []}
    for prediction, label, tags in pairs:
        segments["all"].append((prediction, label))
        for key in ("tour", "surface"):
            if key in tags:
                segments.setdefault(f"{key}={tags[key]}", []).append((prediction, label))
    return tuple(
        segment_metrics(name, rows) for name, rows in sorted(segments.items())
    ), unsupported


def model_card(
    artifact: BaselineArtifact,
    *,
    evaluation: tuple[SegmentMetrics, ...],
    unsupported_rows: int,
    data_availability: str,
    code_revision: str,
    dependency_lock_sha256: str,
    rollback_target: str | None,
    license_notes: str,
    known_limitations: tuple[str, ...] = (),
) -> ModelCard:
    research = data_availability == "RESEARCH_ONLY"
    counted = next((item.count for item in evaluation if item.segment == "all"), 0)
    return ModelCard(
        model=artifact.name,
        model_version=artifact.version,
        artifact_sha256=artifact.artifact_sha256,
        status="RESEARCH_ONLY" if research else "SHADOW_CANDIDATE",
        supported_scope=(
            f"ATP/WTA singles, {', '.join(artifact.supported_best_of)}, pre-match match winner; "
            f"feature {artifact.feature}; sparse below {artifact.min_support_matches} matches"
        ),
        training_cutoff=artifact.training_cutoff,
        dataset_id=artifact.dataset_id,
        data_availability=data_availability.lower().replace("_", "-"),
        feature_set=artifact.feature_set,
        feature_set_sha256=artifact.feature_set_sha256,
        code_revision=code_revision,
        dependency_lock_sha256=dependency_lock_sha256,
        parameters={
            "coefficient": str(artifact.coefficient),
            "feature_sign": str(artifact.feature_sign),
            "l2_penalty": str(artifact.l2_penalty),
            "bootstrap_draws": str(len(artifact.bootstrap_coefficients)),
            "bootstrap_level": str(artifact.bootstrap_level),
            "seed": str(artifact.seed),
            "training_rows": str(artifact.training_rows),
        },
        evaluation=evaluation,
        evaluation_rows=counted,
        unsupported_rows=unsupported_rows,
        uncertainty_limitations=(
            "Week-block bootstrap spread of one refitted coefficient",
            "Not a guarantee or confidence interval for the true probability",
        ),
        known_limitations=(
            "Raw uncalibrated probability; calibration belongs to F11",
            "Not evidence of beating market prices",
            *known_limitations,
        ),
        rollback_target=rollback_target,
        license_notes=license_notes,
    )
