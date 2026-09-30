"""Ranking, global Elo and surface Elo baselines (F09.1, F09.2, F09.6, F09.7).

Each baseline is ``p1 = sigmoid(beta * sign * x)`` on one ``diff.*`` feature, fitted only
on rows whose snapshot cutoff and label observation are both at or before the training
cutoff. Unsupported inputs return ``UNSUPPORTED`` without a probability; nothing is filled.
"""

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from math import ceil, floor
from uuid import UUID

from tennis_engine.common.ids import stable_id
from tennis_engine.features.contracts import FeatureSnapshot, digest
from tennis_engine.features.labels import MatchLabel

from .contracts import (
    BaselineArtifact,
    BaselineKind,
    BaselinePrediction,
    SupportStatus,
    Uncertainty,
    UncertaintyMethod,
)
from .logit import fit, sigmoid

COEFFICIENT_QUANTUM = Decimal("1e-12")
PROBABILITY_QUANTUM = Decimal("1e-9")
LN10 = Decimal(10).ln()


@dataclass(frozen=True)
class BaselineSpec:
    kind: BaselineKind
    feature: str
    sign: int
    support_features: tuple[str, str] | None
    initial: Decimal
    l2: Decimal

    @property
    def name(self) -> str:
        return f"baseline-{self.kind.lower().replace('_', '-')}"


SPECS = {
    BaselineKind.RANKING: BaselineSpec(
        BaselineKind.RANKING, "diff.log_rank", -1, None, Decimal(0), Decimal(1)
    ),
    BaselineKind.GLOBAL_ELO: BaselineSpec(
        BaselineKind.GLOBAL_ELO,
        "diff.elo",
        1,
        ("p1.elo_matches", "p2.elo_matches"),
        LN10 / Decimal(400),
        Decimal(1),
    ),
    BaselineKind.SURFACE_ELO: BaselineSpec(
        BaselineKind.SURFACE_ELO,
        "diff.surface_elo",
        1,
        ("p1.surface_elo_matches", "p2.surface_elo_matches"),
        LN10 / Decimal(400),
        Decimal(1),
    ),
}
DEFAULT_LIMITATIONS = (
    "Spread of week-block bootstrap refits of one coefficient only",
    "Not a confidence interval for the true win probability",
    "Ignores feature noise, rating uncertainty and sparse-player effects",
)


@dataclass(frozen=True)
class TrainingRow:
    snapshot: FeatureSnapshot
    label: MatchLabel


def _x(spec: BaselineSpec, snapshot: FeatureSnapshot) -> Decimal | None:
    value = snapshot.values.get(spec.feature)
    if not isinstance(value, Decimal):
        return None
    return value * spec.sign


def _support(
    spec: BaselineSpec,
    snapshot: FeatureSnapshot,
    *,
    min_support: int,
    supported_best_of: tuple[str, ...],
) -> tuple[SupportStatus, tuple[str, ...]]:
    reasons: list[str] = []
    if snapshot.values.get("match.best_of") not in supported_best_of:
        reasons.append("unsupported_format")
    if _x(spec, snapshot) is None:
        reasons.append(f"missing_{spec.feature.replace('diff.', '')}")
    if reasons:
        return SupportStatus.UNSUPPORTED, tuple(reasons)
    if spec.support_features is not None:
        counts = [snapshot.values.get(name) for name in spec.support_features]
        if any(not isinstance(item, int) or item < min_support for item in counts):
            return SupportStatus.SPARSE, ("sparse_rating_history",)
    return SupportStatus.SUPPORTED, ()


def _week(snapshot: FeatureSnapshot) -> tuple[int, int]:
    year, week, _ = snapshot.as_of.isocalendar()
    return year, week


def train(
    rows: Sequence[TrainingRow],
    kind: BaselineKind,
    *,
    training_cutoff: datetime,
    version: str,
    dataset_id: UUID | None = None,
    min_support_matches: int = 5,
    supported_best_of: tuple[str, ...] = ("BEST_OF_3",),
    bootstrap_draws: int = 200,
    bootstrap_level: Decimal = Decimal("0.9"),
    seed: int = 20260929,
) -> BaselineArtifact:
    spec = SPECS[kind]
    usable: list[TrainingRow] = []
    for row in rows:
        if row.snapshot.as_of > training_cutoff or row.label.observed_at > training_cutoff:
            raise ValueError("Training rows must be known at or before the training cutoff")
        if row.snapshot.match_id != row.label.match_id:
            raise ValueError("Snapshot and label refer to different matches")
        status, _ = _support(spec, row.snapshot, min_support=0, supported_best_of=supported_best_of)
        if status != SupportStatus.UNSUPPORTED:
            usable.append(row)
    if not usable:
        raise ValueError("No supported training rows; the baseline is BLOCKED")
    feature_sets = {(row.snapshot.feature_set, row.snapshot.feature_set_sha256) for row in usable}
    if len(feature_sets) != 1:
        raise ValueError("Training rows must share one feature-set version")
    ((feature_set, feature_sha),) = feature_sets
    usable.sort(key=lambda row: (row.snapshot.as_of, str(row.snapshot.match_id)))
    xs: list[Decimal] = []
    for row in usable:
        x = _x(spec, row.snapshot)
        if x is None:
            raise ValueError("A supported row lost its feature value")
        xs.append(x)
    ys = [int(row.label.player_one_won) for row in usable]
    main = fit(xs, ys, l2=spec.l2, initial=spec.initial)
    blocks: dict[tuple[int, int], list[int]] = {}
    for index, row in enumerate(usable):
        blocks.setdefault(_week(row.snapshot), []).append(index)
    keys = sorted(blocks)
    generator = random.Random(seed)
    draws: list[Decimal] = []
    for _ in range(bootstrap_draws if len(keys) > 1 else 0):
        chosen = [keys[generator.randrange(len(keys))] for _ in keys]
        indexes = [index for key in chosen for index in blocks[key]]
        refit = fit(
            [xs[i] for i in indexes],
            [ys[i] for i in indexes],
            l2=spec.l2,
            initial=main.coefficient,
        )
        if refit.converged:
            draws.append(refit.coefficient.quantize(COEFFICIENT_QUANTUM))
    coefficient = main.coefficient.quantize(COEFFICIENT_QUANTUM)
    body = {
        "kind": kind.value,
        "version": version,
        "coefficient": str(coefficient),
        "draws": [str(item) for item in draws],
        "feature_set": feature_set,
        "feature_set_sha256": feature_sha,
        "cutoff": training_cutoff.isoformat(),
        "rows": [row.snapshot.snapshot_sha256 for row in usable],
        "seed": seed,
    }
    sha = digest(body)
    return BaselineArtifact(
        model_id=stable_id("baseline-model", sha),
        name=spec.name,
        version=version,
        kind=kind,
        feature=spec.feature,
        feature_sign=1 if spec.sign > 0 else -1,
        coefficient=coefficient,
        l2_penalty=spec.l2,
        bootstrap_coefficients=tuple(draws),
        bootstrap_level=bootstrap_level,
        min_support_matches=min_support_matches,
        supported_best_of=supported_best_of,
        feature_set=feature_set,
        feature_set_sha256=feature_sha,
        dataset_id=dataset_id,
        training_cutoff=training_cutoff,
        training_rows=len(usable),
        converged=main.converged,
        seed=seed,
        artifact_sha256=sha,
    )


def _quantile(values: list[Decimal], level: Decimal) -> Decimal:
    ordered = sorted(values)
    position = level * (len(ordered) - 1)
    return ordered[floor(position)] if level < Decimal("0.5") else ordered[ceil(position)]


def predict(
    snapshot: FeatureSnapshot, artifact: BaselineArtifact, *, predicted_at: datetime
) -> BaselinePrediction:
    spec = SPECS[artifact.kind]
    if (snapshot.feature_set, snapshot.feature_set_sha256) != (
        artifact.feature_set,
        artifact.feature_set_sha256,
    ):
        raise ValueError("Snapshot feature set differs from the model's feature set")
    status, reasons = _support(
        spec,
        snapshot,
        min_support=artifact.min_support_matches,
        supported_best_of=artifact.supported_best_of,
    )
    x = _x(spec, snapshot)
    components: dict[str, Decimal | int | None] = {spec.feature: x * spec.sign if x else x}
    if spec.support_features:
        for name in spec.support_features:
            value = snapshot.values.get(name)
            components[name] = value if isinstance(value, int) else None
    probability = None
    uncertainty = Uncertainty(method=UncertaintyMethod.NONE, limitations=DEFAULT_LIMITATIONS)
    if status != SupportStatus.UNSUPPORTED and x is not None:
        with localcontext() as ctx:
            ctx.prec = 40
            probability = sigmoid(artifact.coefficient * x).quantize(PROBABILITY_QUANTUM)
            draws = [sigmoid(beta * x) for beta in artifact.bootstrap_coefficients]
        if len(draws) >= 2:
            tail = (Decimal(1) - artifact.bootstrap_level) / Decimal(2)
            uncertainty = Uncertainty(
                method=UncertaintyMethod.WEEK_BLOCK_BOOTSTRAP,
                level=artifact.bootstrap_level,
                lower=_quantile(draws, tail).quantize(PROBABILITY_QUANTUM),
                upper=_quantile(draws, Decimal(1) - tail).quantize(PROBABILITY_QUANTUM),
                draws=len(draws),
                limitations=DEFAULT_LIMITATIONS,
            )
    return BaselinePrediction(
        match_id=snapshot.match_id,
        player_ids=snapshot.player_ids,
        model=artifact.name,
        model_version=artifact.version,
        artifact_sha256=artifact.artifact_sha256,
        feature_set=snapshot.feature_set,
        snapshot_sha256=snapshot.snapshot_sha256,
        as_of=snapshot.as_of,
        predicted_at=predicted_at,
        training_cutoff=artifact.training_cutoff,
        support=status,
        reasons=reasons,
        probability_player_one=probability,
        components=components,
        uncertainty=uncertainty,
    )
