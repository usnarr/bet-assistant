"""Chronological inner folds inside one training period (F11.1, F11.2, F11.4).

Tuning and stacking use the same inner folds. A match belongs to the fold of its earliest
cutoff, so every cutoff of one match stays together. Inner fold ``k`` validates the
matches that start in its block. It trains only on rows with a cutoff before the block
start and a label observed by then, so no inner model sees its validation labels.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from tennis_engine.models.baselines.baseline import TrainingRow


@dataclass(frozen=True)
class InnerFold:
    index: int
    cutoff: datetime  # First cutoff of the validation block.
    train: tuple[TrainingRow, ...]
    validate: tuple[TrainingRow, ...]


def inner_folds(
    rows: Sequence[TrainingRow], *, folds: int, warmup_fraction: float = 0.4
) -> tuple[InnerFold, ...]:
    """Split the matches after a warm-up share into ``folds`` equal validation blocks."""
    if folds < 1 or not 0 < warmup_fraction < 1:
        raise ValueError("Inner folds need at least one fold and a warm-up share in (0, 1)")
    first: dict[UUID, datetime] = {}
    for row in rows:
        at = row.snapshot.as_of
        match_id = row.snapshot.match_id
        first[match_id] = min(first.get(match_id, at), at)
    ordered = sorted(first, key=lambda match_id: (first[match_id], str(match_id)))
    start = int(len(ordered) * warmup_fraction)
    size = (len(ordered) - start) // folds
    if start < 1 or size < 1:
        raise ValueError(f"{len(ordered)} matches cannot form {folds} inner folds; BLOCKED")
    result = []
    for index in range(folds):
        low = start + index * size
        high = len(ordered) if index == folds - 1 else low + size
        members = set(ordered[low:high])
        cutoff = first[ordered[low]]
        train = tuple(
            row
            for row in rows
            if row.snapshot.as_of < cutoff
            and row.label.observed_at <= cutoff
            and first[row.snapshot.match_id] < cutoff
        )
        validate = tuple(row for row in rows if row.snapshot.match_id in members)
        if not train or not validate:
            raise ValueError(f"Inner fold {index} has no training or validation rows; BLOCKED")
        result.append(InnerFold(index, cutoff, train, validate))
    return tuple(result)
