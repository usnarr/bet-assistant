"""Machine-readable promotion decisions (F13.9, source section 30 conditions).

Every gate is evaluated and recorded. The worst gate status is the decision status, so a
missing threshold, sample, replay, review or rollback target can never produce ``PASS``.
This module decides nothing about deployment: a ``PASS`` still needs the audited
champion switch outside this code.
"""

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID

from tennis_engine.common.contracts import Contract, Digest, Identifier, Timestamp
from tennis_engine.common.ids import stable_id
from tennis_engine.features.contracts import digest
from tennis_engine.models.baselines.report import calibration_fit

from .bootstrap import BootstrapInterval
from .contracts import EvaluationConfig, GateStatus, ScoredPrediction, combine
from .economics import EconomicSummary
from .metrics import loss, matched, paired_log_loss_difference, segment_report
from .runner import RunResult


class PromotionGate(StrEnum):
    CONFIG = "config_frozen_and_complete"
    SAMPLE = "sample_sufficient"
    BEATS_BASELINE = "beats_surface_elo_log_loss"
    CONSENSUS_CALIBRATION = "consensus_calibration_noninferior"
    SEVERE_CALIBRATION = "no_severe_calibration_defect"
    STABLE_PERIODS = "stable_across_folds"
    NO_TINY_SEGMENT = "no_single_week_dependency"
    ECONOMICS = "net_economics_positive"
    LEAKAGE = "no_leakage"
    REPRODUCIBLE = "reproducible_run"
    ROLLBACK = "rollback_artifact"
    REVIEW = "independent_reviewer_signoff"


STATISTICAL = (
    PromotionGate.SAMPLE,
    PromotionGate.BEATS_BASELINE,
    PromotionGate.CONSENSUS_CALIBRATION,
    PromotionGate.SEVERE_CALIBRATION,
    PromotionGate.STABLE_PERIODS,
    PromotionGate.NO_TINY_SEGMENT,
    PromotionGate.ECONOMICS,
)


class GateEvidence(Contract):
    gate: PromotionGate
    status: GateStatus
    detail: str


class Review(Contract):
    reviewer: Identifier
    reviewed_at: Timestamp
    approved: bool
    notes: str = ""


class ReleaseDecision(Contract):
    schema_version: Literal["1.0"] = "1.0"
    decision_id: UUID
    run_id: UUID
    run_sha256: Digest
    candidate: Identifier
    baseline: Identifier
    config_version: Identifier
    config_sha256: Digest
    status: GateStatus
    gates: tuple[GateEvidence, ...]
    comparison: BootstrapInterval | None
    author: Identifier
    reviewer: Identifier | None
    reviewed_at: Timestamp | None
    rollback_target: Identifier | None
    evaluated_at: Timestamp
    decided_at: Timestamp
    limitations: tuple[str, ...]
    content_sha256: Digest


LIMITATIONS = (
    "A PASS is not a guarantee of future profit",
    "Bootstrap intervals are for the listed statistic, not for true win probabilities",
    "Code deployment and model promotion stay separate",
)


def _deviation(pairs: Sequence[ScoredPrediction]) -> Decimal | None:
    fitted = calibration_fit(
        [item.probability_player_one or Decimal(0) for item in pairs],
        [item.outcome or 0 for item in pairs],
    )
    if fitted is None:
        return None
    return max(abs(fitted[0]), abs(fitted[1] - 1))


def _consensus_gate(
    run: RunResult, candidate: str, consensus: str | None, config: EvaluationConfig
) -> GateEvidence:
    gate = PromotionGate.CONSENSUS_CALIBRATION
    if consensus is None or consensus not in run.models:
        return GateEvidence(gate=gate, status=GateStatus.BLOCKED, detail="no consensus model")
    pairs = matched(run.for_model(candidate), run.for_model(consensus))
    ours = _deviation([a for a, _ in pairs])
    theirs = _deviation([b for _, b in pairs])
    if ours is None or theirs is None or config.consensus_calibration_margin is None:
        return GateEvidence(
            gate=gate, status=GateStatus.INCONCLUSIVE, detail="calibration fit is degenerate"
        )
    status = (
        GateStatus.PASS if ours <= theirs + config.consensus_calibration_margin else GateStatus.FAIL
    )
    return GateEvidence(
        gate=gate,
        status=status,
        detail=f"deviation {ours:.6f} vs consensus {theirs:.6f} on {len(pairs)} rows",
    )


def _severe_gate(predictions: Sequence[ScoredPrediction], config: EvaluationConfig) -> GateEvidence:
    gate = PromotionGate.SEVERE_CALIBRATION
    assert config.min_segment_rows is not None and config.calibration_slope_bounds is not None
    assert config.calibration_intercept_limit is not None
    report = segment_report(
        "candidate", predictions, keys=config.segment_keys, min_rows=config.min_segment_rows
    )
    low, high = config.calibration_slope_bounds
    breaches, sparse, degenerate = [], [], []
    for item in report:
        if item.status != GateStatus.PASS:
            sparse.append(item.segment)
            continue
        if item.calibration_slope is None or item.calibration_intercept is None:
            degenerate.append(item.segment)
            continue
        if not low <= item.calibration_slope <= high or (
            abs(item.calibration_intercept) > config.calibration_intercept_limit
        ):
            breaches.append(item.segment)
    detail = f"breaches={breaches} sparse={sparse} degenerate={degenerate}"
    if breaches:
        return GateEvidence(gate=gate, status=GateStatus.FAIL, detail=detail)
    if "all" in sparse or "all" in degenerate:
        return GateEvidence(gate=gate, status=GateStatus.INCONCLUSIVE, detail=detail)
    return GateEvidence(gate=gate, status=GateStatus.PASS, detail=detail)


def _stable_gate(
    pairs: Sequence[tuple[ScoredPrediction, ScoredPrediction]], config: EvaluationConfig
) -> GateEvidence:
    gate = PromotionGate.STABLE_PERIODS
    assert config.min_segment_rows is not None and config.min_winning_fold_fraction is not None
    by_fold: dict[int, list[Decimal]] = defaultdict(list)
    for a, b in pairs:
        by_fold[a.fold].append(loss(a) - loss(b))
    evaluable = {
        fold: sum(values, Decimal(0)) / len(values)
        for fold, values in by_fold.items()
        if len(values) >= config.min_segment_rows
    }
    if len(evaluable) < 2:
        return GateEvidence(
            gate=gate,
            status=GateStatus.INCONCLUSIVE,
            detail=f"{len(evaluable)} folds reach {config.min_segment_rows} matched rows",
        )
    wins = sum(1 for value in evaluable.values() if value < 0)
    fraction = Decimal(wins) / len(evaluable)
    status = GateStatus.PASS if fraction >= config.min_winning_fold_fraction else GateStatus.FAIL
    return GateEvidence(
        gate=gate, status=status, detail=f"candidate better in {wins} of {len(evaluable)} folds"
    )


def _tiny_gate(
    pairs: Sequence[tuple[ScoredPrediction, ScoredPrediction]], estimate: Decimal | None
) -> GateEvidence:
    """Drop the single most favourable week; the improvement must remain."""
    gate = PromotionGate.NO_TINY_SEGMENT
    blocks: dict[str, list[Decimal]] = defaultdict(list)
    for a, b in pairs:
        blocks[a.block].append(loss(a) - loss(b))
    if estimate is None or estimate >= 0 or len(blocks) < 2:
        return GateEvidence(
            gate=gate, status=GateStatus.INCONCLUSIVE, detail="no improvement to test"
        )
    best = min(blocks, key=lambda key: (sum(blocks[key], Decimal(0)), key))
    rest = [value for key, values in blocks.items() if key != best for value in values]
    remaining = sum(rest, Decimal(0)) / len(rest)
    status = GateStatus.PASS if remaining < 0 else GateStatus.FAIL
    return GateEvidence(
        gate=gate, status=status, detail=f"without {best}: mean difference {remaining:.6f}"
    )


def _economics_gate(economics: EconomicSummary | None, config: EvaluationConfig) -> GateEvidence:
    gate = PromotionGate.ECONOMICS
    if economics is None or not economics.execution_grade:
        return GateEvidence(
            gate=gate,
            status=GateStatus.BLOCKED,
            detail="no execution-grade replay; collect prospective shadow evidence",
        )
    interval = economics.roi_interval
    if interval is None or interval.lower is None:
        return GateEvidence(gate=gate, status=GateStatus.INCONCLUSIVE, detail="no ROI interval")
    assert config.minimum_roi_lower_bound is not None
    status = GateStatus.PASS if interval.lower > config.minimum_roi_lower_bound else GateStatus.FAIL
    return GateEvidence(
        gate=gate, status=status, detail=f"net ROI lower bound {interval.lower} at {interval.level}"
    )


def decide_promotion(
    run: RunResult,
    *,
    candidate: str,
    config: EvaluationConfig,
    author: str,
    evaluated_at: datetime,
    decided_at: datetime,
    consensus: str | None = None,
    economics: EconomicSummary | None = None,
    leakage_passed: bool | None = None,
    rerun_sha256: str | None = None,
    rollback_target: str | None = None,
    review: Review | None = None,
) -> ReleaseDecision:
    if candidate == config.baseline:
        raise ValueError("The candidate cannot be its own baseline")
    if candidate not in run.models or config.baseline not in run.models:
        raise ValueError("The run must contain the candidate and the baseline")
    gates: list[GateEvidence] = []
    missing = config.missing()
    comparison = None
    if missing:
        gates.append(
            GateEvidence(
                gate=PromotionGate.CONFIG, status=GateStatus.BLOCKED, detail=f"unset: {missing}"
            )
        )
        gates.extend(
            GateEvidence(gate=gate, status=GateStatus.BLOCKED, detail="configuration incomplete")
            for gate in STATISTICAL
        )
    else:
        assert config.frozen_at is not None
        frozen = config.frozen_at <= evaluated_at
        gates.append(
            GateEvidence(
                gate=PromotionGate.CONFIG,
                status=GateStatus.PASS if frozen else GateStatus.FAIL,
                detail="frozen before evaluation" if frozen else "changed after evaluation",
            )
        )
        assert config.bootstrap_draws is not None and config.bootstrap_seed is not None
        assert config.confidence_level is not None and config.min_test_rows is not None
        assert config.min_blocks is not None
        predictions = run.for_model(candidate)
        pairs = matched(predictions, run.for_model(config.baseline))
        blocks = len({a.block for a, _ in pairs})
        enough = len(pairs) >= config.min_test_rows and blocks >= config.min_blocks
        gates.append(
            GateEvidence(
                gate=PromotionGate.SAMPLE,
                status=GateStatus.PASS if enough else GateStatus.INCONCLUSIVE,
                detail=f"{len(pairs)} matched rows in {blocks} weeks",
            )
        )
        estimate = None
        if pairs:
            comparison = paired_log_loss_difference(
                pairs,
                draws=config.bootstrap_draws,
                seed=config.bootstrap_seed,
                level=config.confidence_level,
            )
            estimate = comparison.estimate
        if comparison is None or comparison.upper is None:
            status = GateStatus.INCONCLUSIVE
        elif comparison.upper < 0:
            status = GateStatus.PASS
        elif comparison.estimate >= 0:
            status = GateStatus.FAIL
        else:
            status = GateStatus.INCONCLUSIVE
        gates.append(
            GateEvidence(
                gate=PromotionGate.BEATS_BASELINE,
                status=status,
                detail="no matched rows"
                if comparison is None
                else f"difference {comparison.estimate} [{comparison.lower}, {comparison.upper}]",
            )
        )
        gates.append(_consensus_gate(run, candidate, consensus, config))
        gates.append(_severe_gate(predictions, config))
        gates.append(_stable_gate(pairs, config))
        gates.append(_tiny_gate(pairs, estimate))
        gates.append(_economics_gate(economics, config))
    gates.append(
        GateEvidence(
            gate=PromotionGate.LEAKAGE,
            status={None: GateStatus.BLOCKED, True: GateStatus.PASS, False: GateStatus.FAIL}[
                leakage_passed
            ],
            detail="leakage suite result"
            if leakage_passed is not None
            else "leakage suite not run",
        )
    )
    if rerun_sha256 is None:
        reproduced = GateStatus.BLOCKED
    else:
        reproduced = GateStatus.PASS if rerun_sha256 == run.content_sha256 else GateStatus.FAIL
    gates.append(
        GateEvidence(
            gate=PromotionGate.REPRODUCIBLE, status=reproduced, detail=f"rerun {rerun_sha256}"
        )
    )
    gates.append(
        GateEvidence(
            gate=PromotionGate.ROLLBACK,
            status=GateStatus.PASS if rollback_target else GateStatus.BLOCKED,
            detail=rollback_target or "no rollback target",
        )
    )
    if review is None:
        signed = GateEvidence(
            gate=PromotionGate.REVIEW, status=GateStatus.BLOCKED, detail="no review"
        )
    elif review.reviewer == author:
        signed = GateEvidence(
            gate=PromotionGate.REVIEW, status=GateStatus.FAIL, detail="author cannot self-approve"
        )
    else:
        signed = GateEvidence(
            gate=PromotionGate.REVIEW,
            status=GateStatus.PASS if review.approved else GateStatus.FAIL,
            detail=f"reviewed by {review.reviewer}",
        )
    gates.append(signed)
    status = combine(item.status for item in gates)
    body = {
        "run": run.content_sha256,
        "candidate": candidate,
        "config": config.sha256,
        "gates": [item.model_dump(mode="json") for item in gates],
        "review": review.model_dump(mode="json") if review else None,
        "rollback": rollback_target,
        "decided_at": decided_at.isoformat(),
    }
    content = digest(body)
    return ReleaseDecision(
        decision_id=stable_id("release-decision", content),
        run_id=run.run_id,
        run_sha256=run.content_sha256,
        candidate=candidate,
        baseline=config.baseline,
        config_version=config.version,
        config_sha256=config.sha256,
        status=status,
        gates=tuple(gates),
        comparison=comparison,
        author=author,
        reviewer=review.reviewer if review else None,
        reviewed_at=review.reviewed_at if review else None,
        rollback_target=rollback_target,
        evaluated_at=evaluated_at,
        decided_at=decided_at,
        limitations=LIMITATIONS,
        content_sha256=content,
    )
