"""Walk-forward baseline runner (F13.1).

Each fold refits every candidate on the rows allowed by ``validate_fit`` and predicts the
fold's frozen snapshots. Snapshots are immutable inputs, so a rerun with the same inputs
gives the same ``content_sha256``. A fit that raises ``ValueError`` is recorded as
``BLOCKED`` and its fold predictions are ``UNSUPPORTED``; nothing is filled.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import Field

from tennis_engine.common.contracts import Contract, Digest, Identifier
from tennis_engine.common.ids import stable_id
from tennis_engine.features.contracts import FeatureSnapshot, digest
from tennis_engine.features.labels import MatchLabel, final_label, label_known_at
from tennis_engine.models.baselines.baseline import SPECS, TrainingRow, predict, train
from tennis_engine.models.baselines.contracts import BaselineArtifact, BaselineKind, SupportStatus
from tennis_engine.models.point import model as point
from tennis_engine.models.point.formats import MatchFormat
from tennis_engine.normalization.contracts import MatchStatus
from tennis_engine.normalization.store import IdentityStore

from .contracts import ScoredPrediction
from .splits import WARMUP, SplitError, SplitManifest, validate_fit

SCORABLE = frozenset({MatchStatus.COMPLETED, MatchStatus.RETIRED, MatchStatus.DEFAULTED})


@dataclass(frozen=True)
class Output:
    support: SupportStatus
    reasons: tuple[str, ...]
    probability: Decimal | None
    lower: Decimal | None = None
    upper: Decimal | None = None


class Fitted(Protocol):
    @property
    def version(self) -> str: ...

    @property
    def artifact_sha256(self) -> str: ...

    def predict(self, snapshot: FeatureSnapshot) -> Output: ...


class Candidate(Protocol):
    @property
    def name(self) -> str: ...

    def fit(self, rows: Sequence[TrainingRow], training_cutoff: datetime) -> Fitted: ...


@dataclass(frozen=True)
class _FittedBaseline:
    artifact: BaselineArtifact

    @property
    def version(self) -> str:
        return self.artifact.version

    @property
    def artifact_sha256(self) -> str:
        return self.artifact.artifact_sha256

    def predict(self, snapshot: FeatureSnapshot) -> Output:
        result = predict(snapshot, self.artifact, predicted_at=snapshot.as_of)
        return Output(
            result.support,
            result.reasons,
            result.probability_player_one,
            result.uncertainty.lower,
            result.uncertainty.upper,
        )


@dataclass(frozen=True)
class BaselineCandidate:
    kind: BaselineKind
    version: str = "v1"
    bootstrap_draws: int = 50
    seed: int = 20260929

    @property
    def name(self) -> str:
        return SPECS[self.kind].name

    def fit(self, rows: Sequence[TrainingRow], training_cutoff: datetime) -> Fitted:
        return _FittedBaseline(
            train(
                rows,
                self.kind,
                training_cutoff=training_cutoff,
                version=self.version,
                bootstrap_draws=self.bootstrap_draws,
                seed=self.seed,
            )
        )


@dataclass(frozen=True)
class _FittedPoint:
    store: IdentityStore
    artifact: point.PointModelArtifact
    formats: Callable[[UUID], MatchFormat | None]

    @property
    def version(self) -> str:
        return self.artifact.version

    @property
    def artifact_sha256(self) -> str:
        return self.artifact.artifact_sha256

    def predict(self, snapshot: FeatureSnapshot) -> Output:
        result = point.predict(
            self.store,
            snapshot.match_id,
            self.artifact,
            as_of=snapshot.as_of,
            fmt=self.formats(snapshot.match_id),
        )
        p, low, high = result.probability_player_one, result.draw_lower, result.draw_upper
        if snapshot.player_ids != result.player_ids:
            flip = Decimal(1)
            p = None if p is None else flip - p
            low, high = (
                (None if high is None else flip - high),
                (None if low is None else flip - low),
            )
        return Output(result.support, result.reasons, p, low, high)


@dataclass(frozen=True)
class PointCandidate:
    """F10 point model. It fits on serve counts known at the cutoff, not on ``rows``."""

    store: IdentityStore
    formats: Callable[[UUID], MatchFormat | None]
    config: point.PointModelConfig = field(default_factory=point.PointModelConfig)
    name: str = "point-serve-return"

    def fit(self, rows: Sequence[TrainingRow], training_cutoff: datetime) -> Fitted:
        artifact = point.fit(self.store, training_cutoff=training_cutoff, config=self.config)
        return _FittedPoint(self.store, artifact, self.formats)


class FitRecord(Contract):
    model: Identifier
    fold: Annotated[int, Field(ge=0, strict=True)]
    status: Literal["FITTED", "BLOCKED"]
    training_rows: Annotated[int, Field(ge=0, strict=True)]
    artifact_sha256: Digest | None
    reason: str | None = None


class RunResult(Contract):
    schema_version: Literal["1.0"] = "1.0"
    run_id: UUID
    name: Identifier
    split_id: UUID
    split_sha256: Digest
    dataset_id: UUID | None
    models: tuple[Identifier, ...]
    fits: tuple[FitRecord, ...]
    predictions: tuple[ScoredPrediction, ...]
    content_sha256: Digest

    def for_model(self, model: str) -> tuple[ScoredPrediction, ...]:
        return tuple(item for item in self.predictions if item.model == model)


def block_of(at: datetime) -> str:
    """Tournament-week block: ISO week of the snapshot cutoff (UTC)."""
    year, week, _ = at.isocalendar()
    return f"{year}-w{week:02d}"


def default_tags(snapshot: FeatureSnapshot) -> dict[str, str]:
    tags = {}
    for key in ("tour", "surface", "best_of"):
        value = snapshot.values.get(f"match.{key}")
        tags[key] = str(value).lower() if value is not None else "unknown"
    return tags


def _scored_label(label: MatchLabel | None) -> MatchLabel | None:
    return label if label is not None and label.status in SCORABLE else None


def run_walk_forward(
    store: IdentityStore,
    snapshots: Sequence[FeatureSnapshot],
    split: SplitManifest,
    candidates: Sequence[Candidate],
    *,
    name: str,
    dataset_id: UUID | None = None,
    tagger: Callable[[FeatureSnapshot], dict[str, str]] = default_tags,
) -> RunResult:
    by_sha = {item.snapshot_sha256: item for item in snapshots}
    missing = [row for row in split.rows if row.snapshot_sha256 not in by_sha]
    if missing:
        raise SplitError(f"{len(missing)} split rows have no frozen snapshot")
    names = [candidate.name for candidate in candidates]
    if len(set(names)) != len(names):
        raise ValueError("Candidate names must be unique")
    finals: dict[UUID, MatchLabel | None] = {}
    fits: list[FitRecord] = []
    predictions: list[ScoredPrediction] = []
    for fold in split.folds:
        rows = []
        for row in split.rows:
            if row.fold >= fold.index and row.fold != WARMUP:
                continue
            if row.as_of > fold.training_cutoff:
                continue
            label = _scored_label(label_known_at(store, row.match_id, fold.training_cutoff))
            if label is not None:
                rows.append(TrainingRow(by_sha[row.snapshot_sha256], label))
        validate_fit(split, fold, rows)
        tests = [by_sha[row.snapshot_sha256] for row in split.test_rows(fold.index)]
        for candidate in candidates:
            try:
                fitted: Fitted | None = candidate.fit(rows, fold.training_cutoff)
            except ValueError as error:
                fitted = None
                fits.append(
                    FitRecord(
                        model=candidate.name,
                        fold=fold.index,
                        status="BLOCKED",
                        training_rows=len(rows),
                        artifact_sha256=None,
                        reason=str(error),
                    )
                )
            else:
                assert fitted is not None
                fits.append(
                    FitRecord(
                        model=candidate.name,
                        fold=fold.index,
                        status="FITTED",
                        training_rows=len(rows),
                        artifact_sha256=fitted.artifact_sha256,
                    )
                )
            for snapshot in tests:
                if snapshot.match_id not in finals:
                    finals[snapshot.match_id] = final_label(store, snapshot.match_id)
                label = finals[snapshot.match_id]
                scored = _scored_label(label)
                output = (
                    fitted.predict(snapshot)
                    if fitted is not None
                    else Output(SupportStatus.UNSUPPORTED, ("fit_blocked",), None)
                )
                outcome: Literal[0, 1] | None = None
                if scored is not None:
                    # The label is for canonical player one; a swapped snapshot flips it.
                    canonical = store.match(snapshot.match_id).player_ids
                    won = scored.player_one_won
                    if snapshot.player_ids[0] != canonical[0]:
                        won = not won
                    outcome = 1 if won else 0
                tags = tagger(snapshot)
                tags["result"] = label.status.value.lower() if label is not None else "none"
                predictions.append(
                    ScoredPrediction(
                        model=candidate.name,
                        model_version=fitted.version if fitted else "unfitted",
                        artifact_sha256=fitted.artifact_sha256 if fitted else None,
                        fold=fold.index,
                        training_cutoff=fold.training_cutoff,
                        match_id=snapshot.match_id,
                        as_of=snapshot.as_of,
                        snapshot_sha256=snapshot.snapshot_sha256,
                        feature_set=snapshot.feature_set,
                        support=output.support,
                        reasons=output.reasons,
                        probability_player_one=output.probability,
                        lower=output.lower,
                        upper=output.upper,
                        outcome=outcome,
                        label_version=label.result_version if label else None,
                        label_observed_at=label.observed_at if label else None,
                        block=block_of(snapshot.as_of),
                        tags=tags,
                    )
                )
    body = {
        "name": name,
        "split": split.content_sha256,
        "dataset": str(dataset_id) if dataset_id else None,
        "fits": [item.model_dump(mode="json") for item in fits],
        "predictions": [item.model_dump(mode="json") for item in predictions],
    }
    content = digest(body)
    return RunResult(
        run_id=stable_id("evaluation-run", f"{name}:{content}"),
        name=name,
        split_id=split.split_id,
        split_sha256=split.content_sha256,
        dataset_id=dataset_id,
        models=tuple(names),
        fits=tuple(fits),
        predictions=tuple(predictions),
        content_sha256=content,
    )
