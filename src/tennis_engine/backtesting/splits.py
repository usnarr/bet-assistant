"""Expanding walk-forward splits grouped by match (F13 evaluation design).

Fold ``k`` tests the rows whose match starts its first cutoff in ``[start_k, end_k)``.
Its training cutoff is ``start_k``. Every cutoff and orientation of one match belongs to
one fold. A training row must have its snapshot cutoff and its label observation at or
before the training cutoff, and must not belong to the tested fold or a later fold.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from tennis_engine.common.contracts import Contract, Digest, Identifier, Timestamp
from tennis_engine.common.ids import stable_id
from tennis_engine.features.contracts import FeatureSnapshot, digest
from tennis_engine.models.baselines.baseline import TrainingRow

WARMUP = -1


class SplitError(ValueError):
    """A split or fit would mix a match across folds or use data after its cutoff."""


class Fold(Contract):
    index: Annotated[int, Field(ge=0, strict=True)]
    training_cutoff: Timestamp
    test_start: Timestamp
    test_end: Timestamp

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if not self.training_cutoff <= self.test_start < self.test_end:
            raise ValueError("A fold needs training_cutoff <= test_start < test_end")
        return self


class RowAssignment(Contract):
    match_id: UUID
    as_of: Timestamp
    snapshot_sha256: Digest
    fold: int


class SplitManifest(Contract):
    schema_version: Literal["1.0"] = "1.0"
    split_id: UUID
    name: Identifier
    folds: tuple[Fold, ...]
    rows: tuple[RowAssignment, ...]
    excluded_after_last_fold: int
    content_sha256: Digest

    def fold_of(self, match_id: UUID) -> int | None:
        return next((row.fold for row in self.rows if row.match_id == match_id), None)

    def test_rows(self, fold: int) -> tuple[RowAssignment, ...]:
        return tuple(row for row in self.rows if row.fold == fold)


def walk_forward(
    snapshots: Sequence[FeatureSnapshot], boundaries: Sequence[datetime], *, name: str
) -> SplitManifest:
    """Folds between consecutive ``boundaries``. Rows before the first are warm-up only."""
    if len(boundaries) < 2 or list(boundaries) != sorted(set(boundaries)):
        raise SplitError("Boundaries must be at least two strictly increasing times")
    folds = tuple(
        Fold(index=index, training_cutoff=start, test_start=start, test_end=end)
        for index, (start, end) in enumerate(zip(boundaries, boundaries[1:], strict=False))
    )
    first: dict[UUID, datetime] = {}
    for snapshot in snapshots:
        seen = first.get(snapshot.match_id)
        if seen is None or snapshot.as_of < seen:
            first[snapshot.match_id] = snapshot.as_of
    rows: list[RowAssignment] = []
    excluded = 0
    for snapshot in sorted(snapshots, key=lambda item: (item.as_of, str(item.match_id))):
        start = first[snapshot.match_id]
        fold = WARMUP if start < folds[0].test_start else None
        for item in folds:
            if item.test_start <= start < item.test_end:
                fold = item.index
        if fold is None:
            excluded += 1
            continue
        rows.append(
            RowAssignment(
                match_id=snapshot.match_id,
                as_of=snapshot.as_of,
                snapshot_sha256=snapshot.snapshot_sha256,
                fold=fold,
            )
        )
    body = {
        "name": name,
        "folds": [item.model_dump(mode="json") for item in folds],
        "rows": [row.model_dump(mode="json") for row in rows],
    }
    content = digest(body)
    manifest = SplitManifest(
        split_id=stable_id("split", f"{name}:{content}"),
        name=name,
        folds=folds,
        rows=tuple(rows),
        excluded_after_last_fold=excluded,
        content_sha256=content,
    )
    validate_split(manifest)
    return manifest


def validate_split(manifest: SplitManifest) -> None:
    """Reject a manifest that puts cutoffs or orientations of one match in two folds."""
    folds: dict[UUID, int] = {}
    for row in manifest.rows:
        if folds.setdefault(row.match_id, row.fold) != row.fold:
            raise SplitError(
                f"Match {row.match_id} appears in folds {folds[row.match_id]} and {row.fold}"
            )
    for row in manifest.rows:
        if row.fold != WARMUP and not any(item.index == row.fold for item in manifest.folds):
            raise SplitError(f"Row assigned to unknown fold {row.fold}")


def validate_fit(manifest: SplitManifest, fold: Fold, rows: Sequence[TrainingRow]) -> None:
    """Reject training rows from the tested period, later folds or later observations."""
    folds = {item.match_id: item.fold for item in manifest.rows}
    for row in rows:
        assigned = folds.get(row.snapshot.match_id)
        if assigned is None:
            raise SplitError("A training row is not in the split manifest")
        if assigned >= fold.index:
            raise SplitError("A training row belongs to the tested fold or a later fold")
        if row.snapshot.as_of > fold.training_cutoff:
            raise SplitError("A training snapshot is after the training cutoff")
        if row.label.observed_at > fold.training_cutoff:
            raise SplitError("A training label was observed after the training cutoff")
