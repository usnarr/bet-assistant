"""F11.4 stacker on chronological out-of-fold component probabilities.

``p = sigmoid(sum_i w_i * logit(p_i))`` with no intercept, so the stack is player-swap
symmetric when its components are. A missing component contributes ``logit = 0`` (no
information) in fitting and in prediction alike. The weights are an L2-penalized logistic
fit on out-of-fold rows only.

Leakage check: every out-of-fold probability must come from a component fitted at or before
the row's cutoff, on rows that did not include the row's match. An in-sample row raises
``StackingLeakage``; it is never silently dropped.
"""

import math
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

import numpy as np
from pydantic import Field

from tennis_engine.common.contracts import Contract, Digest, Identifier, Probability, Timestamp
from tennis_engine.features.contracts import digest

WEIGHT_QUANTUM = Decimal("1e-9")
PROBABILITY_QUANTUM = Decimal("1e-9")
CLIP = 1e-6


class StackingLeakage(ValueError):
    pass


class OutOfFoldRow(Contract):
    match_id: UUID
    as_of: Timestamp
    outcome: Literal[0, 1]
    probabilities: tuple[Probability | None, ...]
    fitted_at: tuple[Timestamp, ...]  # Training cutoff of each component's inner fit.
    in_sample: tuple[bool, ...]  # True if the component was fitted on this match.


class StackerArtifact(Contract):
    schema_version: Literal["1.0"] = "1.0"
    components: tuple[Identifier, ...]
    weights: tuple[Decimal, ...]
    l2: Decimal
    rows: Annotated[int, Field(ge=1, strict=True)]
    folds: Annotated[int, Field(ge=1, strict=True)]
    missing_share: tuple[Decimal, ...]
    training_cutoff: Timestamp
    artifact_sha256: Digest


def _logit(p: Decimal | None) -> float:
    if p is None:
        return 0.0
    value = min(max(float(p), CLIP), 1 - CLIP)
    return math.log(value / (1 - value))


def validate_out_of_fold(rows: Sequence[OutOfFoldRow], components: int) -> None:
    for row in rows:
        if len(row.probabilities) != components or len(row.fitted_at) != components:
            raise ValueError("Every out-of-fold row needs one value per component")
        if len(row.in_sample) != components:
            raise ValueError("Every out-of-fold row needs one sample flag per component")
        for index in range(components):
            if row.in_sample[index] or row.fitted_at[index] > row.as_of:
                raise StackingLeakage(
                    f"Component {index} saw match {row.match_id} or was fitted after its cutoff"
                )


def fit_stacker(
    rows: Sequence[OutOfFoldRow],
    *,
    components: Sequence[str],
    training_cutoff: datetime,
    folds: int,
    l2: Decimal = Decimal(1),
    max_iterations: int = 100,
) -> StackerArtifact:
    if not rows:
        raise ValueError("No out-of-fold rows; the stacker is BLOCKED")
    validate_out_of_fold(rows, len(components))
    for row in rows:
        if row.as_of > training_cutoff:
            raise StackingLeakage("An out-of-fold row is later than the training cutoff")
    x = np.array([[_logit(p) for p in row.probabilities] for row in rows], dtype=np.float64)
    y = np.array([row.outcome for row in rows], dtype=np.float64)
    penalty = float(l2)
    weights = np.zeros(len(components), dtype=np.float64)
    for _ in range(max_iterations):
        p = 1.0 / (1.0 + np.exp(-(x @ weights)))
        gradient = x.T @ (p - y) + penalty * weights
        hessian = (x.T * (p * (1 - p))) @ x + penalty * np.eye(len(components))
        step = np.linalg.solve(hessian, gradient)
        weights -= step
        if float(np.max(np.abs(step))) < 1e-10:
            break
    else:
        raise ValueError("The stacker fit did not converge; BLOCKED")
    missing = tuple(
        Decimal(sum(row.probabilities[index] is None for row in rows)) / len(rows)
        for index in range(len(components))
    )
    rounded = tuple(Decimal(repr(float(item))).quantize(WEIGHT_QUANTUM) for item in weights)
    body = {
        "components": list(components),
        "weights": [str(item) for item in rounded],
        "l2": str(l2),
        "rows": len(rows),
        "folds": folds,
        "cutoff": training_cutoff.isoformat(),
    }
    return StackerArtifact(
        components=tuple(components),
        weights=rounded,
        l2=l2,
        rows=len(rows),
        folds=folds,
        missing_share=tuple(item.quantize(WEIGHT_QUANTUM) for item in missing),
        training_cutoff=training_cutoff,
        artifact_sha256=digest(body),
    )


def stack(artifact: StackerArtifact, probabilities: Sequence[Decimal | None]) -> Decimal:
    if len(probabilities) != len(artifact.components):
        raise ValueError("One probability per component is required")
    score = sum(
        float(weight) * _logit(p) for weight, p in zip(artifact.weights, probabilities, strict=True)
    )
    return Decimal(repr(1.0 / (1.0 + math.exp(-score)))).quantize(PROBABILITY_QUANTUM)
