"""F13 evaluation contracts: gate statuses, frozen configuration and scored predictions.

A required configuration value that is unset makes a run ``BLOCKED``. Missing or skipped
evidence never becomes ``PASS``.
"""

import json
from collections.abc import Iterable
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from tennis_engine.common.contracts import Contract, Digest, Identifier, Probability, Timestamp
from tennis_engine.features.contracts import digest
from tennis_engine.models.baselines.contracts import SupportStatus


class GateStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    INCONCLUSIVE = "INCONCLUSIVE"


_ORDER = (GateStatus.FAIL, GateStatus.BLOCKED, GateStatus.INCONCLUSIVE, GateStatus.PASS)


def combine(statuses: Iterable[GateStatus]) -> GateStatus:
    """The worst status wins. No evidence at all is ``BLOCKED``, never ``PASS``."""
    found = set(statuses)
    if not found:
        return GateStatus.BLOCKED
    return next(status for status in _ORDER if status in found)


REQUIRED_SETTINGS = (
    "confidence_level",
    "bootstrap_draws",
    "bootstrap_seed",
    "min_test_rows",
    "min_blocks",
    "min_segment_rows",
    "consensus_calibration_margin",
    "calibration_slope_bounds",
    "calibration_intercept_limit",
    "min_winning_fold_fraction",
    "candidate_search_budget",
    "minimum_roi_lower_bound",
    "frozen_at",
)


class EvaluationConfig(Contract):
    """Frozen study settings. Change a value only with a reason and a new version."""

    schema_version: Literal["1.0"] = "1.0"
    version: Identifier
    reason: Annotated[str, Field(min_length=1)]
    primary_metric: Literal["log_loss"] = "log_loss"
    baseline: Identifier = "baseline-surface-elo"
    confidence_level: Annotated[Decimal, Field(gt=0, lt=1)] | None = None
    bootstrap_draws: Annotated[int, Field(ge=1, strict=True)] | None = None
    bootstrap_seed: int | None = None
    min_test_rows: Annotated[int, Field(ge=1, strict=True)] | None = None
    min_blocks: Annotated[int, Field(ge=2, strict=True)] | None = None
    min_segment_rows: Annotated[int, Field(ge=1, strict=True)] | None = None
    consensus_calibration_margin: Annotated[Decimal, Field(ge=0)] | None = None
    calibration_slope_bounds: tuple[Decimal, Decimal] | None = None
    calibration_intercept_limit: Annotated[Decimal, Field(ge=0)] | None = None
    min_winning_fold_fraction: Annotated[Decimal, Field(ge=0, le=1)] | None = None
    candidate_search_budget: Annotated[int, Field(ge=1, strict=True)] | None = None
    minimum_roi_lower_bound: Decimal | None = None
    segment_keys: tuple[Identifier, ...] = ("tour", "surface", "cutoff")
    frozen_at: Timestamp | None = None

    @model_validator(mode="after")
    def ordered_bounds(self) -> Self:
        bounds = self.calibration_slope_bounds
        if bounds is not None and not Decimal(0) < bounds[0] < Decimal(1) < bounds[1]:
            raise ValueError("Calibration slope bounds must satisfy 0 < low < 1 < high")
        return self

    def missing(self) -> tuple[str, ...]:
        return tuple(name for name in REQUIRED_SETTINGS if getattr(self, name) is None)

    @property
    def sha256(self) -> str:
        return digest(self.model_dump(mode="json"))


def load_config(path: Path) -> EvaluationConfig:
    return EvaluationConfig.model_validate(json.loads(path.read_text(encoding="utf-8")))


class ScoredPrediction(Contract):
    """One out-of-sample prediction for canonical player one, with its evaluation label.

    ``outcome`` is ``None`` when no scorable label exists (no result yet, or a walkover).
    """

    schema_version: Literal["1.0"] = "1.0"
    model: Identifier
    model_version: Identifier
    artifact_sha256: Digest | None
    fold: Annotated[int, Field(ge=0, strict=True)]
    training_cutoff: Timestamp
    match_id: UUID
    as_of: Timestamp
    snapshot_sha256: Digest
    feature_set: Identifier
    support: SupportStatus
    reasons: tuple[Identifier, ...] = ()
    probability_player_one: Probability | None
    lower: Probability | None = None
    upper: Probability | None = None
    outcome: Literal[0, 1] | None
    label_version: int | None
    label_observed_at: Timestamp | None
    block: Identifier
    tags: dict[Identifier, str]

    @model_validator(mode="after")
    def out_of_sample(self) -> Self:
        if (self.support == SupportStatus.UNSUPPORTED) != (self.probability_player_one is None):
            raise ValueError("Only an unsupported prediction lacks a probability")
        if self.training_cutoff > self.as_of:
            raise ValueError("Training data must end at or before the prediction cutoff")
        if self.label_observed_at is not None and self.label_observed_at <= self.as_of:
            raise ValueError("A label known at prediction time is not out-of-sample")
        return self

    @property
    def key(self) -> tuple[UUID, str]:
        return self.match_id, self.as_of.isoformat()

    @property
    def scorable(self) -> bool:
        return self.probability_player_one is not None and self.outcome is not None
