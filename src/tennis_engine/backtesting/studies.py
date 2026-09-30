"""Reusable studies on the harness: feature-set ablations (F08.8) and model comparisons.

An ablation runs one candidate on each feature set over the same match cutoffs and
compares each set with the reference set on matched rows. Different feature sets give
different snapshot hashes, so rows match by match and cutoff, and must share a label.
"""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from tennis_engine.common.contracts import Contract, Identifier
from tennis_engine.features.contracts import AvailabilityMode
from tennis_engine.features.dataset import build_dataset
from tennis_engine.features.snapshots import FeatureSet
from tennis_engine.normalization.store import IdentityStore

from .bootstrap import BootstrapInterval, block_bootstrap
from .contracts import ScoredPrediction
from .metrics import loss
from .runner import Candidate, RunResult, run_walk_forward
from .splits import walk_forward


class AblationResult(Contract):
    reference: Identifier
    feature_set: Identifier
    model: Identifier
    matched_rows: int
    difference: BootstrapInterval | None


def matched_by_cutoff(
    candidate: Sequence[ScoredPrediction], reference: Sequence[ScoredPrediction]
) -> list[tuple[ScoredPrediction, ScoredPrediction]]:
    index = {item.key: item for item in reference if item.scorable}
    pairs = []
    for item in candidate:
        other = index.get(item.key)
        if item.scorable and other is not None:
            if other.outcome != item.outcome:
                raise ValueError("Matched rows must share one label")
            pairs.append((item, other))
    return sorted(pairs, key=lambda pair: (pair[0].as_of, str(pair[0].match_id)))


def ablation(
    store: IdentityStore,
    rows: Sequence[tuple[UUID, datetime]],
    feature_sets: Sequence[FeatureSet],
    candidate: Candidate,
    boundaries: Sequence[datetime],
    *,
    name: str,
    created_at: datetime,
    code_revision: str,
    draws: int,
    seed: int,
    level: Decimal,
    mode: AvailabilityMode = AvailabilityMode.PROSPECTIVE,
) -> tuple[dict[str, RunResult], tuple[AblationResult, ...]]:
    """The first feature set is the reference. Differences are ``set - reference`` log loss."""
    runs: dict[str, RunResult] = {}
    for feature_set in feature_sets:
        manifest, snapshots = build_dataset(
            store,
            feature_set,
            rows,
            name=f"{name}-{feature_set.version}",
            mode=mode,
            cutoff_rule="caller rows",
            created_at=created_at,
            code_revision=code_revision,
            source_versions={},
        )
        split = walk_forward(snapshots, boundaries, name=f"{name}-{feature_set.version}")
        runs[feature_set.version] = run_walk_forward(
            store,
            snapshots,
            split,
            [candidate],
            name=f"{name}-{feature_set.version}",
            dataset_id=manifest.dataset_id,
        )
    reference = feature_sets[0].version
    base = runs[reference].for_model(candidate.name)
    results = []
    for feature_set in feature_sets[1:]:
        pairs = matched_by_cutoff(runs[feature_set.version].for_model(candidate.name), base)
        difference = (
            block_bootstrap(
                [(a.block, loss(a) - loss(b), Decimal(1)) for a, b in pairs],
                statistic="log_loss_difference",
                draws=draws,
                seed=seed,
                level=level,
            )
            if pairs
            else None
        )
        results.append(
            AblationResult(
                reference=reference,
                feature_set=feature_set.version,
                model=candidate.name,
                matched_rows=len(pairs),
                difference=difference,
            )
        )
    return runs, tuple(results)
